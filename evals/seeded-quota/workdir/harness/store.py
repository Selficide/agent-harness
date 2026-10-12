"""The run store: run folders, atomic JSON writes, and the one-writer-per-run lock.

Layout (home is ~/.agent-harness, or $AGENT_HARNESS_HOME):
  config.json              thresholds and defaults (optional)
  quota-state.json         which quota thresholds have been announced, per provider and window
  runs/<run_id>/           request.json, prompt.md, events[-N].jsonl, stderr[-N].log, result[-N].md,
                           result.json and meta.json (worker-owned), label.json (label-owned), reply-N.md, lock
"""

import ctypes
import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path

DEFAULT_CONFIG = {
    "quota_notify_percent": [25, 50, 75],
    "quota_refuse_percent": 98,
    "default_model": {"codex": "gpt-6-luna"},
    "default_effort": {"review": "high", "research": "medium"},
    "default_timeout_s": 900,
    "inline_result_chars": 32000,
}


def home():
    return Path(os.environ.get("AGENT_HARNESS_HOME") or Path.home() / ".agent-harness")


def runs_dir():
    return home() / "runs"


def config():
    """DEFAULT_CONFIG overlaid with config.json, if present."""
    cfg = dict(DEFAULT_CONFIG)
    user = read_json(home() / "config.json")
    if isinstance(user, dict):
        cfg.update(user)
    return cfg


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


# --- files -------------------------------------------------------------------------------------------------------

def read_json(path):
    """Parsed JSON, or None if the file is missing or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path, data):
    """Write JSON atomically: a temp file in the same folder, then a rename over the target."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path, text):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    _replace(tmp, path)


def _replace(src, dst):
    # On Windows a reader holding the target open can make the rename fail briefly; retry a few times.
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            import time
            time.sleep(0.05)


# --- runs --------------------------------------------------------------------------------------------------------

def slugify(text, limit=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "task"


def new_run_dir(task):
    """Create and return a new, unique run folder named <timestamp>-<slug>."""
    base = runs_dir()
    base.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name, n = f"{stamp}-{slugify(task)}", 2
    while True:
        try:
            (base / name).mkdir()
            return base / name
        except FileExistsError:
            name = f"{stamp}-{slugify(task)}-{n}"
            n += 1


def run_dir(run_id):
    """The folder for run_id; refuses ids that would escape the store."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id or ""):
        raise ValueError(f"not a run id: {run_id!r}")
    return runs_dir() / run_id


def list_run_dirs():
    base = runs_dir()
    if not base.exists():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)


def find_by_request_key(key):
    """The newest run whose request carries this request_key, or None."""
    for d in reversed(list_run_dirs()):
        req = read_json(d / "request.json")
        if isinstance(req, dict) and req.get("request_key") == key:
            return d
    return None


# --- lock --------------------------------------------------------------------------------------------------------

class Locked(Exception):
    """The run is held by a live process."""


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_holder(rdir):
    """The lock's contents if a live process holds it, else None (no lock, or a stale one)."""
    info = read_json(Path(rdir) / "lock")
    if isinstance(info, dict) and pid_alive(info.get("pid")):
        return info
    return None


def acquire(rdir, role, pid=None):
    """Take the run's lock for pid (default: this process). A stale lock (dead PID) is taken over.

    Raises Locked if a live process holds it.
    """
    path = Path(rdir) / "lock"
    info = {"pid": pid or os.getpid(), "role": role, "since": now_iso()}
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = read_json(path)
            if holder is None:
                # Mid-write by another process, or a corrupt file; treat as held unless it stays unreadable.
                import time
                time.sleep(0.1)
                if read_json(path) is None and path.exists():
                    _remove_stale(path, None)
                continue
            if pid_alive(holder.get("pid")):
                raise Locked(f"run is held by pid {holder['pid']} ({holder.get('role')})")
            _remove_stale(path, holder.get("pid"))
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f)
        return info
    raise Locked("could not take the run lock")


def hand_over(rdir, from_pid, to_pid, role):
    """Pass a held lock to another process (the launcher hands its lock to the worker it started)."""
    path = Path(rdir) / "lock"
    holder = read_json(path)
    if not holder or holder.get("pid") != from_pid:
        raise Locked("lock is not held by the expected process")
    write_json(path, {"pid": to_pid, "role": role, "since": now_iso()})


def release(rdir, pid=None):
    path = Path(rdir) / "lock"
    holder = read_json(path)
    if holder and holder.get("pid") == (pid or os.getpid()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _remove_stale(path, stale_pid):
    """Move a stale lock aside. If another process replaced it with a live lock meanwhile, put that one back."""
    grave = path.with_name(f"lock.stale.{os.getpid()}")
    try:
        os.replace(path, grave)
    except FileNotFoundError:
        return
    moved = read_json(grave)
    if isinstance(moved, dict) and moved.get("pid") != stale_pid and pid_alive(moved.get("pid")):
        try:
            os.link(grave, path)  # fails if yet another lock appeared; then that one stands
        except OSError:
            pass
    try:
        os.unlink(grave)
    except FileNotFoundError:
        pass

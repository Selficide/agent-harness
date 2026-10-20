"""The worker: a detached process that owns one run turn. It drives the adapter, then writes meta.json and
result.json. It only starts work once the launcher has handed it the run's lock.

  python -m harness worker <run_id> --turn N
"""

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import adapters, quota, schema, store
from .adapters.base import ProviderUnavailable, run_files

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
REPO_ROOT = Path(__file__).resolve().parent.parent
_detached = []  # keep Popen objects alive so Python doesn't warn about children still running


# --- launching ---------------------------------------------------------------------------------------------------

def spawn(run_id, turn):
    """Start a detached worker for run_id. Returns (pid, breakaway): breakaway is False when Windows refused to let
    it leave the caller's job object (then it may die with the session), None off Windows."""
    argv = [sys.executable, "-m", "harness", "worker", run_id, "--turn", str(turn)]
    log = open(store.run_dir(run_id) / "worker.log", "ab")
    common = dict(cwd=REPO_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
    try:
        if os.name != "nt":
            proc = subprocess.Popen(argv, start_new_session=True, **common)
            _detached.append(proc)
            return proc.pid, None
        base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(argv, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **common)
            breakaway = True
        except OSError:
            proc = subprocess.Popen(argv, creationflags=base, **common)
            breakaway = False
        _detached.append(proc)
        return proc.pid, breakaway
    finally:
        log.close()


# --- running a turn ----------------------------------------------------------------------------------------------

def wait_for_handover(rdir, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        holder = store.read_json(rdir / "lock")
        if holder and holder.get("pid") == os.getpid():
            return True
        time.sleep(0.1)
    return False


def kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def execute(rdir, turn):
    """Run turn N of the run in rdir. The caller holds the lock. Writes meta.json and result.json."""
    meta = store.read_json(rdir / "meta.json")
    request = store.read_json(rdir / "request.json")
    adapter = adapters.get(meta["provider"])
    files = run_files(turn)
    entry = meta["turns"][-1]
    assert entry["turn"] == turn, "meta.json has no entry for this turn"
    entry["started"] = store.now_iso()
    run = dict(meta, run_dir=str(rdir))
    stdin_text = (rdir / files["input"]).read_text(encoding="utf-8")
    env = dict(os.environ, AGENT_HARNESS_DEPTH="1")

    timed_out, exit_code, launch_error = False, None, None
    start = time.monotonic()
    try:
        argv, cwd = adapter.command(run, turn)
        entry["command"] = argv
        with open(rdir / files["events"], "wb") as out, open(rdir / files["stderr"], "wb") as err:
            # The worker has no console, so on Windows the provider (and the shells it runs) would otherwise get a
            # visible console window of its own; closing it kills the run.
            flags = CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err, env=env,
                                    creationflags=flags)
            try:
                proc.communicate(stdin_text.encode("utf-8"), timeout=meta["timeout_s"])
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                timed_out = True
            exit_code = proc.returncode
    except (ProviderUnavailable, OSError) as e:
        launch_error = f"{type(e).__name__}: {e}"
    entry["seconds"] = round(time.monotonic() - start, 1)
    entry["finished"] = store.now_iso()
    entry["exit_code"] = exit_code
    entry["timed_out"] = timed_out

    parsed = adapter.parse(rdir, turn)
    if parsed["thread_id"] and not meta.get("thread_id"):
        meta["thread_id"] = parsed["thread_id"]
    entry["usage"] = parsed["usage"]
    entry["errors"] = parsed["errors"]

    answer, answer_problem = None, None
    if parsed["answer_text"] is not None:
        try:
            candidate = json.loads(parsed["answer_text"])
            problems = schema.validate(candidate, schema.answer_schema(meta["type"]))
            if problems:
                answer_problem = "answer doesn't match the schema: " + "; ".join(problems[:5])
            else:
                answer = candidate
        except ValueError:
            answer_problem = "answer is not JSON"
    entry["answer_file"] = files["answer"] if parsed["answer_text"] is not None else None

    after = adapter.quota_snapshot(meta.get("thread_id"))
    entry["quota_after"] = after
    warnings = list(entry.get("warnings", []))
    warnings += quota.announce(after)
    if parsed.get("commands_blocked"):
        warnings.append(f"{parsed['commands_blocked']} command(s) were blocked by the provider's sandbox policy")

    if launch_error:
        state, error = "failed", {"kind": "provider_unavailable", "message": launch_error}
    elif timed_out:
        state, error = "timed_out", {"kind": "timeout", "message": f"no answer within {meta['timeout_s']} s"}
    elif answer is None and (exit_code != 0 or parsed["errors"]):
        detail = "; ".join(parsed["errors"][-3:]) or _tail(rdir / files["stderr"]) or f"exit code {exit_code}"
        state, error = "failed", {"kind": "provider_error", "message": detail}
    elif answer is None:
        state, error = "failed", {"kind": "schema_invalid", "message": answer_problem or "no answer was written"}
    else:
        state, error = answer["status"], None
        if not answer["files_read"]:
            warnings.append("files_read is empty: the delegate may not have read anything; check its evidence")
    entry["state"] = state
    entry["warnings"] = warnings
    store.write_json(rdir / "meta.json", meta)
    store.write_json(rdir / "result.json", build_result(rdir, meta, state, answer, error))


def build_result(rdir, meta, state, answer, error):
    """The stored result (label is overlaid when read, from label.json)."""
    turns = meta["turns"]
    usage = {k: 0 for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")}
    for t in turns:
        for k in usage:
            usage[k] += (t.get("usage") or {}).get(k, 0)
    running = state == "running"
    finished = None if running else (turns[-1].get("finished") or store.now_iso())
    seconds = None if running else round(sum(t.get("seconds") or 0 for t in turns), 1)
    warnings = [w for t in turns for w in t.get("warnings", [])]
    if meta.get("breakaway") is False:
        warnings.append("the worker could not leave the caller's job object; the run may stop if the session ends")
    return {
        "contract": schema.CONTRACT_VERSION,
        "run_id": meta["run_id"],
        "run_dir": str(rdir),
        "state": state,
        "answer": answer,
        "truncated": False,
        "provider": meta["provider"],
        "model": meta.get("model"),
        "effort_applied": meta.get("effort"),
        "prompt_version": meta["prompt_version"],
        "capabilities": meta["capabilities"],
        "schema_enforcement": meta["schema_enforcement"],
        "usage": usage,
        "quota": quota.result_quota(turns[0].get("quota_before"), turns[-1].get("quota_after")),
        "timing": {"started": meta["created"], "finished": finished, "seconds": seconds},
        "warnings": warnings,
        "error": error,
        "label": None,
    }


def _tail(path, chars=500):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-chars:]


def main(run_id, turn):
    rdir = store.run_dir(run_id)
    if not wait_for_handover(rdir):
        print(f"worker {os.getpid()}: lock for {run_id} was never handed over; exiting", file=sys.stderr)
        return 3
    try:
        execute(rdir, turn)
    except Exception:
        fail(rdir, "provider_error", "worker crashed:\n" + traceback.format_exc()[-1500:])
        raise
    finally:
        store.release(rdir)
    return 0


def fail(rdir, kind, message):
    """Record a failed result for the current turn (used for crashes and interrupted runs)."""
    meta = store.read_json(rdir / "meta.json")
    if not meta:
        return
    state = "failed"
    meta["turns"][-1].setdefault("finished", store.now_iso())
    meta["turns"][-1]["state"] = state
    store.write_json(rdir / "meta.json", meta)
    store.write_json(rdir / "result.json", build_result(rdir, meta, state, None, {"kind": kind, "message": message}))

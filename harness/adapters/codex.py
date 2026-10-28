"""Codex CLI adapter (`codex exec --json`), read-only sandbox, native structured output."""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .. import schema
from ..prompts import codex as prompts
from .base import Adapter, ProviderUnavailable, run_files

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


class CodexAdapter(Adapter):
    name = "codex"
    schema_enforcement = "native"
    prompt_version = prompts.PROMPT_VERSION
    instruction_files = ("AGENTS.md", "AGENTS.override.md")

    def exe(self):
        exe = shutil.which("codex")
        if not exe:
            raise ProviderUnavailable("codex not found on PATH")
        return exe

    def defaults(self, cfg):
        return {"model": cfg["default_model"].get("codex"), "effort": cfg["default_effort"]}

    def capabilities(self):
        # The read-only sandbox lets Codex read files and run read-only commands, with no network. Python is
        # blocked by sandbox policy on this machine (v0 runs); left undetected rather than assumed elsewhere.
        return {"can_read_files": True, "can_run_commands": True, "can_run_python": None, "network": False}

    def render(self, request):
        return prompts.render(request)

    def command(self, run, turn):
        rdir = Path(run["run_dir"])
        files = run_files(turn)
        schema_path = rdir / "answer.schema.json"
        if not schema_path.exists():
            schema_path.write_text(json.dumps(schema.strip_annotations(schema.answer_schema(run["type"])), indent=2),
                                   encoding="utf-8")
        common = [
            "--json",
            "-m", run["model"],
            "-c", f'model_reasoning_effort="{run["effort"]}"',
            "--disable", "multi_agent",
            "--skip-git-repo-check",
            "-o", str(rdir / files["answer"]),
            "--output-schema", str(schema_path),
        ]
        # MCP servers run outside Codex's sandbox, so turn off every one Codex would load here (an empty
        # `mcp_servers={}` override merges rather than replaces, so it does nothing).
        for name in self.mcp_servers(run["workdir"]):
            common += ["-c", f"mcp_servers.{name}.enabled=false"]
        if turn == 1:
            argv = [self.exe(), "exec", "-", "-C", run["workdir"], "-s", "read-only"] + common
        else:
            if not run.get("thread_id"):
                raise ProviderUnavailable("no Codex thread id recorded for this run; cannot resume")
            # `exec resume` has no -s flag; set the sandbox through config so a reply can't widen access.
            argv = [self.exe(), "exec", "resume", run["thread_id"], "-", "-c", 'sandbox_mode="read-only"'] + common
        return argv, run["workdir"]

    def mcp_servers(self, workdir):
        """Names of the MCP servers Codex would load in workdir. Raises ProviderUnavailable if they can't be listed,
        so a run never starts with servers left on."""
        try:
            out = subprocess.run([self.exe(), "mcp", "list", "--json"], cwd=workdir, capture_output=True, text=True,
                                 timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            servers = json.loads(out.stdout) if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            servers = None
        if not isinstance(servers, list) or not all(isinstance(s, dict) and s.get("name") for s in servers):
            raise ProviderUnavailable("could not list Codex MCP servers to turn them off")
        names = [s["name"] for s in servers]
        # `-c` takes the dotted path literally (quotes become part of the name), so only plain names can be addressed.
        bad = [n for n in names if not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", n)]
        if bad:
            raise ProviderUnavailable(f"can't turn off Codex MCP servers with these names: {bad}")
        return names

    def parse(self, run_dir, turn):
        return parse_turn(Path(run_dir), turn)

    def quota_snapshot(self, thread_id=None):
        sessions = CODEX_HOME / "sessions"
        if not sessions.exists():
            return None
        if thread_id:
            matches = sorted(sessions.rglob(f"rollout-*{thread_id}.jsonl"))
            return latest_snapshot(matches[-1]) if matches else None
        for rollout in sorted(sessions.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            snap = latest_snapshot(rollout)
            if snap:
                return snap
        return None

    def version(self):
        try:
            return subprocess.run([self.exe(), "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (ProviderUnavailable, OSError, subprocess.SubprocessError):
            return None

    def doctor(self):
        checks = []
        try:
            exe = self.exe()
            checks.append(("codex installed", True, f"{exe} ({self.version()})"))
        except ProviderUnavailable as e:
            return [("codex installed", False, str(e))]
        try:
            out = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=30)
            text = (out.stdout + out.stderr).strip()
            checks.append(("codex logged in", out.returncode == 0 and "logged in" in text.lower(), text))
        except (OSError, subprocess.SubprocessError) as e:
            checks.append(("codex logged in", False, str(e)))
        cfg = CODEX_HOME / "config.toml"
        text = cfg.read_text(encoding="utf-8", errors="replace") if cfg.exists() else ""
        registered = harness_mcp_registered(text)
        checks.append(("harness not registered as a Codex MCP server (recursion guard)", not registered,
                       f"{cfg}: {'found ' + registered if registered else 'not found'}"))
        try:
            servers = self.mcp_servers(Path.home())
            checks.append(("Codex MCP servers listable (each is turned off per run)", True,
                           ", ".join(servers) or "none configured"))
        except ProviderUnavailable as e:
            checks.append(("Codex MCP servers listable (each is turned off per run)", False, str(e)))
        snap = self.quota_snapshot()
        w = (snap or {}).get("windows") or []
        checks.append(("codex quota readable", bool(w),
                       ", ".join(f"{x['name']} {x['used_percent']:g}% used" for x in w) or "no snapshot found"))
        return checks


def harness_mcp_registered(config_text):
    """The [mcp_servers.<name>] table that launches the harness, if any."""
    current = None
    for line in config_text.splitlines():
        m = re.match(r"\s*\[mcp_servers\.([^\]]+)\]", line)
        if m:
            current = m.group(1)
            continue
        if line.strip().startswith("["):
            current = None
        if current and re.search(r"agent-harness|\bharness\b", line):
            return current
    return None


USAGE_KEYS = {"input_tokens": "input_tokens", "cached_input_tokens": "cached_input_tokens",
              "output_tokens": "output_tokens", "reasoning_output_tokens": "reasoning_tokens"}


def parse_turn(run_dir, turn):
    """Thread id, usage, final answer text, errors, and the shell commands run, from one turn's files."""
    files = run_files(turn)
    out = {"thread_id": None, "usage": {v: 0 for v in USAGE_KEYS.values()}, "answer_text": None, "errors": [],
           "commands_blocked": 0, "commands": []}
    events = run_dir / files["events"]
    if events.exists():
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "thread.started":
                out["thread_id"] = ev.get("thread_id")
            elif kind == "turn.completed":
                for k, v in (ev.get("usage") or {}).items():
                    if k in USAGE_KEYS and isinstance(v, int):
                        out["usage"][USAGE_KEYS[k]] += v
            elif kind == "item.completed" and (ev.get("item") or {}).get("type") == "command_execution":
                if isinstance(ev["item"].get("command"), str):
                    out["commands"].append(ev["item"]["command"])
            elif kind in ("error", "turn.failed"):
                err = ev.get("message") or ev.get("error") or ev
                if isinstance(err, dict) and isinstance(err.get("message"), str):
                    err = err["message"]
                out["errors"].append(err if isinstance(err, str) else json.dumps(err))
    stderr = run_dir / files["stderr"]
    if stderr.exists():
        out["commands_blocked"] = stderr.read_text(encoding="utf-8", errors="replace").count("blocked by policy")
    answer = run_dir / files["answer"]
    if answer.exists():
        out["answer_text"] = answer.read_text(encoding="utf-8", errors="replace")
    return out


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from _find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_key(v, key)


def latest_snapshot(rollout):
    """The last rate-limit snapshot in a Codex session log, as a quota snapshot."""
    last, taken = None, None
    try:
        lines = Path(rollout).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if "rate_limits" not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        for rl in _find_key(ev, "rate_limits"):
            if isinstance(rl, dict) and rl.get("primary"):
                last, taken = rl, ev.get("timestamp")
    return snapshot_from_rate_limits(last, taken) if last else None


def snapshot_from_rate_limits(rl, taken_at=None):
    windows = []
    for name in ("primary", "secondary"):
        w = rl.get(name)
        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)):
            resets = w.get("resets_at")
            windows.append({
                "name": name,
                "used_percent": w["used_percent"],
                "window_minutes": w.get("window_minutes"),
                "resets_at": (dt.datetime.fromtimestamp(resets).astimezone().isoformat(timespec="seconds")
                              if isinstance(resets, (int, float)) else None),
            })
    return {"provider": "codex", "plan_type": rl.get("plan_type"), "taken_at": taken_at, "windows": windows}

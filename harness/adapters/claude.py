"""Claude Code adapter (`claude -p --output-format stream-json`), for research on cheaper Claude models (Haiku by
default). Review stays with Codex: a Claude reviewer of Claude's work is not an independent check.

Read-only comes from the tool list, not an OS sandbox: --restricted removes the tools that run commands or code and
WebFetch, ignores the user's settings files and confines the file tools to the working folders; --tools leaves only
Read, Grep and Glob; --strict-mcp-config loads no MCP servers. Folders of absolute paths the request names are added
with --add-dir. CLAUDE.md files from workdir up are still loaded as instructions (see instruction_files).
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .. import schema, store
from ..prompts import claude as prompts
from .base import Adapter, ProviderUnavailable, run_files

TOOLS = "Read,Grep,Glob"
# The harness may itself run inside Claude Code, whose variables mark a child session (host auth, messaging socket,
# minimal mode); the delegate is a separate session, so they are dropped. These two choose its account and are kept.
KEEP_ENV = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
DROP_ENV = {"AI_AGENT", "BAGGAGE"}


class ClaudeAdapter(Adapter):
    name = "claude"
    schema_enforcement = "native"
    prompt_version = prompts.PROMPT_VERSION
    types = ("research",)
    instruction_files = ("CLAUDE.md", "CLAUDE.local.md")
    instructions_up_to_git_root = False   # Claude Code reads CLAUDE.md in every folder above workdir

    def exe(self):
        exe = store.config().get("claude_exe") or shutil.which("claude") or _desktop_bundle()
        if not exe or not Path(exe).exists():
            raise ProviderUnavailable("claude not found: put it on PATH or set claude_exe in config.json")
        return str(exe)

    def defaults(self, cfg):
        return {"model": cfg["default_model"].get("claude"), "effort": cfg["default_effort"]}

    def capabilities(self):
        return {"can_read_files": True, "can_run_commands": False, "can_run_python": False, "network": False}

    def render(self, request):
        return prompts.render(request)

    def env(self, base):
        return {k: v for k, v in base.items()
                if k in KEEP_ENV or not (k.upper().startswith("CLAUDE") or k.upper() in DROP_ENV)}

    def command(self, run, turn):
        answer_schema = json.dumps(schema.strip_annotations(schema.answer_schema(run["type"])))
        argv = [self.exe(), "-p", "--output-format", "stream-json", "--verbose",
                "--model", run["model"], "--effort", run["effort"],
                "--restricted", "--tools", TOOLS, "--strict-mcp-config",
                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                "--json-schema", answer_schema]
        budget = store.config().get("claude_max_budget_usd")
        if budget:
            argv += ["--max-budget-usd", str(budget)]
        for folder in extra_dirs(_request(run), run["workdir"]):
            argv += ["--add-dir", folder]
        if turn > 1:
            if not run.get("thread_id"):
                raise ProviderUnavailable("no Claude session id recorded for this run; cannot resume")
            argv += ["--resume", run["thread_id"]]
        return argv, run["workdir"]

    def parse(self, run_dir, turn):
        return parse_turn(Path(run_dir), turn)

    def version(self):
        try:
            return subprocess.run([self.exe(), "--version"], capture_output=True, text=True, timeout=30,
                                  env=self.env(dict(os.environ))).stdout.strip()
        except (ProviderUnavailable, OSError, subprocess.SubprocessError):
            return None

    def doctor(self):
        # Claude is an optional provider, so problems are warnings: the doctor still passes with Codex alone.
        try:
            exe = self.exe()
        except ProviderUnavailable as e:
            return [("claude installed (optional, for research)", "warn", str(e))]
        checks = [("claude installed (optional, for research)", True, f"{exe} ({self.version()})")]
        try:
            out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30,
                                 env=self.env(dict(os.environ)))
            status = json.loads(out.stdout)
            ok = bool(status.get("loggedIn"))
            detail = f"{status.get('authMethod')}" if ok else "not logged in: run `claude auth login` in a terminal"
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            ok, detail = False, str(e)
        checks.append(("claude logged in", True if ok else "warn", detail))
        checks.append(("claude spending cap per run", True,
                       f"${store.config().get('claude_max_budget_usd')} (claude_max_budget_usd)"))
        return checks


def _request(run):
    return store.read_json(Path(run["run_dir"]) / "request.json") or {}


def extra_dirs(request, workdir):
    """Folders outside workdir that hold absolute paths the request names (the file tools can't reach them
    otherwise)."""
    from .. import paths
    workdir = Path(workdir).resolve()
    out = []
    for field in paths.PATH_FIELDS:
        for item in request.get(field) or []:
            part = paths.path_part(item)
            if not part or not Path(part).is_absolute():
                continue
            p = Path(part).resolve()
            folder = p if p.is_dir() else p.parent
            if folder != workdir and workdir not in folder.parents and str(folder) not in out:
                out.append(str(folder))
    return out


def _desktop_bundle():
    """The newest claude.exe the Claude desktop app keeps under %LOCALAPPDATA%\\Claude-Data, if any."""
    base = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not base:
        return None
    found = list(Path(base).glob("Claude-Data/*/claude-code/*/*/claude.exe"))

    def version(p):
        return tuple(int(x) for x in re.findall(r"\d+", p.parent.parent.name))
    return str(max(found, key=version)) if found else None


def tool_command(name, args):
    """A tool call as one line of text, for the coverage and outside-workdir checks (they read paths from it)."""
    if name == "Read":
        return f"Read {args.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"{name} {args.get('path', '')} {args.get('pattern', '')}".replace("  ", " ")
    return f"{name} {json.dumps(args, ensure_ascii=False)}"


def parse_turn(run_dir, turn):
    """Session id, usage, answer, errors and tool calls from one turn's stream-json events. The structured answer
    is written to the turn's answer file, so it sits where a Codex answer would."""
    files = run_files(turn)
    out = {"thread_id": None, "answer_text": None, "errors": [], "commands_blocked": 0, "commands": [],
           "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
           "cost_usd": None}
    events = run_dir / files["events"]
    if not events.exists():
        return out
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            out["thread_id"] = ev.get("session_id")
        elif kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                # StructuredOutput is the answer itself, not a read; its paths would trip the outside-workdir check.
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") != "StructuredOutput":
                    out["commands"].append(tool_command(block.get("name"), block.get("input") or {}))
        elif kind == "result":
            usage = ev.get("usage") or {}
            cached = usage.get("cache_read_input_tokens") or 0
            # Codex counts cached tokens inside input_tokens; match it so totals compare.
            out["usage"]["input_tokens"] = ((usage.get("input_tokens") or 0) + cached
                                            + (usage.get("cache_creation_input_tokens") or 0))
            out["usage"]["cached_input_tokens"] = cached
            out["usage"]["output_tokens"] = usage.get("output_tokens") or 0
            out["usage"]["reasoning_tokens"] = (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            out["cost_usd"] = ev.get("total_cost_usd")
            out["commands_blocked"] = len(ev.get("permission_denials") or [])
            if ev.get("structured_output") is not None:
                out["answer_text"] = json.dumps(ev["structured_output"], ensure_ascii=False)
            if ev.get("is_error") or str(ev.get("subtype", "")).startswith("error"):
                out["errors"].append(str(ev.get("result") or ev.get("terminal_reason") or ev.get("subtype")))
    answer = run_dir / files["answer"]
    if out["answer_text"] is not None and not answer.exists():
        store.write_text(answer, out["answer_text"])
    elif answer.exists():
        out["answer_text"] = answer.read_text(encoding="utf-8", errors="replace")
    return out

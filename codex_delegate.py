"""Delegate a task to Codex CLI from a brief file, and keep a record of every run.

Usage:
  python codex_delegate.py run BRIEF [--cd DIR] [--write] [--model M] [--effort E] [--timeout S]
  python codex_delegate.py resume RUN_DIR REPLY_FILE [--timeout S]
  python codex_delegate.py label RUN_DIR --outcome useful|partial|useless [--mast CATEGORY] [--note TEXT]
  python codex_delegate.py report

Each run gets a folder under runs/codex/ holding the brief, Codex's final message (result.md, plus result.json
when the task type has an output schema), the raw event stream, and meta.json (settings, timing, tokens, and
ChatGPT plan usage before and after). The script prints only a short summary and the run folder; read the files
for details. `report` summarizes all runs.

Plan usage comes from the rate-limit snapshots Codex writes to its session logs ($CODEX_HOME/sessions). Codex
reports it as a whole-number percentage of the plan's window, and a snapshot can lag the call it was taken in,
so per-run deltas are coarse; totals across many runs are the reliable number.

Standard library only.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
RUNS_DIR = HARNESS_DIR / "runs" / "codex"
SCHEMAS_DIR = HARNESS_DIR / "schemas"
SESSIONS_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"

DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_EFFORT = {"review": "high", "research": "medium", "execute": "medium"}
DEFAULT_TIMEOUT = 900
TASK_TYPES = ("review", "research", "execute")


def parse_brief(text):
    """Read the header fields every brief carries: '# Task:', 'Type:', 'Template:'."""
    def field(pattern):
        m = re.search(pattern, text, re.MULTILINE)
        return m.group(1).strip() if m else None

    task = field(r"^#\s*Task:\s*(.+)$")
    task_type = field(r"^Type:\s*(\w+)")
    template = field(r"^Template:\s*(\S+)")
    missing = [name for name, v in (("# Task:", task), ("Type:", task_type), ("Template:", template)) if not v]
    if missing:
        sys.exit(f"brief is missing header field(s): {', '.join(missing)}")
    if task_type not in TASK_TYPES:
        sys.exit(f"Type must be one of {', '.join(TASK_TYPES)}, got {task_type!r}")
    return task, task_type, template


def slugify(text, limit=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "task"


def codex_exe():
    exe = shutil.which("codex")
    if not exe:
        sys.exit("codex not found on PATH")
    return exe


def summarize_events(path):
    """Pull the thread id, token usage, item counts, and errors out of a --json event stream."""
    thread_id, usage, items, errors = None, {}, {}, []
    if not path.exists():
        return thread_id, usage, items, errors
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "thread.started":
            thread_id = ev.get("thread_id")
        elif kind == "turn.completed":
            for k, v in (ev.get("usage") or {}).items():
                if isinstance(v, int):
                    usage[k] = usage.get(k, 0) + v
        elif kind == "item.completed":
            t = (ev.get("item") or {}).get("type", "unknown")
            items[t] = items.get(t, 0) + 1
        elif kind in ("error", "turn.failed"):
            errors.append(ev.get("message") or ev.get("error") or ev)
    return thread_id, usage, items, errors


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from _find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_key(v, key)


def rate_limit_snapshots(rollout):
    """Every rate-limit snapshot in a Codex session log, oldest first."""
    snaps = []
    if not rollout or not rollout.exists():
        return snaps
    for line in rollout.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        snaps += [rl for rl in _find_key(ev, "rate_limits") if isinstance(rl, dict) and rl.get("primary")]
    return snaps


def plan_usage(snapshot):
    if not snapshot:
        return None
    primary = snapshot["primary"]
    resets = primary.get("resets_at")
    return {
        "plan_type": snapshot.get("plan_type"),
        "used_percent": primary.get("used_percent"),
        "window_minutes": primary.get("window_minutes"),
        "resets_at": dt.datetime.fromtimestamp(resets).isoformat(timespec="seconds") if resets else None,
    }


def latest_plan_usage():
    """Plan usage from the newest session log on disk, i.e. the state before the next run."""
    if not SESSIONS_DIR.exists():
        return None
    for rollout in sorted(SESSIONS_DIR.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        snaps = rate_limit_snapshots(rollout)
        if snaps:
            return plan_usage(snaps[-1])
    return None


def session_rollout(thread_id):
    if not thread_id or not SESSIONS_DIR.exists():
        return None
    matches = sorted(SESSIONS_DIR.rglob(f"rollout-*{thread_id}.jsonl"))
    return matches[-1] if matches else None


def run_codex(cmd, stdin_text, cwd, run_dir, suffix, timeout):
    """Run one Codex turn, streaming events and stderr to files. Returns (exit_code, timed_out, seconds)."""
    events = run_dir / f"events{suffix}.jsonl"
    stderr = run_dir / f"stderr{suffix}.log"
    start = time.monotonic()
    timed_out = False
    with events.open("wb") as out, stderr.open("wb") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err)
        try:
            proc.communicate(stdin_text.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            timed_out = True
    return proc.returncode, timed_out, round(time.monotonic() - start, 1)


def read_result(run_dir, suffix, has_schema):
    """Load Codex's final message; with a schema, also parse and save it as JSON."""
    result_md = run_dir / f"result{suffix}.md"
    if not result_md.exists():
        return None, None
    text = result_md.read_text(encoding="utf-8", errors="replace")
    if not has_schema:
        return text, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text, None
    (run_dir / f"result{suffix}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return text, data


def record_turn(meta, run_dir, suffix, exit_code, timed_out, seconds, has_schema, plan_before):
    thread_id, usage, items, errors = summarize_events(run_dir / f"events{suffix}.jsonl")
    _, data = read_result(run_dir, suffix, has_schema)
    snaps = rate_limit_snapshots(session_rollout(thread_id or meta.get("thread_id")))
    turn = {
        "plan_before": plan_before,
        "plan_after": plan_usage(snaps[-1]) if snaps else None,
        "suffix": suffix,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "seconds": seconds,
        "usage": usage,
        "items": items,
        "errors": errors,
        "status": (data or {}).get("status"),
        "findings": len(data["findings"]) if data and isinstance(data.get("findings"), list) else None,
    }
    if thread_id and not meta.get("thread_id"):
        meta["thread_id"] = thread_id
    meta["turns"].append(turn)
    return turn


def print_summary(run_dir, turn):
    tokens = turn["usage"].get("input_tokens", 0) + turn["usage"].get("output_tokens", 0)
    state = "TIMED OUT" if turn["timed_out"] else f"exit {turn['exit_code']}"
    parts = [state, f"{turn['seconds']}s", f"{tokens} tokens"]
    if turn["status"]:
        parts.append(f"status={turn['status']}")
    if turn["findings"] is not None:
        parts.append(f"findings={turn['findings']}")
    if turn["errors"]:
        parts.append(f"errors={len(turn['errors'])}")
    before, after = turn["plan_before"], turn["plan_after"]
    if after:
        start = f"{before['used_percent']}% -> " if before else ""
        parts.append(f"plan {start}{after['used_percent']}% used")
    print(" | ".join(parts))
    print(run_dir)


def cmd_run(args):
    brief_path = Path(args.brief).resolve()
    brief = brief_path.read_text(encoding="utf-8")
    task, task_type, template = parse_brief(brief)
    workdir = Path(args.cd).resolve()
    if not workdir.is_dir():
        sys.exit(f"--cd is not a directory: {workdir}")
    if args.write and task_type != "execute":
        sys.exit("--write is only allowed for Type: execute")

    model = args.model or DEFAULT_MODEL
    effort = args.effort or DEFAULT_EFFORT[task_type]
    sandbox = "workspace-write" if args.write else "read-only"
    schema = SCHEMAS_DIR / f"{task_type}.schema.json"
    has_schema = schema.exists()

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"{stamp}-{slugify(task)}"
    n = 2
    while run_dir.exists():
        run_dir = RUNS_DIR / f"{stamp}-{slugify(task)}-{n}"
        n += 1
    run_dir.mkdir(parents=True)
    (run_dir / "brief.md").write_text(brief, encoding="utf-8")

    cmd = [
        codex_exe(), "exec", "-",
        "--json",
        "-C", str(workdir),
        "-s", sandbox,
        "-m", model,
        "-c", f'model_reasoning_effort="{effort}"',
        "--disable", "multi_agent",
        "-o", str(run_dir / "result.md"),
    ]
    if has_schema:
        cmd += ["--output-schema", str(schema)]
    if args.skip_git_check:
        cmd.append("--skip-git-repo-check")

    meta = {
        "task": task,
        "type": task_type,
        "template": template,
        "brief_source": str(brief_path),
        "workdir": str(workdir),
        "model": model,
        "effort": effort,
        "sandbox": sandbox,
        "schema": schema.name if has_schema else None,
        "started": dt.datetime.now().isoformat(timespec="seconds"),
        "codex_version": subprocess.run([codex_exe(), "--version"], capture_output=True, text=True).stdout.strip(),
        "command": cmd,
        "thread_id": None,
        "turns": [],
        "label": None,
    }
    plan_before = latest_plan_usage()
    exit_code, timed_out, seconds = run_codex(cmd, brief, workdir, run_dir, "", args.timeout)
    turn = record_turn(meta, run_dir, "", exit_code, timed_out, seconds, has_schema, plan_before)
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print_summary(run_dir, turn)


def cmd_resume(args):
    run_dir = Path(args.run_dir).resolve()
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    if not meta.get("thread_id"):
        sys.exit("no thread_id recorded for this run; cannot resume")
    reply = Path(args.reply).read_text(encoding="utf-8")
    n = len(meta["turns"]) + 1
    suffix = f"-{n}"
    (run_dir / f"reply{suffix}.md").write_text(reply, encoding="utf-8")
    has_schema = bool(meta.get("schema"))

    cmd = [
        codex_exe(), "exec", "resume", meta["thread_id"], "-",
        "--json",
        "-m", meta["model"],
        "-c", f'model_reasoning_effort="{meta["effort"]}"',
        "--disable", "multi_agent",
        "-o", str(run_dir / f"result{suffix}.md"),
    ]
    if has_schema:
        cmd += ["--output-schema", str(SCHEMAS_DIR / meta["schema"])]
    plan_before = latest_plan_usage()
    exit_code, timed_out, seconds = run_codex(cmd, reply, meta["workdir"], run_dir, suffix, args.timeout)
    turn = record_turn(meta, run_dir, suffix, exit_code, timed_out, seconds, has_schema, plan_before)
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print_summary(run_dir, turn)


def cmd_label(args):
    run_dir = Path(args.run_dir).resolve()
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["label"] = {
        "outcome": args.outcome,
        "mast": args.mast,
        "note": args.note,
        "labelled": dt.datetime.now().isoformat(timespec="seconds"),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"labelled {run_dir.name}: {args.outcome}")


def cmd_report(args):
    """One line per run, then totals: tokens, outcomes, and plan usage now."""
    metas = []
    for path in sorted(RUNS_DIR.glob("*/meta.json")):
        try:
            metas.append((path.parent.name, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    total_in = total_out = 0
    outcomes = {}
    for name, meta in metas:
        turns = meta.get("turns", [])
        tin = sum(t["usage"].get("input_tokens", 0) for t in turns)
        tout = sum(t["usage"].get("output_tokens", 0) for t in turns)
        total_in += tin
        total_out += tout
        outcome = (meta.get("label") or {}).get("outcome", "unlabelled")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        before = next((t["plan_before"] for t in turns if t.get("plan_before")), None)
        after = next((t["plan_after"] for t in reversed(turns) if t.get("plan_after")), None)
        plan = f"{before['used_percent'] if before else '?'}%->{after['used_percent'] if after else '?'}%"
        print(f"{name}  {meta['type']:<8} {meta['effort']:<6} {len(turns)} turn(s)  {tin + tout:>8} tok  "
              f"plan {plan:<10} {outcome}")
    print(f"\n{len(metas)} runs | {total_in} input + {total_out} output tokens | "
          + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
    now = latest_plan_usage()
    if now:
        print(f"plan ({now['plan_type']}): {now['used_percent']}% of window used, resets {now['resets_at']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="start a new delegated task from a brief")
    r.add_argument("brief")
    r.add_argument("--cd", default=".", help="directory Codex works in (default: current)")
    r.add_argument("--write", action="store_true", help="allow edits (workspace-write); execute tasks only")
    r.add_argument("--model")
    r.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    r.add_argument("--skip-git-check", action="store_true", help="allow a --cd that is not a git repo")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("resume", help="answer Codex's question and continue the same session")
    s.add_argument("run_dir")
    s.add_argument("reply")
    s.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    s.set_defaults(func=cmd_resume)

    lb = sub.add_parser("label", help="record how useful a run was")
    lb.add_argument("run_dir")
    lb.add_argument("--outcome", required=True, choices=["useful", "partial", "useless"])
    lb.add_argument("--mast", choices=["specification", "coordination", "verification"],
                    help="failure category, if the run failed")
    lb.add_argument("--note")
    lb.set_defaults(func=cmd_label)

    rp = sub.add_parser("report", help="summarize all runs: tokens, outcomes, plan usage")
    rp.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

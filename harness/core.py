"""The operations behind the CLI and the MCP server: delegate, result, reply, label, report, doctor.

Every function returns plain dicts (contract v1 results) or raises HarnessError for problems that happen before a
run exists (invalid request, recursion, provider unavailable).
"""

import json
import os
import time
from pathlib import Path

from . import adapters, quota, schema, store, template, v0, worker
from .adapters.base import ProviderUnavailable

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class HarnessError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message

    def as_dict(self):
        return {"error": {"kind": self.kind, "message": self.message}}


# --- delegate ----------------------------------------------------------------------------------------------------

def delegate_template(name, values, workdir, overrides=None, wait=True, progress=None, provider=None):
    """Expand a request template (harness/template.py) and delegate it; the run's meta records the template."""
    if os.environ.get("AGENT_HARNESS_DEPTH"):
        raise HarnessError("invalid_request", "refusing to delegate from inside a delegate (AGENT_HARNESS_DEPTH is set)")
    try:
        request, info = template.expand(name, values, workdir, overrides)
    except template.TemplateError as e:
        raise HarnessError("invalid_request", str(e))
    return delegate(request, wait=wait, progress=progress, provider=provider, template_info=info)


def delegate(request, wait=True, progress=None, provider=None, template_info=None):
    """Start a run for request and (by default) wait for it. provider overrides the request's (tests use "fake")."""
    if os.environ.get("AGENT_HARNESS_DEPTH"):
        raise HarnessError("invalid_request", "refusing to delegate from inside a delegate (AGENT_HARNESS_DEPTH is set)")
    errors = schema.validate_request(request)
    if errors:
        raise HarnessError("invalid_request", "; ".join(errors))
    if not Path(request["workdir"]).is_dir():
        raise HarnessError("invalid_request", f"workdir is not a folder: {request['workdir']}")

    if request.get("request_key"):
        existing = store.find_by_request_key(request["request_key"])
        if existing:
            current = result(existing.name, full=True)
            # A failed, timed-out or refused run is retried as a new run under the same key.
            if current["state"] in ("running", "done", "blocked", "turn_limit"):
                return _wait(existing, progress) if wait else result(existing.name)

    # AGENT_HARNESS_TEST_PROVIDER lets protocol tests reach the fake provider through the MCP server.
    adapter = adapters.get(provider or os.environ.get("AGENT_HARNESS_TEST_PROVIDER") or request.get("provider", "codex"))
    try:
        if adapter.name == "codex":
            adapter.exe()
    except ProviderUnavailable as e:
        raise HarnessError("provider_unavailable", str(e))

    cfg = store.config()
    defaults = adapter.defaults(cfg)
    rdir = store.new_run_dir(request["task"])
    store.acquire(rdir, "launcher")
    try:
        store.write_json(rdir / "request.json", request)
        store.write_text(rdir / "prompt.md", adapter.render(request))
        meta = {
            "run_id": rdir.name,
            "contract": schema.CONTRACT_VERSION,
            "provider": adapter.name,
            "provider_version": adapter.version(),
            "type": request["type"],
            "task": request["task"],
            "workdir": request["workdir"],
            "model": request.get("model") or defaults["model"],
            "effort": request.get("effort") or defaults["effort"][request["type"]],
            "timeout_s": request.get("timeout_s") or cfg["default_timeout_s"],
            "request_key": request.get("request_key"),
            "request_template": template_info,
            "prompt_version": adapter.prompt_version,
            "schema_enforcement": adapter.schema_enforcement,
            "capabilities": adapter.capabilities(),
            "created": store.now_iso(),
            "thread_id": None,
            "breakaway": None,
            "turns": [],
        }
        started = _start_turn(rdir, meta, adapter, cfg, turn=1)
    except BaseException:
        store.release(rdir)
        raise
    if not started:
        return result(rdir.name)
    return _wait(rdir, progress) if wait else result(rdir.name)


def _start_turn(rdir, meta, adapter, cfg, turn):
    """With the lock held by this process: check quota, record the turn, launch its worker. Returns False if the
    run was refused (the refused result is written and the lock released)."""
    before = adapter.quota_snapshot()
    entry = {"turn": turn, "quota_before": before, "warnings": quota.announce(before)}
    meta["turns"].append(entry)
    refused = quota.refusal(before, cfg)
    if refused:
        entry.update(state="refused", finished=store.now_iso(), seconds=0)
        store.write_json(rdir / "meta.json", meta)
        store.write_json(rdir / "result.json", worker.build_result(
            rdir, meta, "refused", None, {"kind": "quota_refused", "message": refused}))
        store.release(rdir)
        return False
    store.write_json(rdir / "meta.json", meta)
    store.write_json(rdir / "result.json", worker.build_result(rdir, meta, "running", None, None))

    if os.environ.get("AGENT_HARNESS_INLINE_WORKER"):
        # Tests: run the worker in this process; it already holds the lock.
        try:
            worker.execute(rdir, turn)
        except Exception as e:
            worker.fail(rdir, "provider_error", f"worker crashed: {type(e).__name__}: {e}")
            raise
        finally:
            store.release(rdir)
        return True
    pid, breakaway = worker.spawn(rdir.name, turn)
    if meta.get("breakaway") is None:
        meta["breakaway"] = breakaway
        store.write_json(rdir / "meta.json", meta)
    store.hand_over(rdir, os.getpid(), pid, "worker")
    return True


def _wait(rdir, progress=None, poll=1.0):
    """Block until the run leaves `running`, sending progress (message, elapsed seconds) about every 10 s."""
    start = last = time.monotonic()
    if progress:
        progress(f"run {rdir.name} started", 0)
    while True:
        res = result(rdir.name)
        if res["state"] != "running":
            return res
        now = time.monotonic()
        if progress and now - last >= 10:
            last = now
            progress(f"run {rdir.name} running", round(now - start))
        time.sleep(poll)


# --- reading -----------------------------------------------------------------------------------------------------

def _dir(run_id):
    try:
        rdir = store.run_dir(run_id)
    except ValueError as e:
        raise HarnessError("invalid_request", str(e))
    if not rdir.is_dir():
        raise HarnessError("invalid_request", f"no such run: {run_id}")
    return rdir


def result(run_id, full=False):
    """The run's current result, with its label. A run whose worker died is marked failed (interrupted).
    Unless full, findings are trimmed to fit inline."""
    rdir = _dir(run_id)
    meta = store.read_json(rdir / "meta.json")
    if v0.is_v0(meta):
        res = v0.to_result(rdir, meta)
        return res if full else _fit_inline(res)
    res = store.read_json(rdir / "result.json")
    if res is None:
        raise HarnessError("invalid_request", f"run {run_id} has no result yet")
    if res["state"] == "running" and not store.lock_holder(rdir):
        res = _mark_interrupted(rdir) or res
    res["label"] = store.read_json(rdir / "label.json")
    return res if full else _fit_inline(res)


def _mark_interrupted(rdir):
    try:
        store.acquire(rdir, "reader")
    except store.Locked:
        return None  # someone else took it over; read again later
    try:
        res = store.read_json(rdir / "result.json")
        if res and res["state"] == "running":
            worker.fail(rdir, "interrupted", "the worker stopped before writing a result (its process is gone); "
                                             "re-run with the same request_key, or start a new run")
        return store.read_json(rdir / "result.json")
    finally:
        store.release(rdir)


def _fit_inline(res):
    cap = store.config()["inline_result_chars"]
    if len(json.dumps(res)) <= cap or not res.get("answer"):
        return res
    res = json.loads(json.dumps(res))
    findings = sorted(res["answer"]["findings"], key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 1))
    total = len(findings)
    while findings and len(json.dumps(res)) > cap:
        findings.pop()
        res["answer"]["findings"] = findings
    res["truncated"] = True
    res["warnings"].append(f"{total - len(findings)} of {total} findings left out to fit inline; "
                           f"the full answer is in {res['run_dir']} (or use the full result)")
    return res


# --- reply and label ---------------------------------------------------------------------------------------------

def reply(run_id, text, wait=True, progress=None):
    """Answer a blocked run's question in the same provider session."""
    if os.environ.get("AGENT_HARNESS_DEPTH"):
        raise HarnessError("invalid_request", "refusing to reply from inside a delegate (AGENT_HARNESS_DEPTH is set)")
    rdir = _dir(run_id)
    if not text.strip():
        raise HarnessError("invalid_request", "reply is empty")
    try:
        store.acquire(rdir, "launcher")
    except store.Locked as e:
        raise HarnessError("invalid_request", f"run {run_id} is busy: {e}")
    try:
        if v0.is_v0(store.read_json(rdir / "meta.json")):
            raise HarnessError("invalid_request", f"run {run_id} is a v0 record; v0 runs can't be replied to")
        current = store.read_json(rdir / "result.json")
        if not current or current["state"] != "blocked":
            raise HarnessError("invalid_request",
                               f"run {run_id} is {current['state'] if current else 'unreadable'}, not blocked")
        meta = store.read_json(rdir / "meta.json")
        adapter = adapters.get(meta["provider"])
        turn = len(meta["turns"]) + 1
        store.write_text(rdir / f"reply-{turn}.md", text if text.endswith("\n") else text + "\n")
        started = _start_turn(rdir, meta, adapter, store.config(), turn)
    except BaseException:
        store.release(rdir)
        raise
    if not started:
        return result(run_id)
    return _wait(rdir, progress) if wait else result(run_id)


def label(run_id, outcome, mast=None, note=None):
    rdir = _dir(run_id)
    entry = {"outcome": outcome, "mast": mast, "note": note, "labelled": store.now_iso()}
    props = schema.load("result.schema.json")["properties"]["label"]
    errors = schema.validate(entry, dict(props, type="object"))
    if errors:
        raise HarnessError("invalid_request", "; ".join(errors))
    store.write_json(rdir / "label.json", entry)
    return result(run_id)


# --- report and doctor -------------------------------------------------------------------------------------------

def report(workdir=None, limit=20):
    """Recent runs (newest first), runs in flight, pending questions, totals, and quota now."""
    rows, totals = [], {"runs": 0, "input_tokens": 0, "output_tokens": 0, "outcomes": {}}
    norm = os.path.normcase(os.path.abspath(workdir)) if workdir else None
    for rdir in store.list_run_dirs():
        meta = store.read_json(rdir / "meta.json")
        if not meta:
            continue
        if norm and os.path.normcase(os.path.abspath(meta.get("workdir", ""))) != norm:
            continue
        try:
            res = result(rdir.name, full=True)
        except HarnessError:
            continue
        outcome = (res["label"] or {}).get("outcome", "unlabelled")
        totals["runs"] += 1
        totals["input_tokens"] += res["usage"]["input_tokens"]
        totals["output_tokens"] += res["usage"]["output_tokens"]
        totals["outcomes"][outcome] = totals["outcomes"].get(outcome, 0) + 1
        q = res["quota"] or {}
        rows.append({
            "run_id": rdir.name, "state": res["state"], "type": meta["type"], "task": meta["task"],
            "provider": res["provider"], "effort": res["effort_applied"], "turns": len(meta["turns"]),
            "tokens": res["usage"]["input_tokens"] + res["usage"]["output_tokens"],
            "quota": f"{_pct(q.get('used_percent_before'))}->{_pct(q.get('used_percent_after'))}" if q else None,
            "outcome": outcome, "request_key": meta.get("request_key"),
            "template": _template_tag(meta.get("request_template")),
            # Waiting for a reply: blocked, not yet labelled (labelling means it was dealt with), and replyable.
            "question": ((res["answer"] or {}).get("questions")
                         if res["state"] == "blocked" and res["label"] is None and res["prompt_version"] != "v0"
                         else None),
        })
    rows.reverse()
    now = adapters.get("codex").quota_snapshot()
    return {
        "runs": rows[:limit],
        "in_flight": [r["run_id"] for r in rows if r["state"] == "running"],
        "pending_questions": [{"run_id": r["run_id"], "questions": r["question"]} for r in rows
                              if r["question"] is not None],
        "totals": totals,
        "quota_now": {"codex": now},
        "quota_updates": quota.recent_events(),
    }


def _template_tag(info):
    return f"{info['name']}@{info['version']}" if info else None


def _pct(v):
    return "?" if v is None else f"{v:g}%"


def doctor():
    checks = []
    try:
        store.home().mkdir(parents=True, exist_ok=True)
        probe = store.home() / ".doctor-probe"
        store.write_text(probe, "ok")
        probe.unlink()
        checks.append(("run store writable", True, str(store.home())))
    except OSError as e:
        checks.append(("run store writable", False, f"{store.home()}: {e}"))
    checks.append(("not inside a delegate", not os.environ.get("AGENT_HARNESS_DEPTH"),
                   "AGENT_HARNESS_DEPTH is " + ("set" if os.environ.get("AGENT_HARNESS_DEPTH") else "unset")))
    checks.append(_breakaway_check())
    checks += adapters.get("codex").doctor()
    problems = schema.check_contract()
    checks.append(("contract files valid", not problems, "; ".join(problems) or str(schema.CONTRACT_DIR)))
    # ok is True, False, or "warn" (works, with a weaker guarantee); only False fails the doctor.
    status = {True: "ok", False: "fail", "warn": "warn"}
    return {"ok": all(ok is not False for _, ok, _ in checks),
            "checks": [{"check": c, "status": status[ok], "detail": d} for c, ok, d in checks]}


def _breakaway_check():
    if os.name != "nt":
        return ("workers can detach", True, "not Windows")
    import subprocess
    import sys
    try:
        proc = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=worker.DETACHED_PROCESS
                                | worker.CREATE_NO_WINDOW | worker.CREATE_BREAKAWAY_FROM_JOB)
        proc.wait(timeout=30)
        return ("workers can leave the session's job object", True, "breakaway allowed")
    except OSError as e:
        return ("workers can leave the session's job object", "warn",
                f"breakaway refused ({e}); workers stay in the app's job, so a run may stop if the job is closed "
                "(e.g. when the app exits); results will carry a warning")

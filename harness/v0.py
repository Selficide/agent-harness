"""Reads v0 run records (written by the retired codex_delegate.py) as contract v1 results, without changing them.

A v0 run folder has meta.json without a "contract" field; its result.json is the Codex answer, not a v1 result.
Turn N's files carry the suffix recorded in meta.turns[N].suffix ("" for the first, "-2" after).
"""

import datetime as dt
import shutil
from pathlib import Path

from . import schema, store

USAGE_KEYS = {"input_tokens": "input_tokens", "cached_input_tokens": "cached_input_tokens",
              "output_tokens": "output_tokens", "reasoning_output_tokens": "reasoning_tokens"}


def is_v0(meta):
    return isinstance(meta, dict) and "contract" not in meta and "turns" in meta


def _with_offset(stamp):
    """v0 wrote local times without an offset; attach this machine's."""
    if not stamp:
        return None
    try:
        return dt.datetime.fromisoformat(stamp).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


def _quota(before, after):
    ref = after or before
    if not ref:
        return None
    return {
        "plan_type": ref.get("plan_type"),
        "window": "primary",  # v0 recorded only the primary window
        "used_percent_before": (before or {}).get("used_percent"),
        "used_percent_after": (after or {}).get("used_percent"),
        "window_minutes": ref.get("window_minutes"),
        "resets_at": _with_offset(ref.get("resets_at")),
    }


def to_result(rdir, meta):
    rdir = Path(rdir)
    turns = meta.get("turns") or []
    last = turns[-1] if turns else {}
    usage = {v: 0 for v in USAGE_KEYS.values()}
    for t in turns:
        for k, v in (t.get("usage") or {}).items():
            if k in USAGE_KEYS and isinstance(v, int):
                usage[USAGE_KEYS[k]] += v

    answer, problem = None, None
    if meta.get("type") in ("review", "research"):
        data = store.read_json(rdir / f"result{last.get('suffix', '')}.json")
        if data is None:
            problem = "no answer was recorded"
        elif schema.validate(data, schema.answer_schema(meta["type"])):
            problem = "answer doesn't match the schema"
        else:
            answer = data

    if last.get("timed_out"):
        state, error = "timed_out", {"kind": "timeout", "message": "the v0 run timed out"}
    elif answer is not None:
        state, error = answer["status"], None
    elif last.get("exit_code") not in (0, None) or last.get("errors"):
        msg = "; ".join(str(e) for e in (last.get("errors") or [])[-3:]) or f"exit code {last.get('exit_code')}"
        state, error = "failed", {"kind": "provider_error", "message": msg}
    elif meta.get("type") == "execute":
        state, error = "done", None
    else:
        state, error = "failed", {"kind": "schema_invalid", "message": problem}

    started = _with_offset(meta.get("started")) or store.now_iso()
    seconds = round(sum(t.get("seconds") or 0 for t in turns), 1)
    finished = (dt.datetime.fromisoformat(started) + dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")
    label = store.read_json(rdir / "label.json")
    if label is None and meta.get("label"):
        label = dict(meta["label"], labelled=_with_offset(meta["label"].get("labelled")) or started)
        label.setdefault("mast", None)
        label.setdefault("note", None)
    read_only = meta.get("sandbox", "read-only") == "read-only"
    return {
        "contract": schema.CONTRACT_VERSION,
        "run_id": rdir.name,
        "run_dir": str(rdir),
        "state": state,
        "answer": answer,
        "truncated": False,
        "provider": "codex",
        "model": meta.get("model"),
        "effort_applied": meta.get("effort"),
        "prompt_version": "v0",
        "capabilities": {"can_read_files": True, "can_run_commands": True, "can_run_python": None,
                         "network": False if read_only else None},
        "schema_enforcement": "native",
        "usage": usage,
        "quota": _quota(turns[0].get("plan_before") if turns else None, last.get("plan_after")),
        "timing": {"started": started, "finished": finished, "seconds": seconds},
        "warnings": [f"v0 run ({meta.get('template') or 'no template'}), read from its original record"],
        "error": error,
        "label": label,
    }


def migrate(src, dest=None):
    """Copy v0 run folders from src into the store; existing ones are skipped and originals are left in place.
    Returns (copied, skipped) lists of run ids."""
    src = Path(src)
    dest = Path(dest) if dest else store.runs_dir()
    dest.mkdir(parents=True, exist_ok=True)
    copied, skipped = [], []
    for rdir in sorted(p for p in src.iterdir() if p.is_dir()):
        if not is_v0(store.read_json(rdir / "meta.json")):
            continue
        if (dest / rdir.name).exists():
            skipped.append(rdir.name)
            continue
        shutil.copytree(rdir, dest / rdir.name)  # copy2: bytes and timestamps preserved
        copied.append(rdir.name)
    return copied, skipped

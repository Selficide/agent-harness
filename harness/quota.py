"""Quota snapshots and the threshold policy (plan section 1.4).

A snapshot is provider-neutral:
  {"provider", "plan_type", "taken_at", "windows": [{"name", "used_percent", "window_minutes", "resets_at"}]}
Thresholds apply to every window; the window with the highest used_percent decides a refusal and is the one a result
reports. Announced thresholds are remembered per provider and window (keyed by reset time) in quota-state.json, so
each is announced once per window.
"""

from . import store


def worst_window(snapshot):
    windows = (snapshot or {}).get("windows") or []
    windows = [w for w in windows if isinstance(w.get("used_percent"), (int, float))]
    return max(windows, key=lambda w: w["used_percent"]) if windows else None


def refusal(snapshot, cfg=None):
    """A refusal message if any window is at or above the refuse threshold, else None."""
    cfg = cfg or store.config()
    limit = cfg["quota_refuse_percent"]
    w = worst_window(snapshot)
    if w and w["used_percent"] >= limit:
        return (f"{snapshot['provider']} plan {w['name']} window is {w['used_percent']:g}% used (refusing at "
                f"{limit:g}%); it resets at {w.get('resets_at') or 'an unknown time'}")
    return None


def result_quota(before, after):
    """The result's `quota` field from snapshots taken before and after a run (either may be None)."""
    ref = after or before
    w = worst_window(ref)
    if not w:
        return None

    def pct(snap):
        for x in (snap or {}).get("windows") or []:
            if x["name"] == w["name"]:
                return x.get("used_percent")
        return None

    return {
        "plan_type": ref.get("plan_type"),
        "window": w["name"],
        "used_percent_before": pct(before),
        "used_percent_after": pct(after),
        "window_minutes": w.get("window_minutes"),
        "resets_at": w.get("resets_at"),
    }


def announce(snapshot, cfg=None):
    """Messages for thresholds this snapshot crosses for the first time in its window; records them as announced."""
    if not snapshot:
        return []
    cfg = cfg or store.config()
    path = store.home() / "quota-state.json"
    state = store.read_json(path) or {}
    provider = state.setdefault(snapshot["provider"], {})
    messages = []
    for w in snapshot.get("windows") or []:
        used = w.get("used_percent")
        if not isinstance(used, (int, float)):
            continue
        key = f"{w['name']}@{w.get('resets_at')}"
        entry = provider.setdefault(key, {"announced": [], "events": []})
        for threshold in sorted(cfg["quota_notify_percent"]):
            if used >= threshold and threshold not in entry["announced"]:
                entry["announced"].append(threshold)
                msg = (f"quota crossed {threshold:g}%: {snapshot['provider']} plan {w['name']} window is "
                       f"{used:g}% used, resets {w.get('resets_at') or 'unknown'}")
                entry["events"].append({"at": store.now_iso(), "message": msg})
                messages.append(msg)
    if messages:
        store.home().mkdir(parents=True, exist_ok=True)
        store.write_json(path, state)
    return messages


def recent_events(limit=10):
    """Announced threshold crossings, newest first, for the report."""
    state = store.read_json(store.home() / "quota-state.json") or {}
    events = [e for provider in state.values() for entry in provider.values() for e in entry.get("events", [])]
    return sorted(events, key=lambda e: e["at"], reverse=True)[:limit]

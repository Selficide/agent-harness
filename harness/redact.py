"""Secret redaction: text that looks like a credential is replaced with [REDACTED:<kind>] before a result reaches the
caller, and in the run's own files.

The delegate can read any file the user can (the sandbox limits writes, not reads), so a file that steers it could
get it to quote ~/.codex/auth.json or a private key into its answer. Patterns favour recall over precision: a
redacted quote can always be re-read at its path:line.
"""

import re
from pathlib import Path

PATTERNS = [
    # The body is base64 with line breaks, raw or JSON-escaped (\n); it stops at anything else, so a key without
    # its END line can't swallow the rest of a file.
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s\\]*(?:-----END [A-Z ]*PRIVATE KEY-----)?")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
]
# name = value, name: value, "name": "value" for names that hold secrets; the value is redacted, the name kept.
# Quotes may be JSON-escaped (\"), as in events.jsonl; values stop at a backslash so escapes stay intact.
ASSIGNMENT = re.compile(
    r"""(?P<name>\b[\w-]*(?:access_token|refresh_token|id_token|api[_-]?key|secret|password|passwd|auth[_-]?token)"""
    r"""(?:\\?["'])?\s*[:=]\s*(?:\\?["'])?)(?P<value>[^\s"',;}\\]{12,})""", re.I)


def text(s):
    """(redacted text, {kind: count})."""
    counts = {}

    def sub(kind):
        def repl(m):
            counts[kind] = counts.get(kind, 0) + 1
            return f"[REDACTED:{kind}]"
        return repl

    for kind, pattern in PATTERNS:
        s = pattern.sub(sub(kind), s)

    def assign(m):
        if m.group("value").startswith("[REDACTED:"):
            return m.group(0)
        counts["assignment"] = counts.get("assignment", 0) + 1
        return m.group("name") + "[REDACTED:assignment]"
    s = ASSIGNMENT.sub(assign, s)
    return s, counts


def value(obj):
    """(obj with every string redacted, {kind: count}), for JSON-like data."""
    totals = {}

    def walk(x):
        if isinstance(x, str):
            out, counts = text(x)
            for k, n in counts.items():
                totals[k] = totals.get(k, 0) + n
            return out
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        return x
    return walk(obj), totals


def file(path):
    """Redact a run file in place. Returns {kind: count}."""
    path = Path(path)
    if not path.exists():
        return {}
    original = path.read_text(encoding="utf-8", errors="replace")
    out, counts = text(original)
    if counts:
        path.write_text(out, encoding="utf-8")
    return counts


def warning(counts):
    if not counts:
        return None
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    return (f"redacted: text that looks like secrets was replaced in the result and the run's files ({kinds}); if the "
            "delegate quoted a credential it may have been steered to read one, so check which files it opened")

"""Request templates: fixed task wording with gaps the caller fills (harness/templates/<name>.json).

A template fixes everything that should stay the same between runs (type, objective, criteria, out_of_scope, notes,
effort) and declares vars for the parts that change. Callers fill vars and may set only the request fields in
OVERRIDES, so they can't add author context or reword the criteria. Changing a template's wording means bumping its
template_version, so labels stay comparable.

Placeholders, substituted once (values are never re-expanded):
  "{name}"           a var; inside a string it must be a string var, as a whole list element any var (lists splice)
  "{capture:file}"   as a whole list element: the path of that capture's output

Captures are commands the template names, run in workdir before the run starts. Callers fill only their vars, and
a var used in a capture may not start with "-", so it can't become an option. Output is stored by content under
<home>/captures/, so the exact text reviewed is kept and a repeated request_key finds the same files.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import store

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
OVERRIDES = ("request_key", "timeout_s", "effort", "model")
TEMPLATE_KEYS = {"template", "template_version", "description", "vars", "captures", "require_any", "request"}
VAR_KEYS = {"type", "required", "default", "help"}
PLACEHOLDER = re.compile(r"\{([a-z_]+)(?::([A-Za-z0-9._-]+))?\}")
CAPTURE_TIMEOUT_S = 60


class TemplateError(Exception):
    pass


def names():
    return sorted(p.stem for p in TEMPLATE_DIR.glob("*.json"))


def load(name):
    """A template by name, checked for shape. Raises TemplateError."""
    if name not in names():
        raise TemplateError(f"no such template: {name!r} (have: {', '.join(names()) or 'none'})")
    t = json.loads((TEMPLATE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    problems = check(t, name)
    if problems:
        raise TemplateError(f"template {name} is malformed: " + "; ".join(problems))
    return t


def check(t, name):
    """Problems with a template's shape, as a list of strings."""
    problems = [f"unknown key {k!r}" for k in t if k not in TEMPLATE_KEYS]
    if t.get("template") != name:
        problems.append(f"'template' must be {name!r}")
    if not isinstance(t.get("template_version"), int):
        problems.append("'template_version' must be an integer")
    for v, spec in (t.get("vars") or {}).items():
        problems += [f"var {v}: unknown key {k!r}" for k in spec if k not in VAR_KEYS]
        if spec.get("type") not in ("string", "list"):
            problems.append(f"var {v}: type must be 'string' or 'list'")
    request = t.get("request") or {}
    for k in ("workdir", "request_key"):
        if k in request:
            problems.append(f"request may not set {k!r}")
    captures = t.get("captures") or {}
    for c in t.get("require_any") or []:
        if c not in captures:
            problems.append(f"require_any names unknown capture {c!r}")
    return problems


def describe():
    """One line per template, for tool descriptions."""
    lines = []
    for name in names():
        t = load(name)
        vs = ", ".join(f"{v}{'' if s.get('required') else '?'} ({s['type']})" for v, s in t["vars"].items())
        lines.append(f"{name}: {t['description']} vars: {vs}")
    return lines


def expand(name, values, workdir, overrides=None):
    """(request, info): the template filled with values and captures, ready for core.delegate.
    info records the template, its version and the vars, for the run's meta. Raises TemplateError."""
    t = load(name)
    workdir = Path(workdir)
    if not workdir.is_absolute() or not workdir.is_dir():
        raise TemplateError(f"workdir must be an absolute path to a folder: {workdir}")
    unknown = sorted(set(overrides or {}) - set(OVERRIDES))
    if unknown:
        raise TemplateError(f"templates fix these fields; only {', '.join(OVERRIDES)} may be set: {unknown}")
    filled = _fill_vars(t["vars"], values or {})

    capture_paths = {}
    for fname, argv in (t.get("captures") or {}).items():
        args = [_substitute_str(a, filled, for_command=True) for a in argv]
        capture_paths[fname] = _capture(args, workdir, fname)
    if t.get("require_any") and all(capture_paths[c].stat().st_size == 0 for c in t["require_any"]):
        raise TemplateError(f"nothing to review: {', '.join(t['require_any'])} all empty")

    request = {"workdir": str(workdir)}
    for key, value in t["request"].items():
        if isinstance(value, list):
            request[key] = _substitute_list(value, filled, capture_paths)
        elif isinstance(value, str):
            request[key] = _substitute_str(value, filled)
        else:
            request[key] = value
    request.update(overrides or {})
    info = {"name": name, "version": t["template_version"], "vars": filled}
    return request, info


def _fill_vars(specs, values):
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise TemplateError(f"unknown vars: {unknown} (template takes: {', '.join(specs)})")
    filled = {}
    for v, spec in specs.items():
        if v in values:
            value = values[v]
        elif spec.get("required"):
            raise TemplateError(f"var {v!r} is required: {spec.get('help', '')}")
        else:
            value = spec.get("default", [] if spec["type"] == "list" else "")
        if spec["type"] == "list" and isinstance(value, str):
            value = [value]
        ok = (isinstance(value, str) if spec["type"] == "string"
              else isinstance(value, list) and all(isinstance(x, str) and x for x in value))
        if not ok:
            raise TemplateError(f"var {v!r} must be a {'string' if spec['type'] == 'string' else 'list of strings'}")
        filled[v] = value
    return filled


def _substitute_str(text, filled, for_command=False):
    def sub(m):
        var, arg = m.group(1), m.group(2)
        if arg is not None or var not in filled or not isinstance(filled[var], str):
            raise TemplateError(f"placeholder {m.group(0)!r} can't be used inside a string")
        if for_command and filled[var].startswith("-"):
            raise TemplateError(f"var {var!r} may not start with '-': {filled[var]!r}")
        return filled[var]
    return PLACEHOLDER.sub(sub, text)


def _substitute_list(items, filled, capture_paths):
    out = []
    for item in items:
        m = PLACEHOLDER.fullmatch(item)
        if m and m.group(1) == "capture":
            if m.group(2) not in capture_paths:
                raise TemplateError(f"unknown capture {m.group(2)!r}")
            out.append(str(capture_paths[m.group(2)]))
        elif m and m.group(1) in filled and isinstance(filled[m.group(1)], list):
            out.extend(filled[m.group(1)])
        else:
            out.append(_substitute_str(item, filled))
    return out


def _capture(argv, workdir, fname):
    try:
        proc = subprocess.run(argv, cwd=workdir, capture_output=True, timeout=CAPTURE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TemplateError(f"capture {fname} ({' '.join(argv)}) failed: {e}")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[-500:]
        raise TemplateError(f"capture {fname} ({' '.join(argv)}) exited {proc.returncode}: {err}")
    digest = hashlib.sha256(proc.stdout).hexdigest()[:16]
    folder = store.home() / "captures"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}-{fname}"
    if not path.exists():
        store.write_text(path, proc.stdout.decode("utf-8", "replace"))
    return path

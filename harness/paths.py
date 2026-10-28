"""The files a request names: check they exist, expand wildcard patterns, and after a run measure which of them the
delegate reached (coverage).

An item in inputs or source_of_truth names a path if it is one, optionally followed by a note in parentheses
("meta/Engine.md (the Layers section)"). It looks like a path when it is absolute, when it has a separator and its
first segment has no spaces ("wiki/sources/GPS.gov - GPS Accuracy.md"), or when it is one word with an extension
("quota.py"). Anything else is a phrase ("the code's own intended behaviour", "The docstrings in harness/quota.py") and
is left alone; so are URLs. A path that holds * ? or [ and doesn't exist is a wildcard pattern.
"""

import glob
import os
import re
from pathlib import Path

PATH_FIELDS = ("inputs", "source_of_truth")
MAX_PATTERN_MATCHES = 200
COVERAGE_SHOWN = 5


def path_part(item):
    """The path an item names, without a trailing note in parentheses, or None if the item is a phrase."""
    if "://" in item:
        return None
    if " (" in item and item.endswith(")"):
        item = item[:item.index(" (")]
    if re.match(r"([A-Za-z]:)?[/\\]", item):
        return item
    first = re.split(r"[/\\]", item, maxsplit=1)[0]
    if ("/" in item or "\\" in item) and first and not re.search(r"\s", first):
        return item
    if not re.search(r"\s", item) and re.search(r"\.[A-Za-z0-9]{1,8}$", item):
        return item
    return None


def resolve(request):
    """(request, patterns, problems): a copy of request with wildcard patterns replaced by the files they match
    (sorted, as found under workdir), {pattern: match count}, and a list of problems (missing paths, patterns that
    match nothing or too much). The caller refuses the request if there are problems."""
    workdir = Path(request["workdir"])
    out, patterns, problems = dict(request), {}, []
    for field in PATH_FIELDS:
        if field not in request:
            continue
        items = []
        for item in request[field]:
            path = path_part(item)
            if path is None or (workdir / item).exists() or (workdir / path).exists():
                items.append(item)
            elif any(c in path for c in "*?["):
                matches = sorted(m for m in glob.glob(path, root_dir=workdir, recursive=True)
                                 if (workdir / m).is_file())
                patterns[item] = len(matches)
                if not matches:
                    problems.append(f"{field}: pattern matches no files: {item}")
                elif len(matches) > MAX_PATTERN_MATCHES:
                    problems.append(f"{field}: pattern matches {len(matches)} files (more than "
                                    f"{MAX_PATTERN_MATCHES}); narrow it: {item}")
                # A pattern with a note stays as written, so the note isn't lost.
                items += [Path(m).as_posix() for m in matches] if path == item else [item]
            else:
                problems.append(f"{field}: no such file or folder: {path}")
                items.append(item)
        out[field] = items
    return out, patterns, problems


def coverage(request, commands, files_read):
    """The files named in request's inputs and source_of_truth that appear in none of the delegate's commands and
    not in its files_read. Folders and phrases aren't counted. Returns (unreached, total)."""
    workdir = Path(request["workdir"])
    haystack = "\n".join(_norm(x) for x in list(commands) + list(files_read))
    expected, unreached = [], []
    for field in PATH_FIELDS:
        for item in request.get(field) or []:
            name = path_part(item) or item
            path = workdir / name
            if name in expected or not path.is_file():
                continue
            expected.append(name)
            forms = {_norm(name).removeprefix("./"), _norm(str(path.resolve()))}
            if not any(_mentions(haystack, f) for f in forms):
                unreached.append(name)
    return unreached, len(expected)


def outside_paths(request, commands, allowed=()):
    """Existing paths outside workdir that the delegate's commands name: absolute paths, home references (~, $HOME,
    $env:USERPROFILE, %APPDATA%, ...) and ../ escapes. Paths the request names, the extra allowed roots, system
    program folders, and each command's executable are not counted. Returns sorted display strings."""
    workdir = Path(request["workdir"]).resolve()
    roots = [workdir] + [Path(r).resolve() for r in allowed]
    folders = set()  # folders holding named files: listing them is fine, their other files aren't
    for field in PATH_FIELDS:
        for item in request.get(field) or []:
            part = path_part(item)
            if part and Path(part).is_absolute():
                roots.append(Path(part).resolve())
                folders.add(Path(part).resolve().parent)
    roots += [Path(p) for p in SYSTEM_ROOTS if Path(p).exists()]
    found = set()
    for command in commands:
        text = _expand_home(re.sub(r"\\+", "\\\\", command))
        text = re.sub(r'^\s*("[^"]+"|\S+)', "", text)  # the executable (e.g. powershell.exe) isn't a read
        for m in re.finditer(r"(?<![\w.:/\\-])([A-Za-z]:[\\/]|/(?=[A-Za-z])|\.\.[\\/])", text):
            path = _existing_prefix(text[m.start():], workdir)
            if path and path not in folders and not any(path == r or r in path.parents for r in roots):
                found.add(str(path))
    return sorted(found)


SYSTEM_ROOTS = ["C:/Windows", "C:/Program Files", "C:/Program Files (x86)", "/bin", "/sbin", "/usr", "/System",
                "/Library", "/opt/homebrew", "/dev", "/private/var/folders"]
HOME_REFS = r"\$env:(USERPROFILE|HOMEPATH|HOME|APPDATA|LOCALAPPDATA)|%(USERPROFILE|APPDATA|LOCALAPPDATA)%|\$\{?HOME\}?"


def _expand_home(text):
    home = str(Path.home())
    env = {"appdata": os.environ.get("APPDATA") or home, "localappdata": os.environ.get("LOCALAPPDATA") or home}
    text = re.sub(HOME_REFS, lambda m: env.get((m.group(1) or m.group(2) or "").lower(), home), text,
                  flags=re.IGNORECASE)
    return re.sub(r"(?<![\w.~/\\-])~(?=[\\/]|['\"\s]|$)", lambda m: home, text)


def _existing_prefix(text, base):
    """The longest path at the start of text that exists (paths may hold spaces; quotes and shell punctuation end
    them), resolved against base when relative. None if no prefix exists."""
    candidate = re.split(r"['\"|;,<>\n\r`]", text, maxsplit=1)[0][:400].rstrip(" )")
    while candidate:
        path = (base / candidate) if not Path(candidate).is_absolute() else Path(candidate)
        try:
            if path.exists():
                return path.resolve()
        except OSError:
            pass
        if " " not in candidate:
            return None
        candidate = candidate.rsplit(" ", 1)[0]
    return None


def outside_warning(found):
    if not found:
        return None
    shown = ", ".join(found[:COVERAGE_SHOWN])
    more = f" and {len(found) - COVERAGE_SHOWN} more" if len(found) > COVERAGE_SHOWN else ""
    return (f"outside workdir: the delegate's commands named {len(found)} path(s) outside the working folder that the "
            f"request didn't name: {shown}{more}; a file it read may have steered it there, so check why before "
            "trusting the result")


def instruction_files(workdir, names=("AGENTS.md", "AGENTS.override.md"), up_to_git_root=True):
    """Instruction files (AGENTS.md, CLAUDE.md) a provider loads for a run in workdir: in workdir and each folder
    above it, up to the git root (Codex: only workdir outside git) or to the filesystem root (Claude Code). Files in
    the home folder itself are the user's own and aren't listed."""
    folder = Path(workdir).resolve()
    chain = [folder, *folder.parents]
    if up_to_git_root:
        top = next((p for p in chain if (p / ".git").exists()), None)
        chain = [p for p in chain if p == top or top in p.parents] if top else [folder]
    chain = [p for p in chain if p != Path.home().resolve()]
    return [str(p / name) for p in chain for name in names if (p / name).is_file()]


def coverage_warning(unreached, total):
    if not unreached:
        return None
    shown = ", ".join(unreached[:COVERAGE_SHOWN])
    more = f" and {len(unreached) - COVERAGE_SHOWN} more" if len(unreached) > COVERAGE_SHOWN else ""
    return (f"coverage: {len(unreached)} of {total} files in inputs and source_of_truth appear in none of the "
            f"delegate's commands or its files_read, so it may not have read them: {shown}{more}")


def _norm(text):
    # Codex records Windows commands with doubled backslashes (C:\\WINDOWS), and shell quoting doubles single quotes
    # inside single-quoted strings (PowerShell, e.g. What''s New).
    text = re.sub(r"\\+", "/", text).replace("''", "'")
    return text.lower() if os.name == "nt" else text


def _mentions(haystack, form):
    """form appears in haystack as a whole path, not as the tail of a longer name (a.md inside data.md)."""
    return re.search(r"(?<![\w.-])" + re.escape(form) + r"(?![\w-])", haystack) is not None

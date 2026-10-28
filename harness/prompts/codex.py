"""Renders a request into the brief Codex receives. Wording follows OpenAI's Codex prompting guide: point to files,
use the word "review" for reviews, no requests for plans or progress updates.

Bump PROMPT_VERSION whenever the wording changes, so runs stay comparable (results record it).
"""

import os

PROMPT_VERSION = "codex-2"

# codex-2: on Windows, Windows PowerShell 5.1 shows UTF-8 files garbled (Get-Content reads them as the ANSI code
# page, and its console output re-encodes even with -Encoding UTF8), which made reviewers report valid characters
# such as ² and ± as corrupted. rg's output reaches Codex intact. The example pattern is "^", not "": Windows
# PowerShell drops empty arguments to native programs.
WINDOWS_ENCODING = ("Files are UTF-8. If your shell is Windows PowerShell, its Get-Content and console output garble "
                    "non-ASCII characters (², ±, é, —), even with -Encoding UTF8. Read files with rg, e.g. "
                    "`rg -n \"^\" <path>`, or start the command with "
                    "`[Console]::OutputEncoding=[Text.Encoding]::UTF8;` and use `Get-Content -Encoding UTF8`. "
                    "Don't report a character as corrupted unless rg shows it corrupted too.")


def _bullets(items):
    return "\n".join(f"- {x}" for x in items)


def render(request):
    r = request
    parts = [f"# Task: {r['task']}", ""]
    if r["type"] == "review":
        parts += [
            "Review the files below against the source of truth.", "",
            "## Objective", r["objective"], "",
            "## What to review", _bullets(r["inputs"]), "",
            "## Source of truth", _bullets(r["source_of_truth"]), "",
            "## What counts as a finding (most important first)", _bullets(r["criteria"]), "",
        ]
    else:
        parts += [
            r["objective"], "",
            "## Where to look", _bullets(r["inputs"]), "",
        ]
    if r.get("out_of_scope"):
        parts += ["## Out of scope", _bullets(r["out_of_scope"]), ""]
    if r.get("notes"):
        parts += ["## Notes", r["notes"], ""]
    if r["type"] == "review":
        parts.append("Do not modify files. Give every finding a `path:line` location and the evidence for it. "
                     "If you find nothing, say what you checked.")
    else:
        parts.append("Do not modify files. Back every finding with a `path:line`, short quote, or URL, and mark "
                     "your confidence.")
    if os.name == "nt":
        parts.append(WINDOWS_ENCODING)
    parts.append("List every file you read in files_read. If you cannot continue without an answer from the "
                 "requester, set status to blocked and ask one targeted question.")
    return "\n".join(parts) + "\n"


def render_reply(text):
    return text if text.endswith("\n") else text + "\n"

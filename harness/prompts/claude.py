"""Renders a request into the brief Claude Code (`claude -p`) receives: the Codex brief without the Windows encoding
note, since Claude reads files with its own Read tool rather than a shell.

Bump PROMPT_VERSION whenever the wording changes, so runs stay comparable (results record it).
"""

from . import codex

PROMPT_VERSION = "claude-1"


def render(request):
    return codex.render(request, encoding_note=False)


def render_reply(text):
    return codex.render_reply(text)

# agent-harness

A small harness for Claude Code to delegate tasks to other models. v0 delegates to OpenAI's Codex CLI.

- `codex_delegate.py`: runs `codex exec` from a brief file and records the run under `runs/codex/` (gitignored).
- `templates/`: brief templates for `review`, `research`, and `execute` tasks.
- `schemas/`: JSON output schemas Codex must follow for `review` and `research` tasks.
- `CLAUDE.md`: the rules Claude follows when delegating.

Requires Python 3 (standard library only) and a logged-in `codex` on PATH.

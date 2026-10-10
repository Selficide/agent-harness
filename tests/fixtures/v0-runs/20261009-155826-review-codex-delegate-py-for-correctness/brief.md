# Task: Review codex_delegate.py for correctness bugs
Type: review
Template: review-v1

Review `codex_delegate.py` against its own docstring and the `codex exec` CLI it wraps.

## What to review
- codex_delegate.py

## Source of truth
- The module docstring and argparse help in codex_delegate.py
- `codex exec --help` and `codex exec resume --help` (you may run these)

## What counts as a finding
- Bugs that make a run, resume, or label fail or record wrong data in meta.json
- CLI flags passed to codex that are invalid or misplaced
- Windows-specific problems (paths, encoding, subprocess)

## Out of scope
- Style, naming, and missing features

Do not modify files. Give every finding a `path:line` location and the evidence for it.
If you find nothing, say what you checked.

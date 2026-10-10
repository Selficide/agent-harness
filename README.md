# agent-harness

A small harness for Claude Code to delegate review and research tasks to other models, through one versioned
contract. v1 delegates to OpenAI's Codex CLI (read-only).

- `CONTRACT.md`: the contract for callers; `contract/v1/`: its JSON Schema files.
- `harness/`: the package (`python -m harness …`): core, run store, detached worker, Codex adapter, quota policy, CLI.
- `tests/`: `python -m unittest`; fixtures are recorded v0 runs.
- `docs/`: the design review, build plan, and spike results.
- `CLAUDE.md`: the rules Claude follows when delegating.

Runs are stored in `~/.agent-harness/runs/` (override with `AGENT_HARNESS_HOME`), outside the repo.

Requires Python 3.14 (standard library only) and a logged-in `codex` on PATH. Check with `python -m harness doctor`.

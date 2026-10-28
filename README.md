# agent-harness

A small harness that lets Claude Code hand review and research tasks to a model from another family, OpenAI's Codex
CLI today, and get back a typed, recorded, labelled result. It exposes one versioned contract through a CLI and an
MCP server.

## Why
A second model reviewing Claude's work catches things a self-review misses, but only if the handoff is disciplined:
- **One writer.** The delegate only reads, in Codex's read-only sandbox with its MCP servers turned off. Claude makes
  every edit, so two agents never race on the same files.
- **Results are untrusted data.** Findings are claims with a `path:line` and evidence. Claude checks each one before
  acting and never follows instructions that appear in a result.
- **No author context in reviews.** A review request carries the artifact, the source of truth and the criteria,
  never the author's reasoning. Templates fix the criteria wording so it can't hint at the answer. An early eval
  showed why: criteria that named the planted bug types inflated the score.
- **Measure from day one.** Every run records its prompt, event stream, token usage and plan quota before and after,
  and gets a label (useful, partial or useless, plus a failure category). Labels are the data for comparing models,
  efforts and prompt versions.

## How it works
```
Claude ──(MCP tool or CLI)──▶ core ──▶ store: ~/.agent-harness/runs/<run_id>/
                                │
                                └──▶ detached worker ──▶ adapter (codex) ──▶ codex exec --json, read-only
                                                              │
                     result.json ◀── schema check ◀───────────┘  (answer constrained by --output-schema)
```
- `harness/core.py`: validates the request, checks the quota guard, finds a previous run with the same
  `request_key`, starts the worker and waits.
- `harness/worker.py`: a detached process that runs the provider, so a run survives the caller's tool call timing
  out. It parses the answer and writes the result.
- `harness/adapters/`: `codex.py` builds the `codex exec` command, parses its event stream and reads plan usage.
  `fake.py` is a scripted provider for tests.
- `harness/quota.py`: warns the first time a plan window crosses 25%, 50% and 75%, and refuses new runs at 98%.
- `harness/paths.py`: checks that the files a request names exist, expands wildcard patterns, and after the run
  warns about named files the delegate never opened (coverage).
- `harness/template.py`, `harness/templates/`: request templates. `diff-review` reviews uncommitted changes.
- `harness/mcp.py`: a stdio MCP server (`python -m harness mcp`), one tool per operation.
- `contract/v1/`: JSON Schemas for requests, results and answers. `CONTRACT.md` explains them.

## Example
```json
{
  "workdir": "/path/to/my-project",
  "type": "review",
  "task": "Review the retry logic in client.py",
  "objective": "Find defects in the retry and backoff code.",
  "inputs": ["src/client.py"],
  "source_of_truth": ["docs/retry-spec.md"],
  "criteria": ["Behaviour that contradicts the spec", "Crashes or hangs on valid inputs"]
}
```
```
python -m harness delegate request.json
```
The result has a `state` (`done`, `blocked`, `failed`, `timed_out`, `refused` and others), the answer's findings
(`severity`, `location`, `claim`, `evidence`, `confidence`), the files the delegate read, token usage and quota.
If the delegate asks a question, the run is `blocked`; answer it with `reply` rather than starting over. To review
your uncommitted changes:
```
python -m harness delegate --template diff-review --var spec=docs/retry-spec.md
```

## Setup
Requires Python 3.14 (standard library only) and a logged-in `codex` CLI on PATH. Runs on Windows 11 and macOS; the
test suite passes on both. On macOS, Homebrew's `python@3.14` installs as `python3.14`, so use that in place of
`python` in the commands here.
```
python -m harness doctor
python -m unittest
```
To use it from Claude Code, register the MCP server. With `PYTHONPATH` set to this folder it works from any project:
```
claude mcp add agent-harness --scope user -e PYTHONPATH=<this folder> -- <python 3.14> -m harness mcp
```
`CLAUDE.md` holds the rules Claude follows when delegating. Runs are stored in `~/.agent-harness/runs/` (override with
`AGENT_HARNESS_HOME`), outside the repo.

## Status and evidence
- In daily use as the cross-model reviewer for [wiki-engine](https://github.com/Selficide/wiki-engine) vaults: as of
  2026-10-22, 60 recorded runs, 47 labelled useful, 4 partial and 9 useless (most of the useless ones are test runs
  and infrastructure failures, not bad reviews). 94 unit tests.
- Contract v1 is done (milestones in `docs/plan-contract-v1.md`). Design review: `docs/review-contract-v1.md`.
- `evals/seeded-quota/`: a copy of `harness/quota.py` with 4 planted bugs. A clean run found 4 of 4 with no false
  positives.
- `docs/review-adversarial-2026-10-20.md`: Codex reviewed the harness that runs it, with a control run outside the
  harness. The two medium findings are fixed. The low-severity ones (mostly races between concurrent runs) are open
  and listed there.
- Next work: `docs/next.md`.

## License
MIT. See `LICENSE`.

# Plan: harness contract v1

*Phase 2 of `docs/planning-prompt-contract-v1.md`, following `docs/review-contract-v1.md` and the owner's answers
there. Written 2026-10-10. No code yet; this is for review.*

## Goal
Let Claude, from any repo, hand review and research tasks to the owner's **free** model accounts (Codex; Gemini was
dropped on 2026-10-11 because it no longer has a free tier) through one stable contract, with every run recorded, labelled, and counted against the account's quota.

## Scope of v1
- **In:** `review` and `research` tasks; read-only access; the Codex provider; a CLI and an MCP server over one core;
  quota tracking with status updates; outcome labels; reading v0 run records.
- **Out (v2 or later):** write access (worktrees, diffs), a second provider (only if one with a free tier
  appears; Gemini was dropped), a Claude adapter (not planned), the modern MCP protocol era.

## 1. Contract v1
Defined once as JSON Schema (draft 2020-12) in `contract/v1/`. The CLI's arguments and the MCP tool schemas are
generated from these files. Schemas use only a small subset of JSON Schema (`type`, `properties`, `required`,
`additionalProperties`, `items`, `enum`, `const`, `minLength`/`maxLength`, `minItems`, `minimum`/`maximum`, plus
`description` and `$schema`/`$id`/`title` as annotations), because we validate them ourselves (no third-party
validator) and because Claude Code rejects or flattens some constructs (root-level `anyOf`/`oneOf`). `type` may be
an array (e.g. `["string", "null"]`): that is how nullable fields are written, since `anyOf` is out and Codex's
strict mode needs every property listed in `required`.

### 1.1 Request (`delegate`)
| Field | Type | Required | Notes |
|---|---|---|---|
| `workdir` | string, absolute path | yes | Folder the delegate works in. The repo tag is derived from its git top level. |
| `type` | `review` \| `research` | yes | |
| `task` | string, ≤120 chars | yes | One line; names the run folder. |
| `objective` | string | yes | What a good answer achieves. |
| `inputs` | string[] (paths), ≥1 | yes | Relative to `workdir` or absolute. The delegate reads them itself. |
| `criteria` | string[] | review: yes | What counts as a finding, most important first. |
| `source_of_truth` | string[] | review: yes | Paths, or a phrase like "the code's own intended behaviour". |
| `out_of_scope` | string[] | no | |
| `notes` | string | no | Anything the fields don't cover. Frequent use signals a missing field. |
| `provider` | `codex` | no | Default `codex`. The enum grows with adapters. |
| `model`, `effort` | string; `low`…`max` | no | Requests. The result records what was applied. |
| `access` | `read` (const) | no | Present so `write` can be added later without breaking callers. |
| `request_key` | string | no | Idempotency: a repeat with the same key returns the existing run. |
| `timeout_s` | integer | no | Default 900. |

Per-type requirements (review needs `criteria` and `source_of_truth`) are checked by the core, not the schema, and
fail with a clear error. Unknown fields are rejected (`additionalProperties: false`).

### 1.2 Result
| Field | Notes |
|---|---|
| `contract` | `"1"` |
| `run_id`, `run_dir` | `run_dir` holds the rendered prompt, raw events, and stderr. |
| `state` | `running` \| `done` \| `blocked` \| `failed` \| `timed_out` \| `turn_limit` \| `refused` |
| `answer` | `{status, summary, findings[], questions[], files_read[]}` or null. Review findings: `{severity, location, claim, evidence, confidence}`; research findings: `{claim, evidence, confidence}`. |
| `truncated` | true when findings were cut to fit the inline cap (about 8,000 tokens); the full answer is in `run_dir`. |
| `provider`, `model`, `effort_applied`, `prompt_version` | What actually ran. |
| `capabilities` | `{can_read_files, can_run_commands, can_run_python, network}` as the adapter detected them. |
| `schema_enforcement` | `native` (Codex `--output-schema`) or `validated` (prompt plus check plus one retry). |
| `usage` | `{input_tokens, cached_input_tokens, output_tokens, reasoning_tokens}` |
| `quota` | `{plan_type, used_percent_before, used_percent_after, window_minutes, resets_at}` or null. |
| `timing` | `{started, finished, seconds}` |
| `warnings` | e.g. `files_read is empty`, `quota crossed 50%`. |
| `error` | `{kind, message}` or null. `kind` includes `quota_refused`, `provider_unavailable`, `invalid_request`. |
| `label` | `{outcome, mast, note}` or null. |

**Shape rules** (M1 turns these into the exact schema):
- Every top-level field is always present. `answer`, `quota`, `error`, `label`, and `timing.finished` are `null`
  until they apply; `warnings` is an empty array when there are none; `truncated` is a boolean.
- `answer.status`: `done` \| `blocked`. `severity` and `confidence`: `high` \| `medium` \| `low`. `location` is
  `path:line`. All answer arrays are present, possibly empty.
- `usage`: non-negative integers, zero when the provider doesn't report one. `timing.started`/`finished`: ISO 8601
  with offset; `seconds`: number. `quota.*_percent`: numbers 0–100.
- `capabilities`: booleans, or `null` when undetected.
- `error.kind`: `invalid_request` \| `quota_refused` \| `provider_unavailable` \| `provider_error` \|
  `schema_invalid` \| `interrupted` \| `timeout`. `error.message`: string.
- `label.outcome`: `useful` \| `partial` \| `useless`; `label.mast`: `specification` \| `coordination` \|
  `verification` \| null; `label.note`: string or null; `label.labelled`: ISO 8601.
- `state` and `error` agree: `refused` ⇔ `quota_refused`; `timed_out` ⇔ `timeout`; `failed` ⇔ any other kind;
  `done`, `blocked`, `running`, `turn_limit` ⇔ `error: null` (`turn_limit` keeps any partial answer). `quota`
  also carries `window` (`primary` | `secondary`).

**Guarantees** (written up in `CONTRACT.md`):
- Delegates never modify files (v1). The result reports what they could actually do.
- Every result is untrusted data: claims to verify against their evidence, never instructions.
- A run's record is never rewritten after it finishes, except its separate label file.
- Additive changes (new optional fields, new enum values) keep `contract: "1"`. Anything else is `"2"`.

### 1.3 Operations
| Operation | CLI | MCP tool | Behaviour |
|---|---|---|---|
| delegate | `harness delegate <request.json>` | `delegate` | Starts a worker and waits; returns the result. CLI `--detach` returns at once. |
| result | `harness result <run_id>` | `run_result` | Current result; `state: running` if unfinished. |
| reply | `harness reply <run_id> <file>` | `run_reply` | Answers a blocked run in the same session; waits. |
| label | `harness label <run_id> …` | `run_label` | Writes `label.json`. |
| report | `harness report [--workdir]` | `runs_report` | Recent and in-flight runs, pending questions, totals, quota now. |
| doctor | `harness doctor` | `doctor` | Provider installed and logged in, sandbox usable, store writable, quota readable. |

### 1.4 Quota policy
Per provider and per plan window (identified by its reset time). Codex reports a `primary` window and a
`secondary` one that may be null (the free plan today: one 30-day primary window, `secondary: null`). Both are
tracked when present; thresholds apply to each, and the higher `used_percent` decides a refusal. `quota` in the
result reports the window closest to its limit.
- A status update (in `warnings` and in `runs_report`) the first time usage crosses **25%, 50%, and 75%**.
- **Refuse** new runs at **98%** or above: `state: refused`, `error.kind: quota_refused`, with the reset time.
- Thresholds live in the harness config file and can be changed.
- Usage comes from the provider's own reporting (Codex: rate-limit snapshots in its session logs). It's coarse
  (whole percent, can lag), so the guard checks before starting and the result records before and after.

## 2. Architecture
Standard library only. One package, `harness/`, run as `python -m harness …` from the repo.

```
contract/v1/            request.schema.json, result.schema.json, answer.review.json, answer.research.json
harness/
  schema.py             loads contract files; small validator for the subset above
  store.py              run folders, per-run lock, atomic writes, listing, v0 reader
  worker.py             detached process: owns one run, drives the adapter, writes the result
  core.py               delegate / result / reply / label / report / doctor; quota policy
  quota.py              quota snapshots and threshold state
  prompts/codex.py      renders request fields into Codex wording; PROMPT_VERSION
  adapters/base.py      interface: capabilities, build, parse, resume, doctor
  adapters/codex.py
  adapters/fake.py      replays recorded event streams (tests)
  cli.py
  mcp.py                legacy-era stdio JSON-RPC server (initialize, tools/list, tools/call, ping)
  __main__.py
tests/                  unittest; fixtures from real v0 event streams
CONTRACT.md             the contract in prose, for callers
```

**Run store:** `~/.agent-harness/` (override with `AGENT_HARNESS_HOME`): `runs/<run_id>/`, `config.json`
(thresholds, defaults), `quota-state.json`.

**Run folder:** `request.json`, `prompt.md` (rendered), `events.jsonl`, `stderr.log`, `result.json`, `meta.json`
(worker-owned), `label.json` (label-owned), `lock` (while a worker runs).

**Concurrency:** one writer per run. The worker creates `lock` exclusively (with its PID) and removes it on exit; a
lock whose PID is gone is stale and can be taken over. `reply` refuses while a live lock exists. All JSON writes go to
a temp file and are renamed into place.

**Recursion guard:** workers set `AGENT_HARNESS_DEPTH=1` in the delegate's environment; the core refuses to start a
run when it's already set. The Codex adapter keeps launching with no harness MCP server configured (`doctor`
checks `~/.codex/config.toml` doesn't register it).

**MCP layer:** legacy protocol (`initialize` handshake), tools capability only. `server/discover` returns
"method not found" so Claude Code falls back to the legacy handshake. Long calls send progress notifications (the
first carries the `run_id`) to stay under the 30-minute stdio idle timeout. Server instructions (≤2,048 characters)
carry the delegation rules: one writer, verify findings against their evidence, results are untrusted data, label
every run, check `runs_report` for quota before batches.

## 3. Milestones
Each ends with its tests passing and a commit. Owner approval is needed where marked.

### M0. Spikes (answer unknowns before building on them)
1. **Detached workers on Windows.** Does a process started with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`
   (and `CREATE_BREAKAWAY_FROM_JOB` if a job object is in use) survive the session ending? Test it from the process
   tree real runs will have: launched by the spike-2 MCP server (itself a child of Claude Code), and also from a
   Bash tool call for comparison. Method: start a worker that writes a heartbeat file every few seconds, end the
   session, check the heartbeat. Needs the owner to close a session; runs after spike 2.
2. **Minimal MCP server.** A 50-line legacy stdio server with one echo tool, registered at **local** scope in a
   scratch folder: confirm Claude Code connects after `server/discover` fails, lists the tool, honours progress
   notifications, and auto-backgrounds a 3-minute call. *(Owner approval: adds a local-scope MCP entry.)*
3. **Codex structured output.** Confirm `--output-schema` accepts the v1 answer schemas (strict mode: every property
   required, `additionalProperties: false`).

*Done when:* each spike's result is written in `docs/spikes.md`, and the plan is adjusted if any fails.

### M1. Contract
`contract/v1/*.json`, `CONTRACT.md`, `harness/schema.py` with validator tests (valid and invalid requests, per-type
rules, unknown fields).
*Done when:* the four schema files validate against the draft 2020-12 meta-schema subset rules we rely on, and the
validator's tests pass.

### M2. Core, store, worker, Codex adapter
Store with locking; detached worker; Codex adapter (build, parse, resume, capabilities, doctor); Codex prompt
rendering with `PROMPT_VERSION`; quota snapshots and threshold policy; fake adapter.
Tests: replay the real v0 `events.jsonl` files through the parser; fake-adapter runs for every state (done, blocked,
failed, timed out, refused); stale lock; concurrent `label` and `reply`; quota thresholds crossing once per window;
recursion guard.
*Done when:* tests pass, and one live Codex review through the core matches a v0 run's quality on the same brief.

### M3. CLI and migration
`python -m harness` commands from 1.3. Copy v0 runs from `runs/codex/` into the new store (originals kept until the
owner deletes them); the store reader recognises v0 records (no `contract` field). Retire `codex_delegate.py`,
`templates/`, and `schemas/`; update `CLAUDE.md` and `README.md`.
Migration tests use copies of real v0 run folders as fixtures: one labelled single-turn run, one unlabelled run,
and a constructed two-turn (resumed) run. They check that the v0 reader maps each to a v1 result: label taken from
`meta.json` into `label`, usage summed across turns, `answer` from `result.json` (or `result-N.json` for the last
turn), `quota` from the plan-usage fields when present and `null` before tracking existed, and `prompt_version: "v0"`.
They also check the fixtures' files are byte-identical afterwards (no rewriting).
*Done when:* those tests pass, `harness report` lists v0 and v1 runs together with totals matching the v0
`report` command's, and `doctor` passes.

### M4. MCP server
`harness/mcp.py` and its server instructions. Protocol tests drive it over stdio with scripted JSON-RPC.
Register at **user** scope. *(Owner approval: global Claude Code config.)*
*Done when:* from a session in another repo (the vault), `delegate` runs a review end to end, gets backgrounded
after 2 minutes, and `run_result` recovers a run after the session restarts.
*Status: done 2026-10-11.* Acceptance runs `20261011-021624`, `-021726`, `-021834` (vault workdir, all labelled).
`doctor` all ok. A run lost mid-flight (caller stopped and the MCP server process killed about 40s in) kept running;
Claude Code restarted the server on the next call, `runs_report` listed the run in flight, and `delegate` with the
same `request_key` reattached to it. Not confirmed: backgrounding after 2 minutes. The longest wait was about 95s
and stayed in the foreground. `runs_report` shows 0 tokens for a run in flight because Codex reports usage only in
`turn.completed`. (`events.jsonl` does stream live; directory listings just show a stale size while it's open.)

### M5. Vault integration and the Phase 1 experiment
- The vault rule for cross-model review on complex ingests, pointing at the `delegate` tool. *(Owner approval: vault
  schema change.)*
- The experiment: about 20 vault review tasks, each given to Codex (through the harness) and to a Claude sub-agent
  (in session, same rendered brief, clean context). Compare verified findings, misses, false positives, and cost.
  Results go in a report in the harness repo.

### M6. Gemini adapter: dropped (2026-10-11)
Gemini no longer has a free tier, so it's out of scope. The contract keeps `schema_enforcement: validated` and the
adapter interface for any future provider that can't enforce a schema natively.

## 4. Risks carried into the build
- **Detached workers may not survive** a Claude Code session on Windows (M0.1). There's no fallback that keeps the
  full recovery promise, so this is a decision point, not an automatic fallback. If M0.1 fails, stop and bring the
  owner two options: (a) launch workers through something outside Claude Code's process tree, such as a Windows
  scheduled task triggered on demand (a persistent system configuration change, so the owner decides); or
  (b) accept a weaker guarantee: runs die with the session, the next read marks them `failed` with
  `error.kind: interrupted`, and the caller re-runs with the same `request_key`. Option (b) changes the v1
  acceptance criteria (section 5) and the M4 "done when", and must be written back into this plan.
- **MCP spec drift.** Kept affordable by a thin `mcp.py` and protocol tests.
- **Codex CLI changes** breaking event parsing. Recorded fixtures make breakage visible; `doctor` reports the
  version.
- **Quota reporting is coarse.** A single run can cross 98% after the check; accepted.
- **Hand-written validator** may diverge from real JSON Schema semantics. Kept affordable by the small subset and by
  testing it against Claude Code's actual acceptance (M4).

## 5. Acceptance for v1 as a whole
- A review from the vault goes through `delegate` and comes back as a typed result with findings, usage, and quota.
- A lost call is recoverable by `run_id`, by `request_key`, or from `runs_report`.
- Quota updates appear at 25/50/75%, and a run is refused at 98%.
- v0 and v1 runs report together.
- No third-party dependencies; tests run with `python -m unittest`.

## Codex review of this plan
Codex (gpt-6-luna, high effort, clean context; run `20261010-014946`) reviewed the plan against the review, the owner's
answers, the planning prompt, the code, and the Codex CLI's help. 3 findings, all checked and fixed above:
1. *(high)* The detached-worker fallback ("keep runs attached to the server") contradicted the recovery model. Now
   a decision point with two explicit options, and the knock-on changes named.
2. The result contract lacked requiredness, nullability, and allowed values. Added "Shape rules".
3. The v0 migration had no test for labels in `meta.json` and multi-turn usage. Added fixture-based migration tests.

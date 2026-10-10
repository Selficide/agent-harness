# Critical review: harness contract v1

*Phase 1 of `docs/planning-prompt-contract-v1.md`. Written 2026-10-10 by Claude, then reviewed by Codex (see the end).
No code has changed. Facts marked **[verified]** were checked against current documentation or this machine on
2026-10-10; **[unverified]** means I couldn't check it.*

## Summary
The core idea holds: a versioned, provider-agnostic contract with the harness owning wording, storage, and
normalization. Four things in the proposal are wrong or need to change:

1. **The MCP server isn't a long-lived shared process.** It's a subprocess each Claude Code session launches. Runs
   must therefore live outside the server, as detached workers with all state on disk.
2. **The async API isn't needed for Claude Code.** Claude Code already moves MCP calls longer than 2 minutes into
   the background and notifies on completion. A blocking `delegate` is simpler, and with detached workers it isn't
   fragile.
3. **The MCP spec changed fundamentally in July 2026**, and we're writing our own protocol code (owner decision made
   on 2026-10-10, after the planning prompt was written, which still lists the SDK as the leaning). That
   is a maintenance cost to plan for, not a reason to avoid MCP, but it argues for building the core and CLI first and
   keeping the MCP layer as thin as possible.
4. **Write access should be out of v1.** It's the largest and riskiest part of the contract (worktrees, diffs, who
   applies changes), and the principles say delegates read. v1 = review and research only.

And one addition: **Claude itself should be a provider.** `claude -p` has nearly everything an adapter needs, and it's
the cleanest way to run the Phase 1 experiment (same brief, same recording, different model family).

## 1. Facts checked

| Claim | Status |
|---|---|
| Stdio MCP servers are subprocesses launched by the client | **[verified]** MCP spec 2026-07-28, transports: "newline-delimited messages over the standard streams of a client-launched subprocess". Claude Code: "Background tasks don't survive exiting the session". |
| MCP tool calls time out | **[verified]** `MCP_TOOL_TIMEOUT` defaults to about 28 hours. Stdio idle timeout 30 min (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`). Calls still running after 2 min are auto-backgrounded to a task, result delivered as a notification (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`). Progress notifications don't extend the wall-clock limit. |
| Server instructions carry the delegation rules | **[verified]** Loaded at session start with tool names (tool search defers the schemas). **Truncated at 2,048 characters**, as are tool descriptions. |
| Tool results size | **[verified]** `MAX_MCP_OUTPUT_TOKENS` default 25,000; warning at 10,000; text over 50,000 chars saved to a file. |
| Tool input schemas | **[verified]** Must validate against JSON Schema draft 2020-12; property names ASCII `[A-Za-z0-9_.-]`, 1–64 chars; a root-level `anyOf`/`oneOf`/`allOf` gets flattened and only described, not enforced. |
| Per-tool approval | **[verified]** Tools appear as `mcp__<server>__<tool>` for permission rules; a server can force a prompt on every call with `_meta["anthropic/requiresUserInteraction"]`. |
| MCP protocol eras | **[verified]** Revision 2026-07-28 is "modern": no `initialize`, version and capabilities in every request's `_meta`, servers MUST implement `server/discover`. 2025-11-25 and earlier are "legacy" (`initialize` handshake). A dual-era client probes `server/discover` and falls back to `initialize` on a non-modern error. |
| Which era Claude Code speaks | **[verified]** v2 runtime (v2.1.232+) asks stdio servers for 2026-07-28 and uses it if supported, otherwise connects "as v1 does". `MCP_PROTOCOL_NEGOTIATION=legacy` forces the old handshake. Installed: 2.1.293 and 2.1.295. |
| MCP Tasks extension for long runs | **[partly]** Exists in the spec (`io.modelcontextprotocol/tasks`). Claude Code's docs don't say whether it supports it. Not needed given auto-backgrounding. |
| `claude -p` as an adapter | **[verified]** `--output-format json/stream-json`, **`--json-schema`** (validated output), `--permission-mode plan`, `--tools`, `--disallowedTools`, `--max-turns`, `--max-budget-usd`, `--effort`, `--model`, `--resume`, `--no-session-persistence`, `--bare`, `--restricted`, `--strict-mcp-config`, `--permission-prompts none`. |
| `claude` on PATH | **[verified] No.** Only bundled with the desktop app at versioned paths, e.g. `%APPDATA%\Claude\claude-code\2.1.295\<hash>\claude.exe`. |
| `codex mcp-server` (Codex as an MCP server) | **[verified] Not in 0.162.0.** There's an experimental `app-server`. |
| Gemini CLI | **[unverified]** Not installed. Docs (ingested in the vault): no output-schema flag; headless turns "ask" into "deny"; Windows sandbox persistently changes integrity levels. |
| Python | **[verified]** 3.14.8. No MCP SDK (owner decision: own code). |

## 2. Decision by decision

### Clean, versioned contract; repos only supply content. **Holds.**
This is the right boundary, and it's what makes the next points survivable: if the contract is the stable thing, the
transport (CLI or MCP, legacy or modern era) and the providers can change underneath. Add one rule: **the contract is
defined once, as JSON Schema files, and both the CLI and the MCP tool schemas are generated from them.** Otherwise
they drift.

### MCP as the main interface. **Holds, with a different build order.**
What MCP really gives us in Claude Code today:
- typed, validated inputs (Claude can't call `delegate` without an `objective`);
- automatic backgrounding of long calls, with a completion notification;
- per-tool permission rules, so read-only tools can be always-allowed;
- presence in every repo via user scope, with no paths or environment variables.

What it costs, given our own protocol code:
- a protocol layer to maintain across spec revisions (the July 2026 revision changed the basics);
- a server restart for every code change, and Claude Code doesn't reconnect a dropped stdio server automatically;
- 2,048 characters for the delegation rules (enough, if they're terse);
- harder debugging than a CLI.

A CLI plus a user-level skill would give the rules more room and an easier development loop, but loses typed inputs,
auto-backgrounding, and per-tool permissions. **Recommendation:** keep MCP as the interface I use, but build in this
order: contract, then core and CLI, then a thin MCP layer that only translates tool calls to core calls. The MCP layer
should be small enough to rewrite in an afternoon when the spec moves.

**Own protocol code vs the SDK** (the owner chose own code): the SDK would track spec changes for us and handle
edge cases (cancellation, progress, batching) we'd otherwise write and test ourselves; own code keeps the harness
dependency-free and fully understood. The legacy, tools-only subset we need is small (four methods and one
notification), so own code is reasonable if the layer stays that small. If we ever need resources, prompts,
elicitation, or the modern era, revisit the cost.

**Protocol era:** implement **legacy only** (`initialize` handshake, tools capability, `tools/list`, `tools/call`,
`ping`), and answer `server/discover` with "method not found" so modern clients fall back. Claude Code handles that
fallback today. Add the modern era later if a client needs it.

### "One long-running shared process". **Wrong.** (My claim in the earlier conversation.)
Each Claude Code session spawns its own server. Consequences:
- A run started inside the server process dies with the session (and Windows doesn't kill child processes reliably
  either way, so we'd also leak orphans).
- **Runs must be detached worker processes** (`harness worker <run_id>`) that own the provider subprocess and write
  all state to the run folder atomically (write to a temp file, then rename). The server and CLI only start workers
  and read run folders. Then two sessions, the CLI, or a crashed session all see the same runs.
- Two concurrent sessions may each run a server; the run store must tolerate concurrent readers and writers.
  Atomic renames only stop partial reads; they don't stop two writers overwriting each other's updates. So:
  **one writer per run at a time.** The worker owns a run folder while it runs and holds a lock file (created
  exclusively, removed on exit, stale if its PID is gone). `resume` refuses while a worker holds the lock and starts
  a new worker that takes it. Labels go in their own file (`label.json`), never in the worker's `meta.json`, so
  labelling never races a worker. Tests must cover concurrent `resume`/`label` and a stale lock.

### Async API (`delegate` returns an ID; poll for status). **Not needed; replace with blocking plus recovery.**
- `delegate` waits for the run and returns the result. Claude Code backgrounds it after 2 minutes.
- If the call is lost (session ended, server restarted), `run_result(run_id)` reads it from disk. **But a lost call
  never returned its `run_id`**, so the caller needs another way to find the run: `runs_report` lists recent and
  in-flight runs filtered by `workdir` and task, and `delegate` accepts an optional caller-chosen `request_key`. A
  repeated `delegate` with the same key returns the existing run instead of starting a duplicate (idempotent retry).
  The first progress notification also carries the `run_id`.
- The 30-minute stdio idle timeout means a worker should report progress, and the server should send progress
  notifications for long runs. Progress doesn't extend the 28-hour wall clock, which isn't a concern.
- This removes `run_status` as a separate tool; `run_result` returns the state if the run isn't finished.

### `workdir`. **Holds.**
Claude Code also exposes roots (`roots/list`: launch directory plus added directories), which could supply a default.
Keep it required anyway: the server is shared by every repo in a session, and an explicit path is cheap.

### Structured fields instead of templates. **Holds, but trim and type it.**
Eight optional fields invite boilerplate. Proposal:
- **Always required:** `type` (`review` | `research`), `task` (one line), `objective`, `inputs` (paths).
- **Review also requires:** `criteria`, and `source_of_truth` (which may be "the code's own intended behaviour").
- **Optional:** `out_of_scope`, `notes`.
- `context` and `done_when` belong to execute tasks, which v1 drops (below).
- Avoid a root-level `oneOf` per type: Claude Code flattens it and stops enforcing it. Use one flat schema and
  validate the per-type requirements in the core, returning a clear error.

### Results: inline or a file reference? **Inline, with a reference.**
The result I act on (status, summary, findings with evidence, questions, `files_read`, usage, capabilities, the
settings actually applied) comes back inline as structured JSON: it's what I need immediately, and reviews so far
produced 1–5 findings, a few thousand tokens at most. The raw material (rendered prompt, event stream, stderr) stays
in the run folder, and the result includes the folder path. Guard against size: past a cap (say 8,000 tokens, under
Claude Code's 10,000-token warning), the result keeps the highest-severity findings inline, marks itself truncated,
and points to the full file.

### The harness owns wording, per provider, versioned. **Holds.** Add: store the exact rendered prompt in the run
folder, plus its version, so any run can be reproduced and prompt changes compared.

### Provider-agnostic adapters. **Holds.** The adapter interface:
`capabilities()`, `build(request) -> command`, `parse(event_stream) -> normalized result`, `resume(run, reply)`, and
`doctor()` (is the CLI installed and logged in, is the sandbox usable). `doctor` exists because two of our three
real setup problems (stdin, the Windows sandbox) would have been caught by it.

### `access: read | write`. **Simplify for v1: read only.**
Write access drags in worktrees, returning diffs, applying them, and conflict handling. The design principles say
delegates read and Claude writes, and every real use so far (two ingest reviews, the planned experiment) is
read-only. Keep `access` in the schema with one allowed value so adding `write` later is additive. Results still
report capabilities actually available (e.g. `can_run_commands`, `can_run_python: false` for Codex on this machine).

### Normalized states, usage, central store, labels. **Holds.** Two changes:
- **Store location:** move run data out of the code repo, to a user data directory (e.g. `~/.agent-harness/runs`,
  overridable). Code and data have different lifecycles, and the store now serves every repo.
- **Treat delegate output as untrusted data.** Delegates read untrusted material (fetched articles, other people's
  code). Results must be presented as claims to verify, never instructions. Put this in the server instructions.

### Codex prompting rules. **Hold.** Sourced from OpenAI's guide; no change.

## 3. Claude as a provider
Yes, in v1. Reasons:
- The Phase 1 experiment needs the same brief sent to two model families and recorded the same way. In-session
  sub-agents can't give that: different prompt path, no schema enforcement, no comparable record.
- `claude -p` has native schema enforcement, read-only permission mode, turn and budget caps, effort, model, and
  resume.

Risks to design for:
- **Recursion.** A delegate that can see the harness's MCP tools could delegate again. Every adapter must launch its
  CLI without our server (`claude --strict-mcp-config` with an empty config, or `--bare`; confirm Codex and Gemini
  configs don't include it), and the harness should refuse to run when an environment marker shows it's already
  inside a delegate.
- **Locating the binary.** `claude` isn't on PATH; the desktop app's path changes with each update. Options: install
  the standalone CLI, or have the adapter find the newest bundled version. (Question for the owner.)
- **Cost.** Runs count against the owner's Claude subscription; `--max-budget-usd` gives a hard cap per run.
- **Contamination.** The delegate must not load the owner's CLAUDE.md, memory, or skills, or it isn't a clean context:
  use `--bare` or `--restricted` and test that it really starts clean.

## 4. Gemini's differences
- No output-schema flag: the adapter puts the schema in the prompt, validates, and retries once with the validation
  errors. Results record `schema_enforcement: validated`. Budget one extra turn.
- Headless default denies the shell. For review and research that's acceptable: its read-only tools (`read_file`,
  `glob`) stay allowed. Capabilities report `can_run_commands: false`.
- The Windows native sandbox persistently lowers files' integrity levels. For read-only use, prefer `plan` mode
  (policy-level read-only) over the native sandbox, or Docker. Decide when Gemini is installed.
- **Defer the Gemini adapter** until the CLI is installed and its JSON `stats` fields can be seen. The adapter
  interface should be designed with it in mind, which this review does.

## 5. Risks and failure modes (MAST)
- **Specification:** vague briefs (mitigated by required fields); the rendered prompt drifting from what Codex is
  tuned for (mitigated by versioned prompts and eval labels).
- **Coordination:** recursion (above); orphaned workers; two sessions writing the same store; a blocked run whose
  question never gets answered (show pending questions in `runs_report`).
- **Verification:** delegate output trusted without checking (rule in server instructions; findings carry evidence);
  a delegate that silently couldn't read anything still returning plausible findings (the first Windows-sandbox run did
  the right thing, but don't rely on it: require `files_read` and flag runs where it's empty).
- **Operational:** quota exhaustion (free Codex plan: about 1% per review; add a warning threshold); CLI version
  changes breaking event parsing (keep recorded event streams as parser test fixtures); spec changes breaking the MCP
  layer (keep it thin).

## 6. Testing approach (for the plan)
- Contract tests against a **fake provider** that replays recorded event streams: deterministic and free.
- Parser tests on the real recorded `events.jsonl` from v0 runs.
- One live smoke test per provider, run by hand, behind `doctor`.
- Protocol tests for the MCP layer: drive the server over stdio with scripted JSON-RPC.
- v0 run records stay readable through a small reader; no rewriting of old files.

## 7. Questions for the owner
1. **v1 scope:** review and research only, with write access deferred to v2?
2. **Claude adapter in v1?** If yes: install the standalone `claude` CLI, or locate the desktop app's bundled copy?
3. **Run store location:** a user data directory (`~/.agent-harness/runs`) or keep it in the repo?
4. **Build order:** contract, core and CLI first, then the MCP layer?
5. **Quota guard:** warn, or refuse, above some percentage of the Codex plan window? What threshold?

## Codex review of this document
Codex (gpt-6-luna, high effort, clean context; run `20261010-012211`) reviewed this document against the planning
prompt, the code, the Codex CLI's help, and the design principles. 4 findings, each checked and folded in above:
1. The SDK-vs-own-code choice was called an owner decision while the prompt still lists the SDK as the leaning.
   Correct on the documents; the decision came later in the conversation. Now stated, with the trade-off.
2. A blocking `delegate` whose call is lost never returned a `run_id`, so recovery by ID was impossible. Fixed with
   `request_key`, a run listing by workdir, and the ID in the first progress notification.
3. The inline-vs-file question for results wasn't answered. Now answered.
4. Atomic writes alone don't make concurrent updates safe. Now: one writer per run (lock file), labels in a separate
   file, and tests for it.

Not challenged by Codex: the facts sourced from Claude Code and MCP documentation (out of its reach offline), and
the larger calls (MCP over CLI, Claude as a provider, read-only v1). Those rest on this review alone and are the
owner's questions in section 7.

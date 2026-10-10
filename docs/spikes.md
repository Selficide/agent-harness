# M0 spikes: results

*Plan: `docs/plan-contract-v1.md`, milestone M0. Started 2026-10-10.*

## M0.3 Codex structured output: **pass (from existing evidence; no new run)**
The v1 answer schemas are the v0 schemas (`schemas/review.schema.json`, `schemas/research.schema.json`) unchanged:
every property required, `additionalProperties: false`, string enums, no nullable fields. Codex 0.162.0 accepted them
through `--output-schema` on all 7 v0 runs (6 `done`, 1 `blocked`), and each run's `result.md` parsed as JSON in the
schema's shape. The nullable `type` arrays added to the schema subset only appear in the request and result
schemas, which the harness validates itself and never sends to Codex. If a later answer schema needs a nullable
field, check it against Codex first.

## M0.2 Minimal MCP server: **handshake passes; live checks pending**
`spikes/mcp_echo.py` (legacy era, own code, ~130 lines), registered at local scope for `D:\Dev\harness-spike`
(`claude mcp add --scope local harness-spike -- python D:/Dev/agent-harness/spikes/mcp_echo.py`).

Seen in `spikes/mcp_echo.log` when Claude Code 2.1.295 health-checked it:
1. `server/discover` with `_meta` protocol version `2026-07-28` → we answer `-32601` method not found.
2. Claude Code falls back to `initialize` with `protocolVersion: "2025-11-25"`; we echo it back.
3. `notifications/initialized`, then `tools/list`; the tools are listed and the status shows Connected.

Note: the `initialize` request carried no `jsonrpc` field (`{"method": "initialize", "params": ..., "id": 0}`), so the
server must not require it on incoming messages.

Still to check in a live session (needs the owner, see below): tool calls work, progress notifications are
accepted, and a 3-minute call is moved to the background after 2 minutes.

## M0.1 Detached workers: **partial pass; session-end check pending**
`spikes/heartbeat.py spawn` starts a process with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW |
CREATE_BREAKAWAY_FROM_JOB`, falling back to no breakaway if Windows refuses it.

From a Bash tool call in the desktop app: the launcher **is in a job object** (`IsProcessInJob` true), the
breakaway flag is **allowed**, and the child is **outside** the job. That is the precondition for surviving the session
(a job set to kill-on-close is the usual way a parent takes its children down).

A headless `claude -p` session can't stand in for the desktop session: the standalone CLI isn't signed in.

### Owner steps (one session, about 5 minutes)
1. Open a new Code session in `D:\Dev\harness-spike`.
2. Ask it to run these, one at a time:
   - `echo` with text `hi`
   - `spawn_heartbeat` with `out_file` `D:\Dev\harness-spike\hb-mcp.jsonl` and `minutes` 15
   - `sleep` with `seconds` 180 (check: it goes to the background after about 2 minutes and finishes)
3. Close or archive that session (end it fully, so its MCP server exits).
4. Come back here; Claude checks `hb-mcp.jsonl` keeps growing and `spikes/mcp_echo.log` for the progress traffic.

The Bash-launched comparison can run from this session at the same time.

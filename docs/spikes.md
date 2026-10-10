# M0 spikes: results

*Plan: `docs/plan-contract-v1.md`, milestone M0. Started 2026-10-10.*

## M0.3 Codex structured output: **pass (from existing evidence; no new run)**
The v1 answer schemas are the v0 schemas (`schemas/review.schema.json`, `schemas/research.schema.json`) unchanged:
every property required, `additionalProperties: false`, string enums, no nullable fields. Codex 0.162.0 accepted them
through `--output-schema` on all 7 v0 runs (6 `done`, 1 `blocked`), and each run's `result.md` parsed as JSON in the
schema's shape. The nullable `type` arrays added to the schema subset only appear in the request and result
schemas, which the harness validates itself and never sends to Codex. If a later answer schema needs a nullable
field, check it against Codex first.

## M0.2 Minimal MCP server: **pass (auto-backgrounding unconfirmed)**
`spikes/mcp_echo.py` (legacy era, own code, ~130 lines), registered at local scope for `D:\Dev\harness-spike`
(`claude mcp add --scope local harness-spike -- python D:/Dev/agent-harness/spikes/mcp_echo.py`).

Handshake, seen in `spikes/mcp_echo.log` with Claude Code 2.1.295:
1. `server/discover` with `_meta` protocol version `2026-07-28` → we answer `-32601` method not found.
2. Claude Code falls back to `initialize` with `protocolVersion: "2025-11-25"`; we echo it back.
3. `notifications/initialized`, then `tools/list`; the server shows as Connected.

The `initialize` request carried no `jsonrpc` field, so the server must not require it on incoming messages.

Live session (2026-10-10, session "Running repo tools", owner-driven): `echo`, `sleep` 25 s, `spawn_heartbeat`,
`sleep` 180 s all worked. Every `tools/call` carries `_meta.progressToken` (an integer) and
`_meta["claudecode/toolUseId"]`. Progress notifications every 10 s were accepted; the 180 s call returned normally.

**Unconfirmed:** whether the 180 s call was moved to the background. The transcript shows the result inline and no
backgrounding message. Doesn't block the design (`delegate` blocks either way and progress keeps the stdio idle
timer alive); re-check in M4's acceptance run, and if calls aren't backgrounded, recommend `--detach` plus
`run_result` for long reviews in the server instructions.

## M0.1 Detached workers: **pass**
`spikes/heartbeat.py spawn` starts a process with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW |
CREATE_BREAKAWAY_FROM_JOB`, falling back to no breakaway if Windows refuses it.

- Both the Bash tool and the session's MCP server run **inside a job object**; breakaway is **allowed** and the
  child runs **outside** it.
- Session-end test: the MCP server (PID 27380) spawned a 15-minute heartbeat (PID 33604) at 02:23:15. The session
  was archived and the server exited at 02:26:36 (stdin closed). The heartbeat kept writing until 02:38:10, its full
  15 minutes (180 entries), about 11.5 minutes after the session ended.

So workers launched this way survive the session. M2 keeps the planned design; the worker launcher uses the same
flags and records whether breakaway was granted (if not, warn: the run may die with the session).

### Re-check needed: breakaway refused after a reboot (2026-10-10, found in M3)
After the machine rebooted (11:02) and the desktop app restarted (11:17, packaged app
`C:\Program Files\WindowsApps\Claude_2.31226.1.0_x64__…`), processes started from this session are in a job object
that **refuses** `CREATE_BREAKAWAY_FROM_JOB` (`WinError 5`), with or without the tool sandbox. The innermost job's
limit flags are `0x0`: no breakaway allowed, but also no kill-on-close. The spike above ran before the reboot, when
breakaway was allowed.

What the harness does: the worker falls back to no breakaway, `meta.json` records `breakaway: false`, results carry a
warning, and `doctor` reports `warn`. What's unknown: whether a worker still in the app's job survives (a) the session
ending and (b) the app exiting. Re-run the M0.1 session-end test under these conditions before M4's acceptance run.

**Re-check result (2026-10-10 12:21, after the reboot): workers survive the session ending.** From a new session in
`D:\Dev\harness-spike`, the spike MCP server (PID 26076) started a 15-minute heartbeat (PID 15952). Launched from the
MCP server, `CREATE_BREAKAWAY_FROM_JOB` was *accepted*, yet the heartbeat reports `in_job: true`: it left the
server's job but is still inside an outer job, presumably the packaged app's. The session was archived and the server
exited at 12:21:53; the heartbeat kept writing (12:25:22 and later, PID alive).

So session end is safe. Not tested: the desktop app exiting or the machine restarting, which likely end the outer job
and with it any running worker. Such runs read as `failed` / `interrupted` and can be re-run with the same
`request_key`; the server instructions already say so.

# Adversarial self-review, 2026-10-20

Codex reviewed the harness that runs it, adversarially, against `CONTRACT.md`, the `contract/v1/` schemas and
`CLAUDE.md`. Scope: `harness/` without `v0.py`. Findings 1 and 2 were fixed on 2026-10-21 (see "Fixed"); the
rest are open.

## Method
- **Harness run:** `20261020-174242-adversarial-review-of-the-agent-harness` (gpt-6-luna, xhigh, 273 s, about 494k
  input tokens, quota 3% to 6%). 10 findings.
- **Control:** the same rendered prompt and answer schema, sent through plain `codex exec` with the adapter's flags,
  outside the harness. This checks that a harness bug didn't distort the review of itself. 11 findings, about 723k
  input tokens.
- Claude checked every finding against the cited code. Neither run could execute anything; all findings come from
  reading the code.

**Comparison:** 7 findings appeared in both runs, 3 only in the harness run, 4 only in the control. Both runs made
the same false positive (`notes`). The most important finding, Codex MCP servers, appeared only in the control, so
one run alone misses things. Nothing points to the harness distorting the review: both runs saw the same prompt, and
their overlap is in line with run-to-run variation.

## Valid findings, most important first
| # | Finding | Where | Found by | Severity (checked) |
|---|---|---|---|---|
| 1 | `diff-review` capture runs `git diff` with the caller's git config and environment, so `diff.external`, `GIT_EXTERNAL_DIFF` or a textconv driver runs a program outside the sandbox, before the delegate starts. It also changes what `diff.patch` contains. | `harness/template.py:164`, `harness/templates/diff-review.json:11` | both | medium, **fixed** |
| 2 | Read-only depends on `~/.codex/config.toml`: the adapter doesn't turn off configured MCP servers, which run outside Codex's sandbox. `capabilities` reports `network: false` regardless. No MCP servers are configured today, so this is latent. | `harness/adapters/codex.py:32-63` | control | medium (latent), **fixed** |
| 3 | Two simultaneous `delegate` calls with the same `request_key` can both miss the lookup and start two runs; the key isn't reserved. | `harness/core.py:50-68`, `harness/store.py:132` | both | low |
| 4 | `quota-state.json` is read, changed and written back without a lock, so concurrent runs can announce the same threshold twice or lose an event. | `harness/quota.py:58-77` | both | low |
| 5 | A schema-valid answer with `status: blocked` and empty `questions` makes the run `blocked` with nothing to answer. | `harness/worker.py:147-148`, `contract/v1/answer.review.json:26` | both | low |
| 6 | A `blocked` answer is accepted without a recorded thread id, so the reply then fails instead of resuming. | `harness/worker.py:113-148`, `harness/adapters/codex.py:59-62` | control | low |
| 7 | `acquire` creates the lock file, then writes its JSON. A contender that reads it as empty for 0.1 s removes it as stale and takes the lock, so two processes can both think they own the run (e.g. concurrent `run_reply`). | `harness/store.py:189-207` | control | low |
| 8 | Provider values aren't sanity-checked and the built result isn't validated before writing: negative token counts, `used_percent` above 100 or `NaN` (`NaN` passes the 98% refusal check), and an out-of-range `resets_at` that raises during `_start_turn`, leaving a run folder with no `meta.json`. `schema.validate_result` exists but isn't called on write. | `harness/adapters/codex.py:147,199-206`, `harness/worker.py:154`, `harness/core.py:106` | split across both | low |
| 9 | `_fit_inline` only drops findings; a result oversized for another reason (a long summary) stays oversized but is still marked `truncated`. | `harness/core.py:198-211` | both | low |
| 10 | POSIX only: retained `Popen` objects are never reaped, so a killed worker stays a zombie and `os.kill(pid, 0)` reports it alive; the run stays `running`. Doesn't apply on this Windows machine. | `harness/worker.py:35-37`, `harness/store.py:165-171` | harness | low |
| 11 | With 10+ same-task runs in one second, `-10` sorts before `-9`, so `find_by_request_key` may not return the newest. | `harness/store.py:113-134` | harness | negligible |

## Rejected
- **`notes` can carry author reasoning into a review** (both runs). "No author context" is a rule for callers
  (`CONTRACT.md` request section, `CLAUDE.md` rules for delegating). The harness can't tell reasoning from
  legitimate notes, and `diff-review` uses `notes` legitimately. Not a defect.

## Suggested fixes (not applied)
1. Run the capture as `git --no-pager -c diff.external= diff --no-ext-diff --no-textconv {base}`, or keep a fixed
   environment that drops `GIT_EXTERNAL_DIFF`.
2. Turn off MCP servers per run (check how `codex exec` accepts an empty `mcp_servers` override), and have `doctor`
   flag any configured server.
3. Write `result.json` only after `schema.validate_result` passes, failing with `schema_invalid` if it doesn't.
   Clamp or ignore `used_percent` that isn't finite or isn't between 0 and 100, and catch a bad `resets_at`.
4. Treat a `blocked` answer with no question, or with no thread id, as `schema_invalid` (or as `done` with a warning).
5. Use small lock files (`O_EXCL`) for request-key reservation and for `quota-state.json`. Write the lock contents
   before it becomes visible: write to a temporary file, then link or rename it.
6. In `_fit_inline`, set `truncated` only when findings were dropped; cap or flag an oversized summary.
7. Items 10–11: sort run folders by creation time or a zero-padded suffix; reap workers (`proc.poll()`) before the
   liveness check, or check `/proc/<pid>/stat` for zombies.

## Fixed (2026-10-21)
- **1:** the `diff-review` captures run `git -c core.fsmonitor=false diff --no-ext-diff --no-textconv {base}` (and
  `ls-files` with the same `-c`). `--no-ext-diff` also covers `GIT_EXTERNAL_DIFF`. Test:
  `tests/test_template.py`, `test_capture_ignores_external_diff_and_textconv`. Remaining gap: a `filter.<name>.clean`
  driver named in the repo's `.gitattributes` can still run when git re-reads a modified file; git has no switch to
  turn filters off. That code is the caller's own repo config, so it's left as a known limit.
- **2:** suggested fix 2 doesn't work as written: `-c mcp_servers={}` merges into the loaded config rather than
  replacing it (checked with `codex mcp list` against a scratch `CODEX_HOME`, codex-cli 0.162.0). The adapter now
  lists the servers Codex would load in the run's workdir (`codex mcp list --json`) and passes
  `-c mcp_servers.<name>.enabled=false` for each, on every turn. It fails closed: if the list can't be read, or a
  name can't be addressed in a dotted path (quotes are taken literally), the run fails as `provider_unavailable`.
  `doctor` shows the configured servers. Tests: `tests/test_core.py`, `test_command_turns_off_every_mcp_server`,
  `test_mcp_listing_fails_closed`.

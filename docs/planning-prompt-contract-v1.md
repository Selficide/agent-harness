# Planning prompt: agent-harness contract v1

Paste this into a new Claude Code session opened in `D:\Dev\agent-harness`.

---

I want to redesign this repo (`D:\Dev\agent-harness`) properly before we write more code. This session has two
phases. **Do not write or change any code in either phase.** The output is two documents for me to review.

## Phase 1: critical review (do this first, and take your time)
Read the context below, the current code, and the background material. Then challenge the proposed design. For every
decision listed under "Decided" and "Leaning", ask: is this right, what does it cost, what would break it, and is
there a simpler option? Verify technical claims against current documentation rather than trusting this prompt. I
expect some of it to be wrong. Write the result to `docs/review-contract-v1.md`:
- what holds up, what doesn't, and why;
- risks and failure modes (use MAST categories where they fit: specification, coordination, verification);
- alternatives worth considering, with trade-offs;
- facts you checked, with sources, and anything you couldn't verify;
- questions only I can answer.

Then stop and wait for me before Phase 2.

## Phase 2: plan (only after I've responded to the review)
Write `docs/plan-contract-v1.md`: the contract spec first (request and result shapes, guarantees, versioning), then
the architecture, milestones in order, how each milestone is tested, how existing run records migrate, and
acceptance criteria. Keep it as small as it can be while still being right.

## What this repo is for
A personal harness that lets Claude (in any of my repos) delegate work to other models' coding CLIs and record every
run. It's just for me for now; packaging it for others may come later, so keep the design clean but don't build for
that. The guiding principles come from a research report in my notes vault (not in this repo); read it.
In short: one writer (delegates read, review, and research; Claude makes the edits), a coordinator always checks
delegated output, hand off through files, review with a clean context and a different model family, measure from
day one, label failures.

## Current state (v0, working)
- `codex_delegate.py` (stdlib Python): `run` (brief file → `codex exec`), `resume`, `label`, `report`. Each run gets
  `runs/codex/<timestamp>-<slug>/` with `brief.md`, `result.md`/`result.json`, `events.jsonl`, `stderr.log`,
  `meta.json` (settings, timing, tokens, ChatGPT plan usage before/after, label). `runs/` is gitignored.
- `templates/` (review, research, execute briefs with header lines), `schemas/` (strict JSON output schemas for
  review and research), `CLAUDE.md` (delegation rules). Commits `fe3a4ae`, `1198257`.
- Evidence so far: 5 runs. Two real ingest reviews on the vault each found genuine errors (3 and 5 findings, all
  verified, no false positives, each missed one instance). Costs: about 260k and 400k tokens, about 1% of the plan's
  30-day window each.

## Environment facts (verify anything you rely on)
- Windows 11, PowerShell. codex-cli 0.162.0, logged in with ChatGPT (plan type "free"). Default model `gpt-6-luna`.
- `~/.codex/config.toml`: model, `model_reasoning_effort = "medium"`, `[windows] sandbox = "elevated"`. Without
  that sandbox setting every shell command was "blocked by policy". Also `~/.codex/review.config.toml` (effort high)
  and `~/.codex/AGENTS.md` (4 neutral global rules).
- `codex exec` waits on stdin unless it's closed. Codex's `memories` feature is off; the harness passes
  `--disable multi_agent`. `python.exe` is not accessible inside Codex's sandbox. About 13k tokens of built-in
  prompt per call. Plan usage is readable from rate-limit snapshots in `~/.codex/sessions/**/rollout-*.jsonl`
  (whole percent, can lag).
- Claude Code's permission classifier blocked running Codex with `--approve-for-me`. Don't design around loosening
  approvals.
- Gemini CLI is not installed. Its docs (headless mode, cheatsheet, policy engine, sandboxing, context files) are
  ingested in the vault (pages "Gemini CLI" and "Gemini CLI Docs - *").
  Codex prompting guidance is in the vault page "OpenAI - Codex Prompting Guide".

## Decided (challenge these anyway)
- The harness exports a clean, versioned contract that every repo uses. Repos never import harness code or depend on
  its file layout. Repos supply content; the harness owns everything else.
- `workdir` (the folder the delegate works in) replaces `repo_path`. The repo tag is derived from it (git top level).
- No brief templates. The request carries structured fields (objective, inputs, source_of_truth, criteria, context,
  done_when, out_of_scope, notes). The harness turns them into provider-specific wording, with a prompt version
  recorded on each run. Repo-specific rubrics live in the repo's own instructions and arrive as `criteria`.
- The contract is provider-agnostic: Codex adapter now, Gemini later. Each adapter: report capabilities, build the
  command, parse events, resume.
- `access: read | write` is a semantic promise; adapters map it. Results report what actually happened: exact model,
  effort applied, whether commands could run, how the output schema was enforced (`native` vs `validated`).
- Normalized end states (done, blocked, failed, timed_out, turn_limit) and token usage; quota tracking optional per
  provider. One central run store, tagged by repo. Outcome labels (useful/partial/useless + MAST category).
- Codex-specific prompting (from OpenAI's guide): never ask for plans or progress updates; use the word "review" for
  reviews; the escape hatch is "explicit blocker plus one targeted question"; standing rules go in AGENTS.md.
- Design notes and recommendations live in this repo's docs, not in the vault (the vault holds only what sources say).

## Leaning (not decided)
- An MCP server registered at user scope as the main interface, so the contract is self-describing in every repo
  and the delegation rules travel as server instructions. Async tools: `delegate` → run_id, `run_status`,
  `run_result`, `run_reply`, `run_label`, `runs_report`. A CLI over the same core for me and for debugging.
- ~~Python MCP SDK in a pinned venv rather than a hand-written stdlib protocol layer.~~ **Decided 2026-10-10:** no
  SDK; we maintain our own protocol code.

## Things I specifically want examined
1. **MCP vs a CLI plus a user-level skill.** Is MCP actually the cleaner boundary here, or just the more fashionable
   one? What do server instructions, tool schemas, and per-tool permissions really give us in Claude Code today?
2. **Server lifecycle.** I was told the MCP server would be "one long-running process shared across sessions". I
   doubt that: stdio servers are probably spawned per Claude Code session. If so, where does an async run live when
   the session ends? Should runs be detached processes with all state on disk? Check this properly.
3. **Async vs blocking**, given MCP tool timeouts in Claude Code and runs of 1–3+ minutes.
4. **Should Claude itself be a provider?** The next experiment compares a Claude reviewer with a Codex reviewer on the
   same brief, to test whether a different model family catches more. A provider-agnostic contract with a Claude
   adapter (e.g. headless `claude -p`) might be the cleanest way to run it. Or is in-session sub-agent delegation the
   right tool for Claude and the harness only for other providers?
5. **Contract scope.** Is the field list right? Too many fields? Should results return findings inline or as a file
   reference? How should the contract handle write access (worktrees, diffs, who applies changes)?
6. **What Gemini's differences imply**: no output-schema flag; headless mode turns "ask" into "deny", so default
   headless can't run shell commands; a Windows sandbox that persistently changes file integrity levels.
7. **Anything I haven't thought of.**

## Constraints
- Ask before adding any third-party dependency or changing global config (Claude Code settings, `~/.codex`).
- Windows is the primary platform.
- Existing v0 runs in `runs/codex/` should stay readable.

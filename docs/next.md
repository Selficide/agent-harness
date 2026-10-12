# After v1: progress and todos

*Started 2026-10-12. Contract v1 (M0–M5) is done; see `plan-contract-v1.md`. This page tracks the work since:
request templates and evals.*

## Done (2026-10-12)
- **Smoke test:** `CLAUDE.md` checked against the CLI (run `20261012-020636-…`). 6/6 findings accurate, all
  omissions, no wrong defaults. Not yet acted on; see todos.
- **Seeded-bug eval** (`evals/seeded-quota/`). The first run had hints in its criteria (3/3, inflated); the clean
  re-run found 4/4 with no false positives. Lesson: criteria must not name bug types; templates now enforce this.
- **Request templates** (`7792dc3`): `harness/template.py`, `harness/templates/diff-review.json`, CLI
  `delegate --template` and `templates`, MCP tool `delegate_template`. First dogfood `diff-review` found 1 real
  gap (fixed before the commit).

## Todos
**Templates**
- [ ] `hypothesis` template: independent root-cause analysis from a symptom file plus files to read, without
  the caller's theory.
- [ ] More candidates, by value: `plan-review`, "did I do what was asked" (request text vs final diff),
  `impact` (callers of a symbol), `flow-trace`, `docs-drift`, `test-gaps`.
- [ ] `--dry-run` for `delegate --template`: print the expanded request without launching. (On 2026-10-12 a CLI
  smoke test launched a real run by accident because the tree was dirty.)
- [ ] Open decisions: also allow user templates in `~/.agent-harness/templates/`? Warn when a `hypothesis` symptom
  file contains the caller's theory?
- [ ] Restart the agent-harness MCP server so `delegate_template` shows up, then try it over MCP.

**Evals**
- [ ] Effort comparison: `seeded-quota` at `low`. If still 4/4, consider `low` for small reviews.
- [ ] Clean control: review the real `harness/quota.py` with the same request; every finding is a false positive.
- [ ] Harder eval: plant bugs in `core.py`, including one that spans `core.py` and `store.py`.
- [ ] Consistency: repeat one request 2–3 times and compare.
- [ ] Watch `diff-review` cost: the dogfood run used about 253k tokens against about 53k for single-file reviews.

**Docs**
- [ ] `CLAUDE.md` gaps from the smoke test: `-` for stdin (`delegate`, `reply`), `reply --detach`,
  `report --limit/--json`, `doctor --json`, `AGENT_HARNESS_HOME`. Leave out `--provider` (hidden test flag) and
  maybe `migrate-v0` (one-off).

## Notes
- Runs launched through the Bash tool warn "the worker could not leave the caller's job object", so a run can stop
  if the session ends. Runs through the MCP tools on 2026-10-12 showed no such warning.
- Tests that delegate in a temporary folder need `provider="fake"` and `PYTHONPATH` set to the repo; see
  `tests/test_template.py`.

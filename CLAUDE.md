# Agent Harness

Tools for Claude to delegate work to other models. v0 delegates to Codex CLI (`codex_delegate.py`).
Design background: `D:\Dev\mind-palace\reports\2026-10-09-multi-model-agents-plan.md` and the vault page
"OpenAI - Codex Prompting Guide".

## Rules for delegating
- **One writer.** Codex reads, reviews, and researches. Claude makes the edits. `--write` (execute tasks only) needs
  the owner's OK for that task.
- **Claude coordinates.** Treat Codex's output as findings to check, not facts. Verify each finding against the
  cited `path:line` before acting on it, and filter findings against the owner's instructions.
- **Write the brief from a template** in `templates/`. Keep the header lines (`# Task:`, `Type:`, `Template:`).
  If you change a template's wording, bump its version (e.g. `review-v2`) so runs stay comparable.
- **Point to files; don't paste them.** Codex reads the files itself.
- **Review briefs carry no author context:** no reasoning, drafts, or explanation of why the work is right. Only the
  artifact, the source of truth, and the rubric. Use the word "review"; it triggers Codex's trained review mode.
- **Execute briefs carry the context:** decisions already made and why, and checkable "done when" criteria.
- **Don't ask Codex for a plan or progress updates.** OpenAI's guide says this can make it stop early.
- **If Codex returns `status: blocked`**, answer its question in a reply file and use `resume`; don't start over.

## Running
```
python codex_delegate.py run <brief.md> --cd <repo>
python codex_delegate.py resume <run_dir> <reply.md>
python codex_delegate.py label <run_dir> --outcome useful|partial|useless [--mast specification|coordination|verification] [--note "..."]
```
- Defaults: model `gpt-6-luna`; effort `high` for review, `medium` otherwise; read-only sandbox; 15-minute timeout;
  Codex's own sub-agents disabled.
- Read `result.json` (review/research) or `result.md` (execute) in the run folder. `meta.json` has tokens and timing.
- **Label every run** after acting on it. Labels are the eval data for comparing models, efforts, and templates.
- Write briefs in a scratch location or `briefs/` (gitignored is fine); the run folder keeps a copy.

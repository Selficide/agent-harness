# Evals

Fixed tasks with known answers, for comparing models, efforts and prompt versions. Each folder holds a `workdir/`
(all the delegate sees), a `request.json` (add a fresh `request_key` per run, or none), and an `answer-key.md` kept
**outside** `workdir/` so the delegate can't read it. Never put hints about the answers in the request.

| Eval | What it is | Runs so far |
|---|---|---|
| `seeded-quota` | Copy of `harness/quota.py` (at 08e0e1c) with 4 planted bugs, one not hinted by any docstring | 2026-10-12, gpt-6-luna, high: 4/4 found, 0 false positives, 24 s (`20261012-021439-…`) |

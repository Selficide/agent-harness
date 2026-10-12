Seeded bugs in workdir/harness/quota.py (key is outside the delegate's workdir):
1. refusal(): `>=` -> `>` ; at exactly the refuse threshold the run is not refused. (line 24)
2. announce(): key = w["name"] drops @resets_at ; thresholds announced once ever, not per window. (line 66)
3. recent_events(): reverse=True removed ; oldest first, docstring says newest first. (line 85)
4. result_quota(): before/after swapped ; used_percent_before = pct(after), used_percent_after = pct(before). (lines 46-47) -- not hinted by any docstring or criterion.

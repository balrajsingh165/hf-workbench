# SOP: Reading Metrics & Triaging Alarms

How to interpret a finding from the health/metrics system, decide whether it's
real, and pick the right response. This is the *interpretation* companion to
`docs/daily-backend-health-review.md` (the full review runbook) — read this
when a single alarm fires and you need to classify it.

**Default to read-only.** Diagnose before you touch data. Most alarms do **not**
need a backfill or a write to clear (see "Why backfill is rarely the fix").

---

## 1. Where alarms come from

`scripts/hf_health.py` is the collector. Each alarm is a row appended by
`add_finding(...)` into `findings[]`:

```bash
uv run python scripts/hf_health.py --json | jq '.findings'
```

A finding has: `severity` (`critical` | `warn`), `id` (e.g.
`agent_usage.zero_cost_tokens`), a human message, `value` (what was measured),
and `threshold` (the bar it crossed). The `id` maps directly to a code block in
`hf_health.py` — grep for it to find the **exact SQL** behind the number:

```bash
rg -n "zero_cost_tokens" scripts/hf_health.py
```

Read that query before reacting. The threshold and the window are in the SQL,
not the message.

---

## 2. Triage in three steps

1. **Reproduce the number.** Run the finding's own query against `db/hf.db`. If
   you can't reproduce it, the collector may be stale — re-run it.
2. **Drill into the offending rows.** Don't act on the count; look at the rows.
   Group by the dimension that explains it (`model_id`, `phase`, `status`,
   `created_at`). One `GROUP BY` usually tells you the whole story.
3. **Classify** before fixing (next section).

```bash
# Example drill-down: what's actually behind the count?
sqlite3 -header -column db/hf.db "
SELECT model_id, phase, COUNT(*) rows, MIN(created_at) first, MAX(created_at) last
FROM agent_usage
WHERE cost_usd=0 AND (input_tokens+output_tokens+cache_read_tokens+cache_write_tokens)>0
GROUP BY model_id, phase ORDER BY rows DESC;"
```

---

## 3. Real vs. false alarm

A finding is a *signal*, not a verdict. Before treating it as a live incident,
rule out these common false-alarm shapes:

- **Rolling-window lag.** Most metrics count over `now - 1 day` (or similar). A
  problem you already fixed keeps firing until the bad rows **age out of the
  window**. Check `MIN/MAX(created_at)` of the offending rows: if they're all
  before your fix and the newest is within the window, the alarm is *stale*, not
  live. It clears on its own once they pass the window edge.
- **Dev / test / backfill traffic.** Rows from a smoke test, a one-off script,
  or an integration session can trip production thresholds. Check the timestamps
  and `user_id`/`session_id` against what you know was running.
- **Known-cause, already-handled.** Code that logs a warning at write time
  (e.g. `usage_recorder.py` warns on an unpriced `model_id`) is telling you the
  gap is recognized, not silently corrupting data.
- **Isolated vs. systemic.** Re-run the drill-down without the narrow filter
  (e.g. drop `model_id` constraints). If only one model/phase/user is affected,
  it's a local gap; if many are, it's systemic and more urgent.

State facts before conclusions: *"6 rows, all `composer-2.5-fast`, all from
16:00–16:48 yesterday"* → *"stale, from the integration session, ages out at
16:49 today."*

---

## 4. Choosing the response

| Situation | Response |
|---|---|
| Stale rows from an already-fixed cause | **Let it age out.** Confirm no *new* bad rows are being written (restart the service if the fix is a not-yet-loaded code/config change). |
| Root cause still active | Fix the cause (code/config), then let the window clear. |
| Genuinely live + user-facing | Escalate per the alarm's severity; fix forward. |
| Need historical numbers consistent *now* | Backfill — but only as a deliberate, last resort (below). |

### Why backfill is rarely the fix

Stored aggregates (`cost_usd`, etc.) are computed **at insert time**. A pricing
or logic fix is **not retroactive**, which tempts a backfill. Resist it:

- Rolling-window alarms self-heal as old rows expire — usually within hours.
- Backfilling is a data mutation on historical records; it can mask whether the
  *forward* fix actually works.
- For dev/test or zero-marginal-cost paths, the historical inaccuracy doesn't
  matter.

Backfill only when historical accuracy has a real downstream consumer (billing,
a report you're about to publish) **and** the user asks for it. When you do,
scope it to the exact offending rows and state the row count and time range
first.

---

## 5. Worked example — `agent_usage.zero_cost_tokens`

- **Finding:** critical, value `6`, "Agent usage rows have tokens but zero cost"
  (counts `phase='aggregate'`, 24h, `cost_usd=0` with non-zero tokens).
- **Drill-down:** all 6 are `composer-2.5-fast`, all from one ~50-minute window;
  zero non-composer rows ever trip it.
- **Classify:** real gap (composer had no pricing entry → `compute_cost_usd`
  returned `0.0`), but **stale** — every row predates the pricing fix, and the
  cause is now closed in `pricing.py`.
- **Response:** let it age out of the 24h window. Restart the workbench so the
  running process loads the new pricing for *new* rows (module-level dict is
  read at startup). No backfill — relay traffic is Pro-included, so historical
  `$0` rows have no downstream billing consumer.

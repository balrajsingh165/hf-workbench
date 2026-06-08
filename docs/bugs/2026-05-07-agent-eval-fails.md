# Agent eval fails — 2026-05-07

Surfaced by the first end-to-end run of `hf-evals` against the live workbench
(run_id `20260507_032034`, 5 starter scenarios, judged with Sonnet). Original
report sat untracked in the `jw` worktree; rewritten on 2026-05-07 against the
post-news-rearchitecture state on `main` (commit `6ce7bce`).

Scoreboard from the eval run: **1 pass · 3 fail · 1 needs_human · 0 unjudged.**
All three failures were in the response agent; chart agent was never invoked.

| Scenario | Overall | Failed criterion |
|---|---|---|
| `qa_thesis_state` | fail | response.C7 (length) |
| `chip_counterpoints_multi_thesis` | fail | response.C7 (length) |
| `advice_position_sizing_oil` | fail | response.C3 (factual grounding) |
| `bull_call_industrial_automation` | pass | — |
| `deepdive_fed_pivot` | needs_human | chart never invoked |

Artifacts: `~/hf-evals/runs/20260507_032034/` and
`~/hf-evals/review/20260507_032034.md`.

---

## BUG-008 — `search_evidence` returns empty for most seed theses

### Symptom (original report)
Across all 5 scenarios, every `search_evidence` call returned exactly
`{"evidence": []}` (16 chars). Affected thesis IDs in the eval set: `thesis_001`,
`thesis_002`, `thesis_003`. Both `direction` filters and both with/without
`days_back` returned empty.

### What's actually true on main, 2026-05-07
Live calls against `db/hf.db`:
- `thesis_001` → 3 evidence items (supports, populated by today's smoke test)
- `thesis_002` → empty, note = `"thesis 'thesis_002' has 0 linked stories"`
- `thesis_003` → not in `user_theses` at all; would return
  `"thesis_id 'thesis_003' not found"` if a `theses` row exists, otherwise the
  same not-found note

So the symptom partially survives, but two things have changed since the
original report:

1. **The schema renamed.** `thesis_news` and `thesis_news_links` no longer
   exist; the link table is `thesis_story_links` and the read query in
   `app.py:get_thesis_evidence` joins `story` (commit `6ce7bce`).
2. **Empty results now carry a diagnostic.** `app.py:_diagnose_empty_evidence`
   returns one of:
   - `"thesis_id '…' not found"`
   - `"thesis '…' has 0 linked stories"`
   - `"thesis '…' has N linked stories but none with relation='…'"`
   - `"thesis '…' has N total/with relation='…' linked stories but none
     created within days_back=…; widen the window or drop the filter"`

   The response prompt at `src/agent/prompt_manager.py:37` already instructs
   the agent to acknowledge the `note` and not fabricate. So C3 regressions
   driven specifically by silent empty results should be much rarer now —
   the agent has a reason it can cite.

### Root cause (current)
The matcher hasn't been run for most active theses since the rearchitecture
landed. As of 2026-05-07 there are 13 active rows in `user_theses` but only
`thesis_001` has any rows in `thesis_story_links` (3 backfill links from the
post-rearch smoke test). The scheduler step `match_story_for_thesis` runs
per-thesis and would populate the rest, but it hasn't fired since the rename.

This is a data-state issue, not a code bug. The wiring is correct end-to-end
(today's commit verified it on `thesis_001`).

### Fix
Run the periodic scheduler once (or, equivalently, run the matcher manually
for each active thesis):

```
uv run python -m agents.pipeline_scheduler  # whichever entry point cron uses
# or, per thesis:
uv run python -m agents.match_story_for_thesis --thesis thesis_002 --window 14
```

Backfill cost: ~13 theses × up to 15 candidates × one Gemini judge call
(parallelized 4 ways inside the matcher).

### Verification
After backfill, expect `get_thesis_evidence("thesis_002")` to return at least
one item (or, if it remains empty, a note that explains why — empty result
without a note would be the real bug).

---

## BUG-009 — Chart agent never invoked

### Symptom
Across all 5 scenarios, **zero** chart-tool calls were emitted. Notable cases:
- `deepdive_fed_pivot` listed "expects a chart" in `expected_behaviors` and was
  marked `needs_human` because rubric CH3 only penalizes wrong charts, not
  missing ones.
- `qa_thesis_state` and `bull_call_industrial_automation` both quoted
  multi-day price moves in prose ("QQQ +18.2% from April 7 to May 6") with no
  visualization.

### What's actually true on main, 2026-05-07
The chart agent is correctly wired:
- `src/agent/orchestrator.py:68-78` runs `run_chart_phase` in parallel with
  `run_response_phase` whenever the request flag is set.
- `src/interfaces/ai_sdk_compat/api.py:197` forwards `enable_charts` from
  request metadata into `AgentRunRequest`.

But the flag is opt-in and **defaults to False**:
- `src/agent/chat_models.py:40` — `enable_charts: bool = False`
- `src/agent/models.py:70` — same default on `AgentRunRequest`

So if the eval harness doesn't set `metadata.enable_charts=true` on the
`POST /api/v1/ai-sdk/chat/completions` payload, no chart will ever be
attempted, regardless of whether the question begs for one.

### Root cause
hf-evals submits requests without `enable_charts=true` in metadata. Charts
are not silently disabled by `HF_AGENT_PROTOCOL_SMOKE=1`; they're disabled
by the request itself.

### Fix
Two fixes, in order of priority:
1. **Eval harness.** Set `metadata.enable_charts=true` on every scenario
   request, or expose a per-scenario opt-in for scenarios whose
   `expected_behaviors` mention charts. Outside the workbench codebase.
2. **Workbench (optional).** Decide whether the response agent should be
   able to escalate to a chart on its own. Today the only switch is the
   request-level flag. A heuristic — "if response references a multi-day
   move on a tagged ticker, request a chart" — would sit in
   `src/agent/response.py` or a post-response hook, not in the chart agent
   itself. Park unless the eval harness fix isn't enough.

### Verification
After (1), re-run `deepdive_fed_pivot`. Expect at least one
`tool-input-start: chart` event in the SSE stream. Confirm against both a
normal server and an `HF_AGENT_PROTOCOL_SMOKE=1` server — smoke mode does
not disable the chart phase, only the model backend.

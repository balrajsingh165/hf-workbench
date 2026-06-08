# Eval thesis-pass — 2026-05-16

Follow-on to `2026-05-15-eval-tone-and-400.md`. Ran ~12 thesis-touching
scenarios across `qa`, `advice`, `bull_bear`, `chip_followup`, and `deepdive`
against `main` (working tree on top of `639671e`). Read the SSE traces and
final texts directly. Three new agent-side bugs, plus one harness-side
suspicion that blocks full re-verification.

Artifacts:
- `~/hf-evals/runs/20260516_014000_batch/` — main batch (8 scenarios)
- `~/hf-evals/runs/20260516_0139{57,14,34}/` — 3× re-runs of `qa_market_movement_today`
- `~/hf-evals/runs/20260516_013950/` — `qa_btfp_status` BUG-B verification

Fixes already shipped on the working tree (rubric bumped to v0.3,
`prompt_manager.py` edits):
- **BUG-A** (unsolicited thesis tie-in) — no-staple rule added to Phase 2
  `<response_rules>` with concrete disambiguation examples
- **BUG-C** (internal `thesis_NNN` slug leaks into prose) — explicit
  prohibition added to `<what_the_ui_already_shows>`
- **BUG-F** (trailing empty `[1] [2]` footnote line) — strengthened the
  no-standalone-reference-line rule to also forbid bare-marker-only lines

This doc covers what remains.

---

## BUG-D — chart agent skips on plot-eligible FRED series

### Symptom
`runs/20260516_014000_batch/deepdive_fed_pivot/`:
- `enable_charts: true`, `mode: deep`, thesis is Fed-pivot-delay
- Research phase ran `search_macro` over **`core_cpi` YoY, `headline_cpi` YoY,
  `headline_pce` YoY, `fed_funds` level, `ust_10y` level** — exactly the
  series the scenario's `expected_behaviors` calls out: *"Chart agent decides
  based on the FRED series the research phase collected (rates, CPI, etc.) —
  non-price time-series, plot-eligible."*
- Chart agent emitted `data-chart-skip` with no chart rendered.
- The deep-dive response references rates and CPI prints throughout — these
  are the exact claims a chart would visualize.

Skip reason (visible portion — see BUG-E for why it's truncated):
> 1. **Qualitative evidence** (43 supporting stories vs. 7 stressing stories) — narrative headlines and rationales, not numeric data suitable for charting
> 2. **Price data only** (SPY, TLT, QQQ 14-day returns) — which is suppressed by policy
> 3. **Macro time series** (5 data p…

### Why this reads broken
- The agent acknowledges macro time series exist (point 3 in the visible
  reason), then concludes "skip". The reasoning behind the skip is
  incoherent on the visible portion.
- The price-data-is-suppressed clause (point 2) appears to be bleeding onto
  the macro series — those are two different categories. `search_macro` is
  the canonical chart input; "suppressed by policy" should not apply.
- The user explicitly asked for a deep-dive walkthrough on rates / CPI. A
  rendered chart of CPI YoY or 10Y level would *be* the answer to "walk me
  through where this stands."

### Root cause hypothesis
The chart agent's eligibility logic looks like it's been tuned to suppress
**price** time-series (where the chart-on-price bug from `chart_fix_*` runs
was the original target) but the suppression is now firing too broadly and
catching FRED macro series too. Either:
- The chart agent's prompt rule "do not chart price data" is being
  interpreted as "do not chart any time series the research phase pulled",
  or
- The eligibility filter sees both `price_summary` and `search_macro` rows
  in the tool history and the price rule dominates.

### Fix plan
1. Read the current chart-agent prompt + eligibility filter (likely in
   `src/agent/chart_agent.py` or `src/agent/chart/`).
2. Verify the rule distinguishes `search_macro` (always plottable) from
   `price_summary`/`price_history`/`market_overview` (suppressed).
3. Add a small fixture-style test: if `search_macro` returned ≥1 chartable
   series and `enable_charts: true`, the chart agent must either render or
   produce a reason that *names the macro series it considered and why it
   rejected each*. Generic "not suitable for charting" is not allowed when
   chartable series are in the history.

### Where the fix goes
- `hf-workbench/src/agent/chart*.py` — chart agent prompt/eligibility.
- Re-verify with `hf-evals run --scenario deepdive_fed_pivot` 3×; require
  ≥2 of 3 to render a chart, with the rendered series drawn from
  `search_macro`.

---

## BUG-E — `data-chart-skip` `reason` field truncated mid-sentence

### Symptom
Same event as BUG-D. The raw SSE payload (from `trace.jsonl`):
```json
{
  "type": "data-chart-skip",
  "data": {
    "reason": "The tool history contains:\n1. **Qualitative evidence** (43 supporting stories vs. 7 stressing stories) — narrative headlines and rationales, not numeric data suitable for charting\n2. **Price data only** (SPY, TLT, QQQ 14-day returns) — which is suppressed by policy\n3. **Macro time series** (5 data p"
  }
}
```

The `reason` literally ends at `"(5 data p"`. Mid-word truncation in the
JSON payload itself — not a display/parser issue downstream. The eval
runner is faithfully recording what the server emitted.

### Why this reads broken
- The whole point of `data-chart-skip` is debuggability: when the chart
  agent decides not to render, the reason explains why. A truncated reason
  defeats the contract.
- The truncation is happening at a fixed boundary (looks like ~300 chars).
  That points to a hard length cap upstream of the SSE emission.

### Root cause hypothesis
Three plausible places to look, in order:
1. The chart agent's own `max_tokens` for the skip-decision call may be set
   too low; the agent runs out of tokens producing the reason.
2. A `[:N]` truncation somewhere in the event-emit path before the SSE
   write.
3. A pydantic validator on the chart-skip event payload that caps
   `reason` length.

Grep for `chart_skip`, `data-chart-skip`, and `reason[` over
`src/agent/chart*` and `src/interfaces/ai_sdk_compat/`.

### Fix plan
1. Locate the truncation point.
2. Either lift the cap (preferred — the reason is internal debug copy, not
   user-visible) or bump it to ≥2000 chars.
3. If the chart agent is hitting `max_tokens`, give it more headroom — the
   skip decision needs to be diagnostic, not terse.

### Where the fix goes
- Almost certainly `hf-workbench/src/agent/chart*.py`.
- Verify by re-running `deepdive_fed_pivot` and checking that the
  `data-chart-skip` event's `reason` ends in a complete sentence with a
  trailing period.

---

## BUG-G — confidently ungrounded specific datapoint ("Feb 4.4% unemployment")

### Symptom
`runs/20260516_014000_batch/deepdive_fed_pivot/` final text:
> "the February unemployment number that touched 4.4% is worth watching as
> a secondary pressure point."

No tool output in that run contains unemployment data. `search_evidence`
returned story rows but the `4.4%` figure does not appear in any of them.
`search_macro` was not called for `unemployment_rate`. The judge correctly
flagged this under C3 (factual grounding).

The same number appears in `runs/20260516_014000_batch/chip_counterpoints_multi_thesis/`:
> "unemployment hit 4.4% in February [2] — that directly triggers the
> thesis's named invalidation condition (unemployment above 4.3%)."

In the chip scenario the number is grounded — `[2]` points to `story_133`
which contains the verbatim claim. In the deepdive it is not — the model
recycled the specific number without the supporting tool call.

### Why this reads broken
- The model is treating the thesis's named invalidation condition
  (`Unemployment rate rises above 4.3% before August`) as a fired event
  rather than as a *condition to watch*. It then back-fills a specific
  number and month to justify the framing.
- Cross-scenario "specific number lifts itself out of one trace and into
  another" is the high-risk failure mode: it looks confidently grounded
  but isn't.

### Root cause hypothesis
The thesis context block renders invalidation conditions as bullets
alongside other thesis state. The phrasing — `"Unemployment rate rises
above 4.3% before August"` — reads as a declarative fact in the prompt,
not as a hypothetical trigger. The model interprets it like an evidence
row.

### Fix plan
1. In `prompt_manager.py` `_format_thesis_block` (and/or the upstream
   thesis context generator), label invalidation conditions with an
   explicit framing tag — e.g. *"Invalidation watch list — these are
   future trigger conditions, not events that have occurred. To claim any
   of these has fired, ground the claim in a `search_macro` /
   `search_evidence` / `web_search` tool result from this turn."*
2. Add a fail marker to `rubrics/response_agent.md` C3 calling this out
   specifically: *"States a thesis's invalidation condition as a fired
   event without a tool-output citation from this turn — hard fail. The
   invalidation list is forward-looking; quoting a specific number/date
   from it as if it has happened is fabrication."*
3. Verify by re-running `deepdive_fed_pivot` 5× and `chip_counterpoints_multi_thesis` 3× — no run should mention a specific
   unemployment number unless `search_macro` or `search_evidence` returned
   one in that turn.

### Where the fix goes
- `hf-workbench/src/agent/prompt_manager.py` — `_format_thesis_block`,
  invalidation-condition rendering.
- `hf-evals/rubrics/response_agent.md` — new fail marker under C3, bump
  version to 0.4.

---

## Harness-side: suspected runner payload mismatch (blocking re-verification)

### Symptom
After the BUG-A/C/F prompt edits landed in the working tree, the verify
re-runs of `chip_counterpoints_multi_thesis` and `qa_thesis_state`
(scenarios that had passed cleanly an hour earlier in
`20260516_014000_batch`) started returning:

- `chip_counterpoints_multi_thesis`: *"I don't have a selected thesis or any
  evidence to work against. If you open a specific thesis and ask again, I
  can stress-test it properly."*
- `qa_thesis_state`: *"Not sure what you're referring to. Can you tell me
  which thesis, stock, or topic you want an update on?"*

In both cases:
- Phase 1 ran (system prompt cached, ~7.5k tokens) but emitted **zero tool
  calls**
- Phase 2 produced the refusal
- The same payload reproduces with a fresh `session_id` via direct curl,
  ruling out session caching

`qa_market_movement_today` does NOT exhibit this — it runs tools and
responds correctly. The difference is the user-message shape: that
scenario asks a self-contained question ("why did the market move
today?"), while the failing ones use bare ambient pronouns ("Where does
this stand right now?", "Find the strongest counterpoints.") that depend
on thesis context being in the system prompt.

### Root cause hypothesis (high confidence, not verified)
The harness runner (`~/hf-evals/src/hf_evals/runner.py`) sends the request
as:
```json
{
  "metadata": {
    "thesis_ids": [...],
    "chip_id": null,
    "mode": "quick",
    "surface": "commanddock",
    "enable_charts": false,
    "theme": "dark"
  }
}
```

But the workbench's `ChatCompletionRequest` schema
(`src/agent/chat_models.py:59`) expects:
```python
params: ChatParams   # mode, enable_charts, theme
subject: ChatSubject # thesis_ids, references, active_thesis_id, active_story_id
```

There is no `metadata` field. The runner's payload silently fails to
populate `subject.thesis_ids`, so `_resolve_thesis_ids()`
(`src/interfaces/ai_sdk_compat/api.py:176`) returns `[]`, no theses get
hydrated, and the system prompt arrives at the model with **no thesis
block**. The model then correctly says "no thesis selected".

Why this didn't manifest in the morning's `20260516_014000_batch`: unclear.
Either the schema migration to `params`/`subject` landed *between* the
batch and the verify runs (the working tree has a ~140-line uncommitted
refactor of `prompt_manager.py`, suggesting active in-progress work), or
the schema accepted `metadata` via a `model_config = ConfigDict(extra=
"allow")` fallback that was tightened later in the same session.

### Fix plan (lives in hf-evals, not hf-workbench)
1. Update `~/hf-evals/src/hf_evals/runner.py` to send `params` and
   `subject` instead of `metadata`. Map:
   - `metadata.thesis_ids` → `subject.thesis_ids`
   - `metadata.mode`, `metadata.enable_charts`, `metadata.theme` → `params.*`
   - drop `metadata.chip_id` (chips are frontend-only now per the new
     `hf-evals/CLAUDE.md`); the chip preset prose should already be in the
     scenario body
   - drop `metadata.surface` if the new schema doesn't accept it
2. Also update the runner-side `Scenario` model in
   `~/hf-evals/src/hf_evals/io_utils.py` if it gates the payload shape.
3. Re-run the affected scenarios; they should call tools again.

### Scenarios that need re-running once the harness fix lands

Re-verification of the agent-side fixes (BUG-A / BUG-C / BUG-F) is
partial — only the scenarios that survived the harness regression actually
exercised the new prompt rules. Once the runner payload is corrected,
re-run:

| scenario | why re-run |
|---|---|
| `qa_market_movement_today` (×5) | BUG-A is non-deterministic. Confirm the unsolicited-thesis-tie-in pattern is gone across multiple draws. |
| `chip_counterpoints_multi_thesis` (×3) | Currently blocked. Once unblocked, primary check is BUG-C (slug as section headers) and BUG-F (empty `[1] [2]` footnote line). Both were the original failures. |
| `qa_thesis_state` (×3) | Currently blocked. Once unblocked, confirm the agent still treats ambient "this" as the thesis and still produces a tight 1–3 sentence state-check. |
| `qa_thesis_still_alive`, `qa_thesis_sanity`, `qa_thesis_stress_test` | Re-baseline post-prompt-edit — these passed before but the prompt change might shift behavior. |
| `deepdive_fed_pivot` (×3) | Primary check is whether BUG-D fix lands a chart and BUG-G fix kills the "Feb 4.4% unemployment" hallucination. |
| `advice_position_sizing_oil`, `bull_call_industrial_automation` | Spot-check that strong-stance categories still take clear positions after the new no-staple rule. |

Pass criteria after re-runs:
- 0 occurrences of `thesis_001`, `thesis_002`, `Thesis 003`, etc. in any
  final text (BUG-C)
- 0 trailing bare-marker lines (BUG-F)
- 0 stapled-on closing thesis verdicts on `qa_market_movement_today`
  across 5 draws (BUG-A)
- `deepdive_fed_pivot` renders a chart or names the macro series it
  rejected, with a non-truncated `reason` (BUG-D, BUG-E)
- `deepdive_fed_pivot` does NOT cite "February 4.4%" unemployment unless
  a tool call this turn returned it (BUG-G)

---

## Sequencing

1. **Harness fix first** (hf-evals `runner.py` payload migration). Until
   that lands, none of the agent-side fixes can be verified on the
   blocked scenarios. ~10-line change.
2. BUG-D + BUG-E (chart agent) — paired investigation in
   `hf-workbench/src/agent/chart*.py`. Same area, same restart.
3. BUG-G (invalidation-condition framing) — small prompt edit +
   rubric marker.
4. Re-run the matrix above. Close this doc with a
   "closed YYYY-MM-DD — verified by run_id X" footer.

# Quality Gates

**Status:** Living spec — gates are added/sharpened as the system grows.
**Last updated:** 2026-05-13
**Audience:** Engineers shipping prompt/model/scoring changes; reviewers running periodic quality sweeps; AI agents (Claude Code etc.) auditing the system end-to-end.
**Companion docs:**
- [`docs/daily-backend-health-review.md`](daily-backend-health-review.md) — daily liveness + freshness sweep across the same data.
- [`docs/masterplan-production.md`](masterplan-production.md) — Phase 3 links here.

---

## Why this doc exists

Aggregate stats lie. A `precision = 0.84` on a matching benchmark can hide:

- A direction-flipped link (`stresses` ↔ `supports`) that the labeler counted as correct because the rationale "sounded right".
- A rationale that paraphrases the headline and could be applied to *any* thesis in the same sector.
- A confidence number that drifted upward 0.05 across the board after a prompt change — invisible in precision, fatal for the auto stress-flip threshold (`STRESS_FLIP_CONF`).
- A digest that scores well on length/coverage metrics but reads as generic market commentary.

**Every gate in this doc requires a qualitative read of raw data, not just a number.** The "Pass criterion" column is necessary but never sufficient. Each gate names the specific raw artifacts a reviewer must read and the rubric they must apply. If you're tempted to ship a green metric without doing the read, you are about to regress something the metric can't see.

---

## Operating rules

- Use `uv run python` to execute scripts.
- Treat gate runs as **read-only** unless the gate explicitly writes to a labels table.
- Record findings in `docs/reviews/<gate>-YYYY-MM-DD.md` (one file per run). Include: pass/fail per the criterion, the qualitative-read sample (with row ids), pattern-level observations, and a recommendation (ship / hold / re-prompt).
- Always include the **input window** (date range, row count, model version, prompt sha or commit) so the run is reproducible.
- If aggregate stats and qualitative reads disagree, **trust the qualitative read** and call out the discrepancy in the review.
- New gates land here first; the masterplan only links.

---

## Today's data baseline (2026-05-13)

Numbers reviewers should expect when they run the gates against the current DB. If your run is wildly different, something changed:

| Table | Rows | Window | Notes |
|---|---|---|---|
| `thesis_story_links` | ~213 | 2026-05-08 → today | 152 supports / 61 stresses; ingest avg conf 0.86, backfill stresses avg 0.79. |
| `story` | ~208 | 2026-05-05 → today | All carry `theme_tag`, `sectors_json`, `what_changed`. |
| `theses` | ~39 | 2026-04-24 → today | 13 are user-tracked (`user_theses`). |
| `thesis_snapshots` | ~208 | daily, 2026-04-25 → today | One row per (thesis_id, snapshot_date). |
| `story_quality_label` | ~208 | 2026-05-06 → today | 171 good / 30 unclear / 7 no_value. |
| `daily_briefs` | ~16 | 2026-04-25 → today | One per day; `themes_json`, `source_story_ids`. |
| `agent_usage` | ~207 | 2026-05-05 → today | Per-phase token + cost; substring-matched against `pricing.py`. |
| `pending_instruments` | ~1398 | rolling | Unmapped tickers from sharp + firehose lanes. |
| `entity_tickers` | ~6369 | rolling | Cross-checked against `instruments` for gate 3.3. |

Volume is real: 100+ matches in a 5-day window, 200+ stories, daily briefs accumulating. Qualitative reads are now feasible without waiting for more data.

---

## Gate index

| # | Gate | Trigger | Status |
|---|---|---|---|
| 3.1 | [Matching regression](#31--matching-regression) | Before any judge prompt/model change | ⬜ |
| 3.2 | [Synthesis faithfulness](#32--synthesis-faithfulness) | Monthly; after any synthesis prompt change | ⬜ |
| 3.3 | [Ticker extraction precision](#33--ticker-extraction-precision) | Automated post-ingest | ⬜ |
| 3.4 | [Confidence calibration](#34--confidence-calibration) | One-time + after judge prompt changes | ⬜ |
| 3.5 | [Discovery rejection rate](#35--discovery-rejection-rate) | Before any discovery prompt change | ⬜ |
| 3.6 | [Score reasonableness spot-check](#36--score-reasonableness-spot-check) | Weekly during dev | ⬜ |
| 3.7 | [Digest cold-read test](#37--digest-cold-read-test) | Gate for shipping the digest (masterplan 0.2) | ⬜ |
| 3.8 | [Stress-flip precision gate](#38--stress-flip-precision-gate) | One-time, prerequisite for masterplan 2.7 | ⬜ |
| 3.9 | [End-to-end pipeline smoke](#39--end-to-end-pipeline-smoke) | Every pipeline run | ⬜ |
| 3.10 | [Freshness decay calibration](#310--freshness-decay-calibration) | One-time, after 30+ days of data | ⬜ |
| 3.11 | [LLM cost-per-run tracking](#311--llm-cost-per-run-tracking) | Weekly | ⬜ |

---

## 3.1 — Matching regression

**Goal.** Detect when a judge prompt change or model upgrade silently regresses thesis↔story matching.

**Inputs.**
- Labeled fixture: [`docs/ref/matching-eval-set.md`](ref/matching-eval-set.md) (60 thesis↔story pairs, hand-labeled `supports` / `stresses` / `unrelated`).
- Judge under test: `src/thesis/story_judge.py` (current commit / prompt sha).

**How to run.**
```bash
uv run python scripts/eval_story_gold.py --out docs/reviews/matching-3.1-$(date -u +%F).md
```
*(Wire the script to read the eval set, call the judge, write a per-pair table + aggregate metrics.)*

**Pass criterion (necessary).**
- Precision ≥ 0.80 on `supports`, ≥ 0.75 on `stresses`.
- Recall ≥ 0.70 on both relations.
- No regression > 0.05 vs the previous run on any of the four numbers.

**Qualitative read (required, never skip).** Read every misclassified pair end-to-end:
1. Pull the rationale the judge produced.
2. Read the story body (`global/stories/{id}.md`) AND the thesis (`global/theses/{id}.md`, "Core Thesis" + "Invalidation Conditions").
3. Decide for each: (a) judge wrong, (b) label wrong, (c) genuinely ambiguous.
4. **Check direction flips specifically** — a `supports` mislabeled `stresses` is twice as bad as a missed match because it actively misleads the user.

**What to record.**
- Confusion matrix (predicted × actual).
- Direction-flip count separately from miss count.
- For each flip: thesis_id ← story_id + one-line root cause.
- Pattern observations ("judge over-attaches macro stories to single-ticker theses", etc.).

**Hold the ship if.** Any direction flip is unforced (i.e., not "label is wrong"); precision drop > 0.05; recall drop > 0.05.

---

## 3.2 — Synthesis faithfulness

**Goal.** Every bullet in a synthesized story must be grounded in at least one source-article sentence. Hallucinated facts kill trust.

**Inputs.**
- Random sample of 20 recent synthesized stories (`global/stories/story_*.md`) with non-empty source URLs.
- The original article bodies (re-fetched if needed; we don't store full bodies post-synthesis).

**How to run.**
- Sample 20 stories from the last 30 days.
- For each bullet in `## What Changed` and `## Why It Matters`, verify it maps to at least one sentence in the source article(s).

**Pass criterion (necessary).** ≥ 90% of bullets verifiably grounded.

**Qualitative read (required).** A bullet that is *technically* in a source but exaggerates the source's claim ("hints at" → "confirms") counts as ungrounded. Reviewer must read both the bullet and the cited sentence and decide whether the bullet would mislead a trader who hadn't read the source.

**What to record.**
- Per-story: bullets total, bullets grounded, bullets exaggerated, bullets fabricated.
- Worst offender (bullet + source sentence verbatim) for the synthesis prompt owner to see.

**Hold the ship if.** Any fabricated bullet (no source sentence at all). Exaggeration rate > 5%.

---

## 3.3 — Ticker extraction precision

**Goal.** Symbols that land in `entity_tickers` should resolve against `instruments`. Unmapped tickers are dead weight: they can't be priced, can't be matched, and pollute display.

**How to run.**
```bash
sqlite3 -header -column db/hf.db "
WITH unmapped AS (
  SELECT et.entity_type, et.symbol, COUNT(*) AS occurrences
  FROM entity_tickers et
  LEFT JOIN instruments i ON i.symbol = et.symbol
  WHERE i.symbol IS NULL
  GROUP BY et.entity_type, et.symbol
)
SELECT entity_type, COUNT(DISTINCT symbol) AS unmapped_symbols,
       SUM(occurrences) AS unmapped_rows
FROM unmapped GROUP BY entity_type;

SELECT COUNT(*) AS total_rows FROM entity_tickers;
"
```

**Pass criterion (necessary).** < 5% of `entity_tickers` rows are unmapped.

**Qualitative read (required).** Pull the top 30 unmapped symbols by occurrence:
- Are they real tickers that just aren't in `instruments` yet? → Triage into `pending_instruments` (already done by the firehose path; verify the sharp lane drops + logs them too — masterplan 2.11).
- Are they LLM hallucinations / formatting junk (e.g., `"NVDA."`, `"$$$"`, ticker fragments from foreign exchanges)? → Means a synthesis or NER prompt is leaking; file an issue.
- Are they correct tickers from exchanges we don't index? → Decide whether to extend the registry or drop the source.

**What to record.**
- The 30-row top-N unmapped table.
- Ratio of "missing from registry" vs "junk emission" — these need different fixes.
- Trend: percentage of unmapped rows over the last 7 days (is it growing?).

**Hold the ship if.** Junk-emission rate > 2% of total rows after a prompt change. (Missing-from-registry is a backlog item, not a hold.)

---

## 3.4 — Confidence calibration

**Goal.** Confirm that `thesis_story_links.confidence` is calibrated — i.e., 80% of links at conf ≥ 0.80 are actually correct, etc. This is a **prerequisite for masterplan 2.7** (auto stress-flip), because flipping a thesis to `Stressed` based on `confidence ≥ STRESS_FLIP_CONF` is only safe if that confidence threshold is calibrated.

**Inputs.** ≥ 100 rows from `thesis_story_links` (we now have 213 — gate is unblocked).

**How to run.**
1. Pull a stratified sample: 25 rows from each of `[0.50–0.69]`, `[0.70–0.79]`, `[0.80–0.89]`, `[0.90–1.00]`. Mix `supports` + `stresses` proportionally.
2. For each row, hand-label as **correct / borderline / wrong** using the 3.1 rubric (read story + thesis + rationale).
3. Compute precision per bin.

**Pass criterion (necessary).**
- Precision in `[0.90–1.00]` bin: ≥ 0.90.
- Precision in `[0.80–0.89]` bin: ≥ 0.80.
- Precision in `[0.70–0.79]` bin: ≥ 0.70.
- The current `SUPPORT_STRONG_CONF=0.70` constant is defensible by the data.

**Qualitative read (required).**
- Look at every link in the top bin (`[0.90–1.00]`) that you labeled wrong. These are the most dangerous false-positives — they would auto-flip status. Document each.
- Look at every link with `matched_invalidation` populated. These should be **literal** matches to a named invalidation condition, not paraphrased. A `matched_invalidation` link at conf ≥ 0.95 that's actually a paraphrase is the worst possible failure mode for 2.7.
- Compare ingest vs backfill rows separately. (Today: ingest avg 0.86, backfill stresses 0.79 — backfill is the leakier path and deserves more sample weight.)

**What to record.**
- Per-bin precision table.
- List of wrong rows in the top bin (thesis_id ← story_id + one-line reason).
- Recommendation: keep `STRESS_FLIP_CONF` where it is, raise it, or lower it — with evidence.

**Hold the ship (re: 2.7) if.** Top-bin precision < 0.90, or any wrong `matched_invalidation` row in the sample.

---

## 3.5 — Discovery rejection rate

**Goal.** `discover_thesis` should aggressively reject non-finance and non-thesis-worthy stories. Without a strong rejection rate, the user's thesis list fills with noise.

**Inputs.** 30 articles: 20 deliberately non-finance (sports, weather, regional news, soft tech features) + 10 finance articles a reviewer pre-judges as "should produce a sharp thesis".

**How to run.**
```bash
uv run python scripts/eval_discover_thesis.py --fixture docs/ref/discover-fixture.md --out docs/reviews/discovery-3.5-$(date -u +%F).md
```
*(Build `docs/ref/discover-fixture.md` if it doesn't exist; pin the 30 article ids.)*

**Pass criterion (necessary).**
- ≥ 85% of non-finance articles produce `has_thesis=false`.
- ≥ 7 of 10 finance articles produce a thesis the reviewer calls "sharp" (specific enough to be testable, broad enough to outlive the headline — see [`docs/north-star-magical-moments.md`](north-star-magical-moments.md) §AGENTS.md "what makes a thesis").

**Qualitative read (required).** For every produced thesis (admitted or suppressed):
- Read the thesis statement, tickers, invalidations.
- Compare against the source story.
- Verdict: sharp / vague / wrong-direction / duplicate-of-existing.
- Cross-reference against the 6 layered gates inside `discover_thesis` — which gate should have caught a vague/wrong admission, and didn't?

**What to record.**
- Per-article: admitted/suppressed, gate that fired (if suppressed), reviewer verdict.
- Pattern observations: "LLM keeps emitting horizon='3-6 months' regardless of story timescale", etc.
- Currently never-fired gates: post-creation re-check, registry grounding, structural promotion. Note in the review whether they're still dormant; if they fire, capture the failure mode (per existing `TODO.md` watch).

**Hold the ship if.** Non-finance rejection < 80%; any wrong-direction thesis in the admitted set.

---

## 3.6 — Score reasonableness spot-check

**Goal.** After every scoring run, sanity-check that 3 random thesis scores match what a reviewer would say given the recent news + price action.

**How to run.**
```bash
sqlite3 -header -column db/hf.db "
SELECT ut.user_id, ut.thesis_id, ut.score, ut.score_freshness, ut.score_tailwind
FROM user_theses ut
WHERE ut.status='active'
ORDER BY RANDOM() LIMIT 3;
"
```

For each thesis pulled:
1. Read `global/theses/{id}.md` (Core Thesis + Invalidation Conditions).
2. Pull recent links: `SELECT relation, confidence, rationale, updated_at FROM thesis_story_links WHERE thesis_id='thesis_NNN' ORDER BY updated_at DESC LIMIT 10;`
3. Pull recent price action on tickers: e.g. `uv run python -c "from src.clients.prices import window_returns; print(window_returns(['NVDA','TSM'], period='5d'))"`.
4. Decide: does the composite score (and the freshness/tailwind split) match what a human would say?

**Pass criterion (necessary).** Reviewer agrees direction is correct on all 3.

**Qualitative read.** This entire gate **is** a qualitative read — the score is the metric, the read is the gate. Specifically check:
- Theses with `score_tailwind = NULL`: is it because every ticker failed price lookup, or did the null-handling contract (masterplan 2.10) silently regress?
- Theses with very high freshness but low tailwind: is the news really supportive while prices disagree, or is matching over-attaching?
- Theses with low scores: are the links justifying the low score still attached, or did they age out?

**What to record.** Per-thesis: score, what reviewer expected, why they match/don't match.

**Hold the ship if.** Any of the 3 scores is directionally wrong (e.g., score 80 on a thesis with no fresh signal and adverse price action).

---

## 3.7 — Digest cold-read test

**Goal.** A reviewer with no prior context should, in 60 seconds, articulate (a) what the user believes, (b) what happened to those beliefs today, (c) one action to take.

**Inputs.** Today's `users/{id}/digests/YYYY-MM-DD.md` (once masterplan 0.2 ships).

**How to run.** Hand 3 reviewers the digest cold. Time them. Ask them the three questions afterwards.

**Pass criterion (necessary).** 3/3 reviewers pass within 60 seconds.

**Qualitative read (required).** Beyond pass/fail:
- Did the reviewers identify the *user's* convictions, or did they read the digest as generic market commentary?
- Did they pick the action the digest intended, or a different one?
- Did any reviewer hedge ("I think the user maybe believes...") — that's a tone failure, score it.

**What to record.** Per-reviewer: time, three answers verbatim, hedging count, score (pass/fail/borderline).

**Hold the ship if.** < 3/3 pass; any reviewer reads the digest as generic market commentary.

---

## 3.8 — Stress-flip precision gate

**Goal.** Before enabling auto stress-flip (masterplan 2.7): confirm that `stresses` links above `STRESS_FLIP_CONF` are actually thesis-invalidating ≥ 80% of the time.

**Inputs.** Last 100 `stresses` rows where `confidence ≥ STRESS_FLIP_CONF`.

**How to run.**
```bash
sqlite3 -header -column db/hf.db "
SELECT thesis_id, story_id, confidence, matched_invalidation, rationale, updated_at
FROM thesis_story_links
WHERE relation='stresses' AND confidence >= 0.85
ORDER BY updated_at DESC LIMIT 100;
"
```
*(Substitute the production `STRESS_FLIP_CONF` value. Today there are ~52 ingest stresses + 9 backfill stresses — gate is data-thin; collect more before running, or re-scope to "all stresses ≥ 0.80".)*

For each row, hand-label: would I, as a thoughtful trader, agree this single signal justifies flipping the thesis from Active to Stressed?

**Pass criterion (necessary).** ≥ 80% human agreement.

**Qualitative read (required).** This is the highest-stakes qualitative review in the system because it gates an automatic state change. Specifically:
- Every disagreement: read story + thesis + rationale verbatim. Why did the judge call this stress-worthy? What did it miss?
- Every `matched_invalidation` row: did the story actually name or numerically cross the invalidation, or was it a thematic overlap?
- Cluster failures by thesis: is one thesis collecting bad stresses (suggests the thesis statement is too broad) vs the judge being miscalibrated globally?

**What to record.**
- Per-row label.
- All disagreements with one-line root cause.
- Recommendation: ship 2.7 / raise threshold / hold and re-prompt.

**Hold 2.7 if.** Agreement < 80%; any wrong `matched_invalidation` row.

---

## 3.9 — End-to-end pipeline smoke

**Goal.** Every pipeline run produces a deterministic green/red signal: ingest happened, links updated, scores computed, brief generated, no uncaught exceptions.

**How to run.** Wire the smoke into [`agents/pipeline_scheduler.py`](../agents/pipeline_scheduler.py) so it runs after every full pipeline. Local smoke:
```bash
uv run python scripts/smoke_pipeline_e2e.py --user user_1
```
*(Build the script if it doesn't exist; assert non-empty `news`, `story`, `thesis_story_links`, `thesis_snapshots`, `daily_briefs` for today.)*

**Pass criterion (necessary).** All stages complete; outputs non-empty; no `ERROR` rows in `logs/hf-pipeline-metrics.jsonl` for the run window.

**Qualitative read (recommended).** Even when smoke passes, sample one new story + one new link + the brief once a day and apply the 3.1/3.2/3.7 rubrics. The smoke proves the plumbing is alive; it doesn't prove the output is good. The [`docs/daily-backend-health-review.md`](daily-backend-health-review.md) runbook is the canonical daily read.

**What to record.** Smoke is automated — write to `logs/hf-pipeline-metrics.jsonl`; alerting (masterplan 5.8) consumes from there.

**Hold the ship if.** Smoke fails 2+ consecutive runs (also triggers production alerting per `docs/plan-production-alerting.md`).

---

## 3.10 — Freshness decay calibration

**Goal.** Verify the freshness half-life formula in `src/thesis/scoring.py` matches the spec in [`docs/plan-scoring-system.md`](plan-scoring-system.md).

**Inputs.** ≥ 30 days of `thesis_snapshots` (we have ~18 days as of 2026-05-13 — gate unblocks ~2026-05-25).

**How to run.**
```bash
sqlite3 db/hf.db "
SELECT t.id AS thesis_id,
       julianday('now') - julianday(MAX(l.updated_at)) AS days_since_signal,
       s.score_freshness
FROM theses t
LEFT JOIN thesis_story_links l ON l.thesis_id = t.id
LEFT JOIN thesis_snapshots s
  ON s.thesis_id = t.id AND s.snapshot_date = date('now')
WHERE t.review_status='active'
GROUP BY t.id;
" | (plot freshness vs days_since_signal)
```

**Pass criterion (necessary).** Theses ≥ 14 days without signal score ≤ 30 on a 4-week horizon. Decay curve matches the spec'd half-life.

**Qualitative read (required).** Outliers are the story:
- Any thesis with `score_freshness > 50` and `days_since_signal > 14` — the formula isn't decaying the right input. Is the thesis's stored horizon wrong, is the spec wrong, or is the implementation wrong?
- Any thesis with `score_freshness ≈ 0` and recent links — match exists but isn't being read by the freshness path.

**What to record.** Plot + outlier rows + recommendation.

---

## 3.11 — LLM cost-per-run tracking

**Goal.** Catch cost regressions from prompt bloat, stuck retry loops, or accidental Opus routing.

**Inputs.** `agent_usage` table (shipped per masterplan 5.16) and `llm_calls` for the news/Gemini path (masterplan 4.6).

**How to run.**
```bash
uv run python scripts/hf_metrics.py today
uv run python scripts/hf_metrics.py top-spenders --days 7 --json
```

**Pass criterion (necessary).** ≤ $0.05/story (synthesis path), ≤ $5/day at current volume (chat + pipeline combined). No single user > 50% of weekly spend without a known reason.

**Qualitative read (required).**
- Read the top 5 most-expensive `agent_usage` rows of the week. Are they legitimate (long research turn, justified) or pathological (model retried 10 times, response phase ballooned, chart phase ran on a non-price ticker)?
- Cross-check `cost_usd = 0` rows where `input_tokens > 0` — substring match in `pricing.py` may be missing a model id (silent drift to $0 looks like a feature win in dashboards).
- Watch for endpoint-level regressions: if `digest` cost/run doubles week-over-week, the digest prompt is bloating.

**What to record.** Weekly cost summary table + the top-5 expensive-runs read + any unknown model ids.

**Hold the ship if.** Cost/story > $0.10; any unknown model id in the week's runs (means cost is being silently rounded to $0).

---

## How to add a new gate

1. Open a section here using the same template (Goal / Inputs / How to run / Pass criterion / **Qualitative read (required)** / What to record / Hold-the-ship rule).
2. The qualitative-read section is non-negotiable. If you can't articulate what a human must read, the gate isn't useful — the metric will be gamed by the next prompt change.
3. Add to the gate index table at the top.
4. Cross-link from `docs/masterplan-production.md` Phase 3 only if the gate is structural to production readiness; otherwise leave it here.
5. Pin the inputs in `docs/ref/` if they're hand-curated (eval sets, fixtures). Do not let inputs drift between runs — that defeats the regression purpose.

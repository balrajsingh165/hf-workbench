# Daily Backend Health Review

**Status:** Agent-ready runbook  
**Cadence:** Daily, ideally after the morning full pipeline run  
**Audience:** Claude Code, Codex, or another coding agent with repo and shell access  
**Related:** `docs/plan-production-alerting.md`, `docs/news-rearchitecture-runbook.md`, `docs/news-story-pipeline.md`

This review answers one question:

> Is the backend producing fresh, reliable, user-trustworthy finance output today?

Do not only check whether processes are alive. Review liveness, data freshness,
pipeline failures, provider failures, DB/markdown integrity, and the quality of
generated text.

## Operating Rules

- Use `uv run python` for Python scripts.
- Treat the review as read-only unless the user explicitly asks for fixes.
- Do not reset, delete, rebuild, or overwrite data during review.
- Do not hide warnings because one subsystem is healthy. Report partial health.
- Use exact UTC timestamps from logs/DB when describing recency.
- Separate facts from interpretation. Say "the log shows..." before concluding.
- If generated text is stale, vague, hedged, unsupported, or contradictory, call it out even when the pipeline returned `ok=True`.
- Keep the final report short enough for an operator to act on.

## Daily Review Checklist

### 1. Process & API Liveness

```bash
date -u +%Y-%m-%dT%H:%M:%SZ
git status --short
git log --oneline -n 8
pm2 list
pm2 describe hf-workbench
pm2 logs hf-workbench --lines 160 --nostream
ps -ef | rg 'pipeline_scheduler|agents\.pipeline_scheduler|uvicorn|hf-workbench'
curl -sS -o /tmp/hf_home.json -w "%{http_code} %{time_total}\n" "http://127.0.0.1:8088/api/home?user_id=user_1"
curl -sS -o /tmp/hf_quotes.json -w "%{http_code} %{time_total}\n" "http://127.0.0.1:8088/api/v1/prices/quotes?tickers=%5EGSPC,%5EDJI,%5EIXIC,%5EVIX&include=sparkline"
jq '{brief_date: .brief.date, thesis_count: (.theses | length), news_count: (.news | length)}' /tmp/hf_home.json
jq . /tmp/hf_quotes.json
```

Flag:

- stopped process, restart loop, or missing scheduler
- recent commits touching pipeline/prices/prompts/schemas/agents that
  plausibly explain a regression
- HTTP 5xx, repeated 4xx, or empty `/api/home` sections
- frontend quote API returning only `upstream_failure`

Note: frontend pricing (EODHD) is distinct from scheduled scoring/brief
pricing (Alpaca + Mesh). If one path breaks while the other works, say which.

### 2. Run The Health Collector

Run:

```bash
uv run python scripts/hf_health.py --json
uv run python scripts/hf_health.py --pipeline-funnel
```

Read the full JSON. `--pipeline-funnel` prints the latest `firehose_run` and
`route_funnel` events (evaluated → promote → synth reject rate, story quality
snapshot) without loading the full health payload. The collector is the first-pass signal, not the whole
review — continue even if it says `ok`. Pay particular attention to
`findings[]`, consecutive-failure counts, and any non-`ok` substatus.

### 3. Inspect Scheduler And Pipeline Logs

Run:

```bash
tail -n 180 logs/hf-scheduler.log
tail -n 120 logs/hf-pipeline-metrics.jsonl
rg -n "Traceback|ERROR|RuntimeError|failed|returncode=1|not set|empty|timeout|rate|quota|429|403|500" logs/hf-scheduler.log logs/hf-pipeline-metrics.jsonl
```

Review:

- whether firehose still runs every 10 minutes
- whether the full pipeline still runs on schedule
- failed steps: `route_news_clusters`, `judge_stories`, `match_story_for_thesis`, `score_theses`, `daily_brief`
- provider/auth failures: Gemini, Bedrock, Alpaca, EODHD, Mesh, Exa, Firecrawl
- parse/schema failures from LLM outputs
- repeated non-fatal warnings that correlate with stale data

For each failure, identify the first failing stack trace or metric event, not
just the final `ok=False`.

### 4. Query Core Data Freshness

Run:

```bash
sqlite3 -header -column db/hf.db "
SELECT 'news' AS table_name, COUNT(*) AS total, MAX(created_at) AS newest FROM news
UNION ALL SELECT 'story', COUNT(*), MAX(created_at) FROM story
UNION ALL SELECT 'theses', COUNT(*), MAX(created_at) FROM theses
UNION ALL SELECT 'thesis_story_links', COUNT(*), MAX(updated_at) FROM thesis_story_links
UNION ALL SELECT 'daily_briefs', COUNT(*), MAX(generated_at) FROM daily_briefs;
"

sqlite3 -header -column db/hf.db "
SELECT date(created_at) AS day, COUNT(*) AS news
FROM news GROUP BY date(created_at) ORDER BY day DESC LIMIT 10;
"

sqlite3 -header -column db/hf.db "
SELECT date(created_at) AS day, COUNT(*) AS stories
FROM story GROUP BY date(created_at) ORDER BY day DESC LIMIT 10;
"
```

Red flags:

- firehose inserts news but no stories for 24h
- stories are fresh but links are stale
- links are fresh but score snapshots are stale
- full pipeline says success but DB timestamps did not move
- daily story volume collapses or spikes vs the 7-day baseline

### 5. Check DB And Markdown Integrity

Run:

```bash
find global/stories -maxdepth 1 -name 'story_*.md' | wc -l
find global/theses -maxdepth 1 -name 'thesis_*.md' | wc -l

sqlite3 -header -column db/hf.db "
SELECT COUNT(*) AS story_rows FROM story;
SELECT COUNT(*) AS thesis_rows FROM theses;
"
```

Then spot-check that the newest DB rows have markdown files:

```bash
sqlite3 -noheader db/hf.db "SELECT id FROM story ORDER BY created_at DESC LIMIT 8;"
sqlite3 -noheader db/hf.db "SELECT id FROM theses ORDER BY created_at DESC LIMIT 8;"
```

For each returned id, verify `global/stories/{id}.md` or
`global/theses/{id}.md` exists.

Red flags:

- DB row exists without markdown source-of-truth
- markdown file exists but DB row is missing
- counts collapse unexpectedly
- newest markdown timestamp is much older than newest DB row

### 6. Review Recent Stories For Quality

Run:

```bash
sqlite3 -header -column db/hf.db "
SELECT id, substr(created_at,1,16) AS created, substr(headline,1,100) AS headline,
       theme_tag, json_array_length(sectors_json) AS sectors
FROM story
ORDER BY created_at DESC
LIMIT 12;
"
```

Read the newest 3 to 5 `global/stories/story_NNN.md` files.

Quality bar:

- headline is one declarative market-relevant sentence
- overview is specific, sourced, and not generic
- no hedging language such as "may", "could", "might" unless quoting a source
- tickers and sectors are plausible
- story is durable enough to affect a thesis or market view
- quotes/claims cite real source ids
- no hallucinated dates, fake macro releases, or internally inconsistent numbers
- no single-source PR spam promoted as global market context unless it is clearly material

Also check labels:

```bash
sqlite3 -header -column db/hf.db "
SELECT label, COUNT(*) AS count, MAX(labeled_at) AS latest
FROM story_quality_label
GROUP BY label
ORDER BY count DESC;
"
```

Red flags:

- many recent stories labeled `unclear` or `no_value`
- story judge has not labeled new stories
- headlines look fabricated or temporally impossible
- story markdown is thin, generic, or unsupported

### 7. Review Thesis Matching Quality (Aggregate)

Run:

```bash
sqlite3 -header -column db/hf.db "
SELECT l.thesis_id, l.story_id, l.relation, ROUND(l.confidence,2) AS conf,
       substr(l.updated_at,1,16) AS updated, substr(s.headline,1,90) AS story
FROM thesis_story_links l
LEFT JOIN story s ON s.id = l.story_id
ORDER BY l.updated_at DESC
LIMIT 20;
"

sqlite3 -header -column db/hf.db "
SELECT source, relation, COUNT(*) AS links, ROUND(AVG(confidence),2) AS avg_conf,
       MAX(updated_at) AS latest
FROM thesis_story_links
GROUP BY source, relation
ORDER BY source, relation;
"
```

Read the rationale for the newest/highest-confidence links:

```bash
sqlite3 -header -column db/hf.db "
SELECT thesis_id, story_id, relation, confidence, rationale
FROM thesis_story_links
ORDER BY updated_at DESC
LIMIT 10;
"
```

Quality bar:

- `supports` means the story reinforces the thesis direction
- `stresses` means the story challenges the thesis or an invalidation condition
- rationale names concrete facts from the story
- confidence is calibrated: high confidence only for clear matches
- unrelated macro noise is not forced into a thesis
- repeated matching runs do not rewrite links with weaker rationales

Red flags:

- all links are `supports` for days with obvious stress stories
- all links are `stresses` after a prompt/model change
- average confidence collapses
- rationales are generic or do not mention the actual story fact
- new stories have no links while active theses exist

### 8. Manually Read Recent Thesis-Story Links (Qualitative)

The aggregate stats in Step 7 catch volume and confidence regressions. They do
not catch *direction-flipped* links, theses that get force-attached to broad
macro stories, or rationales that paraphrase the headline without actually
mapping it to the thesis claim. Those failures only surface when a human (or
the agent doing this review) reads the underlying text.

This step is intentionally qualitative. Do not skip it because Step 7's numbers
look fine.

Pull the recent link batch with the data needed to evaluate each one:

```bash
sqlite3 db/hf.db <<'SQL'
.headers on
.mode line
SELECT
  l.thesis_id, l.story_id, l.relation, l.confidence, l.source,
  l.matched_invalidation, l.rationale, l.updated_at
FROM thesis_story_links l
WHERE l.updated_at >= datetime('now','-12 hours')
ORDER BY l.updated_at DESC;
SQL
```

For every distinct `story_id` in that batch, pull the headline, theme, and
`what_changed` summary:

```bash
sqlite3 db/hf.db <<'SQL'
.headers on
.mode line
SELECT id, headline, theme_tag, what_changed
FROM story
WHERE id IN (<comma-separated ids from the previous query>);
SQL
```

For every distinct `thesis_id` in the batch, read the first ~30 lines of
`global/theses/{thesis_id}.md` to capture the **Core Thesis** and
**Invalidation Conditions** verbatim:

```bash
for t in <thesis ids from the link batch>; do
  echo "===== $t ====="
  head -30 "global/theses/${t}.md"
done
```

For each link, render a one-line judgment using this rubric:

- ✅ Solid — the rationale faithfully maps a concrete fact in the story onto a
  specific clause of the thesis statement (or a named invalidation condition),
  AND the relation direction (`supports` vs `stresses`) is correct.
- ⚠️ Borderline — the link is defensible but indirect (2nd-order causal chain,
  shared sector but not shared mechanism, generic macro tailwind for a
  narrowly-scoped thesis). Acceptable at low confidence (≤0.75); a problem at
  high confidence.
- ❌ Wrong — direction-flipped, applied to a thesis the story has nothing to do
  with, or rationale invokes facts not actually present in the story.

Pay extra attention to:

- `matched_invalidation` rows. These should fire only when the story names or
  numerically crosses the exact threshold written in the thesis. A `stresses`
  link with conf ≥ 0.95 and a populated `matched_invalidation` is the most
  consequential output of the matcher — it changes thesis status. Re-read both
  texts and confirm the match is literal, not paraphrased.
- `source = 'backfill'` rows below conf 0.80. These bypass the stricter ingest
  gate and are the most common source of weak matches.
- Any thesis that gained 3+ links in the window. Confirm the thesis isn't
  abstract enough to absorb every story in its sector ("tech is doing well",
  "rates are uncertain").
- Any link whose rationale could be applied to *a different* thesis without
  changing a word. That is a tell that the matcher latched onto theme overlap
  rather than the thesis-specific claim.

Report findings as:

```text
Recent thesis-link qualitative review (last 12h, N links read)
- Solid:      X/N
- Borderline: Y/N  (list thesis_id ← story_id pairs)
- Wrong:      Z/N  (list thesis_id ← story_id pairs with one-line reason)

Standouts:
- Best link: <thesis_id ← story_id> — why it's a clean match (especially named-invalidation fires).
- Worst link: <thesis_id ← story_id> — why it shouldn't have been written.

Pattern-level observations:
- e.g. "backfill is over-attaching macro stories to narrowly-scoped sector theses at conf 0.80–0.85."
- e.g. "every link to thesis_011 in the last 24h cites generic tech-leadership stories with no mention of CPU inference / server OEMs."
```

Red flags:

- ≥1 ❌ wrong link in the window, especially a `stresses`/`matched_invalidation`
  link that would flip a thesis to `Stressed` incorrectly.
- ≥20% borderline rate sustained across two consecutive reviews.
- The same thesis collecting weak links day after day — likely the thesis
  statement is too broad or its tickers/themes are too generic; flag for
  rewrite rather than for a matcher tweak.
- Rationales that read like they were generated from the headline alone with
  no reference to the specific clause of the thesis.

If the qualitative read disagrees with Step 7's aggregate numbers, trust the
qualitative read and call out the discrepancy in the final report's
"Generated-text quality" section.

### 9. Review Discovered (System) Thesis Quality

Discovery runs on every ticker-bearing synthesized story (see
`docs/design-thesis-creation.md`). It generates 0–3 distinct, strong,
**global** (unowned) theses with `origin='system'`, writes them as
`review_status='candidate'`, and promotes to `active` when they clear the
structural gate (≥1 ticker, ≥2 invalidations) and pick up ≥1 story link.
Active system theses surface read-only on story pages (`build_story_suggestions`,
supports + conf ≥ 0.85). This step checks the generator is holding its
contract: **strong-only, prefer-fewer/zero, mutually orthogonal, non-duplicative**
— and that promotion isn't passing weak theses.

Promotion funnel and daily discovery volume:

```bash
sqlite3 -header -column db/hf.db "
SELECT review_status, COUNT(*) AS n, MAX(created_at) AS latest
FROM theses WHERE origin='system'
GROUP BY review_status ORDER BY n DESC;
"

sqlite3 -header -column db/hf.db "
SELECT date(created_at) AS day, COUNT(*) AS discovered
FROM theses WHERE origin='system'
GROUP BY date(created_at) ORDER BY day DESC LIMIT 10;
"
```

Recent system theses with tickers, horizon, and source story:

```bash
sqlite3 -header -column db/hf.db "
SELECT t.id, t.review_status, t.horizon_days, substr(t.created_at,1,16) AS created,
       t.source_context AS story_id,
       (SELECT group_concat(symbol||':'||direction) FROM entity_tickers e
        WHERE e.entity_type='thesis' AND e.entity_id=t.id) AS tickers
FROM theses t WHERE t.origin='system'
ORDER BY t.created_at DESC LIMIT 15;
"
```

Fan-out check (prefer-fewer / orthogonality) — stories that produced 2+ system
theses. For each, the set must be genuinely distinct angles, not near-twins:

```bash
sqlite3 -header -column db/hf.db "
SELECT source_context AS story_id, COUNT(*) AS theses_made, group_concat(id) AS ids
FROM theses
WHERE origin='system' AND created_at >= datetime('now','-3 days')
GROUP BY source_context HAVING COUNT(*) >= 2
ORDER BY theses_made DESC;
"
```

Read the content of the newest active system theses (verbatim Core Thesis +
Invalidation Conditions):

```bash
sqlite3 -noheader db/hf.db "SELECT id FROM theses WHERE origin='system' AND review_status='active' ORDER BY created_at DESC LIMIT 6;"
for t in <ids from the previous query>; do
  echo "===== $t ====="
  head -30 "global/theses/${t}.md"
done
```

Proposals that would surface on a story page (what users actually see):

```bash
sqlite3 -header -column db/hf.db "
SELECT l.story_id, l.thesis_id, ROUND(l.confidence,2) AS conf
FROM thesis_story_links l JOIN theses t ON t.id = l.thesis_id
WHERE t.origin='system' AND t.review_status='active'
  AND l.relation='supports' AND l.confidence >= 0.85
ORDER BY l.story_id DESC LIMIT 15;
"
```

Discovery activity / errors from the scheduler log (the pipeline prints
`[discover]` lines per story):

```bash
rg -n "\[discover\]|discover-story|thesis discovery failed" logs/hf-scheduler.log | tail -40
```

Quality bar:

- every active system thesis is declarative and non-hedged, names registry
  tickers with one clear direction, carries ≥2 concrete testable invalidations,
  and is durable beyond its source headline (not a one-day reaction)
- most stories yield **0–1** system thesis; 2–3 only when genuinely multi-angle,
  and that set must be mutually orthogonal (distinct tickers/mechanism/direction,
  not restatements of one belief)
- discovered theses do not duplicate existing theses — no near-twin of a thesis
  that already existed (dedup-vs-existing held)
- promotion is doing its job: `rejected` rows exist (the gate bites) and every
  `active` row clears the structural gate and has ≥1 story link
- surfaced proposals (active + conf ≥ 0.85) read as strong, specific bets a
  trader would actually take

Red flags:

- system theses with hedged/vague titles ("tech faces headwinds"), <2
  invalidations, or no clear direction → the inlined strength rubric is not
  holding; check the discovery prompt/model
- one story producing 2–3 near-identical theses → padding / orthogonality
  failure (sibling dedup at promotion not biting)
- many discovered theses near-duplicating existing ones → dedup-vs-existing
  regression
- discovery volume spikes (a system thesis on nearly every story) →
  over-generation, prefer-zero broken. Conversely, **zero** discoveries for 24h
  while ticker-bearing stories flow → discovery silently failing; grep the
  scheduler log for `thesis discovery failed` and exceptions
- a growing backlog of un-promoted `candidate` system theses → some is expected
  (no story evidence in the 14-day window), but steady growth suggests a
  matching/backfill problem upstream
- a weak active system thesis surfacing on a story page — it is user-visible

Discovery cost (now recorded in `llm_calls`):

```bash
sqlite3 -header -column db/hf.db "
SELECT caller, COUNT(*) AS calls, ROUND(AVG(cost_usd),5) AS avg_cost,
       ROUND(SUM(cost_usd),4) AS cost_usd, ROUND(AVG(total_tokens)) AS avg_tokens
FROM llm_calls
WHERE caller LIKE 'discover_%'
  AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')
GROUP BY caller ORDER BY cost_usd DESC;
"
```

- `discover_story_generate` (Gemini 3.1 Pro, one per ticker-bearing story) is the
  dominant cost (~$0.027/call; input is fixed overhead — the long system prompt +
  registry block — so a 0-candidate call still costs ~$0.022). `discover_dedup_judge`
  (Flash) fires only in the 0.80–0.88 rerank band and is ~$0.0008/call.
  `discover_single_generate` is the non-story path (repromote/chat).
- Watch the daily `discover_story_generate` count and total against plan
  open-decision #2 (add a `max_conf` cutoff if cost climbs). A daily spend much
  above the ~$0.6/day baseline (or call count >> stories/day) means the
  prefer-zero gate or the ticker pre-filter has slipped.
- **Still un-instrumented:** the ingest matcher (`story_judge.judge_pair`) and the
  per-thesis backfill (`match_story_for_thesis`) Flash calls are not in `llm_calls`.
  Discovery's backfill judge calls are therefore undercounted here; treat the
  `discover_*` rows as the generation cost, not the full discovery-triggered spend.

### 10. Review Thesis Scores

Run:

```bash
sqlite3 -header -column db/hf.db "
SELECT snapshot_date, COUNT(*) AS snapshots
FROM thesis_snapshots
GROUP BY snapshot_date
ORDER BY snapshot_date DESC
LIMIT 10;
"

sqlite3 -header -column db/hf.db "
SELECT t.id AS thesis_id, t.review_status, t.owner_count, t.score,
       t.score_freshness AS freshness, t.score_tailwind AS tailwind,
       COUNT(l.story_id) AS links
FROM theses t
LEFT JOIN thesis_story_links l ON l.thesis_id = t.id
GROUP BY t.id
ORDER BY t.score ASC, t.id;
"
```

If scores are stale or suspicious, dry-run one thesis:

```bash
uv run python -m agents.score_theses --dry-run --thesis thesis_001
```

Quality bar:

- every scored thesis (active `review_status` or any non-resolved owner) has a snapshot for today after the scoring run
- low scores correspond to stale support, price headwind, or stress links
- strong scores correspond to fresh support and aligned price action
- `score_tailwind` is not null for ticker-backed theses unless provider data is unavailable
- score direction is explainable from recent links and price moves

Red flags:

- snapshots missing for today
- all freshness scores are zero
- all tailwinds are null
- score distribution collapses to one value
- scores update but links/prices did not

### 11. Review Daily Brief Quality

Run:

```bash
sqlite3 -header -column db/hf.db "
SELECT brief_date, generated_at, model_version,
       json_array_length(themes_json) AS themes,
       json_array_length(source_story_ids) AS sources
FROM daily_briefs
ORDER BY brief_date DESC
LIMIT 7;
"

ls -lt global/briefs | sed -n '1,12p'
```

Read the newest `global/briefs/YYYY-MM-DD.md`.

Quality bar:

- brief date is today after the expected generation window
- 4 to 6 themes
- every theme cites source story ids
- themes are declarative market dynamics, not neutral summaries
- no unsupported numbers or tickers
- no stale carry-forward theme unless today's story flow is thin and source ids justify it
- market movers table has real prices and daily changes
- text is concise and useful to a multi-day/multi-week trader

Red flags:

- latest brief is not today
- theme count outside 4 to 6
- no sources or unknown sources
- themes cite unrelated stories
- movers are blank
- copy reads like generic market commentary rather than a thesis-driven brief

### 12. Review Price Providers

There are two price paths:

- scheduled scoring/brief movers: `src.clients.prices` routes Alpaca for US equities/ETFs and Mesh for the rest
- frontend-facing quote/chart API: `src.interfaces.prices.api` routes through EODHD

Run:

```bash
uv run python - <<'PY'
from src.clients.prices import quote_snapshot, window_returns

symbols = ["SPY", "QQQ", "BZ=F", "^VIX"]
quotes = quote_snapshot(symbols)
print("scheduled_quotes")
for sym in symbols:
    q = quotes.get(sym)
    print(sym, q.source if q else None, q.price if q else None, q.pct_change if q else None)

print("scheduled_returns")
for sym, row in window_returns(["SPY", "QQQ"], period="5d").items():
    print(sym, row.source, row.pct)
PY
```

Also inspect frontend quote output from Step 1.

Red flags:

- Alpaca env vars missing and scheduled jobs need US equity prices
- Mesh returns all nulls for movers
- EODHD key missing and frontend quotes return `upstream_failure`
- price path changed recently without tests being updated
- scoring says `mesh_price_batches_estimated=0` when price-backed scoring should run

### 13. Review Agent Chat And Cost Metrics

Run:

```bash
uv run python scripts/hf_metrics.py today
uv run python scripts/hf_metrics.py top-spenders --days 7 --json
uv run python scripts/hf_metrics.py charts --days 7
```

`charts` reports Code Interpreter / chart run stats (outcome counts, render rate,
skip reasons, failure stages, latency) plus the chart phase's token cost. Quality
bar: a healthy mix is mostly `plot` + `skip`; watch for `render rate ≈ 0%` (chart
agent never plotting), a spike in `error`/`timeout` outcomes, or one
`failure_stage` (e.g. `init`, `upload`) dominating — those point at a sandbox/R2
problem, not a model decision.

If there are recent agent requests, inspect:

```bash
sqlite3 -header -column db/hf.db "
SELECT created_at, endpoint, status, model_id, input_tokens, output_tokens,
       cost_usd, latency_ms
FROM agent_usage
ORDER BY created_at DESC
LIMIT 20;
"
```

Review:

- errors in the last 24h
- rows with tokens but zero cost
- missing session ids
- latency spikes
- unknown model ids
- repeated user or endpoint spend spikes

Inspect recent news/Gemini LLM calls:

```bash
sqlite3 -header -column db/hf.db "
SELECT created_at, caller, model_id, input_tokens, cache_read_tokens,
       output_tokens, thinking_tokens, total_tokens, cost_usd,
       ROUND(latency_seconds, 2) AS latency_s
FROM llm_calls
ORDER BY created_at DESC
LIMIT 20;

SELECT caller, model_id, COUNT(*) AS calls,
       SUM(input_tokens) AS input_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(output_tokens + thinking_tokens) AS billed_output_tokens,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(cost_usd), 6) AS cost_usd,
       ROUND(AVG(latency_seconds), 2) AS avg_latency_s
FROM llm_calls
WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')
GROUP BY caller, model_id
ORDER BY cost_usd DESC;
"
```

Interpretation:

- `llm_calls` covers the Gemini story-synthesis path (`synthesize_cluster*`) and
  thesis discovery (`discover_*`, see Step 9), not chat turns. The ingest matcher
  and per-thesis backfill judge calls are not yet recorded.
- Historical rows before 2026-05-25 have token columns defaulted to `0`; judge only recent rows after that date for token completeness.
- `total_tokens = 0` on new rows means Gemini usage metadata was missing or not parsed.
- nonzero tokens with `cost_usd = 0` means the model id is not in the local Gemini pricing table.
- input-token spikes usually mean cluster body/prompt bloat; output/thinking-token spikes usually mean the synthesis prompt is allowing verbose JSON or excessive reasoning.

Red flags:

- user-facing chat errors
- cost accounting gaps
- recent `llm_calls` rows with zero tokens or zero cost despite nonzero tokens
- runaway latency
- Bedrock/auth failures hidden by protocol-smoke mode

## Final Report Format

Use this shape:

```markdown
## Backend Health - YYYY-MM-DD HH:MM UTC

Status: ok | degraded | critical

Summary:
- One sentence on whether the system works well.
- One sentence naming the main risk.

Data freshness:
- Firehose:
- Full pipeline:
- News:
- Stories:
- Links:
- Scores:
- Brief:

Generated-text quality:
- Stories:
- Thesis matches:
- Discovered theses:
- Daily brief:

Provider/process status:
- PM2/API:
- Scheduler:
- Pricing:
- LLM providers:

Recommended next actions:
1. Immediate fix or investigation.
2. Follow-up quality review.
3. Optional cleanup.
```

Each section block stands in for findings — put severity-tagged evidence
inline (e.g. `Stories: [warn] no new stories since 14:02 UTC`).

## Severity Guidance

Use the per-step "Red flags" blocks as the source of truth for what to flag.
Map each flag to a status using this rule:

| Status | Trigger |
|---|---|
| `critical` | The product is broken or actively misleading users — process down, pipeline stuck >24h, brief/scores missing past their window, generated text plainly false, secrets leaked, DB/markdown diverged. |
| `degraded` | Subsystem runs but output is stale, weak, or partial — one provider failing with fallback intact, some active theses unlinked, telemetry gaps, qualitative review finds borderline matches but no wrong ones. |
| `ok` | All red-flag blocks are clean and the qualitative spot-checks (Steps 6, 8, 9, 11) pass. |

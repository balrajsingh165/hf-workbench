# Design: Social-Topic Ingestion (Grok X Search → feed stories)

**Status:** agreed (2026-06-03), not yet built. Implementation plan: [`plan-social-ingestion.md`](./plan-social-ingestion.md).
**Scope:** The pipeline that turns X (Twitter) discussion into feed items: generation via Grok X Search, the deterministic verifier, ticker selection and refresh cadence, persistence **as `story` rows** (`kind='x'`), and integration with the existing pipeline's consumers. Feed ordering and the card contract are owned by [`design-feed-ranking.md`](./design-feed-ranking.md); sector derivation by [`design-sectors.md`](./design-sectors.md). Feasibility, model choice, prompt style (V7), and verifiability were settled live in `spikes/grok-social/FINDINGS.md`.

---

## Why

The trending lane converts retail *demand* into more news stories — it starts from the tickers retail is talking about, retrieves articles, and strips the conversation out at ingest. The social conversation itself (the bull/bear arguing, the heat, the source posts) never reaches the product. The spike proved Grok's server-side `x_search` closes that gap without an X API integration: structured heated-topic JSON per ticker in house voice, with per-tweet URLs deterministically verifiable against the API's `url_citation` annotations, at ~$0.056/ticker/call.

## Vocabulary

- **Social topic** — one heated X discussion about one ticker: title, heat 1–5, 2–3-sentence summary, bull/bear angles (V7 voice), 3–6 verified source tweets. The product unit, rendered as a `kind: "x"` feed card.
- **Run** — one pipeline pass: rank-select tickers → one Grok call per selected ticker → verify → admit → persist.

---

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Persistence | **Rows in the existing `story` table, `kind='x'`** — not a parallel table (re-confirmed 2026-06-03 after codebase review) | One feed composer, one detail endpoint, one markdown convention, and `thesis_story_links` matching reuses as-is (the future "crowd is arguing your thesis" hook needs zero new link plumbing). Costs, stated honestly: a one-off copy-rebuild of `story` (SQLite can't relax NOT NULL via ALTER) and `kind='story'` guards on every non-JOIN story reader (full list in § Pipeline integration), enforced by an audit backstop. |
| Generation | One structured call per ticker on `grok-4.20-0309-reasoning`, V7 prompt from the spike | Beat the alternatives on cost and per-ticker depth (FINDINGS § verdict). |
| Admission gate | Deterministic structural verifier, no LLM judge: JSON parses; enums valid; every tweet annotation-backed by status ID; **≥3 tweets survive per topic** | Mirrors `src/news/verifier.py`'s role. Tweet verification kills hallucinated sources for free. No rule-based voice gates (hedge-word/number-density regexes false-positive on legitimate prose) — voice is the prompt's job, reviewed via e2e dry-runs and the soak. |
| Ticker selection | Trending lane Tier-1 (`ticker_trends` effective rank), top `SOCIAL_TICKERS_MAX` | The demand signal already exists and is already failure-isolated; no second ranking source. |
| Volume | **One live topic per ticker** (revised 2026-06-04): only the batch's top-heat topic is considered, gated by heat ≥ `SOCIAL_HEAT_MIN` (4), new rows capped at `SOCIAL_DAILY_CAP` (20)/day, all drops logged | First production day showed Grok slices one event into several near-identical "topics" (AVGO earnings → "Drops on Mixed Guidance" + "Buy the Earnings Dip") and its heat ladder is rank-shaped (every batch leads 5, 4, …), so "heat ≥ 4" was operating as "take top 2". The card already carries bull/bear angles + up to 6 tweets; extra facets belong inside it. No silent caps — dropped topics are counted in metrics. |
| Refresh semantics | **Keyed by ticker** (revised 2026-06-04): a run's top topic refreshes the ticker's live row in place — heat/angles/tweets/headline; `created_at` untouched; refreshes don't consume the daily cap. Title-overlap matching remains for cross-ticker duplicates only | A topic still heated tomorrow IS today's heated topic — it should update, not duplicate. Title matching can't do this: Grok retitles the same discussion every run (`topics_refreshed` was 0 across all runs while near-duplicates piled up), so the stable identity is the ticker, which one-live-topic-per-ticker makes well-defined. Not bumping `created_at` keeps a re-heated topic from camping at the top forever. The `kind='x'` rows are the one mutable exception to story immutability — which is exactly why `hf_health`'s `created_at`-based stall alarm needs the `kind='story'` guard. |
| Expiry | Feed eligibility = `created_at` within 48h — **derived, no `expires_at` column**; expired rows **stay in the DB**; a still-hot discussion re-enters as a fresh row once the old one leaves the window | One less column and one less rule to keep in sync; cheap archive — the history of a debate is the raw material for a future per-ticker social view. |
| Boot guard | The boot sweep is skipped when the lane ran within `social_interval_hours / 2`, judged by `llm_calls` (`caller='social_topics'`) — not `story.created_at`, which refreshes leave untouched (added 2026-06-04) | Every pm2 restart re-ran the full sweep (~$0.80) minutes after the last one; three sweeps in the first production hour. Fails open. `uv run python -m agents.social_topics` forces a run regardless. |
| Sectors | Derived at read from the ticker's `instruments.sectors_json`; nothing stored, never asked of Grok | `design-sectors.md` decision — deterministic, free, keeps the prompt lean. |
| Story-only consumers | **Every non-JOIN story reader gains `kind='story'`** (readers that `JOIN news_cluster` exclude NULL-cluster rows automatically); the audit gains social invariants + an exclusion backstop | Social rows have their own gate and no cluster. Reader-by-reader list in § Pipeline integration. |
| Failure isolation | Identical to the trending lane: the stage never raises, every failure degrades to "fewer social items", `ok=false` metric with `phase` | Social is additive. A Grok outage must never block firehose/route/synth or surface a user error. |
| Kill switch | None (always-on), same as the trending lane; missing `XAI_API_KEY` no-ops the stage with a critical metric finding | Same lesson as dropping `HF_TRENDING_DISABLED`: failure isolation is the off-switch. |

---

## Data model

Additions and relaxations to `story` in `db/schema.py`:

```python
("kind",        "TEXT NOT NULL DEFAULT 'story'"),  # 'story' | 'x'
("heat",        "INTEGER"),                        # 1-5; kind='x' only
("social_json", "TEXT"),                           # kind='x' payload, see below
```

Relaxations: `cluster_id` (today `NOT NULL UNIQUE`) and `centroid_news_id` (`NOT NULL`) become nullable — `kind='x'` rows have no cluster; UNIQUE stays (SQLite admits multiple NULLs). **This is not an additive change:** SQLite cannot drop NOT NULL via `ALTER TABLE`, so it applies as a one-off copy-rebuild of `story` (create new table, `INSERT … SELECT`, drop, rename — all rows preserved, indexes re-created, existing rows take `kind='story'` via the default). No feed-eligibility column — expiry is derived from `created_at` (Decisions).

`social_json` shape (everything the X card renders that isn't already a story field):

```json
{
  "bull_angle": "…",                 // V7 voice, 1-2 sentences
  "bear_angle": "…",
  "tweets": [
    {"handle": "@…", "url": "https://x.com/…/status/…", "stance": "bull",
     "claim": "…", "engagement": "…"}
  ]
}
```

Tweets are stored post-verification — unverified tweets were dropped at the gate, so there is no per-tweet `verified` flag (it would be invariantly true). Per-call cost is not stored here either; it goes to the `llm_calls` table (§ Generation).

Reused story fields: `headline` = topic title; `overview_json` = the summary as one cited-less bullet (`source_doc_ids: []` — social citations are tweets, not news rows); tickers via `entity_tickers` (`entity_type='story'`); `theme_tag` stays `'other'` (also keeps thesis discovery out by its existing filter); `sectors_json` stays `'[]'` (derived at read). Markdown renders to `global/stories/{id}.md` like any story — title, summary, angles, tweet list — so the chat agent can read social topics the same way it reads stories.

---

## Generation

Per selected ticker, one call: `POST https://api.x.ai/v1/responses`, model `grok-4.20-0309-reasoning`, `x_search` tool with `from_date = today − SOCIAL_LOOKBACK_DAYS` (2). Prompt = the spike's settled PROMPT/STYLE/SCHEMA (`spikes/grok-social/social_topics.py`), with two changes: tweets per topic become **3–6** (was 2–6) so the ≥3-survivors gate has headroom, and the topic-kind field (`discussion | debate | event | info`) is **dropped from the schema** — cut 2026-06-03, it earned no read-time consumer. Company name from `instruments.display`. Exact per-call cost from `usage.cost_in_usd_ticks × 1e-10`, logged per call to the `llm_calls` table (`caller='social_topics'`, entity = the ticker) — the same pattern synthesis Gemini calls use (`src/news/persist.py::_log_synth_llm_call`) — plus `usd_total` on the run metric.

## Verifier (deterministic, per topic)

1. Response JSON parses (`parse_json_loose`); `heat`/`stance` in enum; required fields non-empty.
2. Every tweet's status ID present in the API's `url_citation` annotations (join on the numeric ID — annotations use `x.com/i/status/{id}`, the model writes `x.com/{handle}/status/{id}`). Unverified tweets are dropped, not flagged.
3. **≥3 tweets survive**, else the topic is rejected.
4. No voice rules — an earlier hedge-word/number-density regex pass was cut (false-positives on legitimate prose, e.g. the month "May" in dated evidence). Voice lives in the prompt and is evaluated on real output (e2e dry-runs, soak review), not gated. Rejections are metric-counted; no rejection table (the next run is a fresh generation anyway — revisit only if reject rates demand a cooldown).

## Selection, cadence, refresh

- **Run cadence:** its own APScheduler job in `agents/pipeline_scheduler.py` — `IntervalTrigger(hours=SOCIAL_INTERVAL_HOURS)` (24) — exactly how the trending tiers are scheduled (separate jobs, no in-process gating, no state). One run refreshes every selected ticker; halve the interval for 2×/day. No per-ticker "due" bookkeeping. The boot pass (every pm2 restart) is guarded: skipped when `llm_calls` shows the lane ran within half the interval (`social_boot_due`). **Production runs manual-only since 2026-06-04:** `ecosystem.config.cjs` passes `--no-social`, which drops both the interval job and the boot pass; sweeps happen only via `uv run python -m agents.social_topics`.
- **Ticker pool:** Tier-1 from the latest `ticker_trends` snapshot, ordered by effective rank, capped at `SOCIAL_TICKERS_MAX` (12). Registry-known symbols only. Empty snapshot → no-op (fail-soft, same as the trending lane reads).
- **Admission (revised 2026-06-04):** one topic per ticker per run — the batch's top-heat topic; the rest are dropped as `rejected_rank` (Grok's heat ladder is rank-shaped, so the old `heat ≥ 4` gate was effectively "take top 2", and the runner-up is almost always the same event's other facet). The top topic must still clear `heat ≥ SOCIAL_HEAT_MIN` (4); new rows stop at `SOCIAL_DAILY_CAP` (20)/day. Everything dropped (rank, low heat, cap overflow, verifier rejects) is counted in the run metric.
- **Dedupe/refresh (revised 2026-06-04):** keyed by ticker. If the ticker has a live (≤48h-old) topic, refresh it in place — `heat`, `social_json`, `headline`, markdown; `created_at` untouched; exempt from the daily cap (no new row). Otherwise check **all** live topics by normalized-title token overlap for cross-ticker duplicates → skip (`rejected_dup`): one X discussion often surfaces under several tickers in the same run (observed live: a Jensen keynote admitted under both NVDA and AVGO with near-identical titles). No live topic, no cross-ticker match → new row. Title overlap is *not* used for same-ticker refresh — Grok retitles the same discussion every run ("AVGO Drops on Mixed Guidance" → "AVGO Plunges on Unraised 2027 Target"), so it never matched in production (`topics_refreshed=0` across all runs while duplicates accumulated).

## Pipeline integration

- New stage `agents/social_topics.py`, registered in `agents/pipeline_scheduler.py` as **its own APScheduler job** (the trending-tier pattern), so a stall cannot block ingest. It reads whatever the latest `ticker_trends` snapshot is when it fires — no ordering dependency on the trending job beyond snapshot existence. Manual: `uv run python -m agents.social_topics` (`--ticker MSTR` for one symbol, `--dry-run` to print without writing).
- Library code in `src/social/` (ported from the spike): `grok.py` (Responses-API client), `topics.py` (prompt/schema/parse/verify), `persist.py` (story row + markdown + `entity_tickers`).
- **Metrics:** `social_run` event in `logs/hf-pipeline-metrics.jsonl`: `{ok, phase, tickers_selected, tickers_called, topics_returned, topics_admitted, topics_refreshed, rejected_verifier, rejected_rank, rejected_heat, rejected_cap, rejected_dup, tweets_dropped, usd_total, x_searches, duration_s}`. `topics_admitted` counts new rows only; refreshes are counted separately in `topics_refreshed`. Missing `XAI_API_KEY` or all-calls-failed → `ok=false` critical finding on `/metrics` (no fallback source, same posture as `trending.fetch_failed`).
- **Story-only consumer guards.** Readers that `JOIN news_cluster` (the current home feed, `GET /api/stories`, the audit's structural pass) exclude NULL-cluster rows automatically. Every **non-JOIN** reader gains `kind='story'`:
  - `agents/judge_stories.py` candidate selection (social must not be judged; a `no_value` label would tangle with feed hiding)
  - daily-brief story selection (`src/brief/pipeline.py`)
  - `scripts/hf_health.py`: `stories_24h`, the embed-completeness check, and `last_story_created_at` — the pipeline **stall alarm**; a daily social refresh bumping `created_at` must not mask a dead news pipeline
  - `src/pipeline_metrics/funnel.py` story counts
  - `scripts/backfill_story_themes.py`, `scripts/rerender_story_markdown.py` (would overwrite social markdown with the news renderer), `scripts/story_quality_weekly.py`, `scripts/export_story_review_packet.py`, `scripts/regenerate_ambiguous_story_report.py`
  - **No guard needed:** `discover_thesis` (`theme_tag != 'other'` already excludes social) and `src/news/verifier.py` (runs pre-insert inside `write_cluster_story`; never sees social rows).
  - `scripts/audit_news_rearchitecture.py` adds the enforcement backstop: social invariants (`kind='x'` ⇒ NULL `cluster_id`, `heat` 1–5, ≥3 tweets in `social_json`) plus checks that no `kind='x'` id appears in `story_quality_label`, brief inputs, or `story_match_chunks`.
- **Thesis matching (deferred, designed-for):** because social topics are story rows, running `match_thesis_for_story` on admitted topics is a one-line enablement producing ordinary `thesis_story_links`. Off at launch — the matcher prompt is tuned for news stories and needs its own eval pass before social debates flow into thesis timelines and scoring.

## Read surface

- `/api/home` feed items gain `kind`, and for `kind='x'`: `heat`, `bullAngle`, `bearAngle`, `tweets[]` (verified only), no `thumbnail`. Sources array carries the single synthetic `x` source (name "X", domain `x.com`) for the X-logo card. Ordering/density per `design-feed-ranking.md` (32h half-life, 48h expiry, 2-in-5 window).
- `GET /api/news/{id}` returns the social payload for `kind='x'` ids — the topic detail view.
- Frontend: distinct X card; re-run `bun run gen:types` after the API change.

## Cost model

Defaults (12 tickers × 1 refresh/day × ~$0.056) ≈ **$0.67/day**; 2×/day ≈ $1.35/day. Worst-case knobs (20 tickers × 2) ≈ $2.24/day. Every run logs exact spend from usage ticks.

---

## Watch items

- **Refresh-forever.** Resolved 2026-06-04: refreshes no longer bump `created_at`, so a topic's front-page lifetime is bounded by its first admission; a week-long debate re-enters as a fresh row every 48h instead of camping.
- **Cross-ticker title-overlap quality.** Token overlap remains the cross-ticker duplicate check (same-ticker dedupe is now ticker-keyed and exact). If one discussion still lands under two tickers with different titles, upgrade the cross-ticker check to embedding similarity (the clustering pass-2 machinery exists).
- **Matcher readiness.** Before enabling thesis matching, eval whether the matcher treats a bear angle quoting the thesis's own invalidation as a stress (it should) rather than generic same-ticker noise.
- **i18n.** `design-i18n.md`'s `translate_stories` stage (proposal) walks story markdown; refresh-in-place re-stales `source_sha` daily, meaning daily retranslation. When i18n ships, exclude `kind='x'` at first and opt social in deliberately.

## Deferred

- Thesis matching enablement (above) and the digest line it powers ("X is arguing your MSTR thesis").
- Per-ticker social view (the debate's history — expired rows are kept for exactly this).
- Rejection cooldown table (only if reject rates demand it).
- `allowed_x_handles` curation (quality lever if spam handles start surviving verification).

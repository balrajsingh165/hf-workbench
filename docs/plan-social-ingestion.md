# Plan: Social-Topic Ingestion + Feed Integration

**Implements:** [`design-social-ingestion.md`](./design-social-ingestion.md) and the composer/ranking side of [`design-feed-ranking.md`](./design-feed-ranking.md).
**North-star check:** this ladders to the feed's "trendy, alive" texture and (deferred hook) the Tier-1 digest moment — "X is arguing your MSTR thesis."
**Sequencing rule:** social rows must never reach users through an unranked reverse-chron feed (they'd batch-bury, the exact problem the ranking design solves). Phase 0 guards every existing consumer *before* Phase 2 writes the first row; Phase 3 is what makes social user-facing.

The sector track (`design-sectors.md` § Build order) is independent and can run in parallel; until the instrument seed pass lands, the feed's sector-affinity term is simply 0 and the "for me" filter falls back to tickers + judged matches.

---

## Phase 0 — Schema migration + consumer guards (no behavior change)

1. `db/schema.py`: add to `story` — `kind TEXT NOT NULL DEFAULT 'story'`, `heat INTEGER`, `social_json TEXT`; make `cluster_id` and `centroid_news_id` nullable (UNIQUE on `cluster_id` stays — SQLite admits multiple NULLs). **Not additive** (SQLite can't drop NOT NULL via ALTER): apply as a one-off copy-rebuild of `story` — create the new table, `INSERT … SELECT` all rows, drop, rename, re-create indexes; existing rows take `kind='story'` via the default.
2. Add `kind = 'story'` guards to every **non-JOIN** story reader. Readers that `JOIN news_cluster` — today's `build_feed_stories`, `GET /api/stories`, the audit's structural pass — exclude NULL-cluster rows automatically and need nothing.
   - `agents/judge_stories.py` candidate selection
   - daily-brief story selection (`src/brief/pipeline.py`)
   - `scripts/hf_health.py`: `stories_24h`, the embed-completeness check, and `last_story_created_at` (the stall alarm — a social refresh bumping `created_at` must not mask a dead news pipeline)
   - `src/pipeline_metrics/funnel.py` story counts
   - `scripts/backfill_story_themes.py`, `scripts/rerender_story_markdown.py` (would overwrite social markdown with the news renderer), `scripts/story_quality_weekly.py`, `scripts/export_story_review_packet.py`, `scripts/regenerate_ambiguous_story_report.py`
   - No guard for `discover_thesis` (`theme_tag != 'other'` already excludes) or `src/news/verifier.py` (pre-insert only; never sees social rows).
3. `scripts/audit_news_rearchitecture.py`: add the enforcement backstop — social invariants for `kind='x'` rows (NULL `cluster_id`, `heat` 1–5, ≥3 tweets in `social_json`) plus exclusion checks (no `kind='x'` id in `story_quality_label`, brief inputs, or `story_match_chunks`).
4. **Done when:** full test suite + audit script green; `/api/home` byte-identical before/after; `story` row count unchanged across the rebuild.

## Phase 1 — `src/social/` library (offline-testable, no key needed)

Port the spike (`spikes/grok-social/`) into the tree; the spike folder stays as the findings record.

1. `src/social/grok.py` — Responses-API client from `grok_client.py` (httpx, `XSearchOptions`, `GrokResult`, defensive `_extract`). `XAI_API_KEY` moves into `src/config.py` alongside the other keys.
2. `src/social/topics.py` — PROMPT/STYLE/SCHEMA (V7 verbatim from `social_topics.py`, two edits: 3–6 tweets per topic; topic-kind field dropped from the schema), `parse_json_loose`, and `verify_topics(parsed, citations) -> (admitted, rejections)` implementing the structural gate: enums (`heat`/`stance`), per-tweet status-ID annotation join (drop unverified), ≥3 survivors per topic. No rule-based voice checks — voice quality is reviewed via e2e dry-runs (Phase 5 soak). Rejections carry a reason code for metrics.
3. `src/social/persist.py` — `write_social_topic(conn, topic, ticker)`: allocate from the same `story_N` id sequence `write_cluster_story` uses; insert row (`kind='x'`, `heat`, `social_json`, `theme_tag='other'`, headline/overview mapping per design); `entity_tickers` row; markdown render to `global/stories/{id}.md` (title, summary, angles, tweet list); log the Grok call to `llm_calls` (`caller='social_topics'`, mirroring `src/news/persist.py::_log_synth_llm_call`). Plus `refresh_social_topic(...)` — the in-place update path (content + `created_at` bump, which restarts the 48h feed-eligibility window).
4. Unit tests (fixtures = captured spike responses, no API key): parser on fenced/cited/clean JSON; verifier drops an unverified tweet, rejects <3 survivors, rejects an out-of-enum heat/stance; persist/refresh round-trip on a temp DB; title-token-overlap dedupe matcher.
5. **Done when:** `uv run pytest` green offline.

## Phase 2 — Pipeline stage

1. `agents/social_topics.py` — the run: read latest Tier-1 snapshot (rank order, cap `SOCIAL_TICKERS_MAX=12`, registry-known only; empty snapshot → no-op `ok=false phase='no_snapshot'`); per ticker call Grok (`from_date = today − SOCIAL_LOOKBACK_DAYS=2`, name from `instruments.display`); verify; dedupe vs the ticker's live (≤48h) topics (normalized-title token overlap → refresh, else insert); admit by `heat ≥ SOCIAL_HEAT_MIN=4` until `SOCIAL_DAILY_CAP=20` for the day; count every drop. CLI: `--ticker MSTR`, `--dry-run` (generate+verify, print, no write), `--limit N`.
2. Failure isolation: top-level never raises; per-ticker try/except degrades to fewer topics; missing `XAI_API_KEY` → no-op with `ok=false phase='auth'` (critical finding, same posture as `trending.fetch_failed`).
3. Metrics: emit `social_run` to `logs/hf-pipeline-metrics.jsonl` — `{ok, phase, tickers_selected, tickers_called, topics_returned, topics_admitted, topics_refreshed, rejected_verifier, rejected_heat, rejected_cap, tweets_dropped, usd_total, x_searches, duration_s}`. Wire into `scripts/hf_health.py` findings (critical on `ok=false`).
4. Scheduler wiring (`agents/pipeline_scheduler.py`): register as **its own APScheduler job** — `IntervalTrigger(hours=SOCIAL_INTERVAL_HOURS=24)` — exactly the trending-tier pattern. No in-process cadence gate, no "due" bookkeeping, no state table.
5. **Done when:** `uv run python -m agents.social_topics --ticker MSTR` writes a verified `kind='x'` row + markdown + `llm_calls` row; a full module run emits `social_run`; `/api/home` still shows zero social items (the current composer's `JOIN news_cluster` holding).

## Phase 3 — Feed composer (`design-feed-ranking.md`)

1. `api.py`: `build_feed_stories(conn)` → `build_feed(conn, user_id)`. Candidate pool: `kind='story'` last 72h (judge-hidden excluded) ∪ `kind='x'` with `created_at > now − 48h`. Compute `AffinityContext` once (owned-thesis tickers + `user_watchlist` → `user_tickers`; judged matches from `thesis_story_links` filtered to owned theses — the item's `matches[]` payload stays global; sectors via `instruments.sectors_json` ∪ `theses.sectors_json` when populated, else empty).
2. Score per the design: `(base + trend + affinity) × freshness`, hour-quantized age, `FEED_WEIGHTS` block holding every knob (half-lives 14h/32h, base/trend/affinity weights, 0.45 affinity cap, windows, expiry).
3. Composition pass: ≤2 `x` per 5 consecutive slots; same lead ticker never adjacent; demote, don't drop; tie-break `created_at DESC`; cap `HOME_FEED_LIMIT`.
4. Payload: `kind` on every item; for `kind='x'`: `heat`, `bullAngle`, `bearAngle`, `tweets[]` (verified only), no `thumbnail`; synthetic `x` source (name "X", domain `x.com`); sectors derived from ticker instruments. `?explain=1` returns per-item `{kind, freshness, base, trend, affinity, score, demotions}`. `GET /api/news/{id}` returns the social payload for `kind='x'` ids.
5. Tests: decay/affinity-grading unit math; composition windows on synthetic lists (social wall → demoted; adjacent same-ticker → separated); golden ordering test (fixed clock, synthetic rows: fresh heat-5 social ranks above hours-old single-source story, below a judged-match corroborated story for that user); `explain` schema.
6. Docs: update `docs/news-story-pipeline.md`'s feed section — the reverse-chron/single-query description and the "rows are story rows" contract now describe the ranked mixed-kind composer.
7. **Done when:** `/api/home` serves the mixed ranked feed; the morning social batch sits near the front at simulated 3pm and is gone next morning (the half-life feel test, via fixed-clock test).

## Phase 4 — Frontend (`~/heurist-finance-frontend`)

1. `bun run gen:types` against the new OpenAPI shape.
2. Feed renderer discriminates `kind`; new X card: X logo, no image, title + summary, bull/bear angle block, source-tweet rows (handle → x.com link, stance) — visually distinct from news cards per the locked decision.
3. **Done when:** mixed feed renders; X cards link out to real posts; type-check green.

## Phase 5 — Soak + quality review

1. Run at defaults for 3–5 days. Review against the spike's quality bar: admitted topics read in V7 voice, tweets real, no padding.
2. Metrics review: reject rates by reason (high `rejected_verifier` → prompt drift; high `rejected_heat` → consider `SOCIAL_TICKERS_MAX` cut), daily `usd_total` vs the ~$0.67 model, refresh-vs-new ratio.
3. Tune `FEED_WEIGHTS` via `?explain=1` eyeball sessions; revisit the refresh-forever watch item with data (does a week-long debate need a max-refresh retirement?).
4. **Done when:** a week of feed reads diverse, trendy, and honest — the original product complaint — and the knobs have settled.

---

## Explicitly deferred (tracked in the design docs' Deferred sections)

Thesis matching on social rows + the digest line; per-ticker social view; market-wide multi-agent sweep; rejection-cooldown table; handle allowlists; sector-affinity activation (lands automatically when the sector seed pass writes `instruments.sectors_json`).

## Validation notes

- No `hf-evals` run needed through Phase 5 — no agent-chat behavior changes. The deferred matcher enablement is the first step that will need one.
- Verifier and composer are deterministic — unit tests are the contract; no LLM judges in this pipeline.

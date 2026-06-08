# Plan: Trending-Ticker Demand-Driven Retrieval

**Status:** Proposed (2026-06-02).
**Depends on:** firehose ingest spine (`insert_entry`), cluster→route→synth→verify→persist pipeline, `discover_story_theses` (story-thesis proposals), scoring/match — all shipped.
**Related:** `docs/news-story-pipeline.md` (the spine this feeds), `docs/design-thesis-creation.md` (downstream discovery this reuses verbatim), `agents/firehose.py` (the lane this parallels).

---

## Goal (one sentence)

Close the social/idiosyncratic coverage gap by adding **one new ingest lane** that, on a two-tier cadence, pulls the day's trending tickers, retrieves news for them via web search, and drops the results into the **existing** `news` table — so every downstream system (clustering, routing, synthesis, verification, scoring, thesis discovery) consumes them **unchanged**.

### Why (the gap, measured)

The firehose is supply-driven: it polls 20 fixed press-wire + macro feeds and discovers whatever tickers appear. It structurally cannot see attention-driven or off-wire single-name moves. Checked against a Reddit/1ms top-15 on 2026-06-02: **3 of the top names (SPCE #1, OPEN #4, ASTS #10) had zero stories in 7 days**, and others were stale. These are exactly the names that move on retail flow and minor off-wire developments, not press releases.

The fix is to **invert the funnel**: start from the tickers that are hot, then go fetch news for them. The trending list itself becomes the retrieval queue.

---

## The one idea

> **The trending list is the watchlist. Web search is the retriever. The existing pipeline is everything else.**

A ticker stays in the retrieval queue for exactly as long as it stays on the trending list — so "sustained interest" produces sustained retrieval (catching slow follow-on events like a second quantum-computing headline a week after the first), and "fell off the list" is the decay rule for free. No separate watchlist table, no arc-liveness computation, no new story/thesis code.

---

## Architecture

```
  ┌──────────────────────────────────────────────────────────────┐
  │ NEW: agents/trending.py  ──  run_trending(tier)               │
  │  1. fetch 1ms ranking  (source=stocks)                        │
  │  2. resolve symbols → instruments registry; upsert ticker_trends│
  │  3. select symbols due for THIS tier (by rank)                │
  │  4. per symbol: exa_web_search (recency-filtered, last 48h)   │
  │     → exa_scrape_url top hits → full bodies                   │
  │  5. each scraped doc → firehose_gate → FirehoseEntry          │
  │     → insert_entry()   [SAME path as the press-wire firehose] │
  └───────────────────────────────┬──────────────────────────────┘
                                   │  writes `news` rows
                                   ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ EXISTING PIPELINE — ZERO CHANGES (one small routing tweak)    │
  │  cluster → route → body-enrich → synthesize → verify →        │
  │  persist story → match → discover_story_theses → score        │
  └──────────────────────────────────────────────────────────────┘
```

The new lane produces `news` rows and stops. It owns nothing downstream. This is the whole reason it's cheap and safe: every existing quality gate (gate, verifier, judge, promotion gate) still applies untouched.

---

## The two tiers (the user's cadence)

Two scheduler jobs, distinguished only by rank range and interval. Both call the same `run_trending`.

**Tiering is decided by today's *and* yesterday's ranking — a one-day residue.** We persist a snapshot every day, so yesterday's rank is always on hand. Tier membership uses the **best (lowest) rank a symbol held across today and yesterday**:

```
effective_rank(symbol) = min(rank_today, rank_yesterday)     # missing day → +∞
```

This gives the residue effect the design wants: a name that was hot yesterday (rank 8) but slipped today (rank 25) **still gets Tier-1 daily treatment for one more day**, because that is exactly when its off-wire follow-on tends to land. A name has to be cold on **both** days to fall out. Climbers are caught by today's rank; faders get a one-day grace from yesterday's. (The 1ms `变化`/delta and `排名趋势`/rank-trend columns corroborate the move and order symbols within a tier's budget, but the residue itself comes from comparing the two persisted snapshots, not from the delta column alone.)

| Tier | Cadence | Symbols (by `effective_rank`) | Rationale |
|---|---|---|---|
| **Tier 1 — hot** | **daily** | `effective_rank` ≤ `TIER1_MAX` (default **20**) | The fast movers + yesterday's residue. Daily cadence keeps live arcs fresh and catches off-wire follow-ons within a day. |
| **Tier 2 — tail** | **every 3 days** | `effective_rank` in (`TIER1_MAX`, `TIER2_MAX`] (default 21–60) | Broader sweep of the long tail. Lower cadence bounds cost; catches slow-burn / newly-emerging names before they reach Tier 1. |

Tier 2 excludes Tier-1 ranks so the daily names aren't re-scraped on the 3-day day. `ticker_trends` is upserted on **every** run (both tiers fetch the full ranking); only the scrape step is rank-gated. This keeps the persisted trending snapshot complete for the future homepage surface even though we only spend web-search budget on the due tier — and it is what makes yesterday's rank available for the residue computation.

---

## New code (the entire surface — keep it this small)

1. **`agents/trending.py`** — the lane. One module:
   - `fetch_ranking(source="stocks") -> list[TrendRow]` — HTTP GET + HTML-table parse of 1ms `https://1ms.news/ranking?source=stocks`. **Spike-confirmed (2026-06-02):** the page is server-rendered HTML (Phoenix/Caddy), HTTP/2 200, ~60 KB; the ranking is a single `<table>` with a header row and **100 data rows**, columns `排名 / 代码 / 名称 / 24h 提及 / 变化 / 点赞 / 排名趋势` (rank / symbol / name / 24h-mentions / change / upvotes / rank-trend). No JS rendering, no `__NEXT_DATA__`, no auth. So a plain `requests` (or `httpx`) GET with a UA header + `BeautifulSoup`/`lxml` row parse is sufficient — **no browser automation, no JS render, no third-party API fallback.** A non-200, a missing/renamed table, or <N parsed rows is a hard failure (see *Failure handling* below), not a silent empty return.
   - `upsert_trends(conn, rows)` — resolve each raw symbol to the `instruments` registry (reuse the loader behind `src/news/ticker_candidates.py`), write `ticker_trends`. Registry-unknown symbols are still persisted (raw) but **not scraped** (a thesis can't form on an unknown instrument anyway — same constraint the firehose already has).
   - `run_trending(tier: int) -> dict` — orchestrates fetch → upsert → select-by-tier (`effective_rank`) → per-symbol `exa_web_search` + `exa_scrape_url` → `firehose_gate` → `insert_entry`. Mirrors `agents/firehose.py::run_firehose` (`agents/firehose.py:676`), reusing `insert_entry` (`agents/firehose.py:520`) and `FirehoseEntry` (`agents/firehose.py:336`). Always emits a `trending_run` metric with `ok`/`error` (see *Failure handling*); a fetch/parse failure sets `ok=false` and skips the run rather than poisoning the DB.
   - `python -m agents.trending --tier 1|2 [--dry-run]` CLI for manual runs, matching the firehose CLI ergonomics.

2. **`PipelineService.run_trending(tier)`** (`src/pipeline/service.py`) — thin wrapper mirroring `run_firehose` (`:92`): run_id, try/except, `trending_run` metric emit.

3. **Scheduler registration** (`agents/pipeline_scheduler.py`) — two `IntervalTrigger` jobs (`IntervalTrigger(days=1)` tier 1, `IntervalTrigger(days=3)` tier 2), gated by a `HF_TRENDING_DISABLED` env flag, mirroring the `hf_firehose` registration at `:131`. `--once` runs both tiers once.

4. **One routing tweak** (`agents/route_news_clusters.py`) — the caller that builds `active_thesis_tickers` for `route_cluster` (`src/news/routing.py:101`) also unions in **the current Tier-1 trending symbols** (`effective_rank` ≤ `TIER1_MAX`, so yesterday's residue is included). Effect: a trending ticker gets the same anti-discard treatment active-thesis tickers already get (`routing.py:112-117`), so a *minor* off-wire follow-on on a hot name is admitted **because it continues a tracked thread**, not dropped for thin standalone materiality. This is the only change to existing pipeline logic, and it's additive (a larger set passed to an existing parameter).

5. **DB: one new table** (`db/schema.py`):
   ```python
   "ticker_trends": [
       ("snapshot_date",   "TEXT NOT NULL"),     # 'YYYY-MM-DD'
       ("source",          "TEXT NOT NULL"),     # '1ms_stocks'
       ("symbol",          "TEXT NOT NULL"),     # resolved canonical registry symbol (UPPER)
       ("raw_symbol",      "TEXT NOT NULL"),     # as scraped, pre-resolution
       ("rank",            "INTEGER NOT NULL"),
       ("mentions_24h",    "INTEGER"),
       ("mentions_delta",  "INTEGER"),           # velocity — the leading signal
       ("upvotes",         "INTEGER"),
       ("rank_trend",      "INTEGER"),           # +N up / -N down / 0 / NULL=new
       ("in_registry",     "INTEGER NOT NULL DEFAULT 0"),
       ("created_at",      "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
       ("PRIMARY KEY (snapshot_date, source, symbol)", ""),
   ],
   ```
   One row per (day, source, symbol). This is the input to tiering, the observability record, and the data source for the future homepage surface — three uses, one table.

That is the complete build. No other new modules, tables, or columns.

---

## Reused unchanged (the point of the design)

| Concern | Reused machinery |
|---|---|
| Write a news row | `insert_entry` / `FirehoseEntry` (`agents/firehose.py`) |
| URL dedup | `insert_entry`'s `source_url` check |
| Materiality / spam / event-class | `src/news/firehose_gate.py` run on the scraped body — **no new materiality model** |
| Symbol → registry resolution | the `instruments` loader behind `src/news/ticker_candidates.py` |
| Event clustering (one story = one event) | `src/news/cluster.py` |
| Admit / promote decision | `src/news/routing.py` (one additive tweak) |
| Story synthesis + citation verify | `src/news/synthesis.py`, `src/news/verifier.py` |
| Quality judge | `agents/judge_stories.py` |
| Thesis proposals from the new stories | `discover_story_theses` (`docs/design-thesis-creation.md`) |
| Scoring (freshness/tailwind) | `agents/score_theses.py` |
| Metrics stream | `src/pipeline_metrics/` (`trending_run` event) |

---

## Deliberately cut (over-engineering avoided)

These were considered and **excluded from v1** on purpose. Each is a clean later add if metrics demand it — none is a precondition.

- **No separate watchlist table or arc-liveness decay engine.** The trending list *is* the watchlist; the two-tier rank cadence *is* the decay. A name that falls off the list stops being scraped — no liveness scoring needed.
- **No new story/synth/thesis code.** The lane stops at `news` rows. Everything after is reuse.
- **No "story arc" / cluster-of-clusters object.** A developing thread already renders via shared `theme_tag` + the `thesis_story_links` timeline. Don't build a new grouping primitive.
- **No social-momentum score stored on theses.** Tempting as a scoring input, but it couples the lane to the scorer. Defer; revisit only if scoring eval asks for it.
- **No multi-source social aggregation** (e.g. StockTwits/X) in v1. **1ms only — no third-party API fallback.** The `source` column keeps adding a source later a data change, not a redesign, but v1 ships against 1ms alone; a 1ms outage is surfaced as a critical metric (see *Failure handling*), not silently papered over by another provider.
- **No thesis-driven retrieval** (scraping tickers of live theses that aren't trending). v1 is trending-list-driven only. The routing tweak already lets *active-thesis* names benefit from any trending overlap; full thesis-driven retrieval is a Phase-3 idea, not now.
- **No per-user trending personalization.** Global only.

---

## Cost & observability

- **Bounded by construction.** Daily: ≤20 symbols × (1 search + ~3 scrapes). Every 3 days: ≤40 more. Web-search/scrape (Exa via Mesh) is the only marginal cost; cap hits-per-symbol and total scrapes per run via env knobs (`HF_TRENDING_TIER1_MAX`, `HF_TRENDING_MAX_HITS_PER_SYMBOL`).
- **Spend concentrates where there's a live story.** Unlike blanket feed polling, we only retrieve for names the crowd is actually watching.
- **`trending_run` metric** (mirrors `firehose_run`): symbols fetched, scraped, news rows inserted, gate-dropped, registry-unknown skipped. Lands in `logs/hf-pipeline-metrics.jsonl`; surfaced by `scripts/hf_health.py`. Watch insert→story conversion to confirm the lane yields real stories, not noise.

---

## Failure handling — fetch/parse breakage is a *critical* metric

Because v1 has **no fallback source**, a 1ms outage or a page-structure change (renamed/removed table, layout swap, Cloudflare challenge, non-200) would silently starve the lane. That must surface loudly on the frontend `/metrics` page, which renders `critical`-severity findings from `scripts/hf_health.py`. The existing `firehose_run` ok/error pattern is the template.

1. **Emit on every run.** `run_trending` always writes a `trending_run` event via `append_metric` (`src/pipeline_metrics/__init__.py`) carrying `ok: bool`, `error: str | None`, `tier`, `phase` (`"fetch"` | `"parse"` | `"scrape"` | `"insert"`), and the success counters. `fetch_ranking` raises a typed `TrendingFetchError` on non-200, missing table, or a row count below a sanity floor (e.g. `< 10` — the page reliably returns 100); `run_trending` catches it, sets `ok=false`, `phase="fetch"`/`"parse"`, `error=repr(exc)`, and returns without upserting or scraping (no partial/poisoned snapshot).

2. **Promote to a critical finding.** Add to `hf_health.py::evaluate()` a check mirroring the firehose-failure logic (it already computes `consecutive_failures(events, "firehose_run")`):
   - `latest trending_run.ok == false` **or** `consecutive_failures(events, "trending_run") >= 1` → `add_finding(..., "critical", "trending.fetch_failed", "Trending ranking fetch/parse failed — lane is starved", value=error)`.
   - latest `trending_run` older than ~2× the tier-1 interval (no run at all) → `critical` `trending.stale`.
   This makes both *broke* and *silently stopped* visible. The finding flows through `GET /api/internal/metrics/health` to the `/metrics` page with no frontend change — it already renders the `Finding` list by severity.

3. **Parser canary (cheap regression guard).** The spike shows the table has a stable 7-column header (`排名 / 代码 / …`). `fetch_ranking` asserts the header matches before parsing rows; a header drift trips `TrendingFetchError(phase="parse")` rather than silently mis-mapping columns. This is the single most likely breakage and the cheapest to detect.

No paging/alerting wiring is added here — the requirement is "surface on the metrics page," which the `critical` finding satisfies. A notifier is a separate concern (`docs/plan-production-alerting.md`).

---

## Phase 2 (future — table already supports it)

The user's roadmap item: surface the trending list on the homepage so users see the *real* hot names, not just the generic Mag7/SPY/QQQ/GLD set.

- **Read API:** `GET /api/trending?source=stocks` → latest `snapshot_date` rows, joined to `instruments` for display name + asset class, optionally annotated with "has a story today" (join `entity_tickers`/`story`). Pure read over `ticker_trends`.
- **Frontend:** a trending-tickers module on the home feed. Per CLAUDE.md cross-repo rule, add the route to the OpenAPI schema and re-run `bun run gen:types`.

### Discover-thesis ranking: blend in trending overlap

The homepage **Discover** section (`brief.theses[]`, built by `build_discover`, `api.py:210`) surfaces unowned active theses ranked by how many of **today's daily-brief stories** support them. The brief is press-wire-driven, so a socially-trending name's thesis (created by this lane → `discover_story_theses`) can exist yet never surface. Make Discover relevant to what's actually trending with two minimal edits to the existing function — **no new generation, endpoint, table, or frontend**:

- **"Generate" is already free.** The lane → story → `discover_story_theses` path already mints theses tagged with trending tickers. Discover only needs to *read and rank* them; it does not generate.
- **Broaden the candidate set.** In `build_discover`'s SQL, also admit unowned active theses whose tickers hit today's Tier-1 trending snapshot — one extra join `theses → entity_tickers (entity_type='thesis') → ticker_trends` on the latest `snapshot_date` with `effective_rank ≤ TIER1_MAX`. This is what lets a trending name's thesis appear even when it's absent from the press-wire brief.
- **Blend trending into the sort — one additive, tunable term:**
  ```
  discover_score = support_count + TREND_WEIGHT * trending_hit
  ORDER BY discover_score DESC, top_conf DESC, t.id ASC   LIMIT 3
  ```
  `trending_hit` = 1 when the thesis covers ≥1 Tier-1 trending ticker (boolean — simplest). `TREND_WEIGHT` (default ~2) lets a trending thesis rank as if it had ~2 extra brief-supports — enough to surface, not enough to bury a genuinely dominant narrative thesis. Additive, so nothing is hard-pinned.

**Deliberately cut here too:** no rank/velocity weighting in v1 (`trending_hit` is boolean; weighting by `TIER1_MAX − effective_rank` so #1 beats #20 is a one-line later tweak); no persisted trending score on `theses` (the blend is computed in the query); no separate "trending theses" surface (reuse Discover). The frontend is unchanged — it already renders `brief.theses[]`.

Out of scope for v1; listed so the `ticker_trends` shape is built right the first time.

---

## Open decisions (resolve before build)

1. **1ms fetch mechanism — RESOLVED by the spike.** Static server-rendered HTML; plain HTTP GET + table parse (no browser, no JS render). **No third-party API fallback** — a 1ms outage surfaces as the `trending.fetch_failed` critical metric instead. Remaining unknown: poll-time rate limits / Cloudflare behaviour under a daily cron — benign at this cadence, but the `TrendingFetchError` path covers a challenge response if one appears.
2. **Tier thresholds** — `TIER1_MAX=20`, `TIER2_MAX=60` are starting defaults; tune from the first week's insert→story conversion, not by guesswork.
3. **Exa recency window** — default last 48h to bias toward genuinely new developments; widen only if Tier-2 slow-burn names come back empty.
4. **Source authority** — Exa returns secondary/tertiary sources vs. the firehose's primary wires. v1 relies on the existing verifier (verbatim quotes + ticker regex) for integrity; if spot-checks show weak sourcing, add a domain allowlist or a per-`exa_web` materiality floor (data/config, not new architecture).

---

## Phased delivery

1. **Spike the source — DONE (2026-06-02).** Confirmed server-rendered HTML, single 100-row table, fixed 7-column header, HTTP/2 200, no auth/JS. Fetch mechanism resolved (Open decision 1). Remaining build work in this phase: harden `fetch_ranking` with the `TrendingFetchError` + header-canary path.
2. **Ingest lane** — `ticker_trends` table + `upsert_trends` + `run_trending` (fetch→upsert→scrape→gate→`insert_entry`) with the `trending_run` ok/error metric. Run `--tier 1 --dry-run`, then live; confirm `news` rows land and cluster.
3. **Wire the scheduler + routing tweak + health finding** — two interval jobs; union Tier-1 trending symbols into the routing anti-discard set; add the `trending.fetch_failed` / `trending.stale` critical findings to `hf_health.py`. Watch `trending_run` and route-funnel metrics; confirm stories emerge for previously-missed names (SPCE/OPEN/ASTS as the regression check), and force a fetch failure once to verify the `/metrics` page shows the critical finding.
4. **Phase 2 (later)** — `/api/trending` + homepage module + `gen:types`.

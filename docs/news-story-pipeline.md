# News → Story Pipeline

Heurist Finance ingests a high-volume news firehose and synthesizes a small
fraction of it into citation-backed **stories**. Stories — not raw news items —
are what users see on the home feed, and what `discover_thesis` reads to
match/create thesis primitives.

## Vocabulary

- **News (item)** — raw input. One row per fetched URL. Single source, no
  synthesis. Lives in `news`.
- **Cluster** — event identity. One-to-many news items, plus tickers,
  sectors, regions, materiality. Lives in `news_cluster` (members in
  `news_cluster_member`).
- **Story** — synthesized, citation-backed writeup of one cluster. Lives in
  `story` with `kind='story'` + `global/stories/{id}.md`.
- **Social topic** — verified X discussion about one ticker. Lives in the same
  `story` table with `kind='x'`, no cluster, `heat`, and `social_json`.

## Pipeline stages

1. **Ingest** — `agents/firehose.py` pulls feeds defined in
   `src/news/publishers.py` (wires, macro/regulatory, Tier1 majors, trade
   press). Per-doc gate (`src/news/firehose_gate.py`) drops zero-materiality
   noise. Source-derived sector/region tags applied at insert.
2. **Cluster** — `src/news/cluster.py` runs two-pass online clustering:
   - Pass 1 (every item, free): headline-hash bypass + cheap-feature attach
     (ticker / sector / region / event-class / headline-token overlap).
   - Pass 2 (only on promotion candidates, embedding paid): brute-force
     cosine vs. `news_cluster.centroid_json`. The firehose leaves this
     dormant (embed-at-promotion-time policy); the **trending lane activates
     it at insert** (`allow_embedding=True`) for its hot symbols so
     demand-retrieved coverage clusters semantically — see *Demand-driven
     ingest* below.
3. **Route** — `src/news/routing.py` decides discard / firehose-store /
   sharp-promote per cluster using rules R0–R8 (Tier1 + materiality,
   independent-publisher count, active-thesis ticker/sector/region overlap,
   sharp event class). `agents/route_news_clusters.py` runs a **promote-first**
   pass on a filtered candidate pool (see below).
4. **Body enrich** — `src/news/body_enrichment.py` Firecrawl-scrapes thin
   member URLs before synthesis. With `HF_ENRICH_ALL_PROMOTES=1` (default),
   admitted promotes scrape up to two thin members even when another member
   already has a long RSS body.
5. **Synthesize** — `src/news/synthesis.py::synthesize_cluster` runs one
   Gemini Flash structured-output call against up to 3 selected member
   bodies and returns `{headline, what_changed, overview[].source_doc_ids,
   claims, quotes, market_relevance}`.
6. **Verify** — `src/news/verifier.py` checks deterministic invariants:
   every overview bullet carries non-empty `source_doc_ids`, every cited
   `news_id` is a cluster member, every quote
   verbatim-matches a cited body, every ticker matches the Yahoo-form
   regex. Failure rejects the synth into `story_synth_rejected`.
7. **Persist** — `src/news/persist.py::write_cluster_story` writes the
   story row, the markdown file at `global/stories/{id}.md`, and tickers
   via `entity_tickers`. Yahoo-shaped unknowns are admitted as provisional
   (`pending_instruments`). One story per cluster (`story.cluster_id` UNIQUE).
8. **Discover thesis** — `src/thesis/discover.py::discover_thesis` runs
   post-synth against finished stories with `theme_tag != 'other'`. Six
   layered gates (LLM+retrieval, post-LLM cosine, post-creation re-check,
   registry grounding, structural promotion, backfill sanity). See
   `docs/design-thesis-creation.md`.

## Demand-driven ingest: the trending-ticker lane

The firehose (stage 1) is **supply-driven** — it polls a fixed feed set and
takes whatever publishes. The trending lane (`agents/trending.py`) is the
**demand-driven** inverse: it starts from the tickers retail is actually talking
about and retrieves news for them. The hot-ticker list IS the watchlist, web
search IS the retriever, and everything downstream is the existing spine.

The lane is always on (it only adds `news` rows; every failure path degrades to
"fewer stories", never a user-visible error — see *Failure isolation* below).
Per run (Tier 1 daily, Tier 2 every ~3 days; runs in `pm2 hf-pipeline`):

1. **Rank** — fetch the 1ms social leaderboard, upsert a dated snapshot into
   `ticker_trends`. There is no fallback source, so a fetch/parse failure is a
   **critical** health finding (`trending.fetch_failed`) surfaced on `/metrics`.
2. **Tier-select** — `effective_rank = MIN(rank)` across the two most-recent
   snapshots (one-day residue). Tier 1 = rank ≤ `HF_TRENDING_TIER1_MAX` (20),
   Tier 2 = next band up to `…_TIER2_MAX` (60). Registry-known symbols only.
3. **Retrieve** — per symbol, one `exa_web_search` for source URLs, then **one
   `exa_scrape_url` per URL**. Single-doc scraping is deliberate: the digest is
   then unambiguously that article's summary, so each entry maps to its real
   source URL (which dedup/clustering key on) instead of depending on a fragile
   multi-document format the model emits unreliably.
4. **Gate + insert** — each article runs the *existing* firehose gate
   (`tag_text` + `score_materiality`) and `insert_entry`, with the trending
   symbol force-added to the ticker slate. The social rank is the retrieval
   trigger; the firehose gate is still the admission filter.

Two integration points let the demand signal survive into stories:

- **Embedding-attach on insert.** Trending inserts pass `allow_embedding=True`
  and the run's hot symbols as `promote_tickers`, so same-event articles from
  different outlets attach semantically (Pass 2) instead of fragmenting into
  singletons. This is what lets per-outlet corroboration accumulate — three
  independent write-ups of one catalyst become one cluster with
  `independent_pub_count = 3`, promotable on merit via R0c/R2/R3. A trending
  article also attaches to a matching firehose cluster when one already exists,
  merging demand- and supply-side coverage of the same event.
- **Routing union.** Tier-1 trending symbols are unioned into
  `active_thesis_tickers` in `route_news_clusters`, so a hot-ticker cluster
  clears the zero-materiality discard gate and qualifies for R3 the same way an
  owned-thesis ticker does.

The lane owns nothing downstream: it produces `news` rows and lets cluster →
route → synth → verify → discover decide their fate. Momentum/price-action
recaps and single-source previews correctly settle in `firehose_store`;
corroborated real events (earnings beats, M&A, executive catalysts) promote to
stories.

**Failure isolation.** The lane is best-effort and additive — if the 1ms fetch
or the Exa scrape breaks, the only user-visible effect is fewer stories that
cycle. It can't surface an error or block anything else: `run_trending` never
raises (it returns an `ok=false` metric body), `PipelineService.run_trending`
catches anything anyway, and the boot sequence runs trending in its own
`to_thread` step *before* firehose + pipeline, so a trending stall/failure
doesn't stop supply-side ingest. The read surfaces fail soft too — `latest_trends`
returns `[]` (so `GET /api/trending` is empty, not a 500) and `tier1_symbols`
returns an empty set, which collapses the Discover blend back to pure
`support_count` and makes the routing union a no-op. A fetch/parse failure is
still recorded as a critical `trending.fetch_failed` finding on `/metrics` so the
breakage is visible to operators without ever reaching users.

Read surfaces: `GET /api/trending` returns the latest snapshot (rank, effective
rank, tier, per symbol); the homepage Discover ranking blends Tier-1 trending
overlap into its score (`api.py::build_discover`).

**Manual:**

```bash
uv run python -m agents.trending --tier 1            # one Tier-1 run
uv run python -m agents.trending --tier 1 --dry-run  # rank + tier-select only, no retrieval
```

## Promote-first routing (`route_news_clusters`)

Three decoupled knobs (scheduler defaults in parentheses):

| Knob | CLI flag | Default | Role |
|------|----------|---------|------|
| Route eval limit | `--route-eval-limit` / `--limit` | 1200 | Max clusters to `route_cluster()` per run (cheap) |
| Synth budget | `--synth-budget` / `--top` | 40 | Max clusters to synthesize after admit (expensive) |
| Synth workers | `--synth-workers` | 6 | Parallel `write_cluster_story` calls when `--write` |

**Per-run flow:**

1. **SQL candidate pool** — open/firehose/ambiguous clusters in the promotion
   age window, synth-reject cooldown applied, promote-signal pre-filter
   (tier-1, corroboration, or high-mat non–PR-wire). Ordered by
   `has_tier1_primary`, `independent_pub_count`, `max_materiality`, `last_seen_at`
   (not materiality alone).
2. **Route all candidates** — partition discard / firehose / sharp_promote.
3. **Promote-first admit** — sort `sharp_promote` by rule rank (R0c > R0b > …),
   apply diversity quota (≤60% per sector×region after 20 accepts), cap at
   synth budget. Overflow promotes → `firehose` (retry next cycle).
4. **Parallel synth** — admitted clusters run `write_cluster_story` in a thread
   pool; match + thesis discovery run sequentially per successful story.

Summary line on stderr:

```text
[route] evaluated=… promotes=… admitted=… synth_ok=… synth_rejected=…
```

**Scheduler** (`agents/pipeline_scheduler.py`): `--top-stories 40`,
`--route-eval-limit 1200`, `--synth-workers 6`; judge budget
`max(top_stories * 2, 30)`.

**Manual / backfill:**

```bash
uv run python -m agents.route_news_clusters --write --synth-budget 40 --route-eval-limit 1200
uv run python scripts/backfill_stories_recent.py --hours 12
```

Disable enrich-all: `HF_ENRICH_ALL_PROMOTES=0` (legacy thin-only cluster gate).

## Quality

- **Auto-judge.** `agents/judge_stories.py` labels stories `good | unclear
  | no_value` via `story_quality_label`. `unclear`/`no_value` are hidden
  from user-facing endpoints.
- **Read-only review packet.** `scripts/export_story_review_packet.py`
  emits a CSV digest of recent stories for team spot-check reading. The
  team does not write labels.
- **Weekly digest.** `scripts/story_quality_weekly.py` reports good-rate,
  rejected-cluster patterns, cost per good story, and a sector × region
  coverage matrix.
- **Verifier gold.** `db/story_gold/` + `scripts/eval_story_gold.py` lock
  in synth shapes. Wired into CI via `.github/workflows/story-gold.yml`
  on PRs touching the pipeline.
- **Audit.** `scripts/audit_news_rearchitecture.py` is the single
  pre-merge gate: zero structural violations, zero unclustered firehose,
  zero LLM calls on raw news rows, ≥1 verifier-gold fixture.

## Read API

- `GET /api/home` — returns `{ news: FeedItem[], ... }`. The `news` key is
  preserved for frontend stability, but the composer is now a ranked mixed-kind
  feed: recent `kind='story'` rows plus non-expired `kind='x'` social topics.
  Every item carries `kind`; social items carry `heat`, `bullAngle`,
  `bearAngle`, and verified `tweets[]`, and omit `thumbnail`.
- `GET /api/home?explain=1` — adds per-item scoring diagnostics:
  `{kind, freshness, base, trend, affinity, score, demotions}`.
- `GET /api/news/{id}` — returns story detail for `kind='story'` ids and the
  social payload for `kind='x'` ids. Legacy `news_*` IDs return 404.
- `GET /api/stories` — additive listing.
- `GET /api/trending` — latest trending-ticker snapshot (rank, effective rank,
  tier, mentions); `?tier=1` filters. Empty until the lane writes its first
  snapshot.

`api.py::build_feed` is the single read-side composer. It scores
`(base + trend + affinity) * freshness`, then applies composition rules:
no more than two social topics in any five consecutive slots and no adjacent
items with the same lead ticker.

## Metrics (`logs/hf-pipeline-metrics.jsonl`)

All pipeline observability goes through the shared append-only JSONL stream
(`src/pipeline_metrics/`). Human scheduler logs stay in `logs/hf-scheduler.log`.

| Event | Emitter | Purpose |
|-------|---------|---------|
| `firehose_run` | `agents/firehose.py`, scheduler | Per-poll ingest counts + `inserts_by_publisher_top` |
| `trending_run` | `agents/trending.py`, scheduler | Per-tier demand-lane counts (`symbols_due`, `inserted`, `scrape_errors`); `ok=false`/`phase` is the no-fallback fetch-failure signal |
| `social_run` | `agents/social_topics.py`, scheduler | Grok X Search topic counts (`topics_admitted`, `topics_refreshed`, verifier/heat/cap/dup drops, cost); `ok=false`/`phase` is the no-fallback social-source signal |
| `route_funnel` | `agents/route_news_clusters.py` | Cluster routing funnel, publisher mix (`sources`), DB snapshot (`snapshot`) |
| `run_start` / `step` / `step_metrics` / `run_finish` | `agents/pipeline_scheduler.py` | Full 3h pipeline lifecycle |
| `run_start` … | scheduler | Correlates route step via shared `run_id` |

**Health checks:** `uv run python scripts/hf_health.py` (DB + JSONL findings).
Funnel-only view: `uv run python scripts/hf_health.py --pipeline-funnel`.

**HTTP (dev):** set `HF_INTERNAL_METRICS_ENABLED=1` on the workbench, then
`GET /api/internal/metrics/health` and `GET /api/internal/metrics/pipeline-events`.
The frontend `/metrics` page proxies these when `NEXT_PUBLIC_HF_METRICS_ENABLED=1`.

## Schema

- Tables added: `news_cluster`, `news_cluster_member`, `story`,
  `story_quality_label`, `story_synth_rejected`, `ticker_trends`
  (demand-lane snapshots).
- Columns added: `news.cluster_id`, `news.headline_hash`, `news.embedding`,
  `news.event_class`, `news.regions_json`; `instruments.region`.
- Authoritative definitions live in `db/schema.py`.

## Open quality questions

Tracked in `TODO.md` — Phase F (story image gallery), candidate-stage
embedding finish-or-delete, daily brief rebuild, auto-thesis quality
watch, non-large-cap personalization gate. Quote-verbatim repair pass
(after verifier failure) is the next planned synth-yield improvement.

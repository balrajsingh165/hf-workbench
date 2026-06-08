# Reference: Thesis ↔ Story Matching System

Durable description of the shipped matching pipeline. For **why** this shape was chosen, see `docs/spike-thesis-news-matching.md`. For the scoring layer that consumes the output, see `docs/plan-scoring-system.md`.

---

## What it does

Answers this question for any (thesis, story) pair:

> Does this story **support**, **stress**, or is it **unrelated to** this thesis? With what confidence? Which named invalidation condition does it hit?

Two mirrored entry points drive it from either side:

| Direction | Entry point | Triggers |
|---|---|---|
| **Story → thesis** | `agents/match_thesis_for_story.py` | On story arrival / ingest. Given one story, find the theses it moves. |
| **Thesis → story** | `agents/match_story_for_thesis.py` | On thesis creation, cold→hot promotion, ad-hoc reindex. Given one thesis, find the stories that matter. |

Both directions share `src/thesis/story_judge.py` — one place for the prompt, schema, and verdict normalization.

## What makes this hard and important

1. **Theses are abstract, stories are concrete.** Thesis: "Apple Silicon investment deepens, bullish TSMC suppliers." Story: "TSMC Q1 beat on AI chip demand." A human reads support; ticker-overlap or keyword-match alone misses it.
2. **Stressing is often implicit.** A thesis that Fed will cut in June is stressed by a story that inflation surprised upward — "Fed" may never appear. Invalidation has to be matched semantically.
3. **Tickers are noisy both ways.** Ticker overlap is a weak signal on its own. A `^GSPC`-tagged article isn't about every S&P thesis; a TSMC thesis can be stressed by a Fed surprise with no shared ticker.
4. **Confidence calibration matters.** We gate stress flips on a threshold. If the model is systematically over- or under-confident, the threshold is fiction.
5. **Asymmetric update rates.** Theses change slowly, stories change fast. The architecture should reflect this: index theses once (the slow side), query with each incoming story (the fast side). Pay more to preprocess theses; keep the per-story path cheap and streaming-friendly.
6. **New theses need backfill.** When a thesis is created, it has no match history. The pipeline must backfill against recent stories (7–14 days) so the thesis starts with immediate context, not a blank score.

## Design principle: conceptual complexity is the cost to avoid

Code-level complexity (wiring an embedding call, storing chunk vectors) is nearly free. What's expensive is **conceptual / system-level complexity** — new data types, new preprocessing stages that have their own failure modes, new drift and staleness problems, new things a reviewer has to hold in their head to understand why a match happened. We optimize the architecture to minimize that, not lines of code.

This rules out anything that adds a new semantic layer (decomposition, clustering, sub-claims) until we've proven the simple hybrid isn't enough.


---

## Pipeline shape

```
         ┌────────────────────────────────────────────┐
         │  Dense retrieval                           │
         │  Gemini Embedding 2, 1536d                 │
         │  RETRIEVAL_DOCUMENT on build,              │
         │  RETRIEVAL_QUERY on search                 │
         └──────────────────────┬─────────────────────┘
                                │  top candidates
                                ▼
         ┌────────────────────────────────────────────┐
         │  Dedup to one best match per parent        │
         │  (best chunk per thesis, or best story per │
         │   thesis chunk in the mirror direction)    │
         └──────────────────────┬─────────────────────┘
                                │  ≤ M candidates
                                ▼
         ┌────────────────────────────────────────────┐
         │  LLM judge (Flash, thinking_level=medium)  │
         │  Per-pair classification with JSON schema  │
         │  supports | stresses | unrelated           │
         │  + confidence + matched_invalidation       │
         │  + rationale                               │
         └──────────────────────┬─────────────────────┘
                                │
                                ▼
         ┌────────────────────────────────────────────┐
         │  Persist supports/stresses rows to         │
         │  thesis_story_links                        │
         │  (unrelated rows dropped)                  │
         └────────────────────────────────────────────┘
```

---

## Two dense indexes

One embedding per side; both use the same Gemini model + dimensionality so query/document vectors are directly comparable.

### `thesis_match_chunks`
- Built from `global/theses/*.md` at thesis create/edit.
- One vector per **semantic chunk**:
  - `statement` — the thesis sentence.
  - `invalidation_<n>` — one vector per named invalidation condition.
- Columns: `thesis_id`, `chunk_key`, `chunk_kind`, `chunk_text`, `embedding_json`, `updated_at`.
- Invalidations are first-class retrieval targets — that's what lets a "Fed inflation surprise" article stress a "TSMC bullish" thesis via its "TSMC loses pricing power" invalidation.

### `story_match_chunks`
- One vector per story (title + overview bullets, via `story.docs.StoryDocument.query_text`).
- Columns: `story_id`, `chunk_text`, `embedding_json`, `updated_at`.
- Written automatically on story promotion; non-fatal on embedding failure.

Both directions walk chunks on one side and score against the dense index on the other, then dedup to one best match per parent before the judge sees anything.

---

## Judge contract

The judge returns, per pair:

```json
{
  "relation": "supports" | "stresses" | "unrelated",
  "confidence": 0.0-1.0,
  "matched_invalidation": "<exact text of a named invalidation> | null",
  "rationale": "one sentence, no hedging"
}
```

Enforced via Gemini's `response_json_schema`. The prompt carries two policies the schema can't express:

- **Confidence calibration scale.** `0.85–0.95` is reserved for stories that directly name the thesis's own driver/mechanism; an indirect or second-order chain on a shared theme (e.g. "rates are moving" without naming the thesis's specific driver) is capped at `0.80`. This keeps confidence meaningful for the downstream stress-flip threshold.
- **Literal invalidation trigger.** `matched_invalidation` is populated only when the story's facts literally satisfy a named condition's stated direction, magnitude, and threshold — being on the same topic is not enough (a 0.3% dollar move does not trigger a "dollar drops sharply" clause).

Post-processing:

- `matched_invalidation` is matched leniently against the thesis's named invalidations (whitespace / trailing punctuation / case-insensitive) and returned as the **canonical** bullet text, not the model's echo.
- `unrelated` rows are filtered out before persistence — the table only holds `supports` / `stresses`.
- `_coerce_confidence` rejects `bool` explicitly so `True`/`False` don't silently become `1.0`/`0.0`.
- `json.JSONDecodeError` on a single pair logs to stderr and continues — one bad response doesn't kill the batch.
- `_safe_judge` (thesis→story direction) wraps the whole call in try/except. Partial results beat a blank timeline.

### Why three classes, not five

The judge's enum is deliberately coarse. Richer UX labels (`CONFIRMED`, `STRENGTHENED`, `TENSION`, `STRESSED`, `supporting`, `weakened`) and the stress flip thresholds live **downstream** in `src/scoring_config.py` + `src/thesis_news_labels.py`, not in the model output. Two reasons:

1. **Calibration.** More categories means less mass per bucket. A 5-point scale makes it harder — not easier — to tell whether the threshold is trustworthy.
2. **Cost.** `thesis_story_links` rows are expensive (one Gemini judge call each). Pulling UX copy and threshold policy out of the judge means we can retune labels or shift stress floors without re-running matching.

The scoring layer uses a **two-tier stress ladder** (`STRESSED` at `confidence >= STRESS_FLIP_CONF`, `TENSION` in the watch band below it — both requiring `matched_invalidation`). See `docs/plan-scoring-system.md` for the full derivation. A drifted confidence floor at most mis-labels a row between `STRESSED` and `TENSION`; it cannot invent a stress without a named invalidation.

---

## Read model — `thesis_story_links`

This is the single durable output of the pipeline. Scoring and digest both read from here.

| Column | Type | Notes |
|---|---|---|
| `thesis_id` | TEXT | PK part. FK → `theses.id`. |
| `story_id` | TEXT | PK part. FK → `story.id`. |
| `relation` | TEXT | `supports` or `stresses`. `unrelated` never persisted. |
| `confidence` | REAL | 0.0–1.0, from the judge. |
| `matched_invalidation` | TEXT | Canonical bullet text, or NULL. Required for stress flips. |
| `rationale` | TEXT | One sentence from the judge. |
| `retrieval_score` | REAL | Cosine similarity of the winning chunk. Diagnostic. |
| `best_chunk_key` | TEXT | Which thesis chunk won retrieval (e.g. `statement`, `invalidation_2`). Diagnostic. |
| `source` | TEXT | `ingest` (written by story→thesis path) or `backfill` (thesis→story path). |
| `updated_at` | TEXT | Refreshed on conflict. Judgment freshness. |

PK: `(thesis_id, story_id)`.

### Write semantics

- **Upsert on conflict.** Refreshes judgment fields and `updated_at`. Freshness comes from `story.created_at`, not link write time.
- **Confidence-floored prune on thesis→story rerun.** `match_story_for_thesis` calls `prune_backfill_links_for_thesis(thesis_id, keep_above=BACKFILL_KEEP_CONF)` (currently `0.70`) before upserting the new set. Above-floor backfill rows survive across runs even when their stories age out of the retrieval window — they're the durable evidence trail. Below-floor rows get pruned and re-judged if the story still surfaces as a candidate. `source='ingest'` rows are untouched regardless. Candidates with an existing above-floor link are also skipped, so stable verdicts aren't repeatedly re-judged.
- **Provenance via `source`.** Tells you whether a row came from real-time ingest or a catch-up pass. Useful when debugging why a row is / isn't there.

---

## CLI entry points

### Story → thesis

```
uv run python -m agents.match_thesis_for_story story_020
```

- Loads one story.
- Queries all thesis chunks in the dense index, dedupes to best chunk per thesis, keeps top candidates.
- Judges each pair sequentially (parallelization deferred — see TODO).
- Emits `MatchThesisForStoryOutput` JSON to stdout: `{story_id, matches: [{thesis_id, relation, confidence, matched_invalidation, rationale}, ...]}`.
- Writes accepted rows to `thesis_story_links` with `source='ingest'` unless `--dry-run` is set.

### Thesis → story

```
uv run python -m agents.match_story_for_thesis --thesis thesis_003 [--window 14] [--dry-run]
```

- Walks thesis chunks as queries against `story_match_chunks`.
- Optional `--window N` → SQL-level `story.created_at >= today - N days` filter. `--window 0` means all stories.
- Parallel judge (4 workers default, `--max-workers` to override).
- Persists rows with `source='backfill'`. Cleans stale backfill rows for this thesis first.
- Prints a JSON summary of written links.

---

## Chunk-win logging

Every judged candidate (including `unrelated`) emits one line to stderr:

```
chunk-win: story=story_020 thesis=thesis_003 chunk=invalidation_1 score=0.71 relation=supports conf=0.82
```

This is the single most useful debugging signal in the pipeline. When an expected match doesn't happen, it tells you whether the miss was retrieval (candidate never reached the judge) or reasoning (judge saw it and called it `unrelated`).

---

## Invariants (if you change the pipeline, keep these)

1. **Invalidations are retrieval targets**, not just prompt text. Embedding them separately is why stress detection works without shared tickers.
2. **`unrelated` is a valid abstention.** Never coerce a weak signal into `supports`. Silence beats noise.
3. **`matched_invalidation` is canonical text, not the judge's echo.** Downstream stress-flip logic string-compares against the thesis file.
4. **Story time owns Freshness.** Freshness math depends on `story.created_at`.
5. **Backfill cleanup is scoped to `source='backfill'`.** Ingest rows carry real-time provenance and must survive reindex.
6. **One shared judge.** Both directions go through `src/thesis/story_judge.py::judge_pair`. Prompt tuning happens in one place.

---

## Related reading

- `docs/spike-thesis-news-matching.md` — rationale, scale/cost model, open questions, rejected alternatives.
- `docs/ref/matching-eval-set.md` — 60 hand-labeled (thesis, story) pairs. Reference for ad-hoc spot checks; no automated eval runner (deprioritized).
- `docs/ref/news-dense-index-review.md` — historical notes on the pre-story news index side.
- `docs/news-story-pipeline.md` — how raw news rows become synthesized stories.
- `docs/sop-add-new-thesis.md` — how a thesis enters the system (insert → chunks → dense index → on creation, run `match_story_for_thesis` to seed the timeline).
- `docs/sop-add-new-user.md` — user bootstrap; matching doesn't touch users directly but scoring does.
- `docs/plan-p0-p2-slice.md` — overall slice context; matching is Day 2 DONE.
- `docs/plan-scoring-system.md` — what reads `thesis_story_links` next.

---

## Rebuild utilities

Full index rebuilds (rare — only after an embedding model change or a bulk seed):

```
uv run python -m agents.build_match_index --kind thesis
uv run python -m agents.build_match_index --kind story
```

Normal ingest updates the indexes incrementally; these scripts are the big-hammer fallback.


## Scale & cost estimates

These are hypothetical projections so PMs and devs can plan around constraints. All numbers assume the recommended story-centric dense-retrieval pipeline.

### Thesis population model

Not all theses are equal. At 1,000 users × ~5 theses each = **5,000 total theses**, but most users are not checking daily. Matching cost should scale with **engagement, not inventory**.

| Tier | Definition | Estimated count | Matching mode |
|---|---|---|---|
| **Hot** | User active in last 48h, thesis status = `active` or `stressed` | ~500–1,000 | Event-driven: matched on every story arrival in real time. |
| **Cold** | User inactive >48h, thesis still `active` | ~3,000–4,000 | Lazy backfill: matched when the user returns (backfill recent story window). |
| **Resolved** | Status = `resolved` | ~500–1,000 | Not matched. Excluded from index. |

**How cold→hot promotion works:** When a user opens the app or requests a digest, promote all their theses to hot, run a backfill against the last N days of stories (since their last visit), then keep them hot for 48h. This is invisible to the user — the digest looks the same whether the thesis was hot or cold.

**Why this matters:** The retrieval index always contains all 5,000 theses (retrieval is cheap). The expensive part — LLM judge calls — is gated by the hot tier. A story retrieves top-M from the full index, but only sends hot-tier matches to the LLM judge. Cold-tier hits are logged as "pending" and judged on user return.

### Steady-state parameters

| Parameter | Value | Notes |
|---|---|---|
| Total theses | 5,000 | 1,000 users × ~5 each. |
| Hot theses | 500–1,000 | ~10–20% of users active on any given day. |
| Cold theses | 3,000–4,000 | Indexed for retrieval, but LLM-judged lazily. |
| Stories / day | 500 | Promoted from raw feed clusters, each processed once on arrival. |
| Thesis index vectors | ~15,000 | 5,000 statements + ~10,000 invalidation conditions. All tiers indexed. |
| Candidates per story (M) | 15 | After retrieval + fusion, filtered to hot tier for real-time judging. |

### LLM judge calls

| Scenario | Formula | Calls/day | Calls/month |
|---|---|---|---|
| Real-time (hot only) | 500 stories × ~10 hot candidates avg | **5,000–7,500** | **~150,000–225,000** |
| User-return backfill | ~100 returning users × 5 theses × ~30 story backfill × 15% hit rate | **~2,250** | **~67,500** |
| **Total steady state** | | **~7,500–10,000** | **~225,000–300,000** |
| Brute-force (no retrieval, no thresholding) | 500 stories × 5,000 theses | **2,500,000** | **75,000,000** |

**Cost estimate (LLM judge only):**
- Using a mid-tier model (Claude Haiku, GPT-4o-mini), ~800 input + ~100 output tokens per call:
  - ~$0.001–0.003 per call → **$7–30/day** at steady state, **$225–900/month**.
  - With a reasoning model (Sonnet, GPT-4o): ~$0.01–0.02 per call → **$75–200/day**. Too expensive for matching; reserve for digest generation only.
- Brute-force at mid-tier: **$2,500–7,500/day**. This is why both retrieval and tiering are non-negotiable.

### Embedding calls

| Operation | Volume | Frequency |
|---|---|---|
| Thesis create/edit | ~1–4 vectors (statement + invalidations) | Per event, rare (~10–20/day) |
| Story promotion | 1 vector per story | 500/day |
| **Total embeddings/day** | ~520 | Negligible cost (~$0.01/day with OpenAI ada-3) |

Note: the thesis index holds ~15,000 vectors, but these are written once at thesis creation and only updated on edit. The index size is not a daily cost — it's a storage/query-time concern.

### Latency budget (per story)

| Step | Target | Notes |
|---|---|---|
| Embed story | <200ms | Single API call |
| Dense retrieval + thesis dedup | <100ms | Local index, ~15,000 vectors. Still fast. |
| Thresholding + candidate filter | <10ms | In-memory |
| LLM judge × ~10 hot candidates | <3s (parallel) | Batched or concurrent calls |
| **Total per story** | **<4s** | Stress flips land within seconds of story arrival |

### User-return backfill latency

| Step | Target | Notes |
|---|---|---|
| Identify story window since last visit | <50ms | DB query on `story.created_at` |
| Retrieve + judge missed matches | 5–15s | Depends on gap length. Run async, show spinner. |
| Score recomputation | <2s | Only for this user's theses |
| **Total** | **<20s** | Acceptable for a "welcome back" load. |

### Scaling cliffs to watch

| Threshold | What breaks | Mitigation |
|---|---|---|
| >10,000 total theses | Dense index at ~30,000 vectors; query latency may creep | Shard index by sector, or switch to approximate NN (HNSW). |
| >2,000 stories/day | LLM judge cost crosses ~$60–90/day even with tiering | Tighten M from 15→10; raise the abstention threshold to cut weak candidates. |
| >10,000 stories/day | Ingest pipeline needs async workers, queue-based processing | Move from synchronous ingest to job queue (e.g. Celery, Bull). |
| >30% daily active rate | Hot tier grows to 1,500+ theses; real-time LLM calls double | Consider tightening hot window to 24h, or batch hot matching hourly instead of per-story. |
| >50,000 total thesis chunks | Full scan over dense vectors becomes noticeable | Move from brute-force cosine to ANN / vector index while preserving the same chunk schema. |

---

## Out of scope

- Multi-story synthesis ("these 3 stories together stress the thesis even though none does alone").
- Cross-thesis clustering ("you have 4 theses that all break on the same signal").
- Crowding / popularity signal inputs (social/forum sentiment). The dimension itself was dropped from the scoring model — see `docs/plan-scoring-system.md`.

---

## Open questions for the teammate

1. ~~Is the three-class schema (supports / stresses / unrelated) the right granularity, or do we want a 5-point scale?~~ **Resolved.** Three classes stay; richer UX granularity (`CONFIRMED` / `STRENGTHENED` / `TENSION` / `STRESSED` / `supporting` / `weakened`) is derived downstream in `src/thesis_news_labels.py` reading thresholds from `src/scoring_config.py`. Keeps the judge's confidence calibration tractable and decouples UX copy from expensive Gemini state. See `docs/plan-scoring-system.md`.
2. Should `matched_invalidation` be required when `relation=stresses`, or optional? Required forces the model to show its work but may reduce recall if it can't map to a named condition. *(Current stance: required for a status flip, optional for the row to persist — a stress without a named invalidation still surfaces as `weakened`, it just won't flip status or escalate to `TENSION`.)*
3. How should we handle a story that both supports and stresses a thesis (e.g., Fed delay helps a bond thesis but hurts a rate-cut-sensitive equity thesis)? Per-pair framing handles it naturally, but worth stating explicitly.

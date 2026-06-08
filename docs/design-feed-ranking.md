# Design: Feed Ranking

**Status:** agreed (2026-06-03), not yet built.
**Scope:** Ordering and filtering of the home feed (`/api/home` `news[]`): a deterministic scoring function over mixed item kinds (pipeline stories + social topics), composition rules, and the facet architecture that future filters (sector, "for me") plug into. The social-topic *ingestion* pipeline (generation, verification, storage, refresh) is owned by [`design-social-ingestion.md`](./design-social-ingestion.md); sector tags by [`design-sectors.md`](./design-sectors.md); feasibility, prompt, and verification were settled in `spikes/grok-social/FINDINGS.md`.

---

## Why

The feed today is one query: `ORDER BY s.created_at DESC LIMIT 200` (`api.py::build_feed_stories`). Reverse-chron makes one claim — *newer = more worth your attention* — and that claim fails twice:

1. **It ignores magnitude.** A single-source trade-press blurb outranks a three-publisher Tier-1 catalyst from two hours earlier.
2. **Social topics break it completely.** Social items are generated in one or two daily batches, so `created_at` measures our cron schedule, not the topic's heat. Under reverse-chron, every batch sinks beneath whatever news published after it.

The honest claim a thesis-app feed should make: **ranked by how much this user should care right now.** That decomposes into four signals, all of which already exist in the system. Filters are predicates over the same four facets, so the sector and "for me" filters fall out of the ranking work instead of becoming a second system.

| Signal | Question | Source (already exists) |
|---|---|---|
| **Freshness** | how alive is it? | item age, decayed at a per-kind rate |
| **Magnitude** | how big is it? | social: `heat` 1–5; story: `independent_pub_count` |
| **Trend** | how hot is the subject? | `ticker_trends` latest snapshot (effective rank) |
| **Affinity** | how mine is it? | `thesis_story_links`, owned-thesis tickers, `user_watchlist`, thesis sectors |

---

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Ranking | **Deterministic score, no ML** | ~250 candidates per request; a legible formula with named knobs beats a learned ranker we can't inspect and have no engagement data to train. |
| Score shape | **`(base + trend + affinity) × freshness`** | Multiplicative decay means nothing ancient rides a boost to the top. Additive relevance means components are independently tunable and nothing zero-collapses. |
| Social batch problem | **Per-kind half-life, not faked timestamps** | Social topics describe "today", not "this minute" — they decay slower (≈32h vs ≈14h), so a morning batch holds the front all trading day and is gone when tomorrow's lands. |
| Item kind | **`kind: "story" \| "x"` on every feed item** | Frontend renders `x` items as a distinct card (X logo, no thumbnail — social topics have no reliable image). Social is labeled, never disguised as news. |
| Social admission & volume | **Owned by [`design-social-ingestion.md`](./design-social-ingestion.md)** — deterministic verifier, ≥3 verified tweets per topic, 10–20 topics/day | Single ownership: the gate and volume knobs live with the pipeline that enforces them; the feed only ever sees admitted items. |
| Pipeline order | **annotate → filter → score → compose → cap** | Filters run before composition so density windows hold on the list the user actually sees. Score is filter-invariant. |
| Personalization | **Graded ticker/sector overlap + judged-match bonus** | Every user owns theses and a watchlist; theses and items both carry tickers — overlap is a deterministic personalization factor that needs no new data. More shared tickers → higher score. |
| Ordering stability | **Hour-quantized age in the decay** | Order is stable within the hour; drift between hours reads as intentional, not jitter. |

---

## Scoring function

One score per item, computed read-time in Python over the candidate pool (stories from the last ~72h plus non-expired social topics — roughly 250 rows, trivial to score per request).

```python
score = (base + trend + affinity) * freshness
```

All weights below are the initial values, not sacred — they live in one `FEED_WEIGHTS` block and get tuned by eyeball via `?explain=1` (see Tuning).

### Freshness

```python
HALF_LIFE_H = {"story": 14, "x": 32}
age_h     = floor((now - created_at) / 1h)          # hour-quantized for stability
freshness = 0.5 ** (age_h / HALF_LIFE_H[kind])
```

Social topics hard-expire from the candidate pool 48h after `created_at`, regardless of score. Expiry is derived — there is no expiry column; refreshes update content only and never touch `created_at`, so the window is bounded by first admission and a still-hot discussion re-enters as a fresh row after expiry (ingestion design, revised 2026-06-04).

### Base (magnitude)

```python
base = {"story": 0.45 + 0.05 * min(independent_pub_count, 4),   # corroboration
        "x":     0.25 + 0.09 * heat}[kind]                       # heat 5 → 0.70
```

A heat-5 social topic (base 0.70) outranks an uncorroborated story (0.45–0.50) at equal freshness but loses to a judged-match story for the user (≥0.90 with affinity) — the golden ordering. Recalibrated 2026-06-04: the original `0.30 + 0.14·heat` gave heat-5 a base of 1.0, above the *maximum* story base (0.65), so a fresh social batch monopolized the feed top for every persona and the composition window was doing all the diversity work. Heat is also not calibrated cross-ticker (each ticker's topics come back as a 5/4/3 ladder — it's a within-conversation rank), so it shouldn't carry enough weight to dominate cross-kind ordering.

### Trend

```python
# eff_rank from the latest ticker_trends snapshot (1 = hottest, tiers end at 60)
trend = 0.25 * max(((1 - (eff_rank(t) - 1) / 60) for t in item_tickers if ranked(t)),
                   default=0.0)
```

Applies to both kinds. Social items get it almost by construction (their tickers are Tier-1) — heat and trend are correlated but distinct facts, and that is fine.

### Affinity (personalization)

The user side is two deterministic sets, computed once per request into an `AffinityContext`:

```python
user_tickers = owned_thesis_tickers | watchlist_symbols
#   entity_tickers (entity_type='thesis', owned via user_theses) ∪ user_watchlist
user_sectors = instrument sectors over user_tickers | emitted sectors of owned theses
#   instruments.sectors_json for every symbol in user_tickers (watchlist
#   included) ∪ theses.sectors_json — see design-sectors.md
```

Affinity is **graded, not binary** — more shared tickers means the story is more "for" this user:

```python
ticker_hits = len(item_tickers & user_tickers)
sector_hits = len(item_sectors & user_sectors)

affinity = (0.35 if judged_match else 0.0)            # thesis_story_links to an owned thesis
         + 0.25 * min(ticker_hits, 2) / 2             # 1 → 0.125, 2+ → 0.25
         + 0.10 * min(sector_hits, 2) / 2             # 1 → 0.05, 2+ → 0.10
affinity = min(affinity, 0.45)                        # judged matches and ticker overlap
                                                      # co-occur; cap the double count
```

Ticker hits saturate at 2 (was 3): every social topic and most stories carry a single ticker, and at `/3` a direct "this is about a ticker you own" hit was worth a negligible 0.083 — personalization couldn't differentiate social items between users at all.

The judged-match bonus outweighs raw overlap on purpose: a story the pipeline already judged as supporting/stressing an owned thesis is stronger evidence than ticker co-occurrence, and it keeps the ranker on-brand — the top of the feed bends toward *your beliefs*, not just the crowd's.

---

## Composition pass

Score-sort, then one greedy walk applying window constraints. Violators are demoted a few slots, never dropped.

- **Social density: ≤2 `x` items per any 5 consecutive slots.** Allows 40% social density at the front when heat justifies it; prevents a wall of X cards.
- **Ticker cooldown: same lead ticker never in adjacent slots.** Stops MSTR-story / MSTR-social / MSTR-story stacking when one name dominates the day. A story and a social topic on the same event are both legitimate — the report and the argument — they just don't touch.
- **Ticker density: ≤2 items per lead ticker per any 10 consecutive slots, any kind** (added 2026-06-04). The cooldown only *spaces* same-ticker items; on AVGO's earnings day 5 AVGO cards still landed in the first 18 slots, interleaved. The density cap bounds total presence: the report plus one argument make the front page, further takes sink below the fold. Defense in depth against upstream dedupe misses too — an ingestion gap degrades to "spread out", not "page of AVGO".

Tie-break inside equal scores: `created_at DESC`.

---

## Filters (forward-looking)

Pipeline order is the load-bearing decision:

```
candidates → annotate facets → FILTER → score → compose → cap
```

- **Filters are predicates over item facets; score is filter-invariant.** Same item, same score, under any filter. No per-filter ranking logic, ever.
- **Filter before composition** — density windows and cooldowns must hold on the list the user sees; filtering after composition punches holes in the interleaving.
- **Sector filter:** `item_sectors ∩ requested ≠ ∅`.
- **"For me" filter:** judged match to an owned thesis, OR `ticker_hits ≥ 1`. Sector-only overlap does *not* qualify — it is a ranking nudge, too loose for a filter. The filter and the ranker consume the same `AffinityContext`.
- **Server and client filtering both come free.** The API takes filter params, and since every item exposes its facets (`kind`, `tickers`, `sectors`, `matches`), the frontend can also filter the fetched list instantly client-side. Sector chips can ship as pure-frontend before the server param exists.

---

## Social items on the feed

What this doc owns is the feed-side contract; generation, the admission gate, volume targets, and dedupe/refresh semantics are owned by [`design-social-ingestion.md`](./design-social-ingestion.md) — stated once there, not duplicated here.

- `kind: "x"` on the API item; story items carry `kind: "story"`.
- Card: X logo, no `thumbnail` (no reliable image exists for a discussion), visibly distinct from news cards. Labeled social, never disguised.
- Every item carries bull/bear angles in house voice and ≥3 verified source tweets (handle, stance, URL) — the ingestion gate guarantees that floor, so the card renders tweets unconditionally.
- What ranking needs to know: items arrive in ~daily batches, refresh in place with a `created_at` bump, and leave the candidate pool 48h after `created_at`. That batch cadence is the whole reason for the per-kind half-life above.

---

## Tuning and explainability

- **`?explain=1`** on the feed route returns the per-item breakdown: `{kind, freshness, base, trend, affinity, score, demotions}`. Weight arguments become five-minute eyeball sessions instead of guesswork.
- **All knobs in one `FEED_WEIGHTS` block** in the composer: half-lives, base/trend/affinity weights, affinity cap, window sizes, expiry. These are exactly the parameters a learned ranker would replace post-launch if engagement data ever justifies one; until then they stay legible.

---

## Plumbing

- `build_feed_stories(conn)` → `build_feed(conn, user_id)`. It is only called from `build_home`, which already has `user_id`. The single-composer principle holds: one read-side composer produces the ranked, mixed-kind list.
- Candidate pool: stories last ~72h (judge-hidden excluded, as today) + social topics ≤48h old; rank in Python; cap at `HOME_FEED_LIMIT` after composition. The 72h window is *narrower* than today's windowless `LIMIT 200` query — on a dead-quiet weekend the feed gets shorter rather than reaching into stale history. Accepted; add a window-extension floor only if it reads empty in practice.
- `AffinityContext.judged_matches` filters `thesis_story_links` to the requesting user's owned theses (via `user_theses`). The item's `matches[]` payload stays global (all theses, as today) — same table, two consumers.
- `kind` lands on every item in the `/api/home` `news[]` payload. Re-run `bun run gen:types` in the frontend after the schema change (CLAUDE.md frontend-contract rule).

---

## Watch items

- **Hot-ticker pile-on.** Trend, heat, and social base all push the same direction, so a meme-frenzy day could three-stack past genuinely material macro. The 2-in-5 window and ticker cooldown are the guardrails; if they prove insufficient, the next knob is capping `base_x + trend` jointly rather than adding rules.
- **Affinity double-counting.** Judged matches and ticker overlap usually co-occur; the 0.45 cap bounds it. If "for me" users see a monoculture of their own tickers, lower the cap before touching the component weights.
- **Half-life feel.** 14h/32h are first guesses. The test: does the morning social batch still feel present at 3pm, and gone by next morning? Tune against that, not against abstract decay curves.

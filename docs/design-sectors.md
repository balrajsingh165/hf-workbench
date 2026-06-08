# Design: Sector Tags

**Status:** agreed (2026-06-03), not yet built.
**Scope:** Sector as a first-class soft tag across instruments, theses, stories, and social topics: the shared vocabulary, tag semantics, storage, and how each entity gets tagged. Consumers (the feed sector filter and the graded sector-overlap ranking factor) are specified in [`design-feed-ranking.md`](./design-feed-ranking.md); this doc owns the tags themselves.

---

## Why

The feed-ranking design needs sector on both sides of the overlap test: items (stories, social topics) and the user (theses, watchlist). An audit (2026-06-03) of where sector lives today:

| Surface | State |
|---|---|
| `src/news/taxonomies.py` | **Canonical taxonomy exists**: two-level `parent.leaf`, 11 groups × 43 leaves, alias map, `normalize_sectors()`. |
| `story.sectors_json` / `news_cluster.sectors_json` | **Clean.** Enum-enforced in synthesis (`src/news/synthesis.py`). Already soft and multi-valued: of ~1.3k stories, 537 carry 1 tag, 535 carry 2, 195 carry 3–5. |
| `news.sectors_json` (raw) | **Messy.** Canonical tags mixed with unnormalized publisher strings ("Government & Policy", "Defense"). Handled downstream by the alias map; never user-facing. |
| `thesis_match_chunks.sectors_json` | **Empty in practice** — `src/thesis/docs.py` hardcodes `sectors=[]`; 0 of 408 chunks tagged. |
| `instruments` | **No sector column.** 269 rows (219 equities, 22 ETFs, plus crypto/index/commodity/fx/rate). |

So the work is not designing a tag system — it is fixing the two empty surfaces (instruments, theses), formalizing the semantics the story side already exhibits, and reshaping the macro group.

---

## Tag semantics (the "soft tag" contract)

1. **Optional.** Zero tags is a valid state, never an error. SPY, a pure price-action social topic, an "other"-theme story — none of these has a sector, and forcing one fabricates significance. No surface may *require* a sector.
2. **Multi-valued, capped at 3.** A tag *set*, not a field — **at most 3 tags per entity**, enforced at write (synthesis schema for stories, the seed pass for instruments, the creation/discover schema for theses; over-emitting model output is truncated to its first 3). The story backlog runs 1–5 today; existing 4–5-tag rows stay (forward-only), new writes can't exceed 3.
3. **Mixed granularity: leaf implies parent.** Tag at the leaf when known (`technology.semiconductor`), at the parent alone when that is all that can honestly be said (`technology`). One matching rule everywhere: a leaf tag satisfies its parent — `technology.semiconductor` matches a `technology` filter. No other hierarchy machinery.
4. **Multiple aspects, not one classification.** Tags capture facets, which is exactly what single-sector schemes (GICS) cannot:
   - `MSTR` → `technology.software`, `crypto.bitcoin` — a software company *and* a bitcoin proxy.
   - `NVDA` → `technology.semiconductor`, `technology.ai_infrastructure`.
   - `TSLA` → `consumer.autos`, `technology.ai_infrastructure`.
   - `^TNX` → `macro.rates`; `DXY` → `macro.fx`; `GLD` → `macro.commodities`.

---

## Vocabulary

**One shared closed vocabulary, owned by `src/news/taxonomies.py`.** Every entity — instrument, thesis, story, social topic — tags from the same `CANONICAL_SECTORS` enum. No per-entity vocabularies; no free-form strings (the raw-news mess is the cautionary tale already in the DB). Models propose tags only through enum-constrained structured output; humans extend the vocabulary only by editing `SECTOR_HIERARCHY`.

**Extension rule:** add a leaf when real entities demand it — several instruments or recurring stories that don't fit an existing leaf — never because a model invented one.

### Macro group reshape (decided)

`macro.*` is a regular parent group on the filter bar, same as the others, and it covers the broad macro/event aspect: rates, GDP and economic prints, tariffs, geopolitical tensions. The group reshapes to five leaves — two added, two removed:

```python
"macro": ("rates",         # policy + inflation prints (CPI/PCE trade as rate expectations)
          "fx",
          "commodities",
          "growth",        # NEW — GDP, employment, PMI, consumer prints
          "geopolitics"),  # NEW — conflicts, sanctions, tariffs/trade policy, escalation
```

- `macro.growth`: economic-activity prints that aren't rate policy — GDP, payrolls, PMIs, retail sales.
- `macro.geopolitics`: conflicts, sanctions, tensions — and trade policy/tariffs, absorbing the removed `trade_policy` leaf. Re-point the `"geopolitics"` free-form alias here. (`"national security"` stays on `industrials.aerospace_defense`.)
- **Removed:** `macro.trade_policy` (→ `macro.geopolitics`) and `macro.sovereign_credit` (→ `macro.rates`). Both land in the alias map so incoming strings normalize to the new leaves.

Forward-only, no rebuild: stored story tags keep the old strings — leaf-implies-parent still resolves `macro.trade_policy` → `macro`, so parent-level filtering keeps working — while the enum no longer admits removed leaves on new writes.

---

## Storage

`sectors_json TEXT NOT NULL DEFAULT '[]'` columns, matching the established `story`/`news_cluster` pattern (`json_each` + index serves the filter workload):

- **`instruments.sectors_json`** — NEW column.
- **`theses.sectors_json`** — NEW column. Holds **model-emitted tags only** (the thesis's claim aspect). Ticker-derived tags are *not* stored here — they are reachable by join through `entity_tickers → instruments` and would go stale if denormalized. Read paths union the two.
- `story.sectors_json`, `news_cluster.sectors_json` — unchanged.
- Social topics — no stored sectors; derived (below).

A polymorphic `entity_sectors` link table (the `entity_tickers` pattern) is the upgrade path *if* sector ever becomes a primary join path; at 269 instruments and a filter-facet workload it is machinery without a problem.

---

## How each entity gets tagged

| Entity | Source | Notes |
|---|---|---|
| **Instruments** | One-shot seed pass: LLM-proposed, human-skimmed (269 rows — one sitting), written to `sectors_json`. New instruments tagged when they enter the registry (the `pending_instruments` admission flow). | Thematic ETFs get their theme (`SMH` → `technology.semiconductor`, `XLE` → `energy.oil_gas`); broad-market ETFs and indices (SPY, `^GSPC`) get **none**. |
| **Stories** | Already done — synthesis structured output, enum-enforced; add the **≤3 cap** (schema `maxItems: 3`, truncate on excess). | Picks up the macro reshape automatically (the enum is generated from `SECTOR_HIERARCHY`). |
| **Theses** | **Both, unioned at read:** (a) model-emitted — the creation/discover structured output gains a `sectors` enum field (a prompt + schema change in the existing call, near-free), stored on `theses.sectors_json`; (b) ticker-derived — the thesis's tagged tickers' instrument sectors, computed by join. | Emitted tags carry the claim aspect a ticker join can't: a "Fed pivot" thesis with homebuilder tickers is still `macro.rates`. `src/thesis/docs.py` stops hardcoding `sectors=[]`; `thesis_match_chunks.sectors_json` populates from the emitted set on the next index upsert. |
| **Social topics** | **Derived from the ticker's instrument tags — never asked of Grok.** Topics are per-ticker by construction; inheritance is deterministic and free, and keeps the Grok prompt focused on what only it can do. | Owned by the social-ingestion companion design. |
| **Raw news** | **Not tagged — out of scope (decided 2026-06-03).** Raw `news.sectors_json` stays as-is (the firehose writes `'[]'`; legacy messy strings are never user-facing). Canonical tags enter the system at story synthesis. | No consumer reads raw-news sectors; tagging them is work without a workload. |

---

## Feed integration (consumed by design-feed-ranking.md)

- **User side:** `user_sectors = instrument sectors over user_tickers ∪ emitted sectors of owned theses`, where `user_tickers = owned-thesis tickers ∪ watchlist`. The watchlist now contributes sector affinity (it couldn't before — `instruments` had no sector), and both the sector filter and the graded sector-overlap ranking factor get real data from the instrument seed pass alone, without waiting on thesis backfill.
- **Item side:** stories carry their own tags; social topics derive from their ticker's instrument tags.
- **Filter UI:** parent-level chips (11 groups — `Technology`, `Energy`, `Macro`, …). 43 leaves are too many for chips; leaf-implies-parent makes parent filters work with zero re-tagging. Leaf drill-down is deferred until demanded.

---

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Vocabulary | One shared closed enum in `taxonomies.py`, both levels taggable | Single source of truth; free-form strings already proved messy in raw news. |
| Hierarchy semantics | Leaf implies parent; no other machinery | One rule serves mixed-granularity tagging and parent-level filtering. |
| Softness | Zero tags valid everywhere; tags never required | Forcing tags fabricates significance (same failure mode as forcing numbers in angle prompts). |
| Storage | `sectors_json` columns (`instruments`, `theses` new; `story` unchanged) | Matches existing pattern; `json_each` serves the filter workload at this scale. |
| Thesis tags | Model-emitted (stored) ∪ ticker-derived (joined at read) | Emitted carries the claim aspect; derived stays live as instrument tags evolve; no denormalization. |
| Social-topic tags | Derived from ticker's instrument tags | Deterministic, free, keeps the Grok prompt lean. |
| Tag count | **At most 3 per entity**, enforced at write | Forces the dominant aspects; the story backlog's 4–5-tag rows read as noise. Existing rows stay (forward-only). |
| Macro | Regular parent group on the filter bar; **reshape to `rates, fx, commodities, growth, geopolitics`** (add `growth`/`geopolitics`; drop `trade_policy`/`sovereign_credit` via aliases) | Macro is a real user-facing aspect (rates, GDP, tariffs, geopolitics), not a special case; five leaves cover it without splitting hairs. |
| Raw news | **Not tagged** | No consumer reads raw-news sectors; downstream stories carry the canonical tags. |
| Taxonomy changes | Forward-only; no story rebuilds | Old tags stay readable (leaf-implies-parent resolves removed leaves to `macro`); aliases re-map them on any normalize pass. |

---

## Build order

1. Taxonomy edit: reshape macro to `("rates", "fx", "commodities", "growth", "geopolitics")`; alias the removed leaves (`trade_policy → geopolitics`, `sovereign_credit → rates`); re-point the `geopolitics` free-form alias. (Synthesis enum updates automatically.) Add the ≤3-tag cap to the synthesis schema.
2. Schema: `instruments.sectors_json`, `theses.sectors_json`; re-run `db/schema.py`.
3. Instrument seed pass (script: LLM-proposed tags per instrument, print for skim, write on approval). Wire tagging into the `pending_instruments` admission path.
4. Thesis prompt/schema change: emit `sectors` (enum) in creation/discover structured output; persist to `theses.sectors_json`; populate `ThesisDoc.sectors` → chunks.
5. Validate with `hf-evals` only if the thesis-creation prompt change shifts agent behavior beyond the added field.

## Testing

- `normalize_sectors` accepts the new leaves, maps the removed ones (`macro.trade_policy → macro.geopolitics`, `macro.sovereign_credit → macro.rates`), and honors the re-pointed `geopolitics` alias.
- Tag cap: an over-emitting model response is truncated to its first 3 tags deterministically; ≤3 holds across synthesis, seed pass, and thesis creation.
- Seed-pass report: per-instrument proposed tags, count of untagged (expected: broad ETFs, indices); skim before write.
- Thesis creation emits only enum values; zero-sector theses persist as `[]` without error.
- Read-path union: a thesis with emitted `macro.rates` and a homebuilder ticker surfaces both `macro.rates` and `real_estate.homebuilders` in `user_sectors`.

## Deferred

- `entity_sectors` polymorphic link table (only if sector becomes a primary join path).
- Leaf-level filter UI (parent chips first).
- Per-user sector preferences (`sectors_of_interest` in profile markdown) joining `user_sectors` — waits on the broader `user_preferences` migration (see `design-watchlist.md` § Deferred).

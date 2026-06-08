# Plan: End-to-End P0+P1+P2 Vertical Slice

**Status (2026-04-24):** Day 1 and Days 4–5 not started. Days 2–3 done. See per-day status markers below. User-id format is `user_N` (integer suffix); two users seeded (`user_1`, `user_2`).

**Score dimensions.** Two sub-scores, both higher = better: **Freshness** (decay from last supporting signal) and **Tailwind** (price agreement with thesis direction). Full spec in `docs/plan-scoring-system.md`. The legacy "Popularity" dimension was dropped.

**Goal.** Prove the core loop feels magical before investing in any single layer. Build thin, connected versions of thesis creation (P0), scoring (P1), and daily digest (P2) against the seeded stories and 1 test user.

**Success criterion.** A reviewer can: create a thesis in one session, come back the next day, and read a digest that meaningfully references that thesis — including at least one stress signal that feels non-obvious. If that single demo lands, the architecture is validated. If it feels generic, we know where the quality gap is.

**Non-goals for this slice.**
- Automated news ingestion (use the seeded stories)
- Popularity / crowding score (dropped from the model; no external signal source)
- Multi-user, auth, web UI — CLI only
- Thesis resolution / outcome tracking

---

## Timeline — 5 working days

### Day 1 — P0 thesis creation — DROPPED

The standalone `agents/thesis_create.py` CLI + `agents/prompts/thesis_sharpener.md` plan was dropped in favor of an interactive Sage chip on the AI SDK chat surface. See `docs/design-thesis-creation.md` → Caller #2 for the replacement shape. For the slice demo, use the existing seeded theses (already hand-written) and skip Day 1.

---

### Day 2 — P1 matching + freshness scoring — DONE

**Matching pipeline — DONE.** Both directions shipped:
- `agents/match_thesis_for_story.py` — story→thesis (story-arrival / ingest path).
- `agents/match_story_for_thesis.py` — thesis→story (thesis-create / cold→hot promotion / ad-hoc reindex). Same retrieval + judge pipeline, mirrored.
- Shared judge at `src/thesis/story_judge.py`. One place to tune the prompt.

**`thesis_story_links` table — DONE.** Shape is richer than the original sketch:
```
(thesis_id, story_id, relation, confidence, matched_invalidation, rationale,
 retrieval_score, best_chunk_key, source, updated_at)
PRIMARY KEY (thesis_id, story_id)
```
- `source ∈ {ingest, backfill}` tracks provenance.
- `updated_at` is refreshed on conflict (judgment freshness).
- Freshness uses `story.created_at`.
- Re-running `match_story_for_thesis` deletes prior `source='backfill'` rows for the thesis before inserting the new set, so narrowed windows or flipped judgments don't leak stale evidence. Ingest rows survive.

**Deliverable — DONE.** `uv run python -m agents.score_theses` reads `thesis_story_links` and updates `theses.score_freshness` and `theses.score`. Full spec in `docs/plan-scoring-system.md`.

All 12 theses seeded (2 users, 13 user-thesis rows, per-thesis `horizon_days`), backfilled via `match_story_for_thesis`, and scored. Scoring never writes `status` — auto stress-flip deferred (see `docs/plan-scoring-system.md` and `TODO.md → "Auto stress-flip (deferred)"`). Binary `Supports`/`Stresses` chip derivation lives in `src/thesis/scoring.py::chip_for`.

**Done when.** ✓ Multiple theses scored with fresh support (e.g. thesis_002 = 100, thesis_008 = 89 with 1 stresses link). Decay visible across theses (thesis_008 lower than thesis_002 for the same horizon because fewer/older supports). thesis_008 carries a `Stresses` chip. Spot-check: directionally correct.

---

### Day 3 — P1 Tailwind — DONE

Implements the Tailwind half of `docs/plan-scoring-system.md`. Thesis-update CLI is deferred — `agents/score_theses.py` already handles rescoring, and a separate thesis-edit agent is out of scope for the reviewer demo.

**Data source.** Heurist Mesh `YahooFinanceAgent` via `src/clients/mesh.py`. No `yfinance` dep — we call the Mesh REST API so the price path matches how news ingestion already works.

**Ticker canonicalization.** Every thesis ticker is run through Mesh `resolve_symbol` before use; the top match (by Yahoo's own score) is the canonical symbol. Results are cached to `db/ticker_canonical.json` so scoring runs are offline after first warm-up. This fixes cases like `USDJPY` (→ `JPY=X`) and keeps already-canonical symbols (`RHM.DE`, `BA.L`, `FANUY`, `^VIX`) stable.

**Direction source.** Parsed out of the ticker parenthetical in the thesis markdown: `- SPY (bearish — ...)` → `bearish`. `neutral` and unparseable tokens are skipped.

**Tailwind computation.** For each canonical ticker:
1. `yahoo_price_history(..., period="1mo", interval="1d")` — Mesh returns `window_summary.open_close_change_pct` directly, so we don't re-implement the return math.
2. Align with direction: `signed = pct if bullish else -pct`.
3. Map to 0–100 by linear clamp at ±10%: `tailwind_i = round(50 + clamp(signed/10, -1, 1) * 50)`.

Thesis tailwind = mean of per-ticker tailwinds with valid prices + a parsed direction. If every ticker fails or has no direction, leave `score_tailwind = NULL` and keep composite = freshness alone (don't fake it).

**Composite.** `round((score_freshness + score_tailwind) / 2)` when tailwind is non-null; else freshness.

**Done when.** All 12 seeded theses have both scores populated (except the honest-NULL case), and a human reviewer agrees the numbers directionally match recent price action — e.g. a bearish-TLT thesis scores high tailwind when TLT is down over the month.

**Done (2026-04-24).** 13/13 user-thesis rows now carry a non-null composite (`min=54`, `max=96`). Spot-checks that pass: thesis_001 (bearish Fed, SPY/QQQ/TLT bearish) at tailwind 20 with SPY +7.9% — fresh news but market disagreeing; thesis_007 (long gold) at 83 with GLD up; thesis_009 (enterprise AI bulls NOW/PLTR/CRM) at 11 with those names down — thesis under pressure. `USDJPY` correctly canonicalised to `JPY=X` via Mesh. Cache at `db/ticker_canonical.json` (44 entries).

---

### Day 4 — P2 personalized daily digest — NOT STARTED

**Deliverable.** `uv run python -m agents.daily_digest --user <user_id>` prints (and writes to `users/<user_id>/digests/YYYY-MM-DD.md`) a personalized brief.

**Spec — digest structure.**
1. **Your convictions today** — one line per active thesis with score + delta since yesterday + one-line "what moved it".
2. **Under stress** — any thesis flipped to `stressed`, with the specific story and invalidation condition that triggered it.
3. **Fresh support** — theses that gained Freshness today, with the story.
4. **Orphans** — stories in the DB that matched no thesis. One line each. This is the "what convictions am I missing?" prompt.

**Tone mandate.** Each section is 1–3 sentences, declarative, no hedging. "Your TSMC thesis is breaking. Brent at $100 didn't move it, but the Fed delay did." Not "you may want to consider...".

**Done when.** A reviewer reads the digest cold (no context) and can articulate what the user believes and what happened to those beliefs today.

---

### Day 5 — Integration, polish, demo — NOT STARTED

- Use the already-seeded theses for the test user (Day 1 was dropped — no creation agent).
- Run scoring, then digest.
- Reviewer walkthrough. Record what felt sharp and what felt generic.
- Write `docs/slice-retro.md` with the 3 biggest quality gaps — those become the next sprint.

---

## Critical dependency — RESOLVED

The matching classifier was the load-bearing piece. Both directions now ship on the dense-retrieval-plus-LLM-adjudicator shape: `match_thesis_for_story` (story→thesis) and `match_story_for_thesis` (thesis→story), sharing `src/thesis/story_judge.py`. A 60-pair hand-labeled eval set lives at `docs/ref/matching-eval-set.md` as a reference for ad-hoc spot checks. An automated eval runner was considered and deprioritized (schemas still in flux; spot-check quality gate is sufficient for now — see spike doc "Deprioritized" section).

## File layout after this slice

```
agents/
  match_thesis_for_story.py     # shipped
  match_story_for_thesis.py      # shipped
  score_theses.py               # Day 2 — shipped
  daily_digest.py               # Day 4 — not started
  prompts/
    digest_writer.md            # Day 4 — not started
src/
  thesis/story_judge.py         # shipped — shared (thesis, story) judge
  thesis/story_links.py         # shipped — persistence helper
  thesis/scoring.py             # shipped — compute_freshness, chip_for, SUPPORT_STRONG_CONF
  thesis/match_index.py         # shipped
  story/match_index.py          # shipped
users/<user_id>/
  digests/2026-04-24.md         # Day 4 — not started
global/theses/
  thesis_001.md ... thesis_010.md  # 10 theses already present
db/schema.py                    # thesis_story_links shipped
docs/slice-retro.md             # written day 5
```

## Open questions to resolve during the slice

1. Should horizon be stored in DB (`horizon_days`, already there) **and** markdown, or DB-only? Current principle says no overlap — DB wins.
2. Where does thesis "implied direction" (long/short/bias) live? Proposal: markdown frontmatter, since it's narrative-derived, not user-filtered.
3. Does the digest need to be deterministic (same inputs = same output) for reviewability? Probably yes at this stage — set temperature=0 for the digest writer.

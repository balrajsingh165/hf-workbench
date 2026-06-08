# SOP: Add a New Thesis

## Overview

A thesis is a single declarative sentence expressing a durable, actionable market belief. Adding one involves inserting a row into SQLite (queryable index) and creating a markdown file in `global/theses/` (rich content for AI context).

The DB holds only what you'd filter, sort, or join on. Everything narrative — the statement itself, tickers, invalidation conditions, signals — lives in markdown.

---

## Crafting the Thesis: Shallow and Deep Layers

A thesis shows up on two surfaces in the product (see `docs/mock-ux-walkthrough.md`):

- **Shallow** — a chip in the news feed and top zone. 2–4 words. Read in a glance.
- **Deep** — the thesis detail page, with the full statement, core mechanic, invalidations, signal timeline. Where nuance, second-order effects, and contrast-with-consensus live.

> The single most common failure is **the statement trying to do the detail-page's job**. When a title has "not just X", "X is Y not Z", a colon, or a semicolon, it's almost always because the author is packing the non-obvious insight into the title. Move that insight into **Core Thesis**. The statement is the clean, declarative version of the belief; the core thesis is the setup behind it.

### Label (UI chip)

2–4 words. A topical tag, recognizable without context. Lives in markdown only.

- Good: `Fed pivot delayed`, `Energy supply crunch`, `AI adoption gap`, `Nuclear renaissance`, `Gold / de-dollarization`
- Bad: `CPU inference re-rate for server OEMs and DRAM` (too long, multi-concept), `The big trade` (not topical)

### Statement (thesis detail header)

ONE declarative sentence. Target ≤ 15 words, hard cap 20. Present tense. No hedging.

Rules:
1. **One claim.** If you need "and" joining two independent clauses, split into two theses or pick the stronger one.
2. **No contrastive framing.** "X, not Y" and "not just X" both require the reader to know the consensus. That context belongs in Core Thesis.
3. **No semicolons or colons.** Both almost always signal two-claims-in-one.
4. **Name the mechanism, not the news.** "Hormuz at $107" is a headline. "Under-hedged US airlines get caught by sustained Brent" is a thesis.
5. **Specific enough to be testable.** "Tech is doing well" fails. "GPU revenue growth decelerates below 30% by Q3" passes.
6. **Broad enough to outlive the headline.** If the thesis would resolve the moment the next article prints, it's a trade reaction, not a thesis.
7. **No horizon in the title.** Horizon lives in `theses.horizon_days` (intrinsic to the belief, inferred at creation — never user-entered). Don't write "over the next 6–8 weeks."

### Bad → Good

| Bad statement | Why it fails | Good statement |
|---|---|---|
| *CPU inference re-rates server OEMs and DRAM, not just chip designers.* | Contrastive ("not just"); requires knowing the consensus GPU trade. | *CPU-side enterprise inference re-rates server OEMs and memory suppliers.* |
| *Hormuz at $107 is an airline fuel-hedge stress test, not a Spirit Airlines story.* | "X is Y, not Z" — two framings; leans on a headline the reader may not have seen. | *Under-hedged US airlines get caught by sustained $100+ Brent into Q2 earnings.* |
| *GPT-5.5 SWE-Bench numbers cross the enterprise replacement threshold for IT body shops.* | Jargon-dense; mechanism hidden; no direction. | *Indian IT services revenue compresses as enterprises replace junior engineering with AI.* |
| *Data center power demand forces a nuclear renaissance; SMR permits make Cameco the winner.* | Semicolon = two claims. Ticker call belongs in `## Tickers`. | *Data-center power demand forces a nuclear buildout that outruns uranium supply.* |

### Where the nuance goes

The "what the market is missing," "contrast with consensus," and "chain of second-order effects" — all of that belongs in **Core Thesis** (2–4 sentences). That's the detail-page real estate. The statement is the headline; the core thesis is why it's non-obvious.

---

## Step 1 — Generate Thesis ID

Format: `thesis_XXX` with zero-padded incrementing number.

Examples: `thesis_001`, `thesis_042`

---

## Step 2 — Insert into SQLite

Theses are global; the belief's horizon lives on `theses`, while user ownership, status, and scores live on the `user_theses` link table. Inserting a new thesis is two rows:

### Table: `theses`

| Column | Type | Required | Default | Description |
|---|---|---|---|---|
| `id` | TEXT | ✓ | — | Unique thesis ID |
| `owner_count` | INTEGER | — | `0` | Number of users who own this thesis |
| `horizon_days` | INTEGER | — | `45` | Decay clock for scoring. Intrinsic to the belief, inferred at creation — never user-entered. Set a curated value here if the thesis has a clear time anchor; otherwise the default stands. |
| `created_at` | TEXT | auto | `datetime('now')` | ISO timestamp |

### Table: `user_theses`

| Column | Type | Required | Default | Description |
|---|---|---|---|---|
| `user_id` | TEXT | ✓ | — | Owner's user ID (FK → users) |
| `thesis_id` | TEXT | ✓ | — | FK → theses |
| `status` | TEXT | — | `active` | `active` · `stressed` · `resolved` |
| `score` | INTEGER | — | NULL | Composite score 0–100 |
| `score_freshness` | INTEGER | — | NULL | 0–100 — decay from last supporting signal |
| `score_tailwind` | INTEGER | — | NULL | 0–100 — price agreement with thesis direction |
| `created_at` | TEXT | auto | `datetime('now')` | ISO timestamp |
| `resolved_at` | TEXT | — | NULL | Set when status → resolved |
| `outcome` | TEXT | — | NULL | `correct` · `partial` · `incorrect` |

Example:

```python
import sqlite3

conn = sqlite3.connect("db/hf.db")
with conn:
    # horizon_days defaults to 45; pass it only for a curated time anchor.
    conn.execute(
        "INSERT INTO theses (id, horizon_days) VALUES (?, ?)",
        ("thesis_001", 90),
    )
    conn.execute(
        "INSERT INTO user_theses (user_id, thesis_id) VALUES (?, ?)",
        ("user_1", "thesis_001"),
    )
conn.close()
```

Scores are NULL at creation — they get computed by the Scoring Agent in Step 7.

---

## Step 3 — Create Thesis Markdown

Path: `global/theses/{thesis_id}.md`

This file is the **full thesis document** — everything the AI reads to understand, monitor, and challenge this thesis.

### Template

```markdown
# Thesis: {statement}

## Tickers
- TICKER (bullish | bearish — one-line rationale)

## Core Thesis
<!-- 2–4 sentences. The non-obvious mechanic, what the market is missing, and the contrast with consensus live here. -->

## Invalidation Conditions
<!-- What would break this thesis? Be specific and testable. -->
- Condition 1
- Condition 2
```

> **Note:** Thesis ID is derived from the filename (`thesis_001.md` → `thesis_001`). Do not add an ID or Label metadata line to the markdown.

### Data Ownership — No Overlap Except Primary Key

| Data | DB | Markdown | Rule |
|---|---|---|---|
| `id` | ✓ | filename | Filename is the ID — no metadata line in markdown |
| horizon_days (`theses`), user_id + status (`user_theses`) | ✓ | — | Queryable, DB-only |
| Scores (composite + 2 sub: freshness, tailwind) | ✓ | — | Queryable, DB-only |
| created_at, resolved_at, outcome | ✓ | — | Queryable, DB-only |
| Tickers + direction | ✓ (`entity_tickers`) | ✓ | Parsed from markdown, indexed in `entity_tickers` |
| Statement | — | ✓ | Narrative, markdown-only |
| Core thesis | — | ✓ | Narrative, markdown-only |
| Invalidation conditions | — | ✓ | Narrative, markdown-only |
| Signals timeline | ✓ (`thesis_story_links`) | — | Queryable, DB-only |

---

## Step 4 — Verify

```bash
# Check DB rows (global + ownership)
uv run python -c "
import sqlite3
conn = sqlite3.connect('db/hf.db')
conn.row_factory = sqlite3.Row
print(dict(conn.execute('SELECT * FROM theses WHERE id = ?', ('thesis_001',)).fetchone()))
print(dict(conn.execute('SELECT * FROM user_theses WHERE thesis_id = ?', ('thesis_001',)).fetchone()))
"

# Check markdown
cat global/theses/thesis_001.md
```

---

## Step 5 — Backfill the Signal Timeline (Thesis → Story)

A new thesis starts with no signal history. Before it becomes visible to the user it must be matched against recent stories so the timeline is populated at t=0. This is the slow, high-fan-out side of the pipeline.

```bash
uv run python -m agents.match_story_for_thesis --thesis thesis_001 --window 14
```

### No cap — keep everything above the fitness floor

Unlike the story→thesis direction (which is capped at 0–2 best matches per story), the thesis→story direction has **no per-thesis cap**. A durable thesis is corroborated or stressed by many stories over weeks/months, and the full set of links *is* the signal timeline the user reads.

- **Minimum fitness score.** Judge output must clear the retrieval `--min-score` (default `0.5`) and the judge must return `supports` or `stresses` (not `unrelated`). Everything that passes both gates is persisted to `thesis_story_links` with `source='backfill'`.
- **No top-N truncation.** If 12 stories in the last 14 days clear the floor, all 12 land as timeline entries. The user's detail page ranks and filters them at read time; the pipeline does not pre-truncate.
- **0 backfill matches is still valid** for niche theses where recent stories are sparse. Do not widen the window or drop the floor to fabricate a timeline — the thesis will accrue signals as stories arrive and the ingest-side matcher fires. Backfill just prevents a cold start.

### Tuning the floor

| Knob | Default | When to change |
|---|---|---|
| `--window N` | 14 days | Widen to 30 for slow-moving macro theses (Fed, energy). Narrow to 7 for fast-moving single-name theses. `--window 0` = all stories (use sparingly). |
| `--min-score` | 0.5 | Raise to 0.6+ if the timeline is noisy. Lower to 0.4 only after verifying precision holds. |

Stale backfill rows for this thesis are deleted before new ones are written, so re-running after a judge or prompt change cleanly reindexes. `source='ingest'` rows from the story-side path are preserved.

### When to re-run

- **On cold→hot promotion.** User returns after >48h idle: re-run with a window that covers the gap.
- **On thesis edit.** Edit to statement or invalidations → rebuild thesis chunks (`agents.build_match_index --kind thesis`) then re-run backfill.
- **On judge / prompt change.** One-shot reindex per thesis to refresh verdicts.

---

## Step 6 — Verify Timeline

```bash
# Check backfill links (no cap — all supports/stresses within window land here)
uv run python -c "
import sqlite3
conn = sqlite3.connect('db/hf.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
  '''SELECT story_id, relation, confidence, matched_invalidation
     FROM thesis_story_links
     WHERE thesis_id = ?
     ORDER BY confidence DESC''',
  ('thesis_001',),
).fetchall()
for r in rows:
    print(dict(r))
"
```

---

## Step 7 — Score the Thesis

Backfill populates `thesis_story_links` but does not touch `theses.score*`. Run the scoring agent once to initialize Freshness (and the composite `score`) off the fresh link timeline:

```bash
uv run python -m agents.score_theses --thesis thesis_001
```

MVP composite is Freshness only; Tailwind stays NULL until it lands (see `docs/plan-scoring-system.md`). A thesis with zero backfill supports scores `0` — that's correct, not a failure.

Verify the write landed:

```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('db/hf.db')
conn.row_factory = sqlite3.Row
row = conn.execute(
    'SELECT score, score_freshness, score_tailwind FROM user_theses WHERE thesis_id = ?',
    ('thesis_001',),
).fetchone()
print(dict(row))
"
```

Re-run the scoring agent any time `thesis_story_links` changes (new ingest matches, re-backfill after an edit). The call is idempotent.

---

## Lifecycle Notes

- **Status transitions**: `active` → `stressed` (automatic, when composite < 35 or invalidation condition matched) → `active` (if stress resolves). `active`/`stressed` → `resolved` (user-initiated only).
- **Signals**: Append-only. Never edit or delete past signals. Each signal is tagged `(+)` confirming or `(−)` stressing.
- **Scores**: Updated by the Scoring Agent in the DB only.
- **Resolution**: When closing a thesis, set `status=resolved`, `resolved_at`, and `outcome` in the DB only.

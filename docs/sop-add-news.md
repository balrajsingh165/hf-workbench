# SOP: Add a Story

## Overview

A story aggregates one or more raw news rows into a single user-facing narrative. Adding one involves inserting a row into SQLite (queryable index) and creating a markdown file in `global/stories/` (rich content for AI context and user-facing display).

The DB holds what you'd filter, sort, or join on. Everything narrative stays in markdown.

Production stories should normally come from `agents.route_news_clusters` + `src/news/persist.py::write_cluster_story()`. Manual story creation is for fixtures and one-off repairs.

---

## Step 1 — Generate Story ID

Format: `story_XXX` with zero-padded incrementing number.

Examples: `story_001`, `story_042`

---

## Step 2 — Insert into SQLite

Table: `story` (defined in `db/schema.py`)

| Column | Type | Required | Default | Description |
|---|---|---|---|---|
| `id` | TEXT | ✓ | — | Unique story ID |
| `cluster_id` | TEXT | ✓ | — | Source `news_cluster.id` |
| `centroid_news_id` | TEXT | ✓ | — | Representative raw `news.id` |
| `headline` | TEXT | ✓ | — | User-facing headline |
| `overview_json` | TEXT | ✓ | — | JSON claim list: `{text, source_doc_ids, confidence}` |
| `sectors_json` | TEXT | ✓ | `[]` | JSON array of sector labels |
| `regions_json` | TEXT | ✓ | `[]` | JSON array of region labels |
| `theme_tag` | TEXT | ✓ | `other` | Discovery/routing label |
| `created_at` | TEXT | auto | `datetime('now')` | ISO timestamp |

Example:

```python
import sqlite3, json

conn = sqlite3.connect("db/hf.db")
conn.execute(
    """
    INSERT INTO story
    (id, cluster_id, centroid_news_id, headline, overview_json, market_relevance_json,
     sectors_json, regions_json, theme_tag)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        "story_001",
        "cluster_001",
        "news_001",
        "Brent crude breaks higher as supply risk returns.",
        json.dumps([
            {"text": "Brent crude rose as traders repriced supply risk.", "source_doc_ids": ["news_001"], "confidence": "high"}
        ]),
        json.dumps({"why_it_matters": "Energy inflation pressure tightens the Fed path."}),
        json.dumps(["Broad Market Indices", "Energy"]),
        json.dumps(["Global"]),
        "energy",
    ),
)
conn.commit()
conn.close()
```

---

## Step 3 — Create Story Markdown

Path: `global/stories/{story_id}.md`

This file is the **full story** — the narrative content displayed to users and injected into AI context.

### Template

```markdown
# {Headline}

- **ID**: {story_id}
- **What Changed**: {One-sentence event delta}

## Overview

- {Fact/development from source A, with specific numbers and context.} [1]
- {Fact/development from source B.} [2]

## Claims

- {Atomic claim with citation.} [1]

## Quotes

> "{Direct quote with attribution.}" — {Publisher} [1]

## Sources

1. [{Article title}]({URL}) — {Publisher} ({news_id})
```

### Markdown Rules

1. **ID line mirrors the filename** — keep it exact for operator readability.
2. **No Tickers or Sectors sections** — these are structured/filterable data and live in the DB / `entity_tickers`.
3. **Bulleted overview** — each bullet is one discrete fact with a numeric source citation.
4. **One bullet = one claim** — don't merge multiple facts into a single bullet. If a source contributes 3 distinct facts, that's 3 bullets.
5. **Quotes are attributed** — publisher/source citation must be clear.

---

## Ticker Format — Yahoo Finance Canonical Symbols

All story tickers live in `entity_tickers` with `entity_type='story'` and must use Yahoo Finance canonical format so they resolve in the Yahoo Finance API. Common mappings:

| Asset | Yahoo Finance Symbol |
|---|---|
| S&P 500 | `^GSPC` |
| Nasdaq 100 | `^NDX` |
| Nasdaq Composite | `^IXIC` |
| Dow Jones Industrial | `^DJI` |
| PHLX Semiconductor | `^SOX` |
| VIX | `^VIX` |
| US Dollar Index | `DX-Y.NYB` |
| Brent Crude Futures | `BZ=F` |
| WTI Crude Futures | `CL=F` |
| Gold Futures | `GC=F` |
| 10-Year Treasury Yield | `^TNX` |
| Individual stocks | Ticker as-is (e.g. `NOW`, `AAPL`, `TSMC`) |

When in doubt, verify the symbol resolves at `https://finance.yahoo.com/quote/{SYMBOL}`.

---

## Data Ownership — No Overlap Except Primary Key

| Data | DB | Markdown | Rule |
|---|---|---|---|
| `id` | ✓ | — | DB-only (filename is the bridge) |
| source raw IDs | ✓ | ✓ | DB stores `centroid_news_id`; markdown source list names raw `news_id`s |
| tickers | ✓ (`entity_tickers`) | — | Queryable, DB-only |
| sectors, regions, theme_tag | ✓ | — | Queryable, DB-only |
| created_at | ✓ | — | Queryable, DB-only |
| Headline | ✓ | ✓ | DB powers cards; markdown is the archive/rendered story |
| Overview (bulleted narrative) | ✓ (`overview_json`) | ✓ | DB powers brief/matching; markdown is human-readable archive |
| Quotes | — | ✓ | Markdown-only |
| Source URLs | — | ✓ | Markdown-only |

---

## Step 4 — Match Against Theses (Story → Thesis)

Every new story must be judged against the thesis index so supporting/stressing links land in `thesis_story_links`. This is the fast side of the pipeline — one story against many theses.

```bash
uv run python -m agents.match_thesis_for_story story_001
```

### Cap: 0–2 best matches per story

A single story can move **at most 2 theses**. Most stories move **zero**. Keep only the top 1–2 by judge `confidence`; drop the rest even if they scored above the retrieval threshold.

- **0 matches is the common, valid outcome.** Do not lower thresholds or coerce weak candidates into `supports` to avoid an empty result. `unrelated` verdicts are dropped by design (see `docs/ref/thesis-news-matching-system.md`, invariant 2: "Silence beats noise").
- **1–2 matches** — persist rows to `thesis_story_links` with `source='ingest'`. Order by `confidence` descending and truncate at 2.
- **Never 3+.** If retrieval surfaces more than two judge-accepted candidates, keep only the top two. A story that "moves everything" is almost always noise; pick the best signal.

### Why this cap (and why it's asymmetric with the thesis side)

Stories are concrete and usually about one thing — a single ticker, a single decision, a single event. The theses they move are few. The **thesis side** of the pipeline is the opposite: a durable belief gets corroborated or stressed by many stories over time, which is the timeline (see `sop-add-new-thesis.md` Step 5). So:

- **Story → thesis:** hard cap 0–2. Fan-out must stay narrow.
- **Thesis → story:** no per-thesis cap. Every story that clears a minimum fitness score is kept to form the signal timeline.

A capped per-story fan-out keeps `thesis_story_links` clean and keeps stress flips attributable to a real signal, not a weak third-place match.

---

## Step 5 — Verify

```bash
# Check DB row
uv run python -c "
import sqlite3, json
conn = sqlite3.connect('db/hf.db')
conn.row_factory = sqlite3.Row
row = conn.execute('SELECT * FROM story WHERE id = ?', ('story_001',)).fetchone()
for k in row.keys():
    print(f'{k}: {row[k]}')
"

# Check markdown
cat global/stories/story_001.md

# Check links written for this story (expect 0, 1, or 2 rows)
uv run python -c "
import sqlite3
conn = sqlite3.connect('db/hf.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
  'SELECT thesis_id, relation, confidence, matched_invalidation FROM thesis_story_links WHERE story_id = ? ORDER BY confidence DESC',
  ('story_001',),
).fetchall()
for r in rows:
    print(dict(r))
"
```

---

## Ingestion Pipeline Notes

- **Firehose ingest:** `agents/firehose.py` polls the configured RSS feeds, applies the deterministic ticker/macro gate, stores raw `news` rows, and attaches each row to a `news_cluster`. Raw rows are single-source citation targets, not user-facing syntheses.
- **Cluster routing:** `agents/route_news_clusters.py` applies R0-R6 from `src/news/routing.py` and writes stories only for promoted clusters. Non-promoted clusters remain in firehose storage.
- **Story synthesis:** `src/news/persist.py::write_cluster_story()` runs one structured Gemini call against up to three cluster members, verifies citations/tickers deterministically, writes the `story` row, and renders `global/stories/story_XXX.md`.
- **Quality filter:** `agents/judge_stories.py` writes `story_quality_label`; user-facing endpoints hide labels `unclear` and `no_value`.
- **Operator docs:** use `docs/news-story-pipeline.md` for architecture and `docs/news-rearchitecture-runbook.md` for commands.

---

## Aggregation Philosophy

A story in this system is **not** a 1:1 mirror of a source article. It is an
event-level synthesis over a cluster of raw news rows. This means:

- If 3 outlets cover the same Fed decision, that's 1 story with 3 source rows, not 3 user-facing entries.
- Each bullet in the overview should trace back to a specific source.
- The headline should be original and summarize the event, not copy any single source's headline.
- The AI's tone is confident and direct. No hedging. State facts and implications clearly.

---

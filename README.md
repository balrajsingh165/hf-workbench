# Heurist Finance Workbench

A prototype for **Heurist Finance** — a thesis-driven finance app for retail stock and commodity traders with multi-day to multi-week holding horizons.

The core idea: instead of surfacing more data, the app helps traders form, track, and stress-test their own market convictions. The atomic unit is a **thesis** — a single declarative market belief that the system monitors, scores, and keeps honest over time.

## Setup

```bash
# 1. Install dependencies
uv sync

# 2. Set API keys in ~/.env
HEURIST_API_KEY=...
EXA_API_KEY=...
FIRECRAWL_API_KEY=...
GEMINI_API_KEY=...
WEB_SEARCH_PROVIDER=firecrawl  # firecrawl or exa

# 3. Initialize (or reset) the database
uv run python db/schema.py
```

> **Note:** `db/schema.py` drops and recreates all tables — run it once on fresh setup, or any time the schema changes.

## News ingestion

The current pipeline is firehose → cluster → story. Raw RSS items are stored as
single-source `news` rows, clustered by event, and only promoted clusters get a
Gemini synthesis call into `story` rows.

```bash
# Poll all configured firehose feeds and shadow-cluster passing items.
uv run python -m agents.firehose

# Parse and gate without writing.
uv run python -m agents.firehose --dry-run --max-items 50

# Preview route decisions.
uv run python -m agents.route_news_clusters --dry-run --limit 50

# Promote eligible clusters and write stories.
uv run python -m agents.route_news_clusters --write --top 20 --limit 200

# Auto-label new stories for the user-facing quality filter.
uv run python -m agents.judge_stories --limit 50
```

Sources: press wires, macro/regulatory feeds, Tier1 majors, trade press, and
regional feeds defined in `agents/firehose.py`. Synthesis uses Gemini
(`GEMINI_API_KEY`). Output lives in `story` plus
`global/stories/story_XXX.md`; raw `news` rows remain citation targets.

See `docs/news-story-pipeline.md` for architecture and
`docs/news-rearchitecture-runbook.md` for operator commands.

## Pipeline scheduler (pm2)

Production runs the HTTP API and pipeline as **two pm2 apps** (`ecosystem.config.cjs`):

```bash
pm2 start ecosystem.config.cjs   # hf-workbench (API) + hf-pipeline (scheduler)
pm2 restart ecosystem.config.cjs
```

`hf-pipeline` runs `uv run python -m agents.pipeline_scheduler` (default **3h** via
`HF_PIPELINE_INTERVAL_HOURS`, plus 10‑min firehose). Each cycle: news ingest,
thesis–story matching, scoring, daily brief. Logs: `logs/hf-scheduler.log`;
metrics: `logs/hf-pipeline-metrics.jsonl`.

Local-only (no pm2):

```bash
uv run python -m agents.pipeline_scheduler
```

```bash
# One cycle only, useful for smoke tests.
uv run python -m agents.pipeline_scheduler --once

# Start the scheduler but wait for the first interval instead of running immediately.
uv run python -m agents.pipeline_scheduler --no-initial
```

## Thesis match index

Build the thesis-side retrieval index from markdown. This embeds only the
semantic units we care about for matching:
- one `statement` chunk per thesis (`title + core thesis`)
- one chunk per invalidation condition
- no `Signals` embeddings

```bash
# Preview how many chunks will be indexed
uv run python -m agents.build_match_index --kind thesis --dry-run

# Build dense embeddings + FTS5 rows
# Fixed config: gemini-embedding-2-preview at 1536 dimensions
uv run python -m agents.build_match_index --kind thesis

# Probe the index with a news-like query
uv run python scripts/query_thesis_match_index.py "core inflation surprised higher and Fed cuts get pushed out"
```

## Verify output

```bash
# Read the latest article
cat global/news/news_006.md

# List all articles with DB metadata
uv run python -c "
import sqlite3, json
conn = sqlite3.connect('db/hf.db')
for row in conn.execute('SELECT id, sources_json, tickers_json, published_at FROM news ORDER BY created_at DESC'):
    print(row[0], '|', json.loads(row[1]), '|', json.loads(row[2]), '|', row[3])
"

# Check images for a story
ls global/news/news_006/images/
```

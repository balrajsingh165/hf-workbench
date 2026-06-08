# SOP: Schema & Data Changes — What to Rebuild

When you change the schema, the markdown format, the embedding model, or the judge prompt, **don't default to wiping everything.** This SOP is the decision tree for what actually needs to be rebuilt.

---

## Cost of each rebuild step

| Step | How to run | Cost | What it loses if skipped |
|---|---|---|---|
| DB table recreate | `uv run python -c "from db.schema import init_db; init_db(tables=[...])"` | seconds | New schema not applied |
| Reseed structured rows | `uv run python scripts/seed_theses_from_markdown.py` | seconds | Stale thesis rows, or rows missing after a wipe |
| Rebuild dense indexes | `uv run python -m agents.build_match_index --kind thesis` / `--kind story` | ~1 min + Gemini embedding calls | Retrieval returns stale or empty results |
| Re-run matching | `uv run python -m agents.match_story_for_thesis --thesis thesis_XXX` per thesis | minutes + Gemini judge calls (the most expensive stage) | `thesis_story_links` is stale or empty |
| Recompute scores | `uv run python -m agents.score_theses` | seconds | `theses.score*` is stale |

Rule of thumb: **embeddings and `thesis_story_links` are the expensive state.** Preserve them whenever the change doesn't touch them.

---

## Decision tree — what triggers what

### 1. You added a **new column** or **new table** or **new index**

Additive changes don't need a wipe at all.

- **New column, nullable:** run a one-liner `ALTER TABLE <t> ADD COLUMN <name> <type>` directly, then update `db/schema.py` to match. No reseed. No rebuild.
- **New column, NOT NULL with default:** same, but include `NOT NULL DEFAULT <value>` in the ALTER.
- **New table:** either add the DDL to `db/schema.py` and run `init_db(tables=['<new_table>'])`, or ship a `CREATE TABLE IF NOT EXISTS` helper in `src/` (the pattern already used by `story_links.py`, `thesis/match_index.py`, `story/match_index.py`).
- **New index:** add it to the `INDEXES` tuple in `db/schema.py` and run any `init_db()` call (indexes use `CREATE INDEX IF NOT EXISTS` and are always safe to re-run).

Nothing else rebuilds. Embeddings survive. Matches survive.

### 2. You **renamed** or **dropped** a column on **one table**

Use targeted wipe. Only that one table dies.

```bash
# 1. Edit db/schema.py to the new shape.
# 2. Wipe + recreate just that table.
uv run python -c "from db.schema import init_db; init_db(tables=['user_theses'])"
# 3. Reseed rows for the wiped table when markdown is the source.
uv run python scripts/seed_theses_from_markdown.py   # if user_theses / theses / users
# 4. Update any Python code that read or wrote the renamed column.
```

Embeddings untouched. `thesis_story_links` untouched. Scoring can be re-run when ready.

**Watch out for FKs.** If you drop a column that's referenced by another table, you need both tables in the `tables=[...]` list. `init_db` drops in reverse schema order for FK safety.

### 3. You changed the **markdown format** for stories or theses

The markdown files are the source of truth, so the DB index needs to catch up.

- **Add a new markdown field that isn't indexed in DB:** no rebuild. Markdown parsers (`src/story/docs.py`, `src/thesis/docs.py`) may need updates if they extract that field.
- **Change a field that the DB indexes** (e.g. story tickers, story sectors, invalidation bullet format): update the affected structured table from the story/thesis source of truth, and if the change affects embedded text, rebuild that side's dense index.
- **Change a field that the embeddings incorporate** (e.g. you rewrite how invalidations appear in thesis chunk text): rebuild the dense index for the affected side. `thesis_story_links` judgments are now stale because they were made against different chunk text — plan a re-run of matching where the prompt or chunk meaning actually changed.

### 4. You changed the **embedding model** or **embedding dimensionality**

Full rebuild of the affected dense index. Old vectors can't be compared to new ones.

```bash
uv run python -m agents.build_match_index --kind thesis
uv run python -m agents.build_match_index --kind story
```

Update the `*_EMBEDDING_MODEL` / `*_EMBEDDING_DIMENSIONALITY` constants in `src/thesis/match_index.py` and `src/story/match_index.py` before rebuilding.

`thesis_story_links` doesn't *have* to be rebuilt — the retrieval step feeds the judge, the stored rows are the judge's output. But retrieval scores stored in existing rows (`retrieval_score`, `best_chunk_key`) will reference an old embedding space, so treat them as diagnostic-only until you re-run matching.

### 5. You changed the **judge prompt** or **judge schema** in `src/thesis/story_judge.py`

No DB change. Re-run matching on a handful of theses to verify the change moves things in the intended direction; leave the rest alone. Bulk re-matching is expensive and usually unnecessary — the point of ad-hoc spot checks.

```bash
uv run python -m agents.match_story_for_thesis --thesis thesis_003
uv run python -m agents.match_story_for_thesis --thesis thesis_005
```

Backfill rerun will overwrite existing `source='backfill'` rows for the thesis. Ingest rows survive.

### 6. You're making a **big destructive move** (schema redesign, corpus overhaul)

Default `init_db()` with no args. Reseed. Rebuild embeddings. Re-run matching. This is the only case where the everything-wipe is correct.

```bash
uv run python db/schema.py
uv run python scripts/seed_theses_from_markdown.py
uv run python -m agents.build_match_index --kind thesis
uv run python -m agents.build_match_index --kind story
# optionally, per-thesis:
uv run python -m agents.match_story_for_thesis --thesis <id>
```

Expect this to take a few minutes total, mostly Gemini calls.

---

## Invariants — things this SOP protects

1. **Embeddings survive schema iteration.** `thesis_match_chunks` and `story_match_chunks` are expensive to rebuild (Gemini cost) and have no dependency on unrelated schema changes. Default to preserving them.
2. **`thesis_story_links` survives schema iteration on other tables.** Judge output is the most expensive state in the system. Lose it only when you truly have to.
3. **Seed scripts are idempotent.** `INSERT OR REPLACE` in both seeders means re-running after a partial wipe is safe. If a seeder ever becomes destructive, fix the seeder rather than inventing another workaround.
4. **Markdown is the source of truth.** Any DB wipe is recoverable as long as the markdown files exist. That's the whole point of the architecture.

---

## Quick reference

| I changed... | Command |
|---|---|
| New column on `user_theses` | `ALTER TABLE user_theses ADD COLUMN ...` |
| Renamed a column on one table | `init_db(tables=['<table>'])` + reseed that table |
| A thesis markdown file | `seed_theses_from_markdown.py` (+ `agents.build_match_index --kind thesis` if the change affects chunk text) |
| A story markdown file | `agents.build_match_index --kind story` if the change affects embedded story text |
| Embedding model constant | `agents.build_match_index --kind thesis` + `--kind story` |
| Judge prompt | Nothing automatic. Re-run `match_story_for_thesis` on 1–2 theses for a spot check. |
| Something I can't categorize | Default to `init_db()` + reseed + rebuild indexes. Cheap enough. |

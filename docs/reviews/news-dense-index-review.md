# Review: News Dense Index + Supporting Changes

**Scope:** `src/news/match_index.py`, `src/news/docs.py`, `db/schema.py` (news_match_chunks), `src/news/persist.py` (ingest hook), `scripts/build_news_match_index.py`, chunk-win logging in `agents/match_thesis_for_news.py`.

**Verdict:** Ship as-is. Two items worth fixing soon (duplicated helpers, task-type question); the rest are observations for later.

---

## What's good

1. **Schema is clean.** One row per news article, PK is `news_id` with FK to `news(id)`, no AUTOINCREMENT. Correct simplification from the thesis side — news has no multi-chunk decomposition, so the single-row design is right.

2. **Upsert path is correct.** `INSERT … ON CONFLICT(news_id) DO UPDATE` handles both fresh inserts and re-embeds on edit. The thesis side doesn't have this — if thesis chunks ever need re-indexing, only the full rebuild (`DELETE` + re-INSERT) works. News got the better pattern here.

3. **Ingest hook is non-fatal.** The `try/except` around `upsert_news_match_row` in `write_story` means a Gemini outage doesn't block news persistence. The CLI rebuild covers the gap. Correct trade-off for a prototype.

4. **`news_docs.py` extracts shared parsing.** The matcher and the index now share `parse_news_markdown` / `NewsDocument` instead of duplicating regexes. This directly addresses the deferred refactor item from the spike doc.

5. **Chunk-win logging is useful and cheap.** Stderr-only, includes `unrelated` candidates — exactly what's needed to distinguish retrieval-miss from judge-miss during eval. No schema change required.

6. **`query_text` as a property.** `NewsDocument.query_text` composes title + bullets lazily. Good: it means the embedding input is always consistent between build and search paths.

---

## Issues

### 1. Duplicated `_cosine_similarity` and `get_db_connection` — low urgency

`news_match_index.py` and `thesis_match_index.py` both define identical `_cosine_similarity` and `get_db_connection` functions. Not a correctness problem, but a maintenance one — if someone later switches to a faster cosine impl (numpy dot on normalized vectors), they'd need to change it in two places.

**Suggestion:** Extract both into a shared module (e.g. `src/embedding_utils.py` or just add to `src/gemini.py`) when a third consumer appears. Not worth doing now for two call sites.

### 2. `RETRIEVAL_DOCUMENT` task type for the news index — verify this is intentional

The news index embeds articles with `task_type="RETRIEVAL_DOCUMENT"`. The thesis index also uses `task_type="RETRIEVAL_DOCUMENT"`. Both search functions query with `task_type="RETRIEVAL_QUERY"`. This is correct for the **current** use case where the query is a free-text string (e.g. "Fed pivot delayed past Q3") searching against stored documents.

But the spike doc's backfill direction is **thesis-as-query against the news index** — i.e., a thesis statement queries the news embedding space. A thesis is a document, not a user query. Gemini's `RETRIEVAL_QUERY` vs. `RETRIEVAL_DOCUMENT` task types produce asymmetric embeddings optimized for the query→document direction. Using `RETRIEVAL_QUERY` to embed a thesis statement when searching the news index may slightly degrade recall vs. embedding it with `SEMANTIC_SIMILARITY` or `RETRIEVAL_DOCUMENT` and using symmetric cosine.

**Suggestion:** When the backfill agent lands, test both task types on the eval set and pick the winner. If thesis-as-query recall is weak, this is the first knob to turn.

### 3. `_extract_sections` regex requires `\n` after `##` header — fragile

Both `news_docs.py` and `thesis_docs.py` use `SECTION_RE = re.compile(r"^## (?P<name>.+?)\n", re.MULTILINE)`. This requires a newline immediately after the section header text. A markdown file with a `## Heading` on the last line, or a file with `\r\n` line endings, would silently miss sections. Not a live bug (all current files are well-formed), but a latent one.

**Suggestion:** Change to `r"^## (?P<name>.+?)\s*$"` with `re.MULTILINE` in both files, matching the pattern already used by `NEWS_TITLE_RE` and `THESIS_TITLE_RE`.

### 4. `rebuild_news_match_index` sets `max_workers=batch_size` — potentially excessive

```python
batch_embed_contents(
    text_batches,
    ...
    max_workers=max(1, batch_size),
)
```

With default `batch_size=32`, this spawns 32 threads. Each `text_batch` is a single-element list (one article), so this is 32 concurrent single-article embedding calls. The thesis side does the same thing. At 25 articles this is fine; at 500 it'll spike thread count and may hit Gemini rate limits before the `GeminiRateLimiter` kicks in (the limiter has no default RPM set unless the caller passes one).

**Suggestion:** Cap `max_workers` at 8–16 and rely on the rate limiter if you're concerned about burst. Or just leave it — the rebuild is a batch script, not a hot path.

### 5. No `_load_all_news_documents` error handling for malformed files

If any single markdown file in `global/news/` is malformed (missing title or overview), `parse_news_markdown` raises `ValueError` and the entire rebuild aborts. The thesis side has the same behavior. For a prototype this is fine (fail loud, fix the file), but worth noting.

---

## Observations (no action needed)

- **Schema drop order.** `schema.py` drops tables in reverse dict order. With `news_match_chunks` referencing `news(id)`, the FK ordering is `news_match_chunks` → `news`. Reverse of `TABLES` dict puts `news_match_chunks` before `news` in the drop sequence, which is correct. If a future table references `news_match_chunks`, this still works because SQLite doesn't enforce FK constraints during DROP by default.

- **Embedding dimensionality is consistent.** Both modules use 1536d from the same constant pattern. Good.

- **`batch_embed_contents` sends one text per API call.** The `text_batches = [[doc.query_text] for doc in documents]` pattern wraps each article in its own list. This means N API calls for N articles, not batched multi-text calls. The Gemini embedding API supports multiple texts per call (up to 2048 per the docs). Batching 8–16 texts per call would reduce round-trips ~10x. Not a problem at 25 articles; matters at 500/day.

---

## Summary

The change is structurally sound and mirrors the thesis index correctly while simplifying where appropriate (no chunk decomposition, PK instead of AUTOINCREMENT). The ingest hook and the parsing extraction are both clean. The main forward-looking question is whether `RETRIEVAL_QUERY` is the right task type for the thesis-as-query backfill direction — worth a quick ablation when that agent lands.

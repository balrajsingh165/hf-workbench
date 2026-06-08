# SOP: Remove a Thesis or Story

## When to remove

- **Thesis:** title or content fails the quality bar (verbose, hedged, event-anchored, no durable belief). Don't polish — delete and let the pipeline regenerate.
- **Story:** story is not relevant to finance, economics, world politics, or market-affecting events (sports, entertainment, celebrity, local crime, product rumours with no market thesis).

---

## Removing a thesis

```python
import sqlite3

THESIS_ID = "thesis_013"
conn = sqlite3.connect("db/hf.db")
with conn:
    conn.execute("DELETE FROM thesis_story_links  WHERE thesis_id = ?", (THESIS_ID,))
    conn.execute("DELETE FROM thesis_match_chunks WHERE thesis_id = ?", (THESIS_ID,))
    conn.execute("DELETE FROM entity_tickers      WHERE entity_type='thesis' AND entity_id = ?", (THESIS_ID,))
    conn.execute("DELETE FROM user_theses         WHERE thesis_id = ?", (THESIS_ID,))
    conn.execute("DELETE FROM theses              WHERE id = ?", (THESIS_ID,))
conn.close()
```

Then delete the markdown:

```bash
rm global/theses/{thesis_id}.md
```

---

## Removing a story

```python
import sqlite3

STORY_ID = "story_049"
conn = sqlite3.connect("db/hf.db")
with conn:
    conn.execute("DELETE FROM thesis_story_links WHERE story_id = ?", (STORY_ID,))
    conn.execute("DELETE FROM story_match_chunks WHERE story_id = ?", (STORY_ID,))
    conn.execute("DELETE FROM entity_tickers     WHERE entity_type='story' AND entity_id = ?", (STORY_ID,))
    conn.execute("DELETE FROM story              WHERE id = ?", (STORY_ID,))
conn.close()
```

Then delete the markdown:

```bash
rm global/stories/{story_id}.md
```

Legacy news-image references are no longer part of the schema. If you are
removing historical data from a branch that still had uploaded news images,
delete the corresponding `news/{news_id}/images/...` objects from R2 manually.

---

## Bulk removal

For multiple IDs, use `IN (?, ?, ?)` with a list. See `scripts/test_discover_titles.py` for a worked example of batch deletion.

---

## After removal

- No index rebuild needed — `thesis_match_chunks` or `story_match_chunks` rows are deleted above.
- The pipeline will regenerate system theses from the remaining story corpus on its next run.

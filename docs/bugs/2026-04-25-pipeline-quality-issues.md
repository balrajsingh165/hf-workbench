# Pipeline quality bugs — 2026-04-25

Found during review of overnight scheduler run (run_id `34966f804430`, fired 12:35 UTC).

---

## BUG-001 — Off-topic URLs polluting `## Sources` in news files

### Symptom
News files written by the latest ingest run contain unrelated articles in their `## Sources` list, even though the `## Overview` body itself is clean. Examples from this run:

| News file | Topic | Off-topic source pulled in |
|---|---|---|
| `news_055` | Alcaraz French Open withdrawal | Guardian — children's noma disease |
| `news_056` | Netanyahu prostate cancer | NYT — Europe's Ukraine war strategy |
| `news_057` | U.S. soldier Polymarket insider trading | NYT — Europe's Ukraine war strategy |
| `news_058` | DeepSeek V4 release | BBC — armed group attacks in Mali |

### Root cause
`src/news/ingest.py`:
- `enrich_with_search_if_needed` (line 436) calls firecrawl/web search when cheap sources are below the quality floor, then merges the new refs into `collected.items`.
- The merged list is sorted by `_source_quality_score` (line 422), which keys on `(has_body, body_length, is_tier1)` only. There is no topic-relevance filter.
- A tier-1 publisher with a long extracted body but an unrelated topic therefore outranks a relevant snippet from a smaller publisher and survives the top-N truncation.

The synthesis prompt apparently sticks to the original cheap sources for the Overview, so the contamination is currently confined to the Sources list at the bottom of each markdown file — but it is visible to the user and undermines trust in the citations.

### Proposed fix
Add a relevance gate to `enrich_with_search_if_needed` before merging:
1. Embed the topic/headline once.
2. For each firecrawl addition, compute cosine similarity against the topic embedding using the body (or title if body is empty).
3. Drop additions below a threshold (start at ~0.55, tune on the four known-bad cases above).
4. Log the drop count in `collected.notes` for observability (e.g. `firecrawl: filtered 2/6 off-topic additions`).

Secondary: also include topic-similarity as a term in `_source_quality_score` so that even retained additions are ordered by relevance, not just publisher tier.

### Verification
Re-run `agents.ingest_news --top 5 --no-images` against a fixture set including the four affected stories above and confirm none of the listed off-topic URLs appear in the output. Add a unit test in `tests/news/` that feeds known-irrelevant search hits into `enrich_with_search_if_needed` and asserts they are filtered.

---

## BUG-002 — Daily brief cites unrelated news in theme provenance

### Symptom
`global/briefs/2026-04-25.md` line 15, theme 03:

> Equity markets are hitting fresh record highs as the S&P 500 (SPY) rises 0.62% and the Nasdaq (QQQ) gains 1.32%, fueled by the removal of the DOJ probe into Jerome Powell which clears the path for Kevin Warsh's Fed confirmation.
> _Sources: news_051, news_040_

`news_040` is the Powell-probe story (correct citation). `news_051` is the Michael Jackson biopic opening weekend — entirely unrelated to the equity-rally / Fed narrative. The synthesizer hallucinated provenance.

### Root cause (suspected)
`src/brief/pipeline.py` stage-3 ("verifying provenance") logged `[stage 3] verifying provenance…` but still passed the bad citation through. Either:
- The verifier checks that cited IDs *exist* but does not check that the cited article actually supports the theme's claim, or
- The verifier's similarity threshold is too lax for the brief synthesis prompt's tendency to over-cite.

Needs a code read of the stage-3 verifier in `src/brief/pipeline.py` to confirm.

### Proposed fix
1. Read stage 3 in `src/brief/pipeline.py` and confirm what "verify provenance" currently checks.
2. Tighten verification: for each `(theme, cited_news_id)` pair, embed the theme statement and the news headline+overview, require cosine similarity above a threshold (start ~0.45). Drop citations that fail.
3. If a theme ends up with zero verified citations, drop the theme from the brief rather than emitting it with a bad cite.
4. Log dropped citations to stderr so we can monitor false-positive rate.

### Verification
Regenerate `global/briefs/2026-04-25.md` with `agents.daily_brief --force` and confirm theme 03 no longer cites `news_051`. Add a unit test that constructs a synthetic theme + a pool of news, including obvious off-topic ones, and asserts the verifier filters them.

---

## BUG-003 — `mesh` `news_search` consistently times out at 15s

### Symptom
Every cheap-source collection step in this run logged:

```
[note] mesh: Tool 'news_search' timed out after 15s
[note] firecrawl: used as mesh fallback
```

Out of 15 stories collected, 6 hit this timeout. Firecrawl fallback works correctly so there is no functional regression, but mesh is contributing zero value while still costing 15s of wall time per affected story.

### Root cause
Unknown — needs investigation. Either mesh's `news_search` is genuinely slow under current load, the timeout is set too aggressively, or the mesh endpoint is degraded.

### Proposed fix
1. Identify where the 15s timeout is configured (likely `src/news/cheap_sources.py` or a mesh client wrapper).
2. Time a few mesh `news_search` calls in isolation to see whether they actually return given a longer budget (60s) or fail outright.
3. If mesh is genuinely down or chronically slow: short-circuit it (skip mesh, go straight to firecrawl) until it recovers, gated behind a config flag so we can re-enable easily.
4. If mesh just needs more time: bump the timeout. But weigh that against per-story latency budget — the ingest step already takes 5+ minutes.

### Verification
Re-run the pipeline and confirm either (a) mesh succeeds within budget on most calls, or (b) mesh is skipped and per-story collection time drops by ~15s on stories that previously timed out.

---

## BUG-004 — Single judge response unparseable even after retry ✓ FIXED

### Symptom
One log line in this run:
```
warning: could not parse judge response for news=news_053 thesis=thesis_010; skipping.
last_text='{"relation":"supports","confidence":0.65,"matched_invalidation":null,"rationale":"The nomination of a new Fed'
```

The two-attempt retry budget `((500, "low"), (1024, "low"))` was still truncating the rationale mid-sentence.

### Fix applied (2026-04-25)
Removed the retry ladder. `src/thesis/news_judge.py` now uses `attempts = ((2048, "low"),)` — a single call at 2048 tokens, which is sufficient for any realistic rationale.

### Verification
Re-run and confirm no parse warnings on next 10+ pipeline runs.

---

## BUG-005 — Near-duplicate articles ingested across pipeline runs

### Symptom
Two stories covering the same event appear as separate news files after runs that fired hours apart:

| Files | Shared event |
|---|---|
| `news_060`, `news_064` | "Iran Rejects Direct U.S. Talks in Islamabad" |
| Two WHCD files (different runs) | "Trump to Attend White House Correspondents' Dinner" |

Each Particle scrape surfaces the same ongoing story with a slightly different subheadline (e.g. "Pakistan Mediates Ceasefire Extension" vs "Pakistan Hosts Diplomatic Delegations Under Lockdown"), so `_dedupe_by_headline` in `agents/ingest_news.py` passes both through.

### Root cause
Dedup happens at discovery time using an exact headline match against the current batch only. There is no cross-run check against already-persisted news files. Stories that evolve their subheadline between scrapes bypass dedup entirely.

### Proposed fix
Before persisting a new article, check whether an article covering the same event already exists in the DB:
1. Embed the new headline + subtitle.
2. Query the `news` table for articles from the last N days (N = 2 is probably enough for "breaking" stories).
3. Compute cosine similarity between the new embedding and stored ones.
4. Skip persist if any existing article exceeds a threshold (start at ~0.85).

Alternative (simpler): exact-match on the first 60 chars of the Particle headline against `news.title` in the DB — catches most "same story, different subtitle" cases without embedding overhead.

### Verification
Run two consecutive ingest cycles against a fixture set that includes the same Iran-talks story with different subtitles. Confirm only one article is persisted.

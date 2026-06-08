#!/usr/bin/env python3
"""Rebuild docs/report-ambiguous-unclear-stories.md from story_quality_label rows."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "hf.db"
OUT = ROOT / "docs/report-ambiguous-unclear-stories.md"

# PM-facing reference — keep in sync manually when pipelines change materially.
# (Triple-quoted with ''' so embedded judge code can show f"""…""" examples.)
_PM_GUIDE = r'''
---

## Senior PM guide: `unclear` stories, promotion, and ticker logic

### 1) How `unclear` tagging works today

**Separate from cluster promotion.** A row can reach `story` (cluster passed `sharp_promote` + synthesis) and still receive `story_quality_label = unclear`. That hide only affects **feed surfaces** filtered on quality (see §2).

**Who labels.** `agents/judge_stories.py` batches **recent stories missing** an `auto:gemini-judge` row, calls **`generate_text_with_retry`** with **`GEMINI_3_FLASH_PREVIEW`**, strict JSON (`label` ∈ `good` | `unclear` | `no_value`), Gemini 3 default temperature (not customized), `thinking_level="medium"`:

```113:139:agents/judge_stories.py
def _judge(row: sqlite3.Row) -> tuple[str, str]:
    prompt = f"""{RUBRIC}

Story to judge:

headline: {row['headline']}
what_changed: {row['what_changed'] or ''}
overview_json: {row['overview_json']}
market_relevance_json: {row['market_relevance_json']}
sectors_json: {row['sectors_json']}
regions_json: {row['regions_json']}

Return JSON only.
"""
    res = generate_text_with_retry(
        prompt,
        model=GEMINI_3_FLASH_PREVIEW,
        response_mime_type="application/json",
        response_json_schema=SCHEMA,
        thinking_level="medium",
    )
    data = json.loads(res.text)
    return (
        str(data["label"]),
        str(data.get("rationale") or ""),
    )
```

**Rubric cheat-sheet** (canonical text is `RUBRIC` in the same module — embeds fixed “mid-2026 world state”, hallucination thresholds, and product expectations):

```5:109:agents/judge_stories.py
  unclear   — market angle exists but is thin, vague, or has factual issues
              that aren't obviously hallucinated.
  no_value  — hallucinated, factually broken, citation broken, or no
              tradeable angle (humanitarian-only event, fluff).
…
When in doubt between `unclear` and `no_value`, use `unclear`. The product
hates false-negative hallucinations less than it hates a reviewer having to
re-read fine stories that the judge wrote off.
…
   - unclear  : factual inconsistency that's not obviously hallucinated, or
                thin angle with weak market relevance.
```

**Operational notes for PM.**

- **`--rejudge`** clears all `auto:gemini-judge` rows before re-running (destructive QA path).
- **Default labeling budget:** scheduler runs `agents.judge_stories --limit max(top_stories*2, 30)` per pipeline (`agents/pipeline_scheduler.py`).

**Which stories get labeled in one run** — only rows **without** an existing `auto:gemini-judge` label, newest first, capped by `--limit`:

```162:173:agents/judge_stories.py
        rows = conn.execute(
            """
            SELECT s.*
            FROM story s
            LEFT JOIN story_quality_label auto
              ON auto.story_id = s.id AND auto.labeler = 'auto:gemini-judge'
            WHERE auto.story_id IS NULL
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
```

Persisted schema: table `story_quality_label` (`labeler`, `label`, `rationale`, `labeled_at`) — unique on `(story_id, labeler)`.

---

### 2) Related pipeline gates (beyond the Gemini judge)

**End-to-end scheduler order** (full pipeline):

```271:336:agents/pipeline_scheduler.py
def run_pipeline(config: SchedulerConfig) -> dict[str, Any]:
…
    ingest = _run_command("route_news_clusters", ingest_cmd, run_id=run_id)
…
    judge = _run_command("judge_stories", judge_cmd, run_id=run_id)
…
    for thesis_id in thesis_ids:
        cmd = _python_module(
            "agents.match_story_for_thesis",
…
    score = _run_command("score_theses", _python_module("agents.score_theses"), run_id=run_id)
    brief = _run_command("daily_brief", _python_module("agents.daily_brief", "--force"), run_id=run_id)
```

**Cluster → story (`write_cluster_story`).** Up to **3** cluster members (tier-1 / materiality ordering), optional **body enrichment**, an embedding-based **coherence gate** that prunes outlier members before synth (so Frankenstein clusters can't form), **per-cluster ticker candidate slate** built from member bodies + the registry, **Gemini cluster synthesis** constrained to the slate, **deterministic verification** (slate membership + alias-gated `evidence_span` + verbatim quote substring), then DB + markdown write. **Post-synth ticker backfill stages are gone** — the LLM is the discovery layer, the slate is its closed universe, the verifier holds the line.

**Home feed filter** — why `unclear` disappears from product:

```425:447:api.py
def build_feed_stories(conn) -> tuple[list[dict], dict[str, dict]]:
    """Fetch the home-feed story list and the source-color metadata it needs.

    Stories are the unit of the home feed (one synthesized writeup per
    cluster). Stories labeled `unclear` or `no_value` are hidden. Hard cap at
    HOME_FEED_LIMIT keeps the /api/home payload bounded.
    """
    story_rows = conn.execute(
        """
        SELECT s.id, s.cluster_id, s.created_at, s.headline, s.overview_json,
…
        WHERE s.id NOT IN (
          SELECT story_id FROM story_quality_label
          WHERE label IN ('unclear', 'no_value')
        )
```

**Cluster promotion routing (Stage A).** If a cluster never clears `route_cluster` rules (R0–R8 in `src/news/routing.py`), it never reaches synthesis — unrelated to `unclear`. Default sink:

```252:252:src/news/routing.py
    return Decision("firehose_store", "default firehose route")
```

---

### 3) Ticker generation pipeline (slate-constrained synth + deterministic verification)

**A. Candidate slate (`src/news/ticker_candidates.py:build_ticker_candidates`).** Per-cluster closed universe built before synth, priority order:
1. Cluster's routing-attached tickers (already validated upstream).
2. Explicit `$NVDA` / `NASDAQ:NVDA` mentions in member bodies (registry-bounded).
3. Long-form alias hits in titles + first 6K body chars (e.g. "Powell Industries", **not** "Powell" — `short` is excluded from the alias index because it was the source of every Powell/Monday/Constellation name-collision bug).

The slate is rendered as `- SYMBOL — Display | aliases: [...]` lines in the synth prompt and passed to the verifier as `allowed_symbols` + `ticker_aliases`.

**B. Synthesis prompt rules (Gemini Flash, `thinking_level="low"`, default temperature)** — slate-constrained tickers, `evidence_span` must be a verbatim alias, max **6** Yahoo-form symbols, macro stories **should** emit **no** equity tickers:

```264:269:src/news/synthesis.py
- market_relevance.tickers uses Yahoo-form symbols only, max 6, **chosen exclusively from the candidate slate above**.
…
Ticker selection (slate-constrained, evidence-anchored, verification rejects unsupported entries):
- Choose `symbol` from the candidate slate above and nowhere else.
- Set `evidence_span` to the verbatim alias from the symbol's `aliases` list as it appears in the cited body.
- Macro stories (rates, inflation, currency, geopolitics, central-bank policy) usually have NO individual-name tickers.
```

**C. Verifier (`src/news/verifier.py:verify_story_payload`)** — runs **before** flatten, gates four ways per ticker object:
1. Yahoo symbol shape (`YAHOO_SYMBOL_RE`).
2. **Slate membership**: `symbol ∈ allowed_symbols` (rejects off-slate hallucinations).
3. `source_doc_id ∈ member_ids` and `evidence_span` is a substring of the cited body.
4. **Alias gate**: `evidence_span` must case-insensitively contain one of the symbol's registry aliases. Blocks the Jerome-Powell → POWL class deterministically.

**What the second Gemini (`judge_stories`) sees.** After verification, `flatten_market_relevance` collapses tickers/sectors/regions to plain strings for storage and downstream joins. With the slate + alias gates in place, the `GOOGL` / `MNDY` / `POWL` noise patterns at the top of this report should no longer reach the judge — remaining `unclear` rationales now reflect either (a) genuine narrative quality issues (logic contradictions, thin synth) or (b) registry coverage gaps (manufacturer not on the slate → `tickers: []` instead of wrong staples).

---
'''


THEMES: list[tuple[str, re.Pattern[str]]] = [
    ("combined_unrelated_events", re.compile(r"unrelated|two completely|combine[s]? two|attempts to combine|combines .* unrelated", re.I)),
    ("ticker_noise_mndy", re.compile(r"\bMNDY\b|monday\.com", re.I)),
    ("ticker_noise_googl", re.compile(r"\bGOOGL\b|Alphabet(?! Records)", re.I)),
    ("tickers_clutter_or_weak_macro", re.compile(
        r"ticker (tagging|selection|relevance).*(macro|weak|generic|questionable)|(macro|rates|bond).*(ticker|tagging)|clutter",
        re.I,
    )),
    ("logic_contradiction_or_tension", re.compile(r"inconsisten|contradict|logical tension|logically inconsist|incoheren", re.I)),
    ("single_source_thin", re.compile(r"single source|thin for a retail|extremely thin", re.I)),
    ("extreme_claim_or_tick_hallucination", re.compile(r"halluci|improbab|POWL|BTGO|mechanism of such", re.I)),
]


def classify(rationale: str) -> list[str]:
    if not rationale:
        return ["unclassified_empty"]
    hits = [n for n, rx in THEMES if rx.search(rationale)]
    return hits or ["other_unclear_bucket"]


def _trunc(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def mq(text: str) -> str:
    return text.replace("\n", "\n> ")


def main() -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.id, s.created_at, s.cluster_id, s.headline, s.what_changed,
               l.rationale, l.labeled_at, l.labeler
        FROM story_quality_label l
        JOIN story s ON s.id = l.story_id
        WHERE l.label = 'unclear'
          AND s.kind = 'story'
        ORDER BY s.created_at DESC
        """
    ).fetchall()
    conn.close()

    counts: Counter[str] = Counter()
    for r in rows:
        for t in classify(str(r["rationale"])):
            counts[t] += 1

    chunks: list[str] = []
    for r in rows:
        rat = (r["rationale"] or "(none)").strip()
        wc = _trunc(str(r["what_changed"]), 900)
        chunks.append(
            f"""### `{r["id"]}` — {r["created_at"]}

- **Cluster:** `{r["cluster_id"]}`
- **Labeled:** {r["labeled_at"]} (`{r["labeler"]}`)
- **Headline:** {r["headline"]}

**Judge rationale (verbatim):**

> {mq(rat)}

**What changed (snippet):**

> {mq(wc)}

---
"""
        )

    def table_rows(c: Counter[str]) -> str:
        return "\n".join(f"| `{k}` | **{v}** |" for k, v in c.most_common())

    md = f"""# Report: Ambiguous story quality (“unclear” labels)

_Data source:_ `story_quality_label` ∪ `story` · _Regenerate:_ `uv run python scripts/regenerate_ambiguous_story_report.py`

{_PM_GUIDE}

## Automated appendix: theme scan + per-story dump

The tables below are **machine-derived** from current `unclear` rationales (regex tagging for PM triage). The per-story blocks are verbatim judge notes from `auto:gemini-judge`.

## Summary

| `unclear` count | Theme regex (non-exclusive hits) |
|-----------------|--------------------------------|
| **{len(rows)}** | see table below |

### Recurring rationales

| Theme | Matches |
|-------|--------|
{table_rows(counts)}

## Appendix — each story (`unclear`)

{chr(10).join(chunks)}
_End._
"""
    OUT.write_text(md, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes) rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

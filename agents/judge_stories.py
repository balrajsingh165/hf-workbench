#!/usr/bin/env python3
"""Auto-label unlabeled stories with the Heurist story-quality rubric.

Story-quality axis (label):
  good      — well-sourced, citation-integrity intact, sectors/tickers correct.
              Stories organize context for a thesis-driven trader.
  unclear   — market angle exists but is thin, vague, or has factual issues
              that aren't obviously hallucinated.
  no_value  — hallucinated, factually broken, citation broken, or no
              tradeable angle (humanitarian-only event, fluff).

Theses are produced separately by `discover_thesis()` over the same context;
they are not a per-story field and are not part of this rubric.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients.gemini import GEMINI_3_FLASH_PREVIEW, generate_text_with_retry

DB_PATH = ROOT / "db" / "hf.db"

SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["good", "unclear", "no_value"]},
        "rationale": {"type": "string"},
    },
    "required": ["label", "rationale"],
}

RUBRIC = """\
You are judging a synthesized market story for Heurist Finance, a
thesis-driven trading workbench. The product surface is the home feed where
stories appear to a retail trader running multi-day to multi-week holds.

CRITICAL — current world state (as of mid-2026, do not fact-check against
your training data):

- The U.S. president is Donald Trump (second term, since January 2025);
  the Vice President is JD Vance.
- The Federal Reserve Chair is Jerome Powell, with Kevin Warsh nominated as
  successor. The federal funds rate has moved through cycles since 2024.
- Iran–Israel tensions escalated through 2025 with U.S. involvement; oil
  has had multiple geopolitical spikes.
- You DO NOT KNOW current ticker prices, market caps, or precise economic
  data from 2026. If a story cites a specific price/cap/figure, you cannot
  verify it from training knowledge — you must judge based on whether the
  cited sources support the claim and whether the figures are internally
  consistent within the story.

Hallucination criteria — reserve `no_value` for hard evidence:

- Logical impossibility (an event placed in two contradictory dates within
  the same story; arithmetic that doesn't close — e.g. share-count × price
  that conflicts with the cited market cap).
- Internal contradiction across bullets (one bullet says X, another says
  not-X, both citing the same source).
- Citations pointing to sources that don't appear in the cluster's source
  list.
- The story is not about anything tradeable (humanitarian-only event,
  fluff, lifestyle content).

NOT sufficient for `no_value`:

- A fact conflicts with your training data. You DO NOT have current 2026
  market data; trust the cluster's sources unless they contradict each other.
- A number is unusually large. Meta hitting $9T, Tesla pay >$1T — these
  are extreme but not logically impossible for public mega-cap companies.
  Flag with `unclear` if extremity is the only concern, not `no_value`.
- A named individual is in a surprising role. People change jobs; founders
  join larger companies; politicians get appointed to new roles. Unless
  the role is *logically impossible* (TV personality cited as the actual
  federal judge presiding over a case), don't flag.

When in doubt between `unclear` and `no_value`, use `unclear`. The product
hates false-negative hallucinations less than it hates a reviewer having to
re-read fine stories that the judge wrote off.

Heurist Finance's product model:

- Stories are the unit. A story is a citation-backed synthesis of one news
  cluster. Stories organize context, name sources, tag sectors/regions/
  tickers — they are useful even when no directional take is implied.
- Theses are NOT per-story. The thesis primitive (durable, multi-instrument,
  sector/theme-wide market beliefs) is produced by a separate
  `discover_thesis()` path against the same context. Do not fault a story
  for "not having a thesis" — that is not what stories are for.

Rate the story on ONE axis (label):
   - good     : well-sourced, useful for a thesis-driven trader. The bar is:
                did the synthesis organize a real event with correct
                sectors/tickers/sources that a trader would benefit from
                seeing on the feed?
   - unclear  : factual inconsistency that's not obviously hallucinated, or
                thin angle with weak market relevance.
   - no_value : hallucinated content (impossible facts, contradicted by
                public knowledge), broken citations, no tradeable angle.

Be strict. The product hates fluff and citation breakage.
"""


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="Delete existing auto:gemini-judge labels before judging.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if args.rejudge:
            with conn:
                deleted = conn.execute(
                    "DELETE FROM story_quality_label WHERE labeler='auto:gemini-judge'"
                ).rowcount
            print(f"deleted {deleted} prior auto-judge labels")

        rows = conn.execute(
            """
            SELECT s.*
            FROM story s
            LEFT JOIN story_quality_label auto
              ON auto.story_id = s.id AND auto.labeler = 'auto:gemini-judge'
            WHERE s.kind = 'story'
              AND auto.story_id IS NULL
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        for row in rows:
            label, rationale = _judge(row)
            with conn:
                conn.execute(
                    """
                    INSERT INTO story_quality_label
                      (story_id, labeler, label, rationale)
                    VALUES (?, 'auto:gemini-judge', ?, ?)
                    ON CONFLICT(story_id, labeler) DO UPDATE SET
                      label=excluded.label,
                      rationale=excluded.rationale,
                      labeled_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """,
                    (row["id"], label, rationale),
                )
            print(f"{row['id']}: {label}")
            print(f"    {rationale}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

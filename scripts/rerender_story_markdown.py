#!/usr/bin/env python3
"""Re-render `global/stories/<id>.md` from persisted `story` rows.

Use after changing the markdown layout (removing sections, dropping citation
markers, etc.) to bring existing files in line with the renderer in
`src/news/persist.py`. Pulls overview/quotes/headline from the
DB and reconstructs the cluster members from `news_cluster_member` so the
Sources block matches what `write_cluster_story` would produce now.
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

from src.news.persist import render_story_markdown
from src.news.synthesis import ClusterSynthesis
from src.news.types import ClusterSourceDoc

DB_PATH = ROOT / "db" / "hf.db"
STORY_DIR = ROOT / "global" / "stories"


def _members_for_cluster(conn: sqlite3.Connection, cluster_id: str) -> list[ClusterSourceDoc]:
    rows = conn.execute(
        """
        SELECT n.id, n.headline, n.source_url, n.publisher
        FROM news_cluster_member m
        JOIN news n ON n.id = m.news_id
        WHERE m.cluster_id = ?
        ORDER BY
          CASE WHEN n.publisher IN ('Reuters', 'AP', 'Bloomberg', 'WSJ', 'Financial Times', 'CNBC') THEN 0 ELSE 1 END,
          n.materiality_score DESC,
          n.published_at DESC
        LIMIT 3
        """,
        (cluster_id,),
    ).fetchall()
    return [
        ClusterSourceDoc(
            news_id=row["id"],
            title=row["headline"] or row["id"],
            url=row["source_url"] or "",
            publisher=row["publisher"] or "unknown",
            body="",
            published=None,
            tickers=[],
            sectors=[],
            regions=[],
        )
        for row in rows
    ]


def rerender(limit: int, *, dry_run: bool = False) -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, cluster_id, headline, what_changed, overview_json,
                   claims_json, quotes_json, market_relevance_json,
                   sectors_json, regions_json, theme_tag
            FROM story
            WHERE kind = 'story'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        written = 0
        for row in rows:
            members = _members_for_cluster(conn, row["cluster_id"])
            if not members:
                print(f"skip {row['id']}: no cluster members", file=sys.stderr)
                continue
            syn = ClusterSynthesis(
                headline=row["headline"],
                what_changed=row["what_changed"] or "",
                overview=json.loads(row["overview_json"] or "[]"),
                claims=json.loads(row["claims_json"] or "[]"),
                quotes=json.loads(row["quotes_json"] or "[]"),
                market_relevance=json.loads(row["market_relevance_json"] or "{}"),
                theme_tag=row["theme_tag"] or "other",
            )
            markdown = render_story_markdown(row["id"], syn, members)
            out_path = STORY_DIR / f"{row['id']}.md"
            if dry_run:
                print(f"[dry-run] would rewrite {out_path}")
                continue
            out_path.write_text(markdown, encoding="utf-8")
            written += 1
        return written
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="Most-recent N stories to re-render (default 50)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    n = rerender(args.limit, dry_run=args.dry_run)
    print(f"rerendered {n} stor{'y' if n == 1 else 'ies'}")


if __name__ == "__main__":
    main()

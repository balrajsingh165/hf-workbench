#!/usr/bin/env python3
"""Export stories for team read-through (no labeling — read-only digest CSV)."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "hf.db"


def _summary(overview_json: str | None) -> str:
    try:
        overview = json.loads(overview_json or "[]")
    except json.JSONDecodeError:
        return ""
    return " ".join(
        str(item.get("text") or "").strip()
        for item in overview[:3]
        if isinstance(item, dict)
    )


def export_packet(path: Path, *, limit: int = 200) -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.headline, s.what_changed,
                   s.overview_json, s.sectors_json, s.regions_json,
                   c.event_class, c.independent_pub_count,
                   COUNT(DISTINCT m.news_id) AS source_count,
                   auto.label AS auto_label,
                   auto.rationale AS auto_rationale
            FROM story s
            JOIN news_cluster c ON c.id = s.cluster_id
            LEFT JOIN news_cluster_member m ON m.cluster_id = s.cluster_id
            LEFT JOIN story_quality_label auto
              ON auto.story_id = s.id AND auto.labeler = 'auto:gemini-judge'
            WHERE s.kind = 'story'
            GROUP BY s.id
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "story_id",
            "auto_label",
            "auto_rationale",
            "headline",
            "event_class",
            "independent_publishers",
            "source_count",
            "summary",
            "what_changed",
            "sectors_json",
            "regions_json",
            "story_path",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "story_id": row["id"],
                "auto_label": row["auto_label"] or "",
                "auto_rationale": row["auto_rationale"] or "",
                "headline": row["headline"],
                "event_class": row["event_class"] or "",
                "independent_publishers": int(row["independent_pub_count"] or 0),
                "source_count": int(row["source_count"] or 0),
                "summary": _summary(row["overview_json"]),
                "what_changed": row["what_changed"] or "",
                "sectors_json": row["sectors_json"] or "[]",
                "regions_json": row["regions_json"] or "[]",
                "story_path": f"global/stories/{row['id']}.md",
            })
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("db/story_review_packet.csv"),
    )
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    count = export_packet(args.path, limit=args.limit)
    print(f"exported={count} path={args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

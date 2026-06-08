#!/usr/bin/env python3
"""Backfill event_class for story clusters and their member news rows."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.cluster import event_class_from_labels, infer_event_class_from_text

DB_PATH = ROOT / "db" / "hf.db"


def _json_list(value: str | None) -> list[str]:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def _story_body(row: sqlite3.Row) -> str:
    parts: list[str] = []
    for column in ("overview_json", "claims_json", "quotes_json", "open_questions_json"):
        for item in _json_list_of_dicts(row[column]):
            text = item.get("text")
            if text:
                parts.append(str(text))
    return "\n".join(parts)


def _json_list_of_dicts(value: str | None) -> list[dict]:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def backfill(*, write: bool = False) -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    changed = 0
    try:
        stories = conn.execute(
            """
            SELECT s.*, c.event_class
            FROM story s
            JOIN news_cluster c ON c.id = s.cluster_id
            WHERE c.event_class IS NULL OR c.event_class = ''
            ORDER BY s.id
            """
        ).fetchall()
        for story in stories:
            labels: list[str] = []
            text_parts = [story["headline"], _story_body(story)]
            members = conn.execute(
                """
                SELECT n.id, n.headline, n.body_excerpt, n.event_classes, n.event_class
                FROM news_cluster_member m
                JOIN news n ON n.id = m.news_id
                WHERE m.cluster_id = ?
                """,
                (story["cluster_id"],),
            ).fetchall()
            for member in members:
                labels.extend(_json_list(member["event_classes"]))
                text_parts.extend([member["headline"] or "", member["body_excerpt"] or ""])

            event_class = event_class_from_labels(labels) or infer_event_class_from_text(
                story["headline"],
                "\n".join(text_parts),
            )
            if not event_class:
                continue
            changed += 1
            print(f"{story['cluster_id']}: {event_class}")
            if write:
                with conn:
                    conn.execute(
                        """
                        UPDATE news_cluster
                        SET event_class = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                        WHERE id = ?
                        """,
                        (event_class, story["cluster_id"]),
                    )
                    conn.execute(
                        """
                        UPDATE news
                        SET event_class = COALESCE(event_class, ?),
                            event_classes = CASE
                              WHEN event_classes IS NULL OR event_classes = '[]' THEN ?
                              ELSE event_classes
                            END
                        WHERE cluster_id = ?
                        """,
                        (event_class, json.dumps([event_class]), story["cluster_id"]),
                    )
    finally:
        conn.close()
    return changed


def main() -> int:
    write = "--write" in sys.argv[1:]
    changed = backfill(write=write)
    mode = "updated" if write else "would update"
    print(f"{mode}: {changed} cluster(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

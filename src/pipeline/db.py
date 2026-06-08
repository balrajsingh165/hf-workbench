"""SQLite helpers for pipeline run bookkeeping."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    finally:
        conn.close()
    return int(row[0] if row else 0)


def count_story_rows(db_path: Path, *, kind: str = "story") -> int:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(story)")}
        if "kind" not in cols:
            row = conn.execute("SELECT COUNT(*) FROM story").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM story WHERE kind = ?",
                (kind,),
            ).fetchone()
    finally:
        conn.close()
    return int(row[0] if row else 0)


def active_thesis_ids(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        rows = conn.execute(
            "SELECT DISTINCT thesis_id FROM user_theses "
            "WHERE status != 'resolved' ORDER BY thesis_id"
        ).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]

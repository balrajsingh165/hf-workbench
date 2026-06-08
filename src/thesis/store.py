from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class UserThesisRow:
    thesis_id: str
    status: str
    horizon_days: int | None
    score: int | None
    score_freshness: int | None
    score_tailwind: int | None
    owner_count: int
    created_at: str
    resolved_at: str | None
    outcome: str | None


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_user_theses(
    db_path: Path,
    user_id: str,
    *,
    include_resolved: bool = False,
) -> list[UserThesisRow]:
    status_filter = "" if include_resolved else "AND ut.status != 'resolved'"
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT
                t.id AS thesis_id,
                ut.status,
                t.horizon_days,
                t.score,
                t.score_freshness,
                t.score_tailwind,
                t.owner_count,
                ut.created_at,
                ut.resolved_at,
                ut.outcome
            FROM user_theses ut
            JOIN theses t ON t.id = ut.thesis_id
            WHERE ut.user_id = ?
            {status_filter}
            ORDER BY ut.created_at DESC, t.id
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    return [
        UserThesisRow(
            thesis_id=row["thesis_id"],
            status=row["status"],
            horizon_days=row["horizon_days"],
            score=row["score"],
            score_freshness=row["score_freshness"],
            score_tailwind=row["score_tailwind"],
            owner_count=row["owner_count"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            outcome=row["outcome"],
        )
        for row in rows
    ]


__all__ = [
    "UserThesisRow",
    "get_db_connection",
    "get_user_theses",
]

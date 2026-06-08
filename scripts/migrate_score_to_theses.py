"""One-time migration: move thesis score from user_theses → theses.

The composite score and its two sub-dimensions (freshness, tailwind) are
intrinsic to the belief, not the holder — every owner shares one value (see
docs/design-thesis-creation.md and the schema comments on `theses`). This
migration relocates them off the per-user link table without losing the
already-computed values or the daily snapshot history.

Steps (all idempotent — safe to re-run):
  1. ADD score / score_freshness / score_tailwind to `theses`.
  2. Backfill them from `user_theses` (values are identical across an owner
     set, so MAX picks the one non-null value per thesis).
  3. Create `thesis_snapshots` and copy `user_theses_snapshots` collapsed to
     one row per (thesis_id, snapshot_date).
  4. Drop the three score columns from `user_theses`.
  5. Drop `user_theses_snapshots`.

After running, re-run `agents.score_theses` to fill scores for any thesis the
old per-user scorer never reached (unowned active proposals).

    uv run python scripts/migrate_score_to_theses.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "hf.db"

SCORE_COLS = ("score", "score_freshness", "score_tailwind")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def migrate(db_path: Path = DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys=OFF")  # table surgery below
    try:
        with conn:
            # 1. Add score columns to theses (skip ones already present).
            theses_cols = _columns(conn, "theses")
            for col in SCORE_COLS:
                if col not in theses_cols:
                    conn.execute(f"ALTER TABLE theses ADD COLUMN {col} INTEGER")
                    print(f"  + theses.{col}")

            # 2. Backfill from user_theses (identical across owners → MAX).
            if "score" in _columns(conn, "user_theses"):
                conn.execute(
                    """
                    UPDATE theses
                    SET score = sub.score,
                        score_freshness = sub.score_freshness,
                        score_tailwind = sub.score_tailwind
                    FROM (
                        SELECT thesis_id,
                               MAX(score)           AS score,
                               MAX(score_freshness) AS score_freshness,
                               MAX(score_tailwind)  AS score_tailwind
                        FROM user_theses
                        GROUP BY thesis_id
                    ) AS sub
                    WHERE theses.id = sub.thesis_id
                    """
                )
                print(f"  ✓ backfilled scores onto {conn.total_changes} theses rows")

            # 3. Create thesis_snapshots + copy collapsed history.
            if not _table_exists(conn, "thesis_snapshots"):
                conn.execute(
                    """
                    CREATE TABLE thesis_snapshots (
                        thesis_id       TEXT NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
                        snapshot_date   TEXT NOT NULL,
                        score           INTEGER,
                        score_freshness INTEGER,
                        score_tailwind  INTEGER,
                        created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                        PRIMARY KEY (thesis_id, snapshot_date)
                    )
                    """
                )
                print("  + thesis_snapshots")
            if _table_exists(conn, "user_theses_snapshots"):
                before = conn.execute(
                    "SELECT COUNT(*) FROM thesis_snapshots"
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO thesis_snapshots
                        (thesis_id, snapshot_date, score, score_freshness, score_tailwind, created_at)
                    SELECT thesis_id, snapshot_date,
                           MAX(score), MAX(score_freshness), MAX(score_tailwind),
                           MIN(created_at)
                    FROM user_theses_snapshots
                    GROUP BY thesis_id, snapshot_date
                    """
                )
                after = conn.execute(
                    "SELECT COUNT(*) FROM thesis_snapshots"
                ).fetchone()[0]
                print(f"  ✓ copied {after - before} snapshot rows")

            # 4. Drop score columns from user_theses (SQLite ≥ 3.35).
            ut_cols = _columns(conn, "user_theses")
            for col in SCORE_COLS:
                if col in ut_cols:
                    conn.execute(f"ALTER TABLE user_theses DROP COLUMN {col}")
                    print(f"  - user_theses.{col}")

            # 5. Drop the old per-user snapshot table.
            if _table_exists(conn, "user_theses_snapshots"):
                conn.execute("DROP TABLE user_theses_snapshots")
                print("  - user_theses_snapshots")
    finally:
        conn.close()

    print("\nmigration complete. Re-run agents.score_theses to score unowned proposals.")


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Migrating {DB_PATH} ...")
    migrate()

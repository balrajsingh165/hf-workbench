"""Normalize SQLite timestamp storage to UTC-marked ISO strings.

This migration is intentionally small and idempotent:

1. Back up the target database.
2. Rewrite table default metadata from `datetime('now')` to
   `strftime('%Y-%m-%dT%H:%M:%SZ','now')`.
3. Normalize existing timestamp column values from legacy naive UTC
   (`YYYY-MM-DD HH:MM:SS` or `YYYY-MM-DDTHH:MM:SS`) to
   `YYYY-MM-DDTHH:MM:SSZ`.

It does not touch date-only columns such as `brief_date` or `snapshot_date`.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "db" / "hf.db"

UTC_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
LEGACY_DEFAULT = "datetime('now')"
BARE_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$"
)

TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("created_at",),
    "theses": ("created_at",),
    "user_theses": ("created_at", "resolved_at"),
    "thesis_snapshots": ("created_at",),
    "news": ("published_at", "created_at"),
    "news_cluster": ("created_at", "updated_at", "first_seen_at", "last_seen_at"),
    "news_cluster_member": ("attached_at",),
    "news_cluster_dropped": ("dropped_at",),
    "story": ("created_at",),
    "instruments": ("updated_at",),
    "thesis_match_chunks": ("updated_at",),
    "story_match_chunks": ("updated_at",),
    "thesis_story_links": ("updated_at",),
    "daily_briefs": ("generated_at",),
    "agent_sessions": ("created_at", "updated_at"),
    "agent_messages": ("created_at",),
    "chat_titles": ("created_at", "updated_at"),
    "pending_instruments": ("first_seen_at", "last_seen_at"),
    "story_quality_label": ("labeled_at",),
    "story_synth_rejected": ("rejected_at",),
    "llm_calls": ("created_at",),
    "shared_chats": ("created_at", "updated_at"),
    "agent_usage": ("created_at",),
    "code_interpreter_runs": ("created_at",),
}


def _backup(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_suffix(f".{stamp}.bak")
    shutil.copy2(db_path, backup_path)
    return backup_path


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _normalize_timestamp(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.endswith("Z") or "+" in raw[10:]:
        return None
    if not BARE_TIMESTAMP_RE.match(raw):
        return None
    iso = raw.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_column(conn: sqlite3.Connection, table: str, column: str) -> int:
    changed = 0
    rows = conn.execute(
        f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
    ).fetchall()
    for rowid, value in rows:
        normalized = _normalize_timestamp(value)
        if normalized is None or normalized == value:
            continue
        conn.execute(
            f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
            (normalized, rowid),
        )
        changed += 1
    return changed


def _rewrite_schema_defaults(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
          AND sql LIKE ?
        """,
        (f"%{LEGACY_DEFAULT}%",),
    ).fetchall()
    if not rows:
        return 0

    conn.execute("PRAGMA writable_schema = ON")
    try:
        for name, sql in rows:
            conn.execute(
                "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
                (str(sql).replace(LEGACY_DEFAULT, UTC_DEFAULT), name),
            )
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute(f"PRAGMA schema_version = {int(schema_version) + 1}")
    finally:
        conn.execute("PRAGMA writable_schema = OFF")
    return len(rows)


def migrate(db_path: Path) -> dict[str, int | str]:
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    backup_path = _backup(db_path)
    normalized = 0
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        with conn:
            schema_tables = _rewrite_schema_defaults(conn)
            existing_tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table, columns in TIMESTAMP_COLUMNS.items():
                if table not in existing_tables:
                    continue
                existing_columns = _table_columns(conn, table)
                for column in columns:
                    if column in existing_columns:
                        normalized += _normalize_column(conn, table, column)
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"integrity_check failed: {result}")

    return {
        "backup": str(backup_path),
        "schema_tables": schema_tables,
        "values_normalized": normalized,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    result = migrate(args.db)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

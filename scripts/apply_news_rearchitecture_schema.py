#!/usr/bin/env python3
"""Apply the news rearchitecture schema additions without rebuilding hf.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "hf.db"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        print(f"added {table}.{column}")


def _drop_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    if column in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        print(f"dropped {table}.{column}")


def _migrate_story_quality_label_pk(conn: sqlite3.Connection) -> None:
    """Idempotently move story_quality_label PK to (story_id, labeler) and
    drop the deprecated thesis_quality / thesis_should_emit columns."""
    info = list(conn.execute("PRAGMA table_info(story_quality_label)"))
    if not info:
        return
    pk_cols = [row[1] for row in info if row[5]]
    cols = {row[1] for row in info}
    has_thesis_q = "thesis_quality" in cols
    has_should_emit = "thesis_should_emit" in cols
    if pk_cols == ["story_id", "labeler"] and not has_thesis_q and not has_should_emit:
        return
    conn.execute(
        """
        CREATE TABLE story_quality_label_new (
          story_id TEXT NOT NULL REFERENCES story(id),
          labeler TEXT NOT NULL,
          label TEXT NOT NULL,
          rationale TEXT,
          labeled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
          PRIMARY KEY (story_id, labeler)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO story_quality_label_new
            (story_id, labeler, label, rationale, labeled_at)
        SELECT story_id, labeler, label, rationale, labeled_at
        FROM story_quality_label
        """
    )
    conn.execute("DROP TABLE story_quality_label")
    conn.execute("ALTER TABLE story_quality_label_new RENAME TO story_quality_label")
    print("migrated story_quality_label: PK -> (story_id, labeler), dropped thesis_quality/thesis_should_emit")


_STORY_TARGET_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("id", "TEXT PRIMARY KEY", "id"),
    ("cluster_id", "TEXT UNIQUE REFERENCES news_cluster(id)", "cluster_id"),
    ("centroid_news_id", "TEXT REFERENCES news(id)", "centroid_news_id"),
    (
        "created_at",
        "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        "COALESCE(created_at, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
    ),
    ("headline", "TEXT NOT NULL", "COALESCE(headline, id)"),
    ("what_changed", "TEXT", "what_changed"),
    ("overview_json", "TEXT NOT NULL", "COALESCE(overview_json, '[]')"),
    ("claims_json", "TEXT NOT NULL DEFAULT '[]'", "COALESCE(claims_json, '[]')"),
    ("quotes_json", "TEXT NOT NULL DEFAULT '[]'", "COALESCE(quotes_json, '[]')"),
    (
        "market_relevance_json",
        "TEXT NOT NULL",
        "COALESCE(market_relevance_json, '{}')",
    ),
    (
        "open_questions_json",
        "TEXT NOT NULL DEFAULT '[]'",
        "COALESCE(open_questions_json, '[]')",
    ),
    ("sectors_json", "TEXT NOT NULL DEFAULT '[]'", "COALESCE(sectors_json, '[]')"),
    ("regions_json", "TEXT NOT NULL DEFAULT '[]'", "COALESCE(regions_json, '[]')"),
    ("theme_tag", "TEXT NOT NULL DEFAULT 'other'", "COALESCE(theme_tag, 'other')"),
    ("images_json", "TEXT NOT NULL DEFAULT '[]'", "COALESCE(images_json, '[]')"),
    ("kind", "TEXT NOT NULL DEFAULT 'story'", "COALESCE(kind, 'story')"),
    ("heat", "INTEGER", "heat"),
    ("social_json", "TEXT", "social_json"),
)


def _migrate_story_for_social(conn: sqlite3.Connection) -> None:
    """Idempotently relax story.cluster_id/centroid and add social columns.

    SQLite cannot drop NOT NULL from an existing column via ALTER TABLE, so the
    social ingestion schema change is a copy-rebuild. Existing rows preserve
    their ids and take kind='story' when the column was absent.
    """
    info = list(conn.execute("PRAGMA table_info(story)"))
    if not info:
        return
    columns = {row[1]: row for row in info}
    target_names = [name for name, _, _ in _STORY_TARGET_COLUMNS]
    missing = [name for name in target_names if name not in columns]
    cluster_notnull = bool(columns.get("cluster_id") and columns["cluster_id"][3])
    centroid_notnull = bool(columns.get("centroid_news_id") and columns["centroid_news_id"][3])
    if not missing and not cluster_notnull and not centroid_notnull:
        return

    defs = ",\n          ".join(
        f"{name} {ddl}" for name, ddl, _ in _STORY_TARGET_COLUMNS
    )
    conn.execute("DROP TABLE IF EXISTS story_social_new")
    conn.execute(f"CREATE TABLE story_social_new (\n          {defs}\n        )")
    select_exprs: list[str] = []
    for name, _, expr in _STORY_TARGET_COLUMNS:
        if name in columns:
            select_exprs.append(expr)
        elif name == "kind":
            select_exprs.append("'story'")
        elif name in {"overview_json", "claims_json", "quotes_json", "open_questions_json", "sectors_json", "regions_json", "images_json"}:
            select_exprs.append("'[]'")
        elif name == "market_relevance_json":
            select_exprs.append("'{}'")
        elif name == "theme_tag":
            select_exprs.append("'other'")
        elif name == "created_at":
            select_exprs.append("strftime('%Y-%m-%dT%H:%M:%SZ','now')")
        else:
            select_exprs.append("NULL")
    conn.execute(
        f"""
        INSERT INTO story_social_new ({", ".join(target_names)})
        SELECT {", ".join(select_exprs)}
        FROM story
        """
    )
    conn.execute("DROP TABLE story")
    conn.execute("ALTER TABLE story_social_new RENAME TO story")
    print("migrated story: nullable cluster refs + kind/heat/social_json")


def apply(db_path: Path = DB_PATH) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            _add_column(conn, "news", "regions_json", "TEXT DEFAULT '[]'")
            _add_column(conn, "news", "cluster_id", "TEXT")
            _add_column(conn, "news", "headline_hash", "TEXT")
            _add_column(conn, "news", "embedding", "BLOB")
            _add_column(conn, "news", "event_class", "TEXT")
            _add_column(conn, "instruments", "region", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS news_cluster (
                  id TEXT PRIMARY KEY,
                  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                  status TEXT NOT NULL,
                  centroid_news_id TEXT,
                  centroid_json TEXT,
                  headline_norm TEXT NOT NULL,
                  event_class TEXT,
                  max_materiality INTEGER NOT NULL DEFAULT 0,
                  member_count INTEGER NOT NULL DEFAULT 0,
                  independent_pub_count INTEGER NOT NULL DEFAULT 0,
                  has_tier1_primary INTEGER NOT NULL DEFAULT 0,
                  tickers_json TEXT NOT NULL DEFAULT '[]',
                  sectors_json TEXT NOT NULL DEFAULT '[]',
                  regions_json TEXT NOT NULL DEFAULT '[]',
                  sector_confidence REAL DEFAULT 0.0,
                  region_confidence REAL DEFAULT 0.0,
                  first_seen_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL
                )
                """
            )
            _add_column(conn, "news_cluster", "has_institutional_primary", "INTEGER NOT NULL DEFAULT 0")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS news_cluster_member (
                  cluster_id TEXT NOT NULL REFERENCES news_cluster(id),
                  news_id TEXT NOT NULL REFERENCES news(id),
                  attached_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                  similarity REAL,
                  attach_pass TEXT NOT NULL,
                  PRIMARY KEY (cluster_id, news_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS news_cluster_dropped (
                  cluster_id TEXT PRIMARY KEY REFERENCES news_cluster(id),
                  dropped_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                  reason TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS story (
                  id TEXT PRIMARY KEY,
                  cluster_id TEXT NOT NULL UNIQUE REFERENCES news_cluster(id),
                  centroid_news_id TEXT NOT NULL REFERENCES news(id),
                  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                  headline TEXT NOT NULL,
                  what_changed TEXT,
                  overview_json TEXT NOT NULL,
                  claims_json TEXT NOT NULL DEFAULT '[]',
                  quotes_json TEXT NOT NULL DEFAULT '[]',
                  market_relevance_json TEXT NOT NULL,
                  open_questions_json TEXT NOT NULL DEFAULT '[]',
                  sectors_json TEXT NOT NULL DEFAULT '[]',
                  regions_json TEXT NOT NULL DEFAULT '[]',
                  theme_tag TEXT NOT NULL DEFAULT 'other'
                )
                """
            )
            _drop_column(conn, "story", "thesis_line")
            _drop_column(conn, "story", "thesis_json")
            _add_column(conn, "story", "theme_tag", "TEXT NOT NULL DEFAULT 'other'")
            _migrate_story_for_social(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS story_quality_label (
                  story_id TEXT NOT NULL REFERENCES story(id),
                  labeler TEXT NOT NULL,
                  label TEXT NOT NULL,
                  rationale TEXT,
                  labeled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                  PRIMARY KEY (story_id, labeler)
                )
                """
            )
            _migrate_story_quality_label_pk(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS story_synth_rejected (
                  cluster_id TEXT NOT NULL,
                  rejected_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                  reason TEXT NOT NULL,
                  payload_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_calls (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  entity_type TEXT NOT NULL,
                  entity_id TEXT NOT NULL,
                  caller TEXT NOT NULL,
                  model_id TEXT NOT NULL,
                  latency_seconds REAL,
                  input_tokens INTEGER NOT NULL DEFAULT 0,
                  output_tokens INTEGER NOT NULL DEFAULT 0,
                  thinking_tokens INTEGER NOT NULL DEFAULT 0,
                  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                  total_tokens INTEGER NOT NULL DEFAULT 0,
                  cost_usd REAL NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                )
                """
            )
            for column in (
                "input_tokens",
                "output_tokens",
                "thinking_tokens",
                "cache_read_tokens",
                "total_tokens",
            ):
                _add_column(conn, "llm_calls", column, "INTEGER NOT NULL DEFAULT 0")
            for ddl in (
                "CREATE INDEX IF NOT EXISTS idx_news_headline_hash ON news (headline_hash)",
                "CREATE INDEX IF NOT EXISTS idx_news_cluster ON news (cluster_id)",
                "CREATE INDEX IF NOT EXISTS idx_cluster_sectors ON news_cluster (sectors_json)",
                "CREATE INDEX IF NOT EXISTS idx_cluster_regions ON news_cluster (regions_json)",
                "CREATE INDEX IF NOT EXISTS idx_cluster_status ON news_cluster (status, last_seen_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_cluster_dropped_time ON news_cluster_dropped (dropped_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_story_cluster ON story (cluster_id)",
                "CREATE INDEX IF NOT EXISTS idx_story_kind_created ON story (kind, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_story_sectors ON story (sectors_json)",
                "CREATE INDEX IF NOT EXISTS idx_story_regions ON story (regions_json)",
                "CREATE INDEX IF NOT EXISTS idx_llm_calls_entity ON llm_calls (entity_type, entity_id)",
                "CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls (created_at DESC)",
            ):
                conn.execute(ddl)

            conn.execute("UPDATE instruments SET region = 'australasia' WHERE region IS NULL AND symbol LIKE '%.AX'")
            conn.execute("UPDATE instruments SET region = 'japan' WHERE region IS NULL AND symbol LIKE '%.T'")
            conn.execute("UPDATE instruments SET region = 'uk' WHERE region IS NULL AND symbol LIKE '%.L'")
            conn.execute(
                """
                UPDATE instruments SET region = 'europe'
                WHERE region IS NULL
                  AND (symbol LIKE '%.PA' OR symbol LIKE '%.DE'
                       OR symbol LIKE '%.MI' OR symbol LIKE '%.AS')
                """
            )
            conn.execute(
                """
                UPDATE instruments SET region = 'china'
                WHERE region IS NULL
                  AND (symbol LIKE '%.HK' OR symbol LIKE '%.SS' OR symbol LIKE '%.SZ')
                """
            )
            conn.execute("UPDATE instruments SET region = 'north_america' WHERE region IS NULL")
    finally:
        conn.close()
    print(f"schema ready: {db_path}")


if __name__ == "__main__":
    apply()

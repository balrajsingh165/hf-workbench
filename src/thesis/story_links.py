"""Persistence for (thesis, story) judgments.

Mirrors the table defined in db/schema.py but ships a CREATE TABLE IF NOT
EXISTS helper so callers can write to the table on an existing DB without
re-running the full schema rebuild. Same pattern as story_match_index and
thesis_match_index.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


CHUNK_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thesis_story_links (
    thesis_id TEXT NOT NULL REFERENCES theses(id),
    story_id TEXT NOT NULL REFERENCES story(id),
    relation TEXT NOT NULL,
    confidence REAL NOT NULL,
    matched_invalidation TEXT,
    rationale TEXT NOT NULL,
    retrieval_score REAL NOT NULL,
    best_chunk_key TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (thesis_id, story_id)
)
"""

INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_thesis_story_links_thesis "
    "ON thesis_story_links (thesis_id)",
    "CREATE INDEX IF NOT EXISTS idx_thesis_story_links_story "
    "ON thesis_story_links (story_id)",
)


@dataclass(slots=True)
class ThesisStoryLink:
    thesis_id: str
    story_id: str
    relation: str  # supports | stresses
    confidence: float
    matched_invalidation: str | None
    rationale: str
    retrieval_score: float
    best_chunk_key: str
    source: str  # ingest | backfill
    story_created_at: str | None = None


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_thesis_story_links_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CHUNK_TABLE_DDL)
    for ddl in INDEX_DDL:
        conn.execute(ddl)
    conn.commit()


def upsert_links(db_path: Path, links: list[ThesisStoryLink]) -> None:
    """Insert or update judgments. updated_at is refreshed on conflict.

    Source precedence on conflict: an existing 'ingest' row is never
    downgraded to 'backfill' — ingest is the authoritative direction
    (story arrival writes it) and backfill cleanup later deletes by
    source='backfill'. If we let backfill overwrite source, those
    ingest rows would become deletable and silently disappear from
    scoring. The judgment fields still update; only the source label
    is sticky toward 'ingest'.
    """
    if not links:
        return
    conn = get_db_connection(db_path)
    try:
        ensure_thesis_story_links_schema(conn)
        with conn:
            for link in links:
                conn.execute(
                    """
                    INSERT INTO thesis_story_links (
                        thesis_id,
                        story_id,
                        relation,
                        confidence,
                        matched_invalidation,
                        rationale,
                        retrieval_score,
                        best_chunk_key,
                        source,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    ON CONFLICT(thesis_id, story_id) DO UPDATE SET
                        relation=excluded.relation,
                        confidence=excluded.confidence,
                        matched_invalidation=excluded.matched_invalidation,
                        rationale=excluded.rationale,
                        retrieval_score=excluded.retrieval_score,
                        best_chunk_key=excluded.best_chunk_key,
                        source=CASE
                            WHEN thesis_story_links.source = 'ingest' THEN 'ingest'
                            ELSE excluded.source
                        END,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """,
                    (
                        link.thesis_id,
                        link.story_id,
                        link.relation,
                        link.confidence,
                        link.matched_invalidation,
                        link.rationale,
                        link.retrieval_score,
                        link.best_chunk_key,
                        link.source,
                    ),
                )
    finally:
        conn.close()


def prune_backfill_links_for_thesis(
    db_path: Path,
    thesis_id: str,
    *,
    keep_above: float,
) -> int:
    """Delete backfill rows whose confidence is below `keep_above`.

    Above-floor rows survive across runs even when their stories age out of the
    retrieval window — that's the durable evidence trail. Ingest rows are
    untouched regardless of confidence.
    """
    conn = get_db_connection(db_path)
    try:
        ensure_thesis_story_links_schema(conn)
        with conn:
            cursor = conn.execute(
                "DELETE FROM thesis_story_links "
                "WHERE thesis_id = ? AND source = 'backfill' AND confidence < ?",
                (thesis_id, keep_above),
            )
            return cursor.rowcount
    finally:
        conn.close()


def load_backfill_link_story_ids(
    db_path: Path,
    thesis_id: str,
    *,
    min_confidence: float,
) -> set[str]:
    """Return story_ids that already have a backfill link at or above the floor.

    Used by the thesis→story matcher to skip re-judging candidates with a
    stable verdict already on file.
    """
    conn = get_db_connection(db_path)
    try:
        ensure_thesis_story_links_schema(conn)
        rows = conn.execute(
            "SELECT story_id FROM thesis_story_links "
            "WHERE thesis_id = ? AND source = 'backfill' AND confidence >= ?",
            (thesis_id, min_confidence),
        ).fetchall()
    finally:
        conn.close()
    return {row["story_id"] for row in rows}


def load_links_for_thesis(db_path: Path, thesis_id: str) -> list[ThesisStoryLink]:
    """Return judgments for a thesis, most-recently-judged first."""
    conn = get_db_connection(db_path)
    try:
        ensure_thesis_story_links_schema(conn)
        rows = conn.execute(
            """
            SELECT
                tsl.thesis_id,
                tsl.story_id,
                tsl.relation,
                tsl.confidence,
                tsl.matched_invalidation,
                tsl.rationale,
                tsl.retrieval_score,
                tsl.best_chunk_key,
                tsl.source,
                s.created_at AS story_created_at
            FROM thesis_story_links tsl
            LEFT JOIN story s ON s.id = tsl.story_id
            WHERE tsl.thesis_id = ?
            ORDER BY s.created_at DESC
            """,
            (thesis_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        ThesisStoryLink(
            thesis_id=row["thesis_id"],
            story_id=row["story_id"],
            relation=row["relation"],
            confidence=row["confidence"],
            matched_invalidation=row["matched_invalidation"],
            rationale=row["rationale"],
            retrieval_score=row["retrieval_score"],
            best_chunk_key=row["best_chunk_key"],
            source=row["source"],
            story_created_at=row["story_created_at"],
        )
        for row in rows
    ]


__all__ = [
    "ThesisStoryLink",
    "ensure_thesis_story_links_schema",
    "load_backfill_link_story_ids",
    "load_links_for_thesis",
    "prune_backfill_links_for_thesis",
    "upsert_links",
]

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.news.persist import _latest_cluster_source_published_at

# Fixed "generation time" injected into the function so the 6h freshness trick
# is deterministic. Source timestamps below are positioned relative to this.
NOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE news (
            id TEXT PRIMARY KEY,
            published_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE news_cluster_member (
            cluster_id TEXT NOT NULL,
            news_id TEXT NOT NULL
        );
        """
    )
    return conn


def _insert_member(
    conn: sqlite3.Connection,
    news_id: str,
    *,
    published_at: str | None,
    created_at: str = "2026-05-18 09:00:00",
) -> None:
    conn.execute(
        "INSERT INTO news (id, published_at, created_at) VALUES (?, ?, ?)",
        (news_id, published_at, created_at),
    )
    conn.execute(
        "INSERT INTO news_cluster_member (cluster_id, news_id) VALUES ('cluster_1', ?)",
        (news_id,),
    )


def test_uses_freshest_source_time_when_within_6h():
    # All members published within the last 6h of NOW → honor the freshest
    # wire time (11:30, two hours before NOW).
    conn = _conn()
    _insert_member(conn, "news_1", published_at="2026-05-18T10:00:00Z")
    _insert_member(conn, "news_2", published_at="2026-05-18T11:30:00Z")
    _insert_member(conn, "news_3", published_at="2026-05-18T11:00:00Z")

    assert (
        _latest_cluster_source_published_at(conn, "cluster_1", now=NOW)
        == "2026-05-18T11:30:00Z"
    )


def test_ignores_malformed_when_valid_fresh_exists():
    # A malformed published_at is skipped; the valid, fresh one is used.
    conn = _conn()
    _insert_member(conn, "news_bad", published_at="21 hours a", created_at="2026-05-18 14:00:00")
    _insert_member(conn, "news_good", published_at="2026-05-18T10:00:00Z")

    assert (
        _latest_cluster_source_published_at(conn, "cluster_1", now=NOW)
        == "2026-05-18T10:00:00Z"
    )


def test_falls_back_to_source_created_at_when_no_published():
    # No usable published_at → fall back to the freshest ingest (created_at),
    # which here is within 6h of NOW so it is honored.
    conn = _conn()
    _insert_member(conn, "news_1", published_at=None, created_at="2026-05-18 09:00:00")
    _insert_member(conn, "news_2", published_at="not a date", created_at="2026-05-18 11:00:00")

    assert (
        _latest_cluster_source_published_at(conn, "cluster_1", now=NOW)
        == "2026-05-18T11:00:00Z"
    )


def test_stale_source_falls_back_to_generation_time():
    # THE TRICK: the freshest source time is >6h before NOW (30h stale), so a
    # story synthesized "now" is stamped with the generation time instead of
    # being buried at its stale source date.
    conn = _conn()
    _insert_member(conn, "news_1", published_at="2026-05-17T06:00:00Z")  # ~30h before NOW
    _insert_member(conn, "news_2", published_at="2026-05-17T05:00:00Z")

    assert (
        _latest_cluster_source_published_at(conn, "cluster_1", now=NOW)
        == "2026-05-18T12:00:00Z"  # == NOW
    )


def test_just_inside_6h_boundary_honors_source_time():
    # Exactly at the 6h edge (06:00 == NOW - 6h) is still "fresh".
    conn = _conn()
    _insert_member(conn, "news_1", published_at="2026-05-18T06:00:00Z")

    assert (
        _latest_cluster_source_published_at(conn, "cluster_1", now=NOW)
        == "2026-05-18T06:00:00Z"
    )


def test_no_usable_timestamps_returns_generation_time():
    # Neither published_at nor created_at parses → fall through to generation
    # time rather than crashing or returning an empty string.
    conn = _conn()
    _insert_member(conn, "news_1", published_at=None, created_at="garbage")

    assert (
        _latest_cluster_source_published_at(conn, "cluster_1", now=NOW)
        == "2026-05-18T12:00:00Z"  # == NOW
    )

"""Tests for story-citation helpers in api.py.

Stories ship a source-chip list to the frontend. Pre-fix, the list pulled
every cluster member, producing a wall of pills (32 chips for a 32-member
cluster, including publishers the synth never actually cited). These tests
pin the new behavior: chips reflect cited docs only, deduplicated by
publisher, with a stable representative URL per publisher.
"""

from __future__ import annotations

import json
import sqlite3

from api import cited_news_ids, cited_publishers


def test_cited_news_ids_unions_across_payload_sections():
    overview = json.dumps([
        {"text": "a", "source_doc_ids": ["news_1", "news_2"]},
        {"text": "b", "source_doc_ids": ["news_2"]},
    ])
    claims = json.dumps([{"text": "c", "source_doc_ids": ["news_3"]}])
    quotes = json.dumps([{"text": "q", "source_doc_ids": ["news_1", "news_4"]}])
    assert cited_news_ids(overview, claims, quotes) == {
        "news_1", "news_2", "news_3", "news_4",
    }


def test_cited_news_ids_tolerates_missing_and_malformed():
    # None, empty string, non-list, non-dict items, missing key — all OK.
    assert cited_news_ids(None, "", "not json", "[1, 2, 3]") == set()
    assert cited_news_ids(json.dumps([{"text": "no ids"}])) == set()
    assert cited_news_ids(json.dumps([{"source_doc_ids": []}])) == set()


def test_cited_news_ids_strips_blank_entries():
    overview = json.dumps([{"source_doc_ids": ["news_1", "  ", ""]}])
    assert cited_news_ids(overview) == {"news_1"}


def _news_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE news (
            id TEXT PRIMARY KEY,
            publisher TEXT,
            source_url TEXT,
            published_at TEXT
        )
        """
    )
    return conn


def test_cited_publishers_collapses_same_publisher_to_one_row():
    conn = _news_conn()
    conn.executemany(
        "INSERT INTO news (id, publisher, source_url, published_at) VALUES (?,?,?,?)",
        [
            ("news_1", "CoinDesk", "https://coindesk.com/a", "2026-05-15T10:00:00Z"),
            ("news_2", "CoinDesk", "https://coindesk.com/b", "2026-05-15T12:00:00Z"),
            ("news_3", "Decrypt",  "https://decrypt.co/x",   "2026-05-15T11:00:00Z"),
        ],
    )
    rows = cited_publishers(conn, {"news_1", "news_2", "news_3"})
    by_pub = {r["publisher"]: r for r in rows}
    assert set(by_pub) == {"CoinDesk", "Decrypt"}
    # Most-recent published article wins as the representative URL.
    assert by_pub["CoinDesk"]["source_url"] == "https://coindesk.com/b"
    assert by_pub["Decrypt"]["source_url"] == "https://decrypt.co/x"


def test_cited_publishers_drops_blank_publishers():
    conn = _news_conn()
    conn.executemany(
        "INSERT INTO news (id, publisher, source_url, published_at) VALUES (?,?,?,?)",
        [
            ("news_1", None, "https://x.test/a", "2026-05-15T10:00:00Z"),
            ("news_2", "",   "https://x.test/b", "2026-05-15T10:00:00Z"),
            ("news_3", "Reuters", "https://reuters.com/x", "2026-05-15T10:00:00Z"),
        ],
    )
    rows = cited_publishers(conn, {"news_1", "news_2", "news_3"})
    assert [r["publisher"] for r in rows] == ["Reuters"]


def test_cited_publishers_empty_input():
    conn = _news_conn()
    assert cited_publishers(conn, set()) == []

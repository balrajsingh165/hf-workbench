"""Regression test for the next_story_id / INSERT UNIQUE race.

When the route step runs synth with `synth_workers > 1`, two threads can
read the same MAX(id) from `next_story_id` before either has inserted,
then both try to INSERT the same story id and the loser dies with
``sqlite3.IntegrityError: UNIQUE constraint failed: story.id``.

`write_cluster_story` retries on that exact error. This test simulates
the race by monkey-patching `next_story_id` to return a colliding id on
the first call and a fresh one on the retry.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.news.persist as persist


def _setup_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE story (
            id TEXT PRIMARY KEY,
            cluster_id TEXT NOT NULL UNIQUE,
            centroid_news_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            headline TEXT NOT NULL,
            what_changed TEXT,
            overview_json TEXT NOT NULL,
            claims_json TEXT NOT NULL,
            quotes_json TEXT NOT NULL,
            market_relevance_json TEXT NOT NULL,
            open_questions_json TEXT NOT NULL,
            sectors_json TEXT NOT NULL,
            regions_json TEXT NOT NULL,
            theme_tag TEXT NOT NULL,
            images_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE llm_calls (
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            caller TEXT NOT NULL,
            model_id TEXT NOT NULL,
            latency_seconds REAL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            thinking_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_usd REAL,
            created_at TEXT
        );
        CREATE TABLE entity_tickers (
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT,
            PRIMARY KEY (entity_type, entity_id, symbol)
        );
        CREATE TABLE pending_instruments (
            symbol TEXT NOT NULL,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            last_seen_at TEXT,
            seen_count INTEGER DEFAULT 1,
            PRIMARY KEY (symbol, source)
        );
        CREATE TABLE news_cluster (
            id TEXT PRIMARY KEY,
            status TEXT,
            sectors_json TEXT,
            regions_json TEXT,
            updated_at TEXT
        );
        INSERT INTO news_cluster (id, status) VALUES ('cluster_001', 'open'),
                                                     ('cluster_002', 'open');
        """
    )
    return conn


class _StubSynth:
    headline = "h"
    what_changed = "wc"
    overview: list = []
    claims: list = []
    quotes: list = []
    market_relevance: dict = {"tickers": [], "sectors": [], "regions": []}
    sectors: list = []
    regions: list = []
    theme_tag = "other"
    tickers: list = []
    model_id = "stub"
    latency_seconds = 0.0
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=4,
        thinking_tokens=2,
        cache_read_tokens=3,
        total_tokens=19,
    )
    cost_usd = 0.00123


def test_retry_loop_recovers_from_unique_story_id_collision(tmp_path, monkeypatch):
    """First next_story_id collides with a pre-existing row; the second
    call returns a fresh id and the loop succeeds.
    """
    db_path = tmp_path / "race.db"
    conn = _setup_db(db_path)

    # Seed: cluster_001 already has story_001 (winner thread). The loser
    # thread is about to allocate story_001 too — that's the race.
    conn.execute(
        """
        INSERT INTO story (id, cluster_id, centroid_news_id, created_at, headline,
                           overview_json, claims_json, quotes_json,
                           market_relevance_json, open_questions_json,
                           sectors_json, regions_json, theme_tag, images_json)
        VALUES ('story_001', 'cluster_001', 'news_1', strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'h',
                '[]','[]','[]','{}','[]','[]','[]','other','[]')
        """
    )
    conn.commit()

    calls = {"n": 0}

    def fake_next_story_id(_conn: sqlite3.Connection) -> str:
        calls["n"] += 1
        # Force a collision on the first attempt, succeed on retry.
        return "story_001" if calls["n"] == 1 else "story_002"

    monkeypatch.setattr(persist, "next_story_id", fake_next_story_id)

    # Mirror the retry block in write_cluster_story directly — that's the
    # surface we want to lock down.
    syn = _StubSynth()
    story_id = None
    last_integrity = None
    for _ in range(8):
        try:
            with conn:
                candidate = persist.next_story_id(conn)
                persist._persist_story_row(
                    conn,
                    story_id=candidate,
                    cluster_id="cluster_002",  # distinct cluster id
                    centroid_news_id="news_2",
                    created_at="2026-05-19T00:00:00Z",
                    syn=syn,
                    images_json="[]",
                )
            story_id = candidate
            break
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: story.id" not in str(exc):
                raise
            last_integrity = exc
            continue

    assert calls["n"] == 2, "retry must call next_story_id again after IntegrityError"
    assert story_id == "story_002"
    # And the loser's INSERT actually landed.
    row = conn.execute("SELECT cluster_id FROM story WHERE id='story_002'").fetchone()
    assert row is not None and row["cluster_id"] == "cluster_002"
    llm_row = conn.execute(
        """
        SELECT input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
               total_tokens, cost_usd
        FROM llm_calls
        WHERE entity_type='story' AND entity_id='story_002'
        """
    ).fetchone()
    assert dict(llm_row) == {
        "input_tokens": 10,
        "output_tokens": 4,
        "thinking_tokens": 2,
        "cache_read_tokens": 3,
        "total_tokens": 19,
        "cost_usd": 0.00123,
    }


def test_retry_loop_does_not_swallow_unrelated_integrity_errors(tmp_path, monkeypatch):
    """A cluster_id UNIQUE failure (real consistency bug) must not be
    treated like the recoverable story.id race — it should propagate.
    """
    db_path = tmp_path / "race.db"
    conn = _setup_db(db_path)
    conn.execute(
        """
        INSERT INTO story (id, cluster_id, centroid_news_id, created_at, headline,
                           overview_json, claims_json, quotes_json,
                           market_relevance_json, open_questions_json,
                           sectors_json, regions_json, theme_tag, images_json)
        VALUES ('story_001', 'cluster_001', 'news_1', strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'h',
                '[]','[]','[]','{}','[]','[]','[]','other','[]')
        """
    )
    conn.commit()

    def fake_next_story_id(_conn: sqlite3.Connection) -> str:
        return "story_002"  # never collides on id

    monkeypatch.setattr(persist, "next_story_id", fake_next_story_id)

    syn = _StubSynth()
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        for _ in range(8):
            try:
                with conn:
                    candidate = persist.next_story_id(conn)
                    persist._persist_story_row(
                        conn,
                        story_id=candidate,
                        cluster_id="cluster_001",  # already taken — real error
                        centroid_news_id="news_2",
                        created_at="2026-05-19T00:00:00Z",
                        syn=syn,
                        images_json="[]",
                    )
                break
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed: story.id" not in str(exc):
                    raise
                continue
    assert "cluster_id" in str(exc_info.value).lower() or "story.cluster_id" in str(exc_info.value).lower()

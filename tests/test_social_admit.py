"""Unit tests for social-topic admission (`agents/social_topics._admit_fetch`).

The policy under test: one live X topic per ticker. The batch's top-heat topic
either refreshes the ticker's live row in place (keyed by ticker — Grok
retitles the same discussion every run, so title matching is hopeless for
same-ticker dedupe) or inserts, unless it duplicates another ticker's
discussion. Lower-ranked topics in the batch are dropped (`rejected_rank`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.social.persist as persist
from agents.social_topics import _admit_fetch, _base_result

HEAT_MIN = 4
DAILY_CAP = 20


@pytest.fixture
def conn(tmp_path: Path, monkeypatch) -> sqlite3.Connection:
    monkeypatch.setattr(persist, "STORY_DIR", tmp_path / "stories")
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE story (
          id TEXT PRIMARY KEY,
          cluster_id TEXT,
          centroid_news_id TEXT,
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
          theme_tag TEXT NOT NULL DEFAULT 'other',
          images_json TEXT NOT NULL DEFAULT '[]',
          kind TEXT NOT NULL DEFAULT 'story',
          heat INTEGER,
          social_json TEXT
        );
        CREATE TABLE entity_tickers (
          entity_type TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          symbol TEXT NOT NULL,
          direction TEXT,
          PRIMARY KEY (entity_type, entity_id, symbol)
        );
        """
    )
    return db


def _topic(title: str, heat: int = 5) -> dict:
    return {
        "title": title,
        "heat": heat,
        "summary": f"{title}. Traders are split on what it means.",
        "bull_angle": "The dip is a gift.",
        "bear_angle": "Guidance is the tell.",
        "tweets": [
            {
                "handle": f"@user{i}",
                "url": f"https://x.com/user{i}/status/12345{i}",
                "stance": "bull",
                "claim": "a claim",
                "engagement": None,
            }
            for i in range(3)
        ],
    }


def _admit(conn, topics: list[dict], ticker: str, *, admitted_today: int = 0) -> dict:
    result = _base_result(dry_run=False)
    result["_admitted_today"] = admitted_today
    _admit_fetch(
        conn,
        SimpleNamespace(admitted=topics),
        ticker=ticker,
        heat_min=HEAT_MIN,
        daily_cap=DAILY_CAP,
        dry_run=False,
        result=result,
        preview=[],
    )
    return result


def _rows(conn, ticker: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.* FROM story s
        JOIN entity_tickers et ON et.entity_type='story' AND et.entity_id = s.id
        WHERE s.kind='x' AND et.symbol = ?
        """,
        (ticker,),
    ).fetchall()


def test_admits_only_top_topic_per_ticker(conn):
    result = _admit(
        conn,
        [_topic("AVGO Drops on Mixed Guidance", 5), _topic("Buy the AVGO Earnings Dip", 4)],
        "AVGO",
    )
    assert result["topics_admitted"] == 1
    assert result["rejected_rank"] == 1
    rows = _rows(conn, "AVGO")
    assert len(rows) == 1
    assert rows[0]["headline"] == "AVGO Drops on Mixed Guidance"


def test_rerun_refreshes_in_place_despite_retitle(conn):
    _admit(conn, [_topic("AVGO Drops on Mixed Guidance", 5)], "AVGO")
    before = _rows(conn, "AVGO")[0]

    result = _admit(conn, [_topic("AVGO Plunges on Unraised 2027 Target", 5)], "AVGO")
    assert result["topics_refreshed"] == 1
    assert result["topics_admitted"] == 0

    rows = _rows(conn, "AVGO")
    assert len(rows) == 1
    assert rows[0]["id"] == before["id"]
    assert rows[0]["headline"] == "AVGO Plunges on Unraised 2027 Target"
    # Refresh must not restart feed decay / the 48h window.
    assert rows[0]["created_at"] == before["created_at"]


def test_cross_ticker_duplicate_skipped(conn):
    _admit(conn, [_topic("Jensen Keynote Sparks AI Infrastructure Rally", 5)], "NVDA")
    result = _admit(
        conn, [_topic("Jensen Keynote Sparks AI Infrastructure Rally", 5)], "AVGO"
    )
    assert result["rejected_dup"] == 1
    assert result["topics_admitted"] == 0
    assert _rows(conn, "AVGO") == []


def test_refresh_exempt_from_daily_cap(conn):
    _admit(conn, [_topic("AVGO Drops on Mixed Guidance", 5)], "AVGO")
    result = _admit(
        conn,
        [_topic("AVGO Plunges on Unraised 2027 Target", 5)],
        "AVGO",
        admitted_today=DAILY_CAP,
    )
    assert result["topics_refreshed"] == 1
    assert result["rejected_cap"] == 0


def test_insert_blocked_at_daily_cap(conn):
    result = _admit(
        conn, [_topic("MU CEO Sells at New Highs", 5)], "MU", admitted_today=DAILY_CAP
    )
    assert result["rejected_cap"] == 1
    assert _rows(conn, "MU") == []


def test_top_topic_below_heat_min_rejected(conn):
    result = _admit(conn, [_topic("Quiet MU Chatter", 3)], "MU")
    assert result["rejected_heat"] == 1
    assert _rows(conn, "MU") == []

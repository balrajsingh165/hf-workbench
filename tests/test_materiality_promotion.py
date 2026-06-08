"""Promotion funnel: materiality caps."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db.schema import init_db
from src.news.cluster import recompute_cluster_features
from src.news.firehose_gate import (
    PR_WIRE_MATERIALITY_CAP,
    TIER1_MACRO_MATERIALITY_FLOOR,
    score_materiality,
)


def test_pr_wire_materiality_capped() -> None:
    score, _ = score_materiality(
        "Critical Metals Signs Definitive Agreement to Acquire European Lithium",
        "PR body",
        publisher="GlobeNewswire",
    )
    assert score <= PR_WIRE_MATERIALITY_CAP


def test_tier1_macro_headline_floor() -> None:
    score, labels = score_materiality(
        "Allianz's Zeng on US Rates & Bond Yields",
        "",
        publisher="Bloomberg",
    )
    assert score >= TIER1_MACRO_MATERIALITY_FLOOR
    assert labels  # macro_commentary and/or tier1_macro_commentary


def test_bond_yields_gain_scores_yield_shock() -> None:
    score, labels = score_materiality(
        "Bond Yields Gain Edge Over Nifty Dividends",
        "",
        publisher="Bloomberg",
    )
    assert score >= 30
    assert "yield_shock" in labels or "tier1_macro_commentary" in labels


@pytest.fixture()
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "hf.db"
    init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _insert_news(
    conn: sqlite3.Connection, news_id: str, publisher: str, materiality: int
) -> None:
    conn.execute(
        """
        INSERT INTO news (id, headline, body_excerpt, source_url, publisher,
                          materiality_score, event_classes, event_class,
                          published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, '[]', NULL,
                strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """,
        (news_id, f"hl-{news_id}", "body", f"https://x/{news_id}",
         publisher, materiality),
    )


def _insert_cluster_with(
    conn: sqlite3.Connection, cluster_id: str, members: list[tuple[str, str, int]]
) -> None:
    conn.execute(
        """
        INSERT INTO news_cluster
            (id, status, headline_norm, first_seen_at, last_seen_at)
        VALUES (?, 'open', 'hl', strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """,
        (cluster_id,),
    )
    for news_id, publisher, materiality in members:
        _insert_news(conn, news_id, publisher, materiality)
        conn.execute(
            """
            INSERT INTO news_cluster_member
                (cluster_id, news_id, similarity, attach_pass)
            VALUES (?, ?, 1.0, 'test')
            """,
            (cluster_id, news_id),
        )
    conn.commit()


def test_two_pr_wires_cluster_still_capped(tmp_db: sqlite3.Connection) -> None:
    """GlobeNewswire + PR Newswire syndicating the same issuer PR look like
    two independent groups, but it is still uncorroborated by editorial
    journalism — the PR-wire cap must still fire."""
    _insert_cluster_with(
        tmp_db,
        "c1",
        [
            ("n1", "GlobeNewswire", 100),
            ("n2", "PR Newswire", 100),
        ],
    )
    recompute_cluster_features(tmp_db, "c1")
    row = tmp_db.execute(
        "SELECT max_materiality, independent_pub_count FROM news_cluster WHERE id='c1'"
    ).fetchone()
    assert row["independent_pub_count"] == 2  # two distinct groups
    assert row["max_materiality"] <= PR_WIRE_MATERIALITY_CAP


def test_mixed_pr_wire_and_tier1_cluster_not_capped(
    tmp_db: sqlite3.Connection,
) -> None:
    """One Bloomberg member alongside PR wires is corroboration — cap must
    NOT fire so the cluster can promote on its own merits."""
    _insert_cluster_with(
        tmp_db,
        "c2",
        [
            ("n3", "GlobeNewswire", 100),
            ("n4", "Bloomberg", 60),
        ],
    )
    recompute_cluster_features(tmp_db, "c2")
    row = tmp_db.execute(
        "SELECT max_materiality FROM news_cluster WHERE id='c2'"
    ).fetchone()
    assert row["max_materiality"] == 100

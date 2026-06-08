"""Homepage thesis-column queries.

Both queries key off `thesis_story_links` with a **24h recency window**
(asymmetric with the Brief's 48h news-fetch window — see
`docs/plan-daily-brief.md`). No LLM, no markdown reads.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.brief.pipeline import DB_PATH


RANKING_WINDOW_HOURS = 24
SUPPORT_STRONG_CONF = 0.70


@dataclass(slots=True)
class TrackedCard:
    thesis_id: str
    status: str
    score: int | None
    top_conf: float


@dataclass(slots=True)
class RecommendedCard:
    thesis_id: str
    top_conf: float
    link_count: int
    ticker_overlap: int
    owner_count: int


def rank_user_theses_against_today(
    user_id: str,
    *,
    db_path: Path = DB_PATH,
    limit: int = 3,
    as_of: str | None = None,
) -> list[TrackedCard]:
    """Tracked theses the user owns that moved on today's news.

    Order: STRESSED first → max link confidence → composite score.
    `as_of` (ISO timestamp) lets callers reproduce the ranking for a past
    day; defaults to 'now'.
    """
    anchor = as_of or "now"
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            WITH recent_links AS (
              SELECT tsl.thesis_id, MAX(tsl.confidence) AS top_conf
                FROM thesis_story_links tsl
                JOIN story s ON s.id = tsl.story_id
               WHERE datetime(s.created_at) >= datetime(?, '-{RANKING_WINDOW_HOURS} hours')
                 AND tsl.confidence >= ?
               GROUP BY tsl.thesis_id
            )
            SELECT ut.thesis_id, ut.status, t.score, rl.top_conf
              FROM user_theses ut
              JOIN recent_links rl ON rl.thesis_id = ut.thesis_id
              JOIN theses t ON t.id = ut.thesis_id
             WHERE ut.user_id = ?
               AND ut.status != 'resolved'
             ORDER BY CASE ut.status WHEN 'stressed' THEN 0 ELSE 1 END,
                      rl.top_conf DESC,
                      t.score DESC NULLS LAST
             LIMIT ?
            """,
            (anchor, SUPPORT_STRONG_CONF, user_id, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        TrackedCard(
            thesis_id=r["thesis_id"],
            status=r["status"],
            score=r["score"],
            top_conf=float(r["top_conf"]),
        )
        for r in rows
    ]


def recommend_theses_against_today(
    user_id: str,
    *,
    db_path: Path = DB_PATH,
    limit: int = 3,
    as_of: str | None = None,
) -> list[RecommendedCard]:
    """Unowned theses moving on today's news, ranked by heuristics.

    Signals:
      • `top_conf`         — strongest supporting/stressing link today
      • `ticker_overlap`   — user's implicit watchlist = union of tickers on
                             theses they already own (from `entity_tickers`)
      • `owner_count`      — social proof
      • `link_count`       — breadth of today's corroboration (tiebreaker)
    """
    anchor = as_of or "now"
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            WITH recent_links AS (
              SELECT tsl.thesis_id,
                     MAX(tsl.confidence) AS top_conf,
                     COUNT(*) AS link_count
                FROM thesis_story_links tsl
                JOIN story s ON s.id = tsl.story_id
               WHERE datetime(s.created_at) >= datetime(?, '-{RANKING_WINDOW_HOURS} hours')
                 AND tsl.confidence >= ?
               GROUP BY tsl.thesis_id
            ),
            user_tickers AS (
              SELECT DISTINCT et.symbol AS ticker
                FROM user_theses ut
                JOIN entity_tickers et ON et.entity_type = 'thesis'
                 AND et.entity_id = ut.thesis_id
               WHERE ut.user_id = ? AND ut.status != 'resolved'
            ),
            candidate_overlap AS (
              SELECT et.entity_id AS thesis_id, COUNT(DISTINCT et.symbol) AS overlap
                FROM entity_tickers et
               WHERE et.entity_type = 'thesis'
                 AND et.symbol IN (SELECT ticker FROM user_tickers)
               GROUP BY et.entity_id
            )
            SELECT t.id,
                   rl.top_conf,
                   rl.link_count,
                   COALESCE(co.overlap, 0) AS ticker_overlap,
                   t.owner_count
              FROM theses t
              JOIN recent_links rl ON rl.thesis_id = t.id
              LEFT JOIN candidate_overlap co ON co.thesis_id = t.id
             WHERE t.id NOT IN (
               SELECT thesis_id FROM user_theses
                WHERE user_id = ? AND status != 'resolved'
             )
             ORDER BY rl.top_conf DESC,
                      ticker_overlap DESC,
                      t.owner_count DESC,
                      rl.link_count DESC
             LIMIT ?
            """,
            (anchor, SUPPORT_STRONG_CONF, user_id, user_id, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        RecommendedCard(
            thesis_id=r["id"],
            top_conf=float(r["top_conf"]),
            link_count=int(r["link_count"]),
            ticker_overlap=int(r["ticker_overlap"]),
            owner_count=int(r["owner_count"] or 0),
        )
        for r in rows
    ]


def load_latest_brief(
    *,
    db_path: Path = DB_PATH,
    max_age_hours: int = 48,
) -> dict[str, Any] | None:
    """Return the most recent brief row if it is ≤max_age_hours old; else None.

    Homepage fallback helper — returns None when the cron has been down long
    enough that the stale brief should no longer be served.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM daily_briefs ORDER BY brief_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        age = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 24 AS hours",
            (row["generated_at"],),
        ).fetchone()
    finally:
        conn.close()
    if age and age["hours"] is not None and age["hours"] > max_age_hours:
        return None
    return dict(row)


__all__ = [
    "RANKING_WINDOW_HOURS",
    "SUPPORT_STRONG_CONF",
    "RecommendedCard",
    "TrackedCard",
    "load_latest_brief",
    "rank_user_theses_against_today",
    "recommend_theses_against_today",
]

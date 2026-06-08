"""Persistence helpers for verified social topics."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from src.news.cluster import next_story_id
from src.social.grok import GrokResult

ROOT = Path(__file__).resolve().parents[2]
STORY_DIR = ROOT / "global" / "stories"
TITLE_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in into is it its of on or "
    "that the this to with will after amid over under says said new update "
    "stock shares x twitter debate".split()
)
TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalized_title_tokens(title: str) -> set[str]:
    return {
        token
        for token in TOKEN_RE.findall((title or "").lower())
        if len(token) > 2 and token not in TITLE_STOPWORDS
    }


def title_token_overlap(left: str, right: str) -> float:
    a = normalized_title_tokens(left)
    b = normalized_title_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def social_payload(topic: dict[str, Any]) -> dict[str, Any]:
    return {
        "bull_angle": str(topic.get("bull_angle") or "").strip(),
        "bear_angle": str(topic.get("bear_angle") or "").strip(),
        "tweets": [
            {
                "handle": str(tweet.get("handle") or "").strip(),
                "url": str(tweet.get("url") or "").strip(),
                "stance": str(tweet.get("stance") or "").strip(),
                "claim": str(tweet.get("claim") or "").strip(),
                "engagement": tweet.get("engagement"),
            }
            for tweet in topic.get("tweets") or []
            if isinstance(tweet, dict)
        ],
    }


def render_social_markdown(story_id: str, topic: dict[str, Any], ticker: str) -> str:
    payload = social_payload(topic)
    title = str(topic.get("title") or story_id).strip()
    summary = str(topic.get("summary") or "").strip()
    lines = [
        f"# {title}",
        "",
        "## Overview",
        "",
    ]
    if summary:
        lines.append(f"- {summary}")
    lines.extend(["", "## Angles", ""])
    bull = payload["bull_angle"]
    bear = payload["bear_angle"]
    if bull:
        lines.append(f"- Bull: {bull}")
    if bear:
        lines.append(f"- Bear: {bear}")
    lines.extend(["", "## Tweets", ""])
    for tweet in payload["tweets"]:
        handle = tweet["handle"] or "@unknown"
        stance = tweet["stance"] or "neutral"
        claim = tweet["claim"]
        engagement = tweet.get("engagement")
        suffix = f" ({engagement})" if engagement else ""
        lines.append(f"- [{handle}]({tweet['url']}) [{stance}] {claim}{suffix}")
    lines.extend(["", "## Metadata", "", f"- Ticker: {ticker.upper()}"])
    return "\n".join(lines).rstrip() + "\n"


def _overview_json(topic: dict[str, Any]) -> str:
    summary = str(topic.get("summary") or "").strip()
    return json.dumps([{"text": summary, "source_doc_ids": []}])


def _market_relevance_json(ticker: str) -> str:
    return json.dumps({
        "tickers": [ticker.upper()],
        "sectors": [],
        "regions": [],
        "direction": "mixed",
        "horizon": "near_term",
    })


def _write_markdown(story_id: str, topic: dict[str, Any], ticker: str) -> None:
    STORY_DIR.mkdir(parents=True, exist_ok=True)
    (STORY_DIR / f"{story_id}.md").write_text(
        render_social_markdown(story_id, topic, ticker),
        encoding="utf-8",
    )


def write_social_topic(
    conn: sqlite3.Connection,
    topic: dict[str, Any],
    ticker: str,
) -> str:
    """Insert a verified topic as a kind='x' story row."""
    ticker = ticker.upper()
    story_id = next_story_id(conn)
    conn.execute(
        """
        INSERT INTO story (
          id, cluster_id, centroid_news_id, headline, what_changed,
          overview_json, claims_json, quotes_json, market_relevance_json,
          open_questions_json, sectors_json, regions_json, theme_tag,
          images_json, kind, heat, social_json
        )
        VALUES (
          ?, NULL, NULL, ?, ?, ?, '[]', '[]', ?, '[]', '[]', '[]',
          'other', '[]', 'x', ?, ?
        )
        """,
        (
            story_id,
            str(topic.get("title") or "").strip(),
            str(topic.get("summary") or "").strip(),
            _overview_json(topic),
            _market_relevance_json(ticker),
            int(topic.get("heat") or 0),
            json.dumps(social_payload(topic)),
        ),
    )
    conn.execute(
        """
        INSERT INTO entity_tickers (entity_type, entity_id, symbol)
        VALUES ('story', ?, ?)
        ON CONFLICT(entity_type, entity_id, symbol) DO NOTHING
        """,
        (story_id, ticker),
    )
    _write_markdown(story_id, topic, ticker)
    return story_id


def refresh_social_topic(
    conn: sqlite3.Connection,
    story_id: str,
    topic: dict[str, Any],
    ticker: str,
) -> None:
    """Replace a live topic's content in place. `created_at` is deliberately
    untouched: bumping it would restart both feed decay and the 48h window, so
    a continuously re-heated topic would camp at the top forever. Instead the
    row ages out on schedule and a still-hot discussion re-enters as a fresh
    row once the old one leaves the window."""
    ticker = ticker.upper()
    conn.execute(
        """
        UPDATE story
        SET headline = ?,
            what_changed = ?,
            overview_json = ?,
            market_relevance_json = ?,
            heat = ?,
            social_json = ?
        WHERE id = ? AND kind = 'x'
        """,
        (
            str(topic.get("title") or "").strip(),
            str(topic.get("summary") or "").strip(),
            _overview_json(topic),
            _market_relevance_json(ticker),
            int(topic.get("heat") or 0),
            json.dumps(social_payload(topic)),
            story_id,
        ),
    )
    _write_markdown(story_id, topic, ticker)


def find_live_topic_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
) -> str | None:
    """The ticker's live X topic: newest kind='x' row inside the 48h feed
    window. One live topic per ticker is the admission invariant — this is
    its lookup, and the refresh key (Grok retitles the same discussion every
    run, so title matching never fires; the ticker is the stable identity)."""
    row = conn.execute(
        """
        SELECT s.id
        FROM story s
        JOIN entity_tickers et
          ON et.entity_type='story' AND et.entity_id = s.id
        WHERE s.kind = 'x'
          AND et.symbol = ?
          AND s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-48 hours')
        ORDER BY s.created_at DESC
        LIMIT 1
        """,
        (ticker.upper(),),
    ).fetchone()
    return row["id"] if row else None


def find_live_social_topic(
    conn: sqlite3.Connection,
    title: str,
    *,
    overlap_min: float = 0.55,
) -> tuple[str, str] | None:
    """Best title match among ALL live topics, as (story_id, ticker).

    Cross-ticker duplicate detection only — one X discussion (e.g. a Jensen
    keynote) often surfaces under several tickers in the same run. Same-ticker
    refresh is keyed by ticker via `find_live_topic_for_ticker`, not titles.
    """
    rows = conn.execute(
        """
        SELECT s.id, s.headline, et.symbol
        FROM story s
        JOIN entity_tickers et
          ON et.entity_type='story' AND et.entity_id = s.id
        WHERE s.kind = 'x'
          AND s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-48 hours')
        ORDER BY s.created_at DESC
        """,
    ).fetchall()
    best: tuple[str, str] | None = None
    best_score = 0.0
    for row in rows:
        score = title_token_overlap(title, row["headline"])
        if score > best_score:
            best_score = score
            best = (row["id"], row["symbol"])
    return best if best_score >= overlap_min else None


def log_social_grok_call(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    result: GrokResult,
    caller: str = "social_topics",
) -> None:
    usage = result.usage or {}
    cost_ticks = usage.get("cost_in_usd_ticks") or 0
    try:
        cost_usd = float(cost_ticks) / 1e10
    except (TypeError, ValueError):
        cost_usd = 0.0
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    thinking_tokens = int(usage.get("reasoning_tokens") or usage.get("thinking_tokens") or 0)
    cache_read_tokens = int(usage.get("cache_read_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens + thinking_tokens)
    conn.execute(
        """
        INSERT INTO llm_calls (
          entity_type, entity_id, caller, model_id, latency_seconds,
          input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
          total_tokens, cost_usd, created_at
        )
        VALUES (
          'ticker', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          strftime('%Y-%m-%dT%H:%M:%SZ','now')
        )
        """,
        (
            ticker.upper(),
            caller,
            result.model_id,
            result.latency_seconds,
            input_tokens,
            output_tokens,
            thinking_tokens,
            cache_read_tokens,
            total_tokens,
            cost_usd,
        ),
    )


__all__ = [
    "find_live_social_topic",
    "find_live_topic_for_ticker",
    "log_social_grok_call",
    "normalized_title_tokens",
    "refresh_social_topic",
    "render_social_markdown",
    "social_payload",
    "title_token_overlap",
    "write_social_topic",
]

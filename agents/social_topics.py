#!/usr/bin/env python3
"""Generate verified X social topics for hot Tier-1 tickers."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.trending import tier_symbols
from src.config import XAI_API_KEY
from src.instruments import resolver
from src.pipeline_metrics import append_metric
from src.social.persist import (
    find_live_social_topic,
    find_live_topic_for_ticker,
    log_social_grok_call,
    refresh_social_topic,
    write_social_topic,
)
from src.social.topics import SocialFetchResult, fetch_social_topics

DB_PATH = ROOT / "db" / "hf.db"
SOCIAL_TICKERS_MAX = 12
SOCIAL_LOOKBACK_DAYS = 2
SOCIAL_HEAT_MIN = 4
SOCIAL_DAILY_CAP = 20


def _base_result(*, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "phase": None,
        "dry_run": dry_run,
        "tickers_selected": 0,
        "tickers_called": 0,
        "topics_returned": 0,
        "topics_admitted": 0,
        "topics_refreshed": 0,
        "rejected_verifier": 0,
        "rejected_rank": 0,
        "rejected_heat": 0,
        "rejected_cap": 0,
        "rejected_dup": 0,
        "tweets_dropped": 0,
        "usd_total": 0.0,
        "x_searches": 0,
        "errors": [],
    }


def _count_admitted_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM story
        WHERE kind = 'x'
          AND date(created_at) = date('now')
        """
    ).fetchone()
    return int(row[0] if row else 0)


def _select_symbols(
    conn: sqlite3.Connection,
    *,
    ticker: str | None,
    limit: int | None,
) -> list[str]:
    if ticker:
        symbol = resolver.canonical(ticker.strip().upper())
        return [symbol] if resolver.exists(symbol) else []
    rows = tier_symbols(conn, 1)
    symbols = [symbol for symbol, _rank in rows]
    cap = limit if limit is not None else SOCIAL_TICKERS_MAX
    return symbols[:cap]


def _record_call_metrics(result: dict[str, Any], fetch: SocialFetchResult) -> None:
    result["topics_returned"] += fetch.topics_returned
    result["rejected_verifier"] += len(fetch.rejections)
    result["tweets_dropped"] += fetch.tweets_dropped
    result["usd_total"] = round(float(result["usd_total"]) + fetch.usd, 6)
    result["x_searches"] += fetch.x_searches


def _admit_fetch(
    conn: sqlite3.Connection,
    fetch: SocialFetchResult,
    *,
    ticker: str,
    heat_min: int,
    daily_cap: int,
    dry_run: bool,
    result: dict[str, Any],
    preview: list[dict[str, Any]],
) -> None:
    """Admit the ticker's single top topic, mutating the run counters.

    One live X topic per ticker: after a big move Grok slices the same event
    into several near-identical "topics" (and its heat ladder is rank-shaped —
    every batch leads 5, 4, …), so admitting more than one is repetition, not
    coverage. The card already carries bull/bear angles and up to 6 tweets;
    extra facets belong inside it. The top topic refreshes the ticker's live
    row in place when one exists (refreshes are free of the daily cap — no new
    row), otherwise inserts unless it duplicates another ticker's discussion.
    """
    topics = sorted(
        fetch.admitted, key=lambda t: int(t.get("heat") or 0), reverse=True
    )
    if not topics:
        return
    top, rest = topics[0], topics[1:]
    result["rejected_rank"] += len(rest)
    for topic in rest:
        preview.append({
            "ticker": ticker,
            "title": topic.get("title"),
            "drop": "rank",
            "heat": topic.get("heat"),
        })

    if int(top.get("heat") or 0) < heat_min:
        result["rejected_heat"] += 1
        preview.append({
            "ticker": ticker,
            "title": top.get("title"),
            "drop": "heat",
            "heat": top.get("heat"),
        })
        return

    if not dry_run:
        live_id = find_live_topic_for_ticker(conn, ticker)
        if live_id:
            refresh_social_topic(conn, live_id, top, ticker)
            result["topics_refreshed"] += 1
            return
        match = find_live_social_topic(conn, str(top.get("title") or ""))
        if match:
            # No live topic for this ticker, so any title match is another
            # ticker's discussion (e.g. one keynote heating up several
            # names) — skip, don't double-post.
            result["rejected_dup"] += 1
            preview.append({
                "ticker": ticker,
                "title": top.get("title"),
                "drop": "dup",
                "duplicate_of": match[0],
            })
            return

    if result["_admitted_today"] >= daily_cap:
        result["rejected_cap"] += 1
        preview.append({
            "ticker": ticker,
            "title": top.get("title"),
            "drop": "cap",
            "heat": top.get("heat"),
        })
        return

    result["_admitted_today"] += 1
    result["topics_admitted"] += 1
    if dry_run:
        preview.append({
            "ticker": ticker,
            "title": top.get("title"),
            "heat": top.get("heat"),
            "tweets": len(top.get("tweets") or []),
        })
    else:
        write_social_topic(conn, top, ticker)


def run_social_topics(
    *,
    db_path: Path = DB_PATH,
    ticker: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    should_stop=lambda: False,
) -> dict[str, Any]:
    """Run social-topic ingestion. Never raises; returns a social_run body."""
    started = time.perf_counter()
    result = _base_result(dry_run=dry_run)
    preview: list[dict[str, Any]] = []

    def _done(**updates: Any) -> dict[str, Any]:
        result.update(updates)
        result.pop("_admitted_today", None)
        result["duration_s"] = round(time.perf_counter() - started, 3)
        result["usd_total"] = round(float(result.get("usd_total") or 0.0), 6)
        if dry_run:
            result["preview"] = preview
        return result

    if not XAI_API_KEY:
        return _done(ok=False, phase="auth")

    try:
        conn = sqlite3.connect(db_path, timeout=30)
    except Exception as exc:
        return _done(ok=False, phase="db", errors=[repr(exc)])
    conn.row_factory = sqlite3.Row
    try:
        try:
            symbols = _select_symbols(conn, ticker=ticker, limit=limit)
        except Exception as exc:
            return _done(ok=False, phase="select", errors=[repr(exc)])
        result["tickers_selected"] = len(symbols)
        if not symbols:
            return _done(ok=False, phase="no_snapshot")

        result["_admitted_today"] = 0 if dry_run else _count_admitted_today(conn)
        for symbol in symbols:
            if should_stop():
                break
            name = resolver.to_display(symbol, "full")
            try:
                fetch = fetch_social_topics(
                    symbol,
                    name,
                    lookback_days=SOCIAL_LOOKBACK_DAYS,
                )
                result["tickers_called"] += 1
                _record_call_metrics(result, fetch)
            except Exception as exc:
                result["errors"].append({"ticker": symbol, "error": repr(exc)})
                continue

            try:
                if dry_run:
                    _admit_fetch(
                        conn,
                        fetch,
                        ticker=symbol,
                        heat_min=SOCIAL_HEAT_MIN,
                        daily_cap=SOCIAL_DAILY_CAP,
                        dry_run=True,
                        result=result,
                        preview=preview,
                    )
                else:
                    with conn:
                        log_social_grok_call(conn, ticker=symbol, result=fetch.grok)
                        _admit_fetch(
                            conn,
                            fetch,
                            ticker=symbol,
                            heat_min=SOCIAL_HEAT_MIN,
                            daily_cap=SOCIAL_DAILY_CAP,
                            dry_run=False,
                            result=result,
                            preview=preview,
                        )
            except Exception as exc:
                result["errors"].append({"ticker": symbol, "error": repr(exc)})
                continue
    finally:
        conn.close()

    if result["tickers_called"] == 0 and result["errors"]:
        return _done(ok=False, phase="fetch")
    return _done()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="Run one registry-known ticker.")
    parser.add_argument("--dry-run", action="store_true", help="Generate + verify; print only.")
    parser.add_argument("--limit", type=int, default=None, help="Cap selected Tier-1 tickers.")
    args = parser.parse_args(argv)

    result = run_social_topics(
        ticker=args.ticker,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not args.dry_run:
        append_metric({
            "event": "social_run",
            "source": "cli",
            **result,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

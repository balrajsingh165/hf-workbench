#!/usr/bin/env python3
"""Audit the news rearchitecture completion gates."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.verifier import verify_story_payload

DB_PATH = ROOT / "db" / "hf.db"
GOLD_DIR = ROOT / "db" / "story_gold"


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row else 0)


def _json_list(value: str | None) -> list:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _json_dict(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _story_structural_violations(conn: sqlite3.Connection) -> int:
    conn.row_factory = sqlite3.Row
    violations = 0
    rows = conn.execute(
        """
        SELECT s.id, s.cluster_id, s.overview_json, s.claims_json, s.quotes_json,
               s.market_relevance_json, s.open_questions_json, s.sectors_json,
               s.regions_json, c.event_class, c.independent_pub_count,
               c.has_tier1_primary, c.has_institutional_primary
        FROM story s
        JOIN news_cluster c ON c.id = s.cluster_id
        WHERE s.kind = 'story'
        """
    ).fetchall()
    for row in rows:
        story_id = row["id"]
        if not _json_list(row["sectors_json"]) or not _json_list(row["regions_json"]):
            violations += 1
            continue
        if not row["event_class"]:
            violations += 1
            continue
        # Structural source-quality bar: cluster must satisfy at least one of
        # the promotion rules (institutional primary, OR tier1 news primary,
        # OR ≥3 independent groups). Mirrors the routing rules.
        indep = int(row["independent_pub_count"] or 0)
        has_t1 = bool(row["has_tier1_primary"])
        has_inst = bool(row["has_institutional_primary"])
        if not (has_inst or has_t1 or indep >= 3):
            violations += 1
            continue
        # Accept tickers that exist in instruments OR are provisionally
        # admitted in pending_instruments (per plan: registry-unknown but
        # Yahoo-form tickers are valid via the provisional path).
        story_tickers = conn.execute(
            """
            SELECT et.symbol,
                   COALESCE(i.symbol, p.symbol) AS resolved_symbol
            FROM entity_tickers et
            LEFT JOIN instruments i ON i.symbol = et.symbol
            LEFT JOIN pending_instruments p ON p.symbol = et.symbol
            WHERE et.entity_type='story' AND et.entity_id=?
            """,
            (story_id,),
        ).fetchall()
        # Tickers are required for company-level events; macro/institutional
        # and broad-regulatory events (fed_action, macro_print, regulatory)
        # are allowed to have no tickers because their relevance is via
        # sectors (e.g. macro.rates, financials.exchanges) not specific
        # equities. A broad FTC policy or BoE rate hold has no single ticker.
        sector_only_event = row["event_class"] in {
            "fed_action", "macro_print", "regulatory", "sanctions", "executive_order",
        }
        if not sector_only_event and not story_tickers:
            violations += 1
            continue
        if story_tickers and any(t["resolved_symbol"] is None for t in story_tickers):
            violations += 1
            continue

        members = conn.execute(
            """
            SELECT n.id, n.body_excerpt
            FROM news_cluster_member m
            JOIN news n ON n.id = m.news_id
            WHERE m.cluster_id=?
            """,
            (row["cluster_id"],),
        ).fetchall()
        member_bodies = {str(member["id"]): str(member["body_excerpt"] or "") for member in members}
        payload = {
            "overview": _json_list(row["overview_json"]),
            "claims": _json_list(row["claims_json"]),
            "quotes": _json_list(row["quotes_json"]),
            "open_questions": _json_list(row["open_questions_json"]),
            "market_relevance": _json_dict(row["market_relevance_json"]),
        }
        if not payload["overview"]:
            violations += 1
            continue
        if not verify_story_payload(
            payload,
            member_ids=set(member_bodies),
            member_bodies=member_bodies,
        ).ok:
            violations += 1
    return violations


def _social_invariant_violations(conn: sqlite3.Connection) -> int:
    conn.row_factory = sqlite3.Row
    violations = 0
    rows = conn.execute(
        """
        SELECT id, cluster_id, centroid_news_id, heat, social_json
        FROM story
        WHERE kind = 'x'
        """
    ).fetchall()
    for row in rows:
        if row["cluster_id"] is not None or row["centroid_news_id"] is not None:
            violations += 1
            continue
        try:
            heat = int(row["heat"])
        except (TypeError, ValueError):
            violations += 1
            continue
        if heat < 1 or heat > 5:
            violations += 1
            continue
        try:
            payload = json.loads(row["social_json"] or "{}")
        except json.JSONDecodeError:
            violations += 1
            continue
        tweets = payload.get("tweets") if isinstance(payload, dict) else None
        if not isinstance(tweets, list) or len(tweets) < 3:
            violations += 1
            continue
    return violations


def _social_exclusion_violations(conn: sqlite3.Connection) -> int:
    violations = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM story_quality_label l
        JOIN story s ON s.id = l.story_id
        WHERE s.kind = 'x'
        """,
    )
    violations += _count(
        conn,
        """
        SELECT COUNT(*)
        FROM story_match_chunks c
        JOIN story s ON s.id = c.story_id
        WHERE s.kind = 'x'
        """,
    )
    try:
        violations += _count(
            conn,
            """
            SELECT COUNT(*)
            FROM daily_briefs b, json_each(b.source_story_ids) j
            JOIN story s ON s.id = j.value
            WHERE s.kind = 'x'
            """,
        )
    except sqlite3.OperationalError:
        pass
    return violations


def audit(*, min_gold_files: int = 1) -> dict[str, int | float]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        stories = _count(conn, "SELECT COUNT(*) FROM story WHERE kind='story'")
        social_topics = _count(conn, "SELECT COUNT(*) FROM story WHERE kind='x'")
        visible = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM story s
            WHERE s.kind = 'story'
              AND s.id NOT IN (
              SELECT story_id FROM story_quality_label
              WHERE label IN ('unclear', 'no_value')
            )
            """,
        )

        auto_labels = _count(
            conn,
            "SELECT COUNT(*) FROM story_quality_label WHERE labeler='auto:gemini-judge'",
        )
        auto_good = _count(
            conn,
            "SELECT COUNT(*) FROM story_quality_label WHERE labeler='auto:gemini-judge' AND label='good'",
        )

        structural_violations = _story_structural_violations(conn)
        social_invariant_violations = _social_invariant_violations(conn)
        social_exclusion_violations = _social_exclusion_violations(conn)
        unclustered_firehose = _count(
            conn,
            "SELECT COUNT(*) FROM news WHERE headline IS NOT NULL AND cluster_id IS NULL",
        )
        raw_news_llm_calls = _count(
            conn,
            "SELECT COUNT(*) FROM llm_calls WHERE entity_type='news'",
        )

        # Theme tag distribution — visibility metric, no gate.
        theme_other = _count(conn, "SELECT COUNT(*) FROM story WHERE kind='story' AND theme_tag='other'")
        theme_distribution = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT theme_tag, COUNT(*) FROM story WHERE kind='story' GROUP BY theme_tag ORDER BY COUNT(*) DESC, theme_tag"
            ).fetchall()
        }
    finally:
        conn.close()

    gold_files = len(list(GOLD_DIR.glob("*.json")))
    theme_other_rate = round((theme_other / stories * 100) if stories else 0.0, 1)
    return {
        "stories": stories,
        "social_topics": social_topics,
        "visible_stories": visible,
        "auto_labels": auto_labels,
        "auto_good_rate_pct": round((auto_good / auto_labels * 100) if auto_labels else 0.0, 1),
        "theme_other_rate_pct": theme_other_rate,
        "theme_distribution": theme_distribution,
        "structural_violations": structural_violations,
        "social_invariant_violations": social_invariant_violations,
        "social_exclusion_violations": social_exclusion_violations,
        "unclustered_firehose": unclustered_firehose,
        "raw_news_llm_calls": raw_news_llm_calls,
        "gold_files": gold_files,
        "min_gold_files": min_gold_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-gold-files", type=int, default=1,
        help="Minimum gold-set fixtures required. Default 1 (pre-launch).")
    args = parser.parse_args()

    metrics = audit(min_gold_files=args.min_gold_files)
    for key, value in metrics.items():
        print(f"{key}: {value}")

    failures: list[str] = []
    if metrics["structural_violations"]:
        failures.append("story structural gates have violations")
    if metrics["social_invariant_violations"]:
        failures.append("social topic rows have invariant violations")
    if metrics["social_exclusion_violations"]:
        failures.append("social topic rows leaked into story-only consumers")
    if metrics["unclustered_firehose"]:
        failures.append("firehose rows remain unclustered")
    if metrics["raw_news_llm_calls"]:
        failures.append("raw news rows have LLM call records")
    if metrics["gold_files"] < metrics["min_gold_files"]:
        failures.append(
            f"gold-set fixture count {metrics['gold_files']} below target {metrics['min_gold_files']}"
        )

    if failures:
        print("\nFAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

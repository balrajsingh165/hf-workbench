#!/usr/bin/env python3
"""Emit a weekly markdown quality report for story rows."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "hf.db"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        labels = _rows(
            conn,
            """
            SELECT l.label, l.rationale, l.labeler
            FROM story_quality_label l
            JOIN story s ON s.id = l.story_id
            WHERE s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              AND s.kind = 'story'
            """,
            (f"-{args.days} days",),
        )
        previous_labels = _rows(
            conn,
            """
            SELECT l.label
            FROM story_quality_label l
            JOIN story s ON s.id = l.story_id
            WHERE s.created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              AND s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              AND s.kind = 'story'
            """,
            (f"-{args.days} days", f"-{args.days * 2} days"),
        )
        no_value_patterns = _rows(
            conn,
            """
            SELECT COALESCE(NULLIF(TRIM(l.rationale), ''), '(no rationale)') AS rationale,
                   COUNT(*) AS count
            FROM story_quality_label l
            JOIN story s ON s.id = l.story_id
            WHERE s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              AND l.label='no_value'
              AND s.kind = 'story'
            GROUP BY rationale
            ORDER BY count DESC
            LIMIT 5
            """,
            (f"-{args.days} days",),
        )
        stories = _rows(
            conn,
            "SELECT * FROM story WHERE kind='story' AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)",
            (f"-{args.days} days",),
        )
        visible_stories = _rows(
            conn,
            """
            SELECT s.id
            FROM story s
            WHERE s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              AND s.kind = 'story'
              AND s.id NOT IN (
                SELECT story_id FROM story_quality_label
                WHERE label IN ('unclear', 'no_value')
              )
            """,
            (f"-{args.days} days",),
        )
        rejects = _rows(
            conn,
            """
            SELECT reason, COUNT(*) AS count
            FROM story_synth_rejected
            WHERE rejected_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            GROUP BY reason
            ORDER BY count DESC
            LIMIT 5
            """,
            (f"-{args.days} days",),
        )
        clusters = _rows(
            conn,
            """
            SELECT id, headline_norm, member_count, independent_pub_count,
                   tickers_json, sectors_json, regions_json
            FROM news_cluster
            WHERE status='sharp_promoted'
              AND updated_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            ORDER BY RANDOM()
            LIMIT 30
            """,
            (f"-{args.days} days",),
        )
        cost_rows = _rows(
            conn,
            """
            SELECT l.label, SUM(c.cost_usd) AS cost_usd
            FROM story_quality_label l
            JOIN llm_calls c
              ON c.entity_type='story' AND c.entity_id = l.story_id
            JOIN story s ON s.id = l.story_id
            WHERE s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
              AND s.kind = 'story'
            GROUP BY l.label
            """,
            (f"-{args.days} days",),
        )
        raw_news_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM news
            WHERE headline IS NOT NULL
              AND COALESCE(published_at, created_at) >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            """,
            (f"-{args.days} days",),
        ).fetchone()[0]
        pass2_count = conn.execute(
            """
            SELECT COUNT(DISTINCT news_id)
            FROM news_cluster_member
            WHERE attach_pass='embedding'
              AND attached_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            """,
            (f"-{args.days} days",),
        ).fetchone()[0]
        # Synthesis spend = emitted stories (entity_type='story') + synths
        # rejected after the Gemini call (entity_type='cluster'). Persist never
        # writes 'news'-tagged rows, so the old filter always summed to $0.
        raw_llm_cost = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM llm_calls
            WHERE entity_type IN ('story', 'cluster')
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            """,
            (f"-{args.days} days",),
        ).fetchone()[0]
    finally:
        conn.close()

    label_counts = Counter(row["label"] for row in labels)
    total_labels = sum(label_counts.values())
    good_rate = (label_counts["good"] / total_labels * 100) if total_labels else 0.0
    previous_label_counts = Counter(row["label"] for row in previous_labels)
    previous_total = sum(previous_label_counts.values())
    previous_good_rate = (
        previous_label_counts["good"] / previous_total * 100
        if previous_total
        else 0.0
    )

    # Labels here are auto-judge only; team labeling has been removed.
    print(f"# Story Quality Report ({args.days}d)")
    print()
    print(f"- Stories produced: {len(stories)}")
    print(f"- Stories visible after quality filter: {len(visible_stories)}")
    print(f"- Labels: {total_labels}")
    print(f"- Good rate: {good_rate:.1f}%")
    print(f"- Previous-period good rate: {previous_good_rate:.1f}%")
    for label in ("good", "unclear", "no_value"):
        print(f"- {label}: {label_counts[label]}")
    cost_by_label = {row["label"]: float(row["cost_usd"] or 0) for row in cost_rows}
    good_cost = cost_by_label.get("good", 0.0)
    pass2_rate = (pass2_count / raw_news_count * 100) if raw_news_count else 0.0
    print(f"- Cost for good stories: ${good_cost:.4f}")
    print(f"- Pass 2 fired: {pass2_count}/{raw_news_count} raw items ({pass2_rate:.1f}%)")
    print(f"- Synthesis LLM cost (incl. rejected): ${float(raw_llm_cost or 0):.4f}")

    print("\n## Rejected Synths\n")
    if rejects:
        for row in rejects:
            print(f"- {row['count']} — {row['reason']}")
    else:
        print("- none")

    print("\n## No-Value Patterns\n")
    if no_value_patterns:
        for row in no_value_patterns:
            print(f"- {row['count']} — {row['rationale']}")
    else:
        print("- none")

    coverage: dict[tuple[str, str], int] = defaultdict(int)
    for row in stories:
        try:
            sectors = json.loads(row["sectors_json"] or "[]")
            regions = json.loads(row["regions_json"] or "[]")
        except json.JSONDecodeError:
            sectors, regions = [], []
        for sector in sectors or ["none"]:
            parent = sector.split(".", 1)[0]
            for region in regions or ["global"]:
                coverage[(parent, region)] += 1

    print("\n## Sector x Region Coverage\n")
    if coverage:
        for (sector, region), count in sorted(coverage.items()):
            print(f"- {sector} x {region}: {count}")
    else:
        print("- none")

    print("\n## Cluster Precision Sample\n")
    if clusters:
        for row in clusters:
            print(
                f"- {row['id']}: members={row['member_count']} "
                f"independent={row['independent_pub_count']} "
                f"title={row['headline_norm'][:90]}"
            )
    else:
        print("- none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

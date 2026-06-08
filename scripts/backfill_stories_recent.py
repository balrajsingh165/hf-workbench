#!/usr/bin/env python3
"""Re-score recent news, recompute clusters, and run story promotion.

Used after promotion-funnel fixes (candidate pre-filter, tier-1 macro
materiality, PR-wire cluster cap).

    uv run python scripts/backfill_stories_recent.py
    uv run python scripts/backfill_stories_recent.py --hours 12 --top 30
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.cluster import recompute_cluster_features
from src.news.firehose_gate import score_materiality

DB_PATH = ROOT / "db" / "hf.db"


def _rescore_news(conn: sqlite3.Connection, hours: float) -> int:
    rows = conn.execute(
        """
        SELECT id, headline, body_excerpt, publisher
        FROM news
        WHERE headline IS NOT NULL
          AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
        """,
        (f"-{hours} hours",),
    ).fetchall()
    updates: list[tuple[int, str, str]] = []
    for news_id, headline, body, publisher in rows:
        score, classes = score_materiality(
            headline or "",
            body or "",
            publisher=publisher,
        )
        updates.append((score, json.dumps(classes), news_id))
    if updates:
        with conn:
            conn.executemany(
                "UPDATE news SET materiality_score = ?, event_classes = ? WHERE id = ?",
                updates,
            )
    return len(updates)


def _recompute_clusters(conn: sqlite3.Connection, hours: float) -> int:
    cluster_ids = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT c.id
            FROM news_cluster c
            JOIN news_cluster_member m ON m.cluster_id = c.id
            JOIN news n ON n.id = m.news_id
            WHERE n.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
            """,
            (f"-{hours} hours",),
        ).fetchall()
    ]
    for cluster_id in cluster_ids:
        recompute_cluster_features(conn, cluster_id)
    if cluster_ids:
        conn.commit()
    return len(cluster_ids)


def _run_route_write(synth_budget: int, route_eval_limit: int) -> int:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "agents.route_news_clusters",
            "--write",
            "--synth-budget",
            str(synth_budget),
            "--route-eval-limit",
            str(route_eval_limit),
        ],
        cwd=ROOT,
        check=False,
    )
    return proc.returncode


def _run_judge(limit: int) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "agents.judge_stories", "--limit", str(limit)],
        cwd=ROOT,
        check=False,
    )
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--synth-budget", "--top", type=int, default=40, dest="synth_budget")
    parser.add_argument(
        "--route-eval-limit",
        "--limit",
        type=int,
        default=1200,
        dest="route_eval_limit",
    )
    parser.add_argument("--skip-route", action="store_true")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.row_factory = sqlite3.Row
    try:
        n_news = _rescore_news(conn, args.hours)
        print(f"re-scored {n_news} news rows (last {args.hours}h)")
        n_clusters = _recompute_clusters(conn, args.hours)
        print(f"recomputed {n_clusters} clusters")
    finally:
        conn.close()

    if args.skip_route:
        return 0

    print(
        f"route_news_clusters --write --synth-budget {args.synth_budget} "
        f"--route-eval-limit {args.route_eval_limit}"
    )
    rc = _run_route_write(args.synth_budget, args.route_eval_limit)
    if rc != 0:
        print(f"route_news_clusters failed rc={rc}", file=sys.stderr)
        return rc

    print("judge_stories")
    return _run_judge(60)


if __name__ == "__main__":
    raise SystemExit(main())

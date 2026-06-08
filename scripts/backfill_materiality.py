"""One-shot backfill: compute materiality_score + event_classes for every
firehose-lane news row (rows with non-NULL headline). Sharp-lane rows
(synthesized clusters with NULL headline + body_excerpt) stay NULL.

Idempotent — safe to re-run after scorer changes.

    uv run python scripts/backfill_materiality.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.firehose_gate import MATERIALITY_HOME_THRESHOLD, score_materiality

DB_PATH = ROOT / "db" / "hf.db"


def main() -> int:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, headline, body_excerpt, publisher FROM news WHERE headline IS NOT NULL"
    ).fetchall()
    print(f"Scoring {len(rows)} firehose-lane rows…")
    updates: list[tuple[int, str, str]] = []
    hist: dict[int, int] = {}
    for r in rows:
        score, classes = score_materiality(
            r["headline"] or "",
            r["body_excerpt"] or "",
            publisher=r["publisher"],
        )
        updates.append((score, json.dumps(classes), r["id"]))
        bucket = (score // 10) * 10
        hist[bucket] = hist.get(bucket, 0) + 1
    with conn:
        conn.executemany(
            "UPDATE news SET materiality_score = ?, event_classes = ? WHERE id = ?",
            updates,
        )
    conn.close()

    print("Distribution by score bucket:")
    for k in sorted(hist):
        print(f"  {k:>3}–{k + 9:<3}: {hist[k]:>5}")
    below = sum(c for k, c in hist.items() if k < MATERIALITY_HOME_THRESHOLD)
    above = sum(c for k, c in hist.items() if k >= MATERIALITY_HOME_THRESHOLD)
    print(f"\nThreshold = {MATERIALITY_HOME_THRESHOLD}: hide {below}, keep {above}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

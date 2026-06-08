#!/usr/bin/env python3
"""
One-time backfill: rewrite `news.sectors_json` through the canonical taxonomy.

Usage:
    uv run python scripts/normalize_news_sectors.py --dry-run
    uv run python scripts/normalize_news_sectors.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.sectors import ALIAS_MAP, CANONICAL_SECTORS, normalize_sectors

DB_PATH = ROOT / "db" / "hf.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Report changes but do not write.")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, sectors_json FROM news WHERE sectors_json IS NOT NULL"
    ).fetchall()

    before_counts: Counter[str] = Counter()
    after_counts: Counter[str] = Counter()
    dropped_counts: Counter[str] = Counter()
    changed: list[tuple[str, list[str], list[str]]] = []
    updates: list[tuple[str, str]] = []

    for row in rows:
        nid = row["id"]
        try:
            raw = json.loads(row["sectors_json"] or "[]")
        except json.JSONDecodeError:
            print(f"  ! {nid}: invalid JSON in sectors_json, skipping", file=sys.stderr)
            continue
        if not isinstance(raw, list):
            print(f"  ! {nid}: sectors_json not a list, skipping", file=sys.stderr)
            continue

        for label in raw:
            before_counts[label] += 1
            key = (label or "").strip().lower()
            if key and key not in ALIAS_MAP:
                dropped_counts[label] += 1

        canonical = normalize_sectors(raw)
        for label in canonical:
            after_counts[label] += 1

        if canonical != raw:
            changed.append((nid, list(raw), canonical))
            updates.append((json.dumps(canonical), nid))

    print(f"Scanned {len(rows)} news rows.")
    print(f"  rows changed: {len(changed)}")
    print(f"  unique labels before: {len(before_counts)}")
    print(f"  unique labels after:  {len(after_counts)}")
    print()

    if dropped_counts:
        print(f"Dropped {sum(dropped_counts.values())} label-occurrences across "
              f"{len(dropped_counts)} distinct unmapped values:")
        for label, n in dropped_counts.most_common():
            print(f"  {n:5d}  {label!r}")
        print()
    else:
        print("No labels dropped (every raw value mapped to a canonical sector).\n")

    print("Canonical distribution after normalization:")
    for label, n in after_counts.most_common():
        print(f"  {n:5d}  {label}")
    print()

    unexpected = [label for label in after_counts if label not in CANONICAL_SECTORS]
    if unexpected:
        print(f"!! BUG: post-normalization labels not in CANONICAL_SECTORS: {unexpected}",
              file=sys.stderr)
        return 2

    if args.dry_run:
        print("--dry-run: no rows updated.")
        return 0

    if not updates:
        print("Nothing to write.")
        return 0

    with conn:
        conn.executemany(
            "UPDATE news SET sectors_json = ? WHERE id = ?",
            updates,
        )
    print(f"Wrote {len(updates)} updated rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

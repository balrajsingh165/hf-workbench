#!/usr/bin/env python3
"""
Query the thesis match index with free-text news descriptions.

Usage:
    uv run python scripts/query_thesis_match_index.py "Oil prices spike on Hormuz tensions"
    uv run python scripts/query_thesis_match_index.py "Fed holds rates steady" --top-k 3 --min-score 0.70
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.thesis.match_index import search_dense


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe the thesis match index with a free-text query."
    )
    parser.add_argument("query", help="Free-text news or event description to search with.")
    parser.add_argument("--top-k", type=int, default=5, help="Number of results to show.")
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.0,
        help="Minimum cosine similarity to display (0.0 = show all).",
    )
    args = parser.parse_args()

    db_path = ROOT / "db" / "hf.db"
    dense = search_dense(db_path, args.query, top_k=args.top_k, min_score=args.min_score)

    if not dense:
        print("No matches above threshold.")
        return

    for match in dense:
        print(
            f"- {match.thesis_id} | {match.chunk_key} | {match.score:.4f} | {match.chunk_text}"
        )


if __name__ == "__main__":
    main()

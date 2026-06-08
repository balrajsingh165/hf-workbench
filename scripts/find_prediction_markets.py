"""CLI shell over `src.prediction_markets.search.find_markets_for_article`.

This is *not* an agent — it's a thin argparse front for a deterministic
function (semantic search + volume rank). All real work lives in the library.

Usage:
  uv run python scripts/find_prediction_markets.py --thesis thesis_001
  uv run python scripts/find_prediction_markets.py --story story_001
  uv run python scripts/find_prediction_markets.py --query "fed rate cuts 2026"
  uv run python scripts/find_prediction_markets.py --thesis thesis_001 --refresh
  uv run python scripts/find_prediction_markets.py --thesis thesis_001 --top-k 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.prediction_markets.search import (
    DEFAULT_MIN_SIMILARITY,
    MarketMatch,
    find_markets_for_article,
)


def _match_to_dict(match: MarketMatch) -> dict:
    return {
        "source": match.market.source,
        "question": match.market.question,
        "volume_usd": round(match.market.volume_usd, 2),
        "similarity": round(match.similarity, 3),
        "closes_at": match.market.closes_at,
        "url": match.market.url,
        "id": match.market.id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find related open Polymarket markets for a thesis, news article, or query."
    )

    query_group = parser.add_mutually_exclusive_group(required=True)
    query_group.add_argument("--thesis", metavar="ID", help="Thesis id, e.g. thesis_001")
    query_group.add_argument("--story", metavar="ID", help="Story id or path, e.g. story_006")
    query_group.add_argument("--query", metavar="TEXT", help="Free-text query")

    parser.add_argument("--top-k", type=int, default=10,
                        help="Max results to return (default: 10)")
    parser.add_argument("--min-volume", type=float, default=50_000,
                        help="Minimum market volume in USD (default: 50000)")
    parser.add_argument("--min-sim", type=float, default=DEFAULT_MIN_SIMILARITY,
                        help=f"Minimum cosine similarity to include a result (default: {DEFAULT_MIN_SIMILARITY})")
    parser.add_argument("--cache-ttl", type=float, default=1.0,
                        help="Cache TTL in hours before re-fetching (default: 1.0)")
    parser.add_argument("--refresh", action="store_true",
                        help="Force re-fetch and re-embed, ignoring cache")
    args = parser.parse_args()

    if args.thesis:
        label = args.thesis
    elif args.story:
        label = args.story
    else:
        label = f'"{args.query}"'

    print(f"pm-search: query={label}", file=sys.stderr)

    try:
        matches = find_markets_for_article(
            thesis_id=args.thesis,
            story_id=args.story,
            query=args.query,
            min_volume_usd=args.min_volume,
            top_k=args.top_k,
            min_similarity=args.min_sim,
            cache_ttl_hours=args.cache_ttl,
            refresh=args.refresh,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"pm-search: {len(matches)} results (min_sim={args.min_sim}, "
        f"min_vol=${args.min_volume:,.0f})",
        file=sys.stderr,
    )

    output = {
        "query": label,
        "source": "polymarket",
        "min_volume_usd": args.min_volume,
        "results": [_match_to_dict(m) for m in matches],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

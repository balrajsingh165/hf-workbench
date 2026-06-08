#!/usr/bin/env python3
"""Rebuild the dense embedding index for stories or theses.

Usage:
    uv run python -m agents.build_match_index --kind story
    uv run python -m agents.build_match_index --kind thesis
    uv run python -m agents.build_match_index --kind story --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.story.match_index import (
    STORY_MATCH_EMBEDDING_DIMENSIONALITY,
    STORY_MATCH_EMBEDDING_MODEL,
    rebuild_story_match_index,
)
from src.thesis.match_index import (
    THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
    THESIS_MATCH_EMBEDDING_MODEL,
    rebuild_thesis_match_index,
)


_BUILDERS = {
    "story": {
        "label": "stories",
        "model": STORY_MATCH_EMBEDDING_MODEL,
        "dim": STORY_MATCH_EMBEDDING_DIMENSIONALITY,
        "fn": rebuild_story_match_index,
    },
    "thesis": {
        "label": "thesis chunks",
        "model": THESIS_MATCH_EMBEDDING_MODEL,
        "dim": THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
        "fn": rebuild_thesis_match_index,
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Embed markdown source of truth and index into SQLite.",
    )
    parser.add_argument(
        "--kind",
        choices=sorted(_BUILDERS),
        required=True,
        help="Which corpus to rebuild.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse sources and report count without calling Gemini or writing DB rows.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Maximum number of concurrent single-item embedding requests.",
    )
    args = parser.parse_args()

    spec = _BUILDERS[args.kind]
    count = spec["fn"](ROOT, batch_size=args.batch_size, dry_run=args.dry_run)
    verb = "Parsed" if args.dry_run else "Indexed"
    print(f"{verb} {count} {spec['label']} with {spec['model']} ({spec['dim']}d).")


if __name__ == "__main__":
    main()

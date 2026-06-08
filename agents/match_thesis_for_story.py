from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.story.docs import parse_story_markdown
from src.thesis.docs import ThesisDocument, parse_thesis_markdown
from src.thesis.match_helpers import load_entity_tickers, tickers_overlap
from src.thesis.match_index import search_dense
from src.thesis.story_judge import (
    BestChunk,
    JudgeVerdict,
    Relation,
    judge_pair,
    log_chunk_win,
)
from src.thesis.story_links import ThesisStoryLink, upsert_links


class ThesisMatch(TypedDict):
    thesis_id: str
    relation: Relation
    confidence: float
    matched_invalidation: str | None
    rationale: str


class MatchThesisForStoryOutput(TypedDict):
    story_id: str
    matches: list[ThesisMatch]


def _load_thesis_document(root: Path, thesis_id: str) -> ThesisDocument:
    path = root / "global" / "theses" / f"{thesis_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"Missing thesis markdown for {thesis_id}: {path}")
    return parse_thesis_markdown(path)


def _resolve_story_path(root: Path, story_ref: str) -> Path:
    candidate = Path(story_ref)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    return root / "global" / "stories" / f"{story_ref}.md"


def _to_match(thesis_id: str, verdict: JudgeVerdict) -> ThesisMatch:
    return {
        "thesis_id": thesis_id,
        "relation": verdict["relation"],
        "confidence": verdict["confidence"],
        "matched_invalidation": verdict["matched_invalidation"],
        "rationale": verdict["rationale"],
    }


def match_thesis_for_story(
    root: Path,
    story_ref: str,
    *,
    top_k: int = 5,
    min_score: float = 0.70,
    persist: bool = True,
) -> MatchThesisForStoryOutput:
    story_path = _resolve_story_path(root, story_ref)
    story = parse_story_markdown(story_path)
    db_path = root / "db" / "hf.db"

    matches: list[ThesisMatch] = []
    links: list[ThesisStoryLink] = []

    dense_matches = search_dense(
        db_path,
        story.query_text,
        top_k=top_k,
        min_score=min_score,
    )

    if dense_matches:
        story_tickers = load_entity_tickers(db_path, "story", {story.story_id}).get(story.story_id, set())
        thesis_tickers_map = load_entity_tickers(db_path, "thesis")
        before = len(dense_matches)
        dense_matches = [
            m for m in dense_matches
            if tickers_overlap(story_tickers, thesis_tickers_map.get(m.thesis_id, set()))
        ]
        pruned = before - len(dense_matches)
        if pruned:
            print(
                f"match-thesis-for-story: story={story.story_id} pruned {pruned}/{before} "
                f"thesis candidate(s) with no ticker overlap",
                file=sys.stderr,
            )

    for dense_match in dense_matches:
        thesis = _load_thesis_document(root, dense_match.thesis_id)
        best_chunk = BestChunk(
            chunk_key=dense_match.chunk_key,
            chunk_kind=dense_match.chunk_kind,
            chunk_text=dense_match.chunk_text,
            score=dense_match.score,
        )
        verdict = judge_pair(story, thesis, best_chunk)
        if verdict is None:
            continue
        log_chunk_win(
            story_id=story.story_id,
            thesis_id=thesis.thesis_id,
            chunk_key=dense_match.chunk_key,
            retrieval_score=dense_match.score,
            verdict=verdict,
        )
        if verdict["relation"] == "unrelated":
            continue
        matches.append(_to_match(thesis.thesis_id, verdict))
        links.append(
            ThesisStoryLink(
                thesis_id=thesis.thesis_id,
                story_id=story.story_id,
                relation=verdict["relation"],
                confidence=verdict["confidence"],
                matched_invalidation=verdict["matched_invalidation"],
                rationale=verdict["rationale"],
                retrieval_score=dense_match.score,
                best_chunk_key=dense_match.chunk_key,
                source="ingest",
            )
        )

    if persist:
        # Replace this story's ingest-authored set transactionally: drop
        # any stale rows from a prior run (verdict flipped to unrelated,
        # candidate now pruned, etc.) before inserting the current set.
        # Backfill rows for the same story are preserved.
        kept_thesis_ids = {link.thesis_id for link in links}
        removed = _replace_ingest_links_for_story(db_path, story.story_id, kept_thesis_ids)
        if removed:
            print(
                f"match-thesis-for-story: story={story.story_id} removed {removed} stale "
                f"ingest row(s)",
                file=sys.stderr,
            )
        if links:
            upsert_links(db_path, links)
            print(
                f"match-thesis-for-story: story={story.story_id} wrote {len(links)} ingest row(s) "
                f"to thesis_story_links",
                file=sys.stderr,
            )

    return {"story_id": story.story_id, "matches": matches}


def _replace_ingest_links_for_story(
    db_path: Path,
    story_id: str,
    kept_thesis_ids: set[str],
) -> int:
    """Delete ingest rows for this story whose thesis_id is not in the new set.

    Used by match_thesis_for_story to clear stale per-pair ingest evidence
    when a rerun no longer matches a thesis it previously matched.
    Backfill rows are never touched.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            if kept_thesis_ids:
                placeholders = ",".join("?" * len(kept_thesis_ids))
                cursor = conn.execute(
                    f"DELETE FROM thesis_story_links "
                    f"WHERE story_id = ? AND source = 'ingest' "
                    f"AND thesis_id NOT IN ({placeholders})",
                    (story_id, *kept_thesis_ids),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM thesis_story_links "
                    "WHERE story_id = ? AND source = 'ingest'",
                    (story_id,),
                )
            return cursor.rowcount
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Match one story against the thesis index and judge candidate theses."
    )
    parser.add_argument(
        "story",
        help="Story id like story_024 or an explicit path to a story markdown file.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of thesis candidates to retrieve before LLM judgment.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.70,
        help="Minimum dense retrieval score required to judge a thesis.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run retrieval + judge but do not write thesis_story_links rows.",
    )
    args = parser.parse_args()

    output = match_thesis_for_story(
        ROOT,
        args.story,
        top_k=args.top_k,
        min_score=args.min_score,
        persist=not args.dry_run,
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

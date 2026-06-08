"""Match recent stories for one thesis.

The thesis-side mirror of agents/match_thesis_for_story.py. Runs on:
  - thesis creation (seed the signal timeline at t=0)
  - cold→hot promotion (catch up on stories since the user was last active)
  - ad-hoc reindex (after a judge-prompt change, rerun per thesis)

Walks the thesis's semantic chunks as queries against the story dense index,
dedupes to one best-score per story, runs the shared judge on the top-M
candidates in parallel, and persists supports/stresses rows to
thesis_story_links with source='backfill'.

Stable above-floor backfill rows survive across runs even when their stories
age out of the retrieval window — they're the durable evidence trail. Each
run prunes only below-floor backfill rows, then judges candidates whose
story doesn't already have an above-floor link on file. Ingest rows are
untouched regardless.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.story.docs import parse_story_markdown
from src.story.match_index import search_dense_story
from src.thesis.docs import (
    ThesisChunk,
    ThesisDocument,
    build_thesis_chunks,
    parse_thesis_markdown,
)
from src.thesis.match_helpers import load_entity_tickers, tickers_overlap
from src.thesis.story_judge import BestChunk, JudgeVerdict, judge_pair, log_chunk_win
from src.thesis.story_links import (
    ThesisStoryLink,
    load_backfill_link_story_ids,
    prune_backfill_links_for_thesis,
    upsert_links,
)


# Backfill rows at or above this confidence survive across runs and are never
# re-judged. Below-floor rows are pruned each run and re-evaluated if the
# story still surfaces as a candidate.
BACKFILL_KEEP_CONF = 0.70


@dataclass(slots=True)
class _Candidate:
    story_id: str
    best_chunk: ThesisChunk
    retrieval_score: float


def _load_thesis_document(root: Path, thesis_id: str) -> ThesisDocument:
    path = root / "global" / "theses" / f"{thesis_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"Missing thesis markdown for {thesis_id}: {path}")
    return parse_thesis_markdown(path)


def _resolve_story_path(root: Path, story_id: str) -> Path:
    return root / "global" / "stories" / f"{story_id}.md"


def _retrieve_candidates(
    db_path: Path,
    chunks: list[ThesisChunk],
    *,
    top_k_per_chunk: int,
    min_score: float,
    since: str | None,
    max_candidates: int,
) -> list[_Candidate]:
    best_per_story: dict[str, _Candidate] = {}
    for chunk in chunks:
        hits = search_dense_story(
            db_path,
            chunk.search_text,
            top_k=top_k_per_chunk,
            min_score=min_score,
            since=since,
        )
        for hit in hits:
            prev = best_per_story.get(hit.story_id)
            if prev is None or hit.score > prev.retrieval_score:
                best_per_story[hit.story_id] = _Candidate(
                    story_id=hit.story_id,
                    best_chunk=chunk,
                    retrieval_score=hit.score,
                )
    ranked = sorted(best_per_story.values(), key=lambda c: c.retrieval_score, reverse=True)
    return ranked[:max_candidates]


def _default_since(window_days: int | None) -> str | None:
    if window_days is None:
        return None
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=window_days)
    return cutoff.isoformat()


def _safe_judge(
    root: Path,
    thesis: ThesisDocument,
    candidate: _Candidate,
) -> tuple[_Candidate, JudgeVerdict | None]:
    """Judge one candidate, swallowing any exception so one bad call does not kill the whole run."""
    try:
        story = parse_story_markdown(_resolve_story_path(root, candidate.story_id))
        best_chunk = BestChunk(
            chunk_key=candidate.best_chunk.chunk_key,
            chunk_kind=candidate.best_chunk.chunk_kind,
            chunk_text=candidate.best_chunk.chunk_text,
            score=candidate.retrieval_score,
        )
        verdict = judge_pair(story, thesis, best_chunk)
        return candidate, verdict
    except Exception:
        print(
            f"warning: judge failed for story={candidate.story_id} "
            f"thesis={thesis.thesis_id}; skipping.",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return candidate, None


def match_story_for_thesis(
    root: Path,
    thesis_id: str,
    *,
    window_days: int | None = 14,
    top_k_per_chunk: int = 10,
    min_score: float = 0.5,
    max_candidates: int = 15,
    max_workers: int = 4,
    persist: bool = True,
) -> list[ThesisStoryLink]:
    thesis = _load_thesis_document(root, thesis_id)
    chunks = build_thesis_chunks(thesis)

    db_path = root / "db" / "hf.db"
    since = _default_since(window_days)

    candidates = _retrieve_candidates(
        db_path,
        chunks,
        top_k_per_chunk=top_k_per_chunk,
        min_score=min_score,
        since=since,
        max_candidates=max_candidates,
    )

    if candidates:
        thesis_tickers = load_entity_tickers(db_path, "thesis", {thesis_id}).get(thesis_id, set())
        story_tickers_map = load_entity_tickers(db_path, "story", {c.story_id for c in candidates})
        before = len(candidates)
        candidates = [
            c for c in candidates
            if tickers_overlap(thesis_tickers, story_tickers_map.get(c.story_id, set()))
        ]
        pruned = before - len(candidates)
        if pruned:
            print(
                f"match-story-for-thesis: thesis={thesis_id} pruned {pruned}/{before} "
                f"story candidate(s) with no ticker overlap",
                file=sys.stderr,
            )

    locked_in = load_backfill_link_story_ids(
        db_path, thesis_id, min_confidence=BACKFILL_KEEP_CONF
    )
    if candidates and locked_in:
        before = len(candidates)
        candidates = [c for c in candidates if c.story_id not in locked_in]
        skipped = before - len(candidates)
        if skipped:
            print(
                f"match-story-for-thesis: thesis={thesis_id} skipped {skipped}/{before} "
                f"candidate(s) with existing conf>={BACKFILL_KEEP_CONF:.2f} link",
                file=sys.stderr,
            )

    if not candidates:
        print(
            f"match-story-for-thesis: thesis={thesis_id} no new candidates "
            f"(window_days={window_days}, min_score={min_score})",
            file=sys.stderr,
        )
        if persist:
            removed = prune_backfill_links_for_thesis(
                db_path, thesis_id, keep_above=BACKFILL_KEEP_CONF
            )
            if removed:
                print(
                    f"match-story-for-thesis: thesis={thesis_id} pruned {removed} "
                    f"below-floor backfill row(s)",
                    file=sys.stderr,
                )
        return []

    if max_workers <= 1 or len(candidates) == 1:
        results = [_safe_judge(root, thesis, c) for c in candidates]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(
                pool.map(lambda c: _safe_judge(root, thesis, c), candidates)
            )

    links: list[ThesisStoryLink] = []
    for candidate, verdict in results:
        if verdict is None:
            continue
        log_chunk_win(
            story_id=candidate.story_id,
            thesis_id=thesis.thesis_id,
            chunk_key=candidate.best_chunk.chunk_key,
            retrieval_score=candidate.retrieval_score,
            verdict=verdict,
        )
        relation = verdict["relation"]
        if relation == "unrelated":
            continue
        links.append(
            ThesisStoryLink(
                thesis_id=thesis.thesis_id,
                story_id=candidate.story_id,
                relation=relation,
                confidence=verdict["confidence"],
                matched_invalidation=verdict["matched_invalidation"],
                rationale=verdict["rationale"],
                retrieval_score=candidate.retrieval_score,
                best_chunk_key=candidate.best_chunk.chunk_key,
                source="backfill",
            )
        )

    if persist:
        removed = prune_backfill_links_for_thesis(
            db_path, thesis_id, keep_above=BACKFILL_KEEP_CONF
        )
        if removed:
            print(
                f"match-story-for-thesis: thesis={thesis_id} pruned {removed} "
                f"below-floor backfill row(s)",
                file=sys.stderr,
            )
        if links:
            upsert_links(db_path, links)

    return links


def _link_to_dict(link: ThesisStoryLink) -> dict:
    return {
        "thesis_id": link.thesis_id,
        "story_id": link.story_id,
        "relation": link.relation,
        "confidence": round(link.confidence, 3),
        "matched_invalidation": link.matched_invalidation,
        "rationale": link.rationale,
        "retrieval_score": round(link.retrieval_score, 3),
        "best_chunk_key": link.best_chunk_key,
        "source": link.source,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Find the recent stories that matter for one thesis: retrieve, "
            "judge, persist thesis_story_links rows (source='backfill')."
        )
    )
    parser.add_argument("--thesis", required=True, help="Thesis id like thesis_003.")
    parser.add_argument(
        "--window",
        type=int,
        default=14,
        help="Look back this many days of stories. Use 0 for no window (all stories).",
    )
    parser.add_argument("--top-k-per-chunk", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=0.5)
    parser.add_argument("--max-candidates", type=int, default=15)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run retrieval + judge but do not write thesis_story_links rows.",
    )
    args = parser.parse_args()

    window_days: int | None = None if args.window == 0 else args.window
    links = match_story_for_thesis(
        ROOT,
        args.thesis,
        window_days=window_days,
        top_k_per_chunk=args.top_k_per_chunk,
        min_score=args.min_score,
        max_candidates=args.max_candidates,
        max_workers=args.max_workers,
        persist=not args.dry_run,
    )
    print(
        json.dumps(
            {"thesis_id": args.thesis, "links": [_link_to_dict(link) for link in links]},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

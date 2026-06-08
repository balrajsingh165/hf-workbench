#!/usr/bin/env python3
"""Evaluate discover_thesis gates against eligible stories.

Selects stories with `theme_tag != 'other'` AND tagged tickers, runs
`discover_thesis` per story, follows up with the post-creation pipeline for
new theses, and records which gate fired for each story.

Output: per-story trace + aggregate gate-firing table.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.thesis.discover import discover_thesis, run_post_creation_pipeline
from src.thesis.lint import lint_thesis_file

DB_PATH = ROOT / "db" / "hf.db"
THESES_DIR = ROOT / "global" / "theses"


@dataclass(slots=True)
class StoryTrace:
    story_id: str
    theme_tag: str
    action: str  # 'new' | 'existing' | 'none'
    rationale: str = ""
    existing_thesis_id: str | None = None
    similarity_score: float | None = None
    new_thesis_id: str | None = None
    review_status: str | None = None
    rejection_kind: str | None = None  # 'registry' | 'llm' | 'duplicate' | None
    elapsed_seconds: float = 0.0
    error: str | None = None


def _eligible_story_ids(conn: sqlite3.Connection, story_ids: list[str] | None) -> list[str]:
    if story_ids:
        return [sid for sid in story_ids]
    rows = conn.execute(
        """
        SELECT s.id
        FROM story s
        WHERE s.theme_tag != 'other'
          AND EXISTS (
            SELECT 1 FROM entity_tickers
            WHERE entity_type='story' AND entity_id=s.id
          )
        ORDER BY s.id
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


def _classify_rejection(rationale: str) -> str:
    text = (rationale or "").lower()
    if "not in instrument registry" in text:
        return "registry"
    if "already covered" in text or "duplicate" in text or "covers this belief" in text:
        return "duplicate"
    return "llm"


def _run_one(story_id: str, theme_tag: str) -> StoryTrace:
    story_path = ROOT / "global" / "stories" / f"{story_id}.md"
    trace = StoryTrace(story_id=story_id, theme_tag=theme_tag, action="none")
    if not story_path.exists():
        trace.error = "markdown missing"
        return trace
    context = story_path.read_text(encoding="utf-8")

    started = time.time()
    try:
        result = discover_thesis(context, DB_PATH)
    except Exception as exc:
        trace.error = f"discover_thesis raised: {exc}"
        trace.elapsed_seconds = time.time() - started
        return trace
    trace.elapsed_seconds = time.time() - started
    trace.action = result.action
    trace.rationale = (result.rationale or "")[:300]

    if result.action == "existing":
        trace.existing_thesis_id = result.existing_thesis_id
        trace.similarity_score = result.similarity_score
        trace.rejection_kind = "duplicate"
        return trace

    if result.action == "none":
        trace.rejection_kind = _classify_rejection(result.rationale or "")
        return trace

    # action == 'new'
    if result.thesis is None:
        trace.error = "action=new but no thesis returned"
        return trace
    trace.new_thesis_id = result.thesis.thesis_id
    try:
        trace.review_status = run_post_creation_pipeline(ROOT, result.thesis.thesis_id, DB_PATH)
    except Exception as exc:
        trace.error = f"post_creation raised: {exc}"
    return trace


def _format_row(t: StoryTrace) -> str:
    if t.error:
        return f"{t.story_id}  ERROR  {t.error}"
    if t.action == "new":
        rs = t.review_status or "?"
        tail = (
            f"  promotion={rs}"
            if rs != "rejected"
            else f"  promotion=rejected  ({t.rationale or ''})"
        )
        return f"{t.story_id}  NEW  {t.new_thesis_id}{tail}"
    if t.action == "existing":
        return (
            f"{t.story_id}  EXISTING  {t.existing_thesis_id}  "
            f"sim={t.similarity_score:.2f}"
        )
    kind = t.rejection_kind or "llm"
    return f"{t.story_id}  NONE/{kind.upper()}  {t.rationale[:160]}"


def _aggregate(traces: list[StoryTrace]) -> dict:
    out = {
        "total": len(traces),
        "new_active": 0,
        "new_candidate": 0,
        "new_rejected_post_creation": 0,
        "existing_via_cosine": 0,
        "none_llm_rejected": 0,
        "none_duplicate_rejected": 0,
        "none_registry_rejected": 0,
        "errors": 0,
        "max_existing_similarity": 0.0,
    }
    for t in traces:
        if t.error:
            out["errors"] += 1
            continue
        if t.action == "new":
            if t.review_status == "active":
                out["new_active"] += 1
            elif t.review_status == "candidate":
                out["new_candidate"] += 1
            elif t.review_status == "rejected":
                out["new_rejected_post_creation"] += 1
        elif t.action == "existing":
            out["existing_via_cosine"] += 1
            if t.similarity_score and t.similarity_score > out["max_existing_similarity"]:
                out["max_existing_similarity"] = t.similarity_score
        elif t.action == "none":
            kind = t.rejection_kind or "llm"
            if kind == "registry":
                out["none_registry_rejected"] += 1
            elif kind == "duplicate":
                out["none_duplicate_rejected"] += 1
            else:
                out["none_llm_rejected"] += 1
    return out


def _run_lint_mode(ids: list[str] | None, strict: bool) -> int:
    """Grade every thesis markdown file for readability/register compliance.

    Returns 0 when every thesis passes; 1 otherwise (when ``--strict``).

    For each thesis we report banned-vocab hits and price-target hits.
    Aggregates: clean / dirty / issue-density. Designed to be runnable in CI
    after a prompt change to catch regressions in title quality.
    """
    if ids:
        paths = [THESES_DIR / f"{tid}.md" for tid in ids]
    else:
        paths = sorted(THESES_DIR.glob("thesis_*.md"))

    reports = []
    for path in paths:
        if not path.exists():
            print(f"  {path.stem}: file missing")
            continue
        try:
            report = lint_thesis_file(path)
        except Exception as exc:
            print(f"  {path.stem}: parse failed: {exc}")
            continue
        reports.append(report)
        if report.is_clean:
            continue
        print(f"\n  {report.thesis_id}  ({report.issue_count} issue(s))")
        print(f"    title: {report.title}")
        if report.banned_in_title:
            print(f"    banned in title:          {report.banned_in_title}")
        if report.banned_in_core:
            print(f"    banned in core_thesis:    {report.banned_in_core}")
        if report.banned_in_invalidations:
            print(f"    banned in invalidations:  {report.banned_in_invalidations}")
        if report.price_targets_in_title:
            print(f"    price target in title:    {report.price_targets_in_title}")
        if report.price_targets_in_core:
            print(f"    price target in core:     {report.price_targets_in_core}")

    total = len(reports)
    clean = sum(1 for r in reports if r.is_clean)
    dirty = total - clean
    total_issues = sum(r.issue_count for r in reports)

    print("\n" + "=" * 70)
    print("Readability lint summary:")
    print("=" * 70)
    print(f"total theses: {total}")
    print(f"clean:        {clean}")
    print(f"dirty:        {dirty}")
    print(f"total issues: {total_issues}")
    if total:
        print(f"clean rate:   {clean / total:.1%}")

    if strict and dirty > 0:
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--mode",
        choices=["gates", "lint"],
        default="gates",
        help=(
            "gates: run discover_thesis on eligible stories and tally gate "
            "firings (default).  "
            "lint: grade every global/theses/*.md for readability/register."
        ),
    )
    ap.add_argument("--story-ids", nargs="*", help="Specific story IDs (gates mode, default: all eligible).")
    ap.add_argument("--ids", nargs="*", help="Specific thesis IDs (lint mode, default: all).")
    ap.add_argument("--limit", type=int, default=0, help="Max stories to process (gates mode, 0=all).")
    ap.add_argument("--out", type=str, default="", help="Optional JSON dump path for traces (gates mode).")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="lint mode: exit non-zero if any thesis fails the lint.",
    )
    args = ap.parse_args()

    if args.mode == "lint":
        return _run_lint_mode(args.ids, args.strict)

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        story_ids = _eligible_story_ids(conn, args.story_ids)
        rows = {
            r["id"]: r["theme_tag"]
            for r in (
                {"id": sid, "theme_tag": (
                    conn.execute("SELECT theme_tag FROM story WHERE id=?", (sid,)).fetchone()[0]
                )}
                for sid in story_ids
            )
        }
    finally:
        conn.close()

    if args.limit > 0:
        story_ids = story_ids[: args.limit]
    print(f"Evaluating discover_thesis on {len(story_ids)} stories...\n")

    traces: list[StoryTrace] = []
    for story_id in story_ids:
        theme_tag = rows.get(story_id, "?")
        print(f"[{story_id}  theme={theme_tag}] running discover_thesis...", flush=True)
        trace = _run_one(story_id, theme_tag)
        traces.append(trace)
        print(f"  -> {_format_row(trace)}  ({trace.elapsed_seconds:.1f}s)\n", flush=True)

    print("\n" + "=" * 70)
    print("Per-story summary:")
    print("=" * 70)
    for trace in traces:
        print(_format_row(trace))

    print("\n" + "=" * 70)
    print("Gate-firing aggregates:")
    print("=" * 70)
    agg = _aggregate(traces)
    for key, value in agg.items():
        print(f"{key}: {value}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                [{k: getattr(t, k) for k in t.__slots__} for t in traces],
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote traces to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

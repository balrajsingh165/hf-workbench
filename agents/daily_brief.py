"""Daily Market Brief — generate today's brief.

Usage:
  uv run python -m agents.daily_brief
  uv run python -m agents.daily_brief --date 2026-04-24 --force
  uv run python -m agents.daily_brief --dry-run
  uv run python -m agents.daily_brief --no-llm        # skip synthesis (movers-only smoke test)

Re-runnable: the brief is a derived artifact, reruns overwrite. See
`docs/plan-daily-brief.md` for shape and decisions.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.brief.movers import fetch_movers
from src.brief.pipeline import (
    DB_PATH,
    MESH_CACHE_DIR,
    fetch_story_inputs,
    fetch_yesterday_themes,
    persist,
    synthesize,
    verify_provenance,
)
from src.instruments.resolver import to_display


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate today's Daily Market Brief.")
    p.add_argument("--date", default=None, help="ISO date (YYYY-MM-DD). Default: today.")
    p.add_argument("--force", action="store_true", help="Overwrite an existing brief for the date.")
    p.add_argument("--dry-run", action="store_true", help="Print output without writing DB/markdown.")
    p.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM synthesis (use only for movers smoke-testing).",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore cached Mesh response even if present.",
    )
    return p.parse_args()


def _brief_exists(target_date: date) -> bool:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT 1 FROM daily_briefs WHERE brief_date = ?",
            (target_date.isoformat(),),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def main() -> int:
    args = _parse_args()
    target_date = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()

    if _brief_exists(target_date) and not args.force and not args.dry_run:
        print(
            f"[skip] brief for {target_date} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 0

    print(f"[stage 1] fetching inputs for {target_date}…", file=sys.stderr)
    stories = fetch_story_inputs(target_date)
    print(f"  stories={len(stories)} (top-{len(stories)} by recency/sector boost)", file=sys.stderr)
    for s in stories:
        print(f"    {s.id}  {s.created_at[:10]}  {s.headline[:70]}", file=sys.stderr)

    cache_path = MESH_CACHE_DIR / f"{target_date.isoformat()}_movers.json"
    if args.no_cache and cache_path.exists():
        cache_path.unlink()
    movers = fetch_movers(cache_path=cache_path)
    ok = sum(1 for m in movers if m.price is not None)
    print(f"  movers={ok}/{len(movers)} quoted (cache={cache_path.exists()})", file=sys.stderr)
    for m in movers:
        price = f"{m.price:.2f}" if m.price is not None else "—"
        pct = f"{m.pct_change:+.2f}%" if m.pct_change is not None else "—"
        label = to_display(m.spec.symbol, "short")
        print(f"    {m.spec.rank}. {m.spec.symbol:8s} {label:22s} {price:>8s} {pct:>8s}", file=sys.stderr)

    yesterday = fetch_yesterday_themes(target_date)
    print(f"  yesterday_themes={len(yesterday)}", file=sys.stderr)

    if args.no_llm:
        print("\n[stage 2] SKIPPED (--no-llm)", file=sys.stderr)
        return 0

    if not stories:
        print("[abort] no stories in the 48h window; nothing to synthesize.", file=sys.stderr)
        return 1

    print("[stage 2] synthesizing themes…", file=sys.stderr)
    brief = synthesize(target_date, stories, movers, yesterday)
    print(f"  themes={len(brief.themes)}  model={brief.model_version}",
          file=sys.stderr)
    for t in brief.themes:
        print(f"    {t['id']}  {t['text']}", file=sys.stderr)
        print(f"        sources: {', '.join(t['source_story_ids'])}", file=sys.stderr)

    print("[stage 3] verifying provenance…", file=sys.stderr)
    yesterday_source_ids: set[str] = {
        sid
        for t in yesterday
        for sid in (t.get("source_story_ids") or [])
    }
    issues = verify_provenance(brief, stories, yesterday_source_ids=yesterday_source_ids)
    hard_kinds = {"unknown_source", "no_sources"}
    hard_issues = [i for i in issues if i.kind in hard_kinds]
    soft_issues = [i for i in issues if i.kind not in hard_kinds]
    for issue in issues:
        tag = "FAIL" if issue.kind in hard_kinds else "warn"
        print(f"  [{tag}] theme {issue.theme_id} ({issue.kind}): {issue.detail}", file=sys.stderr)
    if hard_issues:
        print("[abort] provenance check failed — not persisting.", file=sys.stderr)
        return 2
    if soft_issues:
        kinds = sorted({i.kind for i in soft_issues})
        print(f"  (soft: {len(soft_issues)} warning(s) [{', '.join(kinds)}] — not blocking)", file=sys.stderr)

    if args.dry_run:
        print("\n[dry-run] would persist:", file=sys.stderr)
        print(json.dumps({
            "brief_date": target_date.isoformat(),
            "themes": brief.themes,
            "source_story_ids": brief.source_story_ids,
            "model_version": brief.model_version,
            "movers": [
                {
                    "rank": m.spec.rank,
                    "symbol": m.spec.symbol,
                    "price": m.price,
                    "pct_change": m.pct_change,
                }
                for m in movers
            ],
        }, indent=2))
        return 0

    md_path = persist(target_date, brief, movers)
    print(f"[stage 3] persisted → {md_path.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

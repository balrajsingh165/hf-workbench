"""A/B-test Daily Brief synthesis: Flash-medium vs Pro.

One-shot script — not part of the production pipeline. Prints both results
side-by-side and runs provenance verification on each.

    uv run python scripts/brief_model_ab.py

Uses today's fetched news + movers + yesterday-themes so both sides see
identical input.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.brief.movers import fetch_movers
from src.brief.pipeline import (
    MESH_CACHE_DIR,
    fetch_story_inputs,
    fetch_yesterday_themes,
    synthesize,
    verify_provenance,
)
from src.clients.gemini import GEMINI_3_1_PRO_PREVIEW, GEMINI_3_FLASH_PREVIEW


@dataclass(slots=True)
class Run:
    label: str
    model: str
    thinking: str | None
    latency_s: float
    themes: list[dict]
    issues: list


def run_one(label: str, model: str, thinking: str | None, *, target_date, stories, movers, yesterday) -> Run:
    t0 = time.perf_counter()
    brief = synthesize(
        target_date, stories, movers, yesterday,
        model=model, thinking_level=thinking,
    )
    latency = time.perf_counter() - t0
    yesterday_source_ids: set[str] = {
        sid
        for t in yesterday
        for sid in (t.get("source_story_ids") or t.get("source_news_ids") or [])
    }
    issues = verify_provenance(brief, stories, yesterday_source_ids=yesterday_source_ids)
    return Run(
        label=label,
        model=model,
        thinking=thinking,
        latency_s=latency,
        themes=brief.themes,
        issues=issues,
    )


def main() -> int:
    target_date = datetime.now(timezone.utc).date()
    stories = fetch_story_inputs(target_date)
    if not stories:
        print("[abort] no stories", file=sys.stderr); return 1
    cache_path = MESH_CACHE_DIR / f"{target_date.isoformat()}_movers.json"
    movers = fetch_movers(cache_path=cache_path)
    yesterday = fetch_yesterday_themes(target_date)

    print(f"inputs: stories={len(stories)}  movers_quoted={sum(1 for m in movers if m.price)}/{len(movers)}  yesterday_themes={len(yesterday)}")

    runs = [
        run_one("FLASH+medium", GEMINI_3_FLASH_PREVIEW, "medium",
                target_date=target_date, stories=stories, movers=movers, yesterday=yesterday),
        run_one("PRO (no-thinking-flag)", GEMINI_3_1_PRO_PREVIEW, None,
                target_date=target_date, stories=stories, movers=movers, yesterday=yesterday),
    ]

    for r in runs:
        print("\n" + "=" * 78)
        print(f"{r.label}  ({r.model}, thinking={r.thinking})  latency={r.latency_s:.2f}s")
        for t in r.themes:
            print(f"\n  [{t['id']}] {t['text']}")
            print(f"       sources: {', '.join(t['source_story_ids'])}")
        if r.issues:
            print("\n  PROVENANCE:")
            for i in r.issues:
                print(f"    [{i.kind}] theme {i.theme_id}: {i.detail}")
        else:
            print("\n  provenance: clean")

    # Also emit raw JSON for each so the user can diff/paste
    print("\n" + "=" * 78)
    print("JSON DUMP (for diffing):\n")
    for r in runs:
        print(f"--- {r.label} ---")
        print(json.dumps({
            "model": r.model, "thinking": r.thinking,
            "latency_s": round(r.latency_s, 2),
            "themes": r.themes,
        }, indent=2))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

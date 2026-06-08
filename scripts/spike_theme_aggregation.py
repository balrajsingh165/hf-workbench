#!/usr/bin/env python3
"""Spike: classify existing stories against the closed theme taxonomy and
report what theme bundles would form.

Read-only. No LLM calls, no DB writes. The hand-classified mapping below
approximates what synthesis-emitted ``theme_tag`` will look like once the
synthesis prompt is updated, so we can sanity-check taxonomy coverage and
bundle thresholds before committing to the design.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.themes import THEME_TAGS

DB_PATH = ROOT / "db" / "hf.db"

BUNDLE_THRESHOLD = 3
WINDOW_DAYS = 14


# Hand-classified story -> theme_tag. Stand-in for what synthesis will emit.
STORY_TAGS: dict[str, str] = {
    "story_025": "fed_policy_uncertainty",
    "story_026": "other",
    "story_027": "crypto_etf_flows",
    "story_028": "ai_capex_cycle",
    "story_029": "ai_capex_cycle",
    "story_030": "us_china_trade",
    "story_031": "fed_easing_cycle",
    "story_032": "russia_ukraine_war",
    "story_034": "ai_capex_cycle",
    "story_035": "middle_east_conflict",
    "story_036": "middle_east_conflict",
    "story_040": "other",
    "story_041": "ecb_policy_shift",
    "story_042": "boe_policy_path",
    "story_043": "boe_policy_path",
    "story_044": "other",
    "story_045": "biotech_fda_cycle",
    "story_046": "us_growth_inflection",
    "story_047": "biotech_fda_cycle",
    "story_048": "biotech_fda_cycle",
    "story_049": "other",
    "story_051": "ecb_policy_shift",
    "story_056": "fed_easing_cycle",
    "story_057": "boe_policy_path",
    "story_059": "us_labor_market_signal",
    "story_060": "biotech_fda_cycle",
    "story_061": "biotech_fda_cycle",
    "story_062": "glp1_obesity_cycle",
    "story_063": "us_labor_market_signal",
}


def _load_stories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            "SELECT id, headline, created_at, sectors_json, regions_json "
            "FROM story ORDER BY created_at DESC"
        )
    )


def main() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        stories = _load_stories(conn)
    finally:
        conn.close()

    tag_to_stories: dict[str, list[sqlite3.Row]] = defaultdict(list)
    unclassified: list[str] = []
    for s in stories:
        tag = STORY_TAGS.get(s["id"])
        if tag is None:
            unclassified.append(s["id"])
            continue
        if tag not in THEME_TAGS:
            print(f"WARN: story {s['id']} mapped to unknown tag '{tag}'")
            continue
        tag_to_stories[tag].append(s)

    tag_order = sorted(tag_to_stories.keys(), key=lambda t: (-len(tag_to_stories[t]), t))

    print("# Theme Aggregation Spike Report")
    print()
    print(f"- Stories scanned: {len(stories)}")
    print(f"- Taxonomy size: {len(THEME_TAGS) - 1} themes + `other`")
    print(f"- Bundle threshold: ≥ {BUNDLE_THRESHOLD} stories per theme over {WINDOW_DAYS}d window")
    print(f"- Hand-classified: {len(stories) - len(unclassified)} / {len(stories)}")
    if unclassified:
        print(f"- Unclassified: {len(unclassified)} → {', '.join(unclassified)}")
    print()

    print("## Distribution by Theme")
    print()
    print("| Theme | Count | Verdict |")
    print("|---|---|---|")
    for tag in tag_order:
        count = len(tag_to_stories[tag])
        if tag == "other":
            verdict = "skip discovery"
        elif count >= BUNDLE_THRESHOLD:
            verdict = "**BUNDLE → discover_thesis**"
        else:
            verdict = "solo / wait for ≥3"
        print(f"| `{tag}` | {count} | {verdict} |")
    print()

    print(f"## Bundle Candidates (≥ {BUNDLE_THRESHOLD} stories)")
    print()
    bundles = [
        (t, ss)
        for t, ss in tag_to_stories.items()
        if t != "other" and len(ss) >= BUNDLE_THRESHOLD
    ]
    if not bundles:
        print("_None at current corpus size._")
        print()
    for tag, ss in sorted(bundles, key=lambda x: -len(x[1])):
        print(f"### `{tag}` — {len(ss)} stories")
        print(f"_{THEME_TAGS[tag]}_")
        print()
        for s in ss:
            print(f"- `{s['id']}` ({s['created_at'][:10]}) — {s['headline']}")
        print()

    print("## Solo / Wait (1–2 stories per theme)")
    print()
    solo = [
        (t, ss)
        for t, ss in tag_to_stories.items()
        if t != "other" and 0 < len(ss) < BUNDLE_THRESHOLD
    ]
    for tag, ss in sorted(solo, key=lambda x: (-len(x[1]), x[0])):
        for s in ss:
            print(f"- `{s['id']}` ({s['created_at'][:10]}) [`{tag}`] {s['headline']}")
    print()

    print("## `other` (no thesis discovery)")
    print()
    for s in tag_to_stories.get("other", []):
        print(f"- `{s['id']}` ({s['created_at'][:10]}) — {s['headline']}")
    print()


if __name__ == "__main__":
    main()

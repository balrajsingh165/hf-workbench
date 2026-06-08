"""Dry-run the STORY discovery prompt over a curated set of recent stories to
check the durable-theme criteria (quality-bar rule 4).

No DB writes, no embedding/registry-grounding/persist. Calls the Gemini model
directly with the live ``DISCOVER_STORY_SYSTEM_PROMPT`` and reports, per story,
either the rejection_reason or the candidate titles. For prompt-quality testing
only.

Usage:
    uv run python scripts/test_discover_durability.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients.gemini import GEMINI_3_1_PRO_PREVIEW, generate_text_with_retry
from src.thesis.discover import (
    DISCOVER_STORY_JSON_SCHEMA,
    DISCOVER_STORY_SYSTEM_PROMPT,
    _format_registry_block,
)

STORIES_DIR = ROOT / "global" / "stories"

# (story_id, expectation) — expectation is what a correct prompt SHOULD do.
TEST_CASES = [
    # One-off corporate events — must REJECT (or reframe to a durable theme).
    ("story_1173", "REJECT: GlobalFoundries acquisition (single-co M&A) [was thesis_099]"),
    ("story_1158", "REJECT: Berkshire/Taylor Morrison buyout (single buyout) [was 091/096]"),
    ("story_1149", "REJECT: Amazon Prime Day event (one-off promo) [was 093/094]"),
    ("story_1172", "REJECT: Watsco acquires Jackson Supply (single-co M&A)"),
    ("story_1159", "REJECT: WTW acquires Redefind (single-co M&A)"),
    ("story_1178", "REJECT: Wedbush raises IBM price target (analyst PT, one-off)"),
    # Endorsed second-order case — should produce a SECTOR thesis, not reject.
    ("story_1151", "ALLOW (sector): SpaceX IPO -> space sector capital rotation"),
    # Durable themes — should PASS with strong theses.
    ("story_1164", "PASS: NatGas / LNG export flows (supply-chain dynamic)"),
    ("story_1157", "PASS: gold over Treasuries as reserve asset (macro regime)"),
    ("story_1161", "PASS: Saudi oil price cuts to Asia (commodity/macro)"),
]

REGISTRY_BLOCK = _format_registry_block()


def _story_context(story_id: str) -> str | None:
    path = STORIES_DIR / f"{story_id}.md"
    if not path.exists():
        return None
    return path.read_text()


def _run_one(story_id: str) -> dict:
    context = _story_context(story_id)
    if context is None:
        return {"error": "story markdown not found"}
    user_prompt = "\n".join(
        [
            "## Story\n",
            context,
            "\n\n## Instrument Registry\n",
            "Use ONLY symbols from this table. Unknown symbols -> thesis rejected.\n",
            "```\n" + REGISTRY_BLOCK + "\n```",
        ]
    )
    result = generate_text_with_retry(
        contents=user_prompt,
        model=GEMINI_3_1_PRO_PREVIEW,
        system_instruction=DISCOVER_STORY_SYSTEM_PROMPT,
        thinking_level="medium",
        response_mime_type="application/json",
        response_json_schema=DISCOVER_STORY_JSON_SCHEMA,
    )
    return json.loads(result.text)


def main() -> None:
    for story_id, expectation in TEST_CASES:
        print("=" * 78)
        print(f"{story_id}  |  EXPECT -> {expectation}")
        try:
            parsed = _run_one(story_id)
        except Exception as exc:  # noqa: BLE001 - test harness, surface anything
            print(f"  ERROR: {type(exc).__name__}: {exc}")
            continue
        if "error" in parsed:
            print(f"  {parsed['error']}")
            continue
        candidates = parsed.get("candidates") or []
        if not candidates:
            print(f"  REJECTED -> {parsed.get('rejection_reason') or '(no reason)'}")
            continue
        print(f"  {len(candidates)} candidate(s):")
        for c in candidates:
            tickers = ", ".join(
                f"{t.get('symbol')}({t.get('direction','?')[:4]})"
                for t in (c.get("tickers") or [])
            )
            print(f"    - {c.get('thesis_statement')}")
            print(f"      tickers: {tickers}")


if __name__ == "__main__":
    main()

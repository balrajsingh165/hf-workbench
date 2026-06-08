"""Dry-run the thesis discovery LLM call against multiple news articles.

Bypasses embedding/DB writes — calls the Gemini model directly with the
current `DISCOVER_SYSTEM_PROMPT` and reports each generated title plus a
compact word/violation report. For prompt-quality testing only.

Usage:
    uv run python scripts/test_discover_titles.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients.gemini import GEMINI_3_1_PRO_PREVIEW, generate_text_with_retry
from src.thesis.discover import DISCOVER_JSON_SCHEMA, DISCOVER_SYSTEM_PROMPT

NEWS_DIR = ROOT / "global" / "news"
THESES_DIR = ROOT / "global" / "theses"

TEST_CASES = [
    "news_005.md",  # ServiceNow earnings beat
    "news_011.md",  # iPhone 18 Pro variable aperture
    "news_018.md",  # Nuclear / SMR / Meta nuclear deals
    "news_020.md",  # Strait of Hormuz oil
    "news_031.md",  # Meta layoffs / AI pivot
    "news_040.md",  # DOJ drops Powell probe / Warsh confirmation
    "news_045.md",  # Meta layoffs / $135B AI capex (overlaps 031)
    "news_049.md",  # Storage Wars death (non-finance)
    "news_052.md",  # China rare earth export controls (overlaps thesis_013)
    "news_060.md",  # Iran ceasefire (geopolitical)
]

BANNED_CONJ = re.compile(r"\b(while|despite|but|although|as well as)\b", re.IGNORECASE)
HEDGE = re.compile(r"\b(might|could|may|possibly)\b", re.IGNORECASE)
SPELLED_OUT = re.compile(
    r"\b(artificial intelligence|electric vehicles?|gross domestic product"
    r"|consumer price index|earnings per share)\b",
    re.IGNORECASE,
)
AND_TWO_VERBS = re.compile(
    # crude heuristic: "<verb> X and <verb> Y" with two distinct verbs
    r"\b(compress|widen|broaden|tighten|re-rate|re-rates|compresses|widens|broadens|"
    r"tightens|drive|drives|delay|delays|trigger|triggers|fade|fades|cap|caps|"
    r"sustain|sustains|fund|funds)\b[^,]+\band\b[^,]+\b(compress|widen|broaden|"
    r"tighten|re-rate|re-rates|compresses|widens|broadens|tightens|drive|drives|"
    r"delay|delays|trigger|triggers|fade|fades|cap|caps|sustain|sustains|fund|funds)\b",
    re.IGNORECASE,
)


def _existing_titles_block() -> str:
    titles: list[str] = []
    for path in sorted(THESES_DIR.glob("thesis_*.md")):
        first = path.read_text(encoding="utf-8").splitlines()[0]
        m = re.match(r"^# Thesis:\s*(.+?)\s*$", first)
        if m:
            titles.append(f"- [{path.stem}]: {m.group(1)}")
    return "Nearby existing theses (do NOT duplicate these):\n" + "\n".join(titles)


def _critique(title: str) -> list[str]:
    flags: list[str] = []
    words = title.strip().rstrip(".").split()
    if len(words) > 12:
        flags.append(f"length={len(words)}>12")
    if BANNED_CONJ.search(title):
        flags.append("two-clause-conjunction")
    if HEDGE.search(title):
        flags.append("hedge-word")
    if SPELLED_OUT.search(title):
        flags.append("spelled-out-abbrev")
    if AND_TWO_VERBS.search(title):
        flags.append("and-joins-two-verbs")
    # Heuristic proper-noun check: capitalized word that isn't sentence-initial
    # and isn't a known abstract-noun start. Surface for human review.
    KNOWN_ABBREV = {"SaaS", "CapEx", "OpEx", "EVs", "ASIC", "ASICs"}
    proper = [
        w for i, w in enumerate(words)
        if i > 0
        and w[:1].isupper()
        and w.isalpha()
        and w.upper() != w  # skip acronyms like AI, US, OPEC, GDP
        and w not in KNOWN_ABBREV
    ]
    if proper:
        flags.append(f"proper-nouns?={proper}")
    return flags


def run_one(news_path: Path, nearby_block: str) -> dict:
    context = news_path.read_text(encoding="utf-8")
    user_prompt = f"## Context\n\n{context}\n\n## Existing Theses\n\n{nearby_block}"

    result = generate_text_with_retry(
        contents=user_prompt,
        model=GEMINI_3_1_PRO_PREVIEW,
        system_instruction=DISCOVER_SYSTEM_PROMPT,
        thinking_level="medium",
        response_mime_type="application/json",
        response_json_schema=DISCOVER_JSON_SCHEMA,
    )
    return json.loads(result.text)


def main() -> None:
    nearby_block = _existing_titles_block()
    print("=" * 78)
    print("DISCOVER TITLE DRY-RUN")
    print("=" * 78)
    for fname in TEST_CASES:
        path = NEWS_DIR / fname
        if not path.exists():
            print(f"\n[{fname}] missing")
            continue
        headline = path.read_text(encoding="utf-8").splitlines()[0]
        print(f"\n--- {fname} ---")
        print(f"news: {headline}")
        try:
            parsed = run_one(path, nearby_block)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            continue
        if not parsed.get("has_thesis"):
            print(f"  REJECTED: {parsed.get('rejection_reason', '(no reason)')}")
            continue
        title = parsed.get("thesis_statement", "")
        words = len(title.rstrip(".").split())
        flags = _critique(title)
        print(f"  TITLE   ({words}w): {title}")
        ticker_lines = [
            f"{t.get('symbol')}({t.get('direction','?')[:4]})"
            for t in parsed.get("tickers", [])
        ]
        print(f"  tickers : {', '.join(ticker_lines)}")
        print(f"  horizon : {parsed.get('horizon_days')}d")
        print(f"  core    : {parsed.get('core_thesis', '')[:200]}...")
        if flags:
            print(f"  FLAGS   : {flags}")
        else:
            print("  FLAGS   : (clean)")


if __name__ == "__main__":
    main()

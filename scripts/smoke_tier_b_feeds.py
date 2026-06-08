#!/usr/bin/env python3
"""Smoke Tier B/trade/regional feed volume and gate pass rates."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.firehose import (
    POLITICS_MARKET_FEEDS,
    REGIONAL_FEEDS,
    TIER_B_FEEDS,
    TRADE_PRESS_FEEDS,
    parse_feed,
)
from src.news.firehose_gate import build_alias_index


def main() -> int:
    ci_index, cs_index = build_alias_index()
    feeds = (
        ("tier-b", TIER_B_FEEDS),
        ("trade", TRADE_PRESS_FEEDS),
        ("regional", REGIONAL_FEEDS),
        ("politics", POLITICS_MARKET_FEEDS),
    )
    for group, urls in feeds:
        print(f"\n## {group}")
        for url in urls:
            try:
                entries = parse_feed(url, ci_index, cs_index, max_items=20)
            except Exception as exc:
                print(f"- FAIL {url}: {exc}")
                continue
            passes = sum(1 for e in entries if e.exchange_tickers or e.registry_symbols or e.macros)
            print(f"- {passes:2d}/{len(entries):2d} pass — {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

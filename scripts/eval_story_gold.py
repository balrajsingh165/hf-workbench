#!/usr/bin/env python3
"""Evaluate deterministic story quality gates against checked-in gold files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.verifier import verify_story_payload

GOLD_DIR = ROOT / "db" / "story_gold"


def main() -> int:
    failures: list[str] = []
    files = sorted(GOLD_DIR.glob("*.json"))
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        member_bodies = {
            str(member["news_id"]): str(member.get("body") or "")
            for member in data.get("members") or []
        }
        result = verify_story_payload(
            data["payload"],
            member_ids=set(member_bodies),
            member_bodies=member_bodies,
        )
        expect_ok = bool(data.get("expect_verifier_ok", True))
        if result.ok != expect_ok:
            failures.append(
                f"{path.name}: verifier ok={result.ok}, expected {expect_ok}: {result.errors}"
            )
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 1
    print(f"PASS {len(files)} story gold file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

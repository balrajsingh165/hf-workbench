#!/usr/bin/env python3
"""Print a story and its Gate-2-matched thesis side-by-side for hand review.

Defaults to the 6 cases from the most recent discover_thesis evaluation. Pass
``--pair STORY THESIS`` to inspect any other pair.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "db" / "hf.db"

DEFAULT_PAIRS: list[tuple[str, str, float]] = [
    ("story_027", "thesis_017", 0.86),
    ("story_029", "thesis_011", 0.81),
    ("story_032", "thesis_013", 0.84),
    ("story_034", "thesis_020", 0.81),
    ("story_036", "thesis_013", 0.89),
    ("story_046", "thesis_001", 0.82),
]


def _json(value: str | None, default):
    try:
        data = json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return data if isinstance(data, type(default)) else default


def _story_block(conn: sqlite3.Connection, story_id: str) -> str:
    row = conn.execute(
        """
        SELECT s.id, s.theme_tag, s.headline, s.what_changed,
               s.overview_json, s.claims_json, s.market_relevance_json,
               c.event_class
        FROM story s JOIN news_cluster c ON c.id = s.cluster_id
        WHERE s.id = ?
        """,
        (story_id,),
    ).fetchone()
    if not row:
        return f"  (story {story_id} not found)"
    overview = [str(b.get("text") or "").strip() for b in _json(row[4], []) if isinstance(b, dict)]
    claims = [str(c.get("text") or "").strip() for c in _json(row[5], []) if isinstance(c, dict)]
    rel = _json(row[6], {})
    tickers = conn.execute(
        "SELECT symbol FROM entity_tickers WHERE entity_type='story' AND entity_id=? ORDER BY symbol",
        (story_id,),
    ).fetchall()
    parts = [
        f"  ID:           {row[0]}",
        f"  Theme tag:    {row[1]}",
        f"  Event class:  {row[7] or '(none)'}",
        f"  Headline:     {row[2]}",
        f"  What changed: {row[3]}",
        f"  Tickers:      {', '.join(t[0] for t in tickers) or '(none)'}",
        f"  Sectors:      {', '.join(rel.get('sectors') or []) or '(none)'}",
        f"  Direction:    {rel.get('direction') or '(none)'}  Horizon: {rel.get('horizon') or '(none)'}",
        "  Overview:",
    ]
    for b in overview[:5]:
        parts.append(f"    - {b}")
    if claims:
        parts.append("  Claims:")
        for c in claims[:3]:
            parts.append(f"    - {c}")
    return "\n".join(parts)


def _thesis_block(conn: sqlite3.Connection, thesis_id: str) -> str:
    md_path = ROOT / "global" / "theses" / f"{thesis_id}.md"
    if not md_path.exists():
        return f"  (thesis {thesis_id} markdown missing)"
    body = md_path.read_text(encoding="utf-8").rstrip()
    tickers = conn.execute(
        "SELECT symbol, direction FROM entity_tickers WHERE entity_type='thesis' AND entity_id=? ORDER BY symbol",
        (thesis_id,),
    ).fetchall()
    review = conn.execute(
        "SELECT review_status FROM theses WHERE id=?",
        (thesis_id,),
    ).fetchone()
    parts = [
        f"  ID:            {thesis_id}",
        f"  Review status: {review[0] if review else '(unknown)'}",
        f"  Tickers:       {', '.join(f'{s} ({d})' for s, d in tickers) or '(none)'}",
        "",
    ]
    parts.append("  " + body.replace("\n", "\n  "))
    return "\n".join(parts)


def _inspect_pair(conn: sqlite3.Connection, story_id: str, thesis_id: str, score: float | None) -> None:
    header = f"=== {story_id} ↔ {thesis_id}"
    if score is not None:
        header += f"  (similarity {score:.2f})"
    header += " ==="
    print("\n" + "=" * len(header))
    print(header)
    print("=" * len(header))
    print("\n[STORY]")
    print(_story_block(conn, story_id))
    print("\n[MATCHED THESIS]")
    print(_thesis_block(conn, thesis_id))
    print("\n[YOUR CALL]")
    print("  duplicate / distinct / borderline ?")
    print("  notes:")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pair", nargs=2, metavar=("STORY", "THESIS"),
                    help="Inspect a single (story_id, thesis_id) pair.")
    ap.add_argument("--from-trace", type=str, default="",
                    help="Path to JSON trace dump from eval_discover_thesis.py "
                         "(reads action='existing' rows).")
    args = ap.parse_args()

    if args.pair:
        pairs = [(args.pair[0], args.pair[1], None)]
    elif args.from_trace:
        data = json.loads(Path(args.from_trace).read_text(encoding="utf-8"))
        pairs = [
            (row["story_id"], row["existing_thesis_id"], row.get("similarity_score"))
            for row in data
            if row.get("action") == "existing"
        ]
    else:
        pairs = DEFAULT_PAIRS

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        for story_id, thesis_id, score in pairs:
            _inspect_pair(conn, story_id, thesis_id, score)
    finally:
        conn.close()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Backfill story.theme_tag for stories created before theme_tag emission.

These rows carry the migration default `other`. We re-tag each story against
the closed taxonomy via a focused Gemini call — no full re-synthesis, just a
single enum pick over the existing story payload.
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

from src.clients.gemini import GEMINI_3_FLASH_PREVIEW, generate_text_with_retry
from src.news.themes import ALL_TAGS, THEME_TAGS

DB_PATH = ROOT / "db" / "hf.db"

_TAG_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "theme_tag": {"type": "string", "enum": list(ALL_TAGS)},
        "rationale": {"type": "string"},
    },
    "required": ["theme_tag", "rationale"],
}


def _json_list_of_dicts(value: str | None) -> list[dict]:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _json_dict(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _texts(items: list[dict]) -> list[str]:
    return [str(it.get("text") or "").strip() for it in items if it.get("text")]


def _build_context(row: sqlite3.Row) -> str:
    overview = _texts(_json_list_of_dicts(row["overview_json"]))
    claims = _texts(_json_list_of_dicts(row["claims_json"]))
    relevance = _json_dict(row["market_relevance_json"])
    return json.dumps({
        "headline": row["headline"],
        "what_changed": row["what_changed"],
        "overview": overview,
        "claims": claims,
        "market_relevance": {
            "tickers": relevance.get("tickers") or [],
            "sectors": relevance.get("sectors") or [],
            "regions": relevance.get("regions") or [],
            "direction": relevance.get("direction"),
            "horizon": relevance.get("horizon"),
        },
        "event_class": row["event_class"],
    }, indent=2)


def _classify(context: str) -> tuple[str, str]:
    prompt = f"""Pick exactly one theme_tag from the closed taxonomy below for this story.

Story payload:
{context}

theme_tag rules:
- Pick the tag that names the durable, multi-week market context the story belongs to.
- A theme is broader than the headline event. It should outlive any single news cycle and frame how the story would inform a multi-week trading thesis.
- Emit `other` when no tag fits — single-name earnings without sector implication, isolated regulatory matters, structural news with limited cross-reading. `other` stories are excluded from thesis discovery.
- Do NOT invent new tags. The list is closed.
- Provide a one-sentence rationale citing the specific signal in the payload that anchored the choice.

Closed theme taxonomy (tag — meaning):
{chr(10).join(f"- {tag}: {desc}" for tag, desc in THEME_TAGS.items())}
"""
    res = generate_text_with_retry(
        prompt,
        model=GEMINI_3_FLASH_PREVIEW,
        response_mime_type="application/json",
        response_json_schema=_TAG_SCHEMA,
        thinking_level="low",
    )
    data = json.loads(res.text)
    tag = str(data.get("theme_tag") or "").strip()
    if tag not in ALL_TAGS:
        tag = "other"
    rationale = str(data.get("rationale") or "").strip()
    return tag, rationale


def backfill(*, write: bool = False, max_id: str = "story_064") -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    changed = 0
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.headline, s.what_changed, s.overview_json,
                   s.claims_json, s.market_relevance_json, s.theme_tag,
                   c.event_class
            FROM story s
            JOIN news_cluster c ON c.id = s.cluster_id
            WHERE s.kind = 'story'
              AND s.id < ? AND s.theme_tag = 'other'
            ORDER BY s.id
            """,
            (max_id,),
        ).fetchall()
        for row in rows:
            try:
                tag, rationale = _classify(_build_context(row))
            except Exception as exc:
                print(f"{row['id']}: ERROR — {exc}")
                continue
            print(f"{row['id']}: {tag}  ({rationale})")
            if tag == "other":
                continue
            changed += 1
            if write:
                with conn:
                    conn.execute(
                        "UPDATE story SET theme_tag=? WHERE id=?",
                        (tag, row["id"]),
                    )
    finally:
        conn.close()
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="Apply UPDATE statements (otherwise dry-run).")
    ap.add_argument("--max-id", default="story_064",
                    help="Only backfill stories with id < this value (default: story_064).")
    args = ap.parse_args()
    changed = backfill(write=args.write, max_id=args.max_id)
    mode = "updated" if args.write else "would update"
    print(f"\n{mode}: {changed} story row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

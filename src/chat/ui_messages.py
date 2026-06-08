from __future__ import annotations

import json
from sqlite3 import Row
from typing import Any


def _json_loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def rows_to_ui_messages(rows: list[Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        role = row["role"]
        parts = _json_loads(row["parts_json"] or "[]", [])
        if not isinstance(parts, list):
            parts = []
        if not parts and row["content_text"]:
            parts = [{"type": "text", "text": row["content_text"], "state": "done"}]
        out.append(
            {
                "id": row["id"],
                "role": role,
                "parts": parts,
            }
        )
    return out


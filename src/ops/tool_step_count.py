"""Count tool steps the same way the frontend session UI does."""

from __future__ import annotations

import json
from typing import Any

# Keep in sync with heurist-finance-frontend src/lib/chat/message-parts.ts
_TOOL_STREAM_CHUNK_TYPES = frozenset({
    "tool-input-start",
    "tool-input-available",
    "tool-output-available",
})


def is_tool_display_part(part: dict[str, Any]) -> bool:
    ptype = part.get("type")
    if not isinstance(ptype, str):
        return False
    if ptype == "dynamic-tool":
        return True
    if not ptype.startswith("tool-"):
        return False
    return ptype not in _TOOL_STREAM_CHUNK_TYPES


def collect_tool_steps(parts: list[Any]) -> int:
    """One per toolCallId (merged), matching collectToolSteps()."""
    if not isinstance(parts, list):
        return 0
    seen: set[str] = set()
    extra = 0
    for raw in parts:
        if not isinstance(raw, dict) or not is_tool_display_part(raw):
            continue
        call_id = raw.get("toolCallId")
        if isinstance(call_id, str) and call_id:
            seen.add(call_id)
        else:
            extra += 1
    return len(seen) + extra


def count_tool_steps_in_parts_json(parts_json: str | None) -> int:
    if not parts_json:
        return 0
    try:
        parts = json.loads(parts_json)
    except json.JSONDecodeError:
        return 0
    return collect_tool_steps(parts if isinstance(parts, list) else [])


def count_session_tool_steps(conn: Any, session_id: str) -> tuple[int, int]:
    """Return (session_total, last_assistant_turn) tool step counts."""
    rows = conn.execute(
        """
        SELECT role, parts_json, created_at
        FROM agent_messages
        WHERE session_id = ?
        ORDER BY created_at ASC
        """,
        (session_id,),
    ).fetchall()

    total = 0
    last_assistant = 0
    for row in rows:
        role = str(row[0] if not hasattr(row, "keys") else row["role"])
        parts_json = row[1] if not hasattr(row, "keys") else row["parts_json"]
        n = count_tool_steps_in_parts_json(parts_json)
        if role == "assistant":
            total += n
            last_assistant = n
    return total, last_assistant

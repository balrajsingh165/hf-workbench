from __future__ import annotations

import json
import sqlite3

from src.ops.tool_step_count import collect_tool_steps, count_session_tool_steps


def test_collect_tool_steps_merges_same_tool_call_id() -> None:
    parts = [
        {"type": "tool-search_macro", "toolCallId": "c1", "state": "input-available"},
        {"type": "tool-search_macro", "toolCallId": "c1", "state": "output-available"},
        {"type": "tool-price_summary", "toolCallId": "c2", "state": "output-available"},
    ]
    assert collect_tool_steps(parts) == 2


def test_collect_tool_steps_ignores_stream_chunk_types() -> None:
    parts = [
        {"type": "tool-input-available", "toolCallId": "c1"},
        {"type": "tool-output-available", "toolCallId": "c1"},
        {"type": "tool-web_search", "toolCallId": "c1", "state": "output-available"},
    ]
    assert collect_tool_steps(parts) == 1


def test_count_session_tool_steps_sums_assistant_turns() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE agent_messages (
            session_id TEXT, role TEXT, parts_json TEXT, created_at TEXT
        )
        """
    )
    turn1 = json.dumps([
        {"type": "tool-a", "toolCallId": "1", "state": "output-available"},
        {"type": "tool-a", "toolCallId": "1", "state": "input-available"},
        {"type": "tool-b", "toolCallId": "2", "state": "output-available"},
    ])
    turn2 = json.dumps([
        {"type": "tool-c", "toolCallId": "3", "state": "output-available"},
    ])
    conn.execute(
        "INSERT INTO agent_messages VALUES ('s1', 'user', '[]', 't1')"
    )
    conn.execute(
        "INSERT INTO agent_messages VALUES ('s1', 'assistant', ?, 't2')",
        (turn1,),
    )
    conn.execute(
        "INSERT INTO agent_messages VALUES ('s1', 'user', '[]', 't3')"
    )
    conn.execute(
        "INSERT INTO agent_messages VALUES ('s1', 'assistant', ?, 't4')",
        (turn2,),
    )
    total, last = count_session_tool_steps(conn, "s1")
    assert total == 3
    assert last == 1

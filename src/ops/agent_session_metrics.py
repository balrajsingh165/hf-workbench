"""Recent agent chat sessions with usage stats for the dev metrics console."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.ops.tool_step_count import count_session_tool_steps

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "db" / "hf.db"


def _short_session_id(full_session_id: str) -> str:
    parts = full_session_id.split(":")
    if len(parts) >= 3:
        return ":".join(parts[2:])
    return full_session_id


def _empty_phase_usage() -> dict[str, int | float | None]:
    return {
        "input_uncached": 0,
        "cache_read": 0,
        "cache_write": 0,
        "output": 0,
        "cost_usd": 0.0,
        "model_id": None,
    }


def _fetch_phase_usage_by_session(
    conn: sqlite3.Connection,
    session_ids: list[str],
) -> dict[str, dict[str, dict[str, int | float | None]]]:
    """Per-session research/response/chart token buckets (excludes aggregate rows)."""
    if not session_ids:
        return {}
    placeholders = ",".join("?" * len(session_ids))
    rows = conn.execute(
        f"""
        SELECT
          u.session_id,
          u.phase,
          SUM(u.input_tokens) AS input_uncached,
          SUM(u.cache_read_tokens) AS cache_read,
          SUM(u.cache_write_tokens) AS cache_write,
          SUM(u.output_tokens) AS output,
          ROUND(SUM(u.cost_usd), 6) AS cost_usd,
          (
            SELECT u2.model_id
            FROM agent_usage u2
            WHERE u2.session_id = u.session_id
              AND u2.phase = u.phase
            ORDER BY u2.created_at DESC, u2.id DESC
            LIMIT 1
          ) AS model_id
        FROM agent_usage u
        WHERE u.session_id IN ({placeholders})
          AND u.phase IN ('research', 'response', 'chart')
        GROUP BY u.session_id, u.phase
        """,
        session_ids,
    ).fetchall()
    out: dict[str, dict[str, dict[str, int | float | None]]] = {}
    for sid in session_ids:
        out[sid] = {
            "research": _empty_phase_usage(),
            "response": _empty_phase_usage(),
            "chart": _empty_phase_usage(),
        }
    for row in rows:
        sid = str(row["session_id"])
        phase = str(row["phase"])
        if sid not in out or phase not in out[sid]:
            continue
        out[sid][phase] = {
            "input_uncached": int(row["input_uncached"] or 0),
            "cache_read": int(row["cache_read"] or 0),
            "cache_write": int(row["cache_write"] or 0),
            "output": int(row["output"] or 0),
            "cost_usd": float(row["cost_usd"] or 0),
            "model_id": row["model_id"],
        }
    return out


def _parse_session_mode(metadata_json: str | None) -> str | None:
    if not metadata_json:
        return None
    try:
        meta = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    params = meta.get("params")
    if not isinstance(params, dict):
        return None
    mode = params.get("mode")
    if mode in ("quick", "deep"):
        return str(mode)
    return None


def _fetch_session_modes(
    conn: sqlite3.Connection,
    session_ids: list[str],
) -> dict[str, str | None]:
    """Latest response mode from persisted session metadata (last turn wins)."""
    if not session_ids:
        return {}
    placeholders = ",".join("?" * len(session_ids))
    rows = conn.execute(
        f"""
        SELECT session_id, metadata_json
        FROM agent_sessions
        WHERE session_id IN ({placeholders})
        """,
        session_ids,
    ).fetchall()
    return {
        str(row["session_id"]): _parse_session_mode(row["metadata_json"])
        for row in rows
    }


def collect_recent_agent_sessions(
    conn: sqlite3.Connection,
    *,
    limit: int = 40,
    days: int = 7,
) -> dict[str, Any]:
    """Aggregate per-session stats from agent_usage + tool counts from messages."""
    limit = max(1, min(int(limit), 200))
    days = max(1, min(int(days), 90))
    window = f"-{days} days"

    usage_rows = conn.execute(
        """
        SELECT
          u.session_id,
          u.user_id,
          COUNT(DISTINCT u.request_id) AS turns,
          SUM(u.cost_usd) AS cost_usd,
          SUM(u.output_tokens) AS output_tokens,
          SUM(u.input_tokens) AS input_tokens,
          AVG(u.latency_ms) AS avg_latency_ms,
          MAX(u.latency_ms) AS max_latency_ms,
          MAX(u.created_at) AS last_at,
          MIN(u.created_at) AS first_at,
          SUM(CASE WHEN u.status != 'ok' THEN 1 ELSE 0 END) AS error_turns,
          MAX(u.endpoint) AS last_endpoint,
          MAX(u.model_id) AS last_model_id
        FROM agent_usage u
        WHERE u.phase = 'aggregate'
          AND u.session_id IS NOT NULL
          AND TRIM(u.session_id) != ''
          AND u.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
        GROUP BY u.session_id, u.user_id
        ORDER BY last_at DESC
        LIMIT ?
        """,
        (window, limit),
    ).fetchall()

    if not usage_rows:
        return {"available": True, "days": days, "limit": limit, "sessions": []}

    session_ids = [str(row["session_id"]) for row in usage_rows]
    placeholders = ",".join("?" * len(session_ids))

    titles: dict[str, str] = {}
    title_rows = conn.execute(
        f"""
        SELECT session_id, title
        FROM chat_titles
        WHERE session_id IN ({placeholders})
        """,
        session_ids,
    ).fetchall()
    for row in title_rows:
        titles[str(row["session_id"])] = str(row["title"])

    tool_totals: dict[str, tuple[int, int]] = {}
    for sid in session_ids:
        tool_totals[sid] = count_session_tool_steps(conn, sid)

    phase_usage = _fetch_phase_usage_by_session(conn, session_ids)
    session_modes = _fetch_session_modes(conn, session_ids)

    response_chars: dict[str, int] = {}
    char_rows = conn.execute(
        f"""
        SELECT session_id, SUM(LENGTH(content_text)) AS chars
        FROM agent_messages
        WHERE session_id IN ({placeholders})
          AND role = 'assistant'
        GROUP BY session_id
        """,
        session_ids,
    ).fetchall()
    for row in char_rows:
        response_chars[str(row["session_id"])] = int(row["chars"] or 0)

    sessions: list[dict[str, Any]] = []
    for row in usage_rows:
        sid = str(row["session_id"])
        sessions.append({
            "session_id": sid,
            "short_session_id": _short_session_id(sid),
            "user_id": str(row["user_id"]),
            "title": titles.get(sid) or _short_session_id(sid),
            "turns": int(row["turns"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0), 6),
            "output_tokens": int(row["output_tokens"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "token_usage": phase_usage.get(sid, {
                "research": _empty_phase_usage(),
                "response": _empty_phase_usage(),
                "chart": _empty_phase_usage(),
            }),
            "mode": session_modes.get(sid),
            "response_chars": response_chars.get(sid, 0),
            "tool_calls": tool_totals.get(sid, (0, 0))[0],
            "tool_calls_last_turn": tool_totals.get(sid, (0, 0))[1],
            "avg_latency_ms": round(float(row["avg_latency_ms"]), 1)
            if row["avg_latency_ms"] is not None
            else None,
            "max_latency_ms": int(row["max_latency_ms"])
            if row["max_latency_ms"] is not None
            else None,
            "error_turns": int(row["error_turns"] or 0),
            "last_endpoint": row["last_endpoint"],
            "last_model_id": row["last_model_id"],
            "first_at": row["first_at"],
            "last_at": row["last_at"],
        })

    totals = {
        "sessions": len(sessions),
        "turns": sum(s["turns"] for s in sessions),
        "cost_usd": round(sum(s["cost_usd"] for s in sessions), 6),
        "tool_calls": sum(s["tool_calls"] for s in sessions),
    }

    return {
        "available": True,
        "days": days,
        "limit": limit,
        "totals": totals,
        "sessions": sessions,
    }


def collect_agent_sessions_from_db(
    db_path: Path | None = None,
    *,
    limit: int = 40,
    days: int = 7,
) -> dict[str, Any]:
    path = db_path or DEFAULT_DB
    if not path.exists():
        return {"available": False, "reason": "db_missing"}
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_usage'"
        ).fetchone()
        if not row:
            return {"available": False, "reason": "no_agent_usage_table"}
        return collect_recent_agent_sessions(conn, limit=limit, days=days)
    finally:
        conn.close()

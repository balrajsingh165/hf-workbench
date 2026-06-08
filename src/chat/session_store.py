from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from api import DB_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


CHAT_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS agent_sessions (
      session_id TEXT PRIMARY KEY,
      platform TEXT NOT NULL,
      user_id TEXT NOT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
      updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_messages (
      id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
      role TEXT NOT NULL,
      content_text TEXT NOT NULL DEFAULT '',
      parts_json TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_titles (
      session_id TEXT PRIMARY KEY REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
      platform TEXT NOT NULL,
      user_id TEXT NOT NULL,
      title TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
      updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shared_chats (
      share_id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL UNIQUE REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
      is_public INTEGER NOT NULL DEFAULT 0,
      preview_question TEXT,
      created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
      updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_sessions_user ON agent_sessions (platform, user_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages (session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_shared_chats_session ON shared_chats (session_id)",
)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_chat_schema() -> None:
    with connect() as conn:
        for ddl in CHAT_TABLE_DDL:
            conn.execute(ddl)
        conn.commit()


def get_session(session_id: str) -> sqlite3.Row | None:
    ensure_chat_schema()
    with connect() as conn:
        return conn.execute(
            """
            SELECT s.*, t.title
            FROM agent_sessions s
            LEFT JOIN chat_titles t ON t.session_id = s.session_id
            WHERE s.session_id = ?
            """,
            (session_id,),
        ).fetchone()


def ensure_session(
    *,
    session_id: str,
    platform: str,
    user_id: str,
    metadata: dict[str, Any] | None = None,
) -> sqlite3.Row:
    """Insert the session row if missing, merge metadata if it already exists.

    Per-turn metadata (surface, route, mode, ambient ids) changes; we don't
    want to clobber accumulated session-level state with whatever the latest
    turn sent.
    """
    ensure_chat_schema()
    incoming = metadata or {}
    now = utc_now()
    with connect() as conn:
        existing = conn.execute(
            "SELECT metadata_json FROM agent_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO agent_sessions (session_id, platform, user_id, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    platform,
                    user_id,
                    json.dumps(incoming, default=str),
                    now,
                    now,
                ),
            )
        else:
            try:
                merged = json.loads(existing["metadata_json"] or "{}")
            except (json.JSONDecodeError, ValueError):
                merged = {}
            merged.update(incoming)
            conn.execute(
                "UPDATE agent_sessions SET metadata_json = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(merged, default=str), now, session_id),
            )
        conn.commit()
    return get_session(session_id)  # type: ignore[return-value]


def touch_session(session_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE agent_sessions SET updated_at = ? WHERE session_id = ?",
            (utc_now(), session_id),
        )
        conn.commit()


def append_message(
    *,
    session_id: str,
    role: str,
    content_text: str,
    parts: list[dict[str, Any]] | None = None,
    message_id: str | None = None,
) -> str:
    ensure_chat_schema()
    mid = message_id or f"msg_{uuid.uuid4().hex}"
    now = utc_now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_messages (id, session_id, role, content_text, parts_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                mid,
                session_id,
                role,
                content_text or "",
                json.dumps(parts or [], default=str),
                now,
            ),
        )
        conn.execute(
            "UPDATE agent_sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        if role == "user":
            _upsert_title(conn, session_id, content_text)
        conn.commit()
    return mid


def _upsert_title(conn: sqlite3.Connection, session_id: str, text: str) -> None:
    row = conn.execute(
        "SELECT platform, user_id FROM agent_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return
    title = " ".join((text or "").split())[:80] or "New chat"
    now = utc_now()
    conn.execute(
        """
        INSERT INTO chat_titles (session_id, platform, user_id, title, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO NOTHING
        """,
        (session_id, row["platform"], row["user_id"], title, now, now),
    )


def list_sessions(
    *,
    platform: str,
    user_id: str,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    ensure_chat_schema()
    with connect() as conn:
        return conn.execute(
            """
            SELECT s.session_id, COALESCE(t.title, 'New chat') AS title, s.updated_at
            FROM agent_sessions s
            LEFT JOIN chat_titles t ON t.session_id = s.session_id
            WHERE s.platform = ? AND s.user_id = ?
            ORDER BY s.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (platform, user_id, limit, offset),
        ).fetchall()


def list_messages(session_id: str) -> list[sqlite3.Row]:
    ensure_chat_schema()
    with connect() as conn:
        return conn.execute(
            """
            SELECT id, session_id, role, content_text, parts_json, created_at
            FROM agent_messages
            WHERE session_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (session_id,),
        ).fetchall()


def delete_session(session_id: str) -> bool:
    ensure_chat_schema()
    with connect() as conn:
        cur = conn.execute("DELETE FROM agent_sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        return cur.rowcount > 0

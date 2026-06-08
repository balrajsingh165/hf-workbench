from __future__ import annotations

import sqlite3
import uuid

from src.chat.session_store import connect, ensure_chat_schema, utc_now


def get_share_by_session_id(session_id: str) -> sqlite3.Row | None:
    ensure_chat_schema()
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM shared_chats WHERE session_id = ?",
            (session_id,),
        ).fetchone()


def get_share_by_share_id(share_id: str) -> sqlite3.Row | None:
    ensure_chat_schema()
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM shared_chats WHERE share_id = ?",
            (share_id,),
        ).fetchone()


def set_share_public(session_id: str, public: bool) -> sqlite3.Row | None:
    ensure_chat_schema()
    now = utc_now()
    with connect() as conn:
        if public:
            existing = conn.execute(
                "SELECT share_id FROM shared_chats WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            share_id = existing["share_id"] if existing else f"share_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO shared_chats (share_id, session_id, is_public, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  is_public = 1,
                  updated_at = excluded.updated_at
                """,
                (share_id, session_id, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE shared_chats
                SET is_public = 0, updated_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )
        conn.commit()
    return get_share_by_session_id(session_id)


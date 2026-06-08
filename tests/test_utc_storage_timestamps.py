from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from db.schema import init_db
from src.chat.session_store import utc_now


UTC_SECOND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_schema_timestamp_defaults_are_utc_marked(tmp_path):
    db_path = tmp_path / "hf.db"
    init_db(str(db_path))

    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO users (id, display_name) VALUES ('u1', 'User')")
        created_at = conn.execute("SELECT created_at FROM users WHERE id = 'u1'").fetchone()[0]

    assert UTC_SECOND_RE.match(created_at)
    assert _parse_utc(created_at).tzinfo == timezone.utc


def test_chat_utc_now_is_utc_marked():
    stamped = utc_now()

    assert UTC_SECOND_RE.match(stamped)
    assert _parse_utc(stamped).tzinfo == timezone.utc

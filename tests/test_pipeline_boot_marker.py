from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.pipeline_scheduler import should_run_boot_pipeline, social_boot_due


def test_boot_pipeline_runs_once_per_session(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / ".pipeline_boot_marker"
    monkeypatch.setattr(
        "agents.pipeline_scheduler.supervisor_session_id",
        lambda: 4242,
    )

    assert should_run_boot_pipeline(marker_path=marker) is True
    assert should_run_boot_pipeline(marker_path=marker) is False


def test_boot_pipeline_runs_again_for_new_session(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / ".pipeline_boot_marker"
    session_ids = iter([111, 222])
    monkeypatch.setattr(
        "agents.pipeline_scheduler.supervisor_session_id",
        lambda: next(session_ids),
    )

    assert should_run_boot_pipeline(marker_path=marker) is True
    assert should_run_boot_pipeline(marker_path=marker) is True


NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _social_db(tmp_path: Path, last_run: datetime | None) -> Path:
    db_path = tmp_path / "hf.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE llm_calls (caller TEXT, created_at TEXT)")
    if last_run is not None:
        conn.execute(
            "INSERT INTO llm_calls VALUES ('social_topics', ?)",
            (last_run.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
    conn.commit()
    conn.close()
    return db_path


def test_social_boot_skipped_after_recent_run(tmp_path: Path) -> None:
    db = _social_db(tmp_path, NOW - timedelta(hours=1))
    assert social_boot_due(interval_hours=24, db_path=db, now=NOW) is False


def test_social_boot_due_after_half_interval(tmp_path: Path) -> None:
    db = _social_db(tmp_path, NOW - timedelta(hours=13))
    assert social_boot_due(interval_hours=24, db_path=db, now=NOW) is True


def test_social_boot_fails_open(tmp_path: Path) -> None:
    # Never ran → due; missing table / fresh DB → due.
    assert social_boot_due(
        interval_hours=24, db_path=_social_db(tmp_path, None), now=NOW
    ) is True
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert social_boot_due(interval_hours=24, db_path=empty, now=NOW) is True

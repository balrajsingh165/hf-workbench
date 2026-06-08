"""Persist one `code_interpreter_runs` row per AgentCore Code Interpreter phase.

Called from `chart.py`'s recording funnel after each chart turn. Run stats only
(outcome, failure stage, skip reason, sandbox action counts, latency); the
token/cost for the same run lives in `agent_usage` (phase='chart'), joined by
`request_id`. Failures are logged and swallowed — a telemetry write must never
break a user's chat response. See docs/agent-observability.md.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(
    os.getenv("HF_DB_PATH") or Path(__file__).resolve().parent.parent.parent / "db" / "hf.db"
)
_LOGGER = logging.getLogger("hf_workbench.agent.ci_run")


def record_ci_run(
    *,
    request_id: str,
    user_id: str,
    session_id: str | None,
    model_id: str,
    outcome: str,
    failure_stage: str | None,
    skip_reason: str | None,
    execute_count: int,
    write_count: int,
    image_bytes: int | None,
    elapsed_ms: int,
    purpose: str = "chart",
) -> None:
    """Insert one row into `code_interpreter_runs`. Never raises."""
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO code_interpreter_runs (
                    request_id, user_id, session_id, purpose, outcome,
                    failure_stage, skip_reason, execute_count, write_count,
                    image_bytes, elapsed_ms, model_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    session_id,
                    purpose,
                    outcome,
                    failure_stage,
                    skip_reason,
                    int(execute_count),
                    int(write_count),
                    int(image_bytes) if image_bytes is not None else None,
                    int(elapsed_ms),
                    model_id,
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — never break a chat turn over telemetry
        _LOGGER.error("code_interpreter_runs write failed: %s", exc, exc_info=True)


__all__ = ["record_ci_run"]

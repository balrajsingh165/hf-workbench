"""Persist per-phase agent token + cost records into `agent_usage`.

Called from `orchestrator.py` after each turn completes. Failures are
logged and swallowed — a billing-write hiccup must never break a user's
chat response.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agent.pricing import compute_cost_usd, lookup_price

_DB_PATH = Path(os.getenv("HF_DB_PATH") or Path(__file__).resolve().parent.parent.parent / "db" / "hf.db")
_LOGGER = logging.getLogger("hf_workbench.agent.usage")


@dataclass(frozen=True)
class PhaseUsage:
    phase: str
    model_id: str
    usage: dict[str, Any]
    status: str = "ok"


def _coerce(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _row_from_usage(
    *,
    request_id: str,
    user_id: str,
    session_id: str | None,
    endpoint: str,
    model_id: str,
    phase: str,
    usage: dict[str, Any],
    status: str,
    cost_override: float | None = None,
) -> tuple:
    input_tokens = _coerce(usage.get("inputTokens"))
    output_tokens = _coerce(usage.get("outputTokens"))
    cache_read = _coerce(usage.get("cacheReadInputTokens"))
    cache_write = _coerce(usage.get("cacheWriteInputTokens"))
    latency = usage.get("latency_ms")
    latency_ms = int(latency) if isinstance(latency, (int, float)) else None
    cost = cost_override if cost_override is not None else compute_cost_usd(model_id, usage)
    return (
        request_id,
        user_id,
        session_id,
        endpoint,
        model_id,
        phase,
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
        cost,
        latency_ms,
        status,
    )


def record_usage(
    *,
    request_id: str,
    user_id: str,
    endpoint: str,
    phases: list[PhaseUsage],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Insert one row per phase plus a denormalized 'aggregate' row.

    Each PhaseUsage carries its own model_id so phases running on different
    Bedrock models (e.g. Haiku research + Sonnet response) land in
    `agent_usage` with the correct model and a correctly-priced cost. The
    aggregate row uses the response phase's model when present (the
    dominant cost contributor in mixed-model turns), falling back to the
    first phase. Aggregate cost is the sum of per-phase costs, not a
    single-model recompute against the totals.

    Returns a summary dict with the totals and a per-phase breakdown so
    the orchestrator can pass the real `cost_usd` into the SSE result event.
    """
    if not phases:
        return {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "phases": {}}

    rows: list[tuple] = []
    totals_usage: dict[str, int] = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 0,
    }
    total_latency = 0
    any_latency = False
    aggregate_status = "ok"
    per_phase: dict[str, dict[str, Any]] = {}
    aggregate_cost = 0.0

    warned_models: set[str] = set()
    for ph in phases:
        if ph.model_id not in warned_models and lookup_price(ph.model_id) is None:
            _LOGGER.warning(
                "agent_usage: no pricing entry for model_id=%s — cost_usd will be 0",
                ph.model_id,
            )
            warned_models.add(ph.model_id)

        rows.append(
            _row_from_usage(
                request_id=request_id,
                user_id=user_id,
                session_id=session_id,
                endpoint=endpoint,
                model_id=ph.model_id,
                phase=ph.phase,
                usage=ph.usage,
                status=ph.status,
            )
        )
        for key in totals_usage:
            totals_usage[key] += _coerce(ph.usage.get(key))
        latency = ph.usage.get("latency_ms")
        if isinstance(latency, (int, float)):
            total_latency += int(latency)
            any_latency = True
        if ph.status != "ok":
            aggregate_status = ph.status
        phase_cost = compute_cost_usd(ph.model_id, ph.usage)
        aggregate_cost += phase_cost
        per_phase[ph.phase] = {
            "input_tokens": _coerce(ph.usage.get("inputTokens")),
            "output_tokens": _coerce(ph.usage.get("outputTokens")),
            "cache_read_tokens": _coerce(ph.usage.get("cacheReadInputTokens")),
            "cache_write_tokens": _coerce(ph.usage.get("cacheWriteInputTokens")),
            "cost_usd": phase_cost,
            "model_id": ph.model_id,
            "status": ph.status,
        }

    aggregate_usage = dict(totals_usage)
    if any_latency:
        aggregate_usage["latency_ms"] = total_latency
    aggregate_model_id = next(
        (ph.model_id for ph in phases if ph.phase == "response"),
        phases[0].model_id,
    )
    rows.append(
        _row_from_usage(
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            endpoint=endpoint,
            model_id=aggregate_model_id,
            phase="aggregate",
            usage=aggregate_usage,
            status=aggregate_status,
            cost_override=aggregate_cost,
        )
    )

    try:
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows_with_created_at = [row + (created_at,) for row in rows]
        with sqlite3.connect(_DB_PATH) as conn:
            conn.executemany(
                """
                INSERT INTO agent_usage (
                    request_id, user_id, session_id, endpoint, model_id, phase,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                    cost_usd, latency_ms, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_with_created_at,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — never break a chat turn over telemetry
        _LOGGER.error("agent_usage write failed: %s", exc, exc_info=True)
        sys.stderr.flush()

    return {
        "cost_usd": aggregate_cost,
        "input_tokens": totals_usage["inputTokens"],
        "output_tokens": totals_usage["outputTokens"],
        "cache_read_tokens": totals_usage["cacheReadInputTokens"],
        "cache_write_tokens": totals_usage["cacheWriteInputTokens"],
        "latency_ms": total_latency if any_latency else None,
        "phases": per_phase,
        "status": aggregate_status,
    }


__all__ = ["PhaseUsage", "record_usage"]

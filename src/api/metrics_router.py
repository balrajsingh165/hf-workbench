"""Internal dev metrics API (gated by HF_INTERNAL_METRICS_ENABLED)."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query

from src.ops.agent_session_metrics import collect_agent_sessions_from_db
from src.ops.health import collect_health
from src.ops.pipeline_events import read_recent_pipeline_events

router = APIRouter(prefix="/api/internal/metrics", tags=["internal-metrics"])


def metrics_api_enabled() -> bool:
    return os.getenv("HF_INTERNAL_METRICS_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _require_enabled() -> None:
    if not metrics_api_enabled():
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/health")
def get_metrics_health() -> dict:
    _require_enabled()
    return collect_health()


@router.get("/pipeline-events")
def get_pipeline_events(
    limit: int = Query(200, ge=1, le=2000),
    event: str | None = Query(None, description="Filter by event name"),
) -> dict:
    _require_enabled()
    events = read_recent_pipeline_events(limit=limit, event=event)
    return {"events": events, "count": len(events), "limit": limit, "event_filter": event}


@router.get("/agent-sessions")
def get_agent_sessions(
    limit: int = Query(40, ge=1, le=200),
    days: int = Query(7, ge=1, le=90),
) -> dict:
    _require_enabled()
    return collect_agent_sessions_from_db(limit=limit, days=days)

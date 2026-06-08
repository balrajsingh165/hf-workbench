"""Append-only pipeline metrics stream (`logs/hf-pipeline-metrics.jsonl`)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.pipeline_metrics.funnel import (
    RouteFunnelSnapshot,
    build_route_run_metric,
    cluster_member_counts,
    member_count_histogram,
    promote_rule_bucket,
    publisher_contribution_for_clusters,
    top_counts,
)

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs"
METRICS_PATH = LOG_DIR / "hf-pipeline-metrics.jsonl"
logger = logging.getLogger("hf.pipeline_metrics")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


MetricEvent = dict[str, Any]
MetricFactory = Callable[[], MetricEvent]


def append_metric(
    event: MetricEvent | MetricFactory,
    *,
    event_name: str | None = None,
) -> bool:
    """Best-effort append for debug metrics; never fail pipeline work."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = event() if callable(event) else dict(event)
        payload.setdefault("ts", utc_now())
        with METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        if event_name is None and isinstance(event, dict):
            event_name = event.get("event")
        logger.warning(
            "failed to append pipeline metric event=%s",
            event_name,
            exc_info=True,
        )
        return False
    return True


__all__ = [
    "METRICS_PATH",
    "RouteFunnelSnapshot",
    "append_metric",
    "build_route_run_metric",
    "cluster_member_counts",
    "member_count_histogram",
    "promote_rule_bucket",
    "publisher_contribution_for_clusters",
    "top_counts",
    "utc_now",
]

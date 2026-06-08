"""Read recent rows from hf-pipeline-metrics.jsonl."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.pipeline_metrics import METRICS_PATH


def read_recent_pipeline_events(
    *,
    path: Path | None = None,
    limit: int = 200,
    event: str | None = None,
) -> list[dict[str, Any]]:
    metrics_path = path or METRICS_PATH
    if not metrics_path.exists():
        return []
    limit = max(1, min(int(limit), 2000))
    matched: list[dict[str, Any]] = []
    with metrics_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if event and row.get("event") != event:
                continue
            matched.append(row)
    return matched[-limit:]

"""Health collector for HTTP and CLI (wraps scripts/hf_health.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _hf_health_module():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    path = ROOT / "scripts" / "hf_health.py"
    spec = importlib.util.spec_from_file_location("hf_health", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load hf_health from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def collect_health(
    *,
    db_path: Path | None = None,
    pipeline_metrics_path: Path | None = None,
) -> dict[str, Any]:
    mod = _hf_health_module()
    db = db_path or mod.DB_PATH
    metrics_path = pipeline_metrics_path or mod.PIPELINE_METRICS_PATH
    return mod.collect(db, metrics_path)

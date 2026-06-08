"""Shared JSON cache utilities for fetch-once-and-store data.

Two patterns are supported by callers; this module owns the file I/O and
freshness primitives, not the payload schema.

1. **TTL caches** (e.g. Polymarket markets + embeddings) — one file with a
   `fetched_at` timestamp. Refresh when older than a TTL or when invalidation
   metadata changes.

2. **Keyed snapshots** (e.g. daily mover quotes at `db/mesh_cache/{date}.json`)
   — written once per key (typically a date) and re-read forever. No TTL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    """UTC ISO-8601 timestamp suitable for `fetched_at` fields."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_hours(fetched_at: str | None) -> float:
    """Hours since `fetched_at` (ISO 8601). Returns +inf on missing/unparseable input.

    Naive timestamps are assumed UTC so callers don't have to defensively
    upgrade legacy cache files.
    """
    if not fetched_at:
        return float("inf")
    try:
        t = datetime.fromisoformat(fetched_at)
    except ValueError:
        return float("inf")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def load_json(path: Path) -> dict | None:
    """Return parsed JSON object, or None if the file is missing or unreadable."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_json(
    path: Path,
    payload: dict[str, Any],
    *,
    indent: int = 2,
    sort_keys: bool = False,
    trailing_newline: bool = False,
) -> None:
    """Pretty-print `payload` as JSON to `path`, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=indent, sort_keys=sort_keys)
    if trailing_newline:
        body += "\n"
    path.write_text(body, encoding="utf-8")


def is_fresh(
    path: Path,
    ttl_hours: float,
    *,
    fetched_at_field: str = "fetched_at",
) -> bool:
    """True when `path` exists, parses, and is younger than `ttl_hours`."""
    meta = load_json(path)
    if meta is None:
        return False
    return age_hours(meta.get(fetched_at_field)) <= ttl_hours


__all__ = ["age_hours", "is_fresh", "load_json", "now_iso", "save_json"]

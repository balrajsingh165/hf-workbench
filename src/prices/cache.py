"""Simple in-process TTL cache for price data.

Key space: arbitrary strings. Entries expire lazily on read. No LRU, no size
cap — cardinality is expected to stay in the low thousands for a single-process
FastAPI server.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_store: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def get(key: str) -> tuple[bool, Any]:
    """Return (hit, value). hit=False on miss or expiry."""
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return False, None
        expiry, value = entry
        if time.monotonic() > expiry:
            del _store[key]
            return False, None
        return True, value


def set(key: str, value: Any, ttl_s: float) -> None:
    with _lock:
        _store[key] = (time.monotonic() + ttl_s, value)


def delete(key: str) -> None:
    with _lock:
        _store.pop(key, None)


def clear() -> None:
    with _lock:
        _store.clear()

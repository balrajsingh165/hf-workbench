"""EODHD Financial Data API client.

Rotates across multiple API keys (free-tier accounts) in round-robin to stay
within per-key daily/minute request limits. All calls are synchronous HTTP via
httpx. Intraday from/to must be Unix seconds; EOD from/to are ISO dates.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any

import httpx

from src import config

BASE = "https://eodhd.com/api"
_DEFAULT_TIMEOUT = 15.0

_keys: list[str] = []
_key_iter: itertools.cycle | None = None
_key_lock = threading.Lock()


def _init_keys() -> None:
    global _keys, _key_iter
    raw = (config.EODHD_API_KEYS or "").strip()
    _keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not _keys:
        raise RuntimeError("EODHD_API_KEYS not set or empty")
    _key_iter = itertools.cycle(_keys)


def _next_key() -> str:
    with _key_lock:
        if _key_iter is None:
            _init_keys()
        return next(_key_iter)  # type: ignore[arg-type]


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    p = dict(params or {})
    p.setdefault("fmt", "json")
    p["api_token"] = _next_key()
    url = f"{BASE}/{path.lstrip('/')}"
    resp = httpx.get(url, params=p, timeout=_DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


class EodhdApiError(Exception):
    pass


def _call(path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        return _get(path, params)
    except httpx.HTTPStatusError as exc:
        raise EodhdApiError(f"EODHD {exc.response.status_code}: {path}") from exc
    except httpx.RequestError as exc:
        raise EodhdApiError(f"EODHD request failed: {exc}") from exc


def real_time_batch(symbols: list[str]) -> list[dict]:
    """Fetch real-time quotes for up to 15 EODHD symbols.

    Returns a list of raw EODHD quote dicts. On single-symbol requests EODHD
    returns an object; we normalize to a list in all cases.
    """
    if not symbols:
        return []
    primary, *rest = symbols
    params: dict[str, Any] = {}
    if rest:
        params["s"] = ",".join(rest)
    raw = _call(f"real-time/{primary}", params)
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return raw
    return []


def intraday(
    symbol: str,
    *,
    interval: str,
    from_ts: int,
    to_ts: int,
) -> list[dict]:
    return _call(
        f"intraday/{symbol}",
        {"interval": interval, "from": from_ts, "to": to_ts},
    )


def eod(
    symbol: str,
    *,
    period: str = "d",
    from_date: str,
    to_date: str,
) -> list[dict]:
    return _call(f"eod/{symbol}", {"period": period, "from": from_date, "to": to_date})


def commodities_historical(
    code: str,
    *,
    from_date: str,
    to_date: str,
) -> list[dict]:
    return _call(
        f"commodities/historical/{code}",
        {"interval": "daily", "from": from_date, "to": to_date},
    )


def ust_yield_rates(*, from_date: str, to_date: str) -> list[dict]:
    return _call("ust/yield-rates", {"from": from_date, "to": to_date})


def exchange_details(exchange_code: str) -> dict:
    raw = _call(f"exchange-details/{exchange_code}")
    if isinstance(raw, list) and raw:
        return raw[0]
    return raw if isinstance(raw, dict) else {}


__all__ = [
    "EodhdApiError",
    "commodities_historical",
    "eod",
    "exchange_details",
    "intraday",
    "real_time_batch",
    "ust_yield_rates",
]

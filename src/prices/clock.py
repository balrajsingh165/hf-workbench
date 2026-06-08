"""Market clock — wraps EODHD /exchange-details/US, cached 60 seconds."""

from __future__ import annotations

from dataclasses import dataclass

from src.clients import eodhd
from src.prices import cache

_CACHE_KEY = "clock:US"
_TTL = 60.0


@dataclass(frozen=True)
class Clock:
    open: bool
    next_open: str | None
    next_close: str | None


def get() -> Clock:
    hit, val = cache.get(_CACHE_KEY)
    if hit:
        return val
    result = _fetch()
    cache.set(_CACHE_KEY, result, _TTL)
    return result


def _fetch() -> Clock:
    try:
        data = eodhd.exchange_details("US")
    except eodhd.EodhdApiError:
        return Clock(open=False, next_open=None, next_close=None)

    is_open = bool(data.get("isOpen"))
    trading = data.get("TradingHours") or {}
    return Clock(
        open=is_open,
        next_open=trading.get("OpenUTC") or None,
        next_close=trading.get("CloseUTC") or None,
    )

"""Sync httpx client for Alpaca Market Data v2 (stocks / ETFs only).

Two narrow surfaces:
- `snapshots(symbols)`     -> latest trade + previous daily close per symbol.
- `bars(symbols, period)`  -> daily bars for a window; window % return derived
                              from first-bar open and last-bar close.

Crypto is intentionally NOT routed through here — HF has better crypto
sources elsewhere. Indices, futures, FX, and foreign listings are not
covered by Alpaca and stay on Mesh.

Failures are localized: per-symbol misses resolve to None inside the
returned dicts; an outright HTTP error raises `AlpacaApiError`. The
caller (the price router) is responsible for falling back to Mesh on
shape gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.config import (
    ALPACA_API_KEY,
    ALPACA_API_SECRET,
    ALPACA_DATA_BASE,
    ALPACA_STOCK_FEED,
    require_env,
)

DEFAULT_TIMEOUT_SECONDS = 30.0
# Alpaca caps `symbols` at 100 per request for snapshots; 50 is conservative
# and keeps URL length sane.
_BATCH = 50


class AlpacaApiError(RuntimeError):
    """Raised when the Alpaca data API returns a non-2xx response."""


@dataclass(frozen=True)
class AlpacaConfig:
    api_key: str
    api_secret: str
    base_url: str
    feed: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "AlpacaConfig":
        return cls(
            api_key=require_env("ALPACA_API_KEY", ALPACA_API_KEY),
            api_secret=require_env("ALPACA_API_SECRET", ALPACA_API_SECRET),
            base_url=ALPACA_DATA_BASE.rstrip("/"),
            feed=ALPACA_STOCK_FEED,
        )


def _headers(cfg: AlpacaConfig) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": cfg.api_key,
        "APCA-API-SECRET-KEY": cfg.api_secret,
        "Accept": "application/json",
    }


def _chunks(symbols: list[str], size: int = _BATCH) -> list[list[str]]:
    out: list[list[str]] = []
    for i in range(0, len(symbols), size):
        out.append(symbols[i : i + size])
    return out


def _get(cfg: AlpacaConfig, path: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{cfg.base_url}{path}"
    with httpx.Client(timeout=cfg.timeout) as client:
        response = client.get(url, headers=_headers(cfg), params=params)
    if response.is_error:
        raise AlpacaApiError(
            f"Alpaca {path} failed with status {response.status_code}: {response.text}"
        )
    return response.json() if response.content else {}


def snapshots(
    symbols: list[str],
    *,
    cfg: AlpacaConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Return raw `{symbol: snapshot_dict}` from Alpaca.

    Snapshot includes `latestTrade`, `latestQuote`, `dailyBar`,
    `prevDailyBar`. Per-symbol failures are simply absent from the dict.
    """
    cfg = cfg or AlpacaConfig.from_env()
    unique = list(dict.fromkeys(s for s in symbols if s))
    out: dict[str, dict[str, Any]] = {}
    if not unique:
        return out
    for chunk in _chunks(unique):
        params = {"symbols": ",".join(chunk), "feed": cfg.feed}
        body = _get(cfg, "/v2/stocks/snapshots", params)
        snaps = body.get("snapshots") if isinstance(body, dict) else None
        if isinstance(snaps, dict):
            out.update(snaps)
        elif isinstance(body, dict):
            # Older shape: top-level dict-of-symbol → snapshot.
            for sym, snap in body.items():
                if isinstance(snap, dict):
                    out[sym] = snap
    return out


def quote_snapshot(
    symbols: list[str],
    *,
    cfg: AlpacaConfig | None = None,
) -> dict[str, tuple[float | None, float | None]]:
    """Return `{symbol: (last_price, pct_change_vs_prev_close)}`.

    `dailyBar.c` is the latest session close; `prevDailyBar.c` is the prior
    session close. We prefer `latestTrade.p` for last price when present
    (gives an intraday number on a live session) and fall back to
    `dailyBar.c` otherwise.
    """
    raw = snapshots(symbols, cfg=cfg)
    out: dict[str, tuple[float | None, float | None]] = {}
    for sym, snap in raw.items():
        last = _f(snap.get("latestTrade", {}).get("p"))
        if last is None:
            last = _f(snap.get("dailyBar", {}).get("c"))
        prev = _f(snap.get("prevDailyBar", {}).get("c"))
        pct: float | None = None
        if last is not None and prev is not None and prev != 0:
            pct = (last - prev) / prev * 100.0
        out[sym] = (last, pct)
    return out


def window_returns(
    symbols: list[str],
    *,
    days: int = 30,
    cfg: AlpacaConfig | None = None,
) -> dict[str, float | None]:
    """Return `{symbol: pct_change_over_window}` using daily bars.

    Window math mirrors Mesh's `open_close_change_pct`: first-bar open vs.
    last-bar close. Days param is calendar days back; Alpaca returns only
    trading days within that span. ~30d window ≈ Mesh's `period='1mo'`.
    """
    cfg = cfg or AlpacaConfig.from_env()
    unique = list(dict.fromkeys(s for s in symbols if s))
    out: dict[str, float | None] = {s: None for s in unique}
    if not unique:
        return out

    end = datetime.now(timezone.utc) - timedelta(minutes=15)
    start = end - timedelta(days=days)
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    for chunk in _chunks(unique):
        params = {
            "symbols": ",".join(chunk),
            "timeframe": "1Day",
            "start": start_iso,
            "end": end_iso,
            "feed": cfg.feed,
            "adjustment": "raw",
            "limit": 1000,
        }
        body = _get(cfg, "/v2/stocks/bars", params)
        bars = body.get("bars") if isinstance(body, dict) else None
        if not isinstance(bars, dict):
            continue
        for sym, rows in bars.items():
            if not isinstance(rows, list) or not rows:
                continue
            first = rows[0] if isinstance(rows[0], dict) else {}
            last = rows[-1] if isinstance(rows[-1], dict) else {}
            o = _f(first.get("o"))
            c = _f(last.get("c"))
            if o is not None and c is not None and o != 0:
                out[sym] = (c - o) / o * 100.0
    return out


def _f(x: Any) -> float | None:
    if isinstance(x, (int, float)):
        return float(x)
    return None


__all__ = [
    "AlpacaApiError",
    "AlpacaConfig",
    "quote_snapshot",
    "snapshots",
    "window_returns",
]

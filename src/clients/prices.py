"""Scheduled-job price router used by movers + Tailwind scoring.

Frontend price-display endpoints intentionally do not use this module; they
call EODHD directly from `src.interfaces.prices.api`. This router preserves the
older scheduled-job behavior: US equities and ETFs route to Alpaca, while
crypto, indices, FX, rates, commodity futures, and foreign listings fall back
to Mesh/YahooFinanceAgent.

Routing predicate: the instruments registry's `asset_class`. We deliberately
do not parse Yahoo symbol shapes (`=F`, `^X`, `=X`, `.XX`) except for the
foreign-listing guard below — the registry remains the source of truth.

Public surface:
    Quote                       — normalized snapshot row.
    WindowReturn                — normalized window-return row.
    quote_snapshot(symbols)     — daily snapshot per symbol.
    window_returns(symbols)     — multi-day window pct return per symbol.
    canonicalize(raw)           — registry alias → canonical Yahoo symbol.
    window_return_pcts(symbols) — shortcut: {symbol: pct | None}.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.clients import alpaca, mesh
from src.instruments import resolver

Source = Literal["alpaca", "mesh"]

# Asset classes routed to Alpaca. Everything else stays on Mesh.
_ALPACA_CLASSES = frozenset({"equity", "etf"})


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: float | None
    pct_change: float | None
    source: Source


@dataclass(frozen=True, slots=True)
class WindowReturn:
    symbol: str
    pct: float | None
    source: Source


def _route(symbol: str) -> Source:
    inst = resolver.get(symbol)
    if inst is None or inst.asset_class not in _ALPACA_CLASSES:
        return "mesh"
    # Foreign listings keep `asset_class='equity'` but use Yahoo's exchange-
    # suffix form (`005930.KS`, `RHM.DE`, `BA.L`, `9684.T`). Alpaca is US
    # markets only.
    if "." in symbol:
        return "mesh"
    return "alpaca"


def _split(symbols: list[str]) -> tuple[list[str], list[str]]:
    """Partition input into (alpaca_eligible, mesh_only). Order preserved."""
    a: list[str] = []
    m: list[str] = []
    for s in symbols:
        (a if _route(s) == "alpaca" else m).append(s)
    return a, m


def canonicalize(raw: str) -> str:
    raw = (raw or "").strip().upper()
    if not raw:
        return raw
    return resolver.canonical(raw)


def quote_snapshot(symbols: list[str]) -> dict[str, Quote]:
    unique = list(dict.fromkeys(s for s in symbols if s))
    if not unique:
        return {}

    alpaca_syms, mesh_syms = _split(unique)
    out: dict[str, Quote] = {}

    if alpaca_syms:
        try:
            a_rows = alpaca.quote_snapshot(alpaca_syms)
        except alpaca.AlpacaApiError:
            a_rows = {}
        for sym in alpaca_syms:
            price, pct = a_rows.get(sym, (None, None))
            out[sym] = Quote(symbol=sym, price=price, pct_change=pct, source="alpaca")

    if mesh_syms:
        m_rows = _mesh_quote_snapshot(mesh_syms)
        for sym in mesh_syms:
            price, pct = m_rows.get(sym.upper(), (None, None))
            out[sym] = Quote(symbol=sym, price=price, pct_change=pct, source="mesh")

    return out


def window_returns(
    symbols: list[str],
    *,
    period: str = "1mo",
) -> dict[str, WindowReturn]:
    unique = list(dict.fromkeys(s for s in symbols if s))
    if not unique:
        return {}

    alpaca_syms, mesh_syms = _split(unique)
    out: dict[str, WindowReturn] = {}

    if alpaca_syms:
        days = _period_to_days(period)
        try:
            a_rows = alpaca.window_returns(alpaca_syms, days=days)
        except alpaca.AlpacaApiError:
            a_rows = {s: None for s in alpaca_syms}
        for sym in alpaca_syms:
            out[sym] = WindowReturn(symbol=sym, pct=a_rows.get(sym), source="alpaca")

    if mesh_syms:
        m_rows = _mesh_window_returns(mesh_syms, period=period)
        for sym in mesh_syms:
            out[sym] = WindowReturn(symbol=sym, pct=m_rows.get(sym.upper()), source="mesh")

    return out


def window_return_pcts(
    symbols: list[str],
    *,
    period: str = "1mo",
) -> dict[str, float | None]:
    return {s: r.pct for s, r in window_returns(symbols, period=period).items()}


# ── Mesh-side adapters ───────────────────────────────────────────────


def _mesh_quote_snapshot(
    symbols: list[str],
) -> dict[str, tuple[float | None, float | None]]:
    out: dict[str, tuple[float | None, float | None]] = {}
    if not symbols:
        return out
    try:
        payload = mesh.yahoo_quote_snapshot(symbols)
    except mesh.MeshApiError:
        return out
    body = mesh.unwrap_results(payload)
    for entry in body.get("results") or []:
        if not isinstance(entry, dict):
            continue
        sym = entry.get("symbol")
        if not isinstance(sym, str):
            continue
        out[sym.upper()] = _extract_mesh_quote(entry)
    return out


def _extract_mesh_quote(entry: dict) -> tuple[float | None, float | None]:
    if entry.get("status") != "success":
        return None, None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None, None
    price_block = data.get("price") if isinstance(data.get("price"), dict) else data
    last = price_block.get("last_price")
    if not isinstance(last, (int, float)):
        last = price_block.get("price")
    prev = price_block.get("previous_close")
    price = float(last) if isinstance(last, (int, float)) else None
    pct: float | None = None
    if isinstance(last, (int, float)) and isinstance(prev, (int, float)) and prev:
        pct = ((float(last) - float(prev)) / float(prev)) * 100.0
    return price, pct


def _mesh_window_returns(
    symbols: list[str],
    *,
    period: str,
) -> dict[str, float | None]:
    out: dict[str, float | None] = {s.upper(): None for s in symbols}
    if not symbols:
        return out
    # Mesh's `price_history` caps batches at 10.
    for i in range(0, len(symbols), 10):
        chunk = symbols[i : i + 10]
        try:
            payload = mesh.yahoo_price_history(
                chunk, interval="1d", period=period, limit_bars=50
            )
        except mesh.MeshApiError:
            continue
        body = mesh.unwrap_results(payload)
        for entry in body.get("results") or []:
            if not isinstance(entry, dict):
                continue
            sym = entry.get("symbol")
            if not isinstance(sym, str):
                continue
            out[sym.upper()] = _extract_mesh_window_pct(entry)
    return out


def _extract_mesh_window_pct(entry: dict) -> float | None:
    if entry.get("status") != "success":
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    window = data.get("window_summary")
    if not isinstance(window, dict):
        return None
    pct = window.get("open_close_change_pct")
    return float(pct) if isinstance(pct, (int, float)) else None


def _period_to_days(period: str) -> int:
    """Translate a Yahoo-style period into calendar days for Alpaca bars."""
    table = {
        "5d": 7, "1mo": 31, "3mo": 95, "6mo": 190,
        "1y": 370, "2y": 740, "5y": 1830, "max": 3650,
    }
    return table.get(period.strip().lower(), 31)


__all__ = [
    "Quote",
    "WindowReturn",
    "canonicalize",
    "quote_snapshot",
    "window_return_pcts",
    "window_returns",
]

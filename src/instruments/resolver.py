"""Instrument registry resolver.

The only place vendor translation happens. Reads the `instruments` table once
into an in-process dict cache; all lookups go through the cache. Re-import or
call `reload()` after seed changes.

Public surface:
    get(symbol)               -> Instrument | None
    to_display(symbol, form)  -> str   (falls back to the symbol on miss)
    resolve(symbol, vendor)   -> str | None
    all_active(asset_class=, tradable=) -> list[Instrument]
    exists(symbol)            -> bool
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DB_PATH = Path(__file__).resolve().parents[2] / "db" / "hf.db"

Vendor = Literal["yahoo", "alpaca", "fmp"]

_YAHOO_TO_EODHD_EXCHANGE: dict[str, str] = {
    ".KS": ".KO",
    ".T":  ".TSE",
    ".L":  ".LSE",
    ".DE": ".XETRA",
    ".SS": ".SHG",
    ".ME": ".MCX",
    ".HK": ".HK",
    ".PA": ".PA",
    ".AS": ".AS",
    ".MI": ".MI",
}

_COMMODITY_FUTURES: dict[str, str] = {
    "GC=F": "GOLD",
    "CL=F": "WTI",
    "BZ=F": "BRENT",
    "NG=F": "NATURAL_GAS",
}


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    display: str
    short: str
    asset_class: str
    aliases: tuple[str, ...]
    canonical_symbol: str | None
    eodhd_symbol: str | None
    alpaca_symbol: str | None
    fmp_symbol: str | None
    tradable: bool
    proxy_for: str | None
    active: bool


_CACHE: dict[str, Instrument] | None = None
_CACHE_LOCK = threading.Lock()
_CACHE_DB_PATH: Path | None = None


def _row_to_instrument(row: sqlite3.Row) -> Instrument:
    aliases_raw = row["aliases_json"] or "[]"
    try:
        aliases = tuple(json.loads(aliases_raw))
    except json.JSONDecodeError:
        aliases = ()
    return Instrument(
        symbol=row["symbol"],
        display=row["display"],
        short=row["short"],
        asset_class=row["asset_class"],
        aliases=aliases,
        canonical_symbol=row["canonical_symbol"],
        eodhd_symbol=row["eodhd_symbol"] if "eodhd_symbol" in row.keys() else None,
        alpaca_symbol=row["alpaca_symbol"],
        fmp_symbol=row["fmp_symbol"],
        tradable=bool(row["tradable"]),
        proxy_for=row["proxy_for"],
        active=bool(row["active"]),
    )


def _load(db_path: Path) -> dict[str, Instrument]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM instruments WHERE active = 1"
        ).fetchall()
    finally:
        conn.close()
    return {row["symbol"]: _row_to_instrument(row) for row in rows}


def _ensure_cache(db_path: Path = DB_PATH) -> dict[str, Instrument]:
    global _CACHE, _CACHE_DB_PATH
    if _CACHE is None or _CACHE_DB_PATH != db_path:
        with _CACHE_LOCK:
            if _CACHE is None or _CACHE_DB_PATH != db_path:
                _CACHE = _load(db_path)
                _CACHE_DB_PATH = db_path
    return _CACHE


def reload(db_path: Path = DB_PATH) -> int:
    """Drop the in-process cache and reload from disk. Returns row count."""
    global _CACHE, _CACHE_DB_PATH
    with _CACHE_LOCK:
        _CACHE = _load(db_path)
        _CACHE_DB_PATH = db_path
    return len(_CACHE)


def get(symbol: str, *, db_path: Path = DB_PATH) -> Instrument | None:
    return _ensure_cache(db_path).get(symbol)


def exists(symbol: str, *, db_path: Path = DB_PATH) -> bool:
    return symbol in _ensure_cache(db_path)


def canonical(symbol: str, *, db_path: Path = DB_PATH) -> str:
    """Return the canonical Yahoo symbol for an alias row.

    Alias rows (`USDJPY`, `DXY`, `BTC`) carry `canonical_symbol` pointing at
    the real Yahoo row (`JPY=X`, `DX-Y.NYB`, `BTC-USD`). For canonical rows
    and unknown symbols, returns the input unchanged so callers can chain
    safely.
    """
    inst = get(symbol, db_path=db_path)
    if inst is None:
        return symbol
    return inst.canonical_symbol or inst.symbol


def to_display(
    symbol: str,
    form: Literal["short", "full"] = "short",
    *,
    db_path: Path = DB_PATH,
) -> str:
    """Return the human-readable name. Falls back to the raw symbol on miss
    so callers can render text safely even if a row is missing."""
    inst = get(symbol, db_path=db_path)
    if inst is None:
        return symbol
    return inst.short if form == "short" else inst.display


def resolve(
    symbol: str,
    vendor: Vendor,
    *,
    db_path: Path = DB_PATH,
) -> str | None:
    """Translate a Yahoo symbol into the given vendor's symbol.

    Returns the symbol itself when the vendor column is NULL (vendor uses the
    same string as Yahoo). Returns None when the symbol is not in the
    registry — callers must decide what to do.
    """
    inst = get(symbol, db_path=db_path)
    if inst is None:
        return None
    if vendor == "yahoo":
        return inst.symbol
    if vendor == "alpaca":
        return inst.alpaca_symbol or inst.symbol
    if vendor == "fmp":
        return inst.fmp_symbol or inst.symbol
    raise ValueError(f"unknown vendor: {vendor}")


def to_eodhd(symbol: str, *, db_path: Path = DB_PATH) -> str:
    """Translate a canonical Yahoo symbol to its EODHD form.

    Checks the DB's eodhd_symbol column first (explicit override), then applies
    the standard suffix-mapping rules documented in docs/ref/eodhd-api.md.
    """
    inst = get(symbol, db_path=db_path)
    if inst is not None and inst.eodhd_symbol:
        return inst.eodhd_symbol

    if symbol in _COMMODITY_FUTURES:
        return _COMMODITY_FUTURES[symbol]

    if symbol.startswith("^"):
        return f"{symbol[1:]}.INDX"

    if symbol.endswith("=X"):
        return f"{symbol[:-2]}.FOREX"

    if "-" in symbol and not symbol.endswith("=F"):
        parts = symbol.split("-")
        if len(parts) == 2 and parts[1] in ("USD", "BTC", "ETH", "USDT", "BNB"):
            return f"{symbol}.CC"

    for yahoo_sfx, eodhd_sfx in _YAHOO_TO_EODHD_EXCHANGE.items():
        if symbol.endswith(yahoo_sfx):
            base = symbol[: -len(yahoo_sfx)]
            return f"{base}{eodhd_sfx}"

    return f"{symbol}.US"


def is_commodity_future(symbol: str) -> bool:
    return symbol in _COMMODITY_FUTURES


def is_ust_yield(symbol: str) -> bool:
    return symbol == "^TNX"


def all_active(
    asset_class: str | None = None,
    tradable: bool | None = None,
    *,
    db_path: Path = DB_PATH,
) -> list[Instrument]:
    cache = _ensure_cache(db_path)
    out: list[Instrument] = []
    for inst in cache.values():
        if asset_class is not None and inst.asset_class != asset_class:
            continue
        if tradable is not None and inst.tradable is not tradable:
            continue
        out.append(inst)
    out.sort(key=lambda i: i.symbol)
    return out


__all__ = [
    "Instrument",
    "all_active",
    "canonical",
    "exists",
    "get",
    "is_commodity_future",
    "is_ust_yield",
    "reload",
    "resolve",
    "to_display",
    "to_eodhd",
]

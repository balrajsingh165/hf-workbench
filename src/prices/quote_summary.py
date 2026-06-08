"""Quote-card fields for ``price_summary``.

Session OHLCV prefers EODHD real-time for registry-mapped symbols when keys are
configured; falls back to Mesh ``quote_snapshot``. 52-week range and market cap
come from Mesh stats. TTM EPS / P/E / forward P/E use a direct Yahoo
``quoteSummary`` fetch (prototype) until EODHD Fundamentals Highlights is wired.
"""

from __future__ import annotations

import math
from typing import Any

from src import config
from src.instruments import resolver

_EODHD_RTH_CLASSES = frozenset({"equity", "etf", "equity_index"})


def _sf(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _si(value: Any) -> int | None:
    v = _sf(value)
    if v is None:
        return None
    return int(v)


def _raw_metric(block: Any) -> float | None:
    if isinstance(block, dict):
        return _sf(block.get("raw"))
    return _sf(block)


def _eodhd_keys_configured() -> bool:
    return bool((config.EODHD_API_KEYS or "").strip())


def fetch_eodhd_session(ticker: str) -> dict[str, Any] | None:
    """Session quote from EODHD real-time, or None when unavailable."""
    if not _eodhd_keys_configured():
        return None
    inst = resolver.get(ticker)
    if inst is not None and inst.asset_class not in _EODHD_RTH_CLASSES:
        return None
    try:
        from src.clients import eodhd
        from src.clients.eodhd import EodhdApiError

        code = resolver.to_eodhd(ticker)
        rows = eodhd.real_time_batch([code])
    except (EodhdApiError, RuntimeError):
        return None
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        return None
    last = _sf(row.get("close") or row.get("last"))
    prev = _sf(row.get("previousClose"))
    change_pct: float | None = _sf(row.get("change_p"))
    if change_pct is None and last is not None and prev and prev != 0:
        change_pct = round((last - prev) / abs(prev) * 100.0, 4)
    return {
        "last_price": last,
        "open": _sf(row.get("open")),
        "previous_close": prev,
        "day_high": _sf(row.get("high")),
        "day_low": _sf(row.get("low")),
        "volume": _si(row.get("volume")),
        "change_pct_1d": change_pct,
        "source": "eodhd",
    }


def fetch_mesh_quote(ticker: str) -> dict[str, Any] | None:
    """Full Mesh ``quote_snapshot`` data block for one symbol."""
    from src.clients.mesh import MeshApiError, unwrap_results, yahoo_quote_snapshot

    sym = ticker.strip().upper()
    if not sym:
        return None
    try:
        raw = yahoo_quote_snapshot([sym])
    except MeshApiError:
        return None
    if isinstance(raw, dict) and raw.get("note"):
        return None
    body = unwrap_results(raw) or {}
    for entry in body.get("results") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("symbol", "")).upper() != sym:
            continue
        if entry.get("status") != "success":
            return None
        data = entry.get("data")
        return data if isinstance(data, dict) else None
    return None


def mesh_session_from_quote(data: dict[str, Any]) -> dict[str, Any]:
    price = data.get("price") if isinstance(data.get("price"), dict) else {}
    last = _sf(price.get("last_price") or price.get("price"))
    prev = _sf(price.get("previous_close"))
    change_pct: float | None = None
    if last is not None and prev and prev != 0:
        change_pct = round((last - prev) / abs(prev) * 100.0, 4)
    return {
        "last_price": last,
        "open": _sf(price.get("open")),
        "previous_close": prev,
        "day_high": _sf(price.get("day_high")),
        "day_low": _sf(price.get("day_low")),
        "volume": _si(price.get("volume")),
        "change_pct_1d": change_pct,
        "source": "mesh",
    }


def mesh_range_from_quote(data: dict[str, Any]) -> dict[str, Any]:
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    return {
        "year_high": _sf(stats.get("year_high")),
        "year_low": _sf(stats.get("year_low")),
        "market_cap": _sf(stats.get("market_cap")),
    }


def fetch_yahoo_valuation(ticker: str) -> dict[str, Any]:
    """TTM EPS and P/E via Yahoo quoteSummary when the endpoint allows it."""
    sym = ticker.strip().upper()
    if not sym:
        return {}
    try:
        from curl_cffi import requests
    except ImportError:
        return {}
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
    try:
        resp = requests.get(
            url,
            params={"modules": "summaryDetail,defaultKeyStatistics"},
            impersonate="chrome",
            timeout=12,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return {}
    results = (payload.get("quoteSummary") or {}).get("result") or []
    if not results or not isinstance(results[0], dict):
        return {}
    block = results[0]
    summary = block.get("summaryDetail") if isinstance(block.get("summaryDetail"), dict) else {}
    stats = (
        block.get("defaultKeyStatistics")
        if isinstance(block.get("defaultKeyStatistics"), dict)
        else {}
    )
    eps_ttm = _raw_metric(stats.get("trailingEps"))
    pe_ttm = _raw_metric(summary.get("trailingPE"))
    forward_pe = _raw_metric(summary.get("forwardPE"))
    if not eps_ttm and not pe_ttm and forward_pe is None:
        return {}
    return {
        "eps_ttm": eps_ttm,
        "pe_ttm": pe_ttm,
        "forward_pe": forward_pe,
        "valuation_note": None,
    }


def fetch_fundamentals_valuation(ticker: str, last_price: float | None) -> dict[str, Any]:
    """Fallback when Yahoo quoteSummary is blocked: FY diluted EPS via Mesh fundamentals."""
    from app import get_fundamentals

    sym = ticker.strip().upper()
    row = get_fundamentals(sym)
    if row.note and "equity-only" in row.note:
        return {}
    eps: float | None = None
    basis = "fy_diluted_eps"
    if row.income_latest_annual and row.income_latest_annual.diluted_eps is not None:
        eps = row.income_latest_annual.diluted_eps
    elif row.income_latest_quarterly and row.income_latest_quarterly.diluted_eps is not None:
        eps = row.income_latest_quarterly.diluted_eps
        basis = "latest_quarter_diluted_eps"
    if eps is None:
        return {}
    pe: float | None = None
    if last_price is not None and eps != 0:
        pe = round(float(last_price) / float(eps), 2)
    return {
        "eps_ttm": eps,
        "pe_ttm": pe,
        "forward_pe": None,
        "valuation_note": (
            f"valuation from {basis} (Yahoo TTM ratios unavailable); "
            "not comparable to headline TTM P/E"
        ),
    }


def fetch_valuation(ticker: str, last_price: float | None) -> dict[str, Any]:
    yahoo = fetch_yahoo_valuation(ticker)
    if yahoo.get("eps_ttm") is not None or yahoo.get("pe_ttm") is not None:
        return yahoo
    return fetch_fundamentals_valuation(ticker, last_price)


def merge_quote_card(
    ticker: str,
    *,
    eodhd_session: dict[str, Any] | None,
    mesh_quote: dict[str, Any] | None,
    valuation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge session, 52w range, and valuation into flat fields for PriceSummary."""
    mesh_sess = mesh_session_from_quote(mesh_quote) if mesh_quote else {}
    mesh_range = mesh_range_from_quote(mesh_quote) if mesh_quote else {}
    sess = dict(mesh_sess)
    if eodhd_session:
        for key in ("last_price", "open", "previous_close", "day_high", "day_low", "volume"):
            if eodhd_session.get(key) is not None:
                sess[key] = eodhd_session[key]
        if eodhd_session.get("change_pct_1d") is not None:
            sess["change_pct_1d"] = eodhd_session["change_pct_1d"]

    val = valuation or {}
    eps_ttm = val.get("eps_ttm")
    pe_ttm = val.get("pe_ttm")
    forward_pe = val.get("forward_pe")
    last = sess.get("last_price")
    if pe_ttm is None and last and eps_ttm:
        try:
            pe_ttm = round(float(last) / float(eps_ttm), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pe_ttm = None

    return {
        "ticker": ticker.strip().upper(),
        "latest_close": last,
        "prev_close": sess.get("previous_close"),
        "change_pct_1d": sess.get("change_pct_1d"),
        "open": sess.get("open"),
        "day_high": sess.get("day_high"),
        "day_low": sess.get("day_low"),
        "volume": sess.get("volume"),
        "year_high": mesh_range.get("year_high"),
        "year_low": mesh_range.get("year_low"),
        "market_cap": mesh_range.get("market_cap"),
        "eps_ttm": eps_ttm,
        "pe_ttm": pe_ttm,
        "forward_pe": forward_pe,
        "valuation_note": val.get("valuation_note"),
        "quote_source": eodhd_session.get("source") if eodhd_session else sess.get("source"),
    }


def build_quote_card(ticker: str) -> dict[str, Any]:
    """Fetch and merge all quote-card fields for one ticker."""
    sym = ticker.strip().upper()
    mesh = fetch_mesh_quote(sym)
    eodhd = fetch_eodhd_session(sym)
    mesh_sess = mesh_session_from_quote(mesh) if mesh else {}
    last = (eodhd or {}).get("last_price") or mesh_sess.get("last_price")
    valuation = fetch_valuation(sym, last)
    return merge_quote_card(
        sym,
        eodhd_session=eodhd,
        mesh_quote=mesh,
        valuation=valuation,
    )

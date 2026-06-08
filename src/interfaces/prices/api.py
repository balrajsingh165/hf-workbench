"""Price display API — v1 endpoints.

Routes:
    GET /api/v1/clock
    GET /api/v1/assets/{symbol}
    GET /api/v1/prices/quotes?tickers=...&include=sparkline
    GET /api/v1/prices/quote?ticker=...
    GET /api/v1/prices/bars?ticker=...&timeframe=1d|1w|1mo|1yr&end=<ISO?>&include=indicators
    GET    /api/v1/watchlist?user_id=...
    POST   /api/v1/watchlist            {user_id, symbol}
    DELETE /api/v1/watchlist/{symbol}?user_id=...
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from db.schema import DB_PATH
from src.clients import eodhd
from src.instruments import resolver
from src.personalization import watchlist as watchlist_store
from src.prices import cache, clock, indicators as ind_mod

router = APIRouter(prefix="/api/v1")

Timeframe = Literal["1d", "1w", "1mo", "1yr"]

_QUOTE_TTL = 45.0
# When the underlying market for a symbol is closed, its quote does not
# change. Cache aggressively so the FE's coordinated 24/7 60s polling loop
# (per design-price-display-v1.md "Frontend coordination contract") doesn't
# hammer EODHD overnight / weekends. 30 min is a balance: short enough that
# the open-market transition surfaces within one or two polls, long enough
# that a 30-row homepage outside RTH is effectively free.
_QUOTE_TTL_CLOSED = 1800.0
_BARS_LIVE_TTL = 60.0
_BARS_LIVE_TTL_CLOSED = 1800.0
_ASSETS_TTL = 3600.0
_BATCH_SIZE = 15

# Asset classes whose schedule tracks US regular trading hours. When
# `clock.get().open` is False, quotes for these classes are stable until
# next open and get the long TTL. Crypto/FX/futures keep the short TTL
# because they continue to trade outside US RTH.
_RTH_LINKED_CLASSES = frozenset({"equity", "equity_index", "etf", "vol", "rate"})

_COMMODITY_EOD_MAP = {
    "GC=F": "GOLD", "CL=F": "WTI", "BZ=F": "BRENT", "NG=F": "NATURAL_GAS",
}

_TIMEFRAME_CONFIG: dict[str, dict] = {
    "1d":  {"endpoint": "intraday", "interval": "5m", "display": 78, "back_days": 5,    "warmup_back_days": 12},
    "1w":  {"endpoint": "intraday", "interval": "1h", "display": 40, "back_days": 15,   "warmup_back_days": 45},
    "1mo": {"endpoint": "eod",      "period": "d",    "display": 22, "back_days": 35,   "warmup_back_days": 300},
    "1yr": {"endpoint": "eod",      "period": "w",    "display": 52, "back_days": 420,  "warmup_back_days": 1825},
}

WARMUP_BARS = 200


# ── Models ───────────────────────────────────────────────────────────


class ClockResponse(BaseModel):
    open: bool
    next_open: str | None
    next_close: str | None


class AssetMeta(BaseModel):
    symbol: str
    name: str
    short: str
    asset_class: str


class ThinQuote(BaseModel):
    ok: bool
    last: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    stale: bool = False
    sparkline: list[float] | None = None
    error: str | None = None


class RichQuote(ThinQuote):
    open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: int | None = None


class QuotesResponse(BaseModel):
    as_of: str
    market_open: bool
    quotes: dict[str, ThinQuote]


class Bar(BaseModel):
    t: str
    o: float
    h: float
    l: float
    c: float
    v: float


class BarsResponse(BaseModel):
    ticker: str
    timeframe: str
    end: str
    bars: list[Bar]
    indicators: dict[str, list[float | None] | None] | None = None
    disabled_indicators: list[str] | None = None
    disabled_reason: str | None = None


# ── /clock ───────────────────────────────────────────────────────────


@router.get("/clock", response_model=ClockResponse)
def get_clock() -> ClockResponse:
    c = clock.get()
    return ClockResponse(open=c.open, next_open=c.next_open, next_close=c.next_close)


# ── /assets/{symbol} ─────────────────────────────────────────────────


@router.get("/assets/{symbol}", response_model=AssetMeta)
def get_asset(symbol: str) -> AssetMeta:
    cache_key = f"asset:{symbol}"
    hit, val = cache.get(cache_key)
    if hit:
        return val
    inst = resolver.get(symbol)
    if inst is None:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")
    result = AssetMeta(
        symbol=inst.symbol,
        name=inst.display,
        short=inst.short,
        asset_class=inst.asset_class,
    )
    cache.set(cache_key, result, _ASSETS_TTL)
    return result


# ── /watchlist ───────────────────────────────────────────────────────
# See docs/design-watchlist.md. No prices in these payloads — the FE feeds
# the returned symbols into the existing /prices/quotes poller.

# Module attribute (not a baked-in default arg) so tests can point the
# routes at a temp DB.
WATCHLIST_DB_PATH = DB_PATH


class WatchlistItem(BaseModel):
    symbol: str
    name: str
    short: str
    asset_class: str
    added_at: str


class WatchlistAddRequest(BaseModel):
    user_id: str = "user_1"
    symbol: str


def _watchlist_conn() -> sqlite3.Connection:
    return sqlite3.connect(WATCHLIST_DB_PATH)


def _to_item(entry: watchlist_store.WatchlistEntry) -> WatchlistItem:
    return WatchlistItem(
        symbol=entry.symbol,
        name=entry.name,
        short=entry.short,
        asset_class=entry.asset_class,
        added_at=entry.added_at,
    )


@router.get("/watchlist", response_model=list[WatchlistItem])
def get_watchlist(user_id: str = Query("user_1")) -> list[WatchlistItem]:
    conn = _watchlist_conn()
    try:
        return [_to_item(e) for e in watchlist_store.list_watchlist(user_id, conn)]
    finally:
        conn.close()


@router.post("/watchlist", response_model=WatchlistItem)
def add_to_watchlist(body: WatchlistAddRequest) -> WatchlistItem:
    conn = _watchlist_conn()
    try:
        try:
            entry = watchlist_store.add_symbol(body.user_id, body.symbol, conn)
        except watchlist_store.UnknownSymbolError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return _to_item(entry)
    finally:
        conn.close()


@router.delete("/watchlist/{symbol}", response_model=list[WatchlistItem])
def remove_from_watchlist(
    symbol: str, user_id: str = Query("user_1")
) -> list[WatchlistItem]:
    """Remove one symbol; returns the updated list so the FE can reconcile."""
    conn = _watchlist_conn()
    try:
        removed = watchlist_store.remove_symbol(user_id, symbol, conn)
        if not removed:
            raise HTTPException(
                status_code=404,
                detail=f"{symbol} is not in {user_id}'s watchlist",
            )
        return [_to_item(e) for e in watchlist_store.list_watchlist(user_id, conn)]
    finally:
        conn.close()


# ── /prices/quotes ───────────────────────────────────────────────────


@router.get("/prices/quotes", response_model=QuotesResponse)
def get_quotes(
    tickers: str = Query(..., description="Comma-separated canonical Yahoo symbols"),
    include: str | None = Query(None),
) -> QuotesResponse:
    symbols = [t.strip() for t in tickers.split(",") if t.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="tickers param is empty")

    want_sparkline = "sparkline" in (include or "")
    clk = clock.get()
    quotes: dict[str, ThinQuote] = {}

    # Pass 1: serve from cache where possible. The FE coordinated poll runs
    # every 60s 24/7; without this gate every poll would hit EODHD even when
    # the cached value is fresh.
    rows: dict[str, dict | None] = {}
    misses: list[str] = []
    for sym in symbols:
        hit, cached_val = cache.get(f"quote:{sym}")
        if hit:
            rows[sym] = cached_val
        else:
            misses.append(sym)

    # Pass 2: batch-fetch only the cache misses. TTL is per-symbol so closed
    # markets cache for 30 min while crypto/FX/futures keep the 45s freshness.
    fetched: dict[str, dict] = {}
    if misses:
        fetched = _batch_real_time(misses)
        for sym in misses:
            row = fetched.get(sym)
            if row is not None:
                cache.set(f"quote:{sym}", row, _quote_ttl_for(sym, clk.open))
            rows[sym] = row

    for sym in symbols:
        row = rows.get(sym)
        # `stale: true` is reserved for the cache-fallback path the design
        # describes (Issue C-2 — currently unreachable; flagged for follow-up).
        # Today, missing rows are simply upstream failures.
        stale = False

        if row is None:
            quotes[sym] = ThinQuote(ok=False, error="upstream_failure")
            continue

        last = _sf(row.get("close") or row.get("last"))
        prev = _sf(row.get("previousClose") or row.get("prev_close"))
        change_pct = _sf(row.get("change_p"))
        if change_pct is None and last is not None and prev and prev != 0:
            change_pct = ((last - prev) / abs(prev)) * 100.0

        sparkline: list[float] | None = None
        if want_sparkline:
            sparkline = _get_sparkline(sym)

        quotes[sym] = ThinQuote(
            ok=True,
            last=last,
            prev_close=prev,
            change_pct=change_pct,
            stale=stale,
            sparkline=sparkline,
        )

    return QuotesResponse(
        as_of=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        market_open=clk.open,
        quotes=quotes,
    )


# ── /prices/quote (rich, single) ─────────────────────────────────────


@router.get("/prices/quote", response_model=RichQuote)
def get_quote_rich(ticker: str = Query(...)) -> RichQuote:
    eodhd_sym = resolver.to_eodhd(ticker)
    cache_key = f"quote_rich:{ticker}"
    hit, row = cache.get(cache_key)
    if not hit:
        try:
            rows = eodhd.real_time_batch([eodhd_sym])
            row = rows[0] if rows else None
        except eodhd.EodhdApiError:
            row = None
        if row:
            cache.set(cache_key, row, _quote_ttl_for(ticker, clock.get().open))

    if not row:
        return RichQuote(ok=False, error="upstream_failure")

    last = _sf(row.get("close") or row.get("last"))
    prev = _sf(row.get("previousClose"))
    change_pct = _sf(row.get("change_p"))
    if change_pct is None and last is not None and prev and prev != 0:
        change_pct = ((last - prev) / abs(prev)) * 100.0

    return RichQuote(
        ok=True,
        last=last,
        prev_close=prev,
        change_pct=change_pct,
        open=_sf(row.get("open")),
        day_high=_sf(row.get("high")),
        day_low=_sf(row.get("low")),
        volume=_int(row.get("volume")),
    )


# ── /prices/bars ─────────────────────────────────────────────────────


@router.get("/prices/bars", response_model=BarsResponse)
def get_bars(
    ticker: str = Query(...),
    timeframe: Timeframe = Query("1d"),
    end: str | None = Query(None),
    include: str | None = Query(None),
) -> BarsResponse:
    want_indicators = "indicators" in (include or "")
    end_dt = _parse_end(end)
    is_replay = end is not None and (datetime.now(timezone.utc) - end_dt).total_seconds() > 300

    cache_key = f"bars:{ticker}:{timeframe}"
    if not is_replay:
        hit, cached_bars = cache.get(cache_key)
        if hit:
            display_bars = cached_bars
        else:
            display_bars = _fetch_bars(ticker, timeframe, end_dt, warmup=False)
            if display_bars:
                cache.set(cache_key, display_bars, _BARS_LIVE_TTL)
    else:
        display_bars = _fetch_bars(ticker, timeframe, end_dt, warmup=False)

    if not display_bars:
        raise HTTPException(status_code=502, detail=f"No bar data for {ticker}/{timeframe}")

    indicator_block: dict[str, list[float | None] | None] | None = None
    disabled: list[str] | None = None
    disabled_reason: str | None = None

    if want_indicators:
        all_bars = _fetch_bars(ticker, timeframe, end_dt, warmup=True)
        all_closes = [b["c"] for b in all_bars]
        computed = ind_mod.compute(all_closes)
        n = len(display_bars)
        def _slice(arr):
            return arr[-n:] if arr else [None] * n
        indicator_block = {
            "rsi_14":  _slice(computed.rsi_14),
            "sma_20":  _slice(computed.sma_20),
            "sma_50":  _slice(computed.sma_50),
            "sma_200": _slice(computed.sma_200) if "sma_200" not in computed.disabled else None,
            "ema_20":  _slice(computed.ema_20),
            "ema_50":  _slice(computed.ema_50),
            "ema_200": _slice(computed.ema_200) if "ema_200" not in computed.disabled else None,
        }
        disabled = computed.disabled or None
        disabled_reason = computed.disabled_reason

    return BarsResponse(
        ticker=ticker,
        timeframe=timeframe,
        end=end_dt.isoformat(),
        bars=[Bar(**b) for b in display_bars],
        indicators=indicator_block,
        disabled_indicators=disabled,
        disabled_reason=disabled_reason,
    )


# ── Internals ────────────────────────────────────────────────────────


def _quote_ttl_for(symbol: str, clock_open: bool) -> float:
    """Per-symbol cache TTL for quote rows.

    Returns the long TTL when the symbol's market is closed (RTH-linked
    asset class + US clock closed). Anything else (clock open, or asset
    class that trades 24/5 / 24/7 like FX, futures, crypto) gets the short
    freshness TTL so the FE polling loop sees real movement.
    """
    if clock_open:
        return _QUOTE_TTL
    inst = resolver.get(symbol)
    if inst is not None and inst.asset_class in _RTH_LINKED_CLASSES:
        return _QUOTE_TTL_CLOSED
    return _QUOTE_TTL


def _batch_real_time(symbols: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    pairs = [(sym, resolver.to_eodhd(sym)) for sym in symbols]
    for i in range(0, len(pairs), _BATCH_SIZE):
        chunk = pairs[i : i + _BATCH_SIZE]
        eodhd_codes = [e for _, e in chunk]
        yahoo_codes = [y for y, _ in chunk]
        try:
            rows = eodhd.real_time_batch(eodhd_codes)
        except eodhd.EodhdApiError:
            continue
        code_map = {r.get("code", ""): r for r in rows if isinstance(r, dict)}
        for yahoo, eodhd_code in zip(yahoo_codes, eodhd_codes):
            row = code_map.get(eodhd_code)
            if row:
                result[yahoo] = row
    return result


def _get_sparkline(symbol: str) -> list[float] | None:
    cache_key = f"sparkline:{symbol}"
    hit, val = cache.get(cache_key)
    if hit:
        return val
    now = datetime.now(timezone.utc)
    bars = _fetch_bars(symbol, "1d", now, warmup=False)
    if not bars:
        bars = _fetch_bars(symbol, "1mo", now, warmup=False)
    if not bars:
        return None
    closes = [b["c"] for b in bars]
    # Same RTH-aware TTL as quotes: a sparkline of an RTH-linked asset
    # doesn't change after-hours, so cache for 30 min and let crypto / FX /
    # futures refresh at the short cadence.
    ttl = _BARS_LIVE_TTL_CLOSED if (
        not clock.get().open and (
            (inst := resolver.get(symbol)) is not None
            and inst.asset_class in _RTH_LINKED_CLASSES
        )
    ) else _BARS_LIVE_TTL
    cache.set(cache_key, closes, ttl)
    return closes


def _fetch_bars(
    symbol: str,
    timeframe: Timeframe,
    end_dt: datetime,
    *,
    warmup: bool,
) -> list[dict]:
    cfg = _TIMEFRAME_CONFIG[timeframe]
    back_days = cfg["warmup_back_days"] if warmup else cfg["back_days"]
    from_dt = end_dt - timedelta(days=back_days)

    try:
        if resolver.is_ust_yield(symbol):
            return _bars_from_yield(from_dt, end_dt, extra)
        if resolver.is_commodity_future(symbol):
            return _bars_commodity(symbol, timeframe, from_dt, end_dt)
        return _bars_standard(symbol, timeframe, cfg, from_dt, end_dt)
    except eodhd.EodhdApiError:
        return []


def _bars_standard(
    symbol: str, timeframe: str, cfg: dict, from_dt: datetime, end_dt: datetime
) -> list[dict]:
    eodhd_sym = resolver.to_eodhd(symbol)
    if cfg["endpoint"] == "intraday":
        raw = eodhd.intraday(
            eodhd_sym,
            interval=cfg["interval"],
            from_ts=int(from_dt.timestamp()),
            to_ts=int(end_dt.timestamp()),
        )
        return [_intraday_to_bar(r) for r in (raw or []) if _valid_bar(r)]
    else:
        raw = eodhd.eod(
            eodhd_sym,
            period=cfg["period"],
            from_date=from_dt.strftime("%Y-%m-%d"),
            to_date=end_dt.strftime("%Y-%m-%d"),
        )
        return [_eod_to_bar(r) for r in (raw or []) if _valid_eod(r)]


def _bars_commodity(
    symbol: str, timeframe: str, from_dt: datetime, end_dt: datetime
) -> list[dict]:
    cfg = _TIMEFRAME_CONFIG[timeframe]
    code = _COMMODITY_EOD_MAP[symbol]
    if cfg["endpoint"] == "eod":
        raw = eodhd.commodities_historical(
            code,
            from_date=from_dt.strftime("%Y-%m-%d"),
            to_date=end_dt.strftime("%Y-%m-%d"),
        )
        return [_eod_to_bar(r) for r in (raw or []) if _valid_eod(r)]
    eodhd_sym = f"{symbol.split('=')[0]}.COMM"
    try:
        raw = eodhd.intraday(
            eodhd_sym,
            interval=cfg["interval"],
            from_ts=int(from_dt.timestamp()),
            to_ts=int(end_dt.timestamp()),
        )
        return [_intraday_to_bar(r) for r in (raw or []) if _valid_bar(r)]
    except eodhd.EodhdApiError:
        return []


def _bars_from_yield(from_dt: datetime, end_dt: datetime, extra: int) -> list[dict]:
    rows = eodhd.ust_yield_rates(
        from_date=from_dt.strftime("%Y-%m-%d"),
        to_date=end_dt.strftime("%Y-%m-%d"),
    )
    bars = []
    for r in (rows or []):
        val = r.get("10_years")
        date = r.get("date")
        if val is None or not date:
            continue
        v = float(val)
        bars.append({"t": date, "o": v, "h": v, "l": v, "c": v, "v": 0.0})
    return bars


def _intraday_to_bar(r: dict) -> dict:
    ts = r.get("datetime") or str(r.get("timestamp", ""))
    return {
        "t": ts,
        "o": float(r.get("open", 0)),
        "h": float(r.get("high", 0)),
        "l": float(r.get("low", 0)),
        "c": float(r.get("close", 0)),
        "v": float(r.get("volume", 0)),
    }


def _eod_to_bar(r: dict) -> dict:
    return {
        "t": r.get("date", ""),
        "o": float(r.get("open", 0)),
        "h": float(r.get("high", 0)),
        "l": float(r.get("low", 0)),
        "c": float(r.get("adjusted_close") or r.get("close", 0)),
        "v": float(r.get("volume", 0)),
    }


def _valid_bar(r: dict) -> bool:
    return bool(r.get("close") and (r.get("datetime") or r.get("timestamp")))


def _valid_eod(r: dict) -> bool:
    return bool(r.get("date") and (r.get("adjusted_close") or r.get("close")))


def _parse_end(end: str | None) -> datetime:
    if not end:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _sf(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _int(val: Any) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None

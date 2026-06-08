## Design: Price Display v1

**Status:** in-progress (EODHD migration landed in 76f3c0a; v1 surface partially shipped)
**Last updated:** 2026-05-10
**Owners:** core
**Scope:** backend HTTP contract for asset surfaces — homepage feed strips, sparklines, hover charts, stock detail pages, and chat-embedded charts — used by `heurist-finance-frontend`.
**Out of scope:** frontend components; the response-time chart agent (`docs/chat-agent-system.md`, Phase 2b) which renders free-form PNGs and is unrelated to this widget surface; WebSocket live-tick path; fundamentals; NER hover-tooltip product (the data middleware accommodates it, but no NER UI work in v1).

---

### Implementation status (at a glance)

| Area | Status | Code |
|---|---|---|
| EODHD client (multi-key round-robin) | ✅ done | `src/clients/eodhd.py` |
| `instruments.eodhd_symbol` column + resolver suffix rules | ✅ done | `src/instruments/resolver.py::to_eodhd`, `db/schema.py` |
| TTL cache | ✅ done | `src/prices/cache.py` |
| Indicator module (RSI, SMA/EMA × 20/50/200) | ✅ done | `src/prices/indicators.py` |
| Market clock | ⚠️ shipped, broken `next_open/next_close` (see Issue C-3) | `src/prices/clock.py` |
| `prices.py` migrated EODHD-first | ⚠️ shipped, `canonicalize` regression (Issue C-4) | `src/clients/prices.py` |
| `GET /api/v1/clock` | ✅ done | `src/interfaces/prices/api.py` |
| `GET /api/v1/assets/{symbol}` | ⚠️ partial — missing `currency, exchange, sector, industry`; ships an unspecified `short` field | `src/interfaces/prices/api.py::get_asset` |
| `GET /api/v1/prices/quotes` (+ `include=sparkline`) | ⚠️ partial — `stale` semantics broken (Issue C-2); no outside-RTH branch | `src/interfaces/prices/api.py::get_quotes` |
| `GET /api/v1/prices/quote` (rich, single) | ⚠️ partial — no outside-RTH branch | `src/interfaces/prices/api.py::get_quote_rich` |
| `GET /api/v1/prices/bars` (+ `include=indicators`, warm-up window, replay-bypass) | ⚠️ shipped, crashes on `^TNX` (Issue C-1) | `src/interfaces/prices/api.py::get_bars` |
| `GET /api/v1/assets/:symbol/news` | ❌ not started | — |
| `GET /api/v1/assets/:symbol/theses` | ❌ not started | — |
| Outside-RTH "frozen at last regular-hours close" branch | ❌ not started | — |
| Drop `instruments.alpaca_symbol` | ❌ kept "for reference" | `db/schema.py` |
| Frontend coordination contract (`PriceQuotesProvider`, single 60s loop) | ❌ frontend-side, not in this repo | — |

Legend: ✅ done · ⚠️ partial / has issues · ❌ not started.

Implementation details that have already shipped are referenced by file/symbol above; the sections below cover only what is still in-flight.

---

### Surfaces → tiers (reference; unchanged)

| # | Surface | Data needed | Where it appears |
|---|---|---|---|
| 1 | Price tag | last + prev_close + Δ% | inline in any text/list |
| 2 | Sparkline | tag + intraday curve | every asset row on the homepage feed (movers, watchlist, indices), thesis cards |
| 3 | Hover chart | switchable 1d/1w/1mo bars + name + numbers | hover over any tag/sparkline (UI deferred, data path live) |
| 4 | Chat chart | switchable 1d/1w/1mo/1yr bars + indicators | embedded in agent chat |
| 5 | Stock detail page | rich quote + switchable bars + indicators + news + theses | dedicated `/asset/[symbol]` route |

Tier mapping, cache TTLs, and surface→primitive matrix: see commit 76f3c0a / [src/interfaces/prices/api.py](file:///home/appuser/hf-workbench/src/interfaces/prices/api.py) for live values. Authoritative TTLs in code: `_QUOTE_TTL=45s`, `_BARS_LIVE_TTL=60s`, `_ASSETS_TTL=3600s`.

Frontend rule (still binding): every homepage asset row carries a sparkline by default; `/quotes?include=sparkline` keeps a 30-row homepage at one round-trip per poll.

---

### Routing — EODHD-first (reference)

All endpoints route through EODHD via [`src/clients/prices.py`](file:///home/appuser/hf-workbench/src/clients/prices.py) and [`src/clients/eodhd.py`](file:///home/appuser/hf-workbench/src/clients/eodhd.py). Mesh/Yahoo remains in the codebase only for the agent's free-form research tools. Routing predicate stays the registry's `instruments.asset_class`; we do not parse Yahoo symbol shapes.

REST quotes from EODHD are 15-min delayed — acceptable for the multi-day / multi-week persona. Sub-15-min ticks would require WebSocket; deferred.

Per-asset-class endpoint mapping and special cases (`^TNX`, futures, `.COMM`) are documented in [docs/ref/eodhd-api.md](file:///home/appuser/hf-workbench/docs/ref/eodhd-api.md) and implemented in `to_eodhd` + `_fetch_bars`.

---

### HTTP contract (binding shapes — kept verbatim)

The wire shapes below are the contract the frontend builds against. Implementation can drift, but the JSON cannot without a coordinated FE change.

#### `GET /api/v1/clock`

```json
{"open": true, "next_open": "2026-05-08T13:30:00Z", "next_close": "2026-05-07T20:00:00Z"}
```

`next_open` / `next_close` MUST be full ISO-8601 timestamps. Currently broken — see Issue C-3.

#### `GET /api/v1/assets/:symbol`

```json
{"symbol": "AAPL", "name": "Apple Inc.", "asset_class": "equity", "currency": "USD", "exchange": "NASDAQ", "sector": "Technology", "industry": "Consumer Electronics"}
```

Static-ish; cache for hours. Does **not** include price. Currently ships `{symbol, name, short, asset_class}` only — see Issue M-2.

#### `GET /api/v1/prices/quotes?tickers=…&include=sparkline`

Per-ticker discriminator: `ok: true` carries the snapshot, `ok: false` carries `error` (`unknown_symbol`, `upstream_failure`, `unsupported_asset`). Partial failures never fail the whole response. `stale: true` means cache returned a value older than its TTL because upstream failed on refresh — frontend renders the value but flags it visually.

```json
{
  "as_of": "2026-05-07T13:32:00Z",
  "market_open": true,
  "quotes": {
    "AAPL":  {"ok": true, "last": 198.42, "prev_close": 196.10, "change_pct": 1.18, "stale": false, "sparkline": [196.1, 196.5]},
    "BADSYM":{"ok": false, "error": "unknown_symbol"}
  }
}
```

#### `GET /api/v1/prices/quote?ticker=…`

Adds `open`, `day_high`, `day_low`, `volume`, `prev_close` to the thin quote.

#### `GET /api/v1/prices/bars?ticker=…&timeframe=1d|1w|1mo|1yr&end=<ISO?>&include=indicators`

Resolution table (coarser, cheaper):

| Timeframe | Bar size | ~Points | EODHD endpoint |
|---|---|---|---|
| 1d  | 5-minute | 78 | `/intraday?interval=5m` |
| 1w  | 1-hour   | 33 | `/intraday?interval=1h` |
| 1mo | 1-day    | 22 | `/eod?period=d` |
| 1yr | 1-week   | 52 | `/eod?period=w` |

Indicator values, when present, are arrays aligned to `bars` (1:1 length). When an indicator's window cannot be seeded, the value is `null` and the key appears in `disabled_indicators` so the frontend can hide that line.

```json
{
  "ticker": "AAPL", "timeframe": "1d", "end": "2026-05-07T13:32:00Z",
  "bars": [{"t":"…","o":1,"h":1,"l":1,"c":1,"v":1}],
  "indicators": {"rsi_14":[null,62.4], "sma_20":[null,197.8], "sma_200": null},
  "disabled_indicators": ["sma_200"], "disabled_reason": "insufficient_history"
}
```

#### `GET /api/v1/assets/:symbol/news?limit=20` *(not yet implemented)*

Articles tagging this ticker. Reads from `entity_tickers`. Returns `[{id, headline, source, published_at, …}]`. Cache 5 min.

#### `GET /api/v1/assets/:symbol/theses` *(not yet implemented)*

Current user's theses tagging this ticker. Owned-only (matches `get_thesis_related` policy). Returns `[{id, statement, status, score, …}]`. Cache 5 min.

---

### URL shape (frontend route)

Stock detail page is `/asset/[symbol]`, with the canonical Yahoo symbol percent-encoded:

- `/asset/AAPL` (US equity)
- `/asset/GC%3DF` (commodity future)
- `/asset/%5EGSPC` (index)
- `/asset/BTC-USD` (crypto, no encoding needed)

NER mentions and chat handoffs construct URLs from canonical Yahoo symbols only. No human-readable slugs in v1.

---

### Frontend coordination contract (binding)

Non-optional. All price-displaying components on a tab share **one** price store:

- A tab-level provider (`PriceQuotesProvider`) holds a registry of mounted tickers.
- Components register / deregister tickers on mount / unmount via `usePriceQuote(ticker)`.
- A single 60s loop unions all registered tickers and hits `/prices/quotes` in one call (paged into ≤15-ticker batches at the EODHD boundary, transparent to the frontend).
- Sparkline curves use `?include=sparkline` on the same loop, not a separate `/bars` fetch per row.

Per-component independent polling is a regression and should fail review.

---

### Polling rules

- One coordinated 60s poll per tab. The poll hits `/quotes` with the union of visible tickers + appropriate `include` flags.
- Suspend when `document.visibilityState === 'hidden'`.
- **Do not** gate the FE poll on `clock.open`. Different asset classes have different schedules (crypto 24/7, FX/futures 24/5, equities 9:30–16:00 ET); a single global gate either over-suspends the open ones or under-suspends the closed ones. The backend per-asset-class TTL (see "Cache layer" below) absorbs the closed-market overhead — every poll for a closed equity is a free local cache hit.
- The clock is read at mount (and every 5 min as a backstop) for the "U.S. equities closed" banner only.
- Hover chart, chat chart, and stock detail page bars fetch on mount; no polling. Chat chart and detail page have manual refresh buttons.
- Stock detail page quote header rides the 60s loop like any other quote consumer; no special path.

---

### Outside regular trading hours *(not yet implemented)*

Polling stops, but **rendered values stay informative**. Multi-day/week traders care about overnight gaps and earnings moves; hiding the daily move is the wrong call.

When `market_open=false`, `/quotes` MUST return:

- `last` = the most recent **regular-hours** close
- `prev_close` = the prior **regular-hours** close
- `change_pct` = computed normally from those two

After-hours and pre-market price action are **not** surfaced in v1. EODHD's 1-min US bars include extended hours; reserved for a later "show extended hours" toggle.

Currently the endpoints unconditionally hit EODHD `/real-time` regardless of market state — see Issue M-1.

---

### Chat chart snapshot mechanism

Stored on the chat message: `chart_spec = {ticker, timeframe, sent_at}` — three fields, no payload bytes. On render, frontend calls `/prices/bars?ticker=…&timeframe=…&end=sent_at&include=indicators` plus a one-time `/assets/:ticker` for the name. Historical bars are immutable, so the series reproduces deterministically across reloads and replays.

**Refresh button is ephemeral.** Clicking it flips a component-local state to call `/prices/bars?…&end=now`. Reloading the page or scrolling the chart out of view and back resets to the `sent_at` view. No 4th field, no persistence — the sent-time snapshot is the canonical record of what the agent saw.

---

### Indicator seeding (rationale — implementation in `_fetch_bars(warmup=True)`)

For any `/bars` request with `include=indicators`, fetch a trailing warm-up window sized to the longest active indicator period (200 bars at the chosen bar size), compute indicators on the full window, and return only the display-window slice with indicator arrays sliced to match. The warm-up bars are not returned. If the upstream provider cannot supply enough trailing bars (new IPO, sparse asset), the affected indicators are returned as `null` and listed in `disabled_indicators` with `disabled_reason: "insufficient_history"`.

We do not call EODHD `/technical` for the v1 indicator set — 7 calls per chart is wasteful when local computation over the warm-up window costs one bars fetch. Reserve `/technical` for indicators we don't implement locally (MACD, Bollinger).

Refactor opportunity tracked in Issue R-3.

---

### Cache layer (reference)

Single FastAPI process for v1; in-process dict with TTL. Replay requests (`end` materially in the past) bypass the cache. Migrate to Redis when we go multi-worker.

**Per-asset-class quote TTL.** `/quotes` and `/quote` size the cache TTL via `_quote_ttl_for(symbol, clock_open)` in `src/interfaces/prices/api.py`:

- RTH-linked classes (`equity`, `equity_index`, `etf`, `vol`, `rate`) + US clock closed → `_QUOTE_TTL_CLOSED = 1800s` (30 min). The underlying market won't move until next open, so a long TTL turns 24/7 FE polling into free cache hits.
- Anything else (US clock open, or asset class is `crypto` / `fx` / `commodity`) → `_QUOTE_TTL = 45s`. Crypto/FX/futures continue trading outside US RTH; they need short freshness regardless of US clock state.

Sparkline TTL follows the same rule (`_BARS_LIVE_TTL_CLOSED = 1800s` vs `_BARS_LIVE_TTL = 60s`).

`/quotes` resolves cache hits **first**, then batches an EODHD `/real-time` call for only the cache-miss subset. Without this, the FE's 60s coordinated poll over a 30-row homepage would hit EODHD on every tick even with a fully-warm cache.

---

### Auth

Existing user auth on all endpoints. EODHD `api_token` is a backend secret, never returned to the frontend. No per-user rate limits in v1: the shared backend cache naturally protects upstream, and any single user is bounded by one 60s coordinated poll per tab. Plan ceiling is 100k req/day, 1k req/min — comfortably above v1 traffic.

---

## Outstanding issues (from review of 76f3c0a)

Severities: **C** = critical (will break a request path or silently corrupt behavior), **M** = medium (spec deviation, observable to FE / users), **R** = refactor / hygiene (no behavior bug today).

### C-1 · `/prices/bars` crashes on `^TNX` (NameError)

`_fetch_bars` calls `_bars_from_yield(from_dt, end_dt, extra)` but `extra` is undefined in that scope; `_bars_from_yield` itself takes an unused `extra` parameter. Every UST yield bars request will raise `NameError`.

**Fix:** drop the third argument from both call site and signature.

### C-2 · `stale: true` cache-fallback is unreachable

`cache.get` deletes expired entries on read, so the `else: stale = True; _, row = cache.get(cache_key)` path in `/quotes` always sees `row=None` and returns `ok: false, error: "upstream_failure"` instead of the stale-but-renderable payload the design promises.

**Fix:** add a `peek_expired()` (or `get(allow_expired=True)`) method to `cache.py` that returns the value without deleting, and use it in the upstream-failure branch. Alternatively, store a separate `last_known:{key}` entry with no TTL.

### C-3 · `/clock.next_open` / `next_close` are HH:MM strings, not ISO datetimes

EODHD's `TradingHours.OpenUTC` / `CloseUTC` are time-of-day strings (e.g., `"13:30:00"`), not full timestamps. The wire contract above and the FE's "resume at `next_open`" wall-clock comparison both expect a full ISO timestamp. Frontend resume logic will be incorrect.

**Fix:** combine the time-of-day with the next valid trading date (consult `ExchangeHolidays` + `ExchangeEarlyCloseDays`) in `clock._fetch` before returning.

### C-4 · `prices.canonicalize` lost registry-alias resolution

The pre-EODHD impl called `resolver.canonical(raw)`, mapping `USDJPY → JPY=X`, `DXY → DX-Y.NYB`, `BTC → BTC-USD`. The new impl only upper-cases the input. Tailwind / movers callers that pass alias rows now hit EODHD with the wrong symbol and silently get nulls.

**Fix:** restore `return resolver.canonical(raw)` after the upper-case normalization.

### C-5 · EODHD key rotation has no per-request retry

`_get` rotates one key per call; on 429 / 5xx the call raises immediately. The whole point of the multi-key array is to absorb per-key rate-limits — currently a single throttled key takes down 1/N of all requests.

**Fix:** in `_call`, on `EodhdApiError` with status 429 / 5xx, retry up to `len(_keys)-1` times with subsequent keys before giving up. Carry the HTTP status on `EodhdApiError` to make this discriminable.

### M-1 · No outside-RTH "frozen at last close" branch

`/quotes` and `/quote` unconditionally hit `/real-time` regardless of `clock.open`. Per design, `market_open=false` should freeze `last` / `prev_close` at the most recent regular-hours close. Today the FE will see whatever EODHD returns (may be stale, may be midnight zero) outside hours.

**Fix:** when `clock.open` is false, replace the real-time call with a 1-day EOD lookup and synthesize the thin/rich quote from those two closes. Cache aggressively (24h until next open).

### M-2 · `/assets/{symbol}` shape doesn't match contract

Ships `{symbol, name, short, asset_class}`; spec requires `{symbol, name, asset_class, currency, exchange, sector?, industry?}` and does not include `short`. The FE detail-page header and chat-chart title cannot render currency / exchange chips without these.

**Fix:** extend the `instruments` table (or join EODHD `/fundamentals`) and remove `short` from the response. If `short` is genuinely needed by another caller, expose it via a separate label endpoint.

### M-3 · Sparkline fallback to `1mo` daily bars violates spec

`_get_sparkline` falls back to a `1mo` daily-bar window when `1d` returns empty. Spec is "1d / 5-min, ~78 close-only points." A daily-bar curve has fundamentally different shape and resolution.

**Fix:** return `None` (and let `sparkline` stay null in the response) instead of substituting a wrong-shape curve.

### M-4 · `/bars` raises 502 instead of returning per-ticker `unsupported_asset`

For futures intraday and other holes in EODHD coverage the design specifies `{ok: false, error: "unsupported_asset"}` per the `/quotes` partial-failure model. `/bars` instead 502s the whole response, which is a worse FE experience and breaks the unified error shape.

**Fix:** return a 200 response with an `ok: false, error: "unsupported_asset"` envelope, mirroring the quotes shape.

### M-5 · `alpaca_symbol` column not dropped

Design says drop pre-launch; commit kept it "for reference." This compromises the "no backcompat" principle and leaves a dead column on the registry.

**Fix:** drop the column, remove the field from `Instrument`, remove the `alpaca` `Vendor` literal.

### M-6 · Sparkline cache drift

`_get_sparkline` writes to a separate `sparkline:{symbol}` key instead of piggy-backing on the `bars:{ticker}:1d` key. Spec is "computed by piggy-backing the bars cache; no extra upstream round-trip on cache hit." Two caches drift and double the upstream call cost on cold load.

**Fix:** read closes off the `bars:{ticker}:1d` cache; populate that cache as a side-effect when sparkline is requested.

### M-7 · Composition endpoints not implemented

`GET /assets/:symbol/news` and `GET /assets/:symbol/theses` are required for the stock detail page (Surface 5). Not present in 76f3c0a.

**Fix:** new `src/interfaces/prices/composition.py` (or extend `api.py`) reading from `entity_tickers` and `user_theses` respectively.

### R-1 · Duplicated EODHD batch / commodity logic across two modules

`_BATCH_SIZE`, `_COMMODITY_EOD_MAP`, the `pairs = [(sym, to_eodhd(sym)) for sym in syms]` batch loop, and `_safe_float`/`_sf` are duplicated between [src/clients/prices.py](file:///home/appuser/hf-workbench/src/clients/prices.py) and [src/interfaces/prices/api.py](file:///home/appuser/hf-workbench/src/interfaces/prices/api.py). The commodity map is *triplicated* — also lives in `resolver._COMMODITY_FUTURES`.

**Fix:** move the canonical commodity map and EODHD batch helper into `src/prices/quotes.py`; have both `clients/prices.py` and `interfaces/prices/api.py` import from it. Make `resolver._COMMODITY_FUTURES` the single source of truth.

### R-2 · `EodhdApiError` should carry HTTP status

Without status, callers cannot distinguish "rate-limited, retry on next key" from "permanent 4xx, give up." Required by C-5.

### R-3 · Warm-up window re-fetches the entire bars range

`/bars` with `include=indicators` calls `_fetch_bars(..., warmup=True)`, which fetches the full 200-bar warmup span from upstream and discards the already-cached display window. On a warm cache this is one wasted upstream call per indicator-bearing chart load.

**Fix:** fetch only the warm-up *prefix* (older than the cached display window), concatenate, then slice. Or: cache the warm-up window separately keyed by `bars_warmup:{ticker}:{timeframe}` with the same TTL.

### R-4 · `IndicatorSet` + `_slice` double-guard disabled indicators

`indicators.compute._try` already returns `None` when an indicator is fully empty; `_slice(computed.sma_200) if "sma_200" not in computed.disabled else None` in `api.py` re-checks the same thing. Collapse to a single representation: have `compute` return `dict[str, list | None]` and drop the `IndicatorSet` NamedTuple ceremony.

### R-5 · Unused imports in `prices.py`

`import math`, `import time` in `src/clients/prices.py` are dead.

### R-6 · `cache.set` shadows the builtin `set`

Cosmetic. Rename to `put` or have callers `from src.prices import cache` and accept the shadow at the call site.

### R-7 · Dead `prev_close` lookup branches

`/prices/quote` looks up `row.get("previousClose") or row.get("prev_close")`. EODHD only emits `previousClose`; the `prev_close` branch is dead. Remove for clarity.

---

### Deferred (TODO — unchanged)

- **Fundamentals** (P/E, P/S, market cap, financials) for the chat chart and stock detail page. Requires EODHD Fundamentals plan ($59.99) or All-In-One bundle. Reserve a slot in `/assets/:symbol` so adding it is non-breaking. v1 stock page header is price-only.
- **NER hover popover.** Data primitives are designed to support it; pipeline + UI + symbol-resolver endpoint are out of scope.
- **Extended-hours price display** (pre-market, after-hours). EODHD 1m US bars already include them; gate behind a per-user toggle.
- **WebSocket live ticks.** EODHD `wss://ws.eodhistoricaldata.com/ws/{market}`. Revisit only if 60s polling proves insufficient.
- **Sector heatmap** on homepage. Composes from existing tailwind cache.
- **Redis cache** (multi-worker milestone).
- **Indicator expansion**: MACD, Bollinger, volume bars. Likely via EODHD `/technical`.
- **Chat-message asset-chart as a first-class AI SDK part-type** (vs the current `chart_spec` stamp).

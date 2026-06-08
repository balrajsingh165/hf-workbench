# EODHD API Reference

Usage notes for the EODHD Financial Data REST API as consumed by the price-display and fundamentals surfaces. Source of truth: [EODHD OpenAPI 3.1.0 spec](https://github.com/EodHistoricalData/EODHD-openapi).

**Plan in use:** EOD+Intraday — All World Extended ($29.99/mo, $24.99/mo annual).

---

## Plan limits

| Limit | Value |
|---|---|
| Requests per day | 100,000 |
| Requests per minute | 1,000 |
| EOD historical depth | 30+ years (incl. delisted) |
| Intraday history (US 1m) | since 2004 |
| Intraday history (FX/crypto 1m) | since 2009 |
| Intraday history (5m/15m/30m/1h, all markets) | since Oct 2020 |
| Intraday max range / request | 1m: 120d · 5m: 600d · 1h: 7,200d |

Rate-limit headers on `429`: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`.

---

## Auth & base URL

- Base: `https://eodhd.com/api`
- Auth: `?api_token=<TOKEN>` query parameter on every request (no header auth).
- Format: `?fmt=json` (default for most endpoints; intraday defaults to `csv` — always pass `fmt=json` explicitly).

```bash
curl "https://eodhd.com/api/eod/AAPL.US?api_token=$EODHD_TOKEN&fmt=json"
```

---

## Symbol format

Pattern: `SYMBOL.EXCHANGE_CODE`. Exchange codes are not MIC codes — see `/exchanges-list` for the canonical set.

Mapping from our registry's Yahoo-form symbols to EODHD form (mechanical, build once into `instruments.resolver`):

| Asset class | Our symbol | EODHD symbol |
|---|---|---|
| US equity / ETF | `AAPL`, `SPY` | `AAPL.US`, `SPY.US` |
| Korea (KOSPI) | `005930.KS` | `005930.KO` |
| Tokyo | `9684.T` | `9684.TSE` |
| London | `BA.L` | `BA.LSE` |
| Xetra | `RHM.DE` | `RHM.XETRA` |
| Shanghai | `600346.SS` | `600346.SHG` |
| Moscow | `PHOR.ME` | `PHOR.MCX` |
| Indices | `^GSPC`, `^VIX`, `^DJI`, `^IXIC` | `GSPC.INDX`, `VIX.INDX`, `DJI.INDX`, `IXIC.INDX` |
| FX | `USDJPY=X`, `EURUSD=X` | `USDJPY.FOREX`, `EURUSD.FOREX` |
| Crypto | `BTC-USD` | `BTC-USD.CC` |
| Commodities (futures) | `GC=F`, `CL=F`, `BZ=F`, `NG=F` | special-case — see [Commodities](#commodities) |
| 10Y Treasury yield | `^TNX` | special-case — see [Treasury yields](#treasury-yields) |

Confirm exchange codes for our registry against `/exchange-details/{CODE}` before committing the mapping table — the codes above are the documented ones but Korea/Shanghai/Moscow have shifted historically.

---

## Endpoints we use

### `GET /real-time/{ticker}` — quote snapshot

Powers the `/api/v1/prices/quotes` surface. **15-min delayed** on REST (real-time requires WebSocket, out of scope per `design-price-display-v1.md`).

Multi-ticker batching via `s=` query param — comma-separated additional tickers. The path ticker is one symbol; the rest go in `s=`. Recommended batch size: 15–20 to stay safe.

```
GET /real-time/AAPL.US?s=MSFT.US,GOOGL.US,EUR.FOREX&api_token=...&fmt=json
```

Response (array when batched, object when single):

```json
[
  {
    "code": "AAPL.US",
    "timestamp": 1729888080,
    "gmtoffset": 0,
    "open": 229.74, "high": 233.22, "low": 229.57, "close": 231.41,
    "volume": 37931706,
    "previousClose": 230.57,
    "change": 0.84,
    "change_p": 0.3643
  }
]
```

Map to our `Quote`: `last = close`, `prev_close = previousClose`, `change_pct = change_p`.

### `GET /intraday/{ticker}` — intraday OHLCV

Powers `/api/v1/prices/bars` for `1d` and `1w` timeframes.

| Param | Notes |
|---|---|
| `interval` | `1m`, `5m`, `15m`, `30m`, `1h` (required) |
| `from`, `to` | **Unix timestamps in seconds (UTC)** — not ISO dates. `1627896900` = `2021-08-02 09:35 UTC` |
| `fmt` | Default is `csv` — pass `fmt=json` |

```
GET /intraday/AAPL.US?interval=5m&from=1762488000&to=1762574400&api_token=...&fmt=json
```

Response array of `{timestamp, gmtoffset, datetime, open, high, low, close, volume}`. `datetime` is in UTC.

Range caps: 120 days per `1m` request, 600 days per `5m`, 7,200 days per `1h`.

### `GET /eod/{ticker}` — daily / weekly / monthly bars

Powers `/api/v1/prices/bars` for `1mo` (daily) and `1yr` (weekly) timeframes.

| Param | Notes |
|---|---|
| `period` | `d` (default), `w`, `m` |
| `from`, `to` | ISO dates `YYYY-MM-DD` |
| `filter` | Optional shortcut: `last_close`, `last_volume`, etc. → returns a single number instead of an array |

```
GET /eod/AAPL.US?period=d&from=2026-04-08&to=2026-05-08&api_token=...&fmt=json
```

Response: `[{date, open, high, low, close, adjusted_close, volume}, …]`. Use `adjusted_close` for return calculations across splits/dividends.

### `GET /technical/{ticker}` — server-computed indicators

Optional alternative to `src/prices/indicators.py`. Server returns the indicator value series aligned to bars; saves a warm-up round-trip and local computation.

Functions: `sma`, `ema`, `wma`, `rsi`, `macd`, `bbands`, `atr`, `cci`, `adx`, `dmi`, `stochastic`, `stochrsi`, `slope`, `stddev`, `volatility`, `sar`, `beta`, `splitadjusted`, `avgvol`, `avgvolccy`.

```
GET /technical/AAPL.US?function=rsi&period=14&from=2026-04-01&api_token=...&fmt=json
GET /technical/AAPL.US?function=sma&period=200&from=2026-04-01&api_token=...&fmt=json
```

One indicator per call. For our v1 set (RSI(14), SMA/EMA at 20/50/200) this is **7 calls per chart**, which is wasteful — keep `src/prices/indicators.py` for inline computation and reserve `/technical` for one-offs the local module doesn't cover (MACD, Bollinger).

### `GET /fundamentals/{ticker}` — fundamentals (NOT in this plan)

**Not included in EOD+Intraday Extended.** Returns `403`. Requires the standalone Fundamentals plan ($59.99) or All-In-One bundle ($99.99 / $83 annual).

When we upgrade, this is the endpoint:

```
GET /fundamentals/AAPL.US?filter=Highlights&api_token=...
GET /fundamentals/AAPL.US?filter=Valuation&api_token=...
```

`filter` selects a section to avoid the multi-MB full payload. Sections: `General`, `Highlights` (P/E, market cap, EPS), `Valuation` (P/B, P/S, EV/EBITDA), `SharesStats`, `Technicals`, `SplitsDividends`, `AnalystRatings`, `Holders`, `InsiderTransactions`, `ESGScores`, `outstandingShares`, `Earnings`, `Financials::Income_Statement::quarterly::2025-12-31`, etc.

### Treasury yields

`^TNX` (10Y constant-maturity yield) is **not** a standard ticker on EODHD. Use the Treasury endpoint and read the field:

```
GET /ust/yield-rates?from=2026-04-01&to=2026-05-08&api_token=...&fmt=json
```

Response includes `1_month`, `3_months`, `1_year`, `2_years`, `10_years`, `30_years`. Read `10_years` for `^TNX`. The endpoint returns yield curve rates — to render as a "price" series, treat the yield as the value and skip OHLCV (it's a single daily number per maturity).

### Commodities

The dedicated `/commodities/historical/{code}` endpoint is **daily-only** and uses commodity codes, not contract symbols:

| Our ticker | EODHD code |
|---|---|
| `GC=F` | `GOLD` |
| `CL=F` | `WTI` |
| `BZ=F` | `BRENT` |
| `NG=F` | `NATURAL_GAS` |

```
GET /commodities/historical/GOLD?interval=daily&from=2026-04-01&api_token=...&fmt=json
```

For **intraday on commodity futures**, EODHD's `.COMM` virtual exchange is the path (e.g. `GC.COMM` via `/intraday`). Coverage is uneven — verify each symbol against `/exchange-symbol-list/COMM` before relying on intraday for the chart.

### `GET /exchanges-list` — exchange catalog

Returns `[{Name, Code, OperatingMIC, Country, Currency, CountryISO2, CountryISO3}, …]`. Run once per registry rebuild to validate exchange-code mapping.

### `GET /exchange-details/{CODE}` — trading hours, holidays

Replaces our use of Alpaca `/v2/clock`. Returns `Timezone`, `TradingHours.OpenUTC/CloseUTC`, `ExchangeHolidays`, `ExchangeEarlyCloseDays`, `isOpen`. Cache 60s in `src/prices/clock.py`.

```
GET /exchange-details/US?api_token=...&fmt=json
```

### `GET /search/{query}` — symbol search

Used by the agent's free-form ticker resolution path.

```
GET /search/Tesla?type=stock&exchange=US&api_token=...&fmt=json
```

Filters: `type` (`stock`, `etf`, `fund`, `bond`, `index`, `crypto`), `exchange`, `limit`.

---

## Error model

Standard HTTP codes:

- `400` — bad params
- `401` — missing/invalid `api_token`
- `403` — endpoint not in your subscription (e.g. `/fundamentals` on this plan)
- `404` — unknown ticker / no data in range
- `429` — rate-limited; check `Retry-After`
- `5xx` — upstream

Error body:

```json
{"status": 400, "error": "Bad Request", "message": "..."}
```

Treat `403` specially in the client — it indicates a tier mismatch, not a transient failure.

---

## What's NOT in this plan

| Feature | Where it lives | Cost |
|---|---|---|
| Fundamentals (P/E, P/S, mcap, statements) | Fundamentals plan or All-In-One | $59.99 standalone or $99.99 bundle ($83/mo annual) |
| Bonds (sovereign, corporate) | All-In-One | bundle only |
| Real-time quotes (sub-15min) | WebSocket `wss://ws.eodhistoricaldata.com/ws/{market}` | streaming endpoint, separate auth |
| News feed, sentiment | Marketplace add-on | separate |
| US options chains | Marketplace add-on | separate |
| ESG (Investverte), risk analytics (illio), PRAAMS | Marketplace add-ons | per-product |

---

## Practical gotchas

- **Intraday `from`/`to` are Unix seconds**, not ISO. Easy to get wrong; wrap in a helper.
- **Intraday default fmt is CSV**, EOD/quote default is JSON. Always pass `fmt=json` to be safe.
- **`s=` batching has no documented hard cap**, but stay ≤20 per call for predictable latency. The path ticker counts toward the batch.
- **`adjusted_close` exists only on `/eod`**, not `/intraday`. Long-window returns must use EOD.
- **`^TNX` and `^VIX` differ**: `^VIX` is `VIX.INDX` (standard `/intraday` + `/eod`), but `^TNX` requires `/ust/yield-rates`.
- **Commodity futures intraday** is split between `/commodities/historical` (daily-only) and `.COMM` virtual exchange (intraday, coverage TBD). Don't assume both work for any given symbol.
- **Free `?api_token=demo`** works for `AAPL.US`, `MSFT.US`, `BTC-USD.CC` only — useful for smoke tests without burning credits.

# Design: Watchlist (production)

**Status:** shipped (2026-06-03)
**Scope:** Promote the watchlist from a markdown-only prompt input to a real, structured, read/write feature with its own DB table, REST BFF for the frontend homepage, and a one-shot migration off `profile.md`. Watchlist **only** — the rest of the personalization profile (`sectors_of_interest`, `risk_tolerance`, `experience`, `asset_classes`) stays in markdown for now (see [Deferred](#deferred)).
**Supersedes (for watchlist):** the `user_preferences.watchlist_json` storage proposed in [`design-profile-as-scaffolding.md`](./design-profile-as-scaffolding.md). That doc bundled watchlist into a single per-user JSON row; we instead give watchlist a dedicated relational table (decision below). The two docs must not both own watchlist storage — this one wins.

---

## Why

Today the watchlist is a `## Watchlist` section in `users/{id}/profile.md`, parsed at request time by `src/personalization/parser.py` and read by exactly one consumer: the chat agent's prompt builder (`src/agent/prompt_manager.py`), and only when `HF_PERSONALIZATION=on` (off in prod). There is:

- **No DB column** — nothing can query, sort, or join on it.
- **No write path** — a user cannot add or remove a symbol.
- **No frontend** — `components/layout/right-rail.tsx` renders a hardcoded `// @MOCK` watchlist (NFLX/DIS/AMZN).

The product needs the homepage to show the user's real watchlist with live performance, and the user to curate it. That requires structured storage, a read endpoint, and write endpoints.

## What we're building

1. A dedicated `user_watchlist` DB table (row per entry).
2. A read endpoint `GET /api/v1/watchlist` returning the user's watchlist (ordered by `added_at`) with instrument metadata. The frontend feeds those symbols into the **existing** `/api/v1/prices/quotes` poller for live performance — no new performance code.
3. Write endpoints: add, remove.
4. Two frontend surfaces: a watchlist panel in the `/feed` right rail (the "homepage"), and a dedicated `/watchlist` page in the left nav.
5. A one-shot migration of the seeded `profile.md` watchlists into the table, and a switch of the agent read path to read watchlist from the table instead of markdown.

Non-goals are listed under [Deferred](#deferred).

---

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Storage | **Dedicated `user_watchlist` table** (not `watchlist_json`) | Row-per-entry gives per-item `added_at`, a real dedup constraint, and FK integrity to `instruments`. The design-scaffolding doc itself noted JSON is only fine "while we only query `WHERE user_id = ?`". |
| Scope | **Watchlist only**; defer the rest of `user_preferences` | Tight scope. The other profile slots have no write/read demand yet; bundling them in is speculative. |
| BFF read shape | **Return symbols + instrument meta; FE reuses the quote poller** | `/api/v1/prices/quotes?tickers=…&include=sparkline` already returns `{last, change_pct, sparkline}` and the `/feed` right rail already polls it every 60s via `PriceQuotesProvider`. Composing quotes server-side would duplicate that logic and lose the live refresh. |
| Read shape | **Plain ordered array, no server pagination** | Watchlists are small (tens of entries); the `/watchlist` page paginates client-side. One array endpoint serves both the right-rail panel and the page — no `limit`/`offset`/`total` machinery. |
| No size cap | **Unbounded** | The homepage doesn't hold many assets in practice; a cap is machinery without a problem to solve. |
| Ordering | **`added_at`, no custom reorder** | Drag-to-reorder (and its `sort_order` column + move endpoint) is cut for v1. A small watchlist is scannable in insertion order; reorder can be added later with one column + one endpoint, pre-launch with no migration pain. |
| Write surface | **REST endpoints only** (add / remove) | The chat agent's `update_profile` tool is deferred to TODO. |
| Auth | **`user_id` query/body param**, default `user_1` | Matches every existing route (`/api/home`, `/api/thesis/*`). No real auth exists yet; inventing it here is out of scope. |

---

## Storage

New table in `db/schema.py` `TABLES`:

```python
"user_watchlist": [
    ("user_id",    "TEXT NOT NULL REFERENCES users(id)"),
    ("symbol",     "TEXT NOT NULL REFERENCES instruments(symbol)"),
    ("added_at",   "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ("PRIMARY KEY (user_id, symbol)", ""),           # one row per (user, symbol); natural dedup
],
```

The read path is `WHERE user_id = ? ORDER BY added_at, symbol`; the composite PK already serves the `user_id` prefix lookup, so no extra index is needed.

Notes:

- **`symbol` is the canonical Yahoo symbol** (the `instruments.symbol` PK — e.g. `AAPL`, `BTC-USD`, `^TNX`). All writes normalize the incoming symbol through the alias-aware `resolve_symbol` gate in the access module before insert. This guarantees the FE can hand the stored symbols straight to `/api/v1/prices/quotes` and to `/api/v1/assets/{symbol}`.
- **FK to `instruments`** means a watchlist can only hold symbols the price layer knows how to quote. Unknown symbols are rejected at the API boundary (below), not silently coerced.
- No JSON columns; no overlap with markdown — consistent with the repo's "DB is the queryable index" principle.

### Access module

A thin module `src/personalization/watchlist.py` owns all reads/writes (no SQL in `app.py`):

```python
@dataclass(slots=True)
class WatchlistEntry:                 # joined with instruments meta — one shape
    symbol: str                       # serves both the list read and the add return
    name: str
    short: str
    asset_class: str
    added_at: str

def resolve_symbol(symbol: str, conn) -> str | None: ...                 # alias-aware; canonical Yahoo symbol or None
def list_watchlist(user_id: str, conn) -> list[WatchlistEntry]: ...      # ORDER BY added_at, symbol
def add_symbol(user_id: str, symbol: str, conn) -> WatchlistEntry: ...   # resolves+validates; idempotent
def remove_symbol(user_id: str, symbol: str, conn) -> bool: ...          # returns True if a row was deleted
```

Resolution runs SQL on the caller's connection (exact symbol match, then
`aliases_json` via `json_each`, following alias rows' `canonical_symbol`) —
not the `src.instruments.resolver` in-process cache, which keys rows by exact
symbol and cannot match aliases like `TSMC` → TSM.

`add_symbol` is idempotent: adding a symbol already present is a no-op that returns the existing entry (dedup is the PK; we don't error). Validation (resolve via `instruments`) lives here so both the REST layer and the future agent tool share one gate.

---

## Read path (BFF)

```
GET /api/v1/watchlist?user_id=user_1
→ 200 [
    { "symbol": "NVDA", "name": "NVIDIA Corp", "short": "Nvidia",
      "asset_class": "equity", "added_at": "2026-05-22T…Z" },
    { "symbol": "BTC-USD", "name": "Bitcoin", "short": "BTC",
      "asset_class": "crypto", "added_at": "…" },
    ...
  ]
```

- Joins `user_watchlist` to `instruments` (or calls `resolver.get`) to attach `name`/`short`/`asset_class` so the FE can render a labeled row without a second round trip — the same fields `/api/v1/assets/{symbol}` returns.
- Ordered by `added_at ASC, symbol ASC` (insertion order; ties broken deterministically).
- **No prices in this payload.** The FE renders the rows, then registers the symbols with `PriceQuotesProvider`, which polls `/api/v1/prices/quotes?tickers=…&include=sparkline` (existing) for `{last, change_pct, sparkline}`. Live 60s refresh comes for free; we write zero new performance code.
- **No pagination params.** Returns the full ordered array; the `/watchlist` page slices it client-side. Empty watchlist → `200 []`.

Lives in the prices v1 router (`src/interfaces/prices/api.py`, `prefix="/api/v1"`) since it sits next to `/assets` and `/prices/quotes` and the FE consumes them together. Pydantic model `WatchlistItem` mirrors `AssetMeta` + the three watchlist fields.

## Write path (REST)

All scoped to `user_id` (body or query); all normalize symbols through the resolver; all return the updated entry/list so the FE can update optimistically without a refetch.

| Method & route | Body | Behavior | Errors |
|---|---|---|---|
| `POST /api/v1/watchlist` | `{ "user_id": "...", "symbol": "nvda" }` | Resolve+validate symbol → insert → return `WatchlistItem`. Idempotent if already present. | `422` if symbol doesn't resolve in `instruments` (message includes the raw input + a hint to check the ticker). |
| `DELETE /api/v1/watchlist/{symbol}` | `?user_id=...` | Remove the row. | `404` if not in the user's watchlist. |

Validation reuses `add_symbol`'s resolver gate. Unknown-symbol handling is a hard `422`, not a `pending_instruments` insert — adding to the instrument universe is a separate pipeline concern, and a watchlist entry the price layer can't quote is useless on the homepage.

No bulk add endpoint in v1 (onboarding, which would want it, isn't built). Add-one is enough for the curate-from-homepage flow.

---

## Migration & agent read-path switch

This is the one place we touch existing behavior, so it's explicit.

1. **One-shot migration script** (`scripts/migrate_watchlist_to_db.py`, run once): for each `users/{id}/profile.md`, parse the `## Watchlist` section (reuse `parse_profile_md`), resolve each symbol through the resolver, and insert rows (file order is not preserved — ordering is `added_at`, and these all migrate at once; that's fine). **Unresolved symbols are logged and skipped, not inserted** (FK would reject them anyway). The seeded files contain a few that may need resolver attention — e.g. `TSMC` (canonical is likely `TSM`), `BTC` (`BTC-USD`), `DXY`, `^TNX`, `TSCO`. The script prints a per-user resolved/skipped report so we can fix aliases in `instruments` and re-run rather than silently dropping symbols.
2. **Agent read path** switches its watchlist source from markdown to the table. In `src/personalization/`, `parse_profile_md` keeps reading the *other* slots (sectors/experience/risk) from markdown, but `StoredProfile.watchlist` is populated from `list_watchlist(user_id, conn)`. Cleanest seam: a small `load_stored_profile(user_id, conn)` that calls `parse_profile_md` then overrides `.watchlist` from the table; `prompt_manager._build_personalization_block` / `_build_holdings_block` call that instead of `parse_profile_md` directly. The render functions are unchanged.
3. **Drop the `## Watchlist` section** from the seeded `profile.md` files after migration (pre-launch, no back-compat — the table is now the source of truth). The narrative slots remain in markdown until the broader `user_preferences` work happens.
4. `derive_profile` (implicit watchlist from `user_theses`) is **unchanged** — it already reads the DB and is independent of the stored watchlist.

This keeps the personalization read contract identical (`StoredProfile` shape, render output) while moving watchlist storage underneath it.

---

## Frontend changes

`~/heurist-finance-frontend`. "Homepage" = `/feed` (a temporary dev convenience — all info lives on `/feed` for now), so the watchlist surfaces in two places:

1. **`/feed` right-rail panel.** Replace the `// @MOCK` Watchlist section in `components/layout/right-rail.tsx` with data from `GET /api/v1/watchlist`. The existing `TickerRow` + `usePriceQuote(symbol, {sparkline:true})` machinery renders performance unchanged — only the symbol-list source changes (mock array → fetched watchlist). Read-only here.
2. **Dedicated `/watchlist` page.** New nav entry in `components/layout/app-sidebar.tsx` (`getNav()`), alongside `Feed` and `Convictions`. New route `app/watchlist/page.tsx`: the full list with per-row performance (same `usePriceQuote` machinery), client-side pagination (slice the array), and add / remove wired to the write endpoints. Optimistic updates; endpoints return the new state.
3. Re-run `bun run gen:types` after the routes land so `src/lib/api-types.ts` picks up `WatchlistItem` and the request bodies (per the CLAUDE.md frontend-contract rule).

The trending section in the right rail stays mock for now — out of scope.

---

## Testing

- **Unit** (`src/personalization/watchlist.py`): add normalizes aliases (`tsmc`→`TSM`), add is idempotent, remove returns False when absent, unknown symbol raises.
- **API**: round-trip add → list → remove for a fresh user; `422` on junk symbol; `404` on removing an absent symbol; empty list for a user with no watchlist.
- **Migration**: dry-run against the four seeded users; assert the resolved/skipped report and that all resolved symbols land as rows.
- **Agent read parity**: with the table populated and `HF_PERSONALIZATION=on`, `build_phase2_system_prompt(user_id="user_1")` renders the same `<user_profile>` watchlist line it did from markdown (snapshot).
- No `hf-evals` run needed — this doesn't change agent *behavior*, only the watchlist's storage substrate (parity test above covers the prompt).

---

## Deferred (tracked in `TODO.md` § Watchlist)

- **Agent `update_profile` tool** for chat-driven watchlist edits ("add NVDA to my watchlist"). The `add_symbol`/`remove_symbol` gate is built to be reused when this lands.
- **Rest of `user_preferences`** (sectors/risk/experience/asset_classes) moving from markdown to DB.
- **`user_profile_events` audit log** for watchlist mutations — no reviewer/undo flow demands it yet.
- **Bulk add** (wants onboarding, which isn't built).
- **Drag-to-reorder** (custom ordering): one `sort_order` column + one move endpoint when the product wants it.
- **Per-entry `note`** field.
- **Wiring the right-rail "Trending" section** to `/api/trending`.


"""User watchlist access — see docs/design-watchlist.md.

All reads/writes against `user_watchlist` live here so the REST layer and
the (future) agent tool share one gate. Symbols are stored as canonical
Yahoo symbols (the `instruments` PK); `add_symbol` normalizes input through
an alias-aware resolve step before insert.

Resolution runs SQL on the caller's connection (not the in-process
`src.instruments.resolver` cache) for two reasons: the cache keys rows by
exact symbol only — it cannot match `aliases_json` entries like "TSMC" →
TSM — and conn-based lookup keeps the module testable against a temp DB
without cache invalidation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(slots=True)
class WatchlistEntry:
    """One watchlist row joined with instrument display meta.

    One shape serves both the list read and the add return — the REST layer
    maps it straight onto its `WatchlistItem` response model.
    """

    symbol: str
    name: str
    short: str
    asset_class: str
    added_at: str


class UnknownSymbolError(ValueError):
    """Raised when an input symbol resolves to no active instrument."""

    def __init__(self, raw: str):
        self.raw = raw
        super().__init__(
            f"Unknown symbol: {raw!r} — not in the instrument registry. "
            "Check the ticker spelling."
        )


_ENTRY_SELECT = """
SELECT w.symbol, i.display, i.short, i.asset_class, w.added_at
FROM user_watchlist w
JOIN instruments i ON i.symbol = w.symbol
"""


def _row_to_entry(row: tuple) -> WatchlistEntry:
    return WatchlistEntry(
        symbol=row[0], name=row[1], short=row[2], asset_class=row[3], added_at=row[4]
    )


def resolve_symbol(symbol: str, conn: sqlite3.Connection) -> str | None:
    """Resolve raw input to a canonical Yahoo symbol, or None.

    Order: exact symbol match (case-insensitive), then alias match against
    `aliases_json` (canonical rows preferred, then alphabetical for
    determinism). Alias rows (`BTC`, `DXY`) are followed through
    `canonical_symbol` to the canonical row.
    """
    raw = symbol.strip()
    if not raw:
        return None
    upper = raw.upper()

    row = conn.execute(
        "SELECT symbol, canonical_symbol FROM instruments "
        "WHERE active = 1 AND UPPER(symbol) = ?",
        (upper,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT symbol, canonical_symbol FROM instruments
            WHERE active = 1 AND EXISTS (
                SELECT 1 FROM json_each(instruments.aliases_json)
                WHERE UPPER(json_each.value) = ?
            )
            ORDER BY (canonical_symbol IS NOT NULL), symbol
            LIMIT 1
            """,
            (upper,),
        ).fetchone()
    if row is None:
        return None

    canonical = row[1] or row[0]
    if canonical != row[0]:
        # Follow the alias row's pointer; the target must itself be active.
        target = conn.execute(
            "SELECT 1 FROM instruments WHERE active = 1 AND symbol = ?",
            (canonical,),
        ).fetchone()
        if target is None:
            return None
    return canonical


def list_watchlist(user_id: str, conn: sqlite3.Connection) -> list[WatchlistEntry]:
    rows = conn.execute(
        _ENTRY_SELECT + "WHERE w.user_id = ? ORDER BY w.added_at, w.symbol",
        (user_id,),
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


def add_symbol(user_id: str, symbol: str, conn: sqlite3.Connection) -> WatchlistEntry:
    """Resolve + insert. Idempotent: re-adding returns the existing entry.

    Raises UnknownSymbolError when the symbol resolves to no instrument.
    """
    canonical = resolve_symbol(symbol, conn)
    if canonical is None:
        raise UnknownSymbolError(symbol)
    conn.execute(
        "INSERT INTO user_watchlist (user_id, symbol) VALUES (?, ?) "
        "ON CONFLICT (user_id, symbol) DO NOTHING",
        (user_id, canonical),
    )
    conn.commit()
    row = conn.execute(
        _ENTRY_SELECT + "WHERE w.user_id = ? AND w.symbol = ?",
        (user_id, canonical),
    ).fetchone()
    return _row_to_entry(row)


def remove_symbol(user_id: str, symbol: str, conn: sqlite3.Connection) -> bool:
    """Delete the row; returns True if one was deleted.

    The symbol is resolved first so `DELETE /watchlist/btc` removes the
    stored `BTC-USD` row; unresolvable input falls back to the raw string
    (matches nothing → False).
    """
    canonical = resolve_symbol(symbol, conn) or symbol.strip().upper()
    cur = conn.execute(
        "DELETE FROM user_watchlist WHERE user_id = ? AND symbol = ?",
        (user_id, canonical),
    )
    conn.commit()
    return cur.rowcount > 0

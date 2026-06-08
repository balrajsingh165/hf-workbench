from __future__ import annotations

import sqlite3
from pathlib import Path


def load_entity_tickers(
    db_path: Path,
    entity_type: str,
    ids: set[str] | None = None,
) -> dict[str, set[str]]:
    """Map entity_id → set of tickers for one entity_type ('story' or 'thesis').

    If `ids` is provided, restrict the query to that subset. An empty `ids`
    set short-circuits to an empty dict.
    """
    if ids is not None and not ids:
        return {}
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        if ids is None:
            rows = conn.execute(
                "SELECT entity_id, symbol FROM entity_tickers WHERE entity_type = ?",
                (entity_type,),
            ).fetchall()
        else:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT entity_id, symbol FROM entity_tickers "
                f"WHERE entity_type = ? AND entity_id IN ({placeholders})",
                (entity_type, *ids),
            ).fetchall()
    finally:
        conn.close()
    out: dict[str, set[str]] = {}
    for entity_id, symbol in rows:
        out.setdefault(str(entity_id), set()).add(str(symbol))
    return out


def tickers_overlap(a: set[str], b: set[str]) -> bool:
    """Macro fallthrough: empty on either side counts as overlapping.

    A thesis or story with zero tagged tickers is treated as macro-relevant
    (Fed/CPI-style), so we don't drop it when the counterpart has tickers
    that would otherwise fail to intersect.
    """
    if not a or not b:
        return True
    return bool(a & b)


__all__ = ["load_entity_tickers", "tickers_overlap"]

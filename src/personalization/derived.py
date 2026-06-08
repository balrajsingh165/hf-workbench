"""Derive a profile view from ``user_theses ⨝ entity_tickers``.

Pure facts only. No labels, no scores, no aggregates. Per the design's
"no inferred classifications in the derived layer" rule.

Audit finding for v0 (2026-05-20): ``thesis_match_chunks.sectors_json`` is
empty across all rows. The doc proposes ``implicit_sectors`` from theses
but there is no upstream sector data attached to theses. We compute
``implicit_watchlist`` reliably; ``implicit_sectors`` stays empty until
the upstream is fixed. The renderer treats that as "no signal" and drops
the slot, which is the design-intended behavior.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass(slots=True)
class DerivedProfile:
    user_id: str
    implicit_watchlist: list[str] = field(default_factory=list)
    implicit_sectors: list[str] = field(default_factory=list)
    active_thesis_count: int = 0

    def is_empty(self) -> bool:
        return not (self.implicit_watchlist or self.implicit_sectors)


def derive_profile(user_id: str, conn: sqlite3.Connection) -> DerivedProfile:
    """One SELECT against an indexed join. No LLM, no writes."""

    cur = conn.execute(
        """
        SELECT DISTINCT et.symbol
        FROM user_theses ut
        JOIN entity_tickers et
          ON et.entity_id = ut.thesis_id
         AND et.entity_type = 'thesis'
        WHERE ut.user_id = ?
          AND ut.status = 'active'
        ORDER BY et.symbol
        """,
        (user_id,),
    )
    tickers = [row[0] for row in cur.fetchall()]

    cur = conn.execute(
        "SELECT COUNT(*) FROM user_theses WHERE user_id = ? AND status = 'active'",
        (user_id,),
    )
    count = cur.fetchone()[0] or 0

    return DerivedProfile(
        user_id=user_id,
        implicit_watchlist=tickers,
        implicit_sectors=[],
        active_thesis_count=int(count),
    )

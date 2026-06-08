"""Personalization read path + watchlist storage.

- ``load_stored_profile``  stored profile; watchlist from the DB, narrative
  slots from ``users/{id}/profile.md``.
- ``parse_profile_md``  reads ``users/{id}/profile.md`` into a ``StoredProfile``
  (markdown-only; no DB watchlist).
- ``derive_profile``    unions tickers from ``user_theses`` into a ``DerivedProfile``.
- ``render_user_profile_block``  renders the ``<user_profile>`` XML block, or
  returns ``None`` when there is too little signal to personalize.
- ``watchlist``  module: ``user_watchlist`` table reads/writes
  (see docs/design-watchlist.md).

The prompt caller decides (via the ``HF_PERSONALIZATION`` env flag) whether
to inject the rendered blocks.
"""

import sqlite3

from src.personalization.parser import StoredProfile, parse_profile_md
from src.personalization.derived import DerivedProfile, derive_profile
from src.personalization.render import render_user_holdings_block, render_user_profile_block
from src.personalization.watchlist import list_watchlist


def load_stored_profile(user_id: str, conn: sqlite3.Connection) -> StoredProfile:
    """Stored profile with the watchlist sourced from `user_watchlist`.

    The narrative slots (experience, risk, sectors, ...) still come from
    ``users/{id}/profile.md``; the watchlist moved to the DB (see
    docs/design-watchlist.md). Same ``StoredProfile`` shape — the render
    functions are unchanged.
    """
    stored = parse_profile_md(user_id)
    stored.watchlist = [e.symbol for e in list_watchlist(user_id, conn)]
    return stored


__all__ = [
    "StoredProfile",
    "DerivedProfile",
    "load_stored_profile",
    "parse_profile_md",
    "derive_profile",
    "render_user_holdings_block",
    "render_user_profile_block",
]

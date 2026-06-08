"""Render ``<user_profile>`` block. Returns ``None`` when too sparse to bother.

Single-shape renderer (per design self-review §3): emit whichever slots
have values; if the total useful slots is ≤1, omit the block entirely.
That sparse-profile fallback is the only conditional. No persona claims,
no aggregate labels — just plain facts.
"""

from __future__ import annotations

from src.personalization.derived import DerivedProfile
from src.personalization.parser import StoredProfile


_WATCHLIST_DISPLAY_CAP = 12
_IMPLICIT_DISPLAY_CAP = 12


def _format_list(items: list[str], cap: int) -> str:
    if not items:
        return ""
    head = items[:cap]
    suffix = "" if len(items) <= cap else f" (+{len(items) - cap} more)"
    return ", ".join(head) + suffix


def render_user_profile_block(
    stored: StoredProfile,
    derived: DerivedProfile,
    *,
    sparse_floor: int = 2,
) -> str | None:
    lines: list[str] = []
    populated = 0

    # Stored slots
    if stored.watchlist:
        lines.append(
            f"  watchlist: {_format_list(stored.watchlist, _WATCHLIST_DISPLAY_CAP)}"
        )
        populated += 1
    if stored.sectors_of_interest:
        lines.append(
            f"  sectors_of_interest: {', '.join(stored.sectors_of_interest)}"
        )
        populated += 1
    if stored.asset_classes:
        lines.append(f"  asset_classes: {', '.join(stored.asset_classes)}")
        populated += 1
    if stored.experience:
        lines.append(f"  experience: {stored.experience}")
        populated += 1
    if stored.risk_tolerance:
        lines.append(f"  risk_tolerance: {stored.risk_tolerance}")
        populated += 1
    if stored.excluded_strategies:
        lines.append(
            f"  excluded_strategies: {', '.join(stored.excluded_strategies)}"
        )
        populated += 1

    stored_block_present = bool(lines)
    if stored_block_present:
        lines.insert(0, "stored (from user-stated preferences):")

    # Derived slots — only the well-grounded one (tickers from tracked theses).
    derived_lines: list[str] = []
    # Filter out tickers the user already explicitly tracks.
    implicit_extra = [
        sym
        for sym in derived.implicit_watchlist
        if sym.upper() not in {s.upper() for s in stored.watchlist}
    ]
    if implicit_extra:
        derived_lines.append(
            f"  implicit_watchlist: {_format_list(implicit_extra, _IMPLICIT_DISPLAY_CAP)}"
        )
        populated += 1

    if derived_lines:
        if stored_block_present:
            lines.append("")
        lines.append(
            "observed (from your tracked theses — facts about your book, not stated preferences):"
        )
        lines.extend(derived_lines)

    # Sparse-profile fallback: omit the block when there is too little signal.
    # A single watchlist entry is not enough to personalize without the model
    # over-anchoring. The threshold is a guess; mark it as such.
    if populated < sparse_floor:
        return None

    body = "\n".join(lines)
    return f"<user_profile>\n{body}\n</user_profile>"


def render_user_holdings_block(
    stored: StoredProfile,
    derived: DerivedProfile,
) -> str | None:
    """Terse Phase-1 holdings hint. Returns ``None`` when there is nothing."""
    explicit = stored.watchlist or []
    explicit_upper = {s.upper() for s in explicit}
    implicit = [
        sym for sym in derived.implicit_watchlist
        if sym.upper() not in explicit_upper
    ]
    sectors = stored.sectors_of_interest or []

    if not explicit and not implicit and not sectors:
        return None

    parts: list[str] = []
    if explicit:
        parts.append(f"{_format_list(explicit, _WATCHLIST_DISPLAY_CAP)} (watchlist)")
    if implicit:
        parts.append(
            f"{_format_list(implicit, _IMPLICIT_DISPLAY_CAP)} (from tracked theses)"
        )
    body = "Tracked exposure: " + "; ".join(parts) if parts else ""
    if sectors:
        sector_str = ", ".join(sectors)
        body = f"{body}. Sectors: {sector_str}." if body else f"Sectors: {sector_str}."

    return f"<user_holdings>\n{body}\n</user_holdings>"

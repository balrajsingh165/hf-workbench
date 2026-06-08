"""Display-string formatters for tool outputs.

The agent reads tool JSON literally, so raw integers like ``2105000000.0`` force
it to mentally segment digits and frequently produce off-by-one perceptual
errors (e.g. claiming "$2.105B is below $2.1B"). Pre-formatted sibling fields
(``*_display``) give the model an unambiguous string to quote.

Formatters here are intentionally tiny and side-effect free so they're safe to
call from Pydantic ``model_validator``s.
"""

from __future__ import annotations

import math
from typing import Any


def format_usd_short(value: Any) -> str | None:
    """Render a USD amount as a compact human string.

    >>> format_usd_short(2_239_000_000)
    '$2.24B'
    >>> format_usd_short(2_105_000_000)
    '$2.10B'
    >>> format_usd_short(15_400_000)
    '$15.40M'
    >>> format_usd_short(-1_200_000_000)
    '-$1.20B'
    >>> format_usd_short(3.10)
    '$3.10'
    >>> format_usd_short(None) is None
    True
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    sign = "-" if v < 0 else ""
    n = abs(v)
    if n >= 1_000_000_000:
        return f"{sign}${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}${n / 1_000:.1f}K"
    return f"{sign}${n:.2f}"


def format_count_short(value: Any) -> str | None:
    """Same magnitude scaling as :func:`format_usd_short` without the ``$``.

    Used for share counts and other non-currency XBRL facts.

    >>> format_count_short(1_234_000_000)
    '1.23B'
    >>> format_count_short(None) is None
    True
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    sign = "-" if v < 0 else ""
    n = abs(v)
    if n >= 1_000_000_000:
        return f"{sign}{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}{n / 1_000:.1f}K"
    return f"{sign}{n:.2f}"


__all__ = ["format_usd_short", "format_count_short"]

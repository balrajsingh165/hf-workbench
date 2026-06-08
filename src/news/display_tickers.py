"""Display-side filter for news-card tickers.

Storage keeps every symbol the synthesizer extracted (incl. macros and OTC
ADRs) because backend joins — brief ranking, scoring, news↔thesis matching —
need them. The UI only wants the chips a retail user would actually click on.

Three filters apply:

1. Drop pure-macro symbols. Either by registry asset class
   (`equity_index`, `rate`, `vol`, `commodity`, `fx`) or by Yahoo syntax for
   uncurated symbols: `^...` (index), `...=F` (futures), `...=X` (FX).
   A card whose only chip is `^GSPC` or `CL=F` adds no information beyond
   the headline.
2. Drop OTC pink-sheet ADRs (5-letter all-caps ending in `F` foreign
   ordinary or `Y` sponsored ADR — DLAKF, NSRGY, SSNLF). NYSE-listed ADRs
   are typically 3–4 chars (BABA, DEO, ASML, TSM) and don't match. We drop
   even when curated; if the issuer has a primary US line it should already
   appear separately, and an empty card is preferable to surfacing an
   illiquid pink-sheet quote on a retail chip strip.
"""

from __future__ import annotations

import re

MACRO_ASSET_CLASSES: frozenset[str] = frozenset(
    {"equity_index", "rate", "vol", "commodity", "fx"}
)

_OTC_PINK_RE = re.compile(r"^[A-Z]{4}[FY]$")


def _is_otc_pink(symbol: str) -> bool:
    return bool(_OTC_PINK_RE.match(symbol))


def filter_for_display(
    symbols: list[str],
    *,
    asset_class_by_symbol: dict[str, str],
) -> list[str]:
    """Return the subset of `symbols` worth showing as chips on a news card.

    `asset_class_by_symbol` is the `instruments.asset_class` lookup for any
    symbol present in the registry. Uncurated symbols fall through to syntax
    heuristics for macro detection.
    """
    out: list[str] = []
    for sym in symbols:
        if _is_otc_pink(sym):
            continue
        cls = asset_class_by_symbol.get(sym)
        if cls is None:
            # Registry-unknown symbol. Either macro-by-syntax (^GSPC, CL=F)
            # or — much more often, post-firehose — a real exchange ticker
            # the registry hasn't adopted yet (BA, CCI, APD). Either way,
            # don't render it as a clickable chip; the pending_instruments
            # queue catches the second case for weekly registry adoption.
            continue
        if cls in MACRO_ASSET_CLASSES:
            continue
        out.append(sym)
    return out


__all__ = ["MACRO_ASSET_CLASSES", "filter_for_display"]

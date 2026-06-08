"""Readability / register lint for thesis titles and core_thesis.

Two separate checks:

- :func:`lint_banned_vocab` flags analyst-deck jargon and em-dashes anywhere
  in the thesis (title, core_thesis, or invalidations).
- :func:`lint_price_targets` flags specific price predictions in the title or
  core_thesis only. Invalidation conditions are EXEMPT because they need
  testable thresholds.

Both checks are deterministic. They exist so we can:

1. Reject system-generated theses at the discovery gate.
2. Drive the retroactive rewrite script (skip already-clean files).
3. Grade the eval harness output without an LLM judge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from src.thesis.docs import ThesisDocument, parse_thesis_markdown

# ---------------------------------------------------------------------------
# Banned vocabulary
# ---------------------------------------------------------------------------

BANNED_WORDS: list[str] = [
    "compress", "compresses", "compressed", "compressing", "compression",
    "re-rate", "re-rates", "re-rated", "re-rating",
    "repricing", "reprice", "repriced",
    "structural", "structurally",
    "valuations", "valuation",
    "outperformance", "underperformance",
    "divergence", "diverging",
    "spillover", "spillovers",
    "convergence", "converging",
    "elevated",
    "headwinds", "tailwinds",
    "secular",
    "multiples",
    "broadens", "broaden", "broadening",
    "unwind", "unwinding",
]


def lint_banned_vocab(text: str) -> list[str]:
    """Return banned tokens (plus 'em-dash' if present) found in *text*.

    Whole-word match (alphanumeric boundaries). Case-insensitive.
    """
    lower = text.lower()
    found: list[str] = []
    for word in BANNED_WORDS:
        idx = 0
        while True:
            pos = lower.find(word, idx)
            if pos == -1:
                break
            left = lower[pos - 1] if pos > 0 else ""
            right = lower[pos + len(word)] if pos + len(word) < len(lower) else ""
            if not left.isalnum() and not right.isalnum():
                found.append(word)
                break
            idx = pos + 1
    if "\u2014" in text:  # em-dash U+2014
        found.append("em-dash")
    return found


# ---------------------------------------------------------------------------
# Price-target rule (title + core_thesis only)
# ---------------------------------------------------------------------------

# Matches "$95", "$3,500", "$3.5K", "$1.2 trillion" etc. and bare-number
# price-target patterns like "10Y to 5.5%", "SPX above 7000", "Brent past 95".
_DOLLAR_RE = re.compile(
    r"\$\d[\d,\.]*(?:\s*(?:K|M|B|T|trillion|billion|million)\b)?",
    re.IGNORECASE,
)
_BARE_TARGET_PHRASES = [
    "above", "below", "past", "to", "toward", "towards",
    "reach", "reaches", "hits", "hit",
    "breaks", "break", "tops", "top",
]
_BARE_NUMBER_RE = re.compile(
    r"\b(?:" + "|".join(_BARE_TARGET_PHRASES) + r")\s+\$?\d[\d,\.]*\s*(?:%|bps|bp)?\b",
    re.IGNORECASE,
)


def lint_price_targets(text: str) -> list[str]:
    """Return price-target tokens detected in *text*.

    Designed for use on title + core_thesis only. Do NOT call this on
    invalidation conditions (those need numeric thresholds to be testable).

    Returns the matched substrings (e.g. ``["$3,500", "above 7000"]``).
    """
    hits: list[str] = []
    for m in _DOLLAR_RE.finditer(text):
        hits.append(m.group(0).strip())
    for m in _BARE_NUMBER_RE.finditer(text):
        hits.append(m.group(0).strip())
    # Dedupe preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# ---------------------------------------------------------------------------
# Document-level readability check
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReadabilityReport:
    thesis_id: str
    title: str
    banned_in_title: list[str]
    banned_in_core: list[str]
    banned_in_invalidations: list[str]
    price_targets_in_title: list[str]
    price_targets_in_core: list[str]

    @property
    def is_clean(self) -> bool:
        return not (
            self.banned_in_title
            or self.banned_in_core
            or self.banned_in_invalidations
            or self.price_targets_in_title
            or self.price_targets_in_core
        )

    @property
    def issue_count(self) -> int:
        return (
            len(self.banned_in_title)
            + len(self.banned_in_core)
            + len(self.banned_in_invalidations)
            + len(self.price_targets_in_title)
            + len(self.price_targets_in_core)
        )


def lint_thesis_document(doc: ThesisDocument) -> ReadabilityReport:
    return ReadabilityReport(
        thesis_id=doc.thesis_id,
        title=doc.title,
        banned_in_title=lint_banned_vocab(doc.title),
        banned_in_core=lint_banned_vocab(doc.core_thesis),
        banned_in_invalidations=lint_banned_vocab("\n".join(doc.invalidations)),
        price_targets_in_title=lint_price_targets(doc.title),
        price_targets_in_core=lint_price_targets(doc.core_thesis),
    )


def lint_thesis_file(path: Path) -> ReadabilityReport:
    return lint_thesis_document(parse_thesis_markdown(path))


__all__ = [
    "BANNED_WORDS",
    "ReadabilityReport",
    "lint_banned_vocab",
    "lint_price_targets",
    "lint_thesis_document",
    "lint_thesis_file",
]

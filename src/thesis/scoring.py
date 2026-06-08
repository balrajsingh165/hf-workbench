"""Pure scoring math + chip derivation for a thesis.

MVP scope: Freshness score + binary `Supports` / `Stresses` feed-card chip.
Tailwind and auto stress-flip land later (see docs/plan-scoring-system.md,
TODO.md → "Auto stress-flip (deferred)").

Everything here is IO-free so it can be spot-checked in a REPL without
hitting the DB.
"""

from __future__ import annotations

from datetime import date

from src.thesis.story_links import ThesisStoryLink


# Minimum judge confidence for a link to render a chip on a feed card.
# One knob gates both `Supports` and `Stresses`; an asymmetric floor can
# land once calibration data justifies it (see TODO.md).
SUPPORT_STRONG_CONF = 0.70


# Inferred-horizon guardrails. Horizon is the thesis's decay clock
# (half_life = horizon_days / 2); it is inferred at creation, never entered or
# seen by the user. We harvest whatever the discovery model proposes and clamp
# it into a sane band, defaulting when the model omits or garbles it.
HORIZON_MIN_DAYS = 10
HORIZON_MAX_DAYS = 120
HORIZON_DEFAULT_DAYS = 45


def clamp_horizon(raw: object) -> int:
    """Normalize an inferred horizon into [HORIZON_MIN_DAYS, HORIZON_MAX_DAYS].

    Falls back to HORIZON_DEFAULT_DAYS when `raw` is missing or not a usable
    positive number, so callers always get a scoreable horizon.
    """
    try:
        days = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return HORIZON_DEFAULT_DAYS
    if days <= 0:
        return HORIZON_DEFAULT_DAYS
    return max(HORIZON_MIN_DAYS, min(HORIZON_MAX_DAYS, days))


def _parse_support_date(link: ThesisStoryLink) -> date | None:
    """`story.created_at` only. Writers populate it from the freshest source
    publish time; never fall back to the link's `updated_at` (re-running
    matching would refresh scores)."""
    raw = link.story_created_at
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def compute_freshness(
    links: list[ThesisStoryLink],
    horizon_days: int,
    as_of: date,
) -> int:
    """Exponential decay (half-life = horizon_days / 2) from the most recent support.

    Anchors: fresh support → 100, support at half-horizon → ~50,
    at full horizon → ~25, no supports → 0.
    """
    if horizon_days <= 0:
        raise ValueError(f"horizon_days must be positive, got {horizon_days}")

    support_dates = [
        d
        for d in (_parse_support_date(l) for l in links if l.relation == "supports")
        if d is not None
    ]
    if not support_dates:
        return 0

    age_days = max(0, (as_of - max(support_dates)).days)
    half_life = horizon_days / 2
    return round(100 * 0.5 ** (age_days / half_life))


# Tailwind saturation band: ±TAILWIND_FULL_PCT percent over the window maps
# to the ends of the 0–100 scale. One month of a healthy single-name move
# lives roughly here; asymmetric / vol-normalized bands are follow-ups.
TAILWIND_FULL_PCT = 10.0


def compute_tailwind(
    ticker_directions: list[tuple[str, str]],
    returns: dict[str, float | None],
) -> int | None:
    """Mean per-ticker tailwind. `None` when no ticker has both a direction and a return.

    `ticker_directions` uses canonical Yahoo symbols (caller resolves). Each
    entry is `(symbol, 'bullish'|'bearish')`. `returns[symbol]` is the window
    percent change as reported by Mesh's `window_summary.open_close_change_pct`.
    Missing returns or unknown directions are silently skipped; an empty
    qualifying set returns `None` so the composite falls back to freshness.
    """
    per_ticker: list[int] = []
    for symbol, direction in ticker_directions:
        pct = returns.get(symbol)
        if pct is None:
            continue
        if direction == "bullish":
            signed = pct
        elif direction == "bearish":
            signed = -pct
        else:
            continue
        clamped = max(-1.0, min(1.0, signed / TAILWIND_FULL_PCT))
        per_ticker.append(round(50 + clamped * 50))
    if not per_ticker:
        return None
    return round(sum(per_ticker) / len(per_ticker))


def chip_for(link: ThesisStoryLink) -> str | None:
    """Feed-card chip for one (thesis, story) link.

    Returns "Supports", "Stresses", or None (no chip rendered).
    `unrelated` is filtered upstream and never lands in thesis_story_links.
    """
    if link.confidence < SUPPORT_STRONG_CONF:
        return None
    if link.relation == "supports":
        return "Supports"
    if link.relation == "stresses":
        return "Stresses"
    return None


# Strength bands per docs/plan-scoring-system.md: 0-34 Critical, 35-69 Active,
# 70-100 Strong. Used by `prescription_for` only; bands are otherwise derived
# in the frontend from the raw score.
STRONG_BAND_MIN = 70
CRITICAL_BAND_MAX = 34


def prescription_for(
    score: int | None,
    status: str,
    most_recent_relation: str | None,
) -> str | None:
    """One-line action verb for a thesis card. Pure, deterministic.

    Maps (band + status + most-recent signal relation) to the prescription
    strings the UX expects (`docs/plan-scoring-system.md` "The Whoop lesson").
    Stressed status overrides band downward. Resolved theses get no
    prescription — the surface that renders them is the resolution ceremony,
    not the home card.

    `most_recent_relation` is `"supports"`, `"stresses"`, or `None` (no
    signals on the thesis yet).
    """
    if status == "resolved":
        return None
    if status == "stressed" or score is None or score <= CRITICAL_BAND_MAX:
        return "Review or restate."
    if score >= STRONG_BAND_MIN:
        if most_recent_relation == "stresses":
            return "Holding, but one signal is challenging."
        return "Holding. Let it run."
    if most_recent_relation == "stresses":
        return "Watch. One invalidation is trending."
    return "Watch."


__all__ = [
    "HORIZON_DEFAULT_DAYS",
    "HORIZON_MAX_DAYS",
    "HORIZON_MIN_DAYS",
    "SUPPORT_STRONG_CONF",
    "TAILWIND_FULL_PCT",
    "chip_for",
    "clamp_horizon",
    "compute_freshness",
    "compute_tailwind",
    "prescription_for",
]

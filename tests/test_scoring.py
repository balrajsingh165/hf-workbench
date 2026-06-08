"""Unit tests for src/thesis/scoring.py. Pure functions, no IO."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from src.thesis.scoring import (  # noqa: E402
    CRITICAL_BAND_MAX,
    STRONG_BAND_MIN,
    SUPPORT_STRONG_CONF,
    TAILWIND_FULL_PCT,
    chip_for,
    compute_freshness,
    compute_tailwind,
    prescription_for,
)
from src.thesis.story_links import ThesisStoryLink  # noqa: E402


# ─── helpers ─────────────────────────────────────────────────────────────


def _link(
    *,
    relation: str = "supports",
    confidence: float = 0.9,
    story_created_at: str | None = "2026-05-13",
) -> ThesisStoryLink:
    return ThesisStoryLink(
        thesis_id="thesis_001",
        story_id="story_001",
        relation=relation,
        confidence=confidence,
        matched_invalidation=None,
        rationale="r",
        retrieval_score=0.5,
        best_chunk_key="chunk_0",
        source="ingest",
        story_created_at=story_created_at,
    )


# ─── compute_freshness ────────────────────────────────────────────────────


def test_freshness_no_supports_is_zero():
    assert compute_freshness([], horizon_days=28, as_of=date(2026, 5, 13)) == 0


def test_freshness_only_stresses_is_zero():
    # `stresses` rows must not anchor freshness — freshness decays only from
    # the latest `supports` story.
    links = [_link(relation="stresses", story_created_at="2026-05-13")]
    assert compute_freshness(links, horizon_days=28, as_of=date(2026, 5, 13)) == 0


def test_freshness_today_is_100():
    today = date(2026, 5, 13)
    links = [_link(story_created_at=today.isoformat())]
    assert compute_freshness(links, horizon_days=28, as_of=today) == 100


def test_freshness_half_horizon_is_50():
    # half-life = horizon_days / 2, so age = half-life → score ≈ 50
    today = date(2026, 5, 13)
    horizon = 28
    age = horizon // 2  # 14 days
    supp = (today - timedelta(days=age)).isoformat()
    assert compute_freshness([_link(story_created_at=supp)], horizon, today) == 50


def test_freshness_full_horizon_is_25():
    today = date(2026, 5, 13)
    horizon = 28
    supp = (today - timedelta(days=horizon)).isoformat()
    assert compute_freshness([_link(story_created_at=supp)], horizon, today) == 25


def test_freshness_uses_most_recent_support():
    # Multiple supports → only the freshest one anchors the decay.
    today = date(2026, 5, 13)
    links = [
        _link(story_created_at="2026-04-01"),
        _link(story_created_at=today.isoformat()),
        _link(story_created_at="2026-04-20"),
    ]
    assert compute_freshness(links, horizon_days=28, as_of=today) == 100


def test_freshness_ignores_unparseable_dates():
    today = date(2026, 5, 13)
    links = [
        _link(story_created_at="21 hours a"),  # firehose junk
        _link(story_created_at=None),
        _link(story_created_at=today.isoformat()),
    ]
    assert compute_freshness(links, horizon_days=28, as_of=today) == 100


def test_freshness_future_dated_support_is_clamped_to_today():
    # A support timestamped tomorrow should not decay below 100 (negative age
    # would multiply, not divide).
    today = date(2026, 5, 13)
    future = (today + timedelta(days=5)).isoformat()
    assert compute_freshness([_link(story_created_at=future)], 28, today) == 100


def test_freshness_rejects_nonpositive_horizon():
    with pytest.raises(ValueError):
        compute_freshness([_link()], horizon_days=0, as_of=date(2026, 5, 13))
    with pytest.raises(ValueError):
        compute_freshness([_link()], horizon_days=-7, as_of=date(2026, 5, 13))


# ─── compute_tailwind ─────────────────────────────────────────────────────


def test_tailwind_no_qualifying_tickers_returns_none():
    # Empty directions → None; lets the composite fall back to freshness.
    assert compute_tailwind([], {}) is None


def test_tailwind_all_returns_missing_is_none():
    # Direction present, but no price → still None (don't guess).
    assert compute_tailwind([("AAPL", "bullish")], {"AAPL": None}) is None


def test_tailwind_neutral_direction_skipped():
    # `neutral` is not a vote. With no other tickers, result is None.
    assert compute_tailwind([("AAPL", "neutral")], {"AAPL": 5.0}) is None


def test_tailwind_flat_return_is_50():
    assert compute_tailwind([("AAPL", "bullish")], {"AAPL": 0.0}) == 50


def test_tailwind_aligned_full_band_is_100():
    # +10% on a bullish bet hits the saturation band.
    pct = TAILWIND_FULL_PCT  # 10.0
    assert compute_tailwind([("AAPL", "bullish")], {"AAPL": pct}) == 100


def test_tailwind_opposed_full_band_is_0():
    # -10% on a bullish bet hits the bottom of the band.
    assert compute_tailwind([("AAPL", "bullish")], {"AAPL": -TAILWIND_FULL_PCT}) == 0


def test_tailwind_clamps_beyond_saturation_band():
    # A +30% move is not 3× a +10% move; both saturate at 100.
    assert compute_tailwind([("AAPL", "bullish")], {"AAPL": 30.0}) == 100
    assert compute_tailwind([("AAPL", "bullish")], {"AAPL": -42.0}) == 0


def test_tailwind_bearish_flips_sign():
    # Bearish direction inverts the return.
    assert compute_tailwind([("SPY", "bearish")], {"SPY": -5.0}) == 75
    assert compute_tailwind([("SPY", "bearish")], {"SPY": 5.0}) == 25


def test_tailwind_averages_qualifying_tickers():
    # Two tickers — one +100, one 0 → mean = 50.
    dirs = [("AAPL", "bullish"), ("TLT", "bearish")]
    returns = {"AAPL": TAILWIND_FULL_PCT, "TLT": TAILWIND_FULL_PCT}
    # AAPL: bullish +10% → 100. TLT: bearish +10% → 0. Mean = 50.
    assert compute_tailwind(dirs, returns) == 50


def test_tailwind_skips_missing_returns_in_mean():
    # Only the ticker with a return contributes; the other is silently skipped.
    dirs = [("AAPL", "bullish"), ("FAKE", "bullish")]
    returns = {"AAPL": 0.0, "FAKE": None}
    assert compute_tailwind(dirs, returns) == 50


# ─── chip_for ─────────────────────────────────────────────────────────────


def test_chip_supports_above_floor():
    assert chip_for(_link(relation="supports", confidence=0.9)) == "Supports"


def test_chip_stresses_above_floor():
    assert chip_for(_link(relation="stresses", confidence=0.9)) == "Stresses"


def test_chip_below_floor_suppressed():
    # Below SUPPORT_STRONG_CONF → no chip, even with a valid relation.
    low = SUPPORT_STRONG_CONF - 0.01
    assert chip_for(_link(relation="supports", confidence=low)) is None
    assert chip_for(_link(relation="stresses", confidence=low)) is None


def test_chip_at_exact_floor_renders():
    # >= floor renders a chip (inclusive boundary).
    assert chip_for(_link(relation="supports", confidence=SUPPORT_STRONG_CONF)) == "Supports"


def test_chip_unknown_relation_returns_none():
    # Defensive: judge enum could drift; non-supports/non-stresses → no chip.
    assert chip_for(_link(relation="unrelated", confidence=0.99)) is None


# ─── prescription_for ─────────────────────────────────────────────────────


def test_prescription_resolved_returns_none():
    # Resolved theses are surfaced by the resolution ceremony, not a card.
    assert prescription_for(82, "resolved", "supports") is None


def test_prescription_stressed_overrides_band():
    # status='stressed' beats any score band — review/restate.
    assert prescription_for(82, "stressed", "supports") == "Review or restate."


def test_prescription_critical_band():
    # 0–CRITICAL_BAND_MAX → review/restate even if status is active.
    assert prescription_for(CRITICAL_BAND_MAX, "active", "supports") == "Review or restate."
    assert prescription_for(0, "active", None) == "Review or restate."


def test_prescription_null_score_treated_as_critical():
    # Score never computed → treat conservatively as critical.
    assert prescription_for(None, "active", None) == "Review or restate."


def test_prescription_strong_band_clean():
    assert prescription_for(STRONG_BAND_MIN, "active", "supports") == "Holding. Let it run."
    assert prescription_for(95, "active", None) == "Holding. Let it run."


def test_prescription_strong_band_with_stressing_signal():
    # Strong score but the most recent signal is a stress → caveat the prescription.
    assert (
        prescription_for(85, "active", "stresses")
        == "Holding, but one signal is challenging."
    )


def test_prescription_active_band():
    # 35–69 → "Watch" family.
    assert prescription_for(50, "active", "supports") == "Watch."
    assert prescription_for(
        50, "active", "stresses"
    ) == "Watch. One invalidation is trending."


# ─── composite-fallback contract (masterplan 2.10) ────────────────────────


def test_compute_tailwind_none_lets_composite_fall_back_to_freshness():
    # The scoring agent treats `None` from compute_tailwind as a signal to
    # leave the existing tailwind in the DB alone and fall back the composite
    # to freshness-only. Lock in the contract at this layer: the only way the
    # downstream branch fires is `compute_tailwind(...) is None`.
    assert compute_tailwind([], {}) is None
    assert compute_tailwind([("AAPL", "neutral")], {"AAPL": 1.0}) is None
    assert compute_tailwind([("AAPL", "bullish")], {"AAPL": None}) is None

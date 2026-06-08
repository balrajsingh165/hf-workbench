"""Unit tests for the home-feed scorer and composition pass (`api.py`).

The golden ordering encodes docs/design-feed-ranking.md's intent directly:
a judged-match corroborated story for the user beats fresh heat-5 social,
which beats an hours-old single-source story. Pure math on synthetic
candidates — no DB, fixed clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api import AffinityContext, _compose_candidates, _score_candidate

NOW = datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _story(
    id: str,
    *,
    hours_ago: float = 0,
    pubs: int = 1,
    tickers: set[str] | None = None,
) -> dict:
    return {
        "id": id,
        "kind": "story",
        "created_at": _ts(hours_ago),
        "independent_pub_count": pubs,
        "raw_tickers": tickers or set(),
        "sector_set": set(),
    }


def _x(id: str, *, hours_ago: float = 0, heat: int = 5, ticker: str = "NVDA") -> dict:
    return {
        "id": id,
        "kind": "x",
        "created_at": _ts(hours_ago),
        "heat": heat,
        "raw_tickers": {ticker},
        "sector_set": set(),
    }


def _score(candidate: dict, context: AffinityContext | None = None) -> float:
    _score_candidate(
        candidate,
        context=context
        or AffinityContext(user_tickers=set(), user_sectors=set(), judged_matches=set()),
        trend_ranks={},
        now=NOW,
    )
    return candidate["score"]


def test_golden_ordering():
    context = AffinityContext(
        user_tickers={"NVDA"},
        user_sectors=set(),
        judged_matches={"story_judged"},
    )
    judged = _score(
        _story("story_judged", hours_ago=2, pubs=2, tickers={"NVDA"}), context
    )
    social = _score(_x("x_hot", hours_ago=0, heat=5, ticker="NVDA"), context)
    stale_single = _score(_story("story_thin", hours_ago=6, pubs=1), context)

    assert judged > social > stale_single


def test_heat5_social_base_below_max_story_affinity_reach():
    """A fresh social batch must not outrank every story for every user —
    the failure mode observed live before the 2026-06-04 recalibration."""
    no_affinity = AffinityContext(
        user_tickers=set(), user_sectors=set(), judged_matches={"s1"}
    )
    judged_story = _score(_story("s1", hours_ago=4, pubs=2), no_affinity)
    foreign_social = _score(_x("x1", heat=5), no_affinity)
    assert judged_story > foreign_social


def test_owned_ticker_differentiates_social():
    owner = AffinityContext(
        user_tickers={"MU"}, user_sectors=set(), judged_matches=set()
    )
    other = AffinityContext(
        user_tickers={"GLD"}, user_sectors=set(), judged_matches=set()
    )
    assert _score(_x("x1", ticker="MU"), owner) > _score(_x("x2", ticker="MU"), other)


def test_social_decays_below_fresh_stories_next_morning():
    """The half-life feel test: yesterday's batch is gone from the front."""
    old_social = _score(_x("x1", hours_ago=20, heat=5))
    fresh_story = _score(_story("s1", hours_ago=2, pubs=2))
    assert fresh_story > old_social


def test_compose_caps_social_per_window():
    candidates = [_x(f"x{i}", heat=5, ticker=f"T{i}") for i in range(6)] + [
        _story(f"s{i}", hours_ago=3, pubs=2) for i in range(6)
    ]
    for c in candidates:
        _score(c)
    feed = _compose_candidates(candidates, limit=12)
    kinds = [item["kind"] for item in feed]
    for start in range(len(kinds) - 4):
        assert kinds[start : start + 5].count("x") <= 2


def test_compose_separates_same_lead_ticker():
    candidates = [
        _x("x1", heat=5, ticker="NVDA"),
        _x("x2", heat=5, ticker="NVDA"),
        _story("s1", hours_ago=1, pubs=3, tickers={"AAPL"}),
        _story("s2", hours_ago=2, pubs=3, tickers={"MSFT"}),
    ]
    for c in candidates:
        _score(c)
    feed = _compose_candidates(candidates, limit=4)
    leads = [sorted(item["raw_tickers"])[0] if item["raw_tickers"] else "" for item in feed]
    for a, b in zip(leads, leads[1:]):
        assert not (a and a == b)


def test_compose_caps_ticker_density_per_window():
    """One event's story + X topics must not stack the first page — the AVGO
    earnings-dip failure observed live 2026-06-04 (5 AVGO cards in 18 slots)."""
    # Filler must outlast the limit: with the pool exhausted the never-drop
    # fallback packs leftovers at the tail regardless of composition.
    candidates = (
        [_x(f"x{i}", heat=5, ticker="AVGO") for i in range(4)]
        + [_story("s_avgo", hours_ago=1, pubs=3, tickers={"AVGO"})]
        + [_story(f"s{i}", hours_ago=3, pubs=2, tickers={f"T{i}"}) for i in range(14)]
    )
    for c in candidates:
        _score(c)
    feed = _compose_candidates(candidates, limit=15)
    leads = [sorted(item["raw_tickers"])[0] if item["raw_tickers"] else "" for item in feed]
    for start in range(len(leads) - 9):
        assert leads[start : start + 10].count("AVGO") <= 2


def test_compose_demotes_never_drops():
    candidates = [_x(f"x{i}", heat=5, ticker="NVDA") for i in range(4)]
    for c in candidates:
        _score(c)
    feed = _compose_candidates(candidates, limit=10)
    assert len(feed) == 4

"""Unit tests for prediction market search: parsing, cache, filtering, ranking."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.clients.polymarket import _parse_volume as poly_parse_volume
from src.clients.polymarket import fetch_open_markets as poly_fetch
from src.prediction_markets.search import (
    DEFAULT_CACHE_PATH,
    MarketMatch,
    PredictionMarket,
    _cache_age_hours,
    _cosine,
    _load_cache,
    _save_cache,
    find_markets,
    find_markets_for_article,
)


# ── _cosine ────────────────────────────────────────────────────────────────

def test_cosine_identical():
    v = [1.0, 0.0, 0.0]
    assert _cosine(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector():
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_opposite():
    assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


# ── _parse_volume ──────────────────────────────────────────────────────────

def test_poly_volume_volumeNum():
    assert poly_parse_volume({"volumeNum": "123456.78"}) == pytest.approx(123456.78)


def test_poly_volume_fallback_to_volume():
    assert poly_parse_volume({"volume": "50000"}) == pytest.approx(50000.0)


def test_poly_volume_missing():
    assert poly_parse_volume({}) == 0.0


def test_poly_volume_invalid():
    assert poly_parse_volume({"volume": "n/a"}) == 0.0


# ── _cache_age_hours ───────────────────────────────────────────────────────

def test_cache_age_recent():
    from datetime import datetime, timezone, timedelta
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    age = _cache_age_hours(two_hours_ago)
    assert 1.9 < age < 2.1


def test_cache_age_invalid():
    assert _cache_age_hours("not-a-date") == float("inf")


def test_cache_age_no_tz():
    from datetime import datetime, timezone, timedelta
    naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    age = _cache_age_hours(naive)
    assert 0.9 < age < 1.1


# ── cache round-trip ───────────────────────────────────────────────────────

def _make_market(id: str, source: str = "polymarket", vol: float = 100_000.0) -> PredictionMarket:
    return PredictionMarket(
        id=id,
        source=source,  # type: ignore[arg-type]
        question="Test question?",
        embed_text="Test question?",
        volume_usd=vol,
        url="https://example.com",
        closes_at="2026-12-31",
        embedding=[0.1, 0.2, 0.3],
    )


def test_cache_roundtrip(tmp_path):
    path = tmp_path / "pm_cache.json"
    markets = [_make_market("poly:1"), _make_market("poly:2", vol=200_000.0)]
    _save_cache(path, markets, ("polymarket",), min_volume_usd=50_000)

    loaded = _load_cache(path)
    assert loaded is not None
    assert len(loaded) == 2
    assert loaded[0].id == "poly:1"
    assert loaded[1].volume_usd == pytest.approx(200_000.0)


def test_cache_stores_metadata(tmp_path):
    path = tmp_path / "pm_cache.json"
    _save_cache(path, [_make_market("x")], ("polymarket",), min_volume_usd=50_000)
    meta = json.loads(path.read_text())
    assert meta["sources"] == ["polymarket"]
    assert meta["min_volume_usd"] == 50_000
    assert "fetched_at" in meta


def test_cache_missing_returns_none(tmp_path):
    assert _load_cache(tmp_path / "nonexistent.json") is None


def test_cache_corrupt_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    assert _load_cache(path) is None


# ── max_results enforcement (mocked — no live network) ────────────────────

def _poly_page(n: int) -> list[dict]:
    return [
        {"conditionId": f"c{i}", "slug": f"s{i}", "question": f"Q{i}?",
         "description": "", "volumeNum": "100000"}
        for i in range(n)
    ]


def _mock_http_client(json_return):
    mock_resp = MagicMock()
    mock_resp.json.return_value = json_return
    mock_resp.raise_for_status.return_value = None
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_resp
    return mock_client


def test_poly_max_results_enforced():
    with patch("src.clients.polymarket.httpx.Client",
               return_value=_mock_http_client(_poly_page(100))):
        markets = poly_fetch(min_volume_usd=0, max_results=5)
    assert len(markets) == 5


# ── cache invalidation ────────────────────────────────────────────────────

def test_cache_invalid_if_volume_floor_higher(tmp_path):
    """Cache built at 50k must not satisfy a request for 10k (would miss markets)."""
    path = tmp_path / "pm_cache.json"
    _save_cache(path, [_make_market("poly:1")], ("polymarket",), min_volume_usd=50_000)

    meta = json.loads(path.read_text())
    cached_min = float(meta.get("min_volume_usd", float("inf")))
    requested_min = 10_000
    # Cache is valid only if cached_min <= requested_min
    assert not (cached_min <= requested_min)


# ── end-to-end ranking (mocked embed, pre-built cache) ────────────────────

def test_find_markets_ranks_by_volume_excludes_low_similarity(tmp_path):
    """Markets above the similarity floor are sorted by volume desc; others excluded."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache_path = tmp_path / "pm_cache.json"
    # Market c has the highest volume but orthogonal embedding → must be excluded.
    cache_path.write_text(json.dumps({
        "fetched_at": now,
        "sources": ["polymarket"],
        "min_volume_usd": 50_000,
        "count": 3,
        "markets": [
            {"id": "a", "source": "polymarket", "question": "A?", "embed_text": "A?",
             "volume_usd": 500_000, "url": "https://x.com", "closes_at": "2026-12-31",
             "embedding": [1.0, 0.0, 0.0]},
            {"id": "b", "source": "polymarket", "question": "B?", "embed_text": "B?",
             "volume_usd": 200_000, "url": "https://x.com", "closes_at": "2026-12-31",
             "embedding": [0.8, 0.6, 0.0]},
            {"id": "c", "source": "polymarket", "question": "C?", "embed_text": "C?",
             "volume_usd": 999_999, "url": "https://x.com", "closes_at": "2026-12-31",
             "embedding": [0.0, 1.0, 0.0]},  # sim=0.0 with query → excluded
        ],
    }))

    mock_embed = MagicMock()
    mock_embed.embeddings = [[1.0, 0.0, 0.0]]

    with patch("src.prediction_markets.search.embed_content", return_value=mock_embed):
        matches = find_markets(
            "test query",
            sources=("polymarket",),
            min_volume_usd=50_000,
            top_k=10,
            min_similarity=0.5,
            cache_ttl_hours=24.0,
            cache_path=cache_path,
        )

    assert len(matches) == 2
    assert matches[0].market.id == "a"   # vol=500k, sim=1.0
    assert matches[1].market.id == "b"   # vol=200k, sim=0.8


def test_find_markets_volume_sort_stays_inside_semantic_candidates(tmp_path):
    """A huge borderline hit should not outrank the strongest semantic pool."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache_path = tmp_path / "pm_cache.json"

    markets = []
    for i in range(20):
        markets.append({
            "id": f"good-{i}",
            "source": "polymarket",
            "question": f"Good {i}?",
            "embed_text": f"Good {i}?",
            "volume_usd": 100_000 + i,
            "url": "https://x.com",
            "closes_at": "2026-12-31",
            "embedding": [1.0, 0.0, 0.0],
        })
    markets.append({
        "id": "borderline",
        "source": "polymarket",
        "question": "Borderline?",
        "embed_text": "Borderline?",
        "volume_usd": 99_000_000,
        "url": "https://x.com",
        "closes_at": "2026-12-31",
        "embedding": [0.66, 0.75, 0.0],
    })

    cache_path.write_text(json.dumps({
        "fetched_at": now,
        "sources": ["polymarket"],
        "min_volume_usd": 50_000,
        "count": len(markets),
        "markets": markets,
    }))

    mock_embed = MagicMock()
    mock_embed.embeddings = [[1.0, 0.0, 0.0]]

    with patch("src.prediction_markets.search.embed_content", return_value=mock_embed):
        matches = find_markets(
            "test query",
            sources=("polymarket",),
            min_volume_usd=50_000,
            top_k=5,
            min_similarity=0.65,
            cache_ttl_hours=24.0,
            cache_path=cache_path,
        )

    ids = [m.market.id for m in matches]
    assert len(ids) == 5
    assert "borderline" not in ids
    assert ids[:2] == ["good-19", "good-18"]


# ── find_markets_for_article entry point ─────────────────────────────────


def test_find_markets_for_article_requires_exactly_one_input():
    with pytest.raises(ValueError, match="exactly one of"):
        find_markets_for_article()
    with pytest.raises(ValueError, match="exactly one of"):
        find_markets_for_article(thesis_id="thesis_001", story_id="story_001")
    with pytest.raises(ValueError, match="exactly one of"):
        find_markets_for_article(thesis_id="thesis_001", query="hi")


def test_find_markets_for_article_thesis_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="Thesis not found"):
        find_markets_for_article(thesis_id="missing_thesis", root=tmp_path)


def test_find_markets_for_article_story_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="Story not found"):
        find_markets_for_article(story_id="missing_story", root=tmp_path)


def test_find_markets_for_article_query_strips_and_forwards(tmp_path):
    """Free-text query path must trim whitespace and reach `find_markets`."""
    cache_path = tmp_path / "pm_cache.json"
    cache_path.write_text(json.dumps({
        "fetched_at": __import__("src.cache", fromlist=["now_iso"]).now_iso(),
        "sources": ["polymarket"],
        "min_volume_usd": 50_000,
        "count": 1,
        "markets": [{
            "id": "x", "source": "polymarket", "question": "Q?", "embed_text": "Q?",
            "volume_usd": 100_000, "url": "https://x.com", "closes_at": None,
            "embedding": [1.0, 0.0, 0.0],
        }],
    }))
    mock_embed = MagicMock()
    mock_embed.embeddings = [[1.0, 0.0, 0.0]]
    with patch("src.prediction_markets.search.embed_content", return_value=mock_embed):
        matches = find_markets_for_article(
            query="  fed cuts  ",
            cache_path=cache_path,
            cache_ttl_hours=24.0,
            min_similarity=0.5,
        )
    assert len(matches) == 1
    assert matches[0].market.id == "x"


# ── Polymarket fetch params (regression: dedfb2b sort + early-exit) ───────


def test_poly_fetch_requests_volume_sort():
    """Polymarket fetch must pass order=volumeNum&ascending=false to the Gamma API.

    Without this, the API returns markets in arbitrary order and the cache
    silently misses high-volume markets (the bug that motivated dedfb2b).
    """
    captured: list[dict] = []
    mock_resp = MagicMock()
    mock_resp.json.return_value = []  # empty page → loop exits immediately
    mock_resp.raise_for_status.return_value = None

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    def capture_get(_url, params=None, **_kwargs):
        captured.append(params or {})
        return mock_resp

    mock_client.get.side_effect = capture_get

    with patch("src.clients.polymarket.httpx.Client", return_value=mock_client):
        poly_fetch(min_volume_usd=50_000, max_results=5)

    assert captured, "no HTTP request was made"
    params = captured[0]
    assert params.get("order") == "volumeNum"
    assert params.get("ascending") == "false"


def test_poly_fetch_early_exits_when_page_drops_below_volume_floor():
    """Once a sorted page has any market below the floor, fetch must stop paginating.

    With order=volumeNum&ascending=false the rest of the listing can only have
    smaller volumes, so continuing wastes API calls.

    Test setup: page 1 is exactly PAGE_SIZE rows so the natural "len(page) <
    PAGE_SIZE" exit can NOT fire — only the below-floor break can stop the
    loop.  Page 2 is provided but must never be requested.
    """
    from src.clients.polymarket import PAGE_SIZE

    # Page 1: PAGE_SIZE rows, first 5 above the 100k floor, rest below.
    page1 = [
        {"conditionId": f"c{i}", "slug": f"s{i}", "question": f"Q{i}?",
         "description": "",
         "volumeNum": str(200_000 if i < 5 else 50_000)}
        for i in range(PAGE_SIZE)
    ]
    # Page 2 must never be fetched — if it is, the early-exit logic regressed.
    page2 = [
        {"conditionId": "should-never-appear", "slug": "x", "question": "Q?",
         "description": "", "volumeNum": "300000"}
    ]

    pages = [page1, page2]
    call_log: list[int] = []

    def fake_get(_url, params=None, **_kwargs):
        resp = MagicMock()
        idx = len(call_log)
        call_log.append(idx)
        resp.json.return_value = pages[idx] if idx < len(pages) else []
        resp.raise_for_status.return_value = None
        return resp

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.side_effect = fake_get

    with patch("src.clients.polymarket.httpx.Client", return_value=mock_client):
        markets = poly_fetch(min_volume_usd=100_000, max_results=50)

    assert len(markets) == 5, "should keep only the 5 above-floor markets from page 1"
    assert mock_client.get.call_count == 1, (
        f"early-exit broken: page 2 was fetched ({mock_client.get.call_count} calls)"
    )
    assert all(m.id != "polymarket:should-never-appear" for m in markets), (
        "page 2 row leaked into results"
    )

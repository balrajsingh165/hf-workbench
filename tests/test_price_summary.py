"""Unit tests for extended price_summary quote card (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.prices import quote_summary as qs  # noqa: E402


MESH_QUOTE = {
    "price": {
        "last_price": 222.32,
        "open": 229.82,
        "previous_close": 224.41,
        "day_high": 230.0,
        "day_low": 218.37,
        "volume": 144_108_922,
    },
    "stats": {
        "market_cap": 5_384_707_345_418.0,
        "year_high": 236.54,
        "year_low": 129.16,
    },
}


def test_merge_prefers_eodhd_session_over_mesh():
    card = qs.merge_quote_card(
        "NVDA",
        eodhd_session={
            "last_price": 231.0,
            "open": 230.0,
            "previous_close": 225.0,
            "day_high": 232.0,
            "day_low": 219.0,
            "volume": 150_000_000,
            "change_pct_1d": 2.67,
            "source": "eodhd",
        },
        mesh_quote=MESH_QUOTE,
        valuation={"eps_ttm": 4.08, "pe_ttm": 54.49, "forward_pe": 40.0},
    )
    assert card["latest_close"] == 231.0
    assert card["open"] == 230.0
    assert card["volume"] == 150_000_000
    assert card["year_high"] == 236.54
    assert card["market_cap"] == 5_384_707_345_418.0
    assert card["eps_ttm"] == 4.08
    assert card["pe_ttm"] == 54.49
    assert card["quote_source"] == "eodhd"


def test_merge_derives_pe_when_yahoo_omits_trailing_pe():
    card = qs.merge_quote_card(
        "NVDA",
        eodhd_session=None,
        mesh_quote=MESH_QUOTE,
        valuation={"eps_ttm": 4.0, "pe_ttm": None, "forward_pe": None},
    )
    assert card["pe_ttm"] == pytest.approx(55.58, rel=0.01)


def test_build_quote_card_monkeypatched(monkeypatch):
    monkeypatch.setattr(qs, "fetch_eodhd_session", lambda _t: None)
    monkeypatch.setattr(qs, "fetch_mesh_quote", lambda _t: MESH_QUOTE)
    monkeypatch.setattr(
        qs,
        "fetch_yahoo_valuation",
        lambda _t: {"eps_ttm": 4.08, "pe_ttm": 54.0, "forward_pe": 38.0},
    )
    card = qs.build_quote_card("nvda")
    assert card["ticker"] == "NVDA"
    assert card["latest_close"] == 222.32
    assert card["pe_ttm"] == 54.0


def test_price_summary_model_display():
    from app import PriceSummary

    row = PriceSummary(
        market_cap=5_440_000_000_000.0,
        asof="2026-05-19T00:00:00+00:00",
    )
    assert row.market_cap_display == "$5440.00B"


def test_fetch_valuation_falls_back_to_fundamentals(monkeypatch):
    monkeypatch.setattr(qs, "fetch_yahoo_valuation", lambda _t: {})

    class _Inc:
        diluted_eps = 4.9

    class _Fund:
        note = None
        income_latest_annual = _Inc()
        income_latest_quarterly = None

    monkeypatch.setattr("app.get_fundamentals", lambda _t: _Fund())
    out = qs.fetch_valuation("NVDA", 222.0)
    assert out["eps_ttm"] == 4.9
    assert out["pe_ttm"] == pytest.approx(45.31, rel=0.01)
    assert "valuation_note" in out

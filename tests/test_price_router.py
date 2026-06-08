"""Unit tests for the price router. No network."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients import alpaca, prices  # noqa: E402
from src.clients.prices import (  # noqa: E402
    Quote,
    WindowReturn,
    _route,
    _split,
    canonicalize,
    quote_snapshot,
    window_return_pcts,
    window_returns,
)


def test_route_alpaca_eligible():
    assert _route("AAPL") == "alpaca"
    assert _route("SPY") == "alpaca"
    assert _route("TLT") == "alpaca"
    assert _route("FANUY") == "alpaca"      # ADR — US-listed


def test_route_mesh_classes():
    assert _route("BTC-USD") == "mesh"      # crypto stays on Mesh by design
    assert _route("^TNX") == "mesh"
    assert _route("^VIX") == "mesh"
    assert _route("JPY=X") == "mesh"
    assert _route("BZ=F") == "mesh"


def test_route_foreign_listings():
    assert _route("005930.KS") == "mesh"
    assert _route("RHM.DE") == "mesh"
    assert _route("BA.L") == "mesh"
    assert _route("9684.T") == "mesh"


def test_route_unknown_symbol_falls_back_to_mesh():
    assert _route("UNKNOWN_XYZ") == "mesh"


def test_split_preserves_order_and_partition():
    a, m = _split(["AAPL", "BZ=F", "SPY", "^VIX", "005930.KS"])
    assert a == ["AAPL", "SPY"]
    assert m == ["BZ=F", "^VIX", "005930.KS"]


def test_split_dedupes_via_caller():
    # The router itself dedupes; _split assumes caller already did. Confirm
    # by passing duplicates and seeing them preserved (caller's job to dedupe).
    a, m = _split(["AAPL", "AAPL"])
    assert a == ["AAPL", "AAPL"]
    assert m == []


def test_canonicalize_alias_rows():
    assert canonicalize("USDJPY") == "JPY=X"
    assert canonicalize("BTC") == "BTC-USD"
    assert canonicalize("DXY") == "DX-Y.NYB"
    assert canonicalize("aapl") == "AAPL"   # canonical row passes through, uppercased
    assert canonicalize("") == ""
    assert canonicalize("   ") == ""


def test_canonicalize_unknown_passes_through_uppercased():
    assert canonicalize("zzzz_unknown") == "ZZZZ_UNKNOWN"


def test_quote_snapshot_routes_and_merges(monkeypatch):
    """Stub both providers; verify the router calls each with the right
    subset, normalizes shapes, and stamps Quote.source correctly."""
    alpaca_calls: list[list[str]] = []
    mesh_calls: list[list[str]] = []

    def fake_alpaca(symbols, **_):
        alpaca_calls.append(list(symbols))
        return {"AAPL": (190.0, 1.5), "SPY": (None, None)}

    def fake_mesh(symbols):
        mesh_calls.append(list(symbols))
        return {"BZ=F": (75.0, -0.4), "^VIX": (None, None)}

    monkeypatch.setattr(alpaca, "quote_snapshot", fake_alpaca)
    monkeypatch.setattr(prices, "_mesh_quote_snapshot", fake_mesh)

    out = quote_snapshot(["AAPL", "BZ=F", "SPY", "^VIX"])

    assert alpaca_calls == [["AAPL", "SPY"]]
    assert mesh_calls == [["BZ=F", "^VIX"]]
    assert out["AAPL"] == Quote("AAPL", 190.0, 1.5, "alpaca")
    assert out["SPY"] == Quote("SPY", None, None, "alpaca")
    assert out["BZ=F"] == Quote("BZ=F", 75.0, -0.4, "mesh")
    assert out["^VIX"] == Quote("^VIX", None, None, "mesh")


def test_window_returns_routes_and_merges(monkeypatch):
    def fake_alpaca(symbols, *, days):
        assert days == 31    # _period_to_days('1mo') == 31
        return {s: 2.5 for s in symbols}

    def fake_mesh(symbols, *, period):
        assert period == "1mo"
        return {s.upper(): -1.0 for s in symbols}

    monkeypatch.setattr(alpaca, "window_returns", fake_alpaca)
    monkeypatch.setattr(prices, "_mesh_window_returns", fake_mesh)

    out = window_returns(["AAPL", "BZ=F"])

    assert out["AAPL"] == WindowReturn("AAPL", 2.5, "alpaca")
    assert out["BZ=F"] == WindowReturn("BZ=F", -1.0, "mesh")


def test_window_return_pcts_drops_source(monkeypatch):
    monkeypatch.setattr(
        alpaca, "window_returns", lambda symbols, *, days: {s: 1.0 for s in symbols}
    )
    monkeypatch.setattr(
        prices, "_mesh_window_returns", lambda symbols, *, period: {}
    )
    out = window_return_pcts(["AAPL"])
    assert out == {"AAPL": 1.0}


def test_alpaca_error_resolves_to_none_quotes(monkeypatch):
    """A failing Alpaca call must not bubble up — Quote with None values."""

    def boom(symbols, **_):
        raise alpaca.AlpacaApiError("simulated 503")

    monkeypatch.setattr(alpaca, "quote_snapshot", boom)
    monkeypatch.setattr(prices, "_mesh_quote_snapshot", lambda syms: {})

    out = quote_snapshot(["AAPL"])
    assert out["AAPL"] == Quote("AAPL", None, None, "alpaca")


def test_empty_input_is_empty_output():
    assert quote_snapshot([]) == {}
    assert window_returns([]) == {}
    assert window_return_pcts([]) == {}


def test_period_to_days_known_and_unknown():
    from src.clients.prices import _period_to_days
    assert _period_to_days("1mo") == 31
    assert _period_to_days("5d") == 7
    assert _period_to_days("garbage") == 31  # default

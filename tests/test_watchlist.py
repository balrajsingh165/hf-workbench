"""Watchlist storage + API tests — docs/design-watchlist.md §Testing. No network."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.schema import init_db  # noqa: E402
from src.personalization import load_stored_profile  # noqa: E402
from src.personalization.watchlist import (  # noqa: E402
    UnknownSymbolError,
    add_symbol,
    list_watchlist,
    remove_symbol,
    resolve_symbol,
)

_INSTRUMENTS = [
    # (symbol, display, short, asset_class, aliases_json, canonical_symbol)
    ("NVDA", "NVIDIA Corp", "Nvidia", "equity", '["NVDA", "Nvidia"]', None),
    ("AAPL", "Apple Inc", "Apple", "equity", '["AAPL", "Apple"]', None),
    ("TSM", "Taiwan Semiconductor", "TSMC", "equity",
     '["TSMC", "Taiwan Semiconductor", "TSM"]', None),
    ("BTC-USD", "Bitcoin", "BTC", "crypto", '["BTC", "Bitcoin"]', None),
    # Alias row pointing at the canonical crypto row.
    ("BTC", "Bitcoin", "BTC", "crypto", '["BTC", "Bitcoin"]', "BTC-USD"),
]


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "hf_test.db"
    # Full schema: init_db always (re)creates INDEXES, which reference tables
    # outside the three we use.
    init_db(db_path=str(db_path))
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO users (id, display_name) VALUES ('user_t', 'Test User')")
    c.executemany(
        "INSERT INTO instruments (symbol, display, short, asset_class, aliases_json,"
        " canonical_symbol) VALUES (?, ?, ?, ?, ?, ?)",
        _INSTRUMENTS,
    )
    c.commit()
    yield c
    c.close()


# ── Unit: resolve gate ───────────────────────────────────────────────


def test_resolve_exact_and_case_insensitive(conn):
    assert resolve_symbol("NVDA", conn) == "NVDA"
    assert resolve_symbol("nvda", conn) == "NVDA"


def test_resolve_alias_to_canonical(conn):
    assert resolve_symbol("tsmc", conn) == "TSM"          # aliases_json match
    assert resolve_symbol("BTC", conn) == "BTC-USD"       # alias row → canonical
    assert resolve_symbol("btc", conn) == "BTC-USD"


def test_resolve_unknown_and_empty(conn):
    assert resolve_symbol("ZZZJUNK", conn) is None
    assert resolve_symbol("   ", conn) is None


# ── Unit: add / list / remove ────────────────────────────────────────


def test_add_normalizes_and_lists_in_added_order(conn):
    add_symbol("user_t", "tsmc", conn)
    add_symbol("user_t", "btc", conn)
    entries = list_watchlist("user_t", conn)
    # Same added_at second → symbol tiebreak (BTC-USD < TSM).
    assert [e.symbol for e in entries] == ["BTC-USD", "TSM"]
    assert entries[1].name == "Taiwan Semiconductor"
    assert entries[1].asset_class == "equity"
    assert entries[0].added_at


def test_add_is_idempotent(conn):
    first = add_symbol("user_t", "NVDA", conn)
    second = add_symbol("user_t", "nvda", conn)
    assert first == second
    assert len(list_watchlist("user_t", conn)) == 1


def test_add_unknown_symbol_raises(conn):
    with pytest.raises(UnknownSymbolError):
        add_symbol("user_t", "ZZZJUNK", conn)
    assert list_watchlist("user_t", conn) == []


def test_remove_resolves_alias_and_reports_absence(conn):
    add_symbol("user_t", "BTC", conn)
    assert remove_symbol("user_t", "btc", conn) is True   # stored as BTC-USD
    assert remove_symbol("user_t", "btc", conn) is False  # already gone
    assert remove_symbol("user_t", "ZZZJUNK", conn) is False


# ── Agent read path ──────────────────────────────────────────────────


def test_load_stored_profile_reads_watchlist_from_table(conn):
    add_symbol("user_t", "NVDA", conn)
    add_symbol("user_t", "tsmc", conn)
    stored = load_stored_profile("user_t", conn)
    # No profile.md for user_t — every other slot empty, watchlist from DB.
    assert stored.watchlist == ["NVDA", "TSM"]
    assert stored.experience is None


# ── API round-trip ───────────────────────────────────────────────────


@pytest.fixture()
def client(conn, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.interfaces.prices import api as prices_api

    monkeypatch.setattr(
        prices_api, "WATCHLIST_DB_PATH", str(tmp_path / "hf_test.db")
    )
    app = FastAPI()
    app.include_router(prices_api.router)
    return TestClient(app)


def test_api_round_trip(client):
    assert client.get("/api/v1/watchlist", params={"user_id": "user_t"}).json() == []

    r = client.post("/api/v1/watchlist", json={"user_id": "user_t", "symbol": "tsmc"})
    assert r.status_code == 200
    assert r.json()["symbol"] == "TSM"
    assert r.json()["name"] == "Taiwan Semiconductor"

    client.post("/api/v1/watchlist", json={"user_id": "user_t", "symbol": "BTC"})
    listed = client.get("/api/v1/watchlist", params={"user_id": "user_t"}).json()
    assert [i["symbol"] for i in listed] == ["BTC-USD", "TSM"]

    r = client.delete("/api/v1/watchlist/TSM", params={"user_id": "user_t"})
    assert r.status_code == 200
    assert [i["symbol"] for i in r.json()] == ["BTC-USD"]


def test_api_unknown_symbol_422(client):
    r = client.post("/api/v1/watchlist", json={"user_id": "user_t", "symbol": "ZZZJUNK"})
    assert r.status_code == 422
    assert "ZZZJUNK" in r.json()["detail"]


def test_api_remove_absent_404(client):
    r = client.delete("/api/v1/watchlist/NVDA", params={"user_id": "user_t"})
    assert r.status_code == 404

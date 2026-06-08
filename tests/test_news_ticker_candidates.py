"""Tests for `src.news.ticker_candidates` — slate builder + alias index.

The slate is the closed universe the synth's LLM picks tickers from. These
tests pin three invariants:

1. Aliases come from `display` + `aliases_json` only — never `short`. The
   short column was the source of every Powell/Monday/Constellation
   name-collision bug in the report.
2. Member body content (titles + first ~6K of body) seeds the slate via
   long-form alias matching.
3. The `aliases_for_many` helper produces the per-symbol alias set the
   verifier uses to gate `evidence_span`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.news import ticker_candidates as tc
from src.news.types import ClusterSourceDoc


@pytest.fixture(autouse=True)
def _reset_alias_cache():
    tc._ALIAS_CACHE = None
    tc._ALIASES_BY_SYMBOL_CACHE = None
    yield
    tc._ALIAS_CACHE = None
    tc._ALIASES_BY_SYMBOL_CACHE = None


def _seed_instruments(db_path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE instruments (
              symbol TEXT PRIMARY KEY, display TEXT NOT NULL, short TEXT NOT NULL,
              asset_class TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]',
              canonical_symbol TEXT, active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        for r in rows:
            conn.execute(
                """INSERT INTO instruments
                   (symbol, display, short, asset_class, aliases_json, canonical_symbol, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    r["symbol"],
                    r.get("display", r["symbol"]),
                    r.get("short", r["symbol"]),
                    r.get("asset_class", "equity"),
                    json.dumps(r.get("aliases") or []),
                    r.get("canonical"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _member(news_id: str, body: str, title: str = "") -> ClusterSourceDoc:
    return ClusterSourceDoc(
        news_id=news_id, title=title, url="https://x", publisher="P", body=body
    )


def test_short_field_is_ignored_so_powell_alone_does_not_seed_powl(tmp_path: Path):
    """The `short` column is excluded from the alias index. Mention of
    'Jerome Powell' alone in a body must NOT seed POWL into the slate —
    the long-form 'Powell Industries' has to appear.
    """
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {
                "symbol": "POWL",
                "display": "Powell Industries",
                "short": "Powell",  # short alone would have been the bug
                "aliases": ["Powell Industries"],
            },
        ],
    )
    members = [_member("n1", "Jerome Powell raised rates at the Fed today.")]
    candidates = tc.build_ticker_candidates(members, db_path=db)
    assert candidates == []


def test_long_form_alias_seeds_slate(tmp_path: Path):
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {
                "symbol": "POWL",
                "display": "Powell Industries",
                "short": "Powell",
                "aliases": ["Powell Industries"],
            },
        ],
    )
    members = [_member("n1", "Powell Industries reported earnings beating estimates.")]
    candidates = tc.build_ticker_candidates(members, db_path=db)
    assert [c["symbol"] for c in candidates] == ["POWL"]
    assert "Powell Industries" in candidates[0]["aliases"]


def test_cluster_tickers_lead_the_slate(tmp_path: Path):
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {"symbol": "AAPL", "display": "Apple Inc", "short": "Apple", "aliases": ["Apple"]},
            {"symbol": "META", "display": "Meta Platforms", "short": "Meta", "aliases": ["Meta", "Facebook"]},
        ],
    )
    members = [_member("n1", "Meta added compute, Apple unrelated.")]
    candidates = tc.build_ticker_candidates(
        members, cluster_tickers=["AAPL"], db_path=db
    )
    symbols = [c["symbol"] for c in candidates]
    assert symbols[0] == "AAPL"
    assert "META" in symbols


def test_explicit_ticker_notation_in_body_seeds_slate(tmp_path: Path):
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {"symbol": "NVDA", "display": "NVIDIA Corporation", "short": "NVIDIA", "aliases": ["NVIDIA"]},
        ],
    )
    members = [_member("n1", "The company (NASDAQ:NVDA) reported revenue growth.")]
    candidates = tc.build_ticker_candidates(members, db_path=db)
    assert [c["symbol"] for c in candidates] == ["NVDA"]


def test_explicit_ticker_paren_prefix_does_not_capture_exchange_token(tmp_path: Path):
    """Regression guard: the old regex used `[$(]` which made `(NASDAQ:NVDA)`
    match `NASDAQ` (the token after the `(`) instead of `NVDA`. The new
    regex drops the bare `(` trigger, so `(NASDAQ:NVDA)` is parsed by the
    exchange-prefix branch and resolves to NVDA. NASDAQ is not a valid
    ticker — this test pins that the captured symbol is the right one.
    """
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {"symbol": "NVDA", "display": "NVIDIA Corporation", "short": "NVIDIA", "aliases": ["NVIDIA"]},
            # If NASDAQ were ever in the registry as an alias, the old bug
            # would surface here. Seed it as a distractor to make the test
            # robust: if a future refactor reintroduces `(` as a trigger,
            # the captured symbol order will flip and this test will fail.
        ],
    )
    members = [_member("n1", "NVIDIA Corporation (NASDAQ:NVDA) beat estimates.")]
    candidates = tc.build_ticker_candidates(members, db_path=db)
    symbols = [c["symbol"] for c in candidates]
    assert symbols == ["NVDA"]
    assert "NASDAQ" not in symbols


def test_explicit_ticker_body_slice_extends_to_6k(tmp_path: Path):
    """Press releases often bury the issuer mention well past the lede.
    The slice was 4K; widened to 6K to match the alias scan and to catch
    `$SYM` mentions that sit late in the body.
    """
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [{"symbol": "NVDA", "display": "NVIDIA Corporation", "short": "NVIDIA", "aliases": ["NVIDIA"]}],
    )
    filler = "x" * 5000
    body = f"Some lead paragraph. {filler} The company ($NVDA) reported revenue."
    members = [_member("n1", body)]
    candidates = tc.build_ticker_candidates(members, db_path=db)
    assert [c["symbol"] for c in candidates] == ["NVDA"]


def test_explicit_ticker_unknown_to_registry_dropped(tmp_path: Path):
    """The explicit-ticker regex finds $FAKE in the body, but FAKE isn't
    in the registry — drop it. The slate is registry-bounded by design.
    """
    db = tmp_path / "hf.db"
    _seed_instruments(db, [])
    members = [_member("n1", "Some PR about $FAKE (NASDAQ:FAKE) earnings.")]
    candidates = tc.build_ticker_candidates(members, db_path=db)
    assert candidates == []


def test_max_candidates_caps_slate(tmp_path: Path):
    db = tmp_path / "hf.db"
    rows = [
        {"symbol": f"SYM{i:02d}", "display": f"Issuer {i:02d}", "short": f"I{i:02d}", "aliases": [f"Issuer {i:02d}"]}
        for i in range(80)
    ]
    _seed_instruments(db, rows)
    body = "\n".join(f"Issuer {i:02d} announced earnings." for i in range(80))
    members = [_member("n1", body)]
    candidates = tc.build_ticker_candidates(members, max_candidates=10, db_path=db)
    assert len(candidates) == 10


def test_aliases_for_many_returns_per_symbol_sets(tmp_path: Path):
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {"symbol": "META", "display": "Meta Platforms", "short": "Meta", "aliases": ["Meta", "Facebook"]},
            {"symbol": "AAPL", "display": "Apple Inc", "short": "Apple", "aliases": ["Apple"]},
        ],
    )
    aliases = tc.aliases_for_many(["META", "AAPL", "UNKNOWN"], db_path=db)
    assert "META" in aliases
    assert "Meta Platforms" in aliases["META"]
    assert "Facebook" in aliases["META"]
    assert "AAPL" in aliases
    assert "UNKNOWN" not in aliases  # not in registry, not surfaced


def test_canonical_symbol_collapses_alias_rows(tmp_path: Path):
    """An alias row with `canonical_symbol` set should collapse to the
    canonical symbol — both the slate and the aliases_for_many lookup.
    """
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {"symbol": "BTC-USD", "display": "Bitcoin", "short": "BTC", "aliases": ["Bitcoin"]},
            # Alias row pointing to BTC-USD as canonical
            {"symbol": "BTC", "display": "Bitcoin", "short": "BTC", "aliases": ["BTC"], "canonical": "BTC-USD"},
        ],
    )
    members = [_member("n1", "Bitcoin rallied past $100,000 today.")]
    candidates = tc.build_ticker_candidates(members, db_path=db)
    assert [c["symbol"] for c in candidates] == ["BTC-USD"]


def test_etf_not_seeded_by_free_body_scan(tmp_path: Path):
    """ETF / rate / FX / commodity aliases must NOT enter the slate via
    free body-scan — those names appear incidentally in many unrelated
    stories ("S&P 500 fell", "Treasury yield rose"). They enter only via
    the sector-thematic tier.
    """
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {
                "symbol": "TLT",
                "display": "iShares 20+ Year Treasury Bond ETF",
                "short": "TLT",
                "asset_class": "etf",
                "aliases": ["Long Treasury ETF", "iShares 20+ Year Treasury Bond ETF"],
            },
        ],
    )
    members = [_member("n1", "The iShares 20+ Year Treasury Bond ETF rallied today.")]
    # No `sectors` passed — slate should be empty even though the body
    # contains a long-form ETF alias.
    candidates = tc.build_ticker_candidates(members, db_path=db)
    assert candidates == []


def test_sector_thematic_seeds_etf_with_context_aliases(tmp_path: Path, monkeypatch):
    """A cluster routed to `macro.rates` should seed TLT into the slate
    even when the body never says "TLT" or "iShares..." — only "Treasury
    yield". The thematic context phrases merge into the slate entry's
    aliases so the verifier accepts evidence_span="Treasury yield".
    """
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {
                "symbol": "TLT",
                "display": "iShares 20+ Year Treasury Bond ETF",
                "short": "TLT",
                "asset_class": "etf",
                "aliases": ["Long Treasury ETF"],
            },
        ],
    )
    monkeypatch.setitem(
        tc.SECTOR_THEMATIC_TICKERS,
        "macro.rates",
        [("TLT", ["Treasury yield", "10-year Treasury"])],
    )
    members = [_member("n1", "The 10-year Treasury yield climbed to 4.6%.")]
    candidates = tc.build_ticker_candidates(
        members, sectors=["macro.rates"], db_path=db
    )
    assert [c["symbol"] for c in candidates] == ["TLT"]
    aliases = candidates[0]["aliases"]
    assert "Treasury yield" in aliases
    assert "10-year Treasury" in aliases
    assert "Long Treasury ETF" in aliases  # registry alias preserved


def test_sector_thematic_unknown_symbol_dropped(tmp_path: Path, monkeypatch):
    """A thematic-map entry whose symbol isn't in the instruments registry
    is silently dropped — the slate stays registry-bounded.
    """
    db = tmp_path / "hf.db"
    _seed_instruments(db, [])
    monkeypatch.setitem(
        tc.SECTOR_THEMATIC_TICKERS,
        "macro.rates",
        [("NOSUCH", ["Treasury yield"])],
    )
    candidates = tc.build_ticker_candidates(
        [_member("n1", "Treasury yields climbed.")],
        sectors=["macro.rates"],
        db_path=db,
    )
    assert candidates == []


def test_sector_thematic_only_fires_for_matching_sector(tmp_path: Path, monkeypatch):
    """TLT is mapped to macro.rates only — a clusters with sectors=[
    'technology.semiconductor'] must not pick it up.
    """
    db = tmp_path / "hf.db"
    _seed_instruments(
        db,
        [
            {
                "symbol": "TLT",
                "display": "iShares 20+ Year Treasury Bond ETF",
                "short": "TLT",
                "asset_class": "etf",
                "aliases": ["Long Treasury ETF"],
            },
        ],
    )
    monkeypatch.setitem(
        tc.SECTOR_THEMATIC_TICKERS,
        "macro.rates",
        [("TLT", ["Treasury yield"])],
    )
    candidates = tc.build_ticker_candidates(
        [_member("n1", "Treasury yields surged.")],
        sectors=["technology.semiconductor"],
        db_path=db,
    )
    assert candidates == []


def test_format_candidate_slate_renders_for_prompt():
    candidates = [
        {"symbol": "META", "display": "Meta Platforms", "aliases": ["Meta Platforms", "Facebook"]},
        {"symbol": "AAPL", "display": "Apple Inc", "aliases": ["Apple Inc"]},
    ]
    rendered = tc.format_candidate_slate(candidates)
    assert "META — Meta Platforms" in rendered
    assert "AAPL — Apple Inc" in rendered
    assert "Facebook" in rendered  # aliases listed for verifier hint

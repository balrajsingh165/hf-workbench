"""Per-cluster ticker candidate slate for synth + verifier alias lookup.

The slate is the *closed universe* of symbols the LLM is invited to
consider when synthesizing a cluster story. The synth prompt presents the
slate with each symbol's display name and long-form aliases; the LLM
picks symbols from the slate and emits `evidence_span` as a verbatim
span from the cited body. The verifier enforces membership +
evidence_span ∈ aliases.

Two seeding tiers:

Tier A — **issuer body-scan** (asset_class='equity'):
  1. Cluster's existing tickers (already validated by routing).
  2. Explicit ticker mentions ($NVDA / NASDAQ:NVDA).
  3. Long-form company-name aliases scanned from member bodies
     (e.g. "Powell Industries", not "Powell").

Tier B — **sector thematic seeds** (etf, rate, fx, commodity):
  4. Symbols mapped from `SECTOR_THEMATIC_TICKERS` for any sector on
     the cluster. These never enter via free body-scan — only when the
     cluster's sectors invite them. Each thematic seed carries
     context-alias phrases (e.g. TLT ← "Treasury yield", USO ← "Brent
     crude") that get merged into the slate entry's aliases and flow
     through to the verifier. The body still has to contain one of
     those phrases verbatim for the LLM to satisfy
     evidence_span ∈ aliases.

Why two tiers? ETF / rate / FX / commodity names appear incidentally in
many unrelated stories ("S&P 500 fell 0.6%"), so they would pollute a
free body-scan. The sector gate restricts them to clusters whose routing
already identified the right macro context.

Capped at MAX_CANDIDATES (60) per cluster, sorted by priority + symbol.

Alias semantics: registry aliases come from `display` + `aliases_json`.
The `short` field is intentionally excluded — it's a UI label
(e.g. POWL.short = "Powell") and the source of every name-collision bug
in the report. Thematic aliases are merged on top per-cluster, never
written back to the registry.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

from src.news.types import ClusterSourceDoc


# Explicit ticker notation in press releases / news bodies:
#   "$NVDA", "(NASDAQ:NVDA)", "NYSE: BRK.B", "NASDAQ:NVDA", etc.
# Note: a bare `(` is intentionally NOT a trigger — that pre-existing bug
# caused "(NASDAQ:NVDA)" to match "NASDAQ" instead of "NVDA". The leading
# paren is tolerated by the exchange-prefix branch consuming what follows it.
EXPLICIT_TICKER_RE = re.compile(
    r"(?:\$\s*|(?:NASDAQ|NYSE|NYSEARCA|AMEX|ASX|LSE|TSE|OTCMKTS)\s*:\s*)"
    r"([A-Z][A-Z0-9.\-]{0,12}(?:=[A-Z])?)"
)


_DB_PATH = Path(__file__).resolve().parents[2] / "db" / "hf.db"
_MIN_ALIAS_LEN = 4
MAX_CANDIDATES = 60

# Asset classes whose registry aliases are eligible for free body-scan
# seeding. Issuer names ("Powell Industries") almost always mean "this
# story is about that issuer." ETF / rate / FX / commodity names appear
# incidentally in many unrelated stories ("S&P 500", "Treasury yield",
# "WTI crude") — they enter the slate only via SECTOR_THEMATIC_TICKERS.
_BODY_SCAN_ASSET_CLASSES: frozenset[str] = frozenset({"equity"})

# All asset classes loaded into the alias index. The body-scan tier is
# bounded by `_BODY_SCAN_ASSET_CLASSES`; the thematic tier can pull any
# loaded class — which is why non-equity instruments must still be in
# the index so `aliases_by_symbol` can answer for them.
_INDEXED_ASSET_CLASSES: tuple[str, ...] = (
    "equity", "etf", "rate", "fx", "commodity",
)

# Per-sector thematic instrument seeds. Each entry: (symbol, [context_aliases]).
#   - `symbol` must be present in the `instruments` registry; entries
#     pointing at unknown symbols are silently dropped.
#   - `context_aliases` are phrases that commonly appear verbatim in
#     bodies for stories about this sector. They are MERGED into the
#     slate entry's alias set so the verifier accepts evidence_span like
#     "Treasury yield" for TLT or "Brent crude" for USO. Without these,
#     thematic ETFs would seed the slate but the LLM could not satisfy
#     the alias-substring rule against any normal story body.
#   - Phrases can be lowercase; the verifier's alias check is case-
#     insensitive. The verifier's separate "evidence_span contains an
#     uppercase letter" rule still applies — the LLM is expected to pick
#     a body span like "10-year Treasury yield" rather than the bare
#     all-lowercase alias.
SECTOR_THEMATIC_TICKERS: dict[str, list[tuple[str, list[str]]]] = {
    "macro.rates": [
        ("TLT", [
            "Treasury yield", "Treasury yields", "Treasury bond", "Treasury bonds",
            "Treasury market", "Treasury note", "Treasury notes",
            "Treasury curve", "U.S. Treasury",
            "10-year Treasury", "30-year Treasury", "2-year Treasury",
            "long-dated Treasuries",
        ]),
        ("TMV", [
            "Treasury yield", "Treasury bond", "Treasury market",
            "10-year Treasury", "30-year Treasury",
        ]),
        ("^TNX", [
            "10-year Treasury", "Treasury yield", "Treasury yields",
            "Treasury market",
        ]),
        ("PFIX", ["Treasury yield", "rate hike", "Federal Reserve"]),
    ],
    "macro.sovereign_credit": [
        ("TLT", [
            "Treasury yield", "Treasury bond", "U.S. Treasury",
            "Treasury market", "sovereign yields",
        ]),
        ("^TNX", ["10-year Treasury", "Treasury yield", "U.S. Treasury"]),
    ],
    "macro.fx": [
        # DXY and USDJPY collapse to canonical DX-Y.NYB / JPY=X via
        # instruments.canonical_symbol; only seed the canonicals.
        ("DX-Y.NYB", ["U.S. dollar", "Dollar Index", "U.S. Dollar Index"]),
        ("EURUSD=X", ["EUR/USD", "Euro", "Euro zone"]),
        ("JPY=X", ["Japanese yen", "USD/JPY", "Yen", "Bank of Japan"]),
        ("FXY", ["Japanese yen", "USD/JPY", "Yen"]),
    ],
    "macro.commodities": [
        ("GLD", ["Gold", "gold prices", "spot gold", "gold futures", "bullion"]),
        ("GDX", ["Gold miners", "gold mining"]),
        ("GC=F", ["Gold", "gold futures", "spot gold"]),
        ("USO", ["WTI", "Brent", "WTI crude", "Brent crude", "crude oil"]),
        ("BZ=F", ["Brent", "Brent crude"]),
        ("CL=F", ["WTI", "WTI crude"]),
        ("UNG", ["Henry Hub", "natural gas"]),
        ("NG=F", ["Henry Hub", "natural gas"]),
    ],
    "energy.oil_gas": [
        ("USO", [
            "WTI", "Brent", "WTI crude", "Brent crude", "crude oil",
            "Strait of Hormuz", "OPEC",
        ]),
        ("XLE", ["U.S. oil producers", "Energy Select", "oil majors"]),
        ("CL=F", ["WTI", "WTI crude"]),
        ("BZ=F", ["Brent", "Brent crude"]),
    ],
    "energy.uranium": [
        ("URA", ["Uranium", "uranium miners", "uranium prices", "uranium supply"]),
    ],
    "materials.metals_mining": [
        ("GLD", ["Gold", "gold prices", "spot gold", "gold futures"]),
        ("GDX", ["Gold miners", "gold mining"]),
        ("GC=F", ["Gold", "gold futures"]),
    ],
    "crypto.bitcoin": [
        ("IBIT", ["Bitcoin", "spot Bitcoin ETF", "Bitcoin ETF", "Bitcoin price"]),
        ("FBTC", ["Bitcoin", "Bitcoin ETF", "spot Bitcoin"]),
        ("GBTC", ["Bitcoin", "Bitcoin Trust", "spot Bitcoin"]),
        ("ARKB", ["Bitcoin", "spot Bitcoin ETF"]),
    ],
    "technology.semiconductor": [
        ("SMH", ["Semiconductor", "semiconductor sector", "chip stocks", "chipmakers"]),
    ],
    "industrials.aerospace_defense": [
        ("DFEN", ["Aerospace", "Defense", "defense stocks", "aerospace and defense"]),
    ],
}


# Cached alias index. Each entry: (pattern, symbol, phrase, asset_class).
# Loaded lazily from the instruments table; reload() to bust the cache
# after schema/data changes.
_AliasIndex = list[tuple[re.Pattern[str], str, str, str]]
_ALIAS_CACHE: _AliasIndex | None = None
_ALIASES_BY_SYMBOL_CACHE: dict[str, set[str]] | None = None
_CACHE_LOCK = threading.Lock()


def _load_alias_index(db_path: Path) -> tuple[_AliasIndex, dict[str, set[str]]]:
    """Build the alias regex index and a symbol→aliases lookup.

    Returns `(index, aliases_by_symbol)`. Both share the same alias
    source: `display` + every entry in `aliases_json`. The `short`
    column is NOT consulted — see module docstring. Non-equity asset
    classes are loaded (so `aliases_for_many` can answer for them and
    sector-thematic seeding can pull their registry aliases), but each
    entry's `asset_class` is tracked so the body-scan path can filter
    them out.
    """
    if not db_path.exists():
        return [], {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in _INDEXED_ASSET_CLASSES)
    try:
        rows = conn.execute(
            f"""
            SELECT symbol, display, aliases_json, canonical_symbol, asset_class
            FROM instruments
            WHERE active = 1 AND asset_class IN ({placeholders})
            """,
            tuple(_INDEXED_ASSET_CLASSES),
        ).fetchall()
    finally:
        conn.close()

    index: _AliasIndex = []
    aliases_by_symbol: dict[str, set[str]] = {}
    seen_phrases: set[tuple[str, str]] = set()
    for row in rows:
        canonical = (row["canonical_symbol"] or row["symbol"]).strip().upper()
        asset_class = (row["asset_class"] or "").strip().lower()
        phrases: list[str] = []
        try:
            phrases.extend(json.loads(row["aliases_json"] or "[]"))
        except json.JSONDecodeError:
            pass
        if row["display"] and row["display"] not in phrases:
            phrases.append(row["display"])
        for phrase in phrases:
            phrase_clean = (phrase or "").strip()
            if len(phrase_clean) < _MIN_ALIAS_LEN:
                # Skip 1-3 char aliases — too prone to false-positive matches.
                # Long-form names ("Eli Lilly", "Powell Industries") are
                # the safe matching surface.
                continue
            aliases_by_symbol.setdefault(canonical, set()).add(phrase_clean)
            key = (canonical, phrase_clean.lower())
            if key in seen_phrases:
                continue
            seen_phrases.add(key)
            # Word-boundary, case-insensitive match. re.escape handles "&",
            # ".", parentheses, etc. in display names like "AT&T".
            pattern = re.compile(rf"\b{re.escape(phrase_clean)}\b", re.IGNORECASE)
            index.append((pattern, canonical, phrase_clean, asset_class))
    return index, aliases_by_symbol


def _audit_thematic_map(aliases_by_symbol: dict[str, set[str]]) -> None:
    """Warn once when a SECTOR_THEMATIC_TICKERS entry references a symbol
    that isn't in the loaded instruments registry. The slate builder
    silently skips such entries (registry is source of truth), so without
    this audit a typo like `PFXI` for `PFIX` would no-op forever with no
    pipeline signal.
    """
    missing: list[tuple[str, str]] = []
    for sector, entries in SECTOR_THEMATIC_TICKERS.items():
        for sym, _aliases in entries:
            sym_upper = (sym or "").strip().upper()
            if sym_upper and sym_upper not in aliases_by_symbol:
                missing.append((sector, sym_upper))
    if missing:
        logger.warning(
            "SECTOR_THEMATIC_TICKERS references %d symbol(s) not in instruments "
            "registry; thematic slate will skip them: %s",
            len(missing),
            ", ".join(f"{sec}:{sym}" for sec, sym in missing),
        )


def _ensure_cache(db_path: Path) -> tuple[_AliasIndex, dict[str, set[str]]]:
    global _ALIAS_CACHE, _ALIASES_BY_SYMBOL_CACHE
    if _ALIAS_CACHE is None or _ALIASES_BY_SYMBOL_CACHE is None:
        with _CACHE_LOCK:
            if _ALIAS_CACHE is None or _ALIASES_BY_SYMBOL_CACHE is None:
                index, aliases_by_symbol = _load_alias_index(db_path)
                _ALIAS_CACHE = index
                _ALIASES_BY_SYMBOL_CACHE = aliases_by_symbol
                _audit_thematic_map(aliases_by_symbol)
    return _ALIAS_CACHE, _ALIASES_BY_SYMBOL_CACHE


def reload(db_path: Path = _DB_PATH) -> int:
    """Drop the in-process alias cache and reload. Returns alias-pattern count."""
    global _ALIAS_CACHE, _ALIASES_BY_SYMBOL_CACHE
    with _CACHE_LOCK:
        index, aliases_by_symbol = _load_alias_index(db_path)
        _ALIAS_CACHE = index
        _ALIASES_BY_SYMBOL_CACHE = aliases_by_symbol
        _audit_thematic_map(aliases_by_symbol)
    return len(_ALIAS_CACHE)


def find_aliases_in_text(
    text: str,
    *,
    db_path: Path = _DB_PATH,
) -> list[tuple[str, str]]:
    """Scan `text` for alias hits, return `[(symbol, matched_phrase), ...]`
    in first-occurrence order. Only equity-class instruments participate —
    issuer names mean "the story is about this issuer", whereas ETF / rate
    / FX / commodity names ("Treasury yield", "S&P 500") appear
    incidentally everywhere and would pollute the body-scan tier. Those
    classes enter the slate via `SECTOR_THEMATIC_TICKERS` instead.
    """
    if not text:
        return []
    index, _ = _ensure_cache(db_path)
    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pattern, symbol, phrase, asset_class in index:
        if asset_class not in _BODY_SCAN_ASSET_CLASSES:
            continue
        if pattern.search(text):
            key = (symbol, phrase)
            if key in seen:
                continue
            hits.append((symbol, phrase))
            seen.add(key)
    return hits


def aliases_for(symbol: str, *, db_path: Path = _DB_PATH) -> set[str]:
    """Return the long-form alias set for `symbol`, or empty set if unknown."""
    _, aliases_by_symbol = _ensure_cache(db_path)
    return set(aliases_by_symbol.get(symbol.strip().upper(), set()))


def aliases_for_many(
    symbols: list[str] | set[str],
    *,
    db_path: Path = _DB_PATH,
) -> dict[str, set[str]]:
    """Return `{symbol: {alias, alias, ...}}` for every symbol in `symbols`
    that exists in the registry. Used by the verifier to enforce
    `evidence_span ∈ aliases`.
    """
    _, aliases_by_symbol = _ensure_cache(db_path)
    out: dict[str, set[str]] = {}
    for raw in symbols:
        sym = (raw or "").strip().upper()
        if not sym:
            continue
        if sym in aliases_by_symbol:
            out[sym] = set(aliases_by_symbol[sym])
    return out


def _explicit_tickers_from_members(
    members: list[ClusterSourceDoc],
) -> list[str]:
    """Pull every `$NVDA` / `NASDAQ:NVDA` style explicit ticker mention
    from member titles + bodies. Order-preserving, deduped.

    Body slice matches the alias scan in `build_ticker_candidates` (6000
    chars) — press releases often bury the issuer mention well below the
    lede, so 4K was too short.
    """
    seen: set[str] = set()
    out: list[str] = []
    for member in members:
        text = f"{member.title or ''}\n{(member.body or '')[:6000]}"
        for match in EXPLICIT_TICKER_RE.finditer(text):
            symbol = match.group(1).strip().upper().rstrip(")")
            if symbol and symbol not in seen:
                seen.add(symbol)
                out.append(symbol)
    return out


def build_ticker_candidates(
    members: list[ClusterSourceDoc],
    *,
    cluster_tickers: list[str] | None = None,
    sectors: list[str] | None = None,
    max_candidates: int = MAX_CANDIDATES,
    db_path: Path = _DB_PATH,
) -> list[dict]:
    """Build the per-cluster ticker candidate slate for `synthesize_cluster`.

    Returns a list of `{symbol, display, aliases}` dicts, capped at
    `max_candidates`, in priority order:

      1. `cluster_tickers` (already attached by routing — validated).
      2. Explicit ticker mentions in member bodies.
      3. Long-form alias hits in member bodies (equity issuers only).
      4. Sector-thematic seeds (ETF/rate/FX/commodity instruments mapped
         to any sector in `sectors` via `SECTOR_THEMATIC_TICKERS`). Each
         seed's context-alias phrases are merged into that symbol's
         alias list so the verifier accepts evidence_span like
         "Treasury yield" (TLT) or "Brent crude" (USO).

    Symbols not present in the `instruments` registry are dropped. Pass
    `sectors=cluster_sector_prior` to enable thematic seeding — without it,
    macro clusters with no named issuer in the body emit `tickers: []`.
    """
    _, aliases_by_symbol = _ensure_cache(db_path)
    if not aliases_by_symbol:
        return []

    ordered: list[str] = []
    seen: set[str] = set()
    thematic_aliases: dict[str, set[str]] = {}

    def _add(symbol: str) -> None:
        sym = (symbol or "").strip().upper()
        if not sym or sym in seen:
            return
        if sym not in aliases_by_symbol:
            return
        seen.add(sym)
        ordered.append(sym)

    for sym in cluster_tickers or []:
        _add(sym)

    for sym in _explicit_tickers_from_members(members):
        _add(sym)

    body_text_parts: list[str] = []
    for member in members:
        if member.title:
            body_text_parts.append(member.title)
        if member.body:
            body_text_parts.append(member.body[:6000])
    body_haystack = "\n".join(body_text_parts)
    for sym, _phrase in find_aliases_in_text(body_haystack, db_path=db_path):
        _add(sym)

    # Tier B — sector-thematic seeds. ETFs / rates / FX / commodities
    # gated on cluster sector. Context aliases get merged below.
    for sector in sectors or []:
        for sym, context_aliases in SECTOR_THEMATIC_TICKERS.get(sector, ()):
            _add(sym)
            sym_upper = (sym or "").strip().upper()
            if sym_upper in seen:
                thematic_aliases.setdefault(sym_upper, set()).update(
                    a.strip() for a in context_aliases if a and a.strip()
                )

    ordered = ordered[:max_candidates]

    out: list[dict] = []
    for sym in ordered:
        merged = set(aliases_by_symbol.get(sym, set()))
        merged.update(thematic_aliases.get(sym, set()))
        if not merged:
            continue
        aliases = sorted(merged, key=lambda s: (-len(s), s))
        out.append({
            "symbol": sym,
            "display": aliases[0],
            "aliases": aliases,
        })
    return out


def format_candidate_slate(candidates: list[dict]) -> str:
    """Render the candidate slate as a prompt block.

    Format per line:  `- SYMBOL — Display Name | aliases: ["A","B"]`
    The LLM uses this to (a) constrain its symbol choice to the slate and
    (b) pick a verbatim alias as `evidence_span` that the verifier accepts.
    """
    if not candidates:
        return ""
    lines: list[str] = []
    for c in candidates:
        sym = c.get("symbol") or ""
        display = c.get("display") or sym
        aliases = c.get("aliases") or [display]
        aliases_json = json.dumps(aliases, ensure_ascii=False)
        lines.append(f"- {sym} — {display} | aliases: {aliases_json}")
    return "\n".join(lines)


__all__ = [
    "EXPLICIT_TICKER_RE",
    "MAX_CANDIDATES",
    "SECTOR_THEMATIC_TICKERS",
    "aliases_for",
    "aliases_for_many",
    "build_ticker_candidates",
    "find_aliases_in_text",
    "format_candidate_slate",
    "reload",
]

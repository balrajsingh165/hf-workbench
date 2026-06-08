#!/usr/bin/env python3
"""Trending-ticker demand-driven retrieval lane.

The firehose is supply-driven: it polls fixed press-wire feeds and discovers
whatever tickers appear, so it is structurally blind to attention-driven or
off-wire single-name moves. This lane inverts the funnel — it starts from the
day's *trending* tickers (1ms.news ranking) and goes and fetches news for them
via web search, dropping the results into the **same** `news` table the
firehose writes to. Everything downstream (cluster → route → synth → verify →
persist story → discover thesis → score) consumes them unchanged.

The one idea: the trending list is the watchlist, web search is the retriever,
the existing pipeline is everything else. See
`docs/plan-trending-ticker-retrieval.md`.

Two tiers, distinguished only by rank range and cadence. Tier membership uses
the **best (lowest) rank a symbol held across the two most-recent snapshots**
(today + yesterday) — `effective_rank` — so a name that was hot yesterday but
slipped today still gets one more day of Tier-1 treatment (the residue effect),
which is exactly when its off-wire follow-on tends to land.

Usage:
    uv run python -m agents.trending --tier 1               # live, daily tier
    uv run python -m agents.trending --tier 2               # live, 3-day tail
    uv run python -m agents.trending --tier 1 --dry-run     # fetch + preview, no Exa, no writes
    uv run python -m agents.trending --tier 1 --max-symbols 2   # bounded live run (testing)
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.firehose import BODY_EXCERPT_CHARS, FirehoseEntry, insert_entry
from src.clients.mesh import exa_digest_tool, unwrap_results
from src.instruments import resolver
from src.news.firehose_gate import (
    LAWYER_SPAM,
    build_alias_index,
    score_materiality,
    tag_text,
)
from src.news.publishers import publisher_for_url

DB_PATH = ROOT / "db" / "hf.db"
logger = logging.getLogger(__name__)

# ── 1ms.news ranking source ───────────────────────────────────────────────
TREND_SOURCE = "1ms_stocks"
RANKING_URL = "https://1ms.news/ranking?source=stocks"
RANKING_FETCH_TIMEOUT_S = 20

# Spike-confirmed (2026-06-02): server-rendered HTML, a single <table> with a
# fixed 7-column header and ~100 data rows. The header canary is the cheapest
# regression guard — a layout change trips it before we mis-map columns.
EXPECTED_HEADER: tuple[str, ...] = (
    "排名", "代码", "名称", "24h 提及", "变化", "点赞", "排名趋势",
)
# The page reliably returns 100 rows; anything under this floor means a broken
# fetch/parse, not a quiet day. A hard failure (not a silent empty return).
MIN_RANKING_ROWS = 10

# Exa extraction prompt: force a per-URL, parseable block so we can mint one
# `news` row per article. The default (empty prompt) returns one combined
# summary for the batch, which we can't split into per-URL citation targets.
_EXTRACT_PROMPT = (
    "Output exactly two lines and nothing else: 'TITLE: <the article "
    "headline>', then 'BODY: <4 to 6 sentence factual summary of what "
    "happened and why it matters for the stock>'."
)
_TITLE_RE = re.compile(r"TITLE:\s*(.+)")
_BODY_RE = re.compile(r"BODY:\s*(.+)", re.S)

# Strip Exa digest artifacts ([WARNING], inline [1] / [2, 3] citation markers)
# so the stored body reads cleanly and stays a faithful verbatim target for
# the synth verifier's quote check.
_DIGEST_ARTIFACT_RE = re.compile(r"\[\s*(?:WARNING|\d+(?:\s*,\s*\d+)*)\s*\]")
_URL_RE = re.compile(r"https?://[^\s\]\)<>]+")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _tier1_max() -> int:
    return _env_int("HF_TRENDING_TIER1_MAX", 20)


def _tier2_max() -> int:
    return _env_int("HF_TRENDING_TIER2_MAX", 60)


def _max_hits_per_symbol() -> int:
    return _env_int("HF_TRENDING_MAX_HITS_PER_SYMBOL", 3)


def _search_limit() -> int:
    # Exa enforces 6..10; 6 is the cheapest valid request.
    return max(6, min(10, _env_int("HF_TRENDING_SEARCH_LIMIT", 6)))


class TrendingFetchError(RuntimeError):
    """1ms fetch/parse breakage. Carries a `phase` ('fetch' | 'parse') so the
    `trending_run` metric and the health check can distinguish a network/HTTP
    failure from a page-structure change. Because v1 has no fallback source,
    this is surfaced as a *critical* finding on the metrics page."""

    def __init__(self, message: str, *, phase: str) -> None:
        super().__init__(message)
        self.phase = phase


@dataclass(slots=True)
class TrendRow:
    rank: int
    raw_symbol: str
    name: str
    mentions_24h: int | None
    mentions_delta: int | None
    upvotes: int | None
    rank_trend: int | None


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _to_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"-?\d[\d,]*", text)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _rank_trend(td) -> int | None:
    """排名趋势 cell. The CSS class carries direction (up/down/same); the text
    carries the magnitude ('↑ 79', '↓ 12', '—')."""
    classes = set(td.get("class") or [])
    magnitude = _to_int(td.get_text(strip=True))
    if "up" in classes:
        return abs(magnitude) if magnitude is not None else None
    if "down" in classes:
        return -abs(magnitude) if magnitude is not None else None
    if "same" in classes:
        return 0
    return magnitude


def fetch_ranking(source: str = "stocks") -> list[TrendRow]:
    """HTTP GET + HTML-table parse of the 1ms ranking. No browser, no JS, no
    fallback source. Raises `TrendingFetchError` (not a silent empty return) on
    a non-200, a missing/renamed table, a header drift, or a sub-floor row
    count — each of which would otherwise silently starve the lane."""
    url = f"https://1ms.news/ranking?source={source}"
    try:
        resp = curl_requests.get(url, impersonate="chrome", timeout=RANKING_FETCH_TIMEOUT_S)
    except Exception as exc:  # network/TLS/timeout
        raise TrendingFetchError(f"GET {url} failed: {exc!r}", phase="fetch") from exc
    if resp.status_code != 200:
        raise TrendingFetchError(
            f"GET {url} returned HTTP {resp.status_code}", phase="fetch"
        )
    if not resp.text:
        raise TrendingFetchError(f"GET {url} returned empty body", phase="fetch")

    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table")
    if table is None:
        raise TrendingFetchError("ranking page has no <table>", phase="parse")
    thead = table.find("thead")
    header = tuple(th.get_text(strip=True) for th in thead.find_all("th")) if thead else ()
    if header != EXPECTED_HEADER:
        raise TrendingFetchError(
            f"ranking header drift: got {header!r}, expected {EXPECTED_HEADER!r}",
            phase="parse",
        )
    body = table.find("tbody")
    rows: list[TrendRow] = []
    for tr in (body.find_all("tr") if body else []):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        rank = _to_int(tds[0].get_text(strip=True))
        raw_symbol = tds[1].get_text(" ", strip=True).strip()
        if rank is None or not raw_symbol:
            continue
        rows.append(TrendRow(
            rank=rank,
            raw_symbol=raw_symbol,
            name=tds[2].get_text(strip=True),
            mentions_24h=_to_int(tds[3].get_text(strip=True)),
            mentions_delta=_to_int(tds[4].get_text(strip=True)),
            upvotes=_to_int(tds[5].get_text(strip=True)),
            rank_trend=_rank_trend(tds[6]),
        ))
    if len(rows) < MIN_RANKING_ROWS:
        raise TrendingFetchError(
            f"ranking returned {len(rows)} rows (< {MIN_RANKING_ROWS} floor)",
            phase="parse",
        )
    return rows


def _resolve_symbol(raw_symbol: str) -> str | None:
    """Resolve a scraped raw symbol to its canonical registry symbol, or None
    if it isn't in `instruments`. Registry-unknowns are still persisted (raw)
    but never scraped — a thesis can't form on an unknown instrument, the same
    constraint the firehose already has."""
    sym = (raw_symbol or "").strip().upper()
    if not sym:
        return None
    if resolver.exists(sym):
        return resolver.canonical(sym)
    return None


def upsert_trends(
    conn: sqlite3.Connection,
    rows: list[TrendRow],
    *,
    snapshot_date: str,
    source: str = TREND_SOURCE,
) -> dict[str, int]:
    """Write one row per (snapshot_date, source, symbol). Idempotent same-day
    via INSERT OR REPLACE on the PK. Upserted on every run (both tiers fetch
    the full ranking) so the snapshot stays complete for the residue
    computation and the homepage surface, even though only the due tier is
    scraped."""
    in_registry = 0
    with conn:
        for row in rows:
            resolved = _resolve_symbol(row.raw_symbol)
            symbol = resolved or row.raw_symbol.strip().upper()
            known = 1 if resolved else 0
            in_registry += known
            conn.execute(
                """
                INSERT OR REPLACE INTO ticker_trends
                  (snapshot_date, source, symbol, raw_symbol, rank,
                   mentions_24h, mentions_delta, upvotes, rank_trend, in_registry)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_date, source, symbol, row.raw_symbol.strip().upper(),
                    row.rank, row.mentions_24h, row.mentions_delta, row.upvotes,
                    row.rank_trend, known,
                ),
            )
    return {"rows": len(rows), "in_registry": in_registry}


def tier_symbols(
    conn: sqlite3.Connection,
    tier: int,
    *,
    tier1_max: int | None = None,
    tier2_max: int | None = None,
    source: str = TREND_SOURCE,
) -> list[tuple[str, int]]:
    """Symbols due for `tier`, ranked by `effective_rank` (min rank across the
    two most-recent snapshots → the one-day residue). Registry-known only.
    Returns [(symbol, effective_rank), ...]. Empty if the ticker_trends table
    is absent (schema not yet applied) — keeps callers resilient."""
    t1 = tier1_max if tier1_max is not None else _tier1_max()
    t2 = tier2_max if tier2_max is not None else _tier2_max()
    lo, hi = _tier_band(tier, t1, t2)
    try:
        rows = conn.execute(
            """
            WITH days AS (
                SELECT snapshot_date FROM ticker_trends
                WHERE source = ?
                GROUP BY snapshot_date
                ORDER BY snapshot_date DESC
                LIMIT 2
            )
            SELECT symbol, MIN(rank) AS eff_rank
            FROM ticker_trends
            WHERE source = ?
              AND in_registry = 1
              AND snapshot_date IN (SELECT snapshot_date FROM days)
            GROUP BY symbol
            HAVING eff_rank BETWEEN ? AND ?
            ORDER BY eff_rank ASC, symbol ASC
            """,
            (source, source, lo, hi),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(str(r[0]).upper(), int(r[1])) for r in rows]


def tier1_symbols(conn: sqlite3.Connection, *, source: str = TREND_SOURCE) -> set[str]:
    """Tier-1 trending symbols (effective_rank ≤ TIER1_MAX). Shared by the
    routing anti-discard union (`agents/route_news_clusters.py`) and the
    homepage Discover blend (`api.py`). Empty set when the table is absent."""
    return {sym for sym, _ in tier_symbols(conn, 1, source=source)}


def _tier_for_rank(eff_rank: int, t1: int, t2: int) -> int:
    """Tier a symbol's effective_rank falls in (0 = below the Tier-2 cutoff)."""
    if eff_rank <= t1:
        return 1
    if eff_rank <= t2:
        return 2
    return 0


def _tier_band(tier: int, t1: int, t2: int) -> tuple[int, int]:
    """Inclusive (lo, hi) effective-rank band for a tier. Tier 1 = [1, t1],
    Tier 2 = [t1+1, t2]. The single source of truth for tier membership, shared
    by the live (`tier_symbols`) and dry-run (`_due_from_rows`) selection paths."""
    if tier == 1:
        return 1, t1
    if tier == 2:
        return t1 + 1, t2
    raise ValueError(f"unknown tier: {tier}")


def latest_trends(
    conn: sqlite3.Connection,
    *,
    source: str = TREND_SOURCE,
    limit: int | None = None,
) -> list[dict]:
    """Latest trending snapshot for the read API (`GET /api/trending`).

    Returns the most-recent snapshot's rows ordered by today's rank, each tagged
    with its `effective_rank` (one-day residue) and the `tier` that residue puts
    it in — the same classification the retrieval lane selects on. Registry-known
    symbols carry a display `name`. Empty list if the table is absent."""
    t1, t2 = _tier1_max(), _tier2_max()
    try:
        rows = conn.execute(
            """
            WITH days AS (
                SELECT snapshot_date FROM ticker_trends
                WHERE source = ?
                GROUP BY snapshot_date
                ORDER BY snapshot_date DESC
                LIMIT 2
            ),
            eff AS (
                SELECT symbol, MIN(rank) AS eff_rank
                FROM ticker_trends
                WHERE source = ?
                  AND snapshot_date IN (SELECT snapshot_date FROM days)
                GROUP BY symbol
            )
            SELECT t.symbol, t.raw_symbol, t.rank, e.eff_rank,
                   t.mentions_24h, t.mentions_delta, t.upvotes,
                   t.in_registry, t.snapshot_date
            FROM ticker_trends t
            JOIN eff e ON e.symbol = t.symbol
            WHERE t.source = ?
              AND t.snapshot_date = (SELECT MAX(snapshot_date) FROM days)
            ORDER BY t.rank ASC
            """,
            (source, source, source),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    out: list[dict] = []
    for r in rows:
        symbol = str(r[0]).upper()
        in_registry = bool(r[7])
        eff_rank = int(r[3])
        out.append({
            "symbol": symbol,
            "name": resolver.to_display(symbol, "full") if in_registry else r[1],
            "rank": int(r[2]),
            "effective_rank": eff_rank,
            "tier": _tier_for_rank(eff_rank, t1, t2),
            "mentions_24h": r[4],
            "mentions_delta": r[5],
            "upvotes": r[6],
            "in_registry": in_registry,
            "snapshot_date": r[8],
        })
        if limit is not None and len(out) >= limit:
            break
    return out


def _extract_source_urls(summary: str) -> list[str]:
    """Pull article URLs from an exa_web_search digest. The digest ends with a
    numbered 'Sources:' list; prefer that region, fall back to the whole text.
    Order-preserving, deduped."""
    if not summary:
        return []
    idx = summary.rfind("Sources:")
    region = summary[idx:] if idx >= 0 else summary
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(region):
        u = m.group(0).rstrip(".,);]")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _clean_body(text: str) -> str:
    return re.sub(r"\s+", " ", _DIGEST_ARTIFACT_RE.sub("", text)).strip()


def _title_from_url(url: str) -> str:
    """Humanized headline from a URL slug — the fallback when the scrape digest
    doesn't carry an explicit TITLE line."""
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.\w+$", "", slug)            # drop .html / .cms
    slug = re.sub(r"[-_]+", " ", slug).strip()
    slug = re.sub(r"\b\d{4,}\b", "", slug).strip()  # drop article-id digits
    return slug.title() if slug else url


def _split_title_body(summary: str, url: str) -> tuple[str, str]:
    """Split a single-document exa_scrape_url digest into (title, body).

    We ask for a 'TITLE:' / 'BODY:' pair, but the model sometimes returns a
    plain prose summary instead. Rather than drop the article, fall back: derive
    the title from the URL slug and treat the whole cleaned digest as the body.
    A scraped page with a non-empty summary always yields a usable entry."""
    tm = _TITLE_RE.search(summary)
    bm = _BODY_RE.search(summary)
    if bm:
        body = _clean_body(bm.group(1))
        title = tm.group(1).strip() if tm else _title_from_url(url)
        return title, body
    # Prose-only response: no BODY marker. Strip any stray TITLE line and keep
    # the rest as the body.
    body = _clean_body(_TITLE_RE.sub("", summary))
    title = tm.group(1).strip() if tm else _title_from_url(url)
    return title, body


def _search_urls(symbol: str, display: str, *, time_filter: str, limit: int) -> list[str]:
    query = f"{display} ({symbol}) stock news catalyst"
    res = exa_digest_tool(
        "exa_web_search",
        {"search_term": query, "time_filter": time_filter, "limit": limit},
    )
    data = unwrap_results(res)
    summary = data.get("processed_summary", "") if isinstance(data, dict) else ""
    return _extract_source_urls(summary)


def _scrape_one(url: str) -> tuple[str, str, str] | None:
    """Scrape a single URL → (url, title, body), or None if it yields nothing.

    One URL per call: the digest is then unambiguously *this* article's summary,
    so we never depend on the model coordinating a multi-document format (which
    it does unreliably — a batch call often returns prose-only and we'd lose
    every article). Each entry maps to its real source_url, which is what the
    firehose dedup and clustering key on."""
    res = exa_digest_tool(
        "exa_scrape_url",
        {"urls": [url], "extract_prompt": _EXTRACT_PROMPT},
    )
    data = unwrap_results(res)
    summary = data.get("processed_summary", "") if isinstance(data, dict) else ""
    if not summary.strip():
        return None
    title, body = _split_title_body(summary, url)
    if not body:
        return None
    return url, title, body


def _scrape_articles(urls: list[str]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            article = _scrape_one(url)
        except Exception as exc:  # one bad URL must not lose the others
            logger.warning("trending scrape failed url=%s: %s", url, exc)
            continue
        if article is not None:
            out.append(article)
    return out


def _build_entry(
    url: str,
    title: str,
    body: str,
    *,
    force_symbol: str,
    ci_index: dict[str, set[str]],
    cs_index: dict[str, set[str]],
    now_iso: str,
) -> FirehoseEntry:
    """Turn a scraped article into a `FirehoseEntry` via the *existing* gate —
    same tagger, same materiality scorer, no new model. The trending symbol is
    force-added to the ticker slate so the row always carries the name we went
    looking for (the social rank is the retrieval trigger; the routing /
    materiality gate downstream is the admission filter)."""
    ex, syms, macros = tag_text(title, body, ci_index, cs_index)
    syms = set(syms)
    syms.add(force_symbol)
    pub = publisher_for_url(url).name
    is_spam = bool(LAWYER_SPAM.search(title))
    publisher = f"{pub}-classaction" if is_spam else pub
    score, classes = score_materiality(title, body, publisher=publisher)
    return FirehoseEntry(
        source_url=url,
        headline=title,
        body_excerpt=body[:BODY_EXCERPT_CHARS],
        published_at=now_iso,
        publisher=publisher,
        exchange_tickers=set(ex),
        registry_symbols=syms,
        macros=set(macros),
        is_lawyer_spam=is_spam,
        materiality_score=score,
        event_classes=classes,
    )


def _due_from_rows(rows: list[TrendRow], tier: int) -> list[tuple[str, int]]:
    """Tier selection for `--dry-run`: no DB, single-day effective_rank = today's
    rank (no residue). Registry-known only."""
    lo, hi = _tier_band(tier, _tier1_max(), _tier2_max())
    # Dedup by canonical symbol keeping the best (lowest) rank — share classes
    # (GOOG/GOOGL) collapse to one canonical, mirroring the live MIN(rank) SQL.
    best: dict[str, int] = {}
    for row in rows:
        resolved = _resolve_symbol(row.raw_symbol)
        if resolved and lo <= row.rank <= hi:
            best[resolved] = min(row.rank, best.get(resolved, row.rank))
    return sorted(best.items(), key=lambda x: (x[1], x[0]))


def run_trending(
    tier: int,
    *,
    db_path: Path = DB_PATH,
    dry_run: bool = False,
    max_symbols: int | None = None,
    time_filter: str = "past_week",
    should_stop=lambda: False,
) -> dict:
    """Orchestrate fetch → upsert → select-by-tier → per-symbol search + scrape
    → gate → insert_entry. Never raises: returns the `trending_run` metric body
    (ok / error / phase / counters). A fetch/parse failure sets ok=false and
    skips scraping (no partial/poisoned snapshot); the caller emits the metric."""
    started = time.perf_counter()
    snapshot_date = _utc_today()
    result: dict = {
        "tier": tier,
        "snapshot_date": snapshot_date,
        "dry_run": dry_run,
        "ok": True,
        "error": None,
        "phase": None,
        "symbols_total": 0,
        "symbols_in_registry": 0,
        "symbols_due": 0,
        "symbols_scraped": 0,
        "searches": 0,
        "scrapes": 0,
        "scrape_errors": 0,
        "articles": 0,
        "inserted": 0,
        "duplicates": 0,
        "gate_dropped": 0,
        "unknown_tickers": 0,
    }

    def _done(**over) -> dict:
        """Stamp wall-clock and return `result` — the single exit point, so the
        duration is recorded no matter which path (success or early error) we
        leave on."""
        result.update(over)
        result["duration_s"] = round(time.perf_counter() - started, 3)
        return result

    # ── Phase 1: fetch the ranking (the critical, no-fallback step) ──────────
    try:
        rows = fetch_ranking()
    except TrendingFetchError as exc:
        logger.error("trending tier=%s fetch failed: %s", tier, exc)
        return _done(ok=False, error=repr(exc), phase=exc.phase)
    except Exception as exc:  # defensive — never let the lane raise
        logger.exception("trending tier=%s unexpected fetch error", tier)
        return _done(ok=False, error=repr(exc), phase="fetch")

    result["symbols_total"] = len(rows)

    if dry_run:
        due = _due_from_rows(rows, tier)
        result["symbols_in_registry"] = sum(
            1 for r in rows if _resolve_symbol(r.raw_symbol)
        )
        if max_symbols is not None:
            due = due[:max_symbols]
        result["symbols_due"] = len(due)
        result["due_symbols"] = [s for s, _ in due]
        return _done()

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        # ── Phase 2: upsert the snapshot ────────────────────────────────────
        try:
            counts = upsert_trends(conn, rows, snapshot_date=snapshot_date)
        except Exception as exc:
            logger.exception("trending tier=%s upsert failed", tier)
            return _done(ok=False, error=repr(exc), phase="insert")
        result["symbols_in_registry"] = counts["in_registry"]

        due = tier_symbols(conn, tier)
        if max_symbols is not None:
            due = due[:max_symbols]
        result["symbols_due"] = len(due)
        # Hot symbols this run mark every inserted article as a clustering /
        # promotion candidate, so the embedding pass fires (see insert_entry).
        promote_tickers = frozenset(s for s, _ in due)

        # ── Phase 3: per-symbol retrieve → gate → insert ────────────────────
        ci_index, cs_index = build_alias_index()
        max_hits = _max_hits_per_symbol()
        now_iso = _utc_now_iso()
        for symbol, _eff_rank in due:
            if should_stop():
                break
            display = resolver.to_display(symbol, "full")
            try:
                urls = _search_urls(symbol, display, time_filter=time_filter, limit=_search_limit())
                result["searches"] += 1
                if not urls:
                    continue
                articles = _scrape_articles(urls[:max_hits])
                result["scrapes"] += 1
            except Exception as exc:  # Exa/Mesh degradation — per-symbol, non-fatal
                result["scrape_errors"] += 1
                logger.warning("trending retrieve failed symbol=%s: %s", symbol, exc)
                continue
            if articles:
                result["symbols_scraped"] += 1
            for url, title, body in articles:
                result["articles"] += 1
                entry = _build_entry(
                    url, title, body,
                    force_symbol=symbol, ci_index=ci_index, cs_index=cs_index,
                    now_iso=now_iso,
                )
                try:
                    nid, unknown = insert_entry(
                        conn, entry,
                        allow_embedding=True,
                        promote_tickers=promote_tickers,
                    )
                except Exception as exc:
                    logger.warning("trending insert failed url=%s: %s", url, exc)
                    continue
                if nid is None:
                    result["duplicates"] += 1
                else:
                    result["inserted"] += 1
                    result["unknown_tickers"] += unknown
    finally:
        conn.close()

    return _done()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--tier", type=int, choices=(1, 2), required=True,
                    help="1 = hot (daily), 2 = tail (every 3 days).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch the ranking and preview the due symbols; no Exa calls, no writes.")
    ap.add_argument("--max-symbols", type=int, default=None,
                    help="Cap symbols scraped this run (bounds cost for testing).")
    ap.add_argument("--time-filter", default="past_week",
                    choices=("past_week", "past_month", "past_year"),
                    help="Exa recency window (default: past_week, the finest the tool offers).")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_trending(
        args.tier,
        dry_run=args.dry_run,
        max_symbols=args.max_symbols,
        time_filter=args.time_filter,
    )

    mode = "dry-run" if args.dry_run else "live"
    print(
        f"\n[trending:{mode}] tier={result['tier']} ok={result['ok']} "
        f"total={result['symbols_total']} in_registry={result['symbols_in_registry']} "
        f"due={result['symbols_due']} scraped={result['symbols_scraped']} "
        f"articles={result['articles']} ins={result['inserted']} "
        f"dup={result['duplicates']} scrape_err={result['scrape_errors']}"
    )
    if result.get("error"):
        print(f"[trending:{mode}] ERROR phase={result['phase']}: {result['error']}", file=sys.stderr)
    if args.dry_run and result.get("due_symbols"):
        print(f"[trending:dry-run] due: {', '.join(result['due_symbols'])}")

    # Live CLI runs write to the same metrics stream the scheduler appends to.
    if not args.dry_run:
        from uuid import uuid4

        from src.pipeline_metrics import append_metric
        append_metric({
            "event": "trending_run",
            "run_id": uuid4().hex[:12],
            "source": "cli",
            **result,
        })
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Firecrawl-based body enrichment for promoted news clusters.

Some RSS feeds (Guardian, FT, Reuters paywall stubs, WSJ, etc.) only emit a
truncated lede in the feed body. When every member of a cluster about to
become a story has a thin body, story synthesis cannot ground claims/quotes
verbatim — quote-verbatim verification then rejects the story (~150 events
in 2 days as of 2026-05-14).

This module runs after a cluster has been admitted for synthesis (so we are
already paying the LLM cost) and before `synthesize_cluster`. It scrapes the
top-N member URLs via Firecrawl when no member body clears a quality floor,
then writes the longer text back to ``news.body_excerpt`` so:

- Synthesis sees real article text (longer ledes, real quotes).
- Quote-verbatim verification can pass.
- The enriched body is persisted so re-runs / downstream readers benefit.

Discovery of which clusters deserve to become stories is unchanged — this is
purely a body-quality rescue.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from dataclasses import replace

from src.config import FIRECRAWL_API_KEY, HF_ENRICH_ALL_PROMOTES
from src.news.types import ClusterSourceDoc

# Firecrawl emits markdown with backslash-escapes (e.g. ``\[is\]``, ``\.``)
# that the LLM unescapes when quoting, breaking the verbatim-quote check.
# Strip the escapes before persisting so quotes can match the body verbatim.
_MD_ESCAPE_RE = re.compile(r"\\([\[\]\(\)\*_#!`>~\.\-+=|{}\\])")

# Firecrawl wraps inline entities/sections in markdown links like
# ``[Federal Reserve](https://www.cnbc.com/federal-reserve/)``. The LLM
# quotes the visible text only, so the verbatim substring check against the
# stored body fails. Reduce ``[text](url)`` (and image variant ``![alt](url)``)
# to just the visible text before persisting.
_MD_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")

# Bodies shorter than this are considered "thin" and trigger enrichment.
# Empirical: 26 of 27 rejected synthesis clusters (2026-05-12 → 05-14) had
# max member body < 1000 chars. The Guardian case at 575 chars still rejects
# because the verbatim title quote sits past the truncation point. 1000
# chars is the smallest threshold that catches the observed failure mode
# without scraping clusters that already have a real article body.
QUALITY_BODY_MIN_CHARS = 1000

# How many member URLs to scrape per cluster (cost cap).
MAX_SCRAPES_PER_CLUSTER = 2

# Cap stored body length to avoid blowing token budgets in synthesis prompts.
BODY_TEXT_CAP_CHARS = 5_000


def _max_body_len(members: list[ClusterSourceDoc]) -> int:
    return max((len(m.body or "") for m in members), default=0)


def _scrape_url(url: str) -> str | None:
    """Firecrawl-scrape one URL and return the extracted text, or None."""
    from src.clients.firecrawl import scrape

    try:
        result = scrape(url, text_max_characters=BODY_TEXT_CAP_CHARS)
    except Exception as exc:
        print(f"  firecrawl: scrape failed url={url} err={exc}", file=sys.stderr)
        return None
    text = (result.get("text") or "").strip()
    if not text:
        return None
    # Unescape markdown punctuation and reduce inline links to their visible
    # text so synthesis quotes can match verbatim.
    text = _MD_ESCAPE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    # Scraped HTML often contains non-breaking spaces (U+00A0) inside quotes
    # ("Law\xa0enforcement officers..."), which the LLM normalizes to a plain
    # space when quoting — also breaks the verbatim check.
    text = text.replace(" ", " ")
    return text[:BODY_TEXT_CAP_CHARS]


def _persist_body(conn: sqlite3.Connection, news_id: str, body: str) -> None:
    with conn:
        conn.execute(
            "UPDATE news SET body_excerpt = ? WHERE id = ?",
            (body, news_id),
        )


def enrich_member_bodies(
    members: list[ClusterSourceDoc],
    *,
    conn: sqlite3.Connection,
    cluster_id: str,
    force: bool = False,
) -> list[ClusterSourceDoc]:
    """Return ``members`` with body_excerpts upgraded via Firecrawl when thin.

    Default gate: skip the whole cluster if any member already has body >=
    QUALITY_BODY_MIN_CHARS. With ``HF_ENRICH_ALL_PROMOTES=1`` (or
    ``force=True``), still scrape up to MAX_SCRAPES_PER_CLUSTER thin members
    even when another member is long — tier-1 RSS stubs often need a scrape.

    Failures are non-fatal: the original members are returned with whatever
    bodies were successfully fetched. Synthesis runs either way.
    """
    if not members:
        return members

    enrich_all = force or HF_ENRICH_ALL_PROMOTES
    before_max = _max_body_len(members)
    if not enrich_all and before_max >= QUALITY_BODY_MIN_CHARS:
        print(
            f"  firecrawl: skipped cluster={cluster_id} max_body={before_max}",
        )
        return members

    if not FIRECRAWL_API_KEY:
        print(
            f"  firecrawl: skipped cluster={cluster_id} reason=no_api_key max_body={before_max}",
            file=sys.stderr,
        )
        return members

    enriched: list[ClusterSourceDoc] = list(members)
    scrapes_attempted = 0
    scrapes_ok = 0
    upgraded_total_chars = 0
    started = time.monotonic()

    for idx, member in enumerate(enriched):
        if scrapes_attempted >= MAX_SCRAPES_PER_CLUSTER:
            break
        url = (member.url or "").strip()
        if not url:
            continue
        existing_len = len(member.body or "")
        if existing_len >= QUALITY_BODY_MIN_CHARS:
            # Already long enough — no need to scrape this one.
            continue
        scrapes_attempted += 1
        text = _scrape_url(url)
        if not text:
            continue
        if len(text) <= existing_len:
            # Scrape returned nothing better than we already had.
            continue
        scrapes_ok += 1
        upgraded_total_chars += len(text) - existing_len
        enriched[idx] = replace(member, body=text)
        _persist_body(conn, member.news_id, text)

    after_max = _max_body_len(enriched)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    mode = "all_promotes" if enrich_all else "thin_only"
    print(
        f"  firecrawl: enriched cluster={cluster_id} mode={mode} "
        f"attempted={scrapes_attempted} ok={scrapes_ok} "
        f"max_body_before={before_max} max_body_after={after_max} "
        f"elapsed_ms={elapsed_ms}"
    )
    return enriched


__all__ = [
    "BODY_TEXT_CAP_CHARS",
    "MAX_SCRAPES_PER_CLUSTER",
    "QUALITY_BODY_MIN_CHARS",
    "enrich_member_bodies",
]

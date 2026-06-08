#!/usr/bin/env python3
"""News firehose ingestion — press-wire RSS poller (Phase 1).

Polls 20 press-wire RSS feeds (PR Newswire industry feeds + GlobeNewswire
subject feeds), runs the three-tier ticker-tagging gate, and writes passing
items to the firehose lane (`news` rows with `headline IS NOT NULL`).

No LLM calls. Idempotent via UNIQUE INDEX on `news.source_url`.

Usage:
    uv run python -m agents.firehose                    # all feeds
    uv run python -m agents.firehose --feeds 0 1 2      # subset by index
    uv run python -m agents.firehose --dry-run          # parse + gate, no writes
    uv run python -m agents.firehose --max-items 5      # cap per-feed for testing
"""
from __future__ import annotations

from collections.abc import Callable

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import feedparser
from curl_cffi import requests as curl_requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.instruments.resolver import exists as instrument_exists
from src.news.firehose_gate import (
    LAWYER_SPAM,
    MATERIALITY_HOME_THRESHOLD,
    build_alias_index,
    score_materiality,
    strip_html,
    tag_text,
)
from src.news.cluster import attach_news_item, event_class_from_labels, headline_hash
from src.news.publishers import publisher_for_url
from src.pipeline_metrics import append_metric, top_counts

DB_PATH = ROOT / "db" / "hf.db"
logger = logging.getLogger(__name__)

FEED_USER_AGENT = "Mozilla/5.0 (compatible; HF-Workbench/1.0; +https://heurist.xyz)"
FEED_FETCH_TIMEOUT_S = 20
RUN_FIREHOSE_MAX_WALL_S = 480.0

# Validated by scripts/smoke_press_wires.py (May 2026): ~308 unique items/day,
# 40% gate pass rate, 91% of passes via the structural (EXCHANGE: TICKER)
# detector. ACCESS Newswire is Cloudflare-blocked; Business Wire's public
# RSS endpoint is dead — both omitted.
PRESS_WIRE_FEEDS: tuple[str, ...] = (
    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    "https://www.prnewswire.com/rss/general-business-latest-news/general-business-latest-news-list.rss",
    "https://www.prnewswire.com/rss/energy-latest-news/energy-latest-news-list.rss",
    "https://www.prnewswire.com/rss/health-latest-news/health-latest-news-list.rss",
    "https://www.prnewswire.com/rss/automotive-transportation-latest-news/automotive-transportation-latest-news-list.rss",
    "https://www.prnewswire.com/rss/consumer-technology-latest-news/consumer-technology-latest-news-list.rss",
    "https://www.prnewswire.com/rss/consumer-products-retail-latest-news/consumer-products-retail-latest-news-list.rss",
    "https://www.prnewswire.com/rss/policy-public-interest-latest-news/policy-public-interest-latest-news-list.rss",
    "https://www.prnewswire.com/rss/telecommunications-latest-news/telecommunications-latest-news-list.rss",
    "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    "https://www.globenewswire.com/RssFeed/subjectcode/13-Earnings%20Releases%20And%20Operating%20Results/feedTitle/GlobeNewswire%20-%20Earnings",
    "https://www.globenewswire.com/RssFeed/subjectcode/27-Mergers%20and%20Acquisitions/feedTitle/GlobeNewswire%20-%20Mergers%20and%20Acquisitions",
    "https://www.globenewswire.com/RssFeed/subjectcode/7-Business%20Contracts/feedTitle/GlobeNewswire%20-%20Business%20Contracts",
    "https://www.globenewswire.com/RssFeed/subjectcode/17-Financing%20Agreements/feedTitle/GlobeNewswire%20-%20Financing%20Agreements",
    "https://www.globenewswire.com/RssFeed/subjectcode/21-Initial%20Public%20Offerings/feedTitle/GlobeNewswire%20-%20IPOs",
    "https://www.globenewswire.com/RssFeed/subjectcode/12-Dividend%20Reports%20And%20Estimates/feedTitle/GlobeNewswire%20-%20Dividends",
    "https://www.globenewswire.com/RssFeed/subjectcode/9-Company%20Announcement/feedTitle/GlobeNewswire%20-%20Company%20Announcement",
    "https://www.globenewswire.com/RssFeed/subjectcode/11-Directors%20And%20Officers/feedTitle/GlobeNewswire%20-%20Directors",
    "https://www.globenewswire.com/RssFeed/subjectcode/76-Equity%20Market%20Information/feedTitle/GlobeNewswire%20-%20Equity%20Market%20Information",
)

# Tier A — macro & first-source feeds (Phase 6.1). Validated by
# `scripts/smoke_macro_feeds.py` (May 2026): combined ~6/day, 67% pass
# rate, marquee items (FOMC/ECB/BOE/RBA decisions, GDP advance,
# EIA Annual Energy Outlook) score 40–100; admin items stay at default
# 15 and are filtered by the home-feed threshold.
#
# US Treasury press is dropped — no public RSS endpoint exists.
MACRO_FEEDS: tuple[str, ...] = (
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "https://apps.bea.gov/rss/rss.xml",
    "https://www.ecb.europa.eu/rss/press.html",
    "https://www.eia.gov/rss/press_rss.xml",
    "https://www.bankofengland.co.uk/rss/news",
    "https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
)

# Tier A regulatory — agency press feeds (Phase 6.2). Validated by
# `scripts/smoke_regulatory_feeds.py` (May 2026): FDA approvals score
# 50–55 via T3 `regulatory`, FTC enforcement 55–100, DOJ admin items
# correctly drop to 0 via `regulatory_admin` and `individual_crime`
# noise patterns.
#
# Dropped after probe: SEC litreleases (404), DOJ Antitrust subfeed
# (404), FDA drug approvals (404), CFTC PressRoom (returns HTML, no
# entries). Antitrust + enforcement coverage flows through the SEC
# press feeds and DOJ via the Google News proxy below.
#
# Replaced May 2026: justice.gov/feeds/justice-news.xml — endpoint now
# emits OIP admin items ("FY26 Q4 Data Due", "FOIA Training") instead
# of press releases. Swapped to Google News `source:justice.gov`
# (13+ real DOJ items/day, latest within minutes).
REGULATORY_FEEDS: tuple[str, ...] = (
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch/rss.xml",
    "https://www.sec.gov/news/pressreleases.rss",
    # SEC speeches + statements: commissioner / staff policy commentary.
    # Most items are reference material and drop at the gate; the few
    # that signal regulatory shifts ("Chairman on crypto", "Remarks on
    # private fund rules") pass via existing keyword patterns.
    "https://www.sec.gov/news/speeches.rss",
    "https://www.sec.gov/news/statements.rss",
    "https://www.ftc.gov/feeds/press-release.xml",
    "https://news.google.com/rss/search?q=when:1d+source:justice.gov&hl=en-US&gl=US&ceid=US:en",
)

# Tier B majors + selected trade and regional sources. Some publishers expose
# broad RSS only; the publisher registry carries the sector/region priors.
#
# Pruned May 2026:
#   - feeds.apnews.com/apf-business — DNS dead (AP retired the feed host).
#   - api.axios.com/feed/business — 404; Axios collapsed to the root /feed/
#     endpoint, moved to TIER_A_NEWS_FEEDS below.
#   - www.ft.com/rss/home/us — 0 entries; replaced with /rss/companies and
#     /rss/markets in TIER_A_NEWS_FEEDS.
TIER_B_FEEDS: tuple[str, ...] = (
    "https://finance.yahoo.com/news/rssindex",
    "https://www.marketwatch.com/rss/topstories",
    "https://www.marketwatch.com/rss/marketpulse",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.cnbc.com/id/15839135/device/rss/rss.html",
    "https://seekingalpha.com/market_currents.xml",
    # Additional news-org feeds to lift independent-publisher diversity.
    # Each adds a distinct independence_group on cross-event coverage.
    "http://feeds.bbci.co.uk/news/business/rss.xml",
    "https://www.theguardian.com/uk/business/rss",
    "https://rss.politico.com/economy.xml",
    # US-stock-trader-focused outlets. Probed May 2026 (smoke_tier_b_feeds.py):
    # IBD root feed carries 100 items / pull at 52% gate pass rate ("IBD 50",
    # "Dow Jones Futures", "Stock Market Week Ahead" — the swing-trade frame).
    # Benzinga markets + news pull 15 each at ~67% pass rate (mid-cap catalysts,
    # biotech P3 readouts, Wall Street commentary that PR wires don't carry).
    # Avoid Benzinga's root /feed — it returns 10/10 crypto-price-prediction
    # spam at mat=0. Both publishers add fresh independence_groups for R2/R2b
    # corroboration on US-equity clusters.
    "https://www.investors.com/feed/",
    "https://www.benzinga.com/markets/feed",
    "https://www.benzinga.com/news/feed",
)

# Tier A news wires (Phase 6.3). Real tier-1 / wire reporting outlets — the
# unblock for routing rule R1 (max_materiality>=30 AND has_tier1_primary)
# and R0c (>=3 independent groups + tier1).
#
# Replaced May 2026 audit:
#   - All 4 feeds.a.dj.com WSJ feeds returned 200 but pinned to Jan 2025
#     (16 mo stale). Swapped for Google News `source:wsj.com` proxy
#     (~31 items/day, latest within hours).
#   - api.axios.com/feed/ returns HTTP 403 Cloudflare block.
#     Swapped for Google News `source:axios.com` proxy (~27 items/day).
#   - Reuters and AP RSS were retired by those publishers in 2020;
#     re-added via Google News proxy (Reuters ~27/day, AP ~56/day).
#     This restores the two largest Tier-1 wires and is the primary
#     driver for overnight `independent_pub_count >= 3` corroboration.
#
# Google News proxy format: each <item> carries a <source url="..."/> tag
# the parser uses to attribute back to the canonical publisher (Reuters,
# AP, WSJ, Axios). The Google redirect URL is stored as source_url; dedup
# remains correct because Google's article ID is deterministic per story.
TIER_A_NEWS_FEEDS: tuple[str, ...] = (
    # Reuters + AP via Google News (direct RSS retired by publishers).
    "https://news.google.com/rss/search?q=when:1d+source:reuters.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=when:1d+source:apnews.com&hl=en-US&gl=US&ceid=US:en",
    # WSJ via Google News (dj.com syndication feeds pinned 16 mo stale).
    "https://news.google.com/rss/search?q=when:1d+source:wsj.com&hl=en-US&gl=US&ceid=US:en",
    # NYT — open RSS, deep coverage.
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/EnergyEnvironment.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/DealBook.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    # Bloomberg public RSS (markets + economics sections).
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.bloomberg.com/economics/news.rss",
    # FT — companies + markets sections (home/us returns empty).
    "https://www.ft.com/rss/companies",
    "https://www.ft.com/rss/markets",
    # The Economist — business + finance sections. Wide archive window;
    # dedupe via news.source_url drops most repeats per pull.
    "https://www.economist.com/business/rss.xml",
    "https://www.economist.com/finance-and-economics/rss.xml",
    # CNBC — additional topic feeds beyond the two already in TIER_B.
    "https://www.cnbc.com/id/19746125/device/rss/rss.html",  # earnings
    "https://www.cnbc.com/id/10001147/device/rss/rss.html",  # economy
    "https://www.cnbc.com/id/10000664/device/rss/rss.html",  # finance
    # Axios via Google News (direct /feed/ endpoint Cloudflare-blocked).
    "https://news.google.com/rss/search?q=when:1d+source:axios.com&hl=en-US&gl=US&ceid=US:en",
    # Washington Post — low volume but adds an independence group.
    "https://feeds.washingtonpost.com/rss/business",
    "https://feeds.washingtonpost.com/rss/business/economy",
)

# Removed May 2026 audit:
#   - anandtech.com/rss/ — site shuttered (forum-only HTML now).
#   - mining.com/feed/ — HTTP 403 Cloudflare anti-bot.
#   - freightwaves.com/news/feed — RSS valid but pinned at 2022-05-17
#     (4 years stale).
TRADE_PRESS_FEEDS: tuple[str, ...] = (
    "https://www.eetimes.com/feed/",
    "https://www.defensenews.com/arc/outboundfeeds/rss/",
    "https://breakingdefense.com/feed/",
    "https://news.usni.org/feed",
    "https://www.fiercepharma.com/rss/xml",
    "https://www.biopharmadive.com/feeds/news/",
    "https://oilprice.com/rss/main",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    # The Block — 3rd crypto independence_group (alongside CoinDesk + Decrypt).
    # Directly unlocks R2 (>=3 indep + mat>=25) on crypto-event clusters that
    # today stall at two pubs. ~20 items/pull, 45% gate pass rate.
    "https://www.theblock.co/rss.xml",
    "https://electrek.co/feed/",
    "https://insideevs.com/rss/news/all/",
    "https://cnevpost.com/feed/",
    "https://www.gamedeveloper.com/rss.xml",
)

# Removed May 2026 audit:
#   - chinadaily.com.cn/rss/bizchina_rss.xml — feed frozen at 2017 items.
REGIONAL_FEEDS: tuple[str, ...] = (
    "https://asia.nikkei.com/rss/feed/nar",
    "https://www.scmp.com/rss/92/feed",
    "https://www.ecb.europa.eu/rss/press.html",
    "https://www.bankofengland.co.uk/rss/news",
    "https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
)

# Removed May 2026 audit:
#   - whitehouse.gov/feed/ — HTTP 404 (endpoint removed).
#   - ustr.gov/.../press-releases/feed — HTTP 404 (endpoint removed).
# Both replaced via Google News proxies below.
POLITICS_MARKET_FEEDS: tuple[str, ...] = (
    "https://news.google.com/rss/search?q=when:1d+source:whitehouse.gov&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=when:1d+source:ustr.gov&hl=en-US&gl=US&ceid=US:en",
)

# Tier A BLS prints (Phase 6.2). Akamai blocks default feedparser UAs
# with HTTP 403; curl_cffi's Chrome impersonation (matching JA3 + ALPN)
# bypasses the bot wall and returns the genuine RSS. These are the four
# most market-moving US macro releases — first-source numbers minutes
# ahead of any wire re-wrap.
BLS_FEEDS: tuple[str, ...] = (
    "https://www.bls.gov/feed/empsit.rss",   # nonfarm payrolls / unemployment
    "https://www.bls.gov/feed/cpi.rss",      # consumer prices
    "https://www.bls.gov/feed/ppi.rss",      # producer prices
    "https://www.bls.gov/feed/jolts.rss",    # job openings
)

# Hosts that require curl_cffi browser-fingerprint impersonation. Default
# urllib + feedparser hits HTTP 403 from these origins regardless of UA
# header. Fetch path branches on `urlparse(url).netloc.endswith(host)`.
IMPERSONATE_HOSTS: frozenset[str] = frozenset(
    {"bls.gov", "www.bls.gov", "investors.com", "www.investors.com"}
)

# Hosts whose RSS feeds emit titles only (no <summary>/<description> body).
# Validated 2026-05-15: Yahoo Finance 99% empty bodies (1426/1435/7d),
# SeekingAlpha 98%, CoinDesk 100%, Nikkei 100%. For these, fetch the
# article's <meta name|property="(og:|twitter:)?description"> tag at
# ingest time so the materiality gate has body text to score against.
# Without this, half the firehose enters with zero body context and the
# regex patterns only see the headline — missing real signal like
# "Silver Plunges on Inflation Worries" or "Treasury Yields Jump Amid
# Fed Shift" whose bodies disambiguate the event.
META_DESCRIPTION_HOSTS: frozenset[str] = frozenset({
    "finance.yahoo.com",
    "seekingalpha.com",
    "www.coindesk.com",
    "coindesk.com",
    "asia.nikkei.com",
    "www.nikkei.com",
})

# Match <meta name="description" content="..."> and the og:/twitter: variants.
# Quoted-attribute form covers all four publishers' page templates.
_META_DESC_RE = re.compile(
    r'<meta\s+(?:name|property)\s*=\s*["\'](?:og:description|twitter:description|description)["\']\s+content\s*=\s*["\']([^"\']*)["\']',
    re.I,
)
# Same pattern with content first (some pages emit attributes in the
# opposite order).
_META_DESC_RE_ALT = re.compile(
    r'<meta\s+content\s*=\s*["\']([^"\']*)["\']\s+(?:name|property)\s*=\s*["\'](?:og:description|twitter:description|description)["\']',
    re.I,
)

# Cap meta-description fetches per firehose run so a hung host can't stall
# the pipeline. ~265 known-empty-body items/day across ALL feeds means
# ~45/cycle on 10-min cadence — well below this ceiling.
META_FETCH_TIMEOUT_S = 4
META_FETCH_MAX_PER_RUN = 200

ALL_FEEDS: tuple[str, ...] = (
    PRESS_WIRE_FEEDS
    + MACRO_FEEDS
    + REGULATORY_FEEDS
    + BLS_FEEDS
    + TIER_A_NEWS_FEEDS
    + TIER_B_FEEDS
    + TRADE_PRESS_FEEDS
    + REGIONAL_FEEDS
    + POLITICS_MARKET_FEEDS
)

BODY_EXCERPT_CHARS = 1000


@dataclass(slots=True)
class FirehoseEntry:
    source_url: str
    headline: str
    body_excerpt: str
    published_at: str
    publisher: str
    exchange_tickers: set[str]
    registry_symbols: set[str]
    macros: set[str]
    is_lawyer_spam: bool
    materiality_score: int
    event_classes: list[str]


@dataclass(slots=True)
class IngestStats:
    feeds_polled: int = 0
    raw_items: int = 0
    duplicates: int = 0           # already in news.source_url
    gate_dropped: int = 0
    inserted: int = 0
    inserted_spam: int = 0
    unknown_tickers: int = 0      # symbols logged to pending_instruments
    low_materiality: int = 0      # inserted but score < home threshold
    inserts_by_publisher: Counter[str] = field(default_factory=Counter)
    wall_clock_exceeded: bool = False


def _publisher_for(url: str) -> str:
    return publisher_for_url(url).name


def _format_published(entry) -> str:
    """Always returns a non-empty ISO timestamp in UTC. Falls back to ingest
    time when the feed entry has no parseable date, so readers can rely on
    `news.published_at` as the single signal-ordering field.

    The trailing 'Z' is critical: without it, JS `new Date()` parses the
    string as local time, producing timezone-sized offsets in the UI."""
    pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if pub_struct:
        try:
            # feedparser's *_parsed structs are always UTC.
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", pub_struct)
        except (TypeError, ValueError, OverflowError):
            pass
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _next_news_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM news ORDER BY CAST(SUBSTR(id, 6) AS INTEGER) DESC LIMIT 1"
    ).fetchone()
    best = 0
    if row:
        try:
            best = int(row[0].split("_", 1)[1])
        except (IndexError, ValueError):
            best = 0
    return f"news_{best + 1:03d}"


def _needs_impersonation(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host.endswith(h) for h in IMPERSONATE_HOSTS)


def _fetch_feed(url: str):
    """Fetch + parse a single RSS URL via curl_cffi with a hard timeout.

    All feeds go through curl_cffi — never feedparser's built-in urllib
    fetch, which has no socket timeout and can hang indefinitely. BLS
    (and any other entry in `IMPERSONATE_HOSTS`) needs browser TLS
    impersonation to bypass Akamai's bot wall.
    """
    opts = (
        {"impersonate": "chrome"}
        if _needs_impersonation(url)
        else {"headers": {"User-Agent": FEED_USER_AGENT}}
    )
    resp = curl_requests.get(url, timeout=FEED_FETCH_TIMEOUT_S, **opts)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def _resolve_google_news_entry(ent) -> tuple[str | None, str | None]:
    """For Google News RSS items, the <source> tag carries the canonical
    publisher (Reuters/AP/WSJ/Axios/etc.) and that publisher's homepage
    URL. Without this resolution every item would attribute to
    'News.Google' and lose its tier1 / independence-group classification.

    Returns (publisher_name, publisher_url) or (None, None) if absent.
    """
    src = ent.get("source") or {}
    pub_name = (src.get("title") or "").strip() or None
    pub_url = (src.get("href") or "").strip() or None
    return pub_name, pub_url


def _strip_publisher_suffix(title: str, publisher_name: str | None) -> str:
    """Google News titles always end with ' - PublisherName'. Strip it so
    headline_hash clustering matches items from direct Reuters/AP feeds.
    """
    if not publisher_name:
        return title
    suffix = f" - {publisher_name}"
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title


def parse_feed(
    url: str,
    ci_index: dict[str, set[str]],
    cs_index: dict[str, set[str]],
    *,
    max_items: int | None = None,
) -> list[FirehoseEntry]:
    parsed = _fetch_feed(url)
    publisher_default = _publisher_for(url)
    is_google_news = urlparse(url).netloc.lower().endswith("news.google.com")
    out: list[FirehoseEntry] = []
    entries = parsed.entries or []
    if max_items is not None:
        entries = entries[:max_items]
    for ent in entries:
        link = (ent.get("link") or "").strip()
        if not link:
            continue
        title = (ent.get("title") or "").strip()
        if not title:
            continue
        # Google News proxy: attribute to the canonical publisher named in
        # the <source> tag, not to "News.Google". This is what lets
        # Reuters/AP items flow into Tier-1 routing rules.
        if is_google_news:
            gn_name, gn_url = _resolve_google_news_entry(ent)
            if gn_url:
                entry_publisher = publisher_for_url(gn_url).name
            elif gn_name:
                entry_publisher = gn_name
            else:
                entry_publisher = publisher_default
            title = _strip_publisher_suffix(title, gn_name or entry_publisher)
        else:
            entry_publisher = publisher_default
        body = strip_html(ent.get("summary") or ent.get("description") or "")
        ex, syms, macros = tag_text(title, body, ci_index, cs_index)
        score, classes = score_materiality(title, body, publisher=entry_publisher)
        if not (ex or syms or macros):
            out.append(FirehoseEntry(
                source_url=link,
                headline=title,
                body_excerpt=body[:BODY_EXCERPT_CHARS],
                published_at=_format_published(ent),
                publisher=entry_publisher,
                exchange_tickers=ex,
                registry_symbols=syms,
                macros=macros,
                is_lawyer_spam=False,
                materiality_score=score,
                event_classes=classes,
            ))
            continue
        is_spam = bool(LAWYER_SPAM.search(title))
        publisher = (
            f"{entry_publisher}-classaction" if is_spam else entry_publisher
        )
        out.append(FirehoseEntry(
            source_url=link,
            headline=title,
            body_excerpt=body[:BODY_EXCERPT_CHARS],
            published_at=_format_published(ent),
            publisher=publisher,
            exchange_tickers=ex,
            registry_symbols=syms,
            macros=macros,
            is_lawyer_spam=is_spam,
            materiality_score=score,
            event_classes=classes,
        ))
    return out


def insert_entry(
    conn: sqlite3.Connection,
    entry: FirehoseEntry,
    *,
    allow_embedding: bool = False,
    promote_tickers: frozenset[str] = frozenset(),
) -> tuple[str | None, int]:
    """Insert a gated entry. Returns (news_id, unknown_ticker_count).

    news_id is None when the entry is a duplicate (matches an existing
    `news.source_url`) or didn't pass the gate. unknown_ticker_count is the
    number of symbols logged to `pending_instruments` (registry-unknown).

    Single transaction: dedup check + news row + entity_tickers +
    pending_instruments rows.

    `allow_embedding` / `promote_tickers` control clustering. The firehose
    leaves them at the default (cheap headline-hash + lexical passes only). The
    trending lane sets `allow_embedding=True` and passes its hot symbols so
    same-event articles from different outlets attach semantically instead of
    fragmenting into singletons — that's what lets per-outlet corroboration
    survive into the diversity/promotion gates.
    """
    if not (entry.exchange_tickers or entry.registry_symbols or entry.macros):
        return None, 0
    unknown_count = 0
    with conn:
        existing = conn.execute(
            "SELECT id FROM news WHERE source_url = ?",
            (entry.source_url,),
        ).fetchone()
        if existing:
            return None, 0
        news_id = _next_news_id(conn)
        conn.execute(
            """INSERT INTO news
               (id, sources_json, sectors_json, regions_json, published_at, created_at, headline,
                body_excerpt, source_url, publisher,
                materiality_score, event_classes, headline_hash, event_class)
               VALUES (?, '[]', ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                news_id,
                json.dumps([]),
                json.dumps([]),
                entry.published_at,
                entry.headline,
                entry.body_excerpt,
                entry.source_url,
                entry.publisher,
                entry.materiality_score,
                json.dumps(entry.event_classes),
                headline_hash(entry.headline),
                event_class_from_labels(entry.event_classes),
            ),
        )
        # T1 verbatim exchange tickers + T2 canonical registry symbols.
        # T3 macros are a gate signal only — not stored as tickers.
        # Registry-unknowns still go into entity_tickers (preserves the
        # ticker-overlap signal for future N×M matching) AND get logged to
        # pending_instruments for weekly registry adoption review. The
        # display filter hides registry-unknown chips so the strip stays
        # clean for users.
        for sym in sorted(entry.exchange_tickers | entry.registry_symbols):
            sym = sym.strip().upper()
            if not sym:
                continue
            conn.execute(
                """INSERT INTO entity_tickers (entity_type, entity_id, symbol)
                   VALUES ('news', ?, ?)
                   ON CONFLICT(entity_type, entity_id, symbol) DO NOTHING""",
                (news_id, sym),
            )
            if not instrument_exists(sym):
                conn.execute(
                    """INSERT INTO pending_instruments
                       (symbol, source, source_id, first_seen_at, last_seen_at)
                       VALUES (?, 'firehose', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                       ON CONFLICT(symbol, source) DO UPDATE SET
                         last_seen_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                         seen_count = pending_instruments.seen_count + 1""",
                    (sym, news_id),
                )
                unknown_count += 1
        try:
            attach_news_item(
                conn,
                news_id,
                active_thesis_tickers=set(promote_tickers),
                allow_embedding=allow_embedding,
            )
        except Exception as exc:
            logger.warning("firehose failed to shadow-cluster %s: %s", news_id, exc)
        return news_id, unknown_count


def _should_meta_enrich(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in META_DESCRIPTION_HOSTS


def _fetch_meta_description(url: str) -> str:
    """Fetch the article's HTML and extract og:/twitter:/description meta tag.

    Returns "" on any failure (timeout, non-200, no meta tag found). The
    caller must treat the empty string as "no enrichment available" and
    fall through to the original body. curl_cffi's Chrome impersonation
    is required for Yahoo Finance / SeekingAlpha — both 403 plain UAs.
    """
    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome",
            timeout=META_FETCH_TIMEOUT_S,
            allow_redirects=True,
        )
    except Exception:
        return ""
    if resp.status_code != 200 or not resp.text:
        return ""
    # Scan the full response: Yahoo Finance pages emit ~58k bytes of
    # inline SSR JSON before the <head> meta tags, and SeekingAlpha
    # places its description past byte 420k. A prefix-only scan misses
    # both. Regex over the full string is still sub-ms.
    body = resp.text
    match = _META_DESC_RE.search(body) or _META_DESC_RE_ALT.search(body)
    if not match:
        return ""
    return unescape(match.group(1)).strip()


def _is_existing_url(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM news WHERE source_url = ? LIMIT 1",
        (url,),
    ).fetchone()
    return row is not None


def _enrich_and_rescore(
    entry: FirehoseEntry,
    ci_index: dict[str, set[str]],
    cs_index: dict[str, set[str]],
) -> FirehoseEntry:
    """Fetch meta description, re-tag and re-score with the longer body.

    Re-running ``tag_text`` is important: the structural ticker detector
    (T1) and the alias matcher (T2) both scan the body, and many empty-
    body items only carry ticker context in the article's lede, not the
    headline. Re-tagging may also flip the spam classification.
    """
    new_body = _fetch_meta_description(entry.source_url)
    if not new_body:
        return entry
    body = new_body[:BODY_EXCERPT_CHARS]
    ex, syms, macros = tag_text(entry.headline, body, ci_index, cs_index)
    score, classes = score_materiality(
        entry.headline, body, publisher=entry.publisher
    )
    is_spam = bool(LAWYER_SPAM.search(entry.headline))
    return replace(
        entry,
        body_excerpt=body,
        exchange_tickers=ex,
        registry_symbols=syms,
        macros=macros,
        is_lawyer_spam=is_spam,
        materiality_score=score,
        event_classes=classes,
    )


def run_firehose(
    feeds: list[str],
    *,
    db_path: Path = DB_PATH,
    max_items: int | None = None,
    dry_run: bool = False,
    should_stop: Callable[[], bool] = lambda: False,
    max_wall_s: float | None = RUN_FIREHOSE_MAX_WALL_S,
) -> IngestStats:
    ci_index, cs_index = build_alias_index()
    stats = IngestStats()
    conn = sqlite3.connect(db_path, timeout=10) if not dry_run else None
    meta_fetched = 0
    deadline = (
        time.monotonic() + max_wall_s
        if max_wall_s is not None and max_wall_s > 0
        else None
    )

    def _abort_requested() -> bool:
        if should_stop():
            return True
        if deadline is not None and time.monotonic() >= deadline:
            stats.wall_clock_exceeded = True
            return True
        return False

    try:
        for url in feeds:
            if _abort_requested():
                break
            stats.feeds_polled += 1
            try:
                entries = parse_feed(url, ci_index, cs_index, max_items=max_items)
            except Exception as exc:  # feedparser is liberal but bad URLs raise
                logger.warning("firehose feed fetch failed url=%s: %s", url, exc)
                continue
            for entry in entries:
                if _abort_requested():
                    break
                # Counted here (not len(entries) up front) so an abort
                # mid-feed doesn't credit entries we never processed.
                stats.raw_items += 1
                # Body enrichment for known-empty-body hosts. Runs BEFORE
                # the tag-gate so an item that only carries ticker
                # context in its lede (not the headline) still has a
                # chance to enter the firehose. Dedup-aware to avoid
                # paying the HTTP cost on items already persisted, and
                # capped per-run to bound worst-case latency.
                if (
                    not entry.body_excerpt
                    and _should_meta_enrich(entry.source_url)
                    and meta_fetched < META_FETCH_MAX_PER_RUN
                    and (conn is None or not _is_existing_url(conn, entry.source_url))
                ):
                    meta_fetched += 1
                    entry = _enrich_and_rescore(entry, ci_index, cs_index)
                if not (entry.exchange_tickers or entry.registry_symbols or entry.macros):
                    stats.gate_dropped += 1
                    continue
                if dry_run:
                    stats.inserted += 1
                    if entry.is_lawyer_spam:
                        stats.inserted_spam += 1
                    if entry.materiality_score < MATERIALITY_HOME_THRESHOLD:
                        stats.low_materiality += 1
                    continue
                nid, unknown = insert_entry(conn, entry)
                if nid is None:
                    stats.duplicates += 1
                else:
                    stats.inserted += 1
                    stats.inserts_by_publisher[entry.publisher] += 1
                    stats.unknown_tickers += unknown
                    if entry.is_lawyer_spam:
                        stats.inserted_spam += 1
                    if entry.materiality_score < MATERIALITY_HOME_THRESHOLD:
                        stats.low_materiality += 1
    finally:
        if conn is not None:
            conn.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and gate, but don't write to the DB.")
    ap.add_argument("--max-items", type=int, default=None,
                    help="Per-feed entry cap (default: feedparser's natural limit).")
    ap.add_argument("--feeds", type=int, nargs="+", metavar="IDX",
                    help=f"Subset of feed indexes 0..{len(ALL_FEEDS) - 1} "
                         "(default: all). Wires are 0..{len(PRESS_WIRE_FEEDS)-1}, "
                         "macro feeds follow.")
    ap.add_argument(
        "--feed-set",
        choices=("all", "wires", "macro", "regulatory", "bls", "tier-a-news", "tier-b", "trade", "regional", "politics"),
        default="all",
        help="Convenience selector: only press wires, only macro (Tier A "
             "central banks + EIA + BEA), only regulatory (FDA/SEC/FTC/DOJ), "
             "only BLS (Empsit/CPI/PPI/JOLTS via curl_cffi), only tier-a-news "
             "(WSJ/NYT/Bloomberg/FT/Economist/CNBC/Axios/WaPo wires), or all "
             "(default).",
    )
    args = ap.parse_args()

    if args.feed_set == "wires":
        pool = PRESS_WIRE_FEEDS
    elif args.feed_set == "macro":
        pool = MACRO_FEEDS
    elif args.feed_set == "regulatory":
        pool = REGULATORY_FEEDS
    elif args.feed_set == "bls":
        pool = BLS_FEEDS
    elif args.feed_set == "tier-a-news":
        pool = TIER_A_NEWS_FEEDS
    elif args.feed_set == "tier-b":
        pool = TIER_B_FEEDS
    elif args.feed_set == "trade":
        pool = TRADE_PRESS_FEEDS
    elif args.feed_set == "regional":
        pool = REGIONAL_FEEDS
    elif args.feed_set == "politics":
        pool = POLITICS_MARKET_FEEDS
    else:
        pool = ALL_FEEDS

    if args.feeds:
        try:
            feeds = [pool[i] for i in args.feeds]
        except IndexError:
            print(f"Feed index out of range (0..{len(pool) - 1}).",
                  file=sys.stderr)
            return 2
    else:
        feeds = list(pool)

    started = time.perf_counter()
    stats = run_firehose(
        feeds,
        max_items=args.max_items,
        dry_run=args.dry_run,
    )
    duration = round(time.perf_counter() - started, 3)

    mode = "dry-run" if args.dry_run else "live"
    print(f"\n[firehose:{mode}] feeds={stats.feeds_polled} "
          f"raw={stats.raw_items} dropped={stats.gate_dropped} "
          f"dup={stats.duplicates} ins={stats.inserted} "
          f"spam={stats.inserted_spam} unknown={stats.unknown_tickers} "
          f"low_mat={stats.low_materiality}")

    # Live runs (including manual CLI invocations) write to the same metrics
    # log the scheduler appends to, so 5.8 alerting and post-hoc analysis see
    # one stream. Dry runs are excluded.
    if not args.dry_run:
        payload = asdict(stats)
        inserts_by_pub = payload.pop("inserts_by_publisher", {})
        append_metric({
            "event": "firehose_run",
            "run_id": uuid4().hex[:12],
            "ok": True,
            "duration_s": duration,
            "source": "cli",
            "inserts_by_publisher": dict(inserts_by_pub)
            if isinstance(inserts_by_pub, Counter)
            else inserts_by_pub,
            "inserts_by_publisher_top": top_counts(
                inserts_by_pub if isinstance(inserts_by_pub, Counter) else Counter()
            ),
            **payload,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

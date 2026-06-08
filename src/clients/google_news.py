"""Google News RSS query client.

Free, no API key, no quota. Returns recent articles for a query, sourced from
the publishers Google News indexes. RSS `<link>` values are encoded Google
redirect URLs (`news.google.com/rss/articles/CBMi...`) — we resolve them to
publisher canonical URLs via `googlenewsdecoder`, which calls Google's
`batchexecute` endpoint to decode the protobuf-encoded ID. Items where the
decoder fails are dropped (they cannot be scraped directly because Google
returns 400 on the encoded URL).
"""

from __future__ import annotations

import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import feedparser
from googlenewsdecoder import gnewsdecoder


@dataclass(slots=True)
class GoogleNewsItem:
    title: str
    link: str  # publisher canonical URL after redirect resolution
    publisher: str
    published: str | None


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
DECODE_INTERVAL_SECONDS = 1
DECODE_WORKERS = 4


def _build_url(query: str, *, hl: str = "en-US", gl: str = "US") -> str:
    params = {"q": query, "hl": hl, "gl": gl, "ceid": f"{gl}:{hl.split('-')[0]}"}
    return f"{GOOGLE_NEWS_RSS}?{urllib.parse.urlencode(params)}"


def _decode(url: str) -> str | None:
    """Resolve one encoded Google News URL to the publisher URL, or None on failure."""
    if "news.google.com" not in url:
        return url
    try:
        out = gnewsdecoder(url, interval=DECODE_INTERVAL_SECONDS)
    except Exception:
        return None
    if not isinstance(out, dict) or not out.get("status"):
        return None
    decoded = out.get("decoded_url")
    return str(decoded) if decoded else None


def _published_from_entry(ent: feedparser.FeedParserDict) -> str | None:
    pub = ent.get("published_parsed") or ent.get("updated_parsed")
    if not pub:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", pub)
    except (TypeError, ValueError, OverflowError):
        return None


def _publisher_from_entry(ent: feedparser.FeedParserDict) -> str:
    src = ent.get("source")
    if isinstance(src, dict):
        return (src.get("title") or "").strip()
    if src:
        return str(src).strip()
    return ""


def search(query: str, *, limit: int = 8, resolve_links: bool = True) -> list[GoogleNewsItem]:
    """Search Google News RSS and return up to `limit` items with resolved URLs.

    When `resolve_links=True` (default), each Google redirect URL is decoded to
    the publisher canonical URL in parallel. Items where decoding fails are
    dropped so callers never see un-scrapeable Google URLs.
    """
    feed = feedparser.parse(_build_url(query))
    entries = (feed.entries or [])[:limit]
    raw: list[GoogleNewsItem] = []
    for ent in entries:
        link = (ent.get("link") or "").strip()
        title = (ent.get("title") or "").strip()
        if not link or not title:
            continue
        raw.append(
            GoogleNewsItem(
                title=title,
                link=link,
                publisher=_publisher_from_entry(ent),
                published=_published_from_entry(ent),
            )
        )

    if not resolve_links or not raw:
        return raw

    with ThreadPoolExecutor(max_workers=DECODE_WORKERS) as pool:
        decoded_links = list(pool.map(_decode, [it.link for it in raw]))

    out: list[GoogleNewsItem] = []
    for it, decoded in zip(raw, decoded_links):
        if not decoded:
            continue
        out.append(
            GoogleNewsItem(
                title=it.title,
                link=decoded,
                publisher=it.publisher,
                published=it.published,
            )
        )
    return out


__all__ = ["GoogleNewsItem", "search"]

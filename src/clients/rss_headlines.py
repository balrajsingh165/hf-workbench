from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

import feedparser

from src.config import DEFAULT_RSS_FEEDS


@dataclass(slots=True)
class RssItem:
    title: str
    link: str
    source: str
    published: str | None


def _feed_label(feed: feedparser.FeedParserDict) -> str:
    t = feed.feed.get("title")
    if t:
        return str(t)[:80]
    return "rss"


def fetch_rss_pooled(
    feed_urls: tuple[str, ...] | list[str] | None = None,
    *,
    max_per_feed: int = 6,
) -> list[RssItem]:
    """Pull recent items from each feed (no de-dupe)."""
    urls = list(feed_urls) if feed_urls else list(DEFAULT_RSS_FEEDS)
    out: list[RssItem] = []
    for url in urls:
        parsed = feedparser.parse(url)
        label = _feed_label(parsed)
        for ent in (parsed.entries or [])[:max_per_feed]:
            link = (ent.get("link") or "").strip()
            if not link:
                continue
            title = (ent.get("title") or "").strip() or link
            pub = ent.get("published_parsed") or ent.get("updated_parsed")
            published: str | None = None
            if pub:
                import time

                try:
                    published = time.strftime("%Y-%m-%dT%H:%M:%SZ", pub)
                except (TypeError, ValueError, OverflowError):
                    published = None
            out.append(RssItem(title=title, link=link, source=label, published=published))
    return out


def filter_rss_by_keywords(items: list[RssItem], *keywords: str) -> list[RssItem]:
    if not keywords:
        return list(items)
    kws = [k.lower() for k in keywords if k.strip()]
    if not kws:
        return list(items)
    out: list[RssItem] = []
    for it in items:
        blob = f"{it.title} {it.link}".lower()
        if any(k in blob for k in kws):
            out.append(it)
    return out


def netloc(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


_STOP = frozenset(
    "a an the to of and or for in on at is are was were be been being with from by as it its this that "
    "says new says says".split()
)


def title_keyword_overlap(a: str, b: str) -> float:
    def words(s: str) -> set[str]:
        return {w for w in re.split(r"[^\w]+", s.lower()) if len(w) > 2 and w not in _STOP}

    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


__all__ = [
    "RssItem",
    "fetch_rss_pooled",
    "filter_rss_by_keywords",
    "netloc",
    "title_keyword_overlap",
]

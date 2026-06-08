"""Scrape hot topics and stories from particle.news frontpage.

Particle is used as a topic-discovery layer: it tells us what the market
is talking about today. We extract story cards, filter to finance-relevant
sections, then hand off headlines as topic queries to the normal ingest pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

_FRONTPAGE_URL = "https://particle.news"
_STORY_BASE_URL = "https://particle.news/story"
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Topic slugs on particle.news that are relevant to finance / thesis generation
FINANCE_TOPIC_SLUGS: frozenset[str] = frozenset(
    {
        "economics",
        "technology",
        "politics",
    }
)

# Subtopic slugs (from frontpage sidebar) treated as finance-relevant
FINANCE_SUBTOPIC_SLUGS: frozenset[str] = frozenset(
    {
        "stock-market",
        "market-analysis",
        "investments",
        "digital-assets",
        "cryptocurrency",
        "electric-vehicles",
        "machine-learning",
        "management",
    }
)

_HTML_ENTITY = re.compile(r"&#(\d+);|&([a-z]+);")
_ENTITY_MAP = {"amp": "&", "quot": '"', "lt": "<", "gt": ">", "apos": "'"}


def _decode_entities(s: str) -> str:
    def _replace(m: re.Match) -> str:
        if m.group(1):
            return chr(int(m.group(1)))
        return _ENTITY_MAP.get(m.group(2), m.group(0))
    return _HTML_ENTITY.sub(_replace, s)


@dataclass
class ParticleStory:
    headline: str
    subtitle: str
    article_count: int
    age_str: str
    topic_slug: str
    topic_name: str
    story_path: str  # e.g. "/story/intel-shares-soar..."


def _parse_frontpage(html: str) -> list[ParticleStory]:
    """Extract all story cards from the particle.news frontpage HTML."""
    stories: list[ParticleStory] = []

    # Each card: <div class="card..." data-topic="NAME" data-slug="SLUG" ...>
    # followed by <a ... href="/story/..."> then metadata/headline/subhead
    card_pattern = re.compile(
        r'<div class="card[^"]*"\s+data-topic="([^"]+)"\s+data-slug="([^"]+)"[^>]*>'
        r'.*?<a [^>]*href="(/story/[^"]+)"[^>]*>'
        r'.*?<div[^>]*>(\d+) ARTICLES</div>'
        r'\s*<div[^>]*>([^<]+)</div>'  # age
        r'.*?<div class="headline"[^>]*>([^<]+)</div>'
        r'.*?<div class="subhead"[^>]*>([^<]*)</div>',
        re.DOTALL,
    )

    for m in card_pattern.finditer(html):
        topic_name, topic_slug, story_path, count_str, age, headline, subtitle = m.groups()
        stories.append(
            ParticleStory(
                headline=_decode_entities(headline.strip()),
                subtitle=_decode_entities(subtitle.strip()),
                article_count=int(count_str),
                age_str=age.strip(),
                topic_slug=topic_slug.strip(),
                topic_name=topic_name.strip(),
                story_path=story_path.strip(),
            )
        )

    return stories


def fetch_frontpage(*, timeout: float = 15.0) -> list[ParticleStory]:
    """Fetch particle.news frontpage and return parsed story cards."""
    r = httpx.get(
        _FRONTPAGE_URL,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    )
    r.raise_for_status()
    return _parse_frontpage(r.text)


def filter_finance(
    stories: list[ParticleStory],
    *,
    topic_slugs: frozenset[str] | None = None,
    min_articles: int = 10,
) -> list[ParticleStory]:
    """Filter stories to finance-relevant topics, sorted by article count desc."""
    slugs = topic_slugs if topic_slugs is not None else FINANCE_TOPIC_SLUGS
    filtered = [
        s for s in stories
        if s.topic_slug in slugs and s.article_count >= min_articles
    ]
    return sorted(filtered, key=lambda s: s.article_count, reverse=True)


__all__ = [
    "FINANCE_SUBTOPIC_SLUGS",
    "FINANCE_TOPIC_SLUGS",
    "ParticleStory",
    "fetch_frontpage",
    "filter_finance",
]

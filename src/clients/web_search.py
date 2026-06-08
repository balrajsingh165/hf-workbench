"""Provider switch for web search/scrape clients.

Set ``WEB_SEARCH_PROVIDER`` in ~/.env to either ``firecrawl`` or ``exa``.
The default is Firecrawl.

``search_snippets`` is for agent discovery (index blurbs / highlights).
``search_text`` and ``scrape`` retain full-page extraction for other callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from src.clients.web_search_snippet import (
    _DISCOVERY_SOURCE_MAX,
    prepare_snippet,
)
from src.config import WEB_SEARCH_PROVIDER


SearchProvider = Literal["firecrawl", "exa"]


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str
    text: str = ""
    published_date: str | None = None
    raw: Any = None


def provider() -> SearchProvider:
    value = WEB_SEARCH_PROVIDER.strip().lower()
    if value not in {"firecrawl", "exa"}:
        raise RuntimeError(
            "WEB_SEARCH_PROVIDER must be either 'firecrawl' or 'exa' "
            f"(got {WEB_SEARCH_PROVIDER!r})."
        )
    return value  # type: ignore[return-value]


def cutoff_date_for_days_back(days_back: int | None) -> str | None:
    if not days_back:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days_back)).date().isoformat()


def search_snippets(
    query: str,
    *,
    num_results: int | None = 8,
    days_back: int | None = None,
) -> list[SearchResult]:
    """Discovery search: short snippets only (no per-hit full-page scrape)."""
    limit = num_results or 8
    cutoff = cutoff_date_for_days_back(days_back)
    active = provider()
    if active == "firecrawl":
        return _firecrawl_snippets(query, limit=limit, cutoff=cutoff)
    return _exa_snippets(query, limit=limit, cutoff=cutoff)


def _firecrawl_snippets(
    query: str,
    *,
    limit: int,
    cutoff: str | None,
) -> list[SearchResult]:
    from src.clients.firecrawl import search as firecrawl_search

    # Over-fetch: junk filter and date post-filter may drop rows.
    fetch_limit = min(max(limit * 2, limit), 15)
    rows = firecrawl_search(
        query,
        num_results=fetch_limit,
        text=False,
        text_max_characters=_DISCOVERY_SOURCE_MAX,
    )
    out: list[SearchResult] = []
    for row in rows:
        if cutoff:
            published = row.get("published_date")
            if published and published < cutoff:
                continue
        snippet = prepare_snippet(row.get("text") or "")
        if not snippet:
            continue
        out.append(
            SearchResult(
                url=row["url"],
                title=row["title"],
                text=snippet,
                published_date=row.get("published_date"),
                raw=row.get("raw"),
            )
        )
        if len(out) >= limit:
            break
    return out


def _exa_snippets(
    query: str,
    *,
    limit: int,
    cutoff: str | None,
) -> list[SearchResult]:
    from src.clients.exa import _result_snippet, search as exa_search

    response = exa_search(
        query,
        num_results=limit,
        text=True,
        highlights=True,
        text_max_characters=_DISCOVERY_SOURCE_MAX,
        start_published_date=cutoff,
    )
    out: list[SearchResult] = []
    for res in response.results or []:
        url = (getattr(res, "url", None) or "").strip()
        if not url:
            continue
        published = (getattr(res, "published_date", None) or "")[:10] or None
        if cutoff and published and published < cutoff:
            continue
        snippet = prepare_snippet(_result_snippet(res, text_max_characters=_DISCOVERY_SOURCE_MAX))
        if not snippet:
            continue
        out.append(
            SearchResult(
                url=url,
                title=(getattr(res, "title", None) or "").strip() or url,
                text=snippet,
                published_date=published,
                raw=res,
            )
        )
        if len(out) >= limit:
            break
    return out


def search_text(
    query: str,
    *,
    num_results: int | None = 10,
    text_max_characters: int = 10_000,
    sources: list[str] | None = None,
) -> list[SearchResult]:
    active = provider()
    if active == "firecrawl":
        from src.clients.firecrawl import search_text as firecrawl_search_text

        results = firecrawl_search_text(
            query,
            num_results=num_results,
            text_max_characters=text_max_characters,
            sources=sources,
        )
        return [
            SearchResult(
                url=r["url"],
                title=r["title"],
                text=r.get("text") or "",
                published_date=r.get("published_date"),
                raw=r.get("raw"),
            )
            for r in results
        ]

    from src.clients.exa import search_text as exa_search_text

    response = exa_search_text(
        query,
        num_results=num_results,
        text_max_characters=text_max_characters,
    )
    out: list[SearchResult] = []
    for res in response.results or []:
        url = (getattr(res, "url", None) or "").strip()
        if not url:
            continue
        out.append(
            SearchResult(
                url=url,
                title=(getattr(res, "title", None) or "").strip() or url,
                text=(getattr(res, "text", None) or "")[:text_max_characters],
                published_date=(getattr(res, "published_date", None) or "")[:10] or None,
                raw=res,
            )
        )
    return out


def scrape(url: str, *, text_max_characters: int = 10_000) -> SearchResult:
    active = provider()
    if active == "firecrawl":
        from src.clients.firecrawl import scrape as firecrawl_scrape

        result = firecrawl_scrape(url, text_max_characters=text_max_characters)
        return SearchResult(
            url=result["url"],
            title=result["title"],
            text=result.get("text") or "",
            published_date=result.get("published_date"),
            raw=result.get("raw"),
        )

    from src.clients.exa import scrape as exa_scrape

    response = exa_scrape(url, text=True)
    first = (response.results or [None])[0]
    if first is None:
        return SearchResult(url=url, title=url)
    return SearchResult(
        url=(getattr(first, "url", None) or url).strip(),
        title=(getattr(first, "title", None) or "").strip() or url,
        text=(getattr(first, "text", None) or "")[:text_max_characters],
        published_date=(getattr(first, "published_date", None) or "")[:10] or None,
        raw=first,
    )


__all__ = [
    "SearchProvider",
    "SearchResult",
    "cutoff_date_for_days_back",
    "provider",
    "scrape",
    "search_snippets",
    "search_text",
]

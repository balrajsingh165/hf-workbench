"""Minimal shared Firecrawl search/content helper.

Official docs checked on 2026-04-25:
- https://docs.firecrawl.dev/sdks/python
- https://docs.firecrawl.dev/features/search
- https://docs.firecrawl.dev/features/scrape
- https://docs.firecrawl.dev/api-reference/endpoint/search
- https://docs.firecrawl.dev/api-reference/endpoint/scrape
"""

from __future__ import annotations

from typing import Any

from firecrawl import Firecrawl

from src.config import FIRECRAWL_API_KEY, require_env


DEFAULT_TEXT_MAX_CHARACTERS = 10_000


def _resolve_api_key(api_key: str | None = None) -> str:
    return require_env("FIRECRAWL_API_KEY", api_key or FIRECRAWL_API_KEY)


def get_firecrawl_client(api_key: str | None = None) -> Firecrawl:
    return Firecrawl(api_key=_resolve_api_key(api_key))


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _metadata_field(obj: Any, name: str, default: Any = None) -> Any:
    metadata = _field(obj, "metadata")
    if metadata is None:
        return default
    return _field(metadata, name, default)


def _result_url(obj: Any) -> str:
    return (
        _field(obj, "url")
        or _metadata_field(obj, "source_url")
        or _metadata_field(obj, "url")
        or _metadata_field(obj, "og_url")
        or ""
    )


def _result_title(obj: Any) -> str:
    return (
        _field(obj, "title")
        or _metadata_field(obj, "title")
        or _metadata_field(obj, "og_title")
        or _result_url(obj)
    )


def _result_snippet(obj: Any, max_characters: int) -> str:
    """Search-index blurb only — never use scraped page markdown."""
    text = (
        _field(obj, "summary")
        or _field(obj, "description")
        or _field(obj, "snippet")
        or _metadata_field(obj, "description")
        or _metadata_field(obj, "og_description")
        or ""
    )
    return str(text)[:max_characters]


def _result_text(obj: Any, max_characters: int) -> str:
    text = (
        _field(obj, "markdown")
        or _field(obj, "summary")
        or _field(obj, "description")
        or _field(obj, "snippet")
        or _metadata_field(obj, "description")
        or _metadata_field(obj, "og_description")
        or ""
    )
    return str(text)[:max_characters]


def _result_date(obj: Any) -> str | None:
    value = (
        _field(obj, "date")
        or _metadata_field(obj, "published_time")
        or _metadata_field(obj, "dc_date")
        or _metadata_field(obj, "dc_date_created")
        or _metadata_field(obj, "dc_terms_created")
    )
    return str(value) if value else None


def search(
    query: str,
    *,
    api_key: str | None = None,
    num_results: int | None = 10,
    sources: list[str] | None = None,
    categories: list[str] | None = None,
    text: bool = False,
    text_max_characters: int = DEFAULT_TEXT_MAX_CHARACTERS,
    timeout: int | None = None,
) -> list[dict[str, Any]]:
    """Run Firecrawl search and normalize web/news results to dictionaries."""

    scrape_options = None
    if text:
        scrape_options = {
            "formats": ["markdown"],
            "onlyMainContent": True,
        }

    response = get_firecrawl_client(api_key).search(
        query,
        limit=num_results,
        sources=sources,
        categories=categories,
        scrape_options=scrape_options,
        timeout=timeout,
    )

    raw_results: list[Any] = []
    for attr in ("web", "news"):
        raw_results.extend(_field(response, attr) or [])

    out: list[dict[str, Any]] = []
    for item in raw_results:
        url = str(_result_url(item)).strip()
        if not url:
            continue
        extract = _result_text if text else _result_snippet
        out.append({
            "url": url,
            "title": str(_result_title(item)).strip() or url,
            "text": extract(item, text_max_characters),
            "published_date": (_result_date(item) or "")[:10] or None,
            "raw": item,
        })
    return out


def search_text(
    query: str,
    *,
    api_key: str | None = None,
    num_results: int | None = 10,
    text_max_characters: int = DEFAULT_TEXT_MAX_CHARACTERS,
    sources: list[str] | None = None,
    categories: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper for search plus markdown extraction."""

    return search(
        query,
        api_key=api_key,
        num_results=num_results,
        sources=sources,
        categories=categories,
        text=True,
        text_max_characters=text_max_characters,
    )


def scrape(
    url: str,
    *,
    api_key: str | None = None,
    text_max_characters: int = DEFAULT_TEXT_MAX_CHARACTERS,
) -> dict[str, Any]:
    """Scrape one URL via Firecrawl's scrape endpoint."""

    doc = get_firecrawl_client(api_key).scrape(
        url,
        formats=["markdown"],
        only_main_content=True,
    )
    resolved_url = str(_result_url(doc)).strip() or url
    return {
        "url": resolved_url,
        "title": str(_result_title(doc)).strip() or resolved_url,
        "text": _result_text(doc, text_max_characters),
        "published_date": (_result_date(doc) or "")[:10] or None,
        "raw": doc,
    }


__all__ = [
    "DEFAULT_TEXT_MAX_CHARACTERS",
    "get_firecrawl_client",
    "scrape",
    "search",
    "search_text",
]

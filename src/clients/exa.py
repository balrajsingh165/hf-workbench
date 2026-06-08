"""Minimal shared Exa search/content helper.

Official docs checked on 2026-04-23:
- https://exa.ai/docs/reference/quickstart
- https://exa.ai/docs/sdks/python-sdk-specification
- https://exa.ai/docs/reference/search
- https://exa.ai/docs/reference/get-contents
- https://exa.ai/docs/reference/contents-retrieval
"""

from __future__ import annotations

from typing import Any

from exa_py import Exa
from exa_py.api import Result, SearchResponse
from src.config import EXA_API_KEY, require_env


DEFAULT_EXA_SEARCH_TYPE = "auto"
DEFAULT_TEXT_MAX_CHARACTERS = 10_000


def _resolve_api_key(api_key: str | None = None) -> str:
    return require_env("EXA_API_KEY", api_key or EXA_API_KEY)


def get_exa_client(api_key: str | None = None) -> Exa:
    return Exa(api_key=_resolve_api_key(api_key))


def search(
    query: str,
    *,
    api_key: str | None = None,
    num_results: int | None = 10,
    search_type: str = DEFAULT_EXA_SEARCH_TYPE,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    end_published_date: str | None = None,
    include_text: list[str] | None = None,
    exclude_text: list[str] | None = None,
    text: bool = False,
    text_max_characters: int = DEFAULT_TEXT_MAX_CHARACTERS,
    highlights: bool = False,
) -> SearchResponse[Result]:
    """Run Exa search using the official Python SDK.

    Per Exa's SDK docs, ``search()`` can optionally retrieve page contents by
    passing a ``contents`` object. This helper exposes the two most common
    content modes: full text and highlights.
    """

    contents: dict[str, Any] | bool = False
    if text or highlights:
        contents = {}
        if text:
            contents["text"] = {"max_characters": text_max_characters}
        if highlights:
            contents["highlights"] = True

    return get_exa_client(api_key).search(
        query,
        type=search_type,
        num_results=num_results,
        contents=contents,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        start_published_date=start_published_date,
        end_published_date=end_published_date,
        include_text=include_text,
        exclude_text=exclude_text,
    )


def search_text(
    query: str,
    *,
    api_key: str | None = None,
    num_results: int | None = 10,
    search_type: str = DEFAULT_EXA_SEARCH_TYPE,
    text_max_characters: int = DEFAULT_TEXT_MAX_CHARACTERS,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    end_published_date: str | None = None,
) -> SearchResponse[Result]:
    """Convenience wrapper for search plus page text extraction."""

    return search(
        query,
        api_key=api_key,
        num_results=num_results,
        search_type=search_type,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        start_published_date=start_published_date,
        end_published_date=end_published_date,
        text=True,
        text_max_characters=text_max_characters,
    )


def _result_snippet(res: Result, *, text_max_characters: int) -> str:
    highlights = getattr(res, "highlights", None) or []
    if highlights:
        return str(highlights[0])[:text_max_characters]
    return (getattr(res, "text", None) or "")[:text_max_characters]


def scrape(
    urls: str | list[str] | list[Result],
    *,
    api_key: str | None = None,
    text: bool = True,
    summary: bool = False,
    subpages: int | None = None,
    subpage_target: str | list[str] | None = None,
    max_age_hours: int | None = None,
    filter_empty_results: bool | None = None,
) -> SearchResponse[Result]:
    """Retrieve contents for one or more URLs via Exa's contents endpoint."""

    return get_exa_client(api_key).get_contents(
        urls,
        text=text,
        summary=summary,
        subpages=subpages,
        subpage_target=subpage_target,
        max_age_hours=max_age_hours,
        filter_empty_results=filter_empty_results,
    )


__all__ = [
    "DEFAULT_EXA_SEARCH_TYPE",
    "DEFAULT_TEXT_MAX_CHARACTERS",
    "get_exa_client",
    "scrape",
    "search",
    "search_text",
]

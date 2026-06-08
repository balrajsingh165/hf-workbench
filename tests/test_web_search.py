from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from app import get_web_search
from src.clients.web_search import SearchResult, search_snippets
from src.clients.web_search_snippet import is_junk_snippet, prepare_snippet, sanitize_snippet


def test_sanitize_strips_markdown_links() -> None:
    raw = "Read [more](https://example.com/x) about yields."
    assert sanitize_snippet(raw) == "Read more about yields."


def test_is_junk_detects_skip_links() -> None:
    assert is_junk_snippet("[Skip to main content](https://fred.stlouisfed.org/x) # Title")
    assert is_junk_snippet("Oops, something went wrong Skip to navigation")
    assert not is_junk_snippet(
        "Treasury yields surge as inflation data points to tricky rates path."
    )


def test_prepare_snippet_drops_junk() -> None:
    assert prepare_snippet("Error 403 (Forbidden)!!1") == ""


def test_firecrawl_snippets_use_index_text_not_scrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.clients.web_search.provider", lambda: "firecrawl")

    today_iso = datetime.now(timezone.utc).date().isoformat()
    calls: list[dict[str, Any]] = []

    def fake_search(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return [
            {
                "url": "https://example.com/a",
                "title": "Good hit",
                "text": "30-year yield moved above 5% on inflation fears.",
                "published_date": today_iso,
                "raw": None,
            },
            {
                "url": "https://fred.stlouisfed.org/series/DGS30",
                "title": "FRED",
                "text": "[Skip to main content](https://fred.stlouisfed.org/) nav only",
                "published_date": None,
                "raw": None,
            },
        ]

    monkeypatch.setattr("src.clients.firecrawl.search", fake_search)

    results = search_snippets("treasury yield drivers", num_results=2, days_back=7)

    assert calls[0]["text"] is False
    assert len(results) == 1
    assert results[0].url == "https://example.com/a"
    assert "5%" in results[0].text


def test_exa_snippets_use_highlights_and_start_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.clients.web_search.provider", lambda: "exa")

    today_iso = datetime.now(timezone.utc).date().isoformat()
    captured: dict[str, Any] = {}

    class FakeResult:
        def __init__(self) -> None:
            self.url = "https://cnbc.com/article"
            self.title = "Yields surge"
            self.text = "fallback body"
            self.published_date = f"{today_iso}T08:00:00.000Z"
            self.highlights = ["Treasury yields spike on messy inflation data."]

    class FakeResponse:
        results = [FakeResult()]

    def fake_exa_search(*_args: Any, **kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("src.clients.exa.search", fake_exa_search)

    results = search_snippets("treasury yield", num_results=3, days_back=7)

    assert captured["highlights"] is True
    assert captured["start_published_date"] is not None
    assert len(results) == 1
    assert "inflation" in results[0].text


def test_get_web_search_dispatches_snippets(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_snippets(
        query: str,
        *,
        num_results: int | None = 8,
        days_back: int | None = None,
    ) -> list[SearchResult]:
        assert query == "test query"
        assert num_results == 5
        assert days_back == 7
        return [
            SearchResult(
                url="https://example.com",
                title="Example",
                text="Clean snippet text.",
                published_date="2026-05-16",
            )
        ]

    monkeypatch.setattr("src.clients.web_search.search_snippets", fake_snippets)

    resp = get_web_search(query="test query", num_results=5, days_back=7)

    assert resp.results[0].snippet == "Clean snippet text."
    assert "Skip to main" not in resp.results[0].snippet

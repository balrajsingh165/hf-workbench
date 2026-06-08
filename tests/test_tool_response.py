from __future__ import annotations

from src.agent.tool_response import strip_tool_input_echo
from src.agent.tools import _dispatch


def test_strip_tool_input_echo_web_search() -> None:
    payload = {
        "query": "treasury yields",
        "results": [{"url": "https://example.com", "title": "x", "snippet": "y"}],
        "asof": "2026-05-19T00:00:00+00:00",
    }
    out = strip_tool_input_echo("web_search", payload)
    assert "query" not in out
    assert out["results"]


def test_strip_tool_input_echo_xbrl() -> None:
    payload = {
        "ticker": "AAPL",
        "metric": "revenue",
        "frequency": "quarterly",
        "concept": "Revenues",
        "asof": "2026-05-19T00:00:00+00:00",
    }
    out = strip_tool_input_echo("xbrl_fact", payload)
    assert out == {
        "concept": "Revenues",
        "asof": "2026-05-19T00:00:00+00:00",
    }


def test_dispatch_web_search_omits_query(monkeypatch) -> None:
    from app import WebSearchHit, WebSearchResponse

    def fake_search(**_kwargs: object) -> WebSearchResponse:
        return WebSearchResponse(
            results=[
                WebSearchHit(
                    url="https://example.com",
                    title="Example",
                    snippet="Snippet text.",
                )
            ],
            asof="2026-05-19T00:00:00+00:00",
        )

    monkeypatch.setattr("app.get_web_search", fake_search)

    out = _dispatch(
        "web_search",
        {"query": "treasury yield", "num_results": 5},
        user_id="user_1",
    )
    assert "query" not in out
    assert out["results"][0]["snippet"] == "Snippet text."

"""Tests for Phase 2 citation ref enrichment."""

from __future__ import annotations

from src.agent.citations import (
    _load_story_db_fields,
    _small_thumbnail_url_from_images_json,
    build_citation_lookup,
    enrich_citations,
)


class _Capture:
    def __init__(self, parts: list[dict]) -> None:
        self.parts = parts


def _evidence_output() -> dict:
    return {
        "type": "tool-search_evidence",
        "state": "output-available",
        "output": {
            "evidence": [
                {
                    "story_id": "story_276",
                    "headline": "Futures traders price hikes, not cuts",
                    "relation": "supports",
                    "confidence": 0.9,
                    "rationale": "Direct Fed-path signal.",
                }
            ]
        },
    }


def test_enrich_story_id_from_search_evidence() -> None:
    capture = _Capture([_evidence_output()])
    kept, dropped = enrich_citations(["story_276"], capture)
    assert dropped == []
    assert kept[0]["index"] == 1
    assert kept[0]["title"] == "Futures traders price hikes, not cuts"
    assert kept[0]["source"] == "Story"
    assert kept[0]["snippet"] == "Direct Fed-path signal."
    assert kept[0]["url"] == "/feed/story_276"
    assert kept[0]["kind"] == "story"


def test_enrich_preserves_marker_order() -> None:
    capture = _Capture(
        [
            _evidence_output(),
            {
                "type": "tool-web_search",
                "state": "output-available",
                "output": {
                    "results": [
                        {
                            "url": "https://example.com/sec",
                            "title": "SEC filing page",
                            "snippet": "Revenue up 12%.",
                        }
                    ]
                },
            },
        ]
    )
    kept, dropped = enrich_citations(
        ["https://example.com/sec", "story_276"], capture
    )
    assert dropped == []
    assert [row["index"] for row in kept] == [1, 2]
    assert kept[0]["url"] == "https://example.com/sec"
    assert kept[0]["title"] == "SEC filing page"
    assert kept[0]["snippet"] == "Revenue up 12%."
    assert kept[0]["kind"] == "web"
    assert kept[1]["title"] == "Futures traders price hikes, not cuts"


def test_enrich_rejects_unbacked_ref() -> None:
    capture = _Capture([_evidence_output()])
    kept, dropped = enrich_citations(["story_999"], capture)
    assert kept == []
    assert dropped[0]["ref"] == "story_999"


def test_build_lookup_web_fetch_uses_tool_input_url() -> None:
    capture = _Capture(
        [
            {
                "type": "tool-web_fetch",
                "state": "output-available",
                "input": {"url": "https://example.com/from-input"},
                "output": {"title": "Title", "text": "Body"},
            }
        ]
    )
    lookup = build_citation_lookup(capture)
    assert lookup["https://example.com/from-input"]["title"] == "Title"


def test_build_lookup_includes_filings_and_fetch() -> None:
    capture = _Capture(
        [
            {
                "type": "tool-recent_filings",
                "state": "output-available",
                "output": {
                    "ticker": "AAPL",
                    "filings": [
                        {
                            "form": "8-K",
                            "report_date": "2026-03-29",
                            "primary_document_url": "https://sec.gov/aapl-8k",
                        },
                        {
                            "form": "10-Q",
                            "report_date": "2026-03-29",
                            "primary_document_url": "https://sec.gov/aapl-10q",
                        },
                    ],
                },
            },
            {
                "type": "tool-web_fetch",
                "state": "output-available",
                "input": {"url": "https://example.com/article"},
                "output": {
                    "title": "Article title",
                    "text": "Body text " * 40,
                },
            },
        ]
    )
    lookup = build_citation_lookup(capture)
    assert lookup["https://sec.gov/aapl-8k"]["title"] == "8-K — AAPL"
    assert lookup["https://sec.gov/aapl-8k"]["kind"] == "filing"
    assert "https://sec.gov/aapl-10q" not in lookup
    assert lookup["https://example.com/article"]["title"] == "Article title"
    assert lookup["https://example.com/article"]["snippet"].startswith("Body text")


def test_small_thumbnail_url_from_images_json() -> None:
    raw = """[
      {
        "variants": [
          {"size": "small", "mime": "image/webp", "url": "https://cdn.example/a.webp"},
          {"size": "small", "mime": "image/jpeg", "url": "https://cdn.example/a.jpg"}
        ]
      }
    ]"""
    assert (
        _small_thumbnail_url_from_images_json(raw) == "https://cdn.example/a.webp"
    )


def test_search_stories_uses_row_snippet() -> None:
    capture = _Capture(
        [
            {
                "type": "tool-search_stories",
                "state": "output-available",
                "output": {
                    "stories": [
                        {
                            "story_id": "story_42",
                            "headline": "Semis rally on export data",
                            "snippet": "Export orders beat expectations for a third month.",
                        }
                    ]
                },
            }
        ]
    )
    lookup = build_citation_lookup(capture)
    assert lookup["story_42"]["snippet"] == "Export orders beat expectations for a third month."


def test_load_story_db_fields_batches_one_query(monkeypatch) -> None:
    queries: list[tuple[str, tuple[str, ...]]] = []

    class _Conn:
        def execute(self, sql: str, params: tuple[str, ...]):
            queries.append((sql, params))

            class _Result:
                def fetchall(self):
                    return [
                        {"id": "story_a", "overview_json": '[{"text": "Overview bullet."}]', "images_json": "[]"},
                        {"id": "story_b", "overview_json": "[]", "images_json": "[]"},
                    ]

            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    import api

    monkeypatch.setattr(api, "db", lambda: _Conn())

    fields = _load_story_db_fields(["story_a", "story_b", "story_a"])
    assert len(queries) == 1
    assert "WHERE id IN (?,?)" in queries[0][0]
    assert queries[0][1] == ["story_a", "story_b"]
    assert fields["story_a"]["overview_blurb"] == "Overview bullet."
    assert fields["story_b"]["overview_blurb"] == ""

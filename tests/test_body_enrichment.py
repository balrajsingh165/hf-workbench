"""Tests for src.news.body_enrichment.

Pure-function tests + a stubbed-Firecrawl integration test that exercises
the full enrich path without touching the network.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.news import body_enrichment as be
from src.news.types import ClusterSourceDoc


def test_md_unescape_strips_backslash_escapes_around_punctuation():
    raw = "He said \\[is\\] true \\(maybe\\) — escaped \\* asterisk \\."
    cleaned = be._MD_ESCAPE_RE.sub(r"\1", raw)
    assert cleaned == "He said [is] true (maybe) — escaped * asterisk ."


def test_md_unescape_preserves_unrelated_backslashes():
    # Backslash before a non-punctuation char is preserved.
    raw = "C:\\path\\to\\file and a quote \\[hi\\]"
    cleaned = be._MD_ESCAPE_RE.sub(r"\1", raw)
    assert cleaned == "C:\\path\\to\\file and a quote [hi]"


def test_md_link_strip_reduces_inline_links_to_visible_text():
    raw = (
        "Markets think the [Federal Reserve](https://www.cnbc.com/federal-reserve/)"
        "'s next move will be a hike."
    )
    cleaned = be._MD_LINK_RE.sub(r"\1", raw)
    assert cleaned == "Markets think the Federal Reserve's next move will be a hike."


def test_md_link_strip_handles_image_and_empty_alt():
    raw = "Headline ![alt text](https://img/x.jpg) and []() blank."
    cleaned = be._MD_LINK_RE.sub(r"\1", raw)
    assert cleaned == "Headline alt text and  blank."


def test_md_link_strip_does_not_eat_across_newlines():
    # Defensive: a stray '[' followed by content and a ']' many lines later
    # must not collapse multiple paragraphs into one.
    raw = "Paragraph one [not a link\n\nParagraph two](https://x.test) tail."
    cleaned = be._MD_LINK_RE.sub(r"\1", raw)
    assert cleaned == raw


def test_scrape_url_strips_markdown_links(monkeypatch):
    """End-to-end: _scrape_url returns body with links reduced to text."""
    raw = (
        "Lede with [Federal Reserve](https://example.test/fed)'s decision "
        "and an image ![chart](https://example.test/c.png)."
    )

    def fake_scrape(url, *, text_max_characters):
        return {"text": raw}

    import src.clients.firecrawl as fc

    monkeypatch.setattr(fc, "scrape", fake_scrape)

    out = be._scrape_url("https://example.test/article")
    assert out == "Lede with Federal Reserve's decision and an image chart."


def test_scrape_url_normalizes_nbsp(monkeypatch):
    """Non-breaking spaces in scraped HTML must be normalized to plain spaces
    so LLM-quoted text matches the body verbatim."""
    raw = "Law enforcement officers risk their lives every day."

    def fake_scrape(url, *, text_max_characters):
        return {"text": raw}

    import src.clients.firecrawl as fc

    monkeypatch.setattr(fc, "scrape", fake_scrape)

    out = be._scrape_url("https://example.test/article")
    assert " " not in out
    assert out == "Law enforcement officers risk their lives every day."


def _make_member(news_id: str, body: str, url: str = "https://example.test/x") -> ClusterSourceDoc:
    return ClusterSourceDoc(
        news_id=news_id,
        title="t",
        url=url,
        publisher="Example",
        body=body,
    )


def _setup_news_table(conn: sqlite3.Connection, news_id: str, body: str) -> None:
    conn.execute("CREATE TABLE news (id TEXT PRIMARY KEY, body_excerpt TEXT)")
    conn.execute("INSERT INTO news (id, body_excerpt) VALUES (?, ?)", (news_id, body))
    conn.commit()


def test_enrich_skips_when_any_member_already_long(monkeypatch):
    """If any member already has body >= QUALITY_BODY_MIN_CHARS, no scrape."""
    long_body = "x" * (be.QUALITY_BODY_MIN_CHARS + 10)
    members = [_make_member("news_1", long_body)]
    conn = sqlite3.connect(":memory:")
    _setup_news_table(conn, "news_1", long_body)

    called = {"n": 0}

    def fake_scrape(*args, **kwargs):
        called["n"] += 1
        return "should not be called"

    monkeypatch.setattr(be, "_scrape_url", fake_scrape)

    out = be.enrich_member_bodies(members, conn=conn, cluster_id="cluster_skip")
    assert called["n"] == 0
    assert len(out[0].body) == len(long_body)


def test_enrich_scrapes_and_persists_when_thin(monkeypatch):
    """Thin body triggers scrape; in-memory + DB are both updated."""
    thin = "tiny rss blurb"
    members = [_make_member("news_2", thin)]
    conn = sqlite3.connect(":memory:")
    _setup_news_table(conn, "news_2", thin)

    big = "FULL ARTICLE TEXT " * 200  # well above threshold

    def fake_scrape(url):
        return big

    # FIRECRAWL_API_KEY check uses module-level constant.
    monkeypatch.setattr(be, "FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr(be, "_scrape_url", fake_scrape)

    out = be.enrich_member_bodies(members, conn=conn, cluster_id="cluster_thin")
    assert out[0].body.startswith("FULL ARTICLE TEXT")
    assert len(out[0].body) > len(thin)

    # DB must reflect the upgrade so re-runs / downstream readers benefit.
    persisted = conn.execute(
        "SELECT body_excerpt FROM news WHERE id='news_2'"
    ).fetchone()[0]
    assert persisted == out[0].body


def test_enrich_skips_when_api_key_missing(monkeypatch):
    """Without FIRECRAWL_API_KEY we must not attempt to scrape."""
    thin = "tiny"
    members = [_make_member("news_3", thin)]
    conn = sqlite3.connect(":memory:")
    _setup_news_table(conn, "news_3", thin)

    monkeypatch.setattr(be, "FIRECRAWL_API_KEY", None)
    monkeypatch.setattr(be, "_scrape_url", lambda url: pytest.fail("should not call"))

    out = be.enrich_member_bodies(members, conn=conn, cluster_id="cluster_no_key")
    assert out[0].body == thin


def test_enrich_all_promotes_scrapes_thin_member_when_another_is_long(monkeypatch):
    """HF_ENRICH_ALL_PROMOTES: one long RSS member must not skip thin tier-1 stubs."""
    long_body = "x" * (be.QUALITY_BODY_MIN_CHARS + 10)
    thin = "tiny bloomberg stub"
    members = [
        _make_member("news_long", long_body),
        _make_member("news_thin", thin, url="https://example.test/bloomberg"),
    ]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE news (id TEXT PRIMARY KEY, body_excerpt TEXT)")
    conn.execute("INSERT INTO news VALUES ('news_long', ?), ('news_thin', ?)", (long_body, thin))
    conn.commit()

    big = "FULL ARTICLE TEXT " * 200
    calls: list[str] = []

    def fake_scrape(url):
        calls.append(url)
        return big

    monkeypatch.setattr(be, "HF_ENRICH_ALL_PROMOTES", True)
    monkeypatch.setattr(be, "FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr(be, "_scrape_url", fake_scrape)

    out = be.enrich_member_bodies(members, conn=conn, cluster_id="cluster_mixed")
    assert len(calls) == 1
    assert out[1].body.startswith("FULL ARTICLE TEXT")


def test_enrich_caps_scrape_count(monkeypatch):
    """Cluster with many thin members must not exceed MAX_SCRAPES_PER_CLUSTER."""
    members = [_make_member(f"news_{i}", "thin") for i in range(5)]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE news (id TEXT PRIMARY KEY, body_excerpt TEXT)")
    for m in members:
        conn.execute("INSERT INTO news (id, body_excerpt) VALUES (?, ?)", (m.news_id, m.body))
    conn.commit()

    big = "X" * 2000
    calls = {"n": 0}

    def fake_scrape(url):
        calls["n"] += 1
        return big

    monkeypatch.setattr(be, "FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr(be, "_scrape_url", fake_scrape)

    be.enrich_member_bodies(members, conn=conn, cluster_id="cluster_cap")
    assert calls["n"] == be.MAX_SCRAPES_PER_CLUSTER

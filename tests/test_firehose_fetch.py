from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from agents import firehose as fh


def test_fetch_feed_uses_timed_curl_for_default_hosts() -> None:
    mock_resp = MagicMock()
    mock_resp.content = b"<rss></rss>"
    with patch.object(fh.curl_requests, "get", return_value=mock_resp) as get:
        fh._fetch_feed("https://www.federalreserve.gov/feeds/press_all.xml")
    get.assert_called_once_with(
        "https://www.federalreserve.gov/feeds/press_all.xml",
        headers={"User-Agent": fh.FEED_USER_AGENT},
        timeout=fh.FEED_FETCH_TIMEOUT_S,
    )


def test_fetch_feed_uses_impersonation_for_bls() -> None:
    mock_resp = MagicMock()
    mock_resp.content = b"<rss></rss>"
    with patch.object(fh.curl_requests, "get", return_value=mock_resp) as get:
        fh._fetch_feed("https://www.bls.gov/feed/cpi.rss")
    get.assert_called_once_with(
        "https://www.bls.gov/feed/cpi.rss",
        impersonate="chrome",
        timeout=fh.FEED_FETCH_TIMEOUT_S,
    )


def test_run_firehose_wall_clock_stops_early() -> None:
    feeds = [f"https://example.com/feed-{i}.rss" for i in range(50)]

    def slow_parse(url: str, ci_index, cs_index, *, max_items=None):
        time.sleep(0.05)
        return []

    with patch.object(fh, "parse_feed", side_effect=slow_parse):
        stats = fh.run_firehose(
            feeds,
            dry_run=True,
            max_wall_s=0.12,
        )

    assert stats.wall_clock_exceeded is True
    assert stats.feeds_polled < len(feeds)

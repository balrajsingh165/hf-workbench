"""Pin the on-disk transcript shape produced by a chart_image SSE event.

The chart agent emits an SSE `chart_image` event containing
`{url, caption}`. That stream is fed through
`convert_legacy_sse_to_ui_stream` which translates it into a `data-chart`
UI part and captures it in `UIStreamCapture.parts`. The FastAPI handler
then persists `capture.parts` into `agent_messages.parts_json`.

This test pins that contract end-to-end (without spinning up FastAPI/SQLite)
by feeding a synthetic SSE stream through the converter and asserting the
captured part has the exact `{url, caption}` shape the spec requires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from src.agent.ai_sdk_stream import UIStreamCapture, convert_legacy_sse_to_ui_stream
from src.agent.sse_emitter import event_chart_image


async def _yield(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.mark.anyio("asyncio")
async def test_chart_image_event_persisted_with_url_and_caption():
    capture = UIStreamCapture()

    sse_chunk = event_chart_image(
        url="https://pub-test.r2.dev/charts/req_persist.png",
        caption="BTC daily close",
    )

    async for _ in convert_legacy_sse_to_ui_stream(
        _yield(sse_chunk),
        message_id="msg_test",
        capture=capture,
    ):
        pass

    chart_parts = [p for p in capture.parts if p.get("type") == "data-chart"]
    assert len(chart_parts) == 1, "expected exactly one data-chart part captured"

    data = chart_parts[0]["data"]
    assert data == {
        "url": "https://pub-test.r2.dev/charts/req_persist.png",
        "caption": "BTC daily close",
    }


@pytest.mark.anyio("asyncio")
async def test_chart_image_part_serializes_to_json():
    """`parts_json` is a SQLite TEXT column populated via json.dumps in
    `append_message`. Make sure the captured part has no non-JSON values."""
    import json

    capture = UIStreamCapture()

    sse_chunk = event_chart_image(
        url="https://pub-test.r2.dev/charts/req_json.png",
        caption=None,
    )

    async for _ in convert_legacy_sse_to_ui_stream(
        _yield(sse_chunk), message_id="msg_test", capture=capture
    ):
        pass

    raw = json.dumps(capture.parts)
    restored = json.loads(raw)
    chart = next(p for p in restored if p.get("type") == "data-chart")
    assert chart["data"]["url"].startswith("https://")
    assert "key" not in chart["data"]
    assert "mime" not in chart["data"]
    assert "variants" not in chart["data"]


@pytest.fixture
def anyio_backend():
    return "asyncio"

"""Pin persistence shape for reasoning stream parts."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from src.agent.ai_sdk_stream import UIStreamCapture, convert_legacy_sse_to_ui_stream
from src.agent.sse_emitter import (
    event_reasoning_delta,
    event_reasoning_done,
    event_reasoning_start,
)


async def _yield(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.mark.anyio("asyncio")
async def test_reasoning_events_are_captured_as_done_ui_part():
    capture = UIStreamCapture()

    async for _ in convert_legacy_sse_to_ui_stream(
        _yield(
            event_reasoning_start("reasoning_test"),
            event_reasoning_delta("reasoning_test", "first "),
            event_reasoning_delta("reasoning_test", "second"),
            event_reasoning_done("reasoning_test"),
        ),
        message_id="msg_test",
        capture=capture,
    ):
        pass

    reasoning_parts = [p for p in capture.parts if p.get("type") == "reasoning"]
    assert reasoning_parts == [
        {
            "type": "reasoning",
            "id": "reasoning_test",
            "text": "first second",
            "state": "done",
        }
    ]


@pytest.fixture
def anyio_backend():
    return "asyncio"

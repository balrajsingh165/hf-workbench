"""Pin the UI part emitted when the orchestrator starts Phase 2."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from src.agent.ai_sdk_stream import UIStreamCapture, convert_legacy_sse_to_ui_stream
from src.agent.sse_emitter import event_agent_phase


async def _yield(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.mark.anyio("asyncio")
async def test_agent_phase_event_becomes_data_agent_phase_part():
    capture = UIStreamCapture()
    sse_chunk = event_agent_phase(phase="response")

    async for _ in convert_legacy_sse_to_ui_stream(
        _yield(sse_chunk),
        message_id="msg_test",
        capture=capture,
    ):
        pass

    phase_parts = [p for p in capture.parts if p.get("type") == "data-agent-phase"]
    assert len(phase_parts) == 1
    assert phase_parts[0]["data"] == {"phase": "response"}


@pytest.fixture
def anyio_backend():
    return "asyncio"

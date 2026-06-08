"""Internal SSE queue encoder.

Produces `data: <json>\\n\\n` rows consumed by the AI SDK stream adapter.
"""

from __future__ import annotations

import json
import re
from typing import Any


_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]",
    re.UNICODE,
)


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def encode_event(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, default=str)}\n\n".encode("utf-8")


def event_start(request_id: str) -> bytes:
    return encode_event({"type": "start", "requestId": request_id})


def event_tool_use_start(tool_use_id: str, name: str, input_payload: Any) -> bytes:
    return encode_event(
        {
            "type": "tool_use_start",
            "id": tool_use_id,
            "name": name,
            "input": input_payload,
        }
    )


def event_tool_use_end(
    tool_use_id: str,
    name: str,
    duration_ms: int,
    output: Any | None = None,
) -> bytes:
    payload: dict[str, Any] = {
        "type": "tool_use_end",
        "id": tool_use_id,
        "name": name,
        "durationMs": duration_ms,
    }
    if output is not None:
        payload["output"] = output
    return encode_event(payload)


def event_text_delta(delta: str) -> bytes:
    return encode_event({"type": "text_delta", "delta": strip_emoji(delta)})


def event_text_done(text: str) -> bytes:
    return encode_event({"type": "text_done", "text": text})


def event_reasoning_start(reasoning_id: str) -> bytes:
    return encode_event({"type": "reasoning_start", "id": reasoning_id})


def event_reasoning_delta(reasoning_id: str, delta: str) -> bytes:
    return encode_event(
        {"type": "reasoning_delta", "id": reasoning_id, "delta": delta}
    )


def event_reasoning_done(reasoning_id: str) -> bytes:
    return encode_event({"type": "reasoning_done", "id": reasoning_id})


def event_result(
    *,
    total_cost_usd: float,
    duration_ms: int,
    model_usage: dict[str, Any] | None,
    full_text: str,
) -> bytes:
    """Result event with raw full text for backend-side metadata extraction."""
    return encode_event(
        {
            "type": "result",
            "totalCostUsd": total_cost_usd,
            "durationMs": duration_ms,
            "modelUsage": model_usage or {},
            "_fullText": full_text,
        }
    )


def event_error(message: str) -> bytes:
    return encode_event({"type": "error", "message": message})


def event_chart_image(
    *,
    url: str,
    caption: str | None = None,
) -> bytes:
    return encode_event(
        {
            "type": "chart_image",
            "url": url,
            "caption": caption,
        }
    )


def event_chart_skip(*, reason: str) -> bytes:
    return encode_event({"type": "chart_skip", "reason": reason})


def event_agent_phase(*, phase: str) -> bytes:
    """Signal orchestrator phase transitions to the UI stream adapter."""
    return encode_event({"type": "agent_phase", "phase": phase})

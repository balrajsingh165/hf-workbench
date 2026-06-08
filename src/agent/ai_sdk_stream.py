from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from src.agent.citations import citation_corpus, enrich_citations
from src.agent.json_block import parse_trailing_json_block


HEADERS_UI_STREAM: dict[str, str] = {
    "x-vercel-ai-ui-message-stream": "v1",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def encode_ui_event(event_type: str, **data: Any) -> bytes:
    return f"data: {json.dumps({'type': event_type, **data}, default=str)}\n\n".encode(
        "utf-8"
    )


def done_event() -> bytes:
    return b"data: [DONE]\n\n"


def _structured(value: Any) -> Any:
    if type(value) is str:
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _normalise_tool_output(raw: Any) -> Any:
    value = _structured(raw)
    if type(value) is list and len(value) == 1 and type(value[0]) is dict:
        inner = value[0]
        if "json" in inner:
            return inner["json"]
        if "text" in inner:
            return _structured(inner["text"])
    return value


# ── Citation enrichment ──────────────────────────────────────────────────────
#
# Phase 2 emits a minimal trailing JSON block: ordered story_id / URL refs.
# We hydrate display fields from captured tool outputs and reject refs that
# were not returned by a citable tool this turn.

_PROSE_STORY_RE = re.compile(r"story_[A-Za-z0-9_]+")


def _unbacked_prose_story_refs(
    visible_text: str, capture: UIStreamCapture
) -> list[dict[str, Any]]:
    """Return one dropped-entry-shaped dict per story_id mentioned in the
    prose that is not present in any captured tool output this turn.

    Story ids returned by `search_evidence` look like `story_276` /
    `story_281` / `story_nonfarm` (free-form alphanumeric after the prefix).
    The corpus is the JSON-serialised tool outputs, so a real story_id will
    appear verbatim there if a tool actually returned it. Anything in prose
    that isn't in the corpus is fabricated."""
    if not visible_text:
        return []
    corpus = citation_corpus(capture)
    if not corpus:
        # No tool outputs captured at all — every prose ref is unbacked, but
        # this is also the "no tools called" case where flagging would be
        # noisy. Skip.
        return []
    seen: set[str] = set()
    unbacked: list[dict[str, Any]] = []
    for match in _PROSE_STORY_RE.finditer(visible_text):
        ref = match.group(0)
        if ref in seen:
            continue
        seen.add(ref)
        if ref.lower() not in corpus:
            unbacked.append(
                {
                    "source": ref,
                    "snippet": "(prose-level reference, no JSON citation entry)",
                    "_validation_reason": (
                        "story_id mentioned inline in answer text but not "
                        "found in any tool output this turn"
                    ),
                }
            )
    return unbacked


class UIStreamCapture:
    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.parts: list[dict[str, Any]] = []

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def append_text(self, delta: str) -> None:
        self.text_parts.append(delta)

    def add_part(self, part: dict[str, Any]) -> None:
        self.parts.append(part)


async def convert_legacy_sse_to_ui_stream(
    chunks: AsyncIterator[bytes],
    *,
    message_id: str,
    capture: UIStreamCapture,
) -> AsyncIterator[bytes]:
    text_id = f"text_{uuid.uuid4().hex}"
    text_started = False
    text_ended = False
    emitted_start = False
    tool_names: dict[str, str] = {}
    tool_inputs: dict[str, Any] = {}
    reasoning_parts: dict[str, dict[str, Any]] = {}

    async for chunk in chunks:
        for part in chunk.decode("utf-8").split("\n\n"):
            if not part.startswith("data: "):
                continue
            payload = part[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            if event_type == "start":
                emitted_start = True
                yield encode_ui_event("start", messageId=message_id)
                yield encode_ui_event("start-step")
                continue

            if event_type == "tool_use_start":
                call_id = str(event.get("id") or f"call_{uuid.uuid4().hex}")
                tool_name = str(event.get("name") or "unknown")
                tool_input = event.get("input") or {}
                tool_names[call_id] = tool_name
                tool_inputs[call_id] = tool_input
                yield encode_ui_event(
                    "tool-input-start",
                    toolCallId=call_id,
                    toolName=tool_name,
                )
                yield encode_ui_event(
                    "tool-input-available",
                    toolCallId=call_id,
                    toolName=tool_name,
                    input=tool_input,
                )
                capture.add_part(
                    {
                        "type": f"tool-{tool_name}",
                        "toolCallId": call_id,
                        "state": "input-available",
                        "input": tool_input,
                    }
                )
                continue

            if event_type == "tool_use_end":
                call_id = str(event.get("id") or f"call_{uuid.uuid4().hex}")
                tool_name = str(
                    event.get("name") or tool_names.get(call_id) or "unknown"
                )
                output = _normalise_tool_output(
                    event.get("output")
                    or {
                        "status": "complete",
                        "duration_ms": event.get("durationMs"),
                    }
                )
                yield encode_ui_event(
                    "tool-output-available",
                    toolCallId=call_id,
                    output=output,
                )
                capture.add_part(
                    {
                        "type": f"tool-{tool_name}",
                        "toolCallId": call_id,
                        "state": "output-available",
                        "input": tool_inputs.get(call_id, {}),
                        "output": output,
                    }
                )
                continue

            if event_type == "reasoning_start":
                reasoning_id = str(event.get("id") or f"reasoning_{uuid.uuid4().hex}")
                if not emitted_start:
                    emitted_start = True
                    yield encode_ui_event("start", messageId=message_id)
                reasoning_part = {
                    "type": "reasoning",
                    "id": reasoning_id,
                    "text": "",
                    "state": "streaming",
                }
                reasoning_parts[reasoning_id] = reasoning_part
                capture.add_part(reasoning_part)
                yield encode_ui_event("reasoning-start", id=reasoning_id)
                continue

            if event_type == "reasoning_delta":
                reasoning_id = str(event.get("id") or "")
                delta = str(event.get("delta") or "")
                if not reasoning_id or not delta:
                    continue
                reasoning_part = reasoning_parts.get(reasoning_id)
                if reasoning_part is not None:
                    reasoning_part["text"] = (
                        str(reasoning_part.get("text") or "") + delta
                    )
                yield encode_ui_event(
                    "reasoning-delta", id=reasoning_id, delta=delta
                )
                continue

            if event_type == "reasoning_done":
                reasoning_id = str(event.get("id") or "")
                if not reasoning_id:
                    continue
                reasoning_part = reasoning_parts.get(reasoning_id)
                if reasoning_part is not None:
                    reasoning_part["state"] = "done"
                yield encode_ui_event("reasoning-end", id=reasoning_id)
                continue

            if event_type == "text_delta":
                delta = str(event.get("delta") or "")
                if not delta:
                    continue
                if not emitted_start:
                    emitted_start = True
                    yield encode_ui_event("start", messageId=message_id)
                if not text_started:
                    text_started = True
                    yield encode_ui_event("text-start", id=text_id)
                capture.append_text(delta)
                yield encode_ui_event("text-delta", id=text_id, delta=delta)
                continue

            if event_type == "text_done":
                # Phase 2 emits text_done with the cleaned prose (no trailing
                # JSON block). Overwrite capture.text_parts so the persisted
                # assistant text matches what was actually visible to the user
                # — any JSON-fence chunks that leaked through text-delta before
                # the strip heuristic engaged are discarded here.
                clean = str(event.get("text") or "")
                if clean:
                    capture.text_parts.clear()
                    capture.text_parts.append(clean)
                if not text_started:
                    text_started = True
                    yield encode_ui_event("text-start", id=text_id)
                if not text_ended:
                    text_ended = True
                    yield encode_ui_event("text-end", id=text_id)
                continue

            if event_type == "result":
                # Forward per-phase token/latency usage so eval and dev tools
                # can attribute reasoning/output tokens to the research vs.
                # response agent. The dict is keyed `phase1`/`phase2`/`phase2b`
                # by `orchestrator.run_pipeline`, each value carrying
                # inputTokens/outputTokens/cacheReadInputTokens/
                # cacheWriteInputTokens/totalTokens/latency_ms (see
                # `summarize_agent_metrics`).
                usage = event.get("modelUsage")
                if usage:
                    yield encode_ui_event("data-model-usage", data=usage)
                    capture.add_part({"type": "data-model-usage", "data": usage})
                meta = parse_trailing_json_block(str(event.get("_fullText") or ""))
                citations = meta.get("citations")
                if type(citations) is list and citations:
                    kept, dropped = enrich_citations(citations, capture)
                    if kept:
                        yield encode_ui_event("data-sources", data={"sources": kept})
                        capture.add_part(
                            {
                                "type": "data-sources",
                                "data": {"sources": kept},
                            }
                        )
                else:
                    dropped = []
                # Independent of the structured-citation validator, scan the
                # final visible prose for `story_\d+` references that no tool
                # actually returned — those are inline citation fabrications
                # that the JSON-array validator can't see.
                prose_unbacked = _unbacked_prose_story_refs(capture.text, capture)
                if prose_unbacked:
                    dropped = list(dropped) + prose_unbacked
                if dropped:
                    # Surface the rejection so evals and dev tools can
                    # catch fabricated citations. The frontend can choose
                    # to render this as a warning badge or ignore it.
                    yield encode_ui_event(
                        "data-sources-warning",
                        data={"dropped": dropped},
                    )
                    capture.add_part(
                        {
                            "type": "data-sources-warning",
                            "data": {"dropped": dropped},
                        }
                    )
                continue

            if event_type == "agent_phase":
                phase = str(event.get("phase") or "")
                if phase:
                    data = {"phase": phase}
                    yield encode_ui_event("data-agent-phase", data=data)
                    capture.add_part({"type": "data-agent-phase", "data": data})
                continue

            if event_type == "chart_image":
                data = {
                    "url": event.get("url"),
                    "caption": event.get("caption"),
                }
                yield encode_ui_event("data-chart", data=data)
                capture.add_part({"type": "data-chart", "data": data})
                continue

            if event_type == "chart_skip":
                data = {"reason": event.get("reason")}
                yield encode_ui_event("data-chart-skip", data=data)
                capture.add_part({"type": "data-chart-skip", "data": data})
                continue

            if event_type == "error":
                yield encode_ui_event(
                    "error", errorText=str(event.get("message") or "Agent error")
                )
                yield encode_ui_event("finish-step")
                yield encode_ui_event("finish", finishReason="error")
                yield done_event()
                return

    if not emitted_start:
        yield encode_ui_event("start", messageId=message_id)
    if text_started and not text_ended:
        yield encode_ui_event("text-end", id=text_id)
    yield encode_ui_event("finish-step")
    yield encode_ui_event("finish", finishReason="stop")
    yield done_event()


async def protocol_smoke_stream(
    *,
    message_id: str,
    user_text: str,
    capture: UIStreamCapture,
) -> AsyncIterator[bytes]:
    text_id = f"text_{uuid.uuid4().hex}"
    call_id = f"call_{uuid.uuid4().hex}"
    answer = (
        "Protocol smoke passed. The frontend reached hf-workbench through "
        f"the AI SDK route and sent: {user_text[:120]}"
    )
    yield encode_ui_event("start", messageId=message_id)
    yield encode_ui_event("start-step")
    yield encode_ui_event("tool-input-start", toolCallId=call_id, toolName="search_evidence")
    yield encode_ui_event(
        "tool-input-available",
        toolCallId=call_id,
        toolName="search_evidence",
        input={"thesis_id": "protocol-smoke"},
    )
    yield encode_ui_event(
        "tool-output-available",
        toolCallId=call_id,
        output={"summary": "Backend protocol smoke mode", "sources": []},
    )
    capture.add_part(
        {
            "type": "tool-search_evidence",
            "toolCallId": call_id,
            "state": "output-available",
            "input": {"thesis_id": "protocol-smoke"},
            "output": {"summary": "Backend protocol smoke mode", "sources": []},
        }
    )
    yield encode_ui_event("text-start", id=text_id)
    capture.append_text(answer)
    yield encode_ui_event("text-delta", id=text_id, delta=answer)
    yield encode_ui_event("text-end", id=text_id)
    yield encode_ui_event("finish-step")
    yield encode_ui_event("finish", finishReason="stop")
    yield done_event()


def smoke_mode_enabled() -> bool:
    return os.getenv("HF_AGENT_PROTOCOL_SMOKE", "").lower() in {"1", "true", "yes"}

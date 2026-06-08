"""Phase 2: response agent.

Streams text deltas via the internal SSE queue (`text_delta` / `text_done`).
The AI SDK route converts the stream and emits structured data parts for
sources/strength. Detects the start of the trailing JSON metadata block
during streaming so the user-facing chat text stays clean while the backend
can convert metadata into structured stream parts.

Extended thinking is mode-driven (constants below): off in quick mode, on
with a small budget in deep mode. Reasoning deltas are forwarded to the
SSE queue (`reasoning_start` / `reasoning_delta` / `reasoning_done`) so the
UI can show progress while Sonnet thinks on the deep-mode handoff.
"""

from __future__ import annotations

import time
import uuid
from asyncio import Queue
from dataclasses import dataclass
from typing import Any

from strands import Agent

from src.agent.config import AgentConfig, get_agent_config
from src.agent.json_block import find_trailing_json_block_start
from src.agent.model_provider import create_response_model
from src.agent.models import ResponseMode, StoryContext, ThesisContext
from src.agent.observability import summarize_agent_metrics
from src.agent.prompt_manager import (
    build_phase2_system_prompt,
    build_phase2_user_prompt,
)
from src.agent.sse_emitter import (
    event_reasoning_delta,
    event_reasoning_done,
    event_reasoning_start,
    event_text_delta,
    event_text_done,
)


# Mode-driven extended thinking. Quick mode runs without thinking so the
# answer starts streaming immediately. Deep mode uses the Bedrock-Anthropic
# minimum (1024) — anything lower fails ConverseStream validation with
# `thinking.enabled.budget_tokens: Input should be greater than or equal to
# 1024`. With reasoning now streamed to the UI, even the floor is enough
# to fill the pre-prose silence on a fat deep-mode handoff.
_THINKING_BUDGET_BY_MODE: dict[str, int] = {
    "quick": 0,
    "deep": 1024,
}


def _thinking_budget(mode: ResponseMode) -> int:
    return _THINKING_BUDGET_BY_MODE.get(str(mode), 0)


@dataclass
class ResponseResult:
    full_text: str
    visible_text: str
    elapsed_seconds: float
    usage: dict[str, Any]


def _create_response_agent(
    cfg: AgentConfig,
    mode: ResponseMode,
    system_prompt: str,
    request_id: str,
    thesis_id: str | None,
) -> Agent:
    thinking_budget = _thinking_budget(mode)
    # Phase 2 is single-shot; the Bedrock factory intentionally skips prompt
    # caching to avoid the 1.25x write premium on roughly 12k tokens/turn.
    model = create_response_model(cfg, thinking_budget=thinking_budget)
    return Agent(
        system_prompt=system_prompt,
        model=model,
        callback_handler=None,
        tools=[],
        agent_id="response",
        name="hf_strands_phase2",
        description="Sage chip response phase.",
        trace_attributes={
            "hf.request.id": request_id,
            "hf.thesis.id": thesis_id,
            "hf.phase": "phase2",
        },
    )


async def run_response_phase(
    *,
    mode: ResponseMode,
    theses: list[ThesisContext],
    stories: list[StoryContext],
    user_text: str,
    research_handoff: str,
    request_id: str,
    thesis_id: str | None,
    sse_queue: Queue[bytes | None],
    cfg: AgentConfig | None = None,
    user_id: str | None = None,
    recent_history: str = "",
    language: str = "en",
) -> ResponseResult:
    if cfg is None:
        cfg = get_agent_config()

    t_start = time.monotonic()
    system_prompt = build_phase2_system_prompt(
        mode, theses, stories, user_id=user_id, language=language
    )
    user_prompt = build_phase2_user_prompt(
        user_text, research_handoff, recent_history, mode
    )
    agent = _create_response_agent(
        cfg,
        mode,
        system_prompt=system_prompt,
        request_id=request_id,
        thesis_id=thesis_id,
    )

    full_text = ""
    visible_len_emitted = 0
    json_started = False
    metrics_summary: dict[str, Any] = {}

    # Reasoning block id. Anthropic can emit multiple thinking blocks per
    # turn; mint a fresh id whenever a new block starts after the previous
    # one closed.
    reasoning_id: str | None = None

    # Hold back the trailing N chars on every emission so an in-progress
    # JSON fence (lang-tagged, lang-less, or BARE-brace) can be retracted
    # before going on the wire. The detector in `find_trailing_json_block_start`
    # has three triggers, in priority order:
    #   1. ``\n```json``      — 8 chars
    #   2. ``\n``` …`` + `{`  — fence with optional spacing then a brace
    #   3. fallback: rightmost `{` that precedes a `"citations"` token
    # Trigger 3 only fires once `"citations"` has been fully written. If we
    # only buffer 8 chars and the model emits a bare ``\n\n{\n  "citations"``
    # opener (16+ chars before the keyword), the leading `{\n  "c…` slips out
    # to the wire before the detector catches up — and there is no way to
    # retract bytes already streamed. Buffer 24 chars so the bare-brace
    # opener fits inside the holdback window for any reasonable indentation.
    fence_buffer_chars = 24

    async for event in agent.stream_async(user_prompt):
        # Reasoning deltas arrive on Strands' ReasoningTextStreamEvent shape:
        # `{"reasoningText": "...", "reasoning": True, "delta": {...}}`.
        # Forward them verbatim — they're never appended to full_text so the
        # JSON-fence stripping logic stays focused on user-visible prose.
        if event.get("reasoning") is True:
            reasoning_text = event.get("reasoningText")
            if isinstance(reasoning_text, str) and reasoning_text:
                if reasoning_id is None:
                    reasoning_id = f"reasoning_{uuid.uuid4().hex}"
                    await sse_queue.put(event_reasoning_start(reasoning_id))
                await sse_queue.put(
                    event_reasoning_delta(reasoning_id, reasoning_text)
                )

        delta = event.get("data")
        if isinstance(delta, str) and delta:
            # First visible text after a reasoning block closes the disclosure.
            if reasoning_id is not None:
                await sse_queue.put(event_reasoning_done(reasoning_id))
                reasoning_id = None

            full_text += delta
            if not json_started:
                start = find_trailing_json_block_start(full_text)
                if start == -1:
                    # Emit only the prefix that's safely past the fence
                    # lookahead window. The trailing fence_buffer_chars stay
                    # held in case the next deltas turn out to open the JSON.
                    safe_end = max(
                        visible_len_emitted, len(full_text) - fence_buffer_chars
                    )
                    if safe_end > visible_len_emitted:
                        await sse_queue.put(
                            event_text_delta(
                                full_text[visible_len_emitted:safe_end]
                            )
                        )
                        visible_len_emitted = safe_end
                else:
                    cleaned = full_text[:start].rstrip()
                    if visible_len_emitted < len(cleaned):
                        await sse_queue.put(
                            event_text_delta(cleaned[visible_len_emitted:])
                        )
                        visible_len_emitted = len(cleaned)
                    json_started = True
                    await sse_queue.put(event_text_done(cleaned))

        if "result" in event:
            metrics_summary = summarize_agent_metrics(
                getattr(event["result"], "metrics", None)
            )

    # Close any reasoning block that never produced visible text (rare, but
    # possible if the stream ends mid-thinking).
    if reasoning_id is not None:
        await sse_queue.put(event_reasoning_done(reasoning_id))
        reasoning_id = None

    if not json_started:
        # No JSON block was detected — flush any chars held back by the
        # fence-lookahead buffer so the final message matches full_text.
        if visible_len_emitted < len(full_text):
            await sse_queue.put(
                event_text_delta(full_text[visible_len_emitted:])
            )
        await sse_queue.put(event_text_done(full_text))

    visible_start = find_trailing_json_block_start(full_text)
    visible_text = (
        full_text[:visible_start].rstrip() if visible_start != -1 else full_text
    )
    return ResponseResult(
        full_text=full_text,
        visible_text=visible_text,
        elapsed_seconds=time.monotonic() - t_start,
        usage=metrics_summary,
    )

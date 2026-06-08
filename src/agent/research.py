"""Phase 1: research agent.

Tool-driven evidence gathering. Streams Strands tool events through the
queue the orchestrator owns; the AI SDK route converts them to UI chunks.
Research text is private: Phase 1 runs until the model naturally stops
after either more tool calls or its terminal DONE marker, then Phase 2 takes
over with the captured tool history.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import time
from asyncio import Queue
from dataclasses import dataclass
from typing import Any

from strands import Agent
from strands.tools.executors import ConcurrentToolExecutor
from strands.types.content import Message

from src.agent.config import AgentConfig, get_agent_config
from src.agent.model_provider import create_research_model
from src.agent.models import ResponseMode, StoryContext, ThesisContext, ToolCallRecord
from src.agent.prompt_manager import (
    build_phase1_system_prompt,
    build_phase1_user_prompt,
)
from src.agent.sse_emitter import event_tool_use_end, event_tool_use_start
from src.agent.tools import build_strands_tools

# Mode-aware handoff truncation. Must mirror `_TOOL_OUTPUT_CAP_BY_MODE` in
# tools.py — the handoff is the second-stage formatter of the same tool
# output, and capping it tighter than the dispatch cap would silently strip
# evidence the writer has already paid for.
_TOOL_OUTPUT_CHAR_LIMIT_BY_MODE: dict[str, int] = {
    "quick": 12_000,
    "deep": 24_000,
}
_DEFAULT_TOOL_OUTPUT_CHAR_LIMIT = _TOOL_OUTPUT_CHAR_LIMIT_BY_MODE["quick"]


@dataclass
class ResearchPackage:
    raw_text: str
    tool_call_count: int
    elapsed_seconds: float
    usage: dict[str, Any]
    messages: list[Message]
    tool_call_records: list[ToolCallRecord]


class _StderrSink:
    """Suppress known Strands/OpenTelemetry teardown noise."""

    _NOISE_RE = re.compile(
        r"Failed to detach context|created in a different Context|GeneratorExit",
        re.IGNORECASE,
    )

    def write(self, text: str) -> int:
        if self._NOISE_RE.search(text):
            return len(text)
        return sys.__stderr__.write(text)

    def flush(self) -> None:
        sys.__stderr__.flush()


def _create_research_agent(
    cfg: AgentConfig,
    system_prompt: str,
    request_id: str,
    thesis_id: str | None,
    user_id: str,
    mode: ResponseMode,
) -> Agent:
    model = create_research_model(cfg)
    tools = build_strands_tools(user_id=user_id, mode=str(mode))
    return Agent(
        system_prompt=system_prompt,
        model=model,
        callback_handler=None,
        tool_executor=ConcurrentToolExecutor(),
        tools=tools,
        agent_id="research",
        name="hf_strands_phase1",
        description="Sage chip research phase.",
        trace_attributes={
            "hf.request.id": request_id,
            "hf.thesis.id": thesis_id,
            "hf.user.id": user_id,
            "hf.phase": "phase1",
        },
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated: original={len(text)} kept={limit}]"


def _count_tool_calls(messages: list[Message]) -> int:
    return sum(
        1
        for msg in messages
        for content in msg.get("content", [])
        if isinstance(content, dict) and "toolUse" in content
    )


def _build_tool_records(messages: list[Message]) -> list[ToolCallRecord]:
    """Pair toolUse + toolResult content blocks into structured records.

    Phase 2 (response) consumes the text rendering via `_build_tool_history`.
    Phase 2b (chart) consumes these structured records directly.
    """
    records: list[ToolCallRecord] = []
    pending: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for msg in messages:
        for content in msg.get("content", []):
            if not isinstance(content, dict):
                continue
            if "toolUse" in content:
                tu = content["toolUse"]
                tid = str(tu.get("toolUseId") or "")
                pending[tid] = {
                    "tool": str(tu.get("name") or "unknown"),
                    "input": tu.get("input") or {},
                }
                order.append(tid)
                continue
            if "toolResult" in content:
                tr = content["toolResult"]
                tid = str(tr.get("toolUseId") or "")
                call = pending.pop(tid, None)
                records.append(
                    ToolCallRecord(
                        tool_use_id=tid or "unknown",
                        tool=(call or {}).get("tool", "unknown"),
                        input=(call or {}).get("input") or {},
                        output=tr.get("content"),
                        status=str(tr.get("status") or "unknown"),
                    )
                )
                if tid in order:
                    order.remove(tid)
    for tid in order:
        call = pending.get(tid, {})
        records.append(
            ToolCallRecord(
                tool_use_id=tid or "unknown",
                tool=call.get("tool", "unknown"),
                input=call.get("input") or {},
                output=None,
                status="pending",
            )
        )
    return records


def _build_tool_history(
    messages: list[Message],
    *,
    char_limit: int = _DEFAULT_TOOL_OUTPUT_CHAR_LIMIT,
) -> str:
    """Compose a flat tool-call history for Phase 2 consumption."""
    records = _build_tool_records(messages)
    if not records:
        return "(no tools called)"
    return "\n\n".join(
        "\n".join(
            [
                f"tool: {r.tool}",
                f"tool_use_id: {r.tool_use_id}",
                f"input: {json.dumps(r.input, default=str)}",
                f"output: {_truncate(json.dumps(r.output, default=str), char_limit)}"
                if r.output is not None
                else "output: (missing tool result)",
                f"status: {r.status}",
            ]
        )
        for r in records
    )


def _format_handoff(tool_history: str) -> str:
    """Phase 1 → Phase 2 handoff. Only the tool call history is unique to this
    phase — thesis metadata and date already live in the system prompt."""
    return f"""PHASE1_HANDOFF
tool_call_history:
{tool_history}
END_PHASE1_HANDOFF"""


async def _run_phase1_stream(
    agent: Agent,
    user_prompt: str,
    sse_queue: Queue[bytes | None],
) -> tuple[str, dict[str, Any]]:
    """Drive Phase 1 to completion and emit tool SSE events.

    Research text is intentionally not forwarded to the UI. The model may
    emit short private transition text between tool rounds and must emit DONE
    as terminal text when it decides research is complete. We allow the
    Strands/Bedrock loop to finish naturally so a text preface cannot kill a
    follow-up tool call that the model was about to make.
    """
    pending_tool_ids: set[str] = set()
    tool_started_at: dict[str, float] = {}
    tool_name_by_id: dict[str, str] = {}
    stop_reason = "end_turn"
    metrics_summary: dict[str, Any] = {}

    stream = agent.stream_async(user_prompt)
    with contextlib.redirect_stderr(_StderrSink()):
        try:
            while True:
                try:
                    event = await stream.__anext__()
                except StopAsyncIteration:
                    break

                if "message" in event:
                    message = event["message"]
                    for content in message.get("content", []):
                        if not isinstance(content, dict):
                            continue
                        if "toolUse" in content:
                            tu = content["toolUse"]
                            tid = str(tu.get("toolUseId") or "")
                            name = str(tu.get("name") or "unknown")
                            if tid:
                                pending_tool_ids.add(tid)
                                tool_started_at[tid] = time.monotonic()
                                tool_name_by_id[tid] = name
                                await sse_queue.put(
                                    event_tool_use_start(tid, name, tu.get("input"))
                                )
                    for content in message.get("content", []):
                        if not isinstance(content, dict):
                            continue
                        if "toolResult" in content:
                            tr = content["toolResult"]
                            tid = str(tr.get("toolUseId") or "")
                            if tid:
                                pending_tool_ids.discard(tid)
                                started = tool_started_at.pop(tid, time.monotonic())
                                duration_ms = int((time.monotonic() - started) * 1000)
                                name = tool_name_by_id.pop(tid, "unknown")
                                await sse_queue.put(
                                    event_tool_use_end(
                                        tid,
                                        name,
                                        duration_ms,
                                        output=tr.get("content"),
                                    )
                                )

                if "result" in event:
                    result = event["result"]
                    stop_reason = str(result.stop_reason)
                    from src.agent.observability import summarize_agent_metrics

                    metrics_summary = summarize_agent_metrics(
                        getattr(result, "metrics", None)
                    )
                    break
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    return stop_reason, metrics_summary


async def run_research_phase(
    *,
    mode: ResponseMode,
    theses: list[ThesisContext],
    stories: list[StoryContext],
    user_text: str,
    recent_history: str,
    request_id: str,
    user_id: str,
    thesis_id: str | None,
    sse_queue: Queue[bytes | None],
    cfg: AgentConfig | None = None,
) -> ResearchPackage:
    if cfg is None:
        cfg = get_agent_config()

    t_start = time.monotonic()
    system_prompt = build_phase1_system_prompt(mode, theses, stories, user_id=user_id)
    char_limit = _TOOL_OUTPUT_CHAR_LIMIT_BY_MODE.get(
        str(mode), _DEFAULT_TOOL_OUTPUT_CHAR_LIMIT
    )
    user_prompt = build_phase1_user_prompt(user_text, recent_history, mode)

    def _new_agent() -> Agent:
        return _create_research_agent(
            cfg,
            system_prompt=system_prompt,
            request_id=request_id,
            thesis_id=thesis_id,
            user_id=user_id,
            mode=mode,
        )

    # An empty phase-1 stream — zero tool calls AND zero output tokens — is an
    # upstream provider flake, not a deliberate "no research needed" decision:
    # that decision still emits the terminal DONE marker (nonzero output). Handing
    # an empty research package to the response phase makes it fabricate ungrounded
    # specifics, so retry once with a fresh agent before giving up.
    agent = _new_agent()
    for attempt in range(2):
        try:
            stop_reason, metrics_summary = await asyncio.wait_for(
                _run_phase1_stream(agent, user_prompt, sse_queue),
                timeout=cfg.agent_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ResearchPackage(
                raw_text=f"PHASE1_TIMEOUT after {cfg.agent_timeout_seconds}s",
                tool_call_count=0,
                elapsed_seconds=time.monotonic() - t_start,
                usage={},
                messages=agent.messages,
                tool_call_records=_build_tool_records(agent.messages),
            )
        empty_stream = _count_tool_calls(agent.messages) == 0 and not metrics_summary.get(
            "outputTokens"
        )
        if not empty_stream or attempt == 1:
            break
        agent = _new_agent()

    tool_history = _build_tool_history(agent.messages, char_limit=char_limit)
    handoff = _format_handoff(tool_history)
    return ResearchPackage(
        raw_text=handoff,
        tool_call_count=_count_tool_calls(agent.messages),
        elapsed_seconds=time.monotonic() - t_start,
        usage=metrics_summary,
        messages=agent.messages,
        tool_call_records=_build_tool_records(agent.messages),
    )

"""Top-level Sage turn orchestrator.

Runs Phase 1 → Phase 2 (and Phase 2b chart agent in parallel when
`req.enable_charts` is True). Emits internal queue events as SSE rows; the
AI SDK compatibility route adapts those chunks into Vercel UI Message Stream
chunks for the frontend.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator

from src.agent.chart import run_chart_phase
from src.agent.config import get_agent_config
from src.agent.model_provider import (
    chart_model_id,
    research_model_id,
    response_model_id,
)
from src.agent.models import AgentRunRequest
from src.agent.observability import request_trace_context
from src.agent.research import run_research_phase
from src.agent.response import run_response_phase
from src.agent.sse_emitter import (
    event_agent_phase,
    event_error,
    event_result,
    event_start,
)
from src.agent.usage_recorder import PhaseUsage, record_usage

_LOGGER = logging.getLogger("hf_workbench.agent.orchestrator")


async def run_chip_streaming(req: AgentRunRequest) -> AsyncIterator[bytes]:
    cfg = get_agent_config()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    primary_thesis = req.primary_thesis
    thesis_id = primary_thesis.id if primary_thesis else None

    async def driver() -> None:
        try:
            with request_trace_context(
                request_id=req.request_id,
                user_id=req.user_id,
                thesis_id=thesis_id,
                session_id=req.session_id,
            ):
                research = await run_research_phase(
                    mode=req.mode,
                    theses=req.theses,
                    stories=req.stories,
                    user_text=req.user_text,
                    recent_history=req.recent_history,
                    request_id=req.request_id,
                    user_id=req.user_id,
                    thesis_id=thesis_id,
                    sse_queue=queue,
                    cfg=cfg,
                )

                # Phase 2 (response) and Phase 2b (chart) run in parallel against
                # the same SSE queue. Chart phase is opt-in per session.
                await queue.put(event_agent_phase(phase="response"))
                response_coro = run_response_phase(
                    mode=req.mode,
                    theses=req.theses,
                    stories=req.stories,
                    user_text=req.user_text,
                    research_handoff=research.raw_text,
                    request_id=req.request_id,
                    thesis_id=thesis_id,
                    sse_queue=queue,
                    cfg=cfg,
                    user_id=req.user_id,
                    recent_history=req.recent_history,
                    language=req.language,
                )

                chart_elapsed = 0.0
                chart_usage: dict = {}
                if req.enable_charts:
                    chart_coro = run_chart_phase(
                        primary_thesis,
                        req.user_text,
                        research.tool_call_records,
                        theme=req.theme,
                        request_id=req.request_id,
                        sse_queue=queue,
                        cfg=cfg,
                        user_id=req.user_id,
                        session_id=req.session_id,
                    )
                    response, chart = await asyncio.gather(response_coro, chart_coro)
                    chart_elapsed = chart.elapsed_seconds
                    chart_usage = chart.usage
                else:
                    response = await response_coro

            duration_ms = int(
                (research.elapsed_seconds + response.elapsed_seconds + chart_elapsed)
                * 1000
            )

            phases = [
                PhaseUsage(
                    phase="research",
                    model_id=research_model_id(cfg),
                    usage=research.usage,
                ),
                PhaseUsage(
                    phase="response",
                    model_id=response_model_id(cfg),
                    usage=response.usage,
                ),
            ]
            if req.enable_charts:
                phases.append(
                    PhaseUsage(
                        phase="chart",
                        model_id=chart_model_id(cfg),
                        usage=chart_usage,
                    )
                )

            recorded = record_usage(
                request_id=req.request_id,
                user_id=req.user_id,
                endpoint="chat",
                phases=phases,
                session_id=req.session_id,
            )

            usage = {
                "phase1": research.usage,
                "phase2": response.usage,
            }
            if req.enable_charts:
                usage["phase2b"] = chart_usage
            await queue.put(
                event_result(
                    total_cost_usd=recorded["cost_usd"],
                    duration_ms=duration_ms,
                    model_usage=usage,
                    full_text=response.full_text,
                )
            )
        except Exception as exc:  # noqa: BLE001  — boundary; surfaced as SSE error
            # Log with traceback so Bedrock validation errors and other
            # mid-stream failures show up in pm2 / server logs, not only in
            # the user-facing SSE `error` event.
            _LOGGER.exception(
                "agent run failed request_id=%s session_id=%s thesis_id=%s",
                req.request_id,
                req.session_id,
                thesis_id,
            )
            await queue.put(event_error(str(exc)))
        finally:
            await queue.put(None)

    yield event_start(req.request_id)

    task = asyncio.create_task(driver())
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
    finally:
        # Client disconnect (or upstream cancellation) propagates here as
        # GeneratorExit / CancelledError. Cancel the driver so the Strands
        # phases stop doing tool calls / Bedrock work for an absent listener.
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

"""Phase 2b: chart agent.

Runs in parallel with `response.py`. Takes the search agent's structured
tool-call history, decides whether one matplotlib chart would help, and (if
yes) renders it inside an AWS Bedrock AgentCore Code Interpreter sandbox.
Returns either a base64 image payload or a skip reason.

Architecture mirrors the reference impl at
awsstrat/heurist_finance_agent/agent.py — same Strands + AgentCore wiring,
same Bedrock model, same JSON action verbs (`initSession`, `writeFiles`,
`executeCode`).

Hard guarantees:
- The agent never fetches market data. It only sees the tool history.
- Every generated snippet starts with `apply_style(theme)` from chart_style.
- One chart per turn. Saved to `/tmp/chart.png`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from asyncio import Queue
from dataclasses import dataclass
from typing import Any

from strands import Agent

from src.agent.ci_run_recorder import record_ci_run
from src.agent.code_interpreter import (
    ImagePayload,
    fetch_image,
    init_session,
    make_session,
    write_chart_style,
)
from src.agent.config import AgentConfig, get_agent_config
from src.agent.model_provider import chart_model_id, create_chart_model
from src.agent.models import Theme, ThesisContext, ToolCallRecord
from src.agent.observability import (
    attach_langfuse_observation_io,
    get_tracer,
    print_agent_log,
    record_code_interpreter_metrics,
    summarize_agent_metrics,
)
from src.agent.r2_storage import r2_configured, upload_chart
from src.agent.sse_emitter import event_chart_image, event_chart_skip


CHART_REMOTE_PATH = "/tmp/chart.png"

CHART_SYSTEM_PROMPT_TEMPLATE = """You are the chart agent. Your only job is to decide whether ONE matplotlib chart would help the user understand the data the research agent already collected, and if so, to render it.

You receive:
- The user's original question
- The thesis under discussion
- A JSON list of the research agent's tool calls (tool name + inputs + outputs)
- A theme: "dark" or "light"

Decide first: PLOT or SKIP.

Real users phrase questions analytically — "Compare X and Y", "Analyze the
bullish factors of Z", "Walk me through where this thesis stands", "What's
driving the move in W". They almost never say "show me a chart", "plot",
"render", or "visually". Your decision MUST be driven by the SHAPE of the
data the research agent collected, not by whether the user used a charting
verb. Treat explicit chart phrasing as a weak hint at best.

Plot when ALL are true:
- The tool history contains structured numeric data — either ≥3 categorical
  bars (e.g. counts across categories, peer values on a single metric) or
  ≥5 temporal points on a non-price series.
- The data fits one of the PREFERRED chart shapes below.
- A chart would communicate the answer faster than prose.

PREFERRED chart shapes (these are the only ones you should produce):
- ROI / total return comparison across tickers, baskets, or sectors.
- Revenue, earnings, or fundamentals series for one or more companies.
- Growth-rate bars (YoY, QoQ) across companies or segments.
- Sector or sub-industry breakdowns (allocation, exposure, contribution).
- Category / segment / product-line breakdowns of sales, units, or margin.
- Distribution comparisons (e.g. histogram of returns, peer-group multiples).
- Peer-group comparisons on a single metric (P/E, FCF yield, margin).

SKIP when ANY is true:
- The only numeric data available is a raw price (or close) time series. Price
  charts are deprecated by product policy — they are rarely the most useful
  visualization for thesis-driven analysis. SKIP even if the user appears to
  ask for one.
- The data is qualitative (news headlines, narrative text, classifications
  without counts).
- There are fewer than 3 categorical bars and fewer than 5 temporal points.
- The question is purely definitional ("what does X mean?", "summarize Y",
  "list the risks") AND the tool history has no comparative numeric structure
  to surface.
- The data has no clear comparative or distributional structure.
- The only fit you can find is none of the PREFERRED shapes above.

Do NOT skip just because the user did not explicitly ask for a chart. If
the data fits a preferred shape and answers the question faster than prose,
render it. Conversely, do NOT plot just because the user said "chart" —
phrasing alone is never sufficient justification.

If you SKIP: your reply MUST start with the literal token `SKIP:` followed by one short sentence reason. Do not wrap it in prose. No tool calls. For price-only data, use reason `price chart suppressed by policy`.

If you PLOT, follow this exact procedure:
1. Use the existing Code Interpreter session named `{session_name}`. It is already initialized and already contains `chart_style.py`; do NOT call `initSession`.
2. Call code_interpreter with action `writeFiles` to save the data you extracted from the tool history as `data.json`. Do NOT inline large datasets in the executed code — write them to a file first.
3. Call code_interpreter with action `executeCode`. The code MUST start with:
       from chart_style import apply_style, finalize_figure
       apply_style({theme!r})
   Then load `data.json`, build ONE figure with matplotlib, call `finalize_figure(fig)`, and save to `{remote_path}`.
4. After saving, your reply MUST end with the literal token `PLOT_DONE` on its own line. Then stop.

Hard rules:
- Never fetch data. Use only what's in the tool history.
- Never invent or simulate data. If the data is missing, SKIP.
- Never import yfinance or any market data library. The sandbox is for analysis, not data fetching.
- Never produce a price (or close) time-series chart. SKIP with reason `price chart suppressed by policy`.
- ONE chart per turn. No subplots unless the question explicitly compares two series.
- Save meaningful titles/labels. Do not leave default `Figure` titles.
- The style helper handles colors, grid, and spines. Do NOT override grid or spines manually.

Tool-call argument examples (copy this wrapped JSON shape exactly):
- write file:
  {{"code_interpreter_input": {{"action": {{"type": "writeFiles", "session_name": "{session_name}", "content": [{{"path": "data.json", "text": "{{...}}"}}]}}}}}}
- execute python:
  {{"code_interpreter_input": {{"action": {{"type": "executeCode", "session_name": "{session_name}", "language": "python", "code": "..."}}}}}}
"""


@dataclass(slots=True)
class ChartResult:
    url: str | None
    caption: str | None
    skip_reason: str | None
    elapsed_seconds: float
    usage: dict[str, Any]
    outcome: str = "unknown"  # plot | skip | error | timeout | unknown
    failure_stage: str | None = None  # init | agent | image_fetch | upload | r2
    execute_count: int = 0
    write_count: int = 0
    image_bytes: int | None = None


def _result(
    *,
    outcome: str,
    t_start: float,
    failure_stage: str | None = None,
    skip_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    execute_count: int = 0,
    write_count: int = 0,
    image_bytes: int | None = None,
    url: str | None = None,
    caption: str | None = None,
) -> "ChartResult":
    return ChartResult(
        url=url,
        caption=caption,
        skip_reason=skip_reason,
        elapsed_seconds=time.monotonic() - t_start,
        usage=usage or {},
        outcome=outcome,
        failure_stage=failure_stage,
        execute_count=execute_count,
        write_count=write_count,
        image_bytes=image_bytes,
    )


def _count_ci_actions(messages: list[Any]) -> tuple[int, int]:
    """Count sandbox executeCode / writeFiles actions from the chart agent's
    message history. Token-search on the stringified tool input is robust to
    whichever input key the Strands code_interpreter wrapper uses."""
    execute = write = 0
    for msg in messages:
        for content in (msg.get("content", []) if isinstance(msg, dict) else []):
            if not isinstance(content, dict) or "toolUse" not in content:
                continue
            tu = content["toolUse"]
            if tu.get("name") != "code_interpreter":
                continue
            blob = json.dumps(tu.get("input") or {}, default=str)
            if "executeCode" in blob:
                execute += 1
            elif "writeFiles" in blob:
                write += 1
    return execute, write


def _create_chart_agent(
    cfg: AgentConfig,
    *,
    session_name: str,
    theme: Theme,
    request_id: str,
    thesis_id: str | None,
    code_interpreter_tool,
) -> Agent:
    model = create_chart_model(cfg)
    system_prompt = CHART_SYSTEM_PROMPT_TEMPLATE.format(
        session_name=session_name,
        theme=theme,
        remote_path=CHART_REMOTE_PATH,
    )
    return Agent(
        system_prompt=system_prompt,
        model=model,
        callback_handler=None,
        tools=[code_interpreter_tool],
        agent_id="chart",
        name="hf_strands_phase2b",
        description="Sage chart agent (parallel to response).",
        trace_attributes={
            "hf.request.id": request_id,
            "hf.thesis.id": thesis_id,
            "hf.phase": "phase2b",
        },
    )


def _records_to_payload(records: list[ToolCallRecord], char_budget: int) -> list[dict[str, Any]]:
    """Trim outputs so we don't blow the chart agent's context window."""
    trimmed: list[dict[str, Any]] = []
    remaining = char_budget
    for r in records:
        body = r.model_dump()
        out_text = json.dumps(body.get("output"), default=str)
        if len(out_text) > remaining:
            body["output"] = (
                out_text[:remaining]
                + f"... [truncated; original={len(out_text)} chars]"
            )
            remaining = 0
        else:
            remaining -= len(out_text)
        trimmed.append(body)
        if remaining <= 0:
            break
    return trimmed


def _parse_decision(text: str) -> tuple[str, str | None]:
    """Return ('skip', reason) or ('plot', None) or ('unknown', text).

    Lenient on format, strict on the verb. PLOT wins over SKIP when both
    tokens appear (the agent rendered a chart and added prose).
    """
    stripped = text.strip()
    upper = stripped.upper()
    if "PLOT_DONE" in upper:
        return "plot", None
    # Scan the whole reply for SKIP. The system prompt asks the agent to
    # lead with `SKIP:`, but Bedrock models routinely produce extensive
    # markdown analysis (`**Analysis:** …`) before ever writing the verb.
    # A position-bounded check (~240 chars) misses those cases. Falling
    # back to "skip" on any occurrence of SKIP is safe: PLOT_DONE was
    # already checked first, so a real plot won't be misclassified, and
    # the agent's prompt only blesses SKIP as a decision verb anyway.
    skip_idx = upper.find("SKIP")
    if skip_idx >= 0:
        rest = stripped[skip_idx + 4:]
        # Strip leading markdown / punctuation / whitespace the agent
        # piled on after the SKIP token (e.g. "**\n\nThe tool history…").
        rest = rest.lstrip("*: -—_\n\t").strip()
        # Reason = first non-empty paragraph after SKIP. Splitting on
        # blank lines preserves a full sentence even when the agent
        # included internal newlines (markdown lists etc).
        paras = [p.strip().lstrip("*").strip() for p in rest.split("\n\n")]
        reason = next((p for p in paras if p), "")
        return "skip", (reason[:300] or "agent skipped without reason")
    return "unknown", stripped


async def run_chart_phase(
    thesis: ThesisContext | None,
    user_question: str,
    tool_call_records: list[ToolCallRecord],
    *,
    theme: Theme,
    request_id: str,
    sse_queue: Queue[bytes | None],
    cfg: AgentConfig | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> ChartResult:
    """Drive Phase 2b, then funnel run stats to all three observability sinks.

    The chart phase runs inside `request_trace_context` (orchestrator), so the
    `code_interpreter.run` span opened here nests under the request trace and is
    visible in Langfuse. After the run we write: the SQLite `code_interpreter_runs`
    row, the Langfuse span attributes, the OTel metrics, and a structured log line.
    Recording never raises — telemetry must not break the response stream.
    """
    if cfg is None:
        cfg = get_agent_config()
    t_start = time.monotonic()

    with get_tracer("hf.agent.code_interpreter").start_as_current_span(
        "code_interpreter.run"
    ) as span:
        try:
            result = await _run_chart_phase_impl(
                thesis,
                user_question,
                tool_call_records,
                theme=theme,
                request_id=request_id,
                sse_queue=sse_queue,
                cfg=cfg,
            )
        except Exception as exc:  # noqa: BLE001 - chart telemetry must not break chat
            reason = f"chart setup failed: {exc}"
            await sse_queue.put(event_chart_skip(reason=reason))
            result = _result(
                outcome="error",
                failure_stage="init",
                skip_reason=reason,
                t_start=t_start,
            )
        _record_ci_run(
            result,
            span=span,
            request_id=request_id,
            user_id=user_id or "",
            session_id=session_id,
            model_id=chart_model_id(cfg),
        )
        return result


def _record_ci_run(
    result: ChartResult,
    *,
    span: Any,
    request_id: str,
    user_id: str,
    session_id: str | None,
    model_id: str,
) -> None:
    """Fan one ChartResult out to SQLite + Langfuse span + OTel metrics + log."""
    elapsed_ms = int(result.elapsed_seconds * 1000)

    record_ci_run(
        request_id=request_id,
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        outcome=result.outcome,
        failure_stage=result.failure_stage,
        skip_reason=result.skip_reason,
        execute_count=result.execute_count,
        write_count=result.write_count,
        image_bytes=result.image_bytes,
        elapsed_ms=elapsed_ms,
    )

    with contextlib.suppress(Exception):
        span.set_attribute("heurist.ci.outcome", result.outcome)
        span.set_attribute("heurist.ci.failure_stage", result.failure_stage or "none")
        span.set_attribute("heurist.ci.execute_count", result.execute_count)
        span.set_attribute("heurist.ci.write_count", result.write_count)
        span.set_attribute("heurist.ci.elapsed_ms", elapsed_ms)
        if result.skip_reason:
            span.set_attribute("heurist.ci.skip_reason", result.skip_reason)
        if result.image_bytes is not None:
            span.set_attribute("heurist.ci.image_bytes", result.image_bytes)
        attach_langfuse_observation_io(
            span,
            output_value={
                "outcome": result.outcome,
                "failure_stage": result.failure_stage,
                "skip_reason": result.skip_reason,
                "execute_count": result.execute_count,
                "write_count": result.write_count,
                "image_bytes": result.image_bytes,
                "elapsed_ms": elapsed_ms,
            },
        )

    record_code_interpreter_metrics(
        outcome=result.outcome,
        failure_stage=result.failure_stage,
        elapsed_ms=elapsed_ms,
        execute_count=result.execute_count,
        write_count=result.write_count,
    )

    with contextlib.suppress(Exception):
        print_agent_log(
            "code_interpreter.run",
            request_id=request_id,
            purpose="chart",
            outcome=result.outcome,
            failure_stage=result.failure_stage,
            skip_reason=result.skip_reason,
            execute_count=result.execute_count,
            write_count=result.write_count,
            image_bytes=result.image_bytes,
            elapsed_ms=elapsed_ms,
        )


async def _run_chart_phase_impl(
    thesis: ThesisContext | None,
    user_question: str,
    tool_call_records: list[ToolCallRecord],
    *,
    theme: Theme,
    request_id: str,
    sse_queue: Queue[bytes | None],
    cfg: AgentConfig,
) -> ChartResult:
    """Decide PLOT/SKIP and render. Always emits exactly one of:
    chart_image | chart_skip. Failures become a chart_skip event so the
    response stream is never blocked by chart issues."""
    t_start = time.monotonic()

    session_name = f"chart-{request_id}"
    ci = make_session(region=cfg.aws_region, session_name=session_name)
    agent = _create_chart_agent(
        cfg,
        session_name=session_name,
        theme=theme,
        request_id=request_id,
        thesis_id=thesis.id if thesis else None,
        code_interpreter_tool=ci.code_interpreter,
    )

    # Bootstrap the sandbox: init + push chart_style.py before the agent runs.
    # If this fails, skip cleanly; never crash the response phase.
    try:
        init_session(ci, session_name)
        write_chart_style(ci, session_name)
    except Exception as exc:  # noqa: BLE001 - boundary; convert to skip
        await sse_queue.put(event_chart_skip(reason=f"sandbox init failed: {exc}"))
        return _result(outcome="error", failure_stage="init", skip_reason=str(exc), t_start=t_start)

    char_budget = max(2_000, cfg.chart_agent_max_tokens * 3)
    user_prompt = json.dumps(
        {
            "user_question": user_question,
            "thesis": (
                {"id": thesis.id, "statement": thesis.statement, "tickers": thesis.tickers}
                if thesis
                else None
            ),
            "theme": theme,
            "tool_call_history": _records_to_payload(tool_call_records, char_budget),
        },
        default=str,
    )

    try:
        result = await asyncio.wait_for(
            agent.invoke_async(user_prompt),
            timeout=cfg.chart_agent_timeout_seconds,
        )
    except asyncio.TimeoutError:
        reason = f"chart agent timed out after {cfg.chart_agent_timeout_seconds}s"
        await sse_queue.put(event_chart_skip(reason=reason))
        return _result(outcome="timeout", failure_stage="agent", skip_reason=reason, t_start=t_start)
    except Exception as exc:  # noqa: BLE001 - never block response
        reason = f"chart agent error: {exc}"
        await sse_queue.put(event_chart_skip(reason=reason))
        return _result(outcome="error", failure_stage="agent", skip_reason=reason, t_start=t_start)

    # The agent ran: capture its token usage (for agent_usage) and sandbox
    # action counts once, and reuse them for every post-run branch.
    text = str(result)
    usage = summarize_agent_metrics(getattr(result, "metrics", None))
    execute_count, write_count = _count_ci_actions(getattr(agent, "messages", []) or [])
    decision, payload = _parse_decision(text)

    def _post_run(**kw: Any) -> ChartResult:
        return _result(
            t_start=t_start,
            usage=usage,
            execute_count=execute_count,
            write_count=write_count,
            **kw,
        )

    if decision == "skip":
        await sse_queue.put(event_chart_skip(reason=payload or "skipped"))
        return _post_run(outcome="skip", skip_reason=payload)

    if decision == "unknown":
        # Surface the raw text so future regressions of this branch are
        # diagnosable without another instrumentation pass.
        import sys
        print(
            f"[chart][{request_id}] unknown decision; raw text head: {text[:500]!r}",
            file=sys.stderr,
            flush=True,
        )
        reason = "chart agent returned no decision"
        await sse_queue.put(event_chart_skip(reason=reason))
        return _post_run(outcome="unknown", failure_stage="agent", skip_reason=reason)

    # decision == "plot": fetch the produced image from the sandbox.
    try:
        image: ImagePayload = fetch_image(ci, session_name, CHART_REMOTE_PATH)
    except Exception as exc:  # noqa: BLE001
        reason = f"chart image not produced: {exc}"
        await sse_queue.put(event_chart_skip(reason=reason))
        return _post_run(outcome="error", failure_stage="image_fetch", skip_reason=reason)

    # Upload the original sandbox PNG bytes to R2 and use the public URL in the
    # SSE event. Chart variants are intentionally not generated; charts are
    # already lightweight and the frontend treats them as inspectable artifacts.
    if not r2_configured(cfg):
        reason = "chart upload skipped: R2 is not configured"
        await sse_queue.put(event_chart_skip(reason=reason))
        return _post_run(outcome="error", failure_stage="r2", skip_reason=reason, image_bytes=image.size_bytes)

    try:
        image_bytes = base64.b64decode(image.image_b64)
        ext = "png" if image.mime == "image/png" else (image.mime.split("/", 1)[-1] or "png")
        key = f"charts/{request_id}.{ext}"
        upload = await asyncio.to_thread(
            upload_chart, cfg, key=key, body=image_bytes, content_type=image.mime
        )
    except Exception as exc:  # noqa: BLE001
        reason = f"chart upload failed: {exc}"
        await sse_queue.put(event_chart_skip(reason=reason))
        return _post_run(outcome="error", failure_stage="upload", skip_reason=reason, image_bytes=image.size_bytes)

    caption = _extract_caption(text)
    await sse_queue.put(
        event_chart_image(
            url=upload.url,
            caption=caption,
        )
    )
    return _post_run(outcome="plot", url=upload.url, caption=caption, image_bytes=image.size_bytes)


def _extract_caption(text: str) -> str | None:
    """If the agent wrote a caption alongside PLOT_DONE, surface it."""
    upper = text.upper()
    idx = upper.find("PLOT_DONE")
    if idx == -1:
        return None
    after = text[idx + len("PLOT_DONE"):].strip(" :-\n\t")
    return after[:200] if after else None


__all__ = ["ChartResult", "run_chart_phase", "CHART_SYSTEM_PROMPT_TEMPLATE"]

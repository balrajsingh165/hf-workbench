"""Unit tests for the Phase 2b chart agent.

Mocks both `Agent.invoke_async` (Bedrock LLM) and the Code Interpreter
sandbox so the test suite stays fully offline. Asserts the three behaviours
the requirement promises:

  - SKIP path emits a `chart_skip` SSE event.
  - PLOT path fetches the rendered image and emits a `chart_image` SSE event.
  - Sandbox-init failure converts to a `chart_skip` event (never crashes).
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent.config import AgentConfig
from src.agent.models import ThesisContext, ToolCallRecord


def _cfg() -> AgentConfig:
    return AgentConfig(
        aws_region="us-west-2",
        bedrock_profile="payments-admin",
        bedrock_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        response_bedrock_model_id="us.anthropic.claude-sonnet-4-6",
        research_max_tokens=12_000,
        response_max_tokens=10_000,
        agent_timeout_seconds=300,
        chart_agent_timeout_seconds=10,
        chart_agent_max_tokens=8_000,
        r2_endpoint="https://example.r2.cloudflarestorage.com",
        r2_bucket="test-bucket",
        r2_access_key="ak",
        r2_secret_key="sk",
        r2_public_base_url="https://pub-test.r2.dev",
        langfuse_public_key=None,
        langfuse_secret_key=None,
        langfuse_base_url=None,
        langfuse_environment="test",
        langfuse_service_name="hf-workbench-test",
    )


def _thesis() -> ThesisContext:
    return ThesisContext(id="thesis_001", statement="BTC up", tickers=["BTC-USD"])


def _records() -> list[ToolCallRecord]:
    return [
        ToolCallRecord(
            tool_use_id="t1",
            tool="price_history",
            input={"ticker": "BTC-USD"},
            output={"prices": [60_000, 60_500, 61_200, 60_800, 61_500]},
            status="success",
        )
    ]


async def _drain(queue: asyncio.Queue) -> list[dict]:
    """Pop everything currently on the queue and JSON-decode each event."""
    out: list[dict] = []
    while not queue.empty():
        chunk = queue.get_nowait()
        if chunk is None:
            continue
        body = chunk.decode("utf-8").removeprefix("data: ").strip()
        if body:
            out.append(json.loads(body))
    return out


def _mock_session():
    """A MagicMock-style stand-in for AgentCoreCodeInterpreter."""
    session = SimpleNamespace()
    session.code_interpreter = lambda payload: {"content": [{"text": "[]"}]}
    return session


def test_ci_recording_funnel_is_best_effort():
    from src.agent import chart as chart_mod
    from src.agent.chart import ChartResult

    result = ChartResult(
        url=None,
        caption=None,
        skip_reason="skipped",
        elapsed_seconds=0.01,
        usage={},
        outcome="skip",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    with patch.object(chart_mod, "record_ci_run", side_effect=boom), \
         patch.object(chart_mod, "record_code_interpreter_metrics", side_effect=boom), \
         patch.object(chart_mod, "print_agent_log", side_effect=boom):
        chart_mod._record_ci_run(
            result,
            span=SimpleNamespace(set_attribute=boom),
            request_id="req_test_recording",
            user_id="user_1",
            session_id="session_1",
            model_id="model",
        )


def test_chart_prompt_uses_strands_code_interpreter_wrapper():
    from src.agent.chart import CHART_SYSTEM_PROMPT_TEMPLATE

    prompt = CHART_SYSTEM_PROMPT_TEMPLATE.format(
        session_name="chart-test",
        theme="dark",
        remote_path="/tmp/chart.png",
    )

    assert '"code_interpreter_input"' in prompt
    assert '"action": {"type": "writeFiles"' in prompt
    assert '"action": {"type": "executeCode"' in prompt
    assert 'do NOT call `initSession`' in prompt
    assert '"action": {"type": "initSession"' not in prompt


@pytest.mark.anyio("asyncio")
async def test_skip_path_emits_chart_skip():
    from src.agent import chart as chart_mod

    queue: asyncio.Queue = asyncio.Queue()

    async def fake_invoke_async(self, prompt):
        return "SKIP: data is qualitative"

    with patch.object(chart_mod, "make_session", return_value=_mock_session()), \
         patch.object(chart_mod, "init_session", return_value={}), \
         patch.object(chart_mod, "write_chart_style", return_value={}), \
         patch("strands.Agent.invoke_async", new=fake_invoke_async):
        result = await chart_mod.run_chart_phase(
            _thesis(),
            "What does the data say?",
            _records(),
            theme="dark",
            request_id="req_test_skip",
            sse_queue=queue,
            cfg=_cfg(),
        )

    assert result.url is None
    assert result.outcome == "skip"
    assert result.skip_reason == "data is qualitative"
    events = await _drain(queue)
    assert len(events) == 1
    assert events[0]["type"] == "chart_skip"
    assert events[0]["reason"] == "data is qualitative"


@pytest.mark.anyio("asyncio")
async def test_plot_path_emits_chart_image():
    from src.agent import chart as chart_mod
    from src.agent.code_interpreter import ImagePayload
    from src.agent.r2_storage import R2Upload

    queue: asyncio.Queue = asyncio.Queue()
    fake_image = ImagePayload(
        image_b64=base64.b64encode(b"PNGBYTES").decode(),
        mime="image/png",
        name="chart.png",
        size_bytes=8,
    )

    async def fake_invoke_async(self, prompt):
        return "PLOT_DONE BTC daily close"

    fake_upload = R2Upload(
        url="https://pub-test.r2.dev/charts/req_test_plot.png",
        key="charts/req_test_plot.png",
        size_bytes=8,
    )

    with patch.object(chart_mod, "make_session", return_value=_mock_session()), \
         patch.object(chart_mod, "init_session", return_value={}), \
         patch.object(chart_mod, "write_chart_style", return_value={}), \
         patch.object(chart_mod, "fetch_image", return_value=fake_image), \
         patch.object(chart_mod, "upload_chart", return_value=fake_upload), \
         patch("strands.Agent.invoke_async", new=fake_invoke_async):
        result = await chart_mod.run_chart_phase(
            _thesis(),
            "Plot BTC last 5 days",
            _records(),
            theme="light",
            request_id="req_test_plot",
            sse_queue=queue,
            cfg=_cfg(),
        )

    assert result.url == fake_upload.url
    assert result.outcome == "plot"
    assert result.image_bytes == fake_image.size_bytes
    assert result.skip_reason is None
    assert result.caption == "BTC daily close"

    events = await _drain(queue)
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "chart_image"
    assert ev["url"] == fake_upload.url
    assert ev["caption"] == "BTC daily close"
    assert "key" not in ev
    assert "mime" not in ev
    assert "variants" not in ev


@pytest.mark.anyio("asyncio")
async def test_sandbox_init_failure_skips_cleanly():
    from src.agent import chart as chart_mod

    queue: asyncio.Queue = asyncio.Queue()

    def boom(*args, **kwargs):
        raise RuntimeError("AgentCore unreachable")

    with patch.object(chart_mod, "make_session", return_value=_mock_session()), \
         patch.object(chart_mod, "init_session", side_effect=boom):
        result = await chart_mod.run_chart_phase(
            _thesis(),
            "anything",
            _records(),
            theme="dark",
            request_id="req_test_init_fail",
            sse_queue=queue,
            cfg=_cfg(),
        )

    assert result.url is None
    assert result.outcome == "error"
    assert result.failure_stage == "init"
    assert "AgentCore unreachable" in (result.skip_reason or "")
    events = await _drain(queue)
    assert len(events) == 1
    assert events[0]["type"] == "chart_skip"


@pytest.mark.anyio("asyncio")
async def test_chart_setup_failure_skips_cleanly():
    from src.agent import chart as chart_mod

    queue: asyncio.Queue = asyncio.Queue()

    def boom(*args, **kwargs):
        raise RuntimeError("bad chart session config")

    with patch.object(chart_mod, "make_session", side_effect=boom):
        result = await chart_mod.run_chart_phase(
            _thesis(),
            "anything",
            _records(),
            theme="dark",
            request_id="req_test_setup_fail",
            sse_queue=queue,
            cfg=_cfg(),
        )

    assert result.url is None
    assert result.outcome == "error"
    assert result.failure_stage == "init"
    assert "bad chart session config" in (result.skip_reason or "")
    events = await _drain(queue)
    assert len(events) == 1
    assert events[0]["type"] == "chart_skip"


@pytest.mark.anyio("asyncio")
async def test_unknown_decision_treated_as_skip():
    from src.agent import chart as chart_mod

    queue: asyncio.Queue = asyncio.Queue()

    async def fake_invoke_async(self, prompt):
        return "Hmm, I'm not sure what to do here."

    with patch.object(chart_mod, "make_session", return_value=_mock_session()), \
         patch.object(chart_mod, "init_session", return_value={}), \
         patch.object(chart_mod, "write_chart_style", return_value={}), \
         patch("strands.Agent.invoke_async", new=fake_invoke_async):
        result = await chart_mod.run_chart_phase(
            _thesis(),
            "anything",
            _records(),
            theme="dark",
            request_id="req_test_unknown",
            sse_queue=queue,
            cfg=_cfg(),
        )

    assert result.url is None
    assert result.outcome == "unknown"
    events = await _drain(queue)
    assert events[0]["type"] == "chart_skip"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_ci_recording():
    """These tests exercise SSE/decision behaviour, not the observability
    funnel. Neutralize recording so they stay offline and don't write
    code_interpreter_runs rows into the dev DB."""
    from src.agent import chart as chart_mod

    with patch.object(chart_mod, "_record_ci_run", lambda *a, **k: None):
        yield

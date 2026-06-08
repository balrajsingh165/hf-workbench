"""Observability helpers for Strands, Bedrock usage, and structured logs.

Ported from heurist-finance-backend on 2026-05-05 (masterplan 5.15).
Wires Langfuse + OpenTelemetry on top of Strands. Safe to import without env
vars set — `setup_strands_telemetry()` short-circuits to no-op in that case.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opentelemetry import metrics, trace
from strands.telemetry import StrandsTelemetry

try:
    from langfuse import get_client, propagate_attributes  # type: ignore
except Exception:  # pragma: no cover — runtime-optional helper
    get_client = None  # type: ignore[assignment]
    propagate_attributes = None  # type: ignore[assignment]


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT_DIR / "logs"
TOKEN_PATH = LOG_DIR / "agent_tokens.json"
APP_LOGGER_NAME = "hf_workbench.agent"

_TOKEN_LOCK = threading.Lock()
_TELEMETRY_INITIALIZED = False
_TELEMETRY_PAYLOAD_CHAR_LIMIT = 16_000
_LANGFUSE_TRACE_INPUT = "langfuse.trace.input"
_LANGFUSE_TRACE_OUTPUT = "langfuse.trace.output"
_LANGFUSE_OBSERVATION_INPUT = "langfuse.observation.input"
_LANGFUSE_OBSERVATION_OUTPUT = "langfuse.observation.output"
_HEURIST_OUTPUT_TEXT = "heurist.output.text"
_HEURIST_OUTPUT_LENGTH = "heurist.output.length"
_HEURIST_OUTPUT_TRUNCATED = "heurist.output.truncated"


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _langfuse_base_url() -> str | None:
    return os.getenv("LANGFUSE_BASE_URL") or None


def _langfuse_public_key() -> str | None:
    return os.getenv("LANGFUSE_PUBLIC_KEY") or None


def _langfuse_secret_key() -> str | None:
    return os.getenv("LANGFUSE_SECRET_KEY") or None


def _langfuse_environment() -> str:
    return os.getenv("LANGFUSE_ENVIRONMENT", "development")


def _langfuse_service_name() -> str:
    return os.getenv("LANGFUSE_SERVICE_NAME", "hf-workbench")


def _langfuse_otlp_enabled() -> bool:
    return bool(_langfuse_base_url())


def _langfuse_auth_enabled() -> bool:
    return bool(_langfuse_public_key() and _langfuse_secret_key() and _langfuse_base_url())


def _build_otlp_headers() -> str:
    headers = ["x-langfuse-ingestion-version=4"]
    if _langfuse_auth_enabled():
        creds = f"{_langfuse_public_key()}:{_langfuse_secret_key()}".encode("utf-8")
        auth = base64.b64encode(creds).decode("ascii")
        headers.insert(0, f"Authorization=Basic {auth}")
    return ",".join(headers)


def _configure_langfuse_otlp() -> None:
    base = _langfuse_base_url()
    if not base:
        return
    base = base.rstrip("/")
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", f"{base}/api/public/otel")
    os.environ.setdefault("OTEL_EXPORTER_OTLP_HEADERS", _build_otlp_headers())
    os.environ.setdefault("OTEL_SERVICE_NAME", _langfuse_service_name())
    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        (
            f"service.name={_langfuse_service_name()},"
            f"deployment.environment={_langfuse_environment()},"
            "service.namespace=heurist"
        ),
    )
    os.environ.setdefault(
        "OTEL_SEMCONV_STABILITY_OPT_IN",
        "gen_ai_latest_experimental,gen_ai_tool_definitions",
    )


def initialize_runtime_observability() -> None:
    """Create the runtime log directory and the token accumulator file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_token_totals()


def setup_strands_telemetry() -> None:
    """Configure Strands telemetry exporters from environment.

    No-op when no Langfuse / OTel env vars are set, so dev runs without
    credentials don't pay any setup cost.
    """
    global _TELEMETRY_INITIALIZED
    if _TELEMETRY_INITIALIZED:
        return

    _configure_langfuse_otlp()
    _patch_strands_tracer_output_preview()

    enable_console = _env_flag("ENABLE_STRANDS_CONSOLE_TELEMETRY")
    enable_otlp = _env_flag("ENABLE_STRANDS_OTLP_TELEMETRY")
    enable_console_metrics = _env_flag("ENABLE_STRANDS_CONSOLE_METRICS")
    enable_otlp_metrics = _env_flag("ENABLE_STRANDS_OTLP_METRICS")
    if _langfuse_otlp_enabled():
        enable_otlp = True

    if not any((enable_console, enable_otlp, enable_console_metrics, enable_otlp_metrics)):
        return

    telemetry = StrandsTelemetry()
    if enable_console:
        telemetry.setup_console_exporter()
    if enable_otlp:
        telemetry.setup_otlp_exporter()
    if enable_console_metrics or enable_otlp_metrics:
        telemetry.setup_meter(
            enable_console_exporter=enable_console_metrics,
            enable_otlp_exporter=enable_otlp_metrics,
        )

    if _langfuse_auth_enabled() and get_client is not None:
        try:
            ok = bool(get_client().auth_check())
            logging.getLogger(APP_LOGGER_NAME).info(
                json.dumps(
                    {
                        "event": "langfuse.auth_check",
                        "base_url": _langfuse_base_url(),
                        "service_name": _langfuse_service_name(),
                        "ok": ok,
                    },
                    sort_keys=True,
                )
            )
        except Exception as exc:
            logging.getLogger(APP_LOGGER_NAME).warning(
                json.dumps(
                    {
                        "event": "langfuse.auth_check",
                        "base_url": _langfuse_base_url(),
                        "service_name": _langfuse_service_name(),
                        "ok": False,
                        "error": str(exc),
                    },
                    sort_keys=True,
                )
            )
    elif _langfuse_otlp_enabled():
        logging.getLogger(APP_LOGGER_NAME).info(
            json.dumps(
                {
                    "event": "langfuse.otlp_configured",
                    "base_url": _langfuse_base_url(),
                    "service_name": _langfuse_service_name(),
                    "auth_mode": "none",
                },
                sort_keys=True,
            )
        )
    _TELEMETRY_INITIALIZED = True


@contextmanager
def request_trace_context(
    *,
    request_id: str,
    user_id: str | None = None,
    thesis_id: str | None = None,
    session_id: str | None = None,
):
    """Attach Langfuse trace metadata to all spans opened inside the block."""
    if not _langfuse_otlp_enabled() or propagate_attributes is None:
        yield
        return

    tags = ["hf-workbench", "chat"]
    if user_id:
        tags.append(f"user:{user_id}")
    metadata = {
        "request_id": request_id,
        "user_id": user_id,
        "thesis_id": thesis_id,
        "service": "hf-workbench",
    }
    try:
        ctx = propagate_attributes(
            trace_name="hf-workbench:chat",
            session_id=session_id or request_id,
            user_id=user_id,
            tags=tags,
            metadata={k: v for k, v in metadata.items() if v is not None},
            as_baggage=True,
        )
    except Exception:
        yield
        return

    with ctx:
        yield


def get_tracer(name: str):
    return trace.get_tracer(name)


_CI_METER_NAME = "hf.agent.code_interpreter"
_ci_instruments: dict[str, Any] = {}


def _code_interpreter_instruments() -> dict[str, Any]:
    """Lazily build (and cache) the Code Interpreter OTel instruments.

    `metrics.get_meter` yields a no-op meter when no MeterProvider is configured
    (the default when `ENABLE_STRANDS_*_METRICS` / OTLP metrics are off), so this
    is free on the no-telemetry path.
    """
    if not _ci_instruments:
        meter = metrics.get_meter(_CI_METER_NAME)
        _ci_instruments["runs"] = meter.create_counter(
            "heurist.code_interpreter.runs",
            description="Code Interpreter phase runs, tagged by outcome.",
        )
        _ci_instruments["latency"] = meter.create_histogram(
            "heurist.code_interpreter.latency_ms",
            unit="ms",
            description="Wall-clock duration of a Code Interpreter phase.",
        )
        _ci_instruments["actions"] = meter.create_histogram(
            "heurist.code_interpreter.sandbox_actions",
            description="Sandbox actions (executeCode + writeFiles) per run.",
        )
    return _ci_instruments


def record_code_interpreter_metrics(
    *,
    outcome: str,
    failure_stage: str | None,
    elapsed_ms: int,
    execute_count: int,
    write_count: int,
) -> None:
    """Emit OTel metrics for one Code Interpreter run. Never raises.

    Attributes are deliberately low-cardinality (outcome / failure_stage only);
    per-request identifiers stay on the trace span and the SQLite row. Langfuse
    does not ingest these metrics — they flow to the OTLP/console metrics
    backend; per-trace visibility comes from the span attributes.
    """
    with contextlib.suppress(Exception):
        inst = _code_interpreter_instruments()
        attrs = {"outcome": outcome, "failure_stage": failure_stage or "none"}
        inst["runs"].add(1, attrs)
        inst["latency"].record(int(elapsed_ms), attrs)
        inst["actions"].record(int(execute_count) + int(write_count), attrs)


def _stringify_payload(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(value)


def _trim_payload(text: str, limit: int = _TELEMETRY_PAYLOAD_CHAR_LIMIT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return text[:limit] + f"...[truncated {omitted} chars]", True


def _build_heurist_output_attributes(value: Any) -> dict[str, Any]:
    text = _stringify_payload(value)
    preview, truncated = _trim_payload(text)
    return {
        _HEURIST_OUTPUT_TEXT: preview,
        _HEURIST_OUTPUT_LENGTH: len(text),
        _HEURIST_OUTPUT_TRUNCATED: truncated,
    }


def _extract_message_output(message: Any) -> Any:
    if not isinstance(message, dict):
        return str(message)

    parts: list[str] = []
    for content in message.get("content", []):
        if not isinstance(content, dict):
            continue
        if "text" in content:
            text = str(content["text"]).strip()
            if text:
                parts.append(text)
            continue
        if "toolUse" in content:
            tu = content["toolUse"]
            parts.append(
                f"[tool_use] {tu.get('name')} {_stringify_payload(tu.get('input', {}))}"
            )
            continue
        if "toolResult" in content:
            tr = content["toolResult"]
            parts.append(
                f"[tool_result] id={tr.get('toolUseId')} "
                f"status={tr.get('status', '')} "
                f"{_stringify_payload(tr.get('content', []))}"
            )
    if parts:
        return "\n".join(parts)
    return message


def _patch_strands_tracer_output_preview() -> None:
    """Monkeypatch Strands tracer to attach readable output preview attributes.

    Strands' native chat-observation output sometimes lands as null in
    Langfuse. We add `heurist.output.*` attributes so reviewers can read
    the actual text from the metadata panel.
    """
    with contextlib.suppress(Exception):
        from strands.telemetry import tracer as strands_tracer_module

        tracer_cls = strands_tracer_module.Tracer
        if getattr(tracer_cls, "_hf_output_preview_patch", False):
            return

        original_end_model = tracer_cls.end_model_invoke_span
        original_end_cycle = tracer_cls.end_event_loop_cycle_span
        original_end_agent = tracer_cls.end_agent_span
        original_start_tool = tracer_cls.start_tool_call_span
        original_end_tool = tracer_cls.end_tool_call_span

        def patched_end_model(self, span, message, usage, metrics, stop_reason):
            with contextlib.suppress(Exception):
                span.set_attributes(_build_heurist_output_attributes(_extract_message_output(message)))
            original_end_model(self, span, message, usage, metrics, stop_reason)

        def patched_end_cycle(self, span, message, tool_result_message=None):
            with contextlib.suppress(Exception):
                payload: list[Any] = [_extract_message_output(message)]
                if tool_result_message is not None:
                    payload.append(_extract_message_output(tool_result_message))
                span.set_attributes(_build_heurist_output_attributes(payload))
            original_end_cycle(self, span, message, tool_result_message)

        def patched_end_agent(self, span, response=None, error=None):
            if response is not None:
                with contextlib.suppress(Exception):
                    span.set_attributes(_build_heurist_output_attributes(str(response)))
            original_end_agent(self, span, response, error)

        def patched_start_tool(self, tool, parent_span=None, custom_trace_attributes=None, **kwargs):
            span = original_start_tool(
                self,
                tool,
                parent_span=parent_span,
                custom_trace_attributes=custom_trace_attributes,
                **kwargs,
            )
            with contextlib.suppress(Exception):
                span.set_attributes(
                    build_langfuse_observation_io_attributes(
                        input_value={
                            "tool_name": tool.get("name"),
                            "tool_use_id": tool.get("toolUseId"),
                            "input": tool.get("input", {}),
                        }
                    )
                )
                span.set_attributes(
                    _build_heurist_output_attributes(
                        _extract_message_output({"content": [{"toolUse": tool}]})
                    )
                )
            return span

        def patched_end_tool(self, span, tool_result, error=None):
            if tool_result is not None:
                with contextlib.suppress(Exception):
                    span.set_attributes(
                        build_langfuse_observation_io_attributes(
                            output_value={
                                "tool_use_id": tool_result.get("toolUseId"),
                                "status": tool_result.get("status"),
                                "content": tool_result.get("content", []),
                            }
                        )
                    )
                    span.set_attributes(
                        _build_heurist_output_attributes(
                            _extract_message_output({"content": [{"toolResult": tool_result}]})
                        )
                    )
            original_end_tool(self, span, tool_result, error)

        tracer_cls.end_model_invoke_span = patched_end_model
        tracer_cls.end_event_loop_cycle_span = patched_end_cycle
        tracer_cls.end_agent_span = patched_end_agent
        tracer_cls.start_tool_call_span = patched_start_tool
        tracer_cls.end_tool_call_span = patched_end_tool
        tracer_cls._hf_output_preview_patch = True


def attach_span_payload(span: Any, *, name: str, value: Any) -> None:
    if span is None:
        return
    text = _stringify_payload(value)
    trimmed, truncated = _trim_payload(text)
    base = f"heurist.payload.{name}"
    with contextlib.suppress(Exception):
        span.set_attribute(f"{base}.length", len(text))
        span.set_attribute(f"{base}.truncated", truncated)
        span.set_attribute(base, trimmed)
        span.add_event(
            f"heurist.payload.{name}",
            {
                "name": name,
                "length": len(text),
                "truncated": truncated,
                "payload": trimmed,
            },
        )


def _build_langfuse_io_attributes(
    *,
    input_value: Any = None,
    output_value: Any = None,
    input_key: str,
    output_key: str,
) -> dict[str, str]:
    attrs: dict[str, str] = {}
    if input_value is not None:
        attrs[input_key] = _trim_payload(_stringify_payload(input_value))[0]
    if output_value is not None:
        attrs[output_key] = _trim_payload(_stringify_payload(output_value))[0]
    return attrs


def build_langfuse_trace_io_attributes(
    *, input_value: Any = None, output_value: Any = None
) -> dict[str, str]:
    return _build_langfuse_io_attributes(
        input_value=input_value,
        output_value=output_value,
        input_key=_LANGFUSE_TRACE_INPUT,
        output_key=_LANGFUSE_TRACE_OUTPUT,
    )


def build_langfuse_observation_io_attributes(
    *, input_value: Any = None, output_value: Any = None
) -> dict[str, str]:
    return _build_langfuse_io_attributes(
        input_value=input_value,
        output_value=output_value,
        input_key=_LANGFUSE_OBSERVATION_INPUT,
        output_key=_LANGFUSE_OBSERVATION_OUTPUT,
    )


def attach_langfuse_trace_io(
    span: Any, *, input_value: Any = None, output_value: Any = None
) -> None:
    if span is None:
        return
    for key, value in build_langfuse_trace_io_attributes(
        input_value=input_value, output_value=output_value
    ).items():
        with contextlib.suppress(Exception):
            span.set_attribute(key, value)


def attach_langfuse_observation_io(
    span: Any, *, input_value: Any = None, output_value: Any = None
) -> None:
    if span is None:
        return
    for key, value in build_langfuse_observation_io_attributes(
        input_value=input_value, output_value=output_value
    ).items():
        with contextlib.suppress(Exception):
            span.set_attribute(key, value)


def flush_telemetry() -> None:
    provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if callable(force_flush):
        with contextlib.suppress(Exception):
            force_flush()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_token_payload() -> dict[str, Any]:
    now = _utc_now()
    return {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cached_input_tokens": 0,
        "total_cache_write_input_tokens": 0,
        "created_at": now,
        "last_updated": now,
    }


def _ensure_token_totals() -> None:
    if not TOKEN_PATH.exists():
        TOKEN_PATH.write_text(json.dumps(_default_token_payload(), indent=2))
        return
    try:
        payload = json.loads(TOKEN_PATH.read_text())
    except Exception:
        TOKEN_PATH.write_text(json.dumps(_default_token_payload(), indent=2))
        return
    changed = False
    for key, value in _default_token_payload().items():
        if key not in payload:
            payload[key] = value
            changed = True
    if changed:
        TOKEN_PATH.write_text(json.dumps(payload, indent=2))


def _update_token_totals(
    input_tokens: int | None,
    output_tokens: int | None,
    cached_input_tokens: int | None = None,
    cache_write_input_tokens: int | None = None,
) -> None:
    if (
        input_tokens is None
        and output_tokens is None
        and cached_input_tokens is None
        and cache_write_input_tokens is None
    ):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _TOKEN_LOCK:
        if TOKEN_PATH.exists():
            try:
                payload = json.loads(TOKEN_PATH.read_text())
            except Exception:
                payload = {}
        else:
            payload = {}
        payload.setdefault("total_input_tokens", 0)
        payload.setdefault("total_output_tokens", 0)
        payload.setdefault("total_cached_input_tokens", 0)
        payload.setdefault("total_cache_write_input_tokens", 0)
        payload.setdefault("created_at", _utc_now())
        payload["total_input_tokens"] += int(input_tokens or 0)
        payload["total_output_tokens"] += int(output_tokens or 0)
        payload["total_cached_input_tokens"] += int(cached_input_tokens or 0)
        payload["total_cache_write_input_tokens"] += int(cache_write_input_tokens or 0)
        payload["last_updated"] = _utc_now()
        tmp = TOKEN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(TOKEN_PATH)


def summarize_agent_metrics(metrics: Any) -> dict[str, Any]:
    """Extract a compact summary from Strands EventLoopMetrics.

    Returns the same `inputTokens`/`outputTokens`/`cacheReadInputTokens`/
    `cacheWriteInputTokens`/`totalTokens` keys the SSE emitter already
    expects, plus a `latency_ms` field. Unchanged from the old shim's
    contract — call sites in research.py / response.py keep working.
    """
    if not metrics:
        return {}
    summary: dict[str, Any] = {}
    accumulated = getattr(metrics, "accumulated_usage", None)
    if isinstance(accumulated, dict):
        for key in (
            "inputTokens",
            "outputTokens",
            "cacheReadInputTokens",
            "cacheWriteInputTokens",
            "totalTokens",
        ):
            value = accumulated.get(key)
            if value is not None:
                summary[key] = int(value)
    perf = getattr(metrics, "accumulated_metrics", None)
    if isinstance(perf, dict) and "latencyMs" in perf:
        summary["latency_ms"] = int(perf["latencyMs"])
    return summary


def print_agent_log(event: str, **fields: Any) -> None:
    """Write one structured JSON log line to stdout and bump the token totals."""
    line = {"event": event, "ts": round(time.time(), 3), **fields}
    _update_token_totals(
        fields.get("input_tokens"),
        fields.get("output_tokens"),
        fields.get("cached_input_tokens"),
        fields.get("cache_write_input_tokens"),
    )
    sys.stdout.write(json.dumps(line, default=str) + "\n")
    sys.stdout.flush()

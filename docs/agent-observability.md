# Agent Observability

**Status:** Reference · current as of 2026-05-25
**Scope:** How the Sage chat agent connects to our observability platform — what is traced, where it lands, and how to read it.
**Related:** [`chat-agent-system.md`](chat-agent-system.md), [`design-agentcore-strands-integration.md`](design-agentcore-strands-integration.md) §4.3, [`masterplan-production.md`](masterplan-production.md) §5F, [`daily-backend-health-review.md`](daily-backend-health-review.md)

---

## Summary

The Sage agent runs on **AWS Strands Agents + Bedrock**. Strands emits
**OpenTelemetry (OTel)** spans for every model invoke, event-loop cycle, tool
call, and agent run. We export those spans over **OTLP** to a self-hosted
**Langfuse** server, which is our observability platform of record for agent
behaviour (per-request traces, inputs/outputs, latency, token counts).

Two things to keep straight, because the naming collides:

- **The "agent core" that produces the telemetry is Strands**, the agent
  framework. Strands' `StrandsTelemetry` is the source of every span.
- **AgentCore Observability** (AWS Bedrock AgentCore's CloudWatch/X-Ray
  surface) is a *different* thing. We use AWS AgentCore for the Code
  Interpreter sandbox (chart phase), and those runs are visible in CloudWatch,
  but **our primary agent traces do not flow through AgentCore Observability** —
  they flow Strands → OTLP → Langfuse. See [§6](#6-what-we-do-not-use-yet).

Token/cost economics are a parallel, durable signal: they are persisted to the
`agent_usage` SQLite table rather than read back out of Langfuse, so we can bill
and audit without depending on the trace store.

```
                    ┌─────────────────────────────────────────┐
  POST chat ──────► │ orchestrator.py                          │
                    │   request_trace_context(request_id, …)   │
                    │   ├─ Phase 1 research.py  ─┐              │
                    │   ├─ Phase 2a response.py ─┤ Strands Agent│
                    │   └─ Phase 2b chart.py    ─┘ (Bedrock)    │
                    └───────────┬──────────────────┬───────────┘
                                │ OTel spans        │ EventLoopMetrics
                                ▼                   ▼
                  StrandsTelemetry OTLP    summarize_agent_metrics()
                  exporter                          │
                                │                    ▼
                                ▼            usage_recorder.record_usage()
                   Langfuse  (self-hosted)           │
                   /api/public/otel                  ▼
                   per-request traces,        agent_usage (SQLite)
                   I/O, latency, tokens       + logs/agent_tokens.json
                                                     │
                                                     ▼
                                              scripts/hf_metrics.py
```

---

## 1. The platform: Langfuse over OTLP

- **Server:** self-hosted Langfuse at `LANGFUSE_BASE_URL`
  (default in the masterplan: `http://151.245.184.3:9956`).
- **Transport:** OpenTelemetry OTLP HTTP. `observability.py` derives the OTLP
  endpoint from the Langfuse base URL: `{base}/api/public/otel`, with
  `Authorization: Basic base64(public_key:secret_key)` plus
  `x-langfuse-ingestion-version=4`.
- **Resource attributes:** `service.name` / `OTEL_SERVICE_NAME` =
  `LANGFUSE_SERVICE_NAME` (default `hf-workbench`),
  `deployment.environment` = `LANGFUSE_ENVIRONMENT`,
  `service.namespace=heurist`.
- **Semconv:** opts into `gen_ai_latest_experimental,gen_ai_tool_definitions`
  so GenAI spans carry model/tool metadata.

All of this is set with `os.environ.setdefault(...)`, so an operator can
override any OTLP env var directly and `observability.py` will not clobber it.

### Trace URL pattern

A trace id equals the orchestrator `request_id` (see [§4](#4-token--cost-economics)).
To jump from a log line to the trace:

```
{LANGFUSE_BASE_URL}/project/{project_id}/traces/{request_id}
```

(Masterplan item 5.20 — surfacing this URL in PR descriptions/log lines — is
still open.)

---

## 2. Configuration

Env vars, read in [`src/agent/config.py`](../src/agent/config.py) and consumed
by [`src/agent/observability.py`](../src/agent/observability.py):

| Env var | Default | Purpose |
|---|---|---|
| `LANGFUSE_BASE_URL` | _(unset)_ | Langfuse server. **Unset ⇒ all tracing is a no-op.** |
| `LANGFUSE_PUBLIC_KEY` | _(unset)_ | OTLP basic-auth user half. |
| `LANGFUSE_SECRET_KEY` | _(unset)_ | OTLP basic-auth secret half. |
| `LANGFUSE_ENVIRONMENT` | `development` | `deployment.environment` resource attr. |
| `LANGFUSE_SERVICE_NAME` | `hf-workbench` | `service.name` resource attr. |

Strands exporter toggles (independent of Langfuse; useful for local debugging):

| Env flag | Effect |
|---|---|
| `ENABLE_STRANDS_CONSOLE_TELEMETRY` | Print spans to the console exporter. |
| `ENABLE_STRANDS_OTLP_TELEMETRY` | Force the OTLP span exporter on. |
| `ENABLE_STRANDS_CONSOLE_METRICS` | Console meter exporter. |
| `ENABLE_STRANDS_OTLP_METRICS` | OTLP meter exporter. |

**Enablement rule:** setting `LANGFUSE_BASE_URL` implicitly turns on OTLP span
export (`enable_otlp = True`). With no Langfuse URL and none of the
`ENABLE_STRANDS_*` flags set, `setup_strands_telemetry()` returns early and the
process pays zero telemetry cost — this is the normal dev path.

> Note: `.env.example` currently ships `LANGFUSE_SERVICE_NAME=heurist-finance-backend`,
> a leftover from the port. The code default is `hf-workbench`; set it explicitly
> per environment.

Dependency: `langfuse>=3.0.0` and the Strands/OTel stack in `pyproject.toml`.

---

## 3. Wiring inside the agent

### 3.1 Process startup (`app.py`)

At import time `app.py` calls:

1. `initialize_runtime_observability()` — creates `logs/` and the running token
   accumulator `logs/agent_tokens.json`.
2. `setup_strands_telemetry()` — configures OTLP env, installs the Strands
   tracer monkeypatch, builds `StrandsTelemetry()` exporters, and (when keys are
   present) runs a Langfuse `auth_check()` whose result is logged as a JSON line
   under the `hf_workbench.agent` logger. Guarded by a module-level
   `_TELEMETRY_INITIALIZED` flag so it runs once.

### 3.2 Per-request trace context (`orchestrator.py`)

The orchestrator wraps the whole turn (all three phases) in
`request_trace_context(request_id, user_id, thesis_id, session_id)`. This uses
Langfuse `propagate_attributes(...)` to stamp every span opened inside the block
with a stable trace name (`hf-workbench:chat`), `session_id`, `user_id`, tags
(`hf-workbench`, `chat`, `user:{id}`), and metadata. When Langfuse is not
configured the context manager is a transparent pass-through.

### 3.3 Per-phase span attributes

Each Strands `Agent` is constructed with `trace_attributes` carrying
`hf.request.id`, `hf.thesis.id`, `hf.user.id`, and `hf.phase`
(`phase1` for research, etc.). These let you filter Langfuse to a single phase
or a single thesis.

### 3.4 Readable I/O on spans — the tracer monkeypatch

Strands' native GenAI/chat spans sometimes record their output as `null` in
Langfuse. `_patch_strands_tracer_output_preview()` monkeypatches the Strands
`Tracer` (model-invoke, event-loop-cycle, agent, and tool start/end spans) to
attach two families of attributes:

- `heurist.output.{text,length,truncated}` — a flattened, human-readable preview
  of the message/tool payload, visible in Langfuse's metadata panel.
- `langfuse.observation.input` / `langfuse.observation.output` and
  `langfuse.trace.input` / `langfuse.trace.output` — Langfuse's first-class
  I/O fields, via the `build_langfuse_*_io_attributes` / `attach_langfuse_*`
  helpers.

All payloads are stringified and **trimmed to 16 000 chars**
(`_TELEMETRY_PAYLOAD_CHAR_LIMIT`) with a `...[truncated N chars]` marker so a
huge tool result can't blow up a span. The patch is idempotent (guarded by
`Tracer._hf_output_preview_patch`) and every attribute write is wrapped in
`contextlib.suppress(Exception)` — instrumentation never breaks a chat turn.

`attach_span_payload(span, name=, value=)` is the general-purpose helper for
attaching an arbitrary named payload (sets `heurist.payload.{name}.*` and an
event) anywhere you hold a span.

### 3.5 Flush

`flush_telemetry()` calls `force_flush()` on the tracer provider when available
— call it at shutdown or after a batch job so buffered spans aren't lost.

---

## 4. Token & cost economics

Trace I/O lives in Langfuse, but **token counts and dollar cost are persisted
to SQLite** so billing/audit doesn't depend on the trace store.

- **Extraction:** `summarize_agent_metrics(EventLoopMetrics)` in
  `observability.py` pulls `inputTokens`, `outputTokens`,
  `cacheReadInputTokens`, `cacheWriteInputTokens`, `totalTokens`, and
  `latency_ms` from the Strands metrics object. Called in `research.py` and
  `response.py`.
- **Pricing:** [`src/agent/pricing.py`](../src/agent/pricing.py) maps Bedrock
  model ids → per-1M-token USD prices (substring match so version-suffix bumps
  don't silently route to $0). `compute_cost_usd(model_id, usage)` does the math.
- **Persistence:** [`src/agent/usage_recorder.py`](../src/agent/usage_recorder.py)
  `record_usage(...)` writes **one `agent_usage` row per phase**
  (`research` / `response` / `chart`) plus a denormalized `aggregate` row, joined
  by `request_id`. Each phase carries its own `model_id` (phases can run on
  different models), so mixed-model turns are priced correctly.
- **`agent_usage` schema** ([`db/schema.py`](../db/schema.py)): `request_id`
  (= Langfuse trace id), `user_id`, `session_id`, `endpoint`, `model_id`,
  `phase`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_write_tokens`, `cost_usd`, `latency_ms`, `status`
  (`ok`/`error`/`cancelled`), `created_at`.

### Running token totals

`print_agent_log(event, **fields)` writes a structured JSON line to stdout and
atomically bumps `logs/agent_tokens.json` (lifetime input/output/cache token
totals). This is a lightweight always-on counter independent of Langfuse.

### Reading it back

[`scripts/hf_metrics.py`](../scripts/hf_metrics.py) is the read-only CLI over
`agent_usage`:

```bash
uv run python scripts/hf_metrics.py today
uv run python scripts/hf_metrics.py user <user_id> [--days N]
uv run python scripts/hf_metrics.py model [--days N]
uv run python scripts/hf_metrics.py top-spenders [--days N]
uv run python scripts/hf_metrics.py endpoint <name> [--days N]
uv run python scripts/hf_metrics.py request <request_id>
uv run python scripts/hf_metrics.py charts [--days N]
```

Every subcommand supports `--json` for piping into ad-hoc analysis. The
[daily backend health review](daily-backend-health-review.md) uses this CLI for
the agent chat usage/cost section.

### Code Interpreter / chart run stats

The Phase 2b chart agent is our only AWS Bedrock AgentCore **Code Interpreter**
consumer. Each chart phase records one run to **three sinks** from a single funnel
in `chart.py` (`_record_ci_run`) — they are complementary, not interchangeable:

| Sink | What | Used for |
|---|---|---|
| **SQLite `code_interpreter_runs`** | one row/run: `outcome` (plot/skip/error/timeout/unknown), `failure_stage`, `skip_reason`, `execute_count`, `write_count`, `image_bytes`, `elapsed_ms`, `model_id` | SQL/CLI rollups (`hf_metrics.py charts`) |
| **Langfuse span** | a `code_interpreter.run` child span (nested under the request trace) with `heurist.ci.*` attributes + observation I/O | per-trace inspection |
| **OTel metrics** | `heurist.code_interpreter.runs` (counter, tagged by `outcome`), `.latency_ms`, `.sandbox_actions` histograms | aggregate dashboards; exported to the OTLP/console metrics backend, **not** Langfuse |

Token cost for the same run lives in `agent_usage` (phase=`chart`), joined by
`request_id`; the `charts` CLI surfaces both together. The `code_interpreter_runs`
table carries a `purpose` column (`chart` today) so a future research-phase
analysis sandbox can reuse it.

---

## 5. Operating it

**Enable in an environment:** set `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`, and a per-env `LANGFUSE_ENVIRONMENT` /
`LANGFUSE_SERVICE_NAME`, then restart `hf-workbench`.

**Confirm it connected:** on startup, look for a `langfuse.auth_check` JSON log
line under the `hf_workbench.agent` logger with `"ok": true` (or
`langfuse.otlp_configured` when keys are set in no-auth mode).

**Local debug without Langfuse:** set `ENABLE_STRANDS_CONSOLE_TELEMETRY=1` to
print spans to the console.

**Smoke the agent stream:** `uv run python scripts/smoke_ai_sdk_chat.py`
(protocol-only without Bedrock: start the server with
`HF_AGENT_PROTOCOL_SMOKE=1`). Behaviour-change validation goes through the
`hf-evals` harness (see root `CLAUDE.md`).

**Cost audit:** `scripts/hf_metrics.py`, above.

---

## 6. What we do *not* use (yet)

- **AgentCore Observability (CloudWatch / X-Ray)** — Bedrock AgentCore has its
  own observability surface. We use AgentCore for the Code Interpreter sandbox
  (chart phase) and now capture its run stats + token cost from the Strands/agent
  layer (see "Code Interpreter / chart run stats" above), but we do **not** pull
  AgentCore's native CloudWatch/X-Ray sandbox metrics. See
  [`design-agentcore-strands-integration.md`](design-agentcore-strands-integration.md)
  §1.4 and §4.3.
- **x402 / AgentCore payments traces** — proposed only; would link payment events
  to Langfuse spans. See the same design doc §3.
- **Claude-API (non-Bedrock) path** — if any phase moves off Bedrock to the
  Anthropic SDK, `record_usage()` must be called from there too (masterplan 5.19,
  forward note).

---

## 7. File map

| Concern | File |
|---|---|
| Telemetry setup, trace context, span helpers, tracer patch | [`src/agent/observability.py`](../src/agent/observability.py) |
| Env config | [`src/agent/config.py`](../src/agent/config.py), [`.env.example`](../.env.example) |
| Startup hooks | [`app.py`](../app.py) |
| Per-request trace wrap | [`src/agent/orchestrator.py`](../src/agent/orchestrator.py) |
| Per-phase metrics extraction | [`src/agent/research.py`](../src/agent/research.py), [`src/agent/response.py`](../src/agent/response.py) |
| Cost persistence | [`src/agent/usage_recorder.py`](../src/agent/usage_recorder.py), [`src/agent/pricing.py`](../src/agent/pricing.py) |
| Code Interpreter run stats (3-sink funnel) | [`src/agent/chart.py`](../src/agent/chart.py) (`_record_ci_run`), [`src/agent/ci_run_recorder.py`](../src/agent/ci_run_recorder.py), `record_code_interpreter_metrics` in [`observability.py`](../src/agent/observability.py) |
| `agent_usage` + `code_interpreter_runs` tables | [`db/schema.py`](../db/schema.py) |
| Metrics CLI (incl. `charts`) | [`scripts/hf_metrics.py`](../scripts/hf_metrics.py) |

---

## 8. References

- Internal: [`chat-agent-system.md`](chat-agent-system.md),
  [`design-agentcore-strands-integration.md`](design-agentcore-strands-integration.md),
  [`masterplan-production.md`](masterplan-production.md) §5F (build history,
  items 5.15–5.20).
- [Strands Agents telemetry](https://strandsagents.com/) (`StrandsTelemetry`,
  OTLP/console exporters).
- [Langfuse OpenTelemetry ingestion](https://langfuse.com/docs/opentelemetry/get-started)
  (`/api/public/otel`, basic-auth header).
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).

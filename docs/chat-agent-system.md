# Chat Agent System

The Sage chat pipeline is a three-phase Strands flow on AWS Bedrock. The
research phase runs first; the response and chart phases then run in parallel
and feed the same SSE queue.

```
Phase 1: research.py ──┬── tool_call_records ──┐
                       ▼                       ▼
             Phase 2a: response.py   Phase 2b: chart.py    (parallel)
             (synthesis + citations) (Code Interpreter,
                                      may skip)
```

`orchestrator.py` is the only entry point. It fans out via
`asyncio.gather(response_coro, chart_coro)` when `req.enable_charts` is true;
otherwise Phase 2b is never spawned.

## Entry point

`POST /api/v1/ai-sdk/chat/completions` (FastAPI) → `run_chat()` →
`orchestrator.run_pipeline()`. Per-turn request shape
([`chat_models.py`](../src/agent/chat_models.py)):

```jsonc
{
  "session_id": "finance:user_1:abc123",
  "messages": [...],
  "params": {
    "mode": "quick",          // quick | deep — affects prompt budgets
    "enable_charts": false,   // opt-in chart agent
    "theme": "dark"           // chart theme
  },
  "subject": {
    "thesis_ids": ["thesis_001"], // explicit refs from the composer chip area
    "references": [...],          // multi-kind explicit attachments
    "active_thesis_id": null,     // ambient — thesis open in a detail panel; FE drops it the moment an explicit thesis ref is attached
    "active_story_id": null       // ambient — news story open in NewsDialog; same precedence rule
  }
}
```

Two typed groups, flat at top level. `params` are conversation parameters
that change agent behaviour. `subject` is what the turn is about — both
explicit attachments and ambient detail-surface state. The old single
`metadata` bag mixed both concerns plus inert telemetry; the split makes
"where does this new field go" answer itself.

The user message text carries the actual task. Frontend chip presets (see
`heurist-finance-frontend/src/lib/composer/chip-presets.ts`) pin a
sentence pill above the input; on send, the sentence ships verbatim as
the user message text (optionally followed by free-form typed extras).
There is no hidden expansion — the persisted user turn is exactly what
the user saw in the composer. The backend's system prompt is universal
Sage; the task structure lives in the user message text.

Ambient subject IDs (`active_thesis_id` / `active_story_id`) are hydrated
by the chat route exactly like explicit refs would be: when the explicit
list is empty, the ambient thesis id becomes the single selected thesis;
the ambient story id becomes a `<active_story>` block prepended to the
context section of both phase prompts. Hydration is best-effort — a
stale id silently drops rather than 500ing the chat.

## Phases

| Phase | File | Responsibility | Output |
|---|---|---|---|
| 1 — research | [`research.py`](../src/agent/research.py) | Bedrock agent with the Heurist tool catalog ([`tools.py`](../src/agent/tools.py)). Gathers data. | `tool_call_records` + raw text |
| 2a — response | [`response.py`](../src/agent/response.py) | Synthesizes a confident, cited answer from research output. Streams text deltas. | `full_text`, parts |
| 2b — chart | [`chart.py`](../src/agent/chart.py) | Reads `tool_call_records`, decides PLOT or SKIP, renders one matplotlib chart in an AgentCore Code Interpreter sandbox. Never blocks 2a. | `chart_image` or `chart_skip` |

Phase 2b uses `chart_style.apply_style(theme)` ([`chart_style.py`](../src/agent/chart_style.py))
pushed into the sandbox via `writeFiles` so generated snippets share the same
visual rules (no vertical grid, horizontal grid only, no spines).

## Streaming contract

Phases push internal events onto an asyncio `Queue[bytes]`. The FastAPI
handler reads the queue and runs each chunk through
[`ai_sdk_stream.convert_legacy_sse_to_ui_stream`](../src/agent/ai_sdk_stream.py),
which translates to the Vercel AI SDK UI Message Stream protocol.

| Internal SSE | UI part | Payload |
|---|---|---|
| `text_delta` | `text-delta` | streaming text |
| `chart_image` | `data-chart` | `{url, caption}` |
| `chart_skip` | `data-chart-skip` | `{reason}` |
| `result` | `data-result` | totals (cost, duration, usage) |
| `error` | `error` | message |

`UIStreamCapture.parts` is persisted to `agent_messages.parts_json` so
transcripts replay verbatim.

## Artifact transport

Charts upload to R2 ([`r2_storage.py`](../src/agent/r2_storage.py)); the SSE
event carries only the public URL and caption. If R2 is unconfigured, the
chart phase emits `chart_skip` rather than blocking.

## AWS

| Setting | Value |
|---|---|
| Account | `441070252417` |
| Region | `us-west-2` |
| Profile | `payments-admin` |
| Bedrock model | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Code Interpreter session | `chart-{request_id}` (per-turn, chart phase only) |

Env keys live in [`config.py`](../src/agent/config.py): `AWS_REGION`,
`BEDROCK_PROFILE`, `BEDROCK_MODEL_ID`, `RESPONSE_BEDROCK_MODEL_ID`,
`AGENT_TIMEOUT_SECONDS`, `RESEARCH_MAX_TOKENS`, `RESPONSE_MAX_TOKENS`,
`CHART_AGENT_TIMEOUT_S`, `CHART_AGENT_MAX_TOKENS`, R2 keys, Langfuse keys.

## Failure isolation

The chart phase **never blocks the response stream**. Every failure path
(sandbox init error, agent timeout, missing `/tmp/chart.png`, indecisive
reply, upload error) converts to a `chart_skip` event.

## Tests + smokes

- `tests/test_chart_style.py` — matplotlib introspection across themes.
- `tests/test_chart_agent.py` — mocked sandbox + LLM; pins skip / plot / init-fail SSE shapes.
- `tests/test_chart_persistence.py` — pins `data-chart` part shape end-to-end.
- `tests/test_chat_api.py` — the FastAPI surface.
- `scripts/smoke_ai_sdk_chat.py` — full pipeline against real Bedrock.
- `scripts/smoke_chart_agent.py`, `smoke_chart_agent_nonprice.py` — Phase 2b only.

For protocol-only local testing without Bedrock:
`HF_AGENT_PROTOCOL_SMOKE=1 uv run uvicorn ...`.

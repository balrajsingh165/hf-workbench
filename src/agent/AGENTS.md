# src/agent/ — Sage chat flow (Strands)

Two-phase research + response pipeline for Sage chat turns. Driven by the
production AI SDK route in `src/interfaces/ai_sdk_compat/`; emits Vercel AI SDK
UI Message Stream chunks the frontend's `useChat` consumes directly.

## Routes

- `POST /api/v1/ai-sdk/chat/completions` — single chat turn, streams UI chunks.
- `/api/v1/chats/*` — session list, message history, share toggle, delete.
- `/api/v1/public/chats/*` — read-only share view + duplicate.

## Modules

| Module | Purpose |
|---|---|
| `config.py` | Bedrock + agent-knob env config |
| `models.py` | Internal Strands request/context models |
| `chat_models.py` | AI SDK chat request and metadata models |
| `prompt_manager.py` | Backend-owned chip prompts + Phase 1/2 prompt assembly |
| `json_block.py` | Shared trailing-JSON block detector for Phase 2 output |
| `ai_sdk_stream.py` | Internal Strands/SSE chunks → AI SDK UI Message Stream chunks |
| `sse_emitter.py` | Internal queue events between Strands phases and stream adapter |
| `tools.py` | Strands `@tool` functions calling app.py handlers directly |
| `research.py` | Phase 1: tool-driven evidence gathering |
| `response.py` | Phase 2: synthesis + citation block |
| `orchestrator.py` | Phase 1 → Phase 2 driver, queue-backed SSE producer |

## Coding rules

- Strands SDK only; `from strands import Agent, tool`.
- Tool handlers call FastAPI handlers as plain Python (no in-process HTTP hop).
- Fresh `Agent` per request — no shared sessions.
- No backwards-compat shims (project is pre-launch).

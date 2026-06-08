# Agent Tools Review — `src/agent/tools.py`

Scope: the four Strands `@tool` functions exposed to the Phase 1 research
agent ([src/agent/tools.py](../../src/agent/tools.py)) and how they relate
to the FastAPI handlers in [app.py](../../app.py) and the chip prompts in
[src/agent/prompt_manager.py](../../src/agent/prompt_manager.py).

Goal of this review: make the tool surface **clearly defined, narrow,
self-describing, and honest about failure modes** so the model picks the
right tool, supplies the right arguments, and degrades gracefully when a
data source is missing.

---

## Findings & Suggestions

### P0 — Correctness / contract bugs

#### P0-1. `search_macro` silently drops the `series_keys` arg
`_dispatch` calls `get_macro(date=date, series_keys=None)` with `series_keys`
hard-coded to `None`. The tool's JSON schema doesn't even expose
`series_keys`, but the underlying handler supports it and falls back to
`DEFAULT_MACRO_SERIES`. This is fine *only if* we never want the model to
narrow the macro slice — but the chip prompts reference macro sensitivity,
sector spillovers, and curve shape, which directly map to specific FRED
series.

**Fix:** add `series_keys: array<string>` to the schema OR document
explicitly that macro is fixed-set.

#### P0-3. Tool schemas lie by omission about defaults & ranges
The schemas declare `days_back: integer` and `top_k` (implicit) with no
`minimum`/`maximum`/`default`, yet `app.py` enforces `Query(..., ge=1, le=365)`
and `Query(8, ge=1, le=25)`. If the model passes `days_back=400` it gets a
422 from the handler, which `_dispatch` converts to `{"note": "workbench
422: ..."}` — wasting a tool call round-trip. Also `direction` enum is
duplicated in two places (schema in `tools.py`, `Literal` in `app.py`)
with no shared source of truth.

**Fix:** add `minimum`/`maximum`/`default` to JSON schemas matching the
FastAPI constraints; or generate the schema from the Pydantic models
already present in `app.py`.

#### P0-5. `kind` parameter on `search_market` overloads four unrelated APIs
`search_market(ticker, kind=price|options|insider|filings)` muxes Yahoo
price history, Yahoo options chain, SEC insider activity, and SEC filing
timeline behind one tool. The model has to know that:
- `days_back` is meaningful only for `price`
- `options`/`insider`/`filings` ignore `days_back`
- output `snapshot` shape changes per `kind`

This is a footgun for the planner. The schemas don't say which params
apply to which kind.

**Fix (preferred):** split into four tools: `get_price_history`,
`get_options_chain`, `get_insider_activity`, `get_filings_timeline`.
Each gets a tight schema and a tight description. Total tool count goes
from 4 → 7, still well under any practical cap.

**Fix (minimum):** document the per-`kind` argument applicability in the
description, and reject `days_back` for non-price kinds with a clear
note.

---

### P1 — Clarity / model usability

#### P1-2. Descriptions don't tell the model *when* to pick a tool
Current descriptions describe *what the tool returns*. The model needs
*when to use this vs that*. Compare:

> "Search news evidence for or against a thesis."

vs.

> "Use this when you need recent news headlines tagged supports/stresses
> for a thesis. Prefer this over `get_related_theses` when you want raw
> evidence; prefer `get_related_theses` when you want neighboring
> theses' conclusions."

**Fix:** rewrite each description as 1 line of *purpose* + 1 line of
*when to pick this over the alternatives*.

#### P1-3. `search_*` naming implies free-text query
`search_evidence`, `search_market`, `search_macro` all start with `search_`,
which strongly biases the model toward passing a query string. None of
them accept a query. `get_related_theses` is the only one named
correctly. Recommend `list_evidence`, `get_market`, `get_macro`,
`list_related_theses` (or split per P0-5).

#### P1-5. Output shapes aren't declared
Strands tools can declare an output JSON schema; we don't. Combined with
the `_normalise_tool_output` parsing dance in `ai_sdk_stream.py`, the
shape downstream code expects is implicit. Worse, `get_thesis_related`
returns `{"related": [...]}` while `get_market` returns `{"ticker":..., 
"snapshot": {...}, "asof": ...}` — different envelopes with no convention.

**Fix:** standardize on either `{data: T, meta: {asof, source, ...}}`
or `{T..., meta: {asof, source, ...}}` and apply uniformly. Declare
output schema per tool.

#### P1-6. Mesh-unavailable note is structurally indistinguishable from real data
`_safe_mesh_call` returns `{"note": "mesh unavailable: ..."}` on failure,
but a successful call can also return a payload that *contains* a `"note"`
key. Macro `regime_payload.get("regime") or ... or .get("note")` even
treats `"note"` as the regime label when nothing else is present. The
agent gets confusing input on partial outages.

**Fix:** wrap failures as `{"error": {"source": "yahoo", "message": "..."}}`,
and reserve `note` for narrative fields. Bonus: log the original `type(exc)`
in observability.

---

### P2 — Hygiene / future-proofing

#### P2-1. `ToolDef` exists but isn't typed for `output_schema`, `examples`, or `applicability`
The dataclass holds `agent_id`, `tool_name`, `description`, `parameters`.
If we want the prompt to render examples ("`search_evidence({thesis_id:
'th_xyz', days_back: 14})`"), we need fields for them.

**Fix:** add `output_schema: dict[str, Any] | None = None`,
`examples: tuple[dict[str, Any], ...] = ()`, `category: str = ""`.

#### P2-2. `_normalize_schema`, `_apply_pydantic_validation`, `_build_strands_tool` are scaffolding for a dynamic catalog that doesn't exist yet
The AGENTS.md notes the static 4-tool registry is intentional ("preserved
so we can swap in a dynamic catalog later, e.g., Mesh x402"). For today,
this is dead complexity. Either keep these helpers and write a unit test
that proves the dynamic catalog story works, or inline the four `@tool`
definitions with explicit signatures and delete the factory.

A typed-signature `@tool` is also vastly more readable for engineers:

```python
@tool(name="search_evidence", description="...")
def search_evidence(
    thesis_id: str,
    direction: Literal["supports", "stresses"] | None = None,
    days_back: Annotated[int, Field(ge=1, le=365)] | None = None,
) -> dict[str, Any]:
    ...
```

#### P2-3. `_dispatch` does an `import app` lazily on every call
Lazy import dodges a circular-import issue, but on every tool invocation
we re-resolve the four symbols. Module imports are cached, so the cost
is negligible, but the comment "Imported lazily so module import doesn't
pull FastAPI handler graph" hints that `app.py` is doing too much at
import time. Worth checking whether moving handlers into a thin
`src/handlers/*.py` would let `tools.py` import directly.

#### P2-4. `_http_to_note` swallows the underlying error type
Every `HTTPException` becomes `{"note": "workbench {status}: {detail}"}`.
The model can't tell a 404 (thesis not found) from a 422 (bad arg) from
a 500. Pass `{"error": {"status": status_code, "detail": detail}}` so the
agent can decide whether to retry with different args vs. abandon the
tool.

#### P2-5. No telemetry for tool-arg quality
We have nothing tracking how often tool calls fail, get truncated, or
return empty. Strands metrics give us cumulative tokens but not
per-tool-call success/empty rates. Add a small counter per tool with:
{`called`, `truncated`, `errored`, `empty_result`}. This will tell us
whether description rewrites actually move behavior.

#### P2-6. `get_macro` ignores `date` for the regime payload
`fred_tool("macro_regime_context", {})` is always "now" — but the tool
takes a `date_or_recent` argument that suggests historical regimes are
addressable. Either make the date apply to regime context too, or rename
the parameter to `as_of_calendar_date` and document that regime is
always live.

#### P2-7. `_MAX_TOOL_OUTPUT_CHARS` mirrors a constant in `research.py`
[`tools.py L103`](../../src/agent/tools.py#L103) and
[`research.py L50`](../../src/agent/research.py#L50) both define
`12_000`. Lift to `src/agent/config.py` (`tool_output_char_limit`).

---

## Recommended next moves (sequenced)

1. **Land P0-1** (`search_macro` `series_keys`) — small correctness fix.
2. **Split `search_market`** (P0-5) and rename per P1-3. This is the
   moment to redo descriptions (P1-2), output envelopes (P1-5), and
   error/note shapes (P1-6, P2-4) — all of which now flow through the
   single `HF_TOOLS` registry.
3. **Move to typed-signature `@tool`s** (P2-2) and decide explicitly:
   keep the `HF_TOOLS` dataclass + factory alive for the dynamic catalog
   story, or delete it.
4. **Add per-tool telemetry** (P2-5) before any further description
   tuning, so we can measure the change.

## Out of scope for this review

- Whether the chip → tool mapping in the prompts is the right one
  (separate prompt-engineering question).
- Whether we should move to Mesh x402 catalog discovery now vs. later.
- Whether `app.py` should be carved into per-domain handler modules
  (touches more than the agent layer).

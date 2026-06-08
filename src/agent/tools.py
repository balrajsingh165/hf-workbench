"""Strands @tool functions for the chip flow.

Routes the four tools through the existing app.py FastAPI handlers as plain
Python calls — no httpx, no localhost loop. The handlers already wrap mesh
errors with `_safe_mesh_call` returning `{note: "mesh unavailable: ..."}`,
so the agent always sees a usable shape.

The ToolDef shape is preserved so we can swap in a dynamic catalog later
(e.g., Mesh x402) without touching the agents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pydantic import ConfigDict, create_model
from strands import tool

from src.agent.tool_response import strip_tool_input_echo


@dataclass(frozen=True)
class ToolDef:
    agent_id: str
    tool_name: str
    description: str
    parameters: dict[str, Any]


HF_TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        agent_id="hf",
        tool_name="search_evidence",
        description=(
            "Returns the thesis's news evidence and authored metadata in one "
            "call: (a) recent news evidence rows — headline, story_id, "
            "created_at, relation (supports/stresses), confidence, one-line "
            "rationale; (b) total_links + returned counts + "
            "{supports,stresses,neutral} summary so you can tell at a glance "
            "how thin or dense the news flow is; (c) invalidation_watch_list "
            "— a {framing, conditions} object listing the thesis-author-"
            "defined kill-switch criteria. These are FORWARD-LOOKING "
            "conditions, NOT events that have occurred. Quoting a value from "
            "`conditions` as if it has happened requires a separate tool "
            "result this turn that contains the matching number/date — the "
            "conditions list itself is not evidence; "
            "(d) ticker_directions — per-ticker bullish/bearish stance as "
            "authored on the thesis (so you can tell which leg is the hedge "
            "vs. the main position); a ticker absent from this list either "
            "has no stance set or was tagged neutral; "
            "(e) a `note` string when the evidence list is empty, which "
            "leads with 'no <direction> evidence found' so you can recover "
            "instead of guessing. Call with direction='supports' or "
            "direction='stresses' to filter; omit direction for a mixed "
            "view. Citable by `story_id`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "thesis_id": {"type": "string"},
                "direction": {"type": "string", "enum": ["supports", "stresses"]},
                "days_back": {"type": "integer"},
            },
            "required": ["thesis_id"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="price_summary",
        description=(
            "Compact quote card for one ticker: last/prev close, 1d change %, "
            "session open/high/low/volume, 52-week high/low, market cap, "
            "EPS (TTM), P/E (TTM), forward P/E, plus window high/low/change "
            "over days_back. No OHLCV bars. Prefer this over price_history "
            "for price level, valuation multiples, or 'is it up or down'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days_back": {"type": "integer"},
            },
            "required": ["ticker"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="market_overview",
        description=(
            "One-call cross-asset snapshot: last price + 1d change % for a "
            "basket of mainstream benchmarks (default: SPY, QQQ, ^TNX 10Y "
            "yield, CL=F oil, GC=F gold, ^VIX, DX-Y.NYB dollar). Pass "
            "`symbols` to override the default basket (e.g. add sector ETFs "
            "like XLE/XLK/XLF, or international like EFA/EEM). Returns no "
            "OHLCV, no window stats — just current quote + 1d move."
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="price_history",
        description=(
            "Full daily OHLCV bars for a ticker over the requested window. "
            "Use only when you need to reason about trajectory or hand the "
            "data to the chart agent. For 'where is X trading' use "
            "price_summary instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "days_back": {"type": "integer"},
            },
            "required": ["ticker"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="recent_filings",
        description=(
            "Flat list of a company's recent SEC filings (one row per "
            "filing): form, filing_date, report_date, 8-K item codes, "
            "primary doc. Plus counts_by_form for quick triage. Metadata "
            "only — for income statement / balance sheet line items use "
            "xbrl_fact; for a fundamentals snapshot use fundamentals_snapshot. "
            "Best for 'what just happened / any material 8-Ks' questions. "
            "Follow-up chain: when an 8-K's `items` array contains 2.02 "
            "(results of operations / earnings) or 5.02 (officer/director "
            "departures, appointments), the body of the filing usually "
            "carries the actionable signal — `web_fetch` the row's "
            "`primary_document_url` when present (8-K / 6-K / S-1 family). "
            "10-K and 10-Q rows omit `primary_document_url` (inline iXBRL "
            "does not scrape to readable prose); use `xbrl_fact` for metrics. "
            "Never guess EDGAR URLs."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="xbrl_fact",
        description=(
            "Quarterly or annual SEC XBRL line item for ONE ticker. Returns "
            "deduped period observations (default ~8 periods, controllable "
            "via `limit`) and a summary with QoQ and YoY growth. One metric "
            "per call — fan out for cross-metric or "
            "cross-ticker comparisons. Supported metrics (alias → us-gaap "
            "concept): revenue, net_income, eps_diluted, eps_basic, "
            "gross_profit, operating_income, cash, total_assets, "
            "total_debt, operating_cash_flow, shares_diluted. Equity-only "
            "— non-US filers (e.g. FANUY) and ETFs/funds (SMH) return a "
            "note pointing to fundamentals_snapshot or price_summary. "
            "Always sanity-check `company` against the requested ticker; "
            "fuzzy SEC issuer matches can resolve to wrong companies."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "metric": {
                    "type": "string",
                    "enum": [
                        "revenue",
                        "net_income",
                        "eps_diluted",
                        "eps_basic",
                        "gross_profit",
                        "operating_income",
                        "cash",
                        "total_assets",
                        "total_debt",
                        "operating_cash_flow",
                        "shares_diluted",
                    ],
                },
                "frequency": {
                    "type": "string",
                    "enum": ["quarterly", "annual"],
                },
                "limit": {"type": "integer"},
            },
            "required": ["ticker", "metric"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="fundamentals_snapshot",
        description=(
            "One-shot fundamentals + forward calendar for a ticker: latest "
            "annual and latest quarterly income statement, balance sheet, "
            "cash flow; market cap and sector; next earnings date; "
            "consensus revenue/EPS estimates; dividend dates. Use for "
            "'how are X's margins / what does the Street expect / when "
            "does Y report' questions. For multi-period growth trends use "
            "xbrl_fact instead. Equity-only — ETFs and funds return a "
            "note pointing to price_summary. ADRs / non-US filers may "
            "return sparse data with a coverage note."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="recent_insider",
        description=(
            "Flat list of insider transactions for a ticker (one row per "
            "transaction, not per Form 4). Includes reporting person, "
            "role, code, shares, price, and post-trade holding."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
            },
            "required": ["ticker"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="search_macro",
        description=(
            "Chartable FRED macro series history. Use for Fed, inflation, "
            "labor, Treasury yield, and yield-curve questions. Required: "
            "`series`, an explicit list of series specs (max 12 per call — "
            "split into multiple calls only if you genuinely need more). "
            "Each spec has `series_key` and optional `view`. Good series "
            "keys: core_cpi, headline_cpi, headline_pce, fed_funds, ust_10y, "
            "curve_10y_minus_2y, unemployment_rate. Good views: level, yoy, "
            "mom_annualized, change. For CPI/PCE inflation, prefer yoy or "
            "mom_annualized over level. For Treasury yields and Fed funds, "
            "use level or change. This tool does not fetch regime summaries "
            "or release calendars; it returns temporal observations suitable "
            "for evidence and charts. Never call with an empty object."
        ),
        parameters={
            "type": "object",
            "properties": {
                "series": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "series_key": {"type": "string"},
                            "view": {
                                "type": "string",
                                "enum": ["level", "yoy", "mom_annualized", "change"],
                            },
                        },
                        "required": ["series_key"],
                    },
                },
                "limit": {"type": "integer"},
            },
            "required": ["series"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="search_stories",
        description=(
            "Ad-hoc semantic search over the curated story index. Returns "
            "story_id, headline, created_at, a snippet, a similarity score, "
            "and a `url` (the centroid article's publisher URL). Citable by "
            "`story_id`. To read a story's full body — overview bullets, "
            "quotes, all member URLs — call `fetch_story(story_id)`. The "
            "`story_id` itself is an internal slug, NOT a URL; never "
            "construct a URL from a story_id. Optional `days_back` restricts "
            "to recent stories; `top_k` defaults to 8."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "days_back": {"type": "integer"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="fetch_story",
        description=(
            "Read the full markdown body of one curated story by `story_id` "
            "(e.g. `story_312`). Returns overview bullets, key quotes, and a "
            "Sources list with publisher URLs. Use this — NOT `web_fetch` — "
            "when you have a story_id from `search_stories` / "
            "`search_evidence` and want more than the search snippet. Also "
            "returns the centroid article's `url` for an optional follow-up "
            "`web_fetch` of the primary source."
        ),
        parameters={
            "type": "object",
            "properties": {
                "story_id": {"type": "string"},
            },
            "required": ["story_id"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="web_search",
        description=(
            "Open-web search (Firecrawl/Exa). Returns url, title, short "
            "snippet, and published_date per hit. Citable by URL with a "
            "verbatim snippet from the `snippet` field. `days_back` post-"
            "filters by published_date when present."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer"},
                "days_back": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="web_fetch",
        description=(
            "Scrape one URL's main content (up to ~4000 chars). Valid URL "
            "sources are the closed set below — anything else is a guess and "
            "will 404 or hallucinate:\n"
            "  - `web_search` result `url` (verbatim);\n"
            "  - `recent_filings.primary_document_url` when present (8-K / 6-K / "
            "S-1 — not 10-K/10-Q);\n"
            "  - `search_stories.url` or `fetch_story.url` (centroid "
            "article's publisher URL).\n"
            "Do NOT construct URLs. In particular, `story_id` slugs "
            "(e.g. `story_312`) are internal identifiers, NOT URLs — never "
            "build `https://<publisher>/story_<n>` or similar. To read a "
            "story's content, call `fetch_story(story_id)` instead of "
            "`web_fetch`. Returns the page's title, body text, and "
            "published_date. Citable by URL with a verbatim snippet from "
            "the returned `text`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
    ),
    ToolDef(
        agent_id="hf",
        tool_name="get_related_theses",
        description=(
            "Top similar theses (user-owned) by embedding cosine similarity. "
            "Each item carries similarity, current score, state, and a "
            "`relation` field that says whether the match traced to this "
            "thesis's statement or one of its invalidation conditions. This "
            "is a retrieval hint, not a semantic agreement/disagreement label."
        ),
        parameters={
            "type": "object",
            "properties": {
                "thesis_id": {"type": "string"},
            },
            "required": ["thesis_id"],
        },
    ),
)


# Quick mode keeps the historical 12k per-tool cap. Deep mode lifts it so
# `search_stories` / `search_evidence` deliver more headlines per call and the
# Phase 2 writer can pick the most decision-relevant ones instead of working
# from a silently trimmed slice.
_TOOL_OUTPUT_CAP_BY_MODE: dict[str, int] = {
    "quick": 12_000,
    "deep": 24_000,
}
_DEFAULT_TOOL_OUTPUT_CAP = _TOOL_OUTPUT_CAP_BY_MODE["quick"]


def _cap(payload: Any, *, limit: int = _DEFAULT_TOOL_OUTPUT_CAP) -> Any:
    """Cap a payload's JSON length so a single tool call can't blow the
    Phase 2 context.

    Strategy: if `payload` is a dict whose largest top-level value is a list,
    drop trailing items from that list until the JSON fits. This preserves
    the response schema (and its citation-ready fields) instead of
    collapsing to a flat preview string.

    Falls back to a string preview only when no list field is trimmable.
    """
    text = json.dumps(payload, default=str)
    original_chars = len(text)
    if original_chars <= limit:
        return payload

    if isinstance(payload, dict):
        list_fields = [(k, v) for k, v in payload.items() if isinstance(v, list) and v]
        if list_fields:
            target_field, target_list = max(
                list_fields,
                key=lambda kv: len(json.dumps(kv[1], default=str)),
            )
            trimmed = list(target_list)
            while trimmed:
                trimmed.pop()
                trial = {
                    **payload,
                    target_field: trimmed,
                    "_truncated_count": {
                        target_field: len(target_list) - len(trimmed)
                    },
                }
                if len(json.dumps(trial, default=str)) <= limit:
                    return trial

    return {
        "_truncated": True,
        "original_chars": original_chars,
        "kept_chars": limit,
        "preview": text[:limit],
        "hint": (
            "Output exceeded the per-tool cap. Narrow arguments "
            "(e.g. smaller days_back, specific direction/kind) and retry."
        ),
    }


def _tool_output_cap_for_mode(mode: str | None) -> int:
    return _TOOL_OUTPUT_CAP_BY_MODE.get(str(mode or "quick"), _DEFAULT_TOOL_OUTPUT_CAP)


def tool_names() -> tuple[str, ...]:
    """Public registry accessor — keeps prompts/citation enums in sync."""
    return tuple(td.tool_name for td in HF_TOOLS)


def tool_signature_lines() -> list[str]:
    """Render `name(arg1, arg2?)` lines for prompt assembly.

    `?` marks optional params (anything not in `required`). Used by the
    prompt manager so the "Available tools" block in the system prompt
    is generated from `HF_TOOLS` instead of hand-maintained.
    """
    lines: list[str] = []
    for td in HF_TOOLS:
        props = (td.parameters.get("properties") or {}).keys()
        required = set(td.parameters.get("required") or [])
        args = ", ".join(p if p in required else f"{p}?" for p in props)
        lines.append(f"- {td.tool_name}({args})")
    return lines


def tool_descriptions_block() -> str:
    """Multi-line block of `name(args) — description` for prompts."""
    out: list[str] = []
    for td, sig in zip(HF_TOOLS, tool_signature_lines(), strict=True):
        out.append(f"{sig}\n  {td.description}")
    return "\n".join(out)


def tool_name_enum_string() -> str:
    """Pipe-joined tool names for use as a JSON-block enum hint."""
    return "|".join(td.tool_name for td in HF_TOOLS)


def _http_to_note(exc: HTTPException) -> dict[str, Any]:
    return {"note": f"workbench {exc.status_code}: {exc.detail}"}


def _dispatch(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    user_id: str,
    mode: str | None = None,
) -> Any:
    # Imported lazily so module import doesn't pull FastAPI handler graph.
    from app import (  # type: ignore[import-not-found]
        get_filings,
        get_fundamentals,
        get_insider,
        get_macro,
        get_market_overview,
        get_price_history,
        get_price_summary,
        get_research_stories,
        get_story_detail,
        get_thesis_evidence,
        get_thesis_related,
        get_web_fetch,
        get_web_search,
        get_xbrl_fact,
    )

    cap_limit = _tool_output_cap_for_mode(mode)

    def cap(payload: Any) -> Any:
        capped = _cap(payload, limit=cap_limit)
        if isinstance(capped, dict) and "_truncated" not in capped:
            return strip_tool_input_echo(tool_name, capped)
        return capped

    try:
        if tool_name == "search_evidence":
            result = get_thesis_evidence(
                thesis_id=arguments["thesis_id"],
                direction=arguments.get("direction"),
                days_back=arguments.get("days_back"),
            )
            return cap(result.model_dump(mode="json"))
        if tool_name == "price_summary":
            result = get_price_summary(
                ticker=arguments["ticker"],
                days_back=arguments.get("days_back"),
            )
            return cap(result.model_dump(mode="json"))
        if tool_name == "market_overview":
            symbols = arguments.get("symbols")
            if symbols is not None and not isinstance(symbols, list):
                raise HTTPException(
                    status_code=400,
                    detail="market_overview symbols must be a list of strings",
                )
            result = get_market_overview(symbols=symbols)
            return cap(result.model_dump(mode="json"))
        if tool_name == "price_history":
            result = get_price_history(
                ticker=arguments["ticker"],
                days_back=arguments.get("days_back"),
            )
            return cap(result.model_dump(mode="json"))
        if tool_name == "recent_filings":
            result = get_filings(ticker=arguments["ticker"])
            return cap(result.model_dump(mode="json"))
        if tool_name == "xbrl_fact":
            result = get_xbrl_fact(
                ticker=arguments["ticker"],
                metric=arguments["metric"],
                frequency=arguments.get("frequency") or "quarterly",
                limit=arguments.get("limit"),
            )
            return cap(result.model_dump(mode="json"))
        if tool_name == "fundamentals_snapshot":
            result = get_fundamentals(ticker=arguments["ticker"])
            return cap(result.model_dump(mode="json"))
        if tool_name == "recent_insider":
            result = get_insider(ticker=arguments["ticker"])
            return cap(result.model_dump(mode="json"))
        if tool_name == "search_macro":
            specs = arguments.get("series")
            if not isinstance(specs, list) or not specs:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "search_macro requires non-empty series specs, e.g. "
                        "{\"series\":[{\"series_key\":\"core_cpi\",\"view\":\"yoy\"}]}"
                    ),
                )
            series_keys: list[str] = []
            views: list[str | None] = []
            for spec in specs:
                if not isinstance(spec, dict) or not spec.get("series_key"):
                    raise HTTPException(
                        status_code=400,
                        detail="each search_macro series spec requires series_key",
                    )
                series_keys.append(str(spec["series_key"]))
                # None = let the backend pick the default view for this series.
                view = spec.get("view")
                views.append(str(view) if view else None)
            try:
                limit = int(arguments.get("limit") or 24)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="search_macro limit must be an integer from 5 to 60",
                ) from exc
            result = get_macro(
                series_keys=series_keys,
                views=views if any(views) else None,
                limit=limit,
            )
            return cap(result.model_dump(mode="json"))
        if tool_name == "search_stories":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise HTTPException(
                    status_code=400, detail="search_stories requires a non-empty query"
                )
            result = get_research_stories(
                query=query,
                days_back=arguments.get("days_back"),
                top_k=int(arguments.get("top_k") or 8),
            )
            return cap(result.model_dump(mode="json"))
        if tool_name == "fetch_story":
            story_id = arguments.get("story_id")
            if not isinstance(story_id, str) or not story_id.strip():
                raise HTTPException(
                    status_code=400, detail="fetch_story requires a story_id"
                )
            result = get_story_detail(story_id=story_id.strip())
            return cap(result.model_dump(mode="json"))
        if tool_name == "web_search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                raise HTTPException(
                    status_code=400, detail="web_search requires a non-empty query"
                )
            result = get_web_search(
                query=query,
                num_results=int(arguments.get("num_results") or 8),
                days_back=arguments.get("days_back"),
            )
            return cap(result.model_dump(mode="json"))
        if tool_name == "web_fetch":
            url = arguments.get("url")
            if not isinstance(url, str) or not url.strip():
                raise HTTPException(
                    status_code=400, detail="web_fetch requires a url"
                )
            result = get_web_fetch(url=url)
            return cap(result.model_dump(mode="json"))
        if tool_name == "get_related_theses":
            result = get_thesis_related(
                thesis_id=arguments["thesis_id"],
                top_k=8,
                user_id=user_id,
            )
            return cap(result.model_dump(mode="json"))
    except HTTPException as exc:
        return _http_to_note(exc)
    return {"error": f"unknown tool {tool_name}"}


def _normalize_schema(parameters: dict[str, Any] | None) -> dict[str, Any]:
    if not parameters or type(parameters) is not dict:
        return {"type": "object", "properties": {}, "required": []}
    schema = dict(parameters)
    if schema.get("type") != "object":
        schema["type"] = "object"
    schema.setdefault("properties", {})
    schema.setdefault("required", [])
    return schema


def _apply_schema_runtime_validation(tool_obj: Any, tool_def: ToolDef) -> Any:
    """Rebuild Strands' per-tool Pydantic input model from our JSON schema.

    Without this, Strands' default `**kwargs` introspection wraps the tool's
    args under a single literal field named `kwargs`, and Bedrock's tool-use
    payloads (which send the args as a flat dict) fail validation. Same
    pattern as awsstrat/heurist_finance_agent/agent.py
    `_apply_schema_runtime_validation`.
    """
    schema = _normalize_schema(tool_def.parameters)
    properties = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    fields = {
        name: (Any, ... if name in required else properties.get(name, {}).get("default", None))
        for name in properties
    }
    model_name = f"{tool_def.agent_id}_{tool_def.tool_name}_Input"
    tool_obj._metadata.input_model = create_model(
        model_name,
        __config__=ConfigDict(extra="allow"),
        **fields,
    )
    return tool_obj


def _build_strands_tool(tool_def: ToolDef, *, user_id: str, mode: str | None) -> Any:
    @tool(
        name=tool_def.tool_name,
        description=tool_def.description.strip() or tool_def.tool_name,
        inputSchema={"json": _normalize_schema(tool_def.parameters)},
    )
    def generated_tool(**kwargs: Any) -> Any:
        return _dispatch(tool_def.tool_name, kwargs, user_id=user_id, mode=mode)

    return _apply_schema_runtime_validation(generated_tool, tool_def)


def build_strands_tools(*, user_id: str, mode: str | None = None) -> list[Any]:
    """Build per-request Strands tools bound to the active user and mode.

    `user_id` is closed over so user-scoped handlers (e.g.
    `get_thesis_related`'s score/status overlay) reflect the caller's
    real positions instead of a hard-coded default.

    `mode` controls the per-tool output cap — deep mode gets a larger cap so
    list-shaped tools (`search_stories`, `search_evidence`) deliver more rows
    before silent truncation.
    """
    return [_build_strands_tool(td, user_id=user_id, mode=mode) for td in HF_TOOLS]

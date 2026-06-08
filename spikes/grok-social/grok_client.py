"""Thin client for the xAI Grok Responses API with the built-in X Search tool.

Spike-quality: one file, httpx only, no SDK. The point is to prove out whether
Grok's server-side `x_search` tool is a good way to source social discussion
for theses, not to be production infra.

Key facts (from docs.x.ai, captured 2026-06-03):
- Endpoint: POST https://api.x.ai/v1/responses  (the new Responses/Agent Tools
  API; the legacy `search_parameters` Live Search API returns 410 since
  2026-01-12).
- The `x_search` tool runs server-side: Grok issues the searches, reads posts,
  and returns synthesized prose with citations back to the source posts. We do
  NOT get a raw post list unless we ask Grok to format one; citations carry the
  post URLs.
- Tool params (set on the tool object): allowed_x_handles / excluded_x_handles
  (<=20, mutually exclusive), from_date / to_date (ISO8601 YYYY-MM-DD),
  enable_image_understanding, enable_video_understanding.

Auth: XAI_API_KEY in the environment or ~/.env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.x.ai/v1/responses"

# Model ids verified against docs.x.ai (2026-06-03). All grok-4.20 / grok-4.3
# variants are billed $1.25/M input, $2.50/M output, 1M-token context.
#   grok-4.20-0309-reasoning      -> extended thinking, agentic tool calling,
#                                    lowest hallucination rate. Default here.
#   grok-4.20-0309-non-reasoning  -> same model, no thinking budget (cheaper
#                                    latency, weaker on multi-step search).
#   grok-4.3                      -> current flagship, also fine for X search.
DEFAULT_MODEL = "grok-4.20-0309-reasoning"


def _load_api_key() -> str:
    key = os.getenv("XAI_API_KEY")
    if not key:
        # Check ~/.env (workbench convention) then the repo root .env.
        candidates = [Path.home() / ".env", Path(__file__).resolve().parents[2] / ".env"]
        for env_file in candidates:
            if not env_file.exists():
                continue
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("XAI_API_KEY=") and not line.startswith("#"):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
            if key:
                break
    if not key:
        raise RuntimeError(
            "XAI_API_KEY not set. Add it to ~/.env or the repo .env, or export it."
        )
    return key


@dataclass
class XSearchOptions:
    """Configuration for the server-side x_search tool."""

    from_date: str | None = None  # "YYYY-MM-DD"
    to_date: str | None = None
    allowed_x_handles: list[str] = field(default_factory=list)
    excluded_x_handles: list[str] = field(default_factory=list)
    enable_image_understanding: bool = False
    enable_video_understanding: bool = False

    def as_tool(self) -> dict[str, Any]:
        tool: dict[str, Any] = {"type": "x_search"}
        if self.from_date:
            tool["from_date"] = self.from_date
        if self.to_date:
            tool["to_date"] = self.to_date
        if self.allowed_x_handles and self.excluded_x_handles:
            raise ValueError(
                "allowed_x_handles and excluded_x_handles are mutually exclusive"
            )
        if self.allowed_x_handles:
            tool["allowed_x_handles"] = self.allowed_x_handles[:20]
        if self.excluded_x_handles:
            tool["excluded_x_handles"] = self.excluded_x_handles[:20]
        if self.enable_image_understanding:
            tool["enable_image_understanding"] = True
        if self.enable_video_understanding:
            tool["enable_video_understanding"] = True
        return tool


@dataclass
class GrokResult:
    text: str  # synthesized answer
    citations: list[str]  # source post / page URLs
    usage: dict[str, Any]
    raw: dict[str, Any]


def _extract(data: dict[str, Any]) -> tuple[str, list[str]]:
    """Pull synthesized text + citation URLs from a Responses API payload.

    The Responses API returns `output`: a list of items. We want the final
    `message` item's `output_text` content and any `url_citation` annotations.
    Parsed defensively because the shape drifts between releases.
    """
    texts: list[str] = []
    citations: list[str] = []

    # Convenience field some responses include.
    if isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])

    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("output_text", "text") and part.get("text"):
                if part["text"] not in texts:
                    texts.append(part["text"])
            for ann in part.get("annotations", []) or []:
                url = ann.get("url") or (ann.get("url_citation") or {}).get("url")
                if url and url not in citations:
                    citations.append(url)

    return "\n\n".join(texts).strip(), citations


def search_x(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    options: XSearchOptions | None = None,
    timeout: float = 180.0,
) -> GrokResult:
    """Ask Grok a question and let it search X server-side to answer it."""
    options = options or XSearchOptions()
    payload = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "tools": [options.as_tool()],
    }
    headers = {
        "Authorization": f"Bearer {_load_api_key()}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(API_URL, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    text, citations = _extract(data)
    return GrokResult(
        text=text,
        citations=citations,
        usage=data.get("usage", {}) or {},
        raw=data,
    )

"""Thin client for xAI Grok Responses API with server-side X Search."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.config import XAI_API_KEY

API_URL = "https://api.x.ai/v1/responses"
DEFAULT_MODEL = "grok-4.20-0309-reasoning"


@dataclass(slots=True)
class XSearchOptions:
    """Configuration for the server-side x_search tool."""

    from_date: str | None = None
    to_date: str | None = None
    allowed_x_handles: list[str] = field(default_factory=list)
    excluded_x_handles: list[str] = field(default_factory=list)
    enable_image_understanding: bool = False
    enable_video_understanding: bool = False

    def as_tool(self) -> dict[str, Any]:
        if self.allowed_x_handles and self.excluded_x_handles:
            raise ValueError(
                "allowed_x_handles and excluded_x_handles are mutually exclusive"
            )
        tool: dict[str, Any] = {"type": "x_search"}
        if self.from_date:
            tool["from_date"] = self.from_date
        if self.to_date:
            tool["to_date"] = self.to_date
        if self.allowed_x_handles:
            tool["allowed_x_handles"] = self.allowed_x_handles[:20]
        if self.excluded_x_handles:
            tool["excluded_x_handles"] = self.excluded_x_handles[:20]
        if self.enable_image_understanding:
            tool["enable_image_understanding"] = True
        if self.enable_video_understanding:
            tool["enable_video_understanding"] = True
        return tool


@dataclass(slots=True)
class GrokResult:
    text: str
    citations: list[str]
    usage: dict[str, Any]
    raw: dict[str, Any]
    model_id: str
    latency_seconds: float


def _extract(data: dict[str, Any]) -> tuple[str, list[str]]:
    """Pull synthesized text and citation URLs from a Responses payload."""
    texts: list[str] = []
    citations: list[str] = []

    if isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])

    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"} and part.get("text"):
                text = str(part["text"])
                if text not in texts:
                    texts.append(text)
            for ann in part.get("annotations", []) or []:
                if not isinstance(ann, dict):
                    continue
                url = ann.get("url") or (ann.get("url_citation") or {}).get("url")
                if isinstance(url, str) and url and url not in citations:
                    citations.append(url)

    return "\n\n".join(texts).strip(), citations


def search_x(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    options: XSearchOptions | None = None,
    timeout: float = 180.0,
) -> GrokResult:
    """Ask Grok to answer with server-side X Search citations."""
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY not set")
    started = time.perf_counter()
    options = options or XSearchOptions()
    payload = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "tools": [options.as_tool()],
    }
    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
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
        model_id=model,
        latency_seconds=round(time.perf_counter() - started, 3),
    )


__all__ = [
    "DEFAULT_MODEL",
    "GrokResult",
    "XSearchOptions",
    "search_x",
]

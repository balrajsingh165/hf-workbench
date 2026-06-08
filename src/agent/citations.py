"""Citation ref resolution for Phase 2 trailing JSON blocks.

The model emits a minimal ordered list of identifiers (``story_id`` or URL).
This module hydrates display fields from captured tool outputs and rejects refs
that do not appear in any citable tool row this turn.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol
from urllib.parse import urlparse

_STORY_ID_RE = re.compile(r"^story_[A-Za-z0-9_]+$")
_CARD_SNIPPET_MAX = 220
# Prefer webp for size, jpeg as universal <img> fallback (matches home feed).
_THUMB_MIME_ORDER = ("image/webp", "image/jpeg", "image/avif")


class _CaptureLike(Protocol):
    parts: list[dict[str, Any]]


def _iter_tool_outputs(
    capture: _CaptureLike,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for part in capture.parts:
        ptype = str(part.get("type", ""))
        if not ptype.startswith("tool-"):
            continue
        if part.get("state") != "output-available":
            continue
        raw = part.get("output")
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(raw, dict):
            continue
        tool_input = part.get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_name = ptype.removeprefix("tool-")
        out.append((tool_name, raw, tool_input))
    return out


def _trim_card_snippet(text: str, *, max_len: int = _CARD_SNIPPET_MAX) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _hostname(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.") or url
    except Exception:
        return url


def _overview_blurb_from_json(raw: str | None) -> str:
    try:
        overview = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(overview, list):
        return ""
    return _trim_card_snippet(
        " ".join(
            str(item.get("text") or "").strip()
            for item in overview[:3]
            if isinstance(item, dict)
        )
    )


def _small_thumbnail_url_from_images_json(raw: str | None) -> str | None:
    """First `small` variant URL from `images_json`, for source-card thumbs."""
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    for image in payload:
        if not isinstance(image, dict):
            continue
        variants = [
            v
            for v in (image.get("variants") or [])
            if isinstance(v, dict)
            and v.get("size") == "small"
            and isinstance(v.get("url"), str)
            and v["url"].strip()
            and isinstance(v.get("mime"), str)
        ]
        if not variants:
            continue
        by_mime = {v["mime"]: v["url"].strip() for v in variants}
        for mime in _THUMB_MIME_ORDER:
            if mime in by_mime:
                return by_mime[mime]
        return variants[0]["url"].strip()
    return None


def _load_story_db_fields(story_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One query for overview + thumbnail fields across cited stories."""
    unique = list(dict.fromkeys(story_ids))
    if not unique:
        return {}
    try:
        from api import db
    except ImportError:
        return {}
    placeholders = ",".join("?" * len(unique))
    sql = (
        "SELECT id, overview_json, images_json FROM story "
        f"WHERE id IN ({placeholders})"
    )
    try:
        with db() as conn:
            rows = conn.execute(sql, unique).fetchall()
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        story_id = str(row["id"])
        out[story_id] = {
            "overview_blurb": _overview_blurb_from_json(row["overview_json"]),
            "thumbnail": _small_thumbnail_url_from_images_json(row["images_json"]),
        }
    return out


def _story_row(
    *,
    story_id: str,
    headline: str,
    tool: str,
    snippet: str = "",
    db_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db_fields = db_fields or {}
    blurb = _trim_card_snippet(snippet) or str(db_fields.get("overview_blurb") or "")
    if blurb.strip().lower() == headline.strip().lower():
        blurb = ""
    row: dict[str, Any] = {
        "kind": "story",
        "title": headline,
        "source": "Story",
        "url": f"/feed/{story_id}",
        "tool": tool,
    }
    if blurb:
        row["snippet"] = blurb
    thumb = db_fields.get("thumbnail")
    if isinstance(thumb, str) and thumb.strip():
        row["image"] = thumb.strip()
    return row


def _web_row(
    *,
    url: str,
    title: str,
    snippet: str,
    tool: str,
    site_name: str | None = None,
) -> dict[str, Any]:
    host = _hostname(url)
    clean_title = title.strip() or host
    clean_snippet = _trim_card_snippet(snippet)
    if clean_snippet.lower() == clean_title.lower():
        clean_snippet = ""
    row: dict[str, Any] = {
        "kind": "web",
        "title": clean_title,
        "source": (site_name or host).strip(),
        "siteName": site_name,
        "hostname": host,
        "url": url,
        "tool": tool,
    }
    if clean_snippet:
        row["snippet"] = clean_snippet
    return row


def build_citation_lookup(capture: _CaptureLike) -> dict[str, dict[str, Any]]:
    """Map story_id / URL → hydrated source card fields from tool outputs."""
    index: dict[str, dict[str, Any]] = {}
    story_drafts: dict[str, dict[str, str]] = {}

    for tool_name, output, tool_input in _iter_tool_outputs(capture):
        if tool_name == "search_evidence":
            for row in output.get("evidence") or []:
                if not isinstance(row, dict):
                    continue
                story_id = str(row.get("story_id") or "").strip()
                headline = str(row.get("headline") or "").strip()
                if story_id and headline:
                    story_drafts[story_id] = {
                        "headline": headline,
                        "tool": tool_name,
                        "snippet": str(row.get("rationale") or "").strip(),
                    }
            continue

        if tool_name == "search_stories":
            for row in output.get("stories") or []:
                if not isinstance(row, dict):
                    continue
                story_id = str(row.get("story_id") or "").strip()
                headline = str(row.get("headline") or "").strip()
                if story_id and headline:
                    story_drafts[story_id] = {
                        "headline": headline,
                        "tool": tool_name,
                        "snippet": str(row.get("snippet") or "").strip(),
                    }
            continue

        if tool_name == "fetch_story":
            story_id = str(
                output.get("story_id") or tool_input.get("story_id") or ""
            ).strip()
            headline = str(output.get("headline") or "").strip()
            if story_id and headline:
                story_drafts[story_id] = {
                    "headline": headline,
                    "tool": tool_name,
                    "snippet": str(output.get("markdown") or "")[:280],
                }
            continue

        if tool_name == "web_search":
            for row in output.get("results") or []:
                if not isinstance(row, dict):
                    continue
                url = str(row.get("url") or "").strip()
                if not url:
                    continue
                title = str(row.get("title") or url).strip()
                snippet = str(row.get("snippet") or "").strip()
                index[url] = _web_row(
                    url=url,
                    title=title,
                    snippet=snippet,
                    tool=tool_name,
                )
            continue

        if tool_name == "web_fetch":
            url = str(output.get("url") or tool_input.get("url") or "").strip()
            if not url:
                continue
            title = str(output.get("title") or url).strip()
            text = str(output.get("text") or "")
            index[url] = _web_row(
                url=url,
                title=title,
                snippet=text,
                tool=tool_name,
            )
            continue

        if tool_name == "recent_filings":
            from src.sec_filings import primary_document_url_fetchable

            ticker = str(
                output.get("ticker") or tool_input.get("ticker") or "SEC filing"
            ).strip()
            for row in output.get("filings") or []:
                if not isinstance(row, dict):
                    continue
                form = str(row.get("form") or "").strip()
                if not primary_document_url_fetchable(form):
                    continue
                url = str(row.get("primary_document_url") or "").strip()
                if not url:
                    continue
                report = str(
                    row.get("report_date") or row.get("filing_date") or ""
                ).strip()
                index[url] = {
                    "kind": "filing",
                    "title": f"{form or 'Filing'} — {ticker}",
                    "source": "SEC EDGAR",
                    "snippet": report,
                    "url": url,
                    "tool": tool_name,
                }

    if story_drafts:
        db_by_id = _load_story_db_fields(list(story_drafts.keys()))
        for story_id, draft in story_drafts.items():
            index[story_id] = _story_row(
                story_id=story_id,
                headline=draft["headline"],
                tool=draft["tool"],
                snippet=draft["snippet"],
                db_fields=db_by_id.get(story_id, {}),
            )

    return index


def _normalize_llm_ref(raw: Any) -> str | None:
    if isinstance(raw, str):
        ref = raw.strip()
        return ref or None
    if isinstance(raw, dict):
        for key in ("story_id", "url", "source"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def enrich_citations(
    raw_citations: list[Any], capture: _CaptureLike
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve minimal LLM refs into frontend-ready source rows."""
    lookup = build_citation_lookup(capture)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    for i, raw in enumerate(raw_citations):
        ref = _normalize_llm_ref(raw)
        if not ref:
            dropped.append(
                {"value": raw, "_validation_reason": "unrecognized citation entry"}
            )
            continue
        row = lookup.get(ref)
        if row is None:
            dropped.append(
                {
                    "ref": ref,
                    "_validation_reason": "not found in any tool output this turn",
                }
            )
            continue
        kept.append({"index": i + 1, **row})

    return kept, dropped


def citation_corpus(capture: _CaptureLike) -> str:
    """Lowercased JSON corpus of captured tool outputs (prose story-id scan)."""
    chunks: list[str] = []
    for _tool_name, output, _tool_input in _iter_tool_outputs(capture):
        try:
            chunks.append(json.dumps(output, default=str, ensure_ascii=False))
        except (TypeError, ValueError):
            chunks.append(str(output))
    return " ".join(chunks).lower()

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencc import OpenCC

from src.clients.gemini import GEMINI_3_FLASH_PREVIEW, generate_text_with_retry
from src.i18n import load_glossary


_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*|\s*```$", re.IGNORECASE)
_OPENCC = OpenCC("s2twp")


@dataclass(frozen=True)
class TranslationSidecars:
    zh_hans_path: Path
    zh_hant_path: Path
    model: str


def _sidecar_path(source_path: Path, locale: str) -> Path:
    return source_path.with_name(f"{source_path.stem}.{locale}{source_path.suffix}")


def _strip_markdown_fence(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _build_translation_prompt(markdown: str, entity_type: str) -> str:
    glossary = load_glossary()
    glossary_block = f"\n\nGlossary:\n{glossary}" if glossary else ""
    return f"""Translate this Heurist Finance {entity_type} markdown from English to Simplified Chinese.

Return markdown only. Do not wrap it in code fences.

Rules:
- Keep all markdown heading labels exactly unchanged. Examples: "# Thesis:", "## Core Thesis", "## Invalidation Conditions", "## Overview", "## Quotes", "## Sources", "## Themes", "## Market Movers".
- Translate user-facing synthesized prose only: titles, thesis text, invalidation bullets, overview bullets, and brief theme text.
- Keep ticker symbols, company names, product names, source IDs, source names, publisher names, URLs, numbers, prices, percentages, dates, and markdown links unchanged.
- Keep block quotes unchanged because they are source text.
- Keep the whole "## Sources" section unchanged because it contains raw source headlines and publisher names.
- Keep source references like "story_123", "news_456", and "_Sources: ..._" unchanged.
- Preserve the direct Heurist Finance voice. Use confident declarative Chinese and avoid hedging words like 可能, 也许, 或许 unless they are inside unchanged source text.
- Preserve markdown structure so the same parser can read the translated file.{glossary_block}

Markdown:
{markdown}"""


def _log_llm_call(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    result: Any,
) -> None:
    usage = getattr(result, "usage", None)
    conn.execute(
        """
        INSERT INTO llm_calls (
          entity_type, entity_id, caller, model_id, latency_seconds,
          input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
          total_tokens, cost_usd, created_at
        ) VALUES (?, ?, 'translate_i18n_markdown', ?, ?, ?, ?, ?, ?, ?, ?,
          strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        """,
        (
            entity_type,
            entity_id,
            getattr(result, "model", "") or "",
            getattr(result, "latency_seconds", 0.0),
            getattr(usage, "input_tokens", 0),
            getattr(usage, "output_tokens", 0),
            getattr(usage, "thinking_tokens", 0),
            getattr(usage, "cache_read_tokens", 0),
            getattr(usage, "total_tokens", 0),
            float(getattr(result, "cost_usd", 0.0) or 0.0),
        ),
    )


def write_translation_sidecars(
    source_path: Path,
    *,
    entity_type: str,
    entity_id: str,
    db_path: Path,
) -> TranslationSidecars | None:
    markdown = source_path.read_text(encoding="utf-8")
    if not markdown.strip():
        return None

    result = generate_text_with_retry(
        _build_translation_prompt(markdown, entity_type),
        model=GEMINI_3_FLASH_PREVIEW,
        max_output_tokens=8192,
        thinking_level="low",
    )
    zh_hans = _strip_markdown_fence(result.text)
    zh_hant = _OPENCC.convert(zh_hans)

    zh_hans_path = _sidecar_path(source_path, "zh-Hans")
    zh_hant_path = _sidecar_path(source_path, "zh-Hant")
    _atomic_write(zh_hans_path, zh_hans)
    _atomic_write(zh_hant_path, zh_hant)

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            _log_llm_call(conn, entity_type=entity_type, entity_id=entity_id, result=result)
            conn.commit()
    except sqlite3.Error:
        pass

    return TranslationSidecars(
        zh_hans_path=zh_hans_path,
        zh_hant_path=zh_hant_path,
        model=result.model,
    )


__all__ = ["TranslationSidecars", "write_translation_sidecars"]

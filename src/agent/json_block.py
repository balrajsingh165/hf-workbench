"""Shared helpers for the trailing JSON metadata block that Phase 2 emits."""

from __future__ import annotations

import json
from typing import Any


_FENCE_OPEN = "\n```json"
_FENCE_CLOSE = "```"
_LANGLESS_FENCE = "\n```"


def find_trailing_json_block_start(text: str) -> int:
    """Index where the trailing citations-bearing JSON block opens, or -1 if absent.

    Detection priority:
      1. The lang-tagged markdown fence ``\\n```json`` (preferred — fires earliest
         in the stream, before the opening-brace chunk arrives).
      2. A lang-less fence ``\\n```` followed by a ``{`` (the model wrapped the
         JSON in a bare ``` fence without the ``json`` lang hint). Only fires
         when the brace is present after the fence — a code fence on its own
         is treated as prose.
      3. Fallback: the rightmost ``{`` that precedes a ``"citations"`` token,
         walking back through any orphan fence chars so they get stripped too.

    Returns -1 when none match.
    """
    fence_idx = text.rfind(_FENCE_OPEN)
    if fence_idx != -1:
        return fence_idx

    # Lang-less fence: search the rightmost ``\n```\s*{`` window.
    langless_idx = text.rfind(_LANGLESS_FENCE)
    if langless_idx != -1:
        after = text[langless_idx + len(_LANGLESS_FENCE):].lstrip()
        if after.startswith("{") or '"citations"' in after:
            return langless_idx

    citations_idx = text.rfind('"citations"')
    if citations_idx == -1:
        return -1
    brace_idx = text.rfind("{", 0, citations_idx)
    if brace_idx == -1:
        return -1

    # Walk back through whitespace + an orphan ``` so the dangling fence
    # chars don't leak into the cleaned prose when the lang-less detector
    # missed an unusual layout (e.g. ``` glued to the brace with no newline).
    lookback = brace_idx
    while lookback > 0 and text[lookback - 1] in " \t\r\n":
        lookback -= 1
    if lookback >= 3 and text[lookback - 3:lookback] == "```":
        fence_start = lookback - 3
        if fence_start > 0 and text[fence_start - 1] == "\n":
            fence_start -= 1
        return fence_start

    return brace_idx


def parse_trailing_json_block(text: str) -> dict[str, Any]:
    """Return the parsed trailing JSON block as a dict, or {} if none/invalid."""
    start = find_trailing_json_block_start(text)
    if start == -1:
        return {}
    snippet = text[start:]
    # When the sentinel was the markdown fence, advance to the actual JSON object.
    brace_idx = snippet.find("{")
    if brace_idx == -1:
        return {}
    snippet = snippet[brace_idx:]
    # Trim a possible trailing ``` fence so json.loads succeeds.
    end_fence = snippet.rfind(_FENCE_CLOSE)
    if end_fence != -1:
        snippet = snippet[:end_fence]
    try:
        parsed = json.loads(snippet)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

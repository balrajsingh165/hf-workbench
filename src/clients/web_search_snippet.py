"""Snippet normalization for open-web search (discovery, not full-page scrape)."""

from __future__ import annotations

import re

SNIPPET_MAX_LEN = 400
_DISCOVERY_SOURCE_MAX = 600

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]+\)")
_JUNK_RE = re.compile(
    r"skip to (?:main|navigation|right column)"
    r"|error 403\b.*\bforbidden"
    r"|oops, something went wrong"
    r"|that['\u2019]s an error\.\s*we['\u2019]re sorry, but you do not have access",
    re.IGNORECASE | re.DOTALL,
)


def sanitize_snippet(text: str, *, max_len: int = SNIPPET_MAX_LEN) -> str:
    """Collapse whitespace, strip markdown links, trim to agent-facing length."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return ""
    stripped = _MARKDOWN_LINK_RE.sub(r"\1", collapsed).strip()
    if len(stripped) > max_len:
        stripped = stripped[: max_len - 3].rstrip() + "..."
    return stripped


def is_junk_snippet(text: str) -> bool:
    """True when the snippet is nav chrome, bot wall, or otherwise unusable."""
    if not text or len(text) < 24:
        return True
    return bool(_JUNK_RE.search(text[:500]))


def prepare_snippet(text: str) -> str:
    """Sanitize and return empty when the result should be dropped."""
    clean = sanitize_snippet(text)
    if is_junk_snippet(clean):
        return ""
    return clean

"""Drop tool-input echoes from agent-facing tool payloads to save tokens."""

from __future__ import annotations

from typing import Any

# Top-level response keys that duplicate the tool call arguments.
_TOOL_INPUT_ECHO_KEYS: dict[str, frozenset[str]] = {
    "web_search": frozenset({"query"}),
    "web_fetch": frozenset({"url"}),
    "fetch_story": frozenset({"story_id"}),
    "price_summary": frozenset({"ticker"}),
    "price_history": frozenset({"ticker"}),
    "recent_filings": frozenset({"ticker"}),
    "recent_insider": frozenset({"ticker"}),
    "fundamentals_snapshot": frozenset({"ticker"}),
    "xbrl_fact": frozenset({"ticker", "metric", "frequency"}),
}


def strip_tool_input_echo(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy without keys the model already sent in tool args."""
    drop = _TOOL_INPUT_ECHO_KEYS.get(tool_name)
    if not drop:
        return payload
    return {key: value for key, value in payload.items() if key not in drop}

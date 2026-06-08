"""Offline check of the Responses API parser — no API key required.

    uv run python spikes/grok-social/test_extract.py
"""

from __future__ import annotations

from grok_client import _extract

# A plausible Responses API payload with a reasoning item + final message
# carrying output_text and url_citation annotations.
SAMPLE = {
    "output": [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "thinking..."}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "The crowd is leaning bullish on NVDA into earnings.",
                    "annotations": [
                        {"type": "url_citation", "url": "https://x.com/trader/status/1"},
                        {"type": "url_citation", "url_citation": {"url": "https://x.com/quant/status/2"}},
                    ],
                }
            ],
        },
    ],
    "usage": {"input_tokens": 1200, "output_tokens": 340},
}


def main() -> int:
    text, citations = _extract(SAMPLE)
    assert "bullish on NVDA" in text, text
    assert citations == [
        "https://x.com/trader/status/1",
        "https://x.com/quant/status/2",
    ], citations
    print("OK — text and 2 citations extracted")
    print(f"  text: {text!r}")
    print(f"  citations: {citations}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

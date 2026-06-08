from __future__ import annotations

from types import SimpleNamespace

from src.clients.gemini import compute_gemini_cost_usd, extract_usage


def test_extract_usage_splits_cached_prompt_tokens() -> None:
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=1200,
            cached_content_token_count=200,
            candidates_token_count=300,
            thoughts_token_count=50,
            total_token_count=1550,
        )
    )

    usage = extract_usage(response)

    assert usage.input_tokens == 1000
    assert usage.cache_read_tokens == 200
    assert usage.output_tokens == 300
    assert usage.thinking_tokens == 50
    assert usage.total_tokens == 1550


def test_gemini_cost_uses_output_rate_for_thinking_tokens() -> None:
    response = {
        "usageMetadata": {
            "promptTokenCount": 1200,
            "cachedContentTokenCount": 200,
            "candidatesTokenCount": 300,
            "thoughtsTokenCount": 50,
            "totalTokenCount": 1550,
        }
    }

    usage = extract_usage(response)

    assert compute_gemini_cost_usd("gemini-3-flash-preview", usage) == 0.00156

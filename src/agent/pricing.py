"""Bedrock model pricing (USD per 1M tokens).

Static dict; revisit quarterly against the AWS Bedrock pricing page. The
markup that turns USD into user-facing credits lives in the billing layer
(see `docs/design-billing-credits.md`), not here. Non-Bedrock models served
via composer-relay carry an estimated rate (see the `composer-*` entries).

Model IDs on Bedrock include a region prefix and a version suffix
(e.g. `us.anthropic.claude-haiku-4-5-20251001-v1:0`); we match by
substring so a version bump doesn't silently route to $0 pricing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """Per-1M-token USD prices for one model family."""

    input_per_1m: float
    output_per_1m: float
    cache_read_per_1m: float
    cache_write_per_1m: float


# Source of truth: Anthropic / AWS Bedrock public pricing as of 2026-Q2.
# Keys are substrings matched against the full Bedrock model_id.
BEDROCK_PRICING: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(
        input_per_1m=1.00,
        output_per_1m=5.00,
        cache_read_per_1m=0.10,
        cache_write_per_1m=1.25,
    ),
    "claude-sonnet-4-6": ModelPrice(
        input_per_1m=3.00,
        output_per_1m=15.00,
        cache_read_per_1m=0.30,
        cache_write_per_1m=3.75,
    ),
    "claude-opus-4-7": ModelPrice(
        input_per_1m=5.00,
        output_per_1m=25.00,
        cache_read_per_1m=0.50,
        cache_write_per_1m=6.25,
    ),
    # Older families kept for safety in case BEDROCK_MODEL_ID rolls back.
    "claude-sonnet-4-5": ModelPrice(
        input_per_1m=3.00,
        output_per_1m=15.00,
        cache_read_per_1m=0.30,
        cache_write_per_1m=3.75,
    ),
    # composer-2.5-fast runs via composer-relay, not Bedrock, so it has no
    # public per-token rate. ESTIMATE: priced at 1/3 of Sonnet 4.6 across all
    # buckets, a placeholder until a real rate exists. Revisit if relay billing
    # changes. (The relay is Pro-included today; this only affects internal
    # cost accounting, not what a user is charged.)
    "composer-2.5-fast": ModelPrice(
        input_per_1m=1.00,  # 3.00 / 3
        output_per_1m=5.00,  # 15.00 / 3
        cache_read_per_1m=0.10,  # 0.30 / 3
        cache_write_per_1m=1.25,  # 3.75 / 3
    ),
}


def lookup_price(model_id: str) -> ModelPrice | None:
    """Find the price entry whose key appears in `model_id`."""
    if not model_id:
        return None
    mid = model_id.lower()
    for key, price in BEDROCK_PRICING.items():
        if key in mid:
            return price
    return None


def compute_cost_usd(model_id: str, usage: dict) -> float:
    """Compute USD cost from a Strands usage summary.

    `usage` keys follow the Bedrock convention:
    `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens`.
    Returns 0.0 when the model is unknown — surface that as a logged warning
    upstream, not an exception, so a missing pricing entry never breaks a
    user's chat turn.
    """
    price = lookup_price(model_id)
    if price is None or not usage:
        return 0.0

    input_tokens = int(usage.get("inputTokens", 0) or 0)
    output_tokens = int(usage.get("outputTokens", 0) or 0)
    cache_read = int(usage.get("cacheReadInputTokens", 0) or 0)
    cache_write = int(usage.get("cacheWriteInputTokens", 0) or 0)

    # Cached input tokens are billed at the discounted cache rate, not at
    # the regular input rate. The Bedrock `inputTokens` field in Strands
    # already excludes cache reads/writes, so we sum the four buckets.
    cost = (
        input_tokens * price.input_per_1m
        + output_tokens * price.output_per_1m
        + cache_read * price.cache_read_per_1m
        + cache_write * price.cache_write_per_1m
    ) / 1_000_000.0
    return round(cost, 6)


__all__ = ["ModelPrice", "BEDROCK_PRICING", "lookup_price", "compute_cost_usd"]

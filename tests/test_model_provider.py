"""Unit tests for per-phase model-provider resolution.

These cover the pure model-id resolvers (no model client is constructed), so
the suite stays offline. The resolvers decide which model id each phase reports
for usage accounting, mirroring which provider its factory will build.
"""

from __future__ import annotations

from src.agent.config import AgentConfig
from src.agent.model_provider import (
    _is_composer,
    chart_model_id,
    research_model_id,
    response_model_id,
)


def _cfg(*, research: str, response: str, chart: str) -> AgentConfig:
    return AgentConfig(
        aws_region="us-west-2",
        bedrock_profile="payments-admin",
        bedrock_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        response_bedrock_model_id="us.anthropic.claude-sonnet-4-6",
        research_max_tokens=12_000,
        response_max_tokens=10_000,
        agent_timeout_seconds=300,
        chart_agent_timeout_seconds=120,
        chart_agent_max_tokens=8_000,
        r2_endpoint=None,
        r2_bucket=None,
        r2_access_key=None,
        r2_secret_key=None,
        r2_public_base_url=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
        langfuse_base_url=None,
        langfuse_environment="test",
        langfuse_service_name="hf-workbench-test",
        research_model_provider=research,
        response_model_provider=response,
        chart_model_provider=chart,
        composer_relay_model_id="composer-2.5-fast",
    )


def test_is_composer_recognizes_aliases() -> None:
    assert _is_composer("composer")
    assert _is_composer("composer-relay")
    assert _is_composer("openai")
    assert _is_composer("COMPOSER")  # case-insensitive
    assert not _is_composer("bedrock")
    assert not _is_composer("")


def test_default_all_bedrock() -> None:
    cfg = _cfg(research="bedrock", response="bedrock", chart="bedrock")
    assert research_model_id(cfg) == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert response_model_id(cfg) == "us.anthropic.claude-sonnet-4-6"
    assert chart_model_id(cfg) == "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_research_and_chart_composer_response_bedrock() -> None:
    """The shipped configuration: research + chart on composer, response on Sonnet."""
    cfg = _cfg(research="composer", response="bedrock", chart="composer")
    assert research_model_id(cfg) == "composer-2.5-fast"
    assert chart_model_id(cfg) == "composer-2.5-fast"
    assert response_model_id(cfg) == "us.anthropic.claude-sonnet-4-6"


def test_phases_are_independent() -> None:
    cfg = _cfg(research="bedrock", response="composer", chart="bedrock")
    assert research_model_id(cfg) == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert response_model_id(cfg) == "composer-2.5-fast"
    assert chart_model_id(cfg) == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

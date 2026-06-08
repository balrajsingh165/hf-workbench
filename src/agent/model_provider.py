"""Model-provider factory for Strands agents.

Each phase (research, response, chart) chooses its provider independently via
its own config field. The default is Bedrock; setting a phase's provider to
`composer`, `composer-relay`, or `openai` swaps that phase's Strands model to
the OpenAI-compatible adapter pointed at composer-relay.
"""

from __future__ import annotations

from typing import Any

import boto3
from strands.models import BedrockModel, OpenAIModel
from strands.models.bedrock import CacheConfig

from src.agent.config import AgentConfig


_COMPOSER_PROVIDERS = {"composer", "composer-relay", "openai"}


def _is_composer(provider: str) -> bool:
    return provider.lower() in _COMPOSER_PROVIDERS


def research_model_id(cfg: AgentConfig) -> str:
    return (
        cfg.composer_relay_model_id
        if _is_composer(cfg.research_model_provider)
        else cfg.bedrock_model_id
    )


def response_model_id(cfg: AgentConfig) -> str:
    return (
        cfg.composer_relay_model_id
        if _is_composer(cfg.response_model_provider)
        else cfg.response_bedrock_model_id
    )


def chart_model_id(cfg: AgentConfig) -> str:
    return (
        cfg.composer_relay_model_id
        if _is_composer(cfg.chart_model_provider)
        else cfg.bedrock_model_id
    )


def _composer_model(cfg: AgentConfig, *, max_tokens: int, temperature: float) -> OpenAIModel:
    return OpenAIModel(
        model_id=cfg.composer_relay_model_id,
        client_args={
            "api_key": cfg.composer_relay_api_key or "local-composer-relay",
            "base_url": cfg.composer_relay_base_url,
        },
        params={
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )


def create_research_model(cfg: AgentConfig) -> Any:
    if _is_composer(cfg.research_model_provider):
        return _composer_model(cfg, max_tokens=cfg.research_max_tokens, temperature=0)
    return BedrockModel(
        boto_session=boto3.Session(
            profile_name=cfg.bedrock_profile,
            region_name=cfg.aws_region,
        ),
        model_id=cfg.bedrock_model_id,
        temperature=0,
        max_tokens=cfg.research_max_tokens,
        cache_config=CacheConfig(strategy="auto"),
        cache_tools="default",
    )


def create_response_model(cfg: AgentConfig, *, thinking_budget: int) -> Any:
    if _is_composer(cfg.response_model_provider):
        # composer-relay surfaces Composer reasoning when available; Bedrock's
        # Anthropic-only `thinking` request field is intentionally not sent to
        # the OpenAI-compatible chat-completions adapter.
        return _composer_model(
            cfg,
            max_tokens=cfg.response_max_tokens,
            temperature=1 if thinking_budget > 0 else 0,
        )
    model_kwargs: dict[str, Any] = {
        "boto_session": boto3.Session(
            profile_name=cfg.bedrock_profile,
            region_name=cfg.aws_region,
        ),
        "model_id": cfg.response_bedrock_model_id,
        # Bedrock requires temperature=1 when extended thinking is enabled.
        "temperature": 1 if thinking_budget > 0 else 0,
        "max_tokens": cfg.response_max_tokens,
    }
    if thinking_budget > 0:
        model_kwargs["additional_request_fields"] = {
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
        }
    return BedrockModel(**model_kwargs)


def create_chart_model(cfg: AgentConfig) -> Any:
    if _is_composer(cfg.chart_model_provider):
        return _composer_model(cfg, max_tokens=cfg.chart_agent_max_tokens, temperature=0)
    return BedrockModel(
        boto_session=boto3.Session(
            profile_name=cfg.bedrock_profile,
            region_name=cfg.aws_region,
        ),
        model_id=cfg.bedrock_model_id,
        temperature=0,
        max_tokens=cfg.chart_agent_max_tokens,
    )

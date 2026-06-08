"""Strands model-provider config for the agent submodule.

The base hf-workbench env (HF_API_BASE, DB paths, etc.) is read elsewhere.
This file adds the AWS Bedrock defaults plus the composer-relay
OpenAI-compatible override. Each phase (research, response, chart) picks its
provider independently via its own env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass(frozen=True)
class AgentConfig:
    aws_region: str
    bedrock_profile: str | None
    bedrock_model_id: str
    response_bedrock_model_id: str
    research_max_tokens: int
    response_max_tokens: int
    agent_timeout_seconds: int
    chart_agent_timeout_seconds: int
    chart_agent_max_tokens: int
    r2_endpoint: str | None
    r2_bucket: str | None
    r2_access_key: str | None
    r2_secret_key: str | None
    r2_public_base_url: str | None
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_base_url: str | None
    langfuse_environment: str
    langfuse_service_name: str
    # Provider per phase: "bedrock" (default) or "composer"/"composer-relay"/
    # "openai" to route that phase through composer-relay. Each phase is
    # independent — no single global switch.
    research_model_provider: str = "bedrock"
    response_model_provider: str = "bedrock"
    chart_model_provider: str = "bedrock"
    composer_relay_base_url: str = "http://127.0.0.1:4005/v1"
    composer_relay_api_key: str | None = "local-composer-relay"
    composer_relay_model_id: str = "composer-2.5-fast"


@cache
def get_agent_config() -> AgentConfig:
    return AgentConfig(
        research_model_provider=os.getenv("RESEARCH_MODEL_PROVIDER", "bedrock").lower(),
        response_model_provider=os.getenv("RESPONSE_MODEL_PROVIDER", "bedrock").lower(),
        chart_model_provider=os.getenv("CHART_MODEL_PROVIDER", "bedrock").lower(),
        aws_region=os.getenv("AWS_REGION", "us-west-2"),
        bedrock_profile=os.getenv("BEDROCK_PROFILE") or os.getenv("AWS_PROFILE"),
        bedrock_model_id=os.getenv(
            "BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
        response_bedrock_model_id=os.getenv(
            "RESPONSE_BEDROCK_MODEL_ID",
            os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ),
        composer_relay_base_url=os.getenv(
            "COMPOSER_RELAY_BASE_URL", "http://127.0.0.1:4005/v1"
        ).rstrip("/"),
        composer_relay_api_key=os.getenv("COMPOSER_RELAY_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or "local-composer-relay",
        composer_relay_model_id=os.getenv("COMPOSER_RELAY_MODEL_ID", "composer-2.5-fast"),
        research_max_tokens=int(os.getenv("RESEARCH_MAX_TOKENS", "12000")),
        response_max_tokens=int(os.getenv("RESPONSE_MAX_TOKENS", "10000")),
        agent_timeout_seconds=int(os.getenv("AGENT_TIMEOUT_SECONDS", "300")),
        chart_agent_timeout_seconds=int(os.getenv("CHART_AGENT_TIMEOUT_S", "120")),
        chart_agent_max_tokens=int(os.getenv("CHART_AGENT_MAX_TOKENS", "8000")),
        r2_endpoint=os.getenv("R2_ENDPOINT") or None,
        r2_bucket=os.getenv("R2_BUCKET") or None,
        r2_access_key=os.getenv("R2_ACCESS_KEY") or None,
        r2_secret_key=os.getenv("R2_SECRET_KEY") or None,
        r2_public_base_url=(os.getenv("R2_PUBLIC_BASE_URL") or "").rstrip("/") or None,
        langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY") or None,
        langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY") or None,
        langfuse_base_url=os.getenv("LANGFUSE_BASE_URL") or None,
        langfuse_environment=os.getenv("LANGFUSE_ENVIRONMENT", "development"),
        langfuse_service_name=os.getenv("LANGFUSE_SERVICE_NAME", "hf-workbench"),
    )

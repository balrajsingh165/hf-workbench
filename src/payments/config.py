"""Configuration for AgentCore Payments.

Defaults point at the permanent live resources in AWS account 441070252417
(agentcore-payments-beta-main/AGENTS.md). Every value is env-overridable. A blank
AGENTPAY_MANAGER_ARN disables the payments API surface (returns 503).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_MANAGER_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:441070252417:"
    "payment-manager/mypaymentmanager-ysmz9kzgdx"
)
_DEFAULT_CONNECTOR_ID = "mycoinbaseconnector-s8a2swf9ic"
_DEFAULT_MANAGEMENT_ROLE = "arn:aws:iam::441070252417:role/AgentCorePaymentsManagementRole"
_DEFAULT_PROCESS_PAYMENT_ROLE = "arn:aws:iam::441070252417:role/AgentCorePaymentsProcessPaymentRole"


def _opt(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def _float_opt(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class PaymentsConfig:
    enabled: bool
    aws_region: str
    aws_profile: str | None
    dp_endpoint: str | None
    manager_arn: str
    connector_id: str
    management_role_arn: str
    process_payment_role_arn: str
    wallet_network: str
    default_x402_network: str
    linked_email_domain: str
    session_expiry_minutes: int
    unlimited_session_cap_usd: float
    default_max_per_query_usd: float | None
    default_max_per_day_usd: float | None
    usdc_decimals: int


def get_payments_config() -> PaymentsConfig:
    manager_arn = os.environ.get("AGENTPAY_MANAGER_ARN", _DEFAULT_MANAGER_ARN).strip()
    return PaymentsConfig(
        enabled=bool(manager_arn),
        aws_region=os.environ.get("AWS_REGION", "us-west-2"),
        aws_profile=_opt("AWS_PROFILE"),
        dp_endpoint=_opt("AGENTPAY_DP_ENDPOINT"),
        manager_arn=manager_arn,
        connector_id=os.environ.get("AGENTPAY_CONNECTOR_ID", _DEFAULT_CONNECTOR_ID).strip(),
        management_role_arn=os.environ.get("AGENTPAY_MANAGEMENT_ROLE_ARN", _DEFAULT_MANAGEMENT_ROLE).strip(),
        process_payment_role_arn=os.environ.get(
            "AGENTPAY_PROCESS_PAYMENT_ROLE_ARN", _DEFAULT_PROCESS_PAYMENT_ROLE
        ).strip(),
        wallet_network=os.environ.get("AGENTPAY_WALLET_NETWORK", "ETHEREUM").strip(),
        default_x402_network=os.environ.get("AGENTPAY_X402_NETWORK", "base-sepolia").strip(),
        linked_email_domain=os.environ.get("AGENTPAY_LINKED_EMAIL_DOMAIN", "hf-users.heurist.xyz").strip(),
        session_expiry_minutes=int(os.environ.get("AGENTPAY_SESSION_EXPIRY_MINUTES", "60")),
        unlimited_session_cap_usd=float(os.environ.get("AGENTPAY_UNLIMITED_SESSION_CAP_USD", "10000.0")),
        default_max_per_query_usd=_float_opt("AGENTPAY_DEFAULT_MAX_PER_QUERY_USD"),
        default_max_per_day_usd=_float_opt("AGENTPAY_DEFAULT_MAX_PER_DAY_USD"),
        usdc_decimals=int(os.environ.get("AGENTPAY_USDC_DECIMALS", "6")),
    )

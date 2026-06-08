"""Dev-friendly HTTP API for per-user wallets, quotas, and x402 payments.

Simple shapes to exercise the core logic end to end before wiring payments into
the agent orchestrator. Like the rest of hf-workbench there is no auth layer —
`user_id` is a path param the trusted caller supplies.

    POST /api/v1/payments/users/{user_id}/wallet     provision (idempotent)
    GET  /api/v1/payments/users/{user_id}/wallet     address + balance + grant URL
    PUT  /api/v1/payments/users/{user_id}/quotas     set per-query / per-day caps
    GET  /api/v1/payments/users/{user_id}/quotas     read caps
    GET  /api/v1/payments/users/{user_id}/spending   today's spend + recent ledger
    POST /api/v1/payments/users/{user_id}/pay        pay one x402 resource
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.payments import quotas, wallets
from src.payments.agentcore import get_agentcore
from src.payments.config import get_payments_config
from src.payments.service import pay_for_resource

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


def _require_enabled() -> None:
    if not get_payments_config().enabled:
        raise HTTPException(status_code=503, detail="AgentCore Payments is not configured")


# ── models ────────────────────────────────────────────────────────────
class ProvisionWalletRequest(BaseModel):
    email: str | None = Field(default=None, description="Linked email; synthesized if omitted")


class WalletResponse(BaseModel):
    user_id: str
    payment_instrument_id: str
    wallet_address: str | None = None
    wallet_network: str
    status: str
    redirect_url: str | None = None
    grant_required: bool
    balance: dict[str, Any] | None = None


class QuotasRequest(BaseModel):
    max_spend_per_query_usd: float | None = Field(default=None, ge=0)
    max_spend_per_day_usd: float | None = Field(default=None, ge=0)
    x402_network: str | None = None


class QuotasResponse(BaseModel):
    user_id: str
    max_spend_per_query_usd: float | None
    max_spend_per_day_usd: float | None
    x402_network: str
    spent_today_usd: float


class SpendingResponse(BaseModel):
    user_id: str
    spent_today_usd: float
    max_spend_per_day_usd: float | None
    remaining_today_usd: float | None
    ledger: list[dict[str, Any]]


class PayRequest(BaseModel):
    resource_url: str
    body: dict[str, Any] | None = None
    method: str = "POST"
    invocation_id: str | None = Field(
        default=None, description="Group several payments under one agent turn"
    )


# ── wallet ────────────────────────────────────────────────────────────
def _wallet_response(user_id: str, row: dict[str, Any], balance: dict[str, Any] | None) -> WalletResponse:
    return WalletResponse(
        user_id=user_id,
        payment_instrument_id=row["payment_instrument_id"],
        wallet_address=row.get("wallet_address"),
        wallet_network=row.get("wallet_network", "ETHEREUM"),
        status=row.get("status", "pending_grant"),
        redirect_url=row.get("redirect_url"),
        grant_required=row.get("status") != "active",
        balance=balance,
    )


@router.post("/users/{user_id}/wallet", response_model=WalletResponse)
def provision_wallet(user_id: str, req: ProvisionWalletRequest | None = None) -> WalletResponse:
    _require_enabled()
    try:
        row = wallets.get_or_provision(user_id, email=(req.email if req else None))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"wallet provisioning failed: {exc}") from exc
    return _wallet_response(user_id, row, balance=None)


@router.get("/users/{user_id}/wallet", response_model=WalletResponse)
def get_wallet(user_id: str) -> WalletResponse:
    _require_enabled()
    row = wallets.refresh_status(user_id)  # syncs ACTIVE status after the grant
    if row is None:
        raise HTTPException(status_code=404, detail="No wallet for this user; POST to provision one")
    balance = get_agentcore().get_wallet_balance(user_id, row["payment_instrument_id"])
    return _wallet_response(user_id, row, balance=balance)


# ── quotas ────────────────────────────────────────────────────────────
@router.put("/users/{user_id}/quotas", response_model=QuotasResponse)
def put_quotas(user_id: str, req: QuotasRequest) -> QuotasResponse:
    _require_enabled()
    q = quotas.set_quotas(
        user_id,
        max_spend_per_query_usd=req.max_spend_per_query_usd,
        max_spend_per_day_usd=req.max_spend_per_day_usd,
        x402_network=req.x402_network,
    )
    return QuotasResponse(
        user_id=user_id,
        max_spend_per_query_usd=q["max_spend_per_query_usd"],
        max_spend_per_day_usd=q["max_spend_per_day_usd"],
        x402_network=q["x402_network"],
        spent_today_usd=quotas.spent_today_usd(user_id),
    )


@router.get("/users/{user_id}/quotas", response_model=QuotasResponse)
def get_quotas_endpoint(user_id: str) -> QuotasResponse:
    _require_enabled()
    q = quotas.get_quotas(user_id)
    return QuotasResponse(
        user_id=user_id,
        max_spend_per_query_usd=q["max_spend_per_query_usd"],
        max_spend_per_day_usd=q["max_spend_per_day_usd"],
        x402_network=q["x402_network"],
        spent_today_usd=quotas.spent_today_usd(user_id),
    )


@router.get("/users/{user_id}/spending", response_model=SpendingResponse)
def get_spending(user_id: str) -> SpendingResponse:
    _require_enabled()
    q = quotas.get_quotas(user_id)
    spent = quotas.spent_today_usd(user_id)
    per_day = q["max_spend_per_day_usd"]
    remaining = None if per_day is None else max(0.0, per_day - spent)
    return SpendingResponse(
        user_id=user_id,
        spent_today_usd=spent,
        max_spend_per_day_usd=per_day,
        remaining_today_usd=remaining,
        ledger=quotas.recent_ledger(user_id),
    )


# ── pay ───────────────────────────────────────────────────────────────
@router.post("/users/{user_id}/pay")
def pay(user_id: str, req: PayRequest) -> dict[str, Any]:
    _require_enabled()
    try:
        return pay_for_resource(
            user_id,
            req.resource_url,
            body=req.body,
            method=req.method,
            invocation_id=req.invocation_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"payment error: {exc}") from exc

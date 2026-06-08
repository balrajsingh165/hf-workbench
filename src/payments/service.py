"""Pay for a single x402 resource on behalf of a user, within their quotas.

The core logic the dev `/pay` endpoint exercises and the agent orchestrator will
later call per paid tool:

  1. Ensure the user has an embedded wallet (lazy provision).
  2. Compute the per-query / remaining-per-day caps from quotas + ledger.
  3. Hard-block up front if the daily cap is already spent.
  4. Open a PaymentSession whose maxSpendAmount = the effective cap (AWS enforces
     it in flight) and run the x402 flow, with an on_quote gate that rejects an
     over-cap quote before any payment is signed.
  5. Record the outcome (settled / blocked_quota / failed / ...) to the ledger.

Multiple calls sharing one `invocation_id` accumulate toward the same per-query
cap, so an agent that pays for several resources in one turn is bounded in total.
"""

from __future__ import annotations

import uuid
from typing import Any

from src.payments import quotas, wallets
from src.payments.agentcore import AgentCorePayments, QuoteRejected, X402Interceptor, get_agentcore
from src.payments.config import PaymentsConfig, get_payments_config
from src.payments.quotas import QuotaError


def _effective_caps(
    user_id: str, invocation_id: str, q: dict[str, Any]
) -> tuple[float | None, float | None]:
    """(remaining_per_query, remaining_per_day) in USD; None = unlimited."""
    per_query = q.get("max_spend_per_query_usd")
    per_day = q.get("max_spend_per_day_usd")
    remaining_query = (
        None if per_query is None
        else max(0.0, per_query - quotas.spent_in_invocation_usd(invocation_id))
    )
    remaining_day = (
        None if per_day is None
        else max(0.0, per_day - quotas.spent_today_usd(user_id))
    )
    return remaining_query, remaining_day


def pay_for_resource(
    user_id: str,
    resource_url: str,
    *,
    body: dict[str, Any] | None = None,
    method: str = "POST",
    invocation_id: str | None = None,
    email: str | None = None,
    agentcore: AgentCorePayments | None = None,
    cfg: PaymentsConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or get_payments_config()
    agentcore = agentcore or get_agentcore()
    invocation_id = invocation_id or f"inv_{uuid.uuid4().hex[:16]}"

    wallet = wallets.get_or_provision(user_id, email=email, agentcore=agentcore, cfg=cfg)
    instrument_id = wallet["payment_instrument_id"]
    q = quotas.get_quotas(user_id, cfg)
    network = q.get("x402_network") or cfg.default_x402_network

    remaining_query, remaining_day = _effective_caps(user_id, invocation_id, q)

    # Daily cap already exhausted → block without touching AgentCore.
    if remaining_day is not None and remaining_day <= 0:
        quotas.record_payment(
            user_id=user_id, invocation_id=invocation_id, resource_url=resource_url,
            amount_usd=0.0, status="blocked_quota", detail="daily cap exhausted",
            payment_instrument_id=instrument_id, x402_network=network,
        )
        return _envelope(invocation_id, wallet, q, status="blocked_quota",
                         detail="Daily spend cap already reached.", result=None)

    # Session limit = tightest active cap (AWS enforces it in flight).
    caps = [c for c in (remaining_query, remaining_day) if c is not None]
    session_cap = min(caps) if caps else cfg.unlimited_session_cap_usd
    session_id = agentcore.create_session(user_id, session_cap)

    def on_quote(quote_usd: float | None) -> None:
        if quote_usd is None:
            return  # unknown price — let AWS's session cap be the backstop
        if remaining_query is not None and quote_usd > remaining_query:
            raise QuotaError("per_query", remaining_query, quote_usd)
        if remaining_day is not None and quote_usd > remaining_day:
            raise QuotaError("per_day", remaining_day, quote_usd)

    interceptor = X402Interceptor(
        agentcore,
        user_id=user_id,
        session_id=session_id,
        instrument_id=instrument_id,
        default_network=network,
        on_quote=on_quote,
    )

    try:
        result = interceptor.fetch_with_payment(resource_url, body=body, method=method)
    except QuoteRejected as exc:  # pragma: no cover - hook raises QuotaError directly
        result = None
        quotas.record_payment(
            user_id=user_id, invocation_id=invocation_id, resource_url=resource_url,
            amount_usd=0.0, status="blocked_quota", detail=str(exc),
            payment_session_id=session_id, payment_instrument_id=instrument_id,
            x402_network=network,
        )
        return _envelope(invocation_id, wallet, q, status="blocked_quota",
                         detail=str(exc), result=None)
    except QuotaError as exc:
        quotas.record_payment(
            user_id=user_id, invocation_id=invocation_id, resource_url=resource_url,
            amount_usd=0.0, status="blocked_quota", detail=str(exc),
            payment_session_id=session_id, payment_instrument_id=instrument_id,
            x402_network=network,
        )
        return _envelope(invocation_id, wallet, q, status="blocked_quota",
                         detail=str(exc), result=None)

    status = result.get("status")
    quote_usd = result.get("quote_usd") or 0.0
    if status == "success":
        ledger_status, amount = "settled", float(quote_usd)
    elif status == "success_without_payment":
        ledger_status, amount = "settled", 0.0  # resource was free
    elif status == "insufficient_balance":
        ledger_status, amount = "insufficient_balance", 0.0
    else:
        ledger_status, amount = "failed", 0.0

    quotas.record_payment(
        user_id=user_id, invocation_id=invocation_id, resource_url=resource_url,
        amount_usd=amount, status=ledger_status, detail=status,
        payment_session_id=session_id, payment_instrument_id=instrument_id,
        x402_network=network,
    )
    return _envelope(invocation_id, wallet, q, status=ledger_status,
                     detail=status, result=result, amount_usd=amount,
                     payment_session_id=session_id)


def _envelope(
    invocation_id: str,
    wallet: dict[str, Any],
    q: dict[str, Any],
    *,
    status: str,
    detail: str | None,
    result: dict[str, Any] | None,
    amount_usd: float = 0.0,
    payment_session_id: str | None = None,
) -> dict[str, Any]:
    return {
        "invocation_id": invocation_id,
        "status": status,
        "detail": detail,
        "amount_usd": amount_usd,
        "payment_session_id": payment_session_id,
        "wallet": {
            "payment_instrument_id": wallet["payment_instrument_id"],
            "wallet_address": wallet.get("wallet_address"),
            "wallet_status": wallet.get("status"),
            "redirect_url": wallet.get("redirect_url"),
        },
        "quotas": {
            "max_spend_per_query_usd": q.get("max_spend_per_query_usd"),
            "max_spend_per_day_usd": q.get("max_spend_per_day_usd"),
            "spent_today_usd": quotas.spent_today_usd(wallet["user_id"]),
        },
        "x402": result,
    }

"""Per-user CDP Embedded Wallet store and lazy provisioning.

One wallet (AgentCore payment instrument) per user in `user_wallets`, created on
first need and reused thereafter. A new wallet is `pending_grant` until the user
opens `redirect_url` (Coinbase WalletHub) to grant delegated signing.
"""

from __future__ import annotations

from typing import Any

from api import db
from src.payments.agentcore import AgentCorePayments, get_agentcore
from src.payments.config import PaymentsConfig, get_payments_config


def synthesize_email(user_id: str, cfg: PaymentsConfig) -> str:
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in user_id).strip("-")
    return f"{safe or 'user'}@{cfg.linked_email_domain}"


def get_wallet_row(user_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM user_wallets WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_or_provision(
    user_id: str,
    *,
    email: str | None = None,
    agentcore: AgentCorePayments | None = None,
    cfg: PaymentsConfig | None = None,
) -> dict[str, Any]:
    """Return the user's wallet row, creating the embedded wallet if absent."""
    existing = get_wallet_row(user_id)
    if existing:
        return existing

    cfg = cfg or get_payments_config()
    agentcore = agentcore or get_agentcore()
    linked_email = email or synthesize_email(user_id, cfg)
    summary = agentcore.create_embedded_wallet(user_id, linked_email)

    status = "active" if (summary.get("status") == "ACTIVE") else "pending_grant"
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_wallets (
                user_id, payment_instrument_id, wallet_address, linked_email,
                wallet_network, redirect_url, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (
                user_id,
                summary["payment_instrument_id"],
                summary.get("wallet_address"),
                linked_email,
                cfg.wallet_network,
                summary.get("redirect_url"),
                status,
            ),
        )
        conn.commit()
    return get_wallet_row(user_id)  # type: ignore[return-value]


def refresh_status(user_id: str, agentcore: AgentCorePayments | None = None) -> dict[str, Any] | None:
    """Re-read the instrument from AgentCore and sync address/status locally
    (the instrument flips to ACTIVE once the WalletHub grant is completed)."""
    row = get_wallet_row(user_id)
    if not row:
        return None
    agentcore = agentcore or get_agentcore()
    summary = agentcore.get_wallet(user_id, row["payment_instrument_id"])
    status = "active" if (summary.get("status") == "ACTIVE") else row["status"]
    with db() as conn:
        conn.execute(
            """
            UPDATE user_wallets
               SET wallet_address = COALESCE(?, wallet_address),
                   status = ?,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
             WHERE user_id = ?
            """,
            (summary.get("wallet_address"), status, user_id),
        )
        conn.commit()
    return get_wallet_row(user_id)

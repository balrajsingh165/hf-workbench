"""Per-user spend quotas + the x402 payment ledger.

Two caps per user: `max_spend_per_query_usd` (one agent invocation) and
`max_spend_per_day_usd` (rolling UTC day). Both nullable → unlimited.

Daily spend is *computed* from the append-only `x402_payment_ledger` (SUM of
settled amounts since UTC midnight), not tracked in a counter — no reset job,
no races, and the ledger doubles as the audit trail. Per-query enforcement is
belt-and-suspenders: we pre-check here AND set the PaymentSession maxSpendAmount
so AWS rejects an over-cap payment in flight even if our estimate was low.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from api import db
from src.payments.config import PaymentsConfig, get_payments_config


class QuotaError(Exception):
    """Raised when a payment would breach a per-query or per-day cap."""

    def __init__(self, scope: str, limit_usd: float, attempted_usd: float | None) -> None:
        self.scope = scope  # 'per_query' | 'per_day'
        self.limit_usd = limit_usd
        self.attempted_usd = attempted_usd
        super().__init__(
            f"{scope} cap of ${limit_usd:.4f} would be exceeded"
            + (f" by this ${attempted_usd:.4f} charge" if attempted_usd is not None else "")
        )


def _utc_day_start_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


def get_quotas(user_id: str, cfg: PaymentsConfig | None = None) -> dict[str, Any]:
    """Effective quotas for a user, falling back to config defaults when unset."""
    cfg = cfg or get_payments_config()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM user_payment_quotas WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return dict(row)
    return {
        "user_id": user_id,
        "max_spend_per_query_usd": cfg.default_max_per_query_usd,
        "max_spend_per_day_usd": cfg.default_max_per_day_usd,
        "x402_network": cfg.default_x402_network,
        "updated_at": None,
    }


def set_quotas(
    user_id: str,
    *,
    max_spend_per_query_usd: float | None,
    max_spend_per_day_usd: float | None,
    x402_network: str | None = None,
    cfg: PaymentsConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or get_payments_config()
    network = x402_network or cfg.default_x402_network
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_payment_quotas (
                user_id, max_spend_per_query_usd, max_spend_per_day_usd, x402_network
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                max_spend_per_query_usd = excluded.max_spend_per_query_usd,
                max_spend_per_day_usd   = excluded.max_spend_per_day_usd,
                x402_network            = excluded.x402_network,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            """,
            (user_id, max_spend_per_query_usd, max_spend_per_day_usd, network),
        )
        conn.commit()
    return get_quotas(user_id, cfg)


def spent_today_usd(user_id: str) -> float:
    """Sum of settled x402 spend since UTC midnight."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_usd), 0) AS total
            FROM x402_payment_ledger
            WHERE user_id = ? AND status = 'settled' AND created_at >= ?
            """,
            (user_id, _utc_day_start_iso()),
        ).fetchone()
    return float(row["total"] or 0.0)


def spent_in_invocation_usd(invocation_id: str) -> float:
    """Sum of settled spend already recorded for one agent invocation."""
    with db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_usd), 0) AS total
            FROM x402_payment_ledger
            WHERE invocation_id = ? AND status = 'settled'
            """,
            (invocation_id,),
        ).fetchone()
    return float(row["total"] or 0.0)


def record_payment(
    *,
    user_id: str,
    invocation_id: str,
    resource_url: str,
    amount_usd: float,
    status: str,
    detail: str | None = None,
    payment_session_id: str | None = None,
    payment_instrument_id: str | None = None,
    x402_network: str | None = None,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO x402_payment_ledger (
                user_id, invocation_id, resource_url, amount_usd, status, detail,
                payment_session_id, payment_instrument_id, x402_network
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                invocation_id,
                resource_url,
                amount_usd,
                status,
                detail,
                payment_session_id,
                payment_instrument_id,
                x402_network,
            ),
        )
        conn.commit()


def recent_ledger(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, invocation_id, resource_url, amount_usd, status, detail,
                   x402_network, created_at
            FROM x402_payment_ledger
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]

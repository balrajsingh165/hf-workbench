#!/usr/bin/env python3
"""End-to-end smoke test for the AgentCore Payments integration.

Exercises the core logic directly (no HTTP server needed): ensures a test user,
provisions their embedded wallet, sets quotas, and pays for a real x402
resource on Base Sepolia.

Run where the `payments-admin` AWS profile is configured (the SG machine):
    uv run python -m scripts.smoke_payments
    uv run python -m scripts.smoke_payments --resource https://mesh.heurist.xyz/x402/base-sepolia/agents/<AgentId>/<tool>

Prerequisites:
  * Schema applied:  uv run python -c "from db.schema import init_db; \
        init_db(tables=['user_wallets','user_payment_quotas','x402_payment_ledger'])"
  * The user's wallet funded with Base Sepolia USDC (faucet: https://faucet.circle.com/)
    AND the WalletHub delegated-signing grant completed (open the redirect_url
    printed below once). The first run typically stops at "pending_grant" — fund
    + grant, then re-run.
"""

from __future__ import annotations

import argparse
import json
import sys

from api import db
from src.payments import quotas, wallets
from src.payments.config import get_payments_config
from src.payments.service import pay_for_resource

TEST_USER = "payments-smoke-user"
# A Base Sepolia x402 resource. Override with --resource for a known-priced tool.
DEFAULT_RESOURCE = "https://mesh.heurist.xyz/x402/base-sepolia/agents"


def ensure_user(user_id: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO users (id, display_name) VALUES (?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (user_id, "Payments Smoke Test"),
        )
        conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default=TEST_USER)
    parser.add_argument("--resource", default=DEFAULT_RESOURCE)
    parser.add_argument("--method", default="POST")
    parser.add_argument("--per-query", type=float, default=0.50)
    parser.add_argument("--per-day", type=float, default=2.00)
    args = parser.parse_args()

    cfg = get_payments_config()
    if not cfg.enabled:
        print("AgentCore Payments disabled (AGENTPAY_MANAGER_ARN blank).")
        return 1
    print(f"Region={cfg.aws_region} profile={cfg.aws_profile} network={cfg.default_x402_network}")

    ensure_user(args.user)

    print("\n[1/4] Provisioning wallet ...")
    wallet = wallets.get_or_provision(args.user)
    print(json.dumps(wallet, indent=2, default=str))
    if wallet.get("status") != "active":
        print(
            "\n⚠️  Wallet needs the delegated-signing grant + funding before payments work:\n"
            f"    1. Open: {wallet.get('redirect_url')}\n"
            f"    2. Fund {wallet.get('wallet_address')} with Base Sepolia USDC: https://faucet.circle.com/\n"
            "    Then re-run this script."
        )

    print("\n[2/4] Setting quotas ...")
    q = quotas.set_quotas(
        args.user,
        max_spend_per_query_usd=args.per_query,
        max_spend_per_day_usd=args.per_day,
    )
    print(json.dumps(q, indent=2, default=str))

    print(f"\n[3/4] Paying for resource: {args.resource}")
    result = pay_for_resource(args.user, args.resource, method=args.method, body={})
    print(json.dumps(result, indent=2, default=str))

    print("\n[4/4] Spending after payment:")
    print(f"  spent_today_usd = {quotas.spent_today_usd(args.user):.6f}")
    for row in quotas.recent_ledger(args.user, limit=5):
        print(f"  - {row['created_at']} {row['status']:18s} ${row['amount_usd']:.6f} {row['resource_url']}")

    return 0 if result.get("status") in {"settled", "blocked_quota"} else 2


if __name__ == "__main__":
    sys.exit(main())

# Design: AgentCore Payments (x402) — per-user wallets + spend quotas

Status: Draft · 2026-06-08

Lets each hf-workbench user pay for x402 resources from their own funds, capped
by per-query and per-day spend limits, using **AWS Bedrock AgentCore Payments**
+ **CDP Embedded Wallets**. This is the *buy* side of agentic payments (our
agents paying others), distinct from the AWS Marketplace seller listing (the
*sell* side).

The payment mechanics are ported from the working prototype at
`agentcore-payments-beta-main/heurist_finance_agent`, generalizing its single
shared wallet + session into one wallet per user and a fresh session per query.

## How the pieces map

| Requirement | AgentCore primitive | Where |
|---|---|---|
| One wallet per user | `create_payment_instrument` (EMBEDDED_CRYPTO_WALLET, per `userId`) | `wallets.py` |
| Max spend per query | `PaymentSession.maxSpendAmount` — a fresh session per invocation, AWS-enforced | `service.py` |
| Max spend per day | Computed from the append-only ledger (SUM since UTC midnight); pre-checked | `quotas.py` |
| Pay an x402 resource | `process_payment` (CRYPTO_X402) inside the 402→proof→retry interceptor | `agentcore.py` |

Two IAM roles (already created by the prototype's `setup_roles.sh`) are assumed
at runtime: **ManagementRole** (instruments + sessions + balance) and
**ProcessPaymentRole** (process_payment only). Permanent resources live in
account `441070252417`: manager `mypaymentmanager-ysmz9kzgdx`, connector
`mycoinbaseconnector-s8a2swf9ic`.

## Decisions

- **Quota semantics:** daily window is the **UTC calendar day**, derived from the
  ledger (no reset job, no races); breaches **hard-block** before any payment is
  signed, with the session `maxSpendAmount` as an in-flight AWS backstop.
- **Wallet lifecycle:** **lazy + idempotent** — provisioned on first wallet/pay
  call, reused thereafter; `POST /wallet` lets a user pre-provision.
- **Network:** default **Base Sepolia** (testnet), configurable via
  `AGENTPAY_X402_NETWORK`. The embedded wallet itself is one ETHEREUM-family
  address usable across Base mainnet + Sepolia.

## Delegated-signing grant (important)

A freshly created embedded wallet is `pending_grant`: the user must open its
`redirect_url` (Coinbase WalletHub) once and grant the agent permission, and the
wallet must hold USDC, before `process_payment` succeeds. The wallet API surfaces
`redirect_url` + `grant_required` for exactly this.

## Schema (db/schema.py)

`user_wallets` (1:1 user→instrument), `user_payment_quotas` (per-query / per-day
caps), `x402_payment_ledger` (append-only audit + daily-spend source). Apply
additively without wiping other tables:

```
uv run python -c "from db.schema import init_db; \
  init_db(tables=['user_wallets','user_payment_quotas','x402_payment_ledger'])"
```

## Dev API (`src/interfaces/payments/api.py`)

```
POST /api/v1/payments/users/{user_id}/wallet     provision (idempotent)
GET  /api/v1/payments/users/{user_id}/wallet     address + balance + grant URL
PUT  /api/v1/payments/users/{user_id}/quotas     set caps
GET  /api/v1/payments/users/{user_id}/quotas     read caps
GET  /api/v1/payments/users/{user_id}/spending   today's spend + ledger
POST /api/v1/payments/users/{user_id}/pay        pay one x402 resource
```

Smoke test: `uv run python -m scripts.smoke_payments` (see the script header).

## Not yet done (next phases)

- Wire `pay_for_resource` into the agent orchestrator so paid tools draw from the
  acting user's wallet under one shared `invocation_id` per turn.
- Auth (the whole app currently trusts the caller-supplied `user_id`).
- Real funding/onboarding UX for the WalletHub grant.
- STS credential refresh for long-lived processes (clients cached ~1h today).

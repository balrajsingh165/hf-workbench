"""AgentCore Payments (x402) integration for hf-workbench.

Per-user CDP Embedded Wallets + per-query / per-day spend quotas, layered on
top of AWS Bedrock AgentCore Payments. See docs/design-agentcore-payments.md.

The payment mechanics (402 → ProcessPayment → retry) are ported from the
reference prototype in agentcore-payments-beta-main/heurist_finance_agent, with
the single shared wallet/session generalized to one wallet per user and a fresh
session (carrying the per-query cap) per agent invocation.
"""

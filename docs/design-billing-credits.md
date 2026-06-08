# Design: Credits + Billing

**Status:** Draft · 2026-05-05
**Owner:** TBD
**Prerequisites:** Masterplan items **5.15** (Langfuse port), **5.16** (`agent_usage` persistence), **5.17** (model pricing). Without these, none of this design can ship — the unit-of-work is the per-request usage row.

---

## 1. Goals

- Give every user a predictable monthly AI budget without per-request anxiety.
- Bill overage in cash, only when the user has agreed to it.
- Keep the abstraction simple enough that a user who has never thought about LLM tokens can understand their balance.
- Keep the implementation simple enough that one engineer can hold it in their head: SQLite tables, a deduction hook in the orchestrator, a Stripe webhook for overage.

**Non-goals (for v1):**
- Per-feature pricing tiers, promotional credits, referral bonuses, gift cards.
- Multi-currency. USD only.
- Real-time hard cutoff at the credit line — see "Soft cutoff" below.
- Team/org billing. Single user accounts only.

---

## 2. The credit

**Definition:** 1 credit = $0.01 (one US cent).

That is the entire abstraction. The conversion rate is fixed and global. We do not vary credit value per user, per cohort, or per promotion. If we ever want to give someone "20% off," we discount the cash price of a credit pack — never the credit-to-cost ratio inside the system.

**Why this rate:** at $0.01 per credit, typical numbers stay readable:
- A single chat turn (~3k input + 1k output on Claude Sonnet 4.6 at current Bedrock prices) costs roughly **$0.02–$0.05**, i.e. **2–5 credits**. Users see "you used 4 credits" and it scans.
- A research-heavy multi-tool turn might cost **15–30 credits**.
- A monthly budget in the **2,000–5,000 credit** range ($20–$50 of cost) is a recognizable subscription number.

**Why not 1 credit = $1 or $0.001:** at $1 a single chat turn is "0.04 credits" (unreadable as fraction), at $0.001 a single turn is "20–50 credits" but a month is "5,000–50,000 credits" (too big to feel meaningful).

---

## 3. Cost → credit conversion

Every persisted `agent_usage` row carries a `cost_usd` (computed from the model pricing table in masterplan **5.17**). Credits charged to the user are:

```
credits_charged = ceil(cost_usd × 100 × markup)
```

Where `markup` is a global multiplier (config: `HF_CREDIT_MARKUP`, default `1.5`). The markup absorbs:
- Rounding granularity (we always round up to the next whole credit).
- Non-LLM costs the user benefits from but we don't bill separately (price API calls, news ingest, embeddings).
- A safety margin against Bedrock price changes between the pricing-table update and the next deploy.

A 1.5× markup means a $0.04 chat costs the user 6 credits, not 4. Whether 1.5× is the right number is a business question — surface the lever, don't bake it in.

**Always round up.** Floor or banker's rounding creates a free-tokens edge case at low-cost requests.

---

## 4. Allocation model

### 4.1 Monthly grant
Each user gets `HF_MONTHLY_CREDIT_GRANT` credits (default: **3,000** = $30 cost-equivalent at our markup). The grant is tied to a billing cycle anchored on the user's `subscription_start_at` (not calendar month — avoids end-of-month spikes and lets us hand out grants on signup day).

### 4.2 Reset semantics
**Credits do not roll over.** At the start of each billing cycle, the user's balance resets to `HF_MONTHLY_CREDIT_GRANT`. Unused credits are forfeited.

Rationale: rollover creates an incentive to hoard credits for "the big query" and discourages everyday use, which is the opposite of what a thesis-tracking product wants. If users complain, the right answer is to *raise the grant*, not enable rollover.

### 4.3 Free tier
v1 is single-tier — every active user gets the same grant. Tiered pricing is a v2 question once we have signal on what "heavy use" actually costs.

---

## 5. Spend surfaces

For v1, **only AI inference deducts credits.** Everything else is free at the API level.

| Surface | Deducts? | How |
|---|---|---|
| AI SDK chat (`POST /api/v1/ai-sdk/chat/completions`) | ✅ | Sum of `agent_usage` rows for that `request_id` × markup |
| Sharpen-thesis chip (when shipped, masterplan 2.1) | ✅ | Same path — it's just another orchestrator endpoint |
| Daily digest generation (masterplan 0.2) | ❌ (v1) | We pay for it; it's a product surface, not a user request |
| News ingest, scoring, brief synthesis | ❌ | Backend cost, not user-attributable |
| Chart agent (phase2b, `chart.py`) | ✅ | Folded into the chat turn it ran inside |
| `/api/home`, `/api/news`, etc. (read-only) | ❌ | No LLM cost |

The principle: **the user pays for inference triggered by their direct action.** Background jobs we run on their behalf (digest, scoring, ingest) are operational cost.

---

## 6. Soft cutoff + overage

### 6.1 The soft cutoff
We do **not** block a chat mid-turn when the user crosses zero. The streaming SSE response from `orchestrator.py` is half-finished by the time we know the cost — killing it leaves the user with a useless half-answer they were already promised.

Instead:
- Before each chat turn, check `users.credit_balance`. If `> 0`, allow. If `<= 0`, behavior depends on overage status (below).
- After the turn completes, deduct the full credit cost. The balance can go negative.

### 6.2 No-overage users (default)
Users without a payment method on file get a hard cutoff *before* the request, but no mid-turn cutoff:

- `credit_balance > 0` → request allowed; may go negative on completion. This is the small cap we explicitly accept to avoid mid-stream cuts.
- `credit_balance <= 0` → request rejected with HTTP 400 and an error code (`out_of_credits`). UI shows "out of credits, refills on YYYY-MM-DD" or a "Add payment method to continue" CTA. We use 400 (not 402) because 402 is reserved/inconsistently handled in browsers and clients; the discriminator is the error code in the body, not the status.

The "may go slightly negative" leak is fine: it's bounded by the cost of one chat turn (rarely > 30¢) and resets each cycle.

### 6.3 Overage-enabled users
Users who have explicitly enabled overage (added a card and toggled "allow paid overage"):

- Requests are allowed regardless of balance.
- Each cycle's cumulative negative balance becomes their overage bill.
- At cycle close: if `balance < 0`, charge `abs(balance) × $0.01` to their saved payment method via Stripe.
- If the charge fails: revert the user to no-overage mode, send an email, do not retry automatically.

### 6.4 Spend cap (safety)
Even with overage on, hard cap each cycle at `HF_OVERAGE_CAP_CREDITS` (default: **10× the monthly grant**, i.e. ~$300 cost-equivalent). Beyond that, return 400 with error code `overage_cap_hit` until cycle reset. Protects users from a runaway client; protects us from being on the hook for a $5,000 mistake.

---

## 7. Schema additions

```sql
-- Added to db/schema.py TABLES dict.

users_billing(
  user_id              TEXT PRIMARY KEY REFERENCES users(id),
  credit_balance       INTEGER NOT NULL DEFAULT 0,        -- can go negative
  monthly_grant        INTEGER NOT NULL,                  -- normally HF_MONTHLY_CREDIT_GRANT
  cycle_start_at       TEXT NOT NULL,                     -- ISO date; anchors reset
  cycle_overage_cap    INTEGER NOT NULL,                  -- HF_OVERAGE_CAP_CREDITS
  overage_enabled      INTEGER NOT NULL DEFAULT 0,        -- 0 or 1
  stripe_customer_id   TEXT,                              -- nullable; set when card added
  payment_status       TEXT NOT NULL DEFAULT 'ok',        -- 'ok' | 'failed' | 'disabled'
  updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
)

credit_ledger(
  -- Append-only audit log. Source of truth for "what did the user pay/get?"
  -- users_billing.credit_balance is a denormalized cache of SUM(delta) over this table.
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       TEXT NOT NULL REFERENCES users(id),
  delta         INTEGER NOT NULL,           -- positive (grant/topup) or negative (charge)
  reason        TEXT NOT NULL,              -- 'monthly_grant' | 'agent_usage' | 'overage_topup' | 'manual_adjustment' | 'cycle_reset_forfeit'
  request_id    TEXT,                       -- agent_usage.request_id when reason='agent_usage'
  cost_usd      REAL,                       -- snapshot for audit
  note          TEXT,                       -- free-form for manual adjustments
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
)
-- Index: (user_id, created_at)

billing_cycles(
  -- One row per (user, cycle). Closed at end-of-cycle by a background job.
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id           TEXT NOT NULL REFERENCES users(id),
  cycle_start_at    TEXT NOT NULL,
  cycle_end_at      TEXT NOT NULL,
  starting_balance  INTEGER NOT NULL,
  ending_balance    INTEGER NOT NULL,                    -- pre-reset; can be negative
  overage_credits   INTEGER NOT NULL DEFAULT 0,          -- abs(ending_balance) when negative
  overage_charged_usd REAL,                              -- nullable until Stripe completes
  stripe_charge_id  TEXT,
  status            TEXT NOT NULL DEFAULT 'open',        -- 'open' | 'closed' | 'overage_pending' | 'overage_failed'
  closed_at         TEXT
)
-- Unique: (user_id, cycle_start_at)
```

The ledger is **the source of truth.** `users_billing.credit_balance` is a denormalized cache, recomputable as `SELECT SUM(delta) FROM credit_ledger WHERE user_id = ?`. Whenever there's a discrepancy, trust the ledger and rebuild the cache.

---

## 8. Hooks

### 8.1 Pre-request check
In the orchestrator entry path (whatever routes `/api/v1/ai-sdk/chat/completions`), before kicking off research:

```python
billing = get_billing_row(user_id)
if billing.credit_balance <= 0 and not billing.overage_enabled:
    return 400, {"error": "out_of_credits", "refills_at": billing.cycle_start_at + 1mo}
if billing.overage_enabled and cycle_overage_credits >= billing.cycle_overage_cap:
    return 400, {"error": "overage_cap_hit"}
```

### 8.2 Post-request deduction
In `orchestrator.py` after the run completes, in the same place that today emits the SSE `event_result` and (per masterplan 5.16) writes `agent_usage`:

```python
total_cost_usd = sum(row.cost_usd for row in agent_usage_rows)
credits = math.ceil(total_cost_usd * 100 * HF_CREDIT_MARKUP)
deduct_credits(user_id, credits, reason="agent_usage", request_id=request_id, cost_usd=total_cost_usd)
```

`deduct_credits` is two writes in one transaction: insert into `credit_ledger`, decrement `users_billing.credit_balance`. SQLite's default isolation is fine — we're single-writer per user.

### 8.3 Cycle close (background)
A daily cron job picks up rows where `users_billing.cycle_start_at + 1 month <= now()`:

1. Compute `ending_balance` from the ledger.
2. Insert a `billing_cycles` row with `status='open'`.
3. If `overage_enabled` and `ending_balance < 0`: charge Stripe; update `billing_cycles.status` and `users_billing.payment_status` based on outcome.
4. Insert a `credit_ledger` row with `reason='cycle_reset_forfeit'` setting balance back to `monthly_grant`.
5. Bump `cycle_start_at` forward.

---

## 9. UX surfaces

Out of scope for this doc beyond the contract — frontend will own the rendering. The backend exposes:

- `GET /api/v1/billing/me` → `{credit_balance, monthly_grant, cycle_start_at, cycle_end_at, overage_enabled, recent_charges: [...]}`
- `POST /api/v1/billing/overage` → `{enabled: true|false}`
- `POST /api/v1/billing/payment-method` → Stripe Setup Intent flow (defer to Stripe's hosted UI; do not handle card details ourselves)

UI hooks the frontend will need:
- A persistent balance pill in the chat header.
- A toast on the response showing "this turn cost X credits" (only when the user has the dev/transparency setting on, otherwise silent — most users don't want a charge notification on every message).
- A handler for 400 responses with `error_code in {out_of_credits, overage_cap_hit}` that shows a modal: either "you're out, refills X" or "add card to continue."

---

## 10. Stripe integration

Out of scope for v1 of this design beyond noting:
- Use Stripe Checkout / Setup Intents — never store card details ourselves.
- One Stripe Customer per HF user; ID stored in `users_billing.stripe_customer_id`.
- Overage charges are PaymentIntents created server-side at cycle close.
- Webhook handler for `payment_intent.succeeded` / `payment_intent.payment_failed` updates `billing_cycles.status` and `users_billing.payment_status`.
- All of this is dwarfed by the work of getting the credit accounting right; it's the boring last 10%.

---

## 11. Phasing

**Phase B0 — Telemetry foundation** (blocks everything below; lives in masterplan 5.15–5.18)
- Langfuse port, `agent_usage` table, model pricing, metrics CLI.

**Phase B1 — Credit accounting (no money)**
- Schema (`users_billing`, `credit_ledger`, `billing_cycles`).
- Pre-request check + post-request deduction in `orchestrator.py`.
- Monthly grant on user creation; cycle close cron.
- `GET /api/v1/billing/me` endpoint.
- **No Stripe yet.** Hard cutoff at zero for everyone.

This phase alone is shippable internally — it gives us the "X credits per user per month" semantic and we can start tuning the grant/markup levers against real usage.

**Phase B2 — Overage billing (Stripe)**
- Stripe Customer + Setup Intent flow.
- `POST /api/v1/billing/overage`, `POST /api/v1/billing/payment-method`.
- Cycle-close charge logic.
- Webhook handler.
- 400 + modal UI hooks keyed by `error_code`.

**Phase B3 — Polish**
- Per-turn cost transparency toggle.
- Email notifications (low balance, overage charged, charge failed).
- Admin tooling: manual ledger adjustments via `scripts/hf_billing_admin.py`.

---

## 12. Open questions

1. **Markup value.** 1.5× is a guess. We should run B1 with markup recorded but billing dormant for two weeks to see what real usage looks like, then set the production markup based on actual cost/turn distribution.
2. **Grant value.** 3,000 credits/mo is a guess. Same approach — measure first.
3. **Anchored vs. calendar cycles.** Anchored (per-user signup day) is operationally better but harder to forecast. Calendar (1st of month) is simpler for ops and accounting. Decide before B1 ships; don't migrate later.
4. **Refund policy on errored requests.** Today an `agent_usage` row with `status='error'` would still deduct credits. Probably we should not charge for our own errors. Easy fix: in the post-request hook, skip deduction when `status='error'`. But what about partial-error (research succeeded, response failed)? Punt to B1 implementation — answer becomes obvious once we see real error modes.
5. **Trial period.** Should new users get a 7-day "all you can eat" before the grant kicks in? Probably no — the grant *is* the trial. But marketing may have a view.
6. **Team accounts.** Out of scope for v1; flagging because the schema decision (one user → one billing row) constrains future team support. If we expect teams in <6 months, refactor `users_billing` keys to a `billing_account_id` now.

---

## 13. What this doc explicitly does not cover

- **Tax handling.** Stripe Tax can take this; engage it when we cross the threshold that requires it.
- **Refunds for legitimate complaints.** Manual ledger adjustment via admin script. Not automated.
- **Promo codes / referral credits.** Future.
- **Annual plans / discounts.** Future.
- **Enterprise billing (invoice, NET-30).** Future.

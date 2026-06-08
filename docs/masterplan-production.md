# Masterplan: Production Readiness

**Last updated:** 2026-05-05 (added observability v2 + credits prerequisite)
**Status key:** ✅ Done · 🔶 Partial / needs work · ⬜ Not started

Note: user-based personalization system is under-specified. We need to work on a spike to scope it out before working on any personalization.

Note: deprioritize authentication because we want fast testing for devs at this stage.

Next priority: 1.7, 3.1/3.5 do before any prompt tuning. Observability (5.15–5.18) is a hard prerequisite for the billing/credits system — see `docs/design-billing-credits.md`.

Single source of truth for what ships before Heurist Finance is production-ready. Links to the design doc or code that owns each item. Update status markers as things land.

---

## What "production-ready" means here

1. A user can create a thesis, come back the next day, and read a digest that references it with at least one non-obvious stress signal. (Tier 1 moments 1–3)
2. All product surfaces in `docs/mock-ux-walkthrough.md` are backed by real API endpoints.
3. Every pipeline stage has a quality gate we can actually run.
4. The pipeline runs unattended without silent data corruption.
5. No secrets in the repo, CORS locked down, user_id is validated.

---

## Phase 0 — Close the vertical slice (demo milestone)

The internal demo requires Days 4–5 of `docs/plan-p0-p2-slice.md`. Two days and one data-integrity fix.

| # | Item | Status |
|---|---|---|
| 0.1 | **Wire `match_thesis_for_story` writes** — `agents/match_thesis_for_story.py` now upserts `source='ingest'` rows to `thesis_story_links` by default (mirroring `match_story_for_thesis`'s persist contract). `--dry-run` flag preserves the JSON-only behavior for ad-hoc inspection. The legacy `agents.ingest_news` writer was removed; story creation now flows through `agents.route_news_clusters`. | ✅ |
| 0.2 | **`agents/daily_digest.py`** — personalized daily digest. Sections: convictions today (Strength + one-line what moved it), under stress, fresh support, orphan news ("what conviction am I missing?"). Writes `users/{id}/digests/YYYY-MM-DD.md`. Tone: declarative, no hedging. | ⬜ |
| 0.3 | **Demo + retro** — run the full pipeline on real seeded users, write `docs/slice-retro.md` naming the 3 biggest quality gaps. These gaps drive Phase 2 priorities. | ⬜ |

---

## Phase 1 — API completeness

**Status:** 🔶 **TO BE SPECIFIED.** Every button visible in `docs/mock-ux-walkthrough.md` needs a backend endpoint and the frontend is blocked on these — but the per-endpoint contracts (request/response shapes, error semantics, optimistic-update affordances) are gated on UI/UX design that is still in flight. Do not start implementation against the previous detailed table; it has been retired so we don't ship to a stale spec.

What the layer must cover, generically:

| Surface area | What's needed | Status |
|---|---|---|
| **Thesis write endpoints** | Create / update / close / adopt / unadopt for theses, including the background work each triggers (backfill, re-embed, scoring, resolution ceremony hand-off). | ⬜ TBS |
| **News endpoints** | Paginated per-user news feed and per-article detail with thesis-chip payloads. Today the only path is a 200-row embed inside `/api/home` (`api.py::HOME_NEWS_LIMIT`) — load-bearing once firehose volume grows. | ⬜ TBS |
| **Thesis history / time-series** | Read endpoints that surface `thesis_snapshots` (and any other accumulating per-thesis time-series) for score-history charts. | ⬜ TBS |
| **Per-article computed fields** | `suggested_thesis` payload on unmatched news cards across `/api/home` and the future news endpoints. `build_discover` exists but is only wired into the top-zone recommendations today. | 🔶 partial |
| **Prescription line on existing endpoints** | "Holding. Let it run." / "Watch." / "Review or restate." on `/api/home` thesis cards and `/api/thesis/{id}`. | ✅ shipped (`src/thesis/scoring.py`) |

**Unblocking sequence.**
1. UI/UX lands the v1 design for the thesis lifecycle (create, adopt, restate, close) and for the news feed/detail views.
2. Spec out the endpoint table here (or in a dedicated `docs/design-api-phase1.md`) with concrete request/response schemas, including the multi-thesis attachment contract, error codes, and which endpoints are auth-gated.
3. Re-open this section with per-endpoint rows and assign owners.

Until step 1 closes, do not pre-build endpoints against guessed shapes — frontend is blocked on the design, not on the implementation.

---

## Phase 2 — Core product loop

All the flows a user runs through on Day 1 and every day after.

### 2A — Thesis creation (Tier 1 moment #1)

| # | Item | Status | Notes |
|---|---|---|---|
| 2.1 | **`sharpen-thesis` Sage chip** — add `ChipId` value, chip prompt in `prompt_manager.py`, Strands tool wrapping `discover_thesis` write path with `origin='chat'`, `review_status='active'` | ⬜ | See `docs/design-thesis-creation.md` Caller #2. The user types a fuzzy belief; the agent challenges for tickers and 2–3 concrete invalidations (horizon is inferred silently onto `theses.horizon_days`, never asked); on confirmation writes the thesis and runs post-creation pipeline. |
| 2.2 | **Instant backfill confirmation** — after `sharpen-thesis` completion, respond with signal count found | ⬜ | "Created. Found 2 supporting signals from the past week and 0 stresses." Requires 2.1 + 0.1 both wired. |
| 2.3 | **`short_belief` field** — ≤18-word declarative line, written at creation, stored as `- **Short Belief**: <line>` in thesis markdown header, parsed into `ThesisDocument` | ⬜ | See `TODO.md → "short_belief on theses"`. Backfill 12 seeded theses with a script. |

### 2B — Thesis lifecycle management

| # | Item | Status | Notes |
|---|---|---|---|
| 2.5 | **Update/Restate flow** — `PATCH /api/thesis/{id}` (1.4) + a Sage chip variant that reads the current thesis + stress trigger, proposes 3 options (restate/close-partial/hold), and executes on user choice | ⬜ | See mock UX §4 "Updating a stressed thesis". The agent offers structured options, not a free-form chat. |
| 2.6 | **Close flow** — `POST /api/thesis/{id}/close` (1.5) with outcome enum; resolution ceremony narrative (deferred to Phase 6 for the full treatment, but the DB write + basic confirmation must ship here) | ⬜ |  |
| 2.7 | **Auto stress-flip** — write `user_theses.status='stressed'` when `confidence >= STRESS_FLIP_CONF` AND verified invalidation match. Blocked on calibration (item 3.4). | ⬜ | See `TODO.md → "Auto stress-flip (deferred)"`. Do not enable until 3.4 is done. |

### 2C — Scoring completeness

| # | Item | Status | Notes |
|---|---|---|---|
| 2.9 | **Freshness decay correctness** — verify the half-life formula in `src/thesis/scoring.py`. Theses with 14+ days of silence on a 4-week horizon must score ≤ 30. | 🔶 | Freshness shipped but never formally spot-checked. |
| 2.10 | **Tailwind null-handling contract** — when every ticker fails price lookup, `score_tailwind = NULL` and composite = freshness alone. Verify this is what ships, not a zero or a crash. | 🔶 | Specified in the scoring plan; verify the actual path in `agents/score_theses.py`. |

### 2D — Data integrity

| # | Item | Status | Notes |
|---|---|---|---|
| 2.11 | **Ticker registry validation at ingest** — `instrument_exists()` check before every INSERT to `entity_tickers` in `src/news/persist.py:114`; drop + log unknown symbols | ✅ | Sharp-lane synthesizer emissions now drop registry-unknowns and upsert them into `pending_instruments` with `source='ingest'` (mirrors the firehose treatment from 4.5a, but drops rather than preserves since LLM-emitted symbols are lower-trust). |
| 2.13 | **Auto-persist `match_thesis_for_story` with `source='ingest'`** (same as 0.1) | ✅ | Shipped with 0.1 — see `agents/match_thesis_for_story.py`. |

---

## Phase 3 — Quality gates

Without quality gates there is no way to know whether a prompt change broke matching, whether scores are believable, or whether the digest is actually readable. The per-gate definitions, pass criteria, triggers, and required qualitative-read protocols now live in their own doc:

➡️ **See [`docs/quality-gates.md`](quality-gates.md).**

That doc is the single source of truth for: matching regression (3.1), synthesis faithfulness (3.2), ticker extraction precision (3.3), confidence calibration (3.4), discovery rejection rate (3.5), score reasonableness (3.6), digest cold-read (3.7), stress-flip precision (3.8), end-to-end smoke (3.9), freshness decay calibration (3.10), and LLM cost-per-run tracking (3.11). Status of each gate is tracked there.

The companion runbook for daily reads against the same data is [`docs/daily-backend-health-review.md`](daily-backend-health-review.md). The two docs intentionally overlap on the qualitative-read sections — health review is "is today healthy", quality gates is "did this prompt/model change regress us".

---

## Phase 4 — News at scale

Target: 2k stories/day. Prerequisite for the digest having dense-enough signal.

| # | Item | Status | Notes |
|---|---|---|---|
| 4.1 | **Smoke test press wire quality** — run `scripts/smoke_press_wires.py` against Business Wire, PR Newswire, GlobeNewswire; measure items/hour, pass rate through ticker gate, alias gaps | ✅ | May 2026 baseline: 40.1% pass / 4.0% spam / ~308/day. Business Wire RSS is dead and ACCESS is Cloudflare-blocked — not used. |
| 4.2 | **Ticker-tagging gate** — pure-Python NER using `instruments.aliases_json` on headline + first 200 chars; pass-through if ≥1 instrument tagged OR ≥1 macro keyword (Fed, CPI, payrolls, GDP, inflation, etc.) | ✅ | Lives in `src/news/firehose_gate.py`. Three tiers: T1 exchange regex (carries 91% of passes), T2 alias index, T3 macro keywords. Shared between smoke script and production agent. |
| 4.3 | **News firehose schema** | ✅ | Plan was revised mid-flight — instead of `news_compact` we added 4 columns to `news` (`headline`, `body_excerpt`, `source_url`, `publisher`) + `UNIQUE INDEX ON source_url WHERE source_url IS NOT NULL`. Both lanes share the table; `headline IS NULL` is the discriminator. |
| 4.4 | **`agents/firehose.py`** — multi-source RSS poller → ticker-tagging gate → firehose lane in `news` | ✅ | 20 press-wire feeds (PR Newswire industry + GlobeNewswire subject). Idempotent dedup via `source_url`. Class-action releases marked `publisher='*-classaction'`. Wired into `pipeline_scheduler` at 10-min cadence. |
| 4.5 | **Union sharp + firehose lanes in `build_news`** — firehose items appear in the feed; display-ticker filter applied to both | ✅ | `api.py::build_news` branches on `headline IS NULL` for body source. Display filter drops registry-unknown chips so firehose-only symbols don't render until adopted. |
| 4.5a | **`pending_instruments` queue + registry-unknown logging** (split out of 2.11/2.12 for the firehose path) | ✅ | New `pending_instruments` table; `firehose.insert_entry` logs unmapped tags with `source='firehose'`. Backfilled from existing rows — 103 distinct candidates ready for weekly review. |
| 4.5b | **Firehose metrics emitted to `logs/hf-pipeline-metrics.jsonl`** | ✅ | `firehose_run` event with feeds_polled/raw/dropped/dup/inserted/spam/unknown. Both scheduler and CLI write to the same stream so 5.8 alerting sees firehose. |
| 4.5c | **Brief input excludes firehose rows** | ✅ | `src/brief/pipeline.py` filters on `headline IS NULL`. Firehose is single-source unsynthesized noise; brief only consumes sharp clusters until Phase 3 promotion ships. |
| 4.6 | **LLM cost telemetry** — `llm_calls` table + token logging in `src/clients/gemini.py` | ✅ | Shipped 2026-05-25. `llm_calls` records Gemini prompt/output/thinking/cache/total tokens plus computed `cost_usd` for story synthesis. Historical rows before this change have token fields defaulted to 0. |
| 4.7 | **Aggregator expansion** — Yahoo Finance Latest News + per-ticker, MarketWatch RSS, Seeking Alpha | ⬜ | After press-wire firehose is stable. Targets the +1k/day gap to hit 2k. |
| 4.8 | **Validate dedup scaling** (R3) — benchmark O(N) cosine scan at 10k/30k/60k vectors; decide ANN cutover | ⬜ | Measure first; only act if latency crosses a threshold. |
| 4.9 | **Validate thesis-matching N×M** (R4) — measure per-story thesis-match cost; build ticker-overlap pre-filter if load-bearing | ⬜ | At 2k stories × 200 theses = 400k checks/day. Likely needs the inverted index. Measure before building. Note: raw firehose rows do not get `thesis_story_links`; only promoted stories are matched. |
| 4.10 | **Wikimedia image fallback** | ⬜ | When `og:image` scraping fails. Low priority; cosmetic. |
| 4.11 | **Translation dedup** — same release in 5 languages currently lands as 5 rows | ⬜ | Phase 3 with cluster aggregation. Cosmetic for soft launch. |
| 4.12 | **Sibling-promotion to sharp lane** — when a firehose cluster grows ≥3 publishers, promote to full synthesis | ⬜ | Phase 3, requires aggregator expansion (4.7) for cluster signal. |

---

## Phase 5 — Operational hardening

Without this, the system is a prototype. None of these are glamorous; all are required before public users.

### 5A — Auth + API security

| # | Item | Status | Notes |
|---|---|---|---|
| 5.1 | **Auth middleware** — `user_id` is currently a raw query param with no validation (`?user_id=user_1`). Every write endpoint (adopt, close, create, update) must validate the caller owns what they're writing to. | ⬜ | Minimum: a simple token-based auth or session header. Must be on before any real users. |
| 5.2 | **CORS tightening** — `allow_origins=["*"]` in `app.py` must be locked to the frontend origin before launch | ⬜ | One-line change; do not forget it. |
| 5.3 | **Rate limiting** — at minimum on the AI SDK chat endpoint and on the news feed | ⬜ | A simple per-user rate limit prevents runaway LLM cost from a misbehaving client or scraper. |

### 5C — Database

| # | Item | Status | Notes |
|---|---|---|---|
| 5.6 | **Non-destructive migration path** — `init_db` currently drops and recreates all tables. Before launch, schema changes must migrate existing data rather than wiping it. Minimum: a versioned migration script pattern (not full ORM — see `docs/sop-schema-change.md`). | ⬜ | The current `init_db(tables=[...])` partial approach is a start; document the safe procedure and enforce it. |
| 5.7 | **DB backup strategy** — at minimum a daily `sqlite3 .backup` to a separate path before the pipeline runs | ⬜ | One cron line. `global/stories/*.md` and `global/theses/*.md` are git-versioned; the DB is not. |

### 5D — Observability (v1: pipeline + errors)

| # | Item | Status | Notes |
|---|---|---|---|
| 5.8 | **Pipeline alerting** — notify (email or Slack) when any pipeline stage fails 2+ consecutive runs | 🔶 | Local collector shipped in `scripts/hf_health.py`; notification transport still missing. Alert plan: `docs/plan-production-alerting.md`. |
| 5.9 | **Error monitoring** — structured error capture beyond log files; at minimum a local error log that can be grepped for `ERROR` counts per day | 🔶 | Logs exist; structured alerting does not. |
| 5.10 | **Agent/Bedrock health check** — startup validation that Bedrock credentials are valid and the Strands pipeline can reach its model; surface clearly in server logs | 🔶 | `HF_AGENT_PROTOCOL_SMOKE=1` bypasses this entirely; the real path needs explicit validation. |

### 5F — Observability v2: agent traces + token economics

The current `src/agent/observability.py` is a no-op shim — every Langfuse / OTel hook is stubbed (lines 19–68), token metrics are extracted in-memory but discarded after the SSE event (`sse_emitter.py:75-91`), and `cost_usd` is hardcoded `0.0` (`orchestrator.py:87`). The full implementation already exists in `~/heurist-finance-backend/src/observability.py` (~600 LOC) and a self-hosted Langfuse server at `http://151.245.184.3:9956`. This section ports it forward and extends it with persistence + cost tracking, both of which are hard prerequisites for the credits/billing system.

| # | Item | Status | Notes |
|---|---|---|---|
| 5.15 | **Port Langfuse + OTel from old backend** — replace the no-op shim in `src/agent/observability.py` with the real implementation. Keep the same public surface (`request_trace_context`, `attach_langfuse_observation_io`, `setup_strands_telemetry`, `summarize_agent_metrics`). Wire env vars in `src/agent/config.py`: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` (default `http://151.245.184.3:9956`), `LANGFUSE_ENVIRONMENT` (`development`/`production`), `LANGFUSE_SERVICE_NAME` (`hf-workbench`). | ✅ | Shipped 2026-05-05. Direct port; `langfuse>=3.0.0` added to `pyproject.toml`. Init hooks called from `app.py`. No-env path is a clean no-op. |
| 5.16 | **Persist agent token usage** — add an `agent_usage` table; write one row per orchestrator run (research + response + chart phases summed, plus per-phase breakdown). Hook in `src/agent/orchestrator.py` after the run completes, before the final SSE `event_result`. | ✅ | Shipped 2026-05-05. Writer in `src/agent/usage_recorder.py`. Orchestrator now passes the real `cost_usd` into `event_result`. Chart phase usage was `{}` until a follow-up — now captured from the chart agent's metrics (2026-05-25); chart run stats also land in `code_interpreter_runs`. See `docs/agent-observability.md`. |
| 5.17 | **Model pricing table + cost calculation** — `src/agent/pricing.py` with a static dict mapping Bedrock model IDs to `{input_per_1m, output_per_1m, cache_read_per_1m, cache_write_per_1m}` USD prices. Function `compute_cost_usd(model_id, usage_dict) -> float`. Called from the orchestrator hook (5.16). Source of truth: AWS Bedrock pricing page; revisit quarterly. | ✅ | Shipped 2026-05-05. Haiku 4.5, Sonnet 4.5/4.6, Opus 4.7 pricing in dict. Substring match against `model_id` so version suffix bumps don't silently route to $0. |
| 5.18 | **Metrics CLI — `scripts/hf_metrics.py`** — read-only CLI that surfaces token + cost rollups. Subcommands: `today`, `user <id> [--days N]`, `model [--days N]`, `top-spenders [--days N]`, `endpoint <name> [--days N]`. Prints both a human table and (with `--json`) a machine-readable payload so Claude Code can pipe it into ad-hoc analytics. | ✅ | Shipped 2026-05-05. Subcommands `today`, `user`, `model`, `top-spenders`, `endpoint`, `request`, and `charts` (Code Interpreter run stats, added 2026-05-25). Both `--json` and ASCII-table output. |
| 5.19 | **Claude API agent path coverage** — if/when any phase migrates off Bedrock to the Anthropic SDK directly, the same `record_usage()` call must fire from there. Today everything is Bedrock + Strands, so this is a forward note, not work. | ⬜ | Cross-reference: 4.6 `llm_calls` covers the news/Gemini path. Decide whether to unify into one `agent_usage` table or keep two — they have different shapes (per-story vs. per-chat-turn) so probably keep separate. |
| 5.20 | **Langfuse dashboard URLs in PR descriptions** — operator convenience; document the trace URL pattern (`{base}/project/{id}/traces/{trace_id}`) so reviewers can click straight from a server log line to the trace. | ⬜ | Doc-only. Cheap. |

**`agent_usage` schema (proposed):**

```
agent_usage(
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id          TEXT NOT NULL,                    -- orchestrator request id, also the Langfuse trace id
  user_id             TEXT NOT NULL,
  session_id          TEXT,                             -- agent_sessions.id when present
  endpoint            TEXT NOT NULL,                    -- 'chat' | 'digest' | 'sharpen-thesis' | ...
  model_id            TEXT NOT NULL,
  phase               TEXT NOT NULL,                    -- 'research' | 'response' | 'chart' | 'aggregate'
  input_tokens        INTEGER NOT NULL DEFAULT 0,
  output_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
  cost_usd            REAL NOT NULL DEFAULT 0,
  latency_ms          INTEGER,
  status              TEXT NOT NULL DEFAULT 'ok',       -- 'ok' | 'error' | 'cancelled'
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
)
-- Indexes: (user_id, created_at), (created_at), (model_id, created_at)
```

One row per phase, joined by `request_id`. The aggregate ('aggregate' phase row) is denormalized for fast `SUM` queries — it's redundant with the per-phase rows but worth the storage.

### 5E — Test coverage

| # | Item | Status | Notes |
|---|---|---|---|
| 5.11 | **Matching pipeline tests** — both directions (`match_thesis_for_story`, `match_story_for_thesis`) with a real fixture pair | ⬜ | Currently: `tests/test_price_router.py` only. |
| 5.12 | **Scoring unit tests** — `compute_freshness` with known inputs; `chip_for` edge cases; `compute_tailwind` clamping + null path; `prescription_for` band × status truth table | ✅ | `tests/test_scoring.py` (32 tests). |
| 5.13 | **API response shape tests** — `/api/home`, `/api/thesis/{id}`, `/api/news/{id}` return the schema the frontend expects | ⬜ | Prevents silent regressions when the DB shape changes. |
| 5.14 | **Chat persistence round-trip test** — create session, add messages, read back, delete; verify share toggle | 🔶 | Coverage gap in `tests/test_chat_api.py` — only one test today. |

---

## Phase 6 — Supporting magic (Tier 2 moments)

Ship after Tier 1 is working reliably and the retro (`docs/slice-retro.md`) has been written.

| # | Item | Status | Notes |
|---|---|---|---|
| 6.1 | **Resolution ceremony** (Tier 2 moment #4) — when `POST /api/thesis/{id}/close` fires, generate a narrative reading the thesis history: score arc, which signals confirmed it, which invalidations were watched, most co-occurring thesis. Archived to `users/{id}/closed/thesis_NNN.md`. | ⬜ | Needs `thesis_snapshots` data + `thesis_story_links` history. The narrative generator is a simple LLM call on a well-structured prompt. |
| 6.2 | **Tension insight** (Tier 2 moment #5) — cross-thesis analysis pass in the digest: "Your AI adoption thesis strengthened, but NVDA near highs creates tension with your semiconductor mean-reversion thesis." Gated behind user owning ≥ 3 theses. | ⬜ |  |
| 6.3 | **Upcoming-event tripwire** (Tier 2 moment #6) — at thesis creation, scan FRED release calendar for events matching invalidation conditions; surface at creation + notify on event day. | ⬜ | Do not ship the promise ("I'll watch CPI Tuesday") before the follow-through is reliable. |
| 6.5 | **Thesis sharing endpoint** — `POST /api/thesis/{id}/share` returns a shareable URL or payload. The `[Share]` button is present in the mock UX. | ⬜ | Chat sharing exists via `src/chat/shares.py`; thesis sharing is separate. |

---

## Phase 7 — Tier 3 + growth

Exploratory. Do not plan around these until Phase 5 is solid and user data exists.

| # | Item | Status | Notes |
|---|---|---|---|
| 7.1 | **Accountability loop** (Tier 3 moment #7) — digest calls out theses stuck in `stressed` beyond the user's stated tolerance | ⬜ | Needs per-user config; high annoyance risk if poorly calibrated. |
| 7.2 | **Community adoption signal** (Tier 3 moment #8) — "47 traders adopted this thesis this week" | ⬜ | Needs real user base. Do not fake the number. Cold-start problem unsolved. |
| 7.3 | **Pre-market stance briefing** (Tier 3 moment #9) — 30-second read-aloud of where each thesis stands before market open | ⬜ | Overlaps heavily with daily digest; decide whether one surface or two after digest has real usage data. |
| 7.4 | **S3 migration** — move `global/stories/` from git to S3 | ⬜ | Blocks at ~50k files. Not urgent at current volume. Plan for it once story volume grows. |
| 7.5 | **Automated matching eval runner** | ⬜ | `docs/ref/matching-eval-set.md` exists; schema is still churning. Wire it when schemas stabilize. |
| 7.6 | **`judge_version` stamp on `thesis_story_links`** | ⬜ | Worth adding after the first meaningful judge prompt tuning pass. |

---

## Dependency graph

```
Phase 0 (slice demo)                        ← start here, everything else depends on it
  ├── 0.1 wire ingest writes
  └── 0.2 daily digest

Phase 1 (API completeness)                  ← can start in parallel with Phase 0
  ├── 1.1–1.5 thesis write endpoints
  ├── 1.6–1.7 news endpoints
  └── 1.8–1.12 computed fields

Phase 2 (core loop)
  ├── 2.1 sharpen-thesis chip              ← needs Phase 0 complete for backfill to work
  ├── 2.4 macro context frames             ← feeds into sharpen-thesis prompt quality
  ├── 2.7 auto stress-flip                 ← BLOCKED on 3.4 (calibration)
  └── 2.11–2.13 data integrity             ← no dependencies; do anytime

Phase 3 (quality gates)
  ├── 3.1 matching regression              ← run before any prompt change
  ├── 3.4 calibration                      ← gating block for 2.7
  ├── 3.7 digest cold-read                 ← gate for 0.2 ship decision
  └── 3.9 e2e smoke                        ← wire into pipeline_scheduler

Phase 4 (news at scale)
  ├── 4.1 smoke test first                 ← validates all remaining Phase 4 assumptions
  └── 4.8–4.9 scaling validation           ← only after volume exists

Phase 5 (ops hardening)
  ├── 5.1–5.3 auth + security              ← before any public users, no exceptions
  ├── 5.4–5.5 secrets                      ← do immediately
  ├── 5.6–5.14 DB + tests + monitoring     ← before launch
  └── 5.15–5.18 observability v2           ← prerequisite for credits/billing
                                              (5.15 → 5.16 → 5.17 → 5.18 strict order)

Phase 6 (Tier 2 magic)                     ← after Phase 5 is solid
Phase 7 (growth)                           ← after user data exists
```

---

## This week (2026-05-05)

In priority order:

1. **0.2 + 0.3** — Start `agents/daily_digest.py`. This is the demo milestone.
2. **5.15 + 5.16** — Port Langfuse and add `agent_usage` persistence. Without these we can't answer "what did this user spend last week," which blocks the credits design from `docs/design-billing-credits.md` from leaving the doc stage. Self-contained from product work; safe to do in parallel.

2.11 (sharp-lane ticker registry validation in `src/news/persist.py`) shipped 2026-05-04 — synthesizer-emitted unknown tickers now drop + log to `pending_instruments` with `source='ingest'`.

0.1 (`match_thesis_for_story` writes) shipped 2026-05-03 — the ingest pipeline now feeds the scoring read model.

Firehose Phase 1 (4.1–4.5c) shipped 2026-05-03. Soft launch is unblocked
on the news ingest path; remaining cross-cutting blockers are 0.1, 5.1–5.4,
and the digest milestone.

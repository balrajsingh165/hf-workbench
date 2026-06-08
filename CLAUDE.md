# Heurist Finance Workbench

This repo is for prototyping Heurist Finance - a thesis-driven finance app for retail stock and commodity traders with multi-day to multi-week holding horizons.

## Repo Navigation
- `db/schema.py` — DB schema definition. Edit `TABLES` dict and re-run to apply.
- `db/hf.db` — SQLite database (queryable index).
- `users/{id}/profile.md` — Per-user profile (personalization, memory, preferences).
- `global/theses/{id}.md` — Per-thesis document (statement, tickers, signals, invalidation conditions).
- `global/news/{id}.md` — Per-article document (headline, overview, metadata).
- `docs/` — SOPs, feature plans, and reference material.

## SOPs
Read the relevant SOP before doing the task it covers; each is a step-by-step procedure.
- `docs/sop-add-new-user.md` — add a user (DB row + `users/{id}/` markdown profile).
- `docs/sop-add-new-thesis.md` — add a thesis (DB row + `global/theses/{id}.md`).
- `docs/sop-add-news.md` — add a story (DB row + `global/stories/{id}.md`).
- `docs/sop-remove-content.md` — delete a thesis or story (rows + markdown, no orphans).
- `docs/sop-schema-change.md` — decide what to rebuild after a schema/format/embedding/prompt change.
- `docs/sop-metrics-and-alarms.md` — interpret a health/metrics finding, tell real from false alarm, choose the response (rarely a backfill).
- `docs/daily-backend-health-review.md` — full daily backend health review runbook (see also Backend Health Review below).

## Related Repos
- `~/heurist-finance-frontend` — Next.js 16 / React 19 web frontend. Consumes this repo's HTTP API (notably `/api/v1/ai-sdk/chat/completions`, `/api/v1/chats/*`, `/api/home`, `/api/v1/prices/*`) and the agent's UI Message Stream / tool output shapes. **When you change backend API routes, response payloads, tool names/params, or agent stream events, check the frontend for consumers and keep them consistent.** The frontend generates TypeScript types from the backend's OpenAPI schema via `bun run gen:types` (writes `src/lib/api-types.ts` against `${HF_API_BASE:-http://localhost:8088}/openapi.json`) — re-run it after any route/schema change so the frontend type-checks against the new shape.
- `~/hf-evals` — Eval harness that runs scripted scenarios against a live workbench server, captures the SSE trace, and grades it against rubrics with Sonnet. Use it whenever you're prompted to validate an agent behavior change (prompt edit, tool change, model swap, scoring tweak): start the workbench (`HF_AGENT_PROTOCOL_SMOKE=1` is fine for protocol-only runs), then `hf-evals run [--scenario id|category]` followed by `hf-evals judge` and `hf-evals scoreboard` / `hf-evals review`. Scenarios live under `scenarios/{category}/{id}.md`, rubrics under `rubrics/`.
## Heurist Mesh (external agents)

Sage tools for prices, macro, and filings are thin wrappers in `app.py` → `src/clients/mesh.py` → remote Mesh (`MESH_API_ENDPOINT`, default `https://mesh.heurist.xyz`). Prompts and response shapes live in this workbench; fetch/transform logic runs on Mesh.

| Workbench tool / route | Mesh agent | Local source (when needed) |
|---|---|---|
| `price_summary`, `market_overview`, `fundamentals_snapshot` | `YahooFinanceAgent` | `~/heurist-agent-framework/mesh/agents/yahoo_finance_agent.py` |
| `search_macro` | `FredMacroAgent` | `~/heurist-agent-framework/mesh/agents/fred_macro_agent.py` |
| `recent_filings`, `xbrl_fact`, `recent_insider` | `SecEdgarAgent` | `~/heurist-agent-framework/mesh/agents/sec_edgar_agent.py` |
| `web_search` | `ExaSearchDigestAgent` | `~/heurist-agent-framework/mesh/agents/exa_search_digest_agent.py` |

Allowlisted tool names: `SELECTED_MESH_TOOLS` in `src/clients/mesh.py`. Auth: `HEURIST_API_KEY` (see `src/config.py`).

**Only open the local Mesh agent source when you believe a tool response is incorrect** (wrong number, bad transform, missing field). Trace workbench → `src/clients/mesh.py` → Mesh agent id/tool name → file under `~/heurist-agent-framework/mesh/agents/`. Fixes there require a Mesh redeploy; restarting hf-workbench alone does not change Mesh behavior.

## Architecture Principles
- **Markdown is the source of truth** for rich/narrative data. SQLite is a queryable index for structured lookups.
- **No data overlap** between DB and markdown, except primary keys (`id`). If a field is useful for filtering/sorting/joining, it lives in the DB. Everything else lives in markdown.
- **Theses are global** (not nested under users) to enable cross-user analysis and clustering. A user can own many theses and a thesis can be owned by many users; the N:M mapping lives in the `user_theses` link table. The primary query path is user → theses (e.g. a user's dashboard); we don't query users-by-thesis in product surfaces.
- **Files over config.** Data is scattered as markdown files for maximum flexibility. No complex frameworks or migration tools — just edit `schema.py` and re-run.
- **Prefer the simpler method.** When weighing approaches, designs, or implementations, default to the simplest one that meets the need — fewer layers, fewer abstractions, less new machinery. Don't over-engineer; reach for first principles before adding structure. If a heavier approach is warranted, say why.
- **Simplify during review.** When reviewing a diff, look beyond correctness for ways to simplify, refactor, or cut: redundant code, needless abstractions, dead branches, and minor cruft. Call these out and prune them.

## Product Philosophy
Our main differentiator is our belief management system. Most finance apps surface more data. Heurist Finance surfaces the user's own convictions, made rigorous and kept alive.

The core primitive is a **thesis** — a single declarative sentence expressing a durable, actionable market belief. A thesis is not a summary of news. It is a position derived from news, macro context, and price behavior. It should be specific enough to be testable and broad enough to outlive the headline that inspired it.

Example of a weak thesis: "Tech is doing well."

Example of a strong thesis: "Hardware-focused Apple leadership signals deeper Apple Silicon investment, bullish for TSMC suppliers over the next 4–6 weeks."

The AI's job throughout this system is to help users form sharper theses, keep them honest about their convictions, and synthesize multiple theses into decisive analysis. The tone of all AI output is confident and direct. No hedging language. No neutral stances. The system is allowed to be biased because the user's theses are biased — that is the point.

## Agent Layer (`src/agent/`)

Two-phase Strands pipeline (research → response) on AWS Bedrock. `orchestrator.py` drives `research.py` then `response.py`; the migration target is AI SDK UI Message Stream output. The rest of the subpackage is the supporting cast (config, models, tools, prompt manager, stream emitter, cancel/session helpers).

The legacy `POST /api/agent/run` / `POST /api/agent/cancel` chip SSE flow has been removed. New work should target `POST /api/v1/ai-sdk/chat/completions` plus `/api/v1/chats/*`.

Config reads AWS/Bedrock credentials and Strands knobs from env — see `src/agent/config.py` for the full list and defaults. AI SDK stream smoke test: `uv run python scripts/smoke_ai_sdk_chat.py`. For protocol-only local testing without Bedrock, start the server with `HF_AGENT_PROTOCOL_SMOKE=1`.

## Dev Environment
- Always use `uv run python` to execute Python scripts (ensures correct venv and dependencies).
- When you build new features or refactor, DO NOT worry about backward compatibility. The product is not launched yet.
- Committing directly to `main` is fine — this is a prototyping repo and feature branches/PRs are not required (use them only if a change genuinely warrants review).

## Pipeline Ingestion
The story pipeline (firehose → route_news_clusters → judge → match → score → daily_brief) runs in a **separate pm2 process** from the API: `hf-pipeline` (`uv run python -m agents.pipeline_scheduler`), configured in `ecosystem.config.cjs` alongside `hf-workbench` (uvicorn only — no `--reload`, no embedded scheduler). To trigger one full cycle manually: `uv run python -m agents.pipeline_scheduler --once`. For finer-grained runs, invoke a single stage directly (e.g. `uv run python -m agents.route_news_clusters --write` for story routing only, or `uv run python -m agents.firehose` for feed polling only). Output goes to `logs/hf-scheduler.log` (human) and `logs/hf-pipeline-metrics.jsonl` (machine). Restart both: `pm2 restart ecosystem.config.cjs`.

## Backend Health Review
`docs/daily-backend-health-review.md` is the runbook for probing how the system is running. Follow it when asked to inspect liveness, recent logs, pipeline metrics, DB freshness, markdown integrity, story/thesis-match/brief quality, price providers, or agent chat usage and cost. It bundles the exact `pm2`, `curl`, `sqlite3`, log-tailing, and `scripts/hf_health.py` / `scripts/hf_metrics.py` commands to use, plus quality bars and red flags. Default to read-only — don't reset, delete, or overwrite data during a review unless the user explicitly asks for a fix.

## Core Concepts
Thesis is the atomic unit of the system. One thesis = one declarative market belief, owned by one user.

Thesis Status Lifecycle
- Active — User-created, user-owned, being monitored. The AI watches for confirming and stressing signals continuously.
- Stressed — Triggered automatically when the composite score drops below 35, or when an incoming signal semantically matches one of the thesis's named invalidation conditions.
- Resolved — User-initiated only, via Close command.

Thesis Score
A single composite number 0–100, computed by the Scoring Agent. The score is
intrinsic to the belief, not the holder — both sub-dimensions derive purely from
global inputs (the `thesis_story_links` timeline and market price action on the
tagged tickers), so it lives on the `theses` row, is computed once per thesis,
and is shared by every owner. Unowned proposal theses (`owner_count=0`) are
scored too. `user_theses` holds only genuinely per-user state (status,
resolution). Three sub-dimensions:

Freshness (0–100): time decay from the last supporting signal relative to the thesis horizon. Decays faster for short-horizon theses. Stored as `theses.score_freshness`.
Tailwind (0–100): directional agreement between recent price action on tagged tickers and the thesis's implied direction. High = market moving with the thesis; low = headwind. Stored as `theses.score_tailwind`.

# Plan: Production Alerting

**Status:** Draft - metrics collector implemented, notification transport not implemented  
**Last updated:** 2026-05-05  
**Implemented foundation:** `scripts/hf_health.py`

This doc defines what should eventually page, Slack, or email an operator. The
current repo does not have a notifier. Until then, run:

```bash
uv run python scripts/hf_health.py --json --append --fail-on-alert
```

The script is read-only. It collects DB and pipeline health metrics and emits
local alert candidates. A future notifier should consume the JSON output and
route by severity.

## Severity Model

| Severity | Meaning | Future route |
|---|---|---|
| critical | Core loop is broken, billing/accounting is wrong, or user-visible data is stale/corrupt. | Page or urgent Slack. |
| warn | Degraded quality, backlog growth, suspicious drift, or a condition that becomes critical if it persists. | Slack/email digest. |
| info | Capacity, trend, and baseline data. | Dashboard only. |

## Metric Sources

| Source | Current owner | Data |
|---|---|---|
| `logs/hf-pipeline-metrics.jsonl` | `agents/pipeline_scheduler.py`, `agents/firehose.py` | Pipeline step success, firehose raw/inserted/drop counts, judge calls, score counts, daily brief metrics. |
| SQLite `db/hf.db` | schema tables | News, sectors, tickers, thesis lifecycle, match links, scoring snapshots, agent usage. |
| `scripts/hf_health.py` | new collector | Aggregated business health metrics and local alert candidates. |
| `scripts/hf_metrics.py` | agent usage CLI | Agent cost, tokens, request, user, model, endpoint spend. |
| Langfuse / OTel | `src/agent/observability.py` | Trace-level agent latency, spans, payload previews, tool traces. |
| Server and scheduler logs | `tmux`, `logs/hf-scheduler.log` | Runtime errors, failed commands, provider exceptions. |
| Future billing tables | `users_billing`, `credit_ledger`, `billing_cycles` | Credit balance and overage correctness once billing ships. |

## Implemented Health Collector

`scripts/hf_health.py` currently tracks:

- pipeline liveness and consecutive failures
- firehose liveness and raw item health
- news volume by lane over 1h/24h/7d
- news publisher and sector distribution
- ticker coverage and sharp-lane embedding coverage
- thesis review-status counts and match-index coverage
- thesis-news link recency and judge health
- scoring snapshot coverage and null score rates
- daily brief freshness, theme count, and source count
- pending instrument backlog
- agent usage accounting gaps: errors, missing session IDs, zero-cost token rows, missing aggregate rows

It emits `status: ok|warn|critical` and a `findings[]` list.

## Pipeline Liveness Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `pipeline.no_runs` | critical | No `run_finish` event exists. | Scheduler never completed a full run. |
| `pipeline.stale` | critical | Latest full run older than 24h. | Daily digest/scoring/matching likely stale. |
| `pipeline.consecutive_failures` | critical | 2+ failed full runs in a row. | Persistent pipeline break. |
| `pipeline.step_failed` | critical | Any required step returns nonzero in latest run. | One product subsystem failed. |
| `pipeline.duration_spike` | warn | Runtime > 2x 7-day p95. | Provider slowness, retries, or hung step. |

## Firehose Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `firehose.no_runs` | critical | No `firehose_run` event exists. | Continuous ingest never started. |
| `firehose.stale` | critical | Latest firehose run older than 30 minutes. | Scheduler or RSS polling stopped. |
| `firehose.consecutive_failures` | critical | 2+ failed firehose runs. | Persistent RSS/parser/DB failure. |
| `firehose.zero_raw_items` | critical | Latest `raw_items = 0`. | Feed parser, network, or feed URLs broke. |
| `firehose.raw_items_drop` | warn | Raw items < 40% of 7-day same-hour baseline. | Feed outage or parser drift. |
| `firehose.inserted_spam_rate` | warn | Class-action/spam inserts > 20% of inserts. | Spam filter too weak. |
| `firehose.unknown_ticker_rate` | warn | Unknown tickers / inserted > 30%. | Registry/resolver drift or bad ticker extraction. |
| `firehose.duplicate_rate_spike` | warn | Duplicates / raw_items > 90% for 6+ runs. | Feeds are stale; not always fatal, but worth knowing. |

## News Ingest Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `news.no_news_24h` | critical | No `news` rows created in 24h. | Product has no new market context. |
| `news.no_story_24h` | critical | No `story` rows created in 24h. | Cluster routing or synthesis failed while raw firehose still runs. |
| `news.volume_drop` | warn | 24h volume < 40% of trailing 7-day daily average. | Source/provider degradation. |
| `news.volume_spike` | warn | 24h volume > 3x trailing 7-day daily average. | Duplicate/spam/flood failure. |
| `news.publisher_dominance` | warn | One publisher > 70% of 24h rows. | Source mix is unhealthy. |
| `news.ticker_coverage_low` | warn | > 50% of 24h news has no `entity_tickers`. | Ticker extraction or registry validation degraded. |
| `story.markdown_missing` | critical | `story` row exists but `global/stories/{id}.md` missing. | Source-of-truth mismatch. |

## Sector And Taxonomy Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `story.empty_sector_rate_high` | warn | > 20% story rows have empty sectors. | Synthesis schema or normalization drift. |
| `story.unknown_sector_label` | critical | `sectors_json` contains labels outside `CANONICAL_SECTORS`. | Contract drift that breaks filters. |
| `news.sector_disappeared` | warn | Core sector has zero rows for 7d while total volume is normal. | Feed mix or classifier drift. |
| `news.sector_dominance` | warn | One sector > 60% of sharp-lane 24h rows. | Classifier stuck or source bias. |

Core sectors to watch first: `Macro`, `Energy`, `Technology`, `Financial Services`,
`Semiconductors`, `Healthcare`, `Commodities`, `Government & Policy`.

## Instrument Registry Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `pending_instruments.backlog_high` | warn | Unreviewed pending instruments > 250. | Registry review is falling behind. |
| `pending_instruments.new_spike` | warn | New pending instruments 24h > 3x 7-day average. | Resolver drift or noisy feeds. |
| `pending_instruments.repeated_symbol` | warn | Unknown symbol seen >= 10 times. | Likely real instrument missing from registry. |
| `news.unknown_tickers_dropped` | warn | Sharp ingest drops registry-unknown LLM tickers. | Synthesis prompt may emit invalid symbols. |
| `thesis.unknown_ticker_rejections` | warn | Discovery rejects theses due to unknown symbols. | Registry blocks thesis creation. |

## Thesis Lifecycle Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `thesis.missing_match_chunks` | critical | Active thesis has zero `thesis_match_chunks`. | Matching cannot find evidence. |
| `thesis.no_links` | warn | Active thesis has zero `thesis_story_links`. | Feed/scoring may look dead for that thesis. |
| `thesis.stuck_candidates` | warn | Candidate thesis older than 7 days. | Discovery/promotion loop is not closing. |
| `thesis.discovery_zero_rate` | warn | No thesis candidates for many days despite sharp ingest volume. | Discovery gate/prompt may be too strict. |
| `thesis.discovery_spike` | warn | Candidate count spikes. | Discovery prompt too permissive. |
| `thesis.active_count_drop` | critical | Active thesis count drops unexpectedly. | Data loss or destructive schema operation. |
| `thesis.markdown_missing` | critical | `theses` DB row lacks markdown file. | Markdown source-of-truth mismatch. |

## Thesis-News Matching Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `matches.no_updates` | critical | Sharp news exists but no links updated in 24h. | Matching pipeline broken. |
| `matches.theses_failed` | critical | Latest backfill failed for one or more theses. | Some theses are stale. |
| `matches.judge_failures` | warn | Judge parse failures > 0 in latest run. | Prompt/model response drift. |
| `matches.judge_failure_rate` | critical | Judge failures / judge calls > 10%. | Matching quality unreliable. |
| `matches.all_unrelated` | warn | Judge calls normal but links written = 0 for repeated runs. | Thresholds or judge prompt too strict. |
| `matches.support_stress_ratio_drift` | warn | Supports/stresses ratio differs sharply from 30-day baseline. | Judge direction drift or market regime shift. |
| `matches.low_confidence_spike` | warn | Average confidence drops below baseline. | Retrieval quality or prompt drift. |
| `matches.retrieval_score_drop` | warn | Average retrieval score drops below baseline. | Embedding/index issue. |

## Scoring Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `scoring.snapshots_missing` | critical | Today's snapshots < active `user_theses`. | Scores are stale or missing. |
| `scoring.null_scores` | critical | Active user_theses have null composite score. | Dashboard ranking breaks. |
| `scoring.null_tailwind` | warn | Active user_theses have null tailwind. | Price provider/canonicalization gap. |
| `scoring.score_freeze` | warn | Scores unchanged for multiple days while links/prices changed. | Scoring job not writing. |
| `scoring.score_collapse` | critical | Median score collapses unexpectedly. | Formula, data, or matching failure. |
| `scoring.price_batches_zero` | warn | Price batch estimate is zero in scheduled scoring. | Tailwind not recomputed. |

## Daily Brief Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `brief.missing` | critical | No brief row exists. | Homepage/digest surface is empty. |
| `brief.not_today` | warn | Latest brief date is not today after generation window. | Brief is stale. |
| `brief.theme_count_invalid` | critical | Theme count outside 4-6. | Prompt/schema contract broke. |
| `brief.no_sources` | critical | Latest brief has no source IDs. | Provenance broken. |
| `brief.themes_without_sources` | critical | Any theme lacks sources. | Unsupported claims likely. |
| `brief.provenance_issues` | critical | Provenance verifier reports unknown or unsupported sources. | User-visible trust failure. |
| `brief.movers_missing` | warn | Quoted movers < expected movers. | Market data provider degradation. |
| `brief.model_fallback` | info/warn | Brief falls back from Flash to Pro. | Cost/quality signal. |

## Agent Chat And Cost Alerts

These use `agent_usage`, `scripts/hf_metrics.py`, and Langfuse traces.

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `agent_usage.errors` | critical | Aggregate rows with `status != ok`. | User-visible agent failures. |
| `agent_usage.zero_cost_tokens` | critical | Nonzero tokens but `cost_usd = 0`. | Pricing table missing; billing leak. |
| `agent_usage.missing_aggregate` | critical | Request has phase rows but no aggregate row. | Dashboards/billing will undercount. |
| `agent_usage.missing_session_id` | warn | Aggregate rows missing session id. | Debug and billing correlation gap. |
| `agent_usage.cost_spike` | warn | 24h cost > baseline or configured cap. | Prompt/tool loop runaway. |
| `agent_usage.user_spend_spike` | warn | One user spend > threshold or 3x baseline. | Abuse or runaway client. |
| `agent_usage.model_unknown` | critical | Model id not in pricing table. | Cost silently routes to zero. |
| `agent_usage.cache_miss_spike` | info/warn | Cache read tokens collapse while writes/inputs spike. | Prompt cache not working. |
| `agent_latency.p95_high` | warn | Endpoint or phase p95 latency > threshold. | Provider/tool slowdown. |
| `agent_trace.missing` | warn | Usage row exists without Langfuse trace. | Observability gap. |
| `agent_tool.failure_rate` | warn | Tool error rate > threshold in traces/logs. | Research quality degraded. |

Initial thresholds:

- chat error rate critical above 5% over 1h
- endpoint p95 latency warn above 45s, critical above 90s
- single request cost warn above `$0.50`, critical above `$2.00`
- daily total cost warn above configured soft budget
- zero-cost token rows critical at any count

## Billing And Credit Alerts

These become active when `docs/design-billing-credits.md` ships.

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `billing.ledger_balance_mismatch` | critical | `users_billing.credit_balance != SUM(credit_ledger.delta)`. | Billing source-of-truth divergence. |
| `billing.usage_without_charge` | critical | Agent aggregate row has no ledger debit. | Revenue leak. |
| `billing.charge_without_usage` | critical | Ledger debit has no matching usage row. | User overcharged. |
| `billing.out_of_credits_errors_spike` | warn | `out_of_credits` responses spike. | Grant too low or UX issue. |
| `billing.overage_cap_hit` | warn/critical | User hits cap. | Runaway client or heavy-user risk. |
| `billing.payment_failed` | critical | Stripe payment fails. | Access and revenue issue. |
| `billing.cycle_close_failed` | critical | Billing cycle close job fails. | Overage accounting stale. |

## Runtime And Infrastructure Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `api.down` | critical | `/api/home` or health endpoint fails. | API unavailable. |
| `api.error_rate` | critical | 5xx rate > 2% over 10m. | User-visible breakage. |
| `api.bad_request_spike` | warn | 400s spike by endpoint. | Frontend/backend contract drift. |
| `server.restart_loop` | critical | Repeated process restarts. | Runtime instability. |
| `scheduler.down` | critical | tmux/process missing for scheduler. | Background jobs stopped. |
| `disk.low` | critical | Disk free < 10%. | SQLite/log writes at risk. |
| `db.lock_contention` | warn | SQLite busy/locked errors appear. | Concurrent writers or long transactions. |
| `db.backup_missing` | critical | No successful DB backup in 24h once backups ship. | Data loss risk. |

## Provider Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `bedrock.auth_failed` | critical | Startup or chat smoke cannot reach Bedrock. | Agent unavailable. |
| `bedrock.throttle_rate` | warn | Throttles above baseline. | Capacity issue. |
| `gemini.failures` | critical | Ingest/judge/brief Gemini calls repeatedly fail. | News/thesis pipeline degraded. |
| `mesh.price_failures` | warn | Price batches fail or return empty. | Tailwind and movers degrade. |
| `particle.empty` | warn | Particle frontpage returns zero stories. | Sharp ingest discovery degraded. |
| `rss.feed_failures` | warn | Individual RSS feeds repeatedly fail. | Firehose source degraded. |
| `langfuse.export_failures` | warn | OTel export/auth failures. | Trace visibility lost. |

## Security And Data Integrity Alerts

| Alert | Severity | Rule | Why |
|---|---|---|---|
| `auth.user_cross_access` | critical | Any endpoint returns another user's session/data in tests/logs. | Trust/security failure. |
| `share.private_exposed` | critical | Non-public shared chat accessible by public route. | Privacy failure. |
| `secret_in_logs` | critical | Known secret patterns appear in logs. | Credential leak. |
| `db_destructive_rebuild` | critical | DB table counts collapse or schema init full rebuild detected outside maintenance. | Data loss. |
| `markdown_db_drift` | critical | DB rows and markdown source-of-truth diverge materially. | Product data corruption. |

## Notification Phasing

1. **Phase A - Local health collector**  
   Done: `scripts/hf_health.py`.

2. **Phase B - Cron/systemd check**  
   Run every 10 minutes:
   `uv run python scripts/hf_health.py --json --append --fail-on-alert`.

3. **Phase C - Slack/email notifier**  
   Add `scripts/hf_alerts.py` that shells out to `hf_health.py --json`,
   dedupes repeated findings, and posts critical/warn routes.

4. **Phase D - Dashboard**  
   Read `logs/hf-health-metrics.jsonl`, `logs/hf-pipeline-metrics.jsonl`,
   and `agent_usage` into a lightweight admin page.

5. **Phase E - Baseline-aware thresholds**  
   Replace static thresholds with rolling same-hour baselines after there is
   enough production history.

## Immediate Next Work

- Add `hf_health.py` to the scheduler or cron once notification transport is chosen.
- Add markdown/DB drift checks for `global/stories` and `global/theses`.
- Add a Bedrock live health check distinct from protocol smoke.
- Add a DB backup success marker before enabling `db.backup_missing`.
- Decide whether firehose rows should ever receive sectors/embeddings; keep
  alert rules lane-specific until that contract changes.

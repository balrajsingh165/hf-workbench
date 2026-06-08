# TODO

## Pending Instruments — Backlog Watch (2026-05-20)

`pending_instruments.backlog_high` has been firing on `scripts/hf_health.py`.
Use `uv run python scripts/hf_health.py` and the source-count query below for
the current count before triage.

**2026-05-20 partial pass:** Promoted 35 obvious large/mid-caps to
`seed.py` (ADI, ALIT, APO, BAC, BCE, BIDU, BX, CCL, COTY, CRWD, DOCU,
EQNR, FSK, GLOB, GPK, GS, HD, HII, HTGC, IBRX, INGR, KBH, LOW, MEDP,
RKLB, RTX, SAP, SLF, SPGI, STLA, TJX, UPST, VEEV, VZ, WTW). Registry
201 → 236. Corresponding `pending_instruments` rows marked `keep=1`.
Motivation: these were causing meaningful stories (e.g. Lowe's Q1
earnings `story_453`) to ship with empty tickers under the slate-gate
and get judged `unclear`. The rest of the backlog (2175 still
unreviewed) deferred — a focused promotion was preferred over a sweeping
keep=0 reject pass.

**Is the backlog a real bug?** No, the firehose gate is doing what it
was designed to do — surface unknown symbols for human triage. The bug
is operational: the weekly review hasn't happened.

**Before draining, check:**
- [ ] **Confirm the firehose-only growth model still holds.** Run
  `SELECT source, COUNT(*) FROM pending_instruments WHERE keep IS NULL
  GROUP BY source` and verify `firehose` still dominates. If `story`
  starts growing, the slate constraint has a leak — investigate before
  any registry edits.
- [ ] **Sanity-check the top-10 repeat offenders for real-ness.** A
  symbol seen 10+ times across distinct firehose articles is the
  intended auto-signal. Spot-check 3–4 by joining `source_id` back to
  `news.id` and reading the linked headline/source metadata.
- [ ] **Decide whether the threshold (250) is still appropriate** given
  current firehose volume (~200 new symbols / 24h). At today's growth
  rate any weekly review starts at >1k unreviewed. Either (a) raise the
  threshold to ~500 so the alert reflects real backlog, not normal
  steady-state, or (b) tighten the firehose gate to only stash symbols
  with `seen_count` ≥ 2 across distinct headlines, cutting one-off
  noise. Don't both at once — pick the cheaper signal first.
- [ ] **Run the manual cadence** per the Instrument Registry section
  below: promote accepted symbols to `src/instruments/seed.py`, re-run
  `uv run python -m src.instruments.seed`, and `UPDATE
  pending_instruments SET keep=0/1` for reviewed rows. Date this pass.

Related: the existing "Weekly pending-instruments review" item under
*Instrument Registry — Deferred* below, and the
*Synth path no longer feeds `pending_instruments`* item under *News
Synthesis — Slate / Coherence Follow-ups*.

## News Synthesis — Slate / Coherence Follow-ups (2026-05-18)

Landed in the slate-constrained synth PR: hard `allowed_symbols` verifier
gate, alias-substring evidence check, embedding-based coherence gate
before synth, `story.created_at` now derived from max source publish
time. The items below are explicitly deferred follow-ups — each has a
listed trigger so you know when to pick it up rather than work on it
prophylactically.

- [ ] **Frontend pass for `story.created_at` semantic change.** It used
  to be row-insert time; it's now the freshest member `published_at`
  (fallback: member `created_at`, then `now()`). The home feed orders
  by `s.created_at` (`api.py:build_feed_stories`), so the home-feed sort
  is now wire-time, not pipeline-time. Check **how to validate**: (1)
  start the workbench, `curl http://localhost:8088/api/home` and confirm
  the visible ordering matches what a trader would expect (recent news
  first, not "ingest order"); (2) re-run `bun run gen:types` in
  `~/heurist-finance-frontend` against `${HF_API_BASE}/openapi.json` —
  the field's type didn't change, but the workspace rule says re-run on
  any schema/route change; (3) grep the frontend for `created_at` use on
  story objects (`rg "story.*created_at"` in `~/heurist-finance-frontend`)
  and confirm no UI label says "added X minutes ago" when the wire is
  actually hours old.
- [ ] **Decide parenthesized bare tickers like `(KMI)`.** `EXPLICIT_TICKER_RE`
  in `src/news/ticker_candidates.py` deliberately drops the bare `(`
  trigger to avoid the pre-existing `(NASDAQ:NVDA)` → `NASDAQ` bug. So
  press-release bodies that write `(KMI)` without an exchange prefix are
  silently dropped from the slate. **When to pick up:** once
  `instruments` coverage grows past ~250 symbols (current ~200), false-
  positive risk on bare-uppercase tokens (`OPEC`, `CEO`, `USA`) becomes
  the bottleneck, not coverage. **How to fix:** add a second pass that
  matches `\(([A-Z][A-Z0-9.\-]{1,8})\)` and accepts only symbols that
  pass `instrument_exists`. **How to check it's needed:** query
  `SELECT * FROM pending_instruments WHERE source='firehose' AND keep IS NULL ORDER BY seen_count DESC` —
  if you see bare-parenthesized symbols repeatedly (e.g. KMI seen
  20+ times) that the synth path isn't surfacing, this is the gap.
- [ ] **Coherence gate observability before tuning the threshold.** The
  gate at `COHERENCE_MIN_PEER_SIM = 0.55` correctly drops orthogonal
  outliers but misses sector-adjacent Frankensteins (nat-gas futures +
  utility M&A both score ≥0.55 on Gemini embeddings). **Don't raise the
  threshold blindly** — coherent multi-article clusters will also clear
  ~0.55 to ~0.70 and you'll start dropping good signal. **What to add
  first:** log surviving max-peer-sim per kept cluster + dropped ids
  into `logs/hf-pipeline-metrics.jsonl` so you can chart the
  distribution. The `_log_synth_rejection` path already captures drops;
  add the kept-cluster side too. **How to check:** after ~1 week,
  inspect `unclear` rationales tagged `combined_unrelated_events` in
  `docs/report-ambiguous-unclear-stories.md` and cross-reference with
  the metric — if the bad merges sit at sim≈0.60-0.70 and good merges
  sit at ≥0.75, you have a threshold case; if they overlap, you need a
  topic-pair detector (heavier, defer further).
- [ ] **Synth path no longer feeds `pending_instruments` — confirm
  firehose alone is enough.** The pre-slate code path in
  `_persist_story_row` writes to `pending_instruments` when synth emits a
  non-registry symbol; with the slate constraint, this path is
  effectively dead because the LLM is constrained to known symbols.
  Registry growth now relies entirely on `agents/firehose.py:568-578`
  (firehose gate logs unknowns from headlines/bodies). **How to check:**
  weekly, run `SELECT source, COUNT(*) FROM pending_instruments WHERE
  keep IS NULL GROUP BY source` — if `firehose` count keeps growing and
  real names like NEE/D/KMI/VB show up, the firehose path is sufficient.
  If it stagnates, consider re-enabling synth-side discovery by relaxing
  the slate (e.g. allow a verifier "candidate symbol" path that bypasses
  `allowed_symbols` but still requires `instrument_exists` OR adds to
  pending). The existing
  "Weekly pending-instruments review" item below is the manual flip
  side of this — should now be the only source of registry growth.

## Chat Agent — Citation Grounding

- [ ] **Tighten citation validator from corpus-overlap heuristic to
  row-level provenance check.** Current `src/agent/ai_sdk_stream.py`
  validation is useful as a best-effort warning, but it only proves that a
  citation snippet overlaps some captured tool output this turn. It does not
  prove the citation's `tool`, `source`, `url`, and `snippet` belong to the
  same source row, and it currently allows empty snippets through. Later fix:
  allow only citable tools (`search_evidence`, `recent_filings`); require
  non-empty distinctive snippets; for `search_evidence`, require
  `source == story_id` and `snippet == headline` from the same evidence row;
  for `recent_filings`, require the cited URL/source/snippet to match the
  same filing row. Until then, treat `data-sources-warning` as a helpful
  detector, not a full provenance guarantee.
- [ ] **Preserve ambient-vs-explicit selection origin through to Phase 1
  prompt.** `_resolve_thesis_ids` / `_resolve_story_ids` in
  `src/interfaces/ai_sdk_compat/api.py:200,213` collapse the frontend's
  ambient (`active_thesis_id` / `active_story_id`) and explicit
  (`thesis_ids` / `story_ids`) channels into one list before hydration. By
  the time the lists reach `build_phase1_system_prompt`, the prompt builder
  can't tell whether the user explicitly chose the subject or just landed
  on a page that made it ambient. Current selected-story discipline
  (`prompt_manager.py:_SELECTED_STORY_RULE`) treats both the same. **When
  to pick up:** if we start seeing turns where the user asks a general
  market question with an ambient `active_story_id` set and Sage
  over-anchors the answer to that story instead of treating it as
  background. **How to check it's needed:** scan `chat_messages` for
  sessions where `subject.active_story_id` is set, `subject.story_ids` is
  empty, and the user_text is a general market/macro question — then read
  the response to see whether it stapled a story-specific verdict onto an
  off-topic question. **How to fix:** add an `origin: "explicit" |
  "ambient"` field on `StoryContext` / `ThesisContext`, render a softer
  framing in `_format_context_block` for ambient items ("background
  context, not the subject"), and let the selected-story rule key off
  origin instead of mere presence.

## News Re-architecture — Story Pipeline

Product reframe (2026-05-06): stories are the product. Theses are a
**separate** primitive owned by `discover_thesis` (`src/thesis/discover.py`,
see `docs/design-thesis-creation.md`). Stories no longer carry a `thesis`
field; the `should_emit` per-story gate has been removed. The thesis
quality bar lives entirely in `discover_thesis` going forward.

Still open:

- [ ] **Non-large-cap company stories in the global feed — needs
  personalization.** Some sharp-promoted stories are about specific
  smaller-cap companies that probably don't belong in *everyone's* home
  feed even when the cluster passes routing. Instinct: filter from the
  global feed, surface only on per-user feeds for users with a thesis or
  watchlist touching the name. Park until the personalization layer is
  being designed; do not add a hardcoded market-cap gate to routing in
  the meantime.
- [ ] **Earnings autopromote at materiality≥50 — gated on personalization.**
  After the tier-A news wires shipped (2026-05-07) the routing wall moved:
  ~25 single-publisher PR-wire earnings releases per day (Cognex, Loblaw,
  Sun Life, Penumbra, Apollo, Waters, etc.) sit in firehose at mat≥50
  because no tier-1 picks them up. A new R0c rule
  (`event_class='earnings' AND max_materiality>=50 AND
  has_institutional_or_pr_primary` → sharp_promote, mirroring R0 for
  fed/macro) would unblock them in one routing change. Holding off:
  these mostly-small/mid-cap earnings only matter to readers who have
  watchlisted or thesised the name, and dropping them into everyone's
  global feed would dilute it. Pick this up alongside the
  personalization layer (same blocker as the bullet above) — at that
  point the rule should write to a per-user lane, not the global feed.
- [ ] **Decide candidate-stage embedding (`_embedding_attach`): finish or
  delete.** Today the production policy is "embed only at promotion time"
  (`persist.write_cluster_story`). The Pass-2 candidate-stage path in
  `src/news/cluster.py::_embedding_attach` is implemented but dormant —
  firehose calls `attach_news_item(allow_embedding=False)`. Two options:
  (a) delete the dead code and lock in "promotion-time embedding only" in
  the plan, or (b) wire it for real (pass `cluster_member_count` /
  `max_materiality` into `_promotion_candidate`, decide which callers
  enable embedding). Pick a side once a cluster-precision regression
  traceable to cheap-pass-only attach surfaces.
- [ ] **Reconsider promotion rule R7 (mainstream single-source).**
  `src/news/routing.py` R7 promotes a `MAINSTREAM_SINGLE_SOURCE_EVENT_CLASSES`
  cluster only at `max_materiality >= 45`. Audit of 169 promoted stories over
  7d (May 10–17 2026) shows ~80% sit at `independent_pub_count == 1` and avg
  daily story count is 9–21 (with one 87-day outlier). AP/Reuters RSS being
  retired means tier-1 corroboration has structurally thinned — R7 may now be
  doing more gating than it should. Before tuning the 45 threshold, write
  down the **product policy** for single-source mainstream promotion (what
  classes, what bar) — feedback says don't chase metrics until the policy is
  explicit. Then look at the dropped clusters at `35 <= max_materiality < 45`
  with a `MAINSTREAM_SINGLE_SOURCE_EVENT_CLASSES` event class and ask: are
  these the stories we want, or do they read like wire noise? Don't touch
  thresholds until that pass is done. Related: also revisit R8
  (`MACRO_COMMENTARY_MIN_MATERIALITY = 55`) under the same lens — both were
  set when AP+Reuters were live corroborators.

## Auto-thesis discovery — quality watch

The story → `discover_thesis` path shipped 2026-05-07. Stories with
`theme_tag != 'other'` AND tagged tickers are routed to `discover_thesis`,
which has 6 layered gates (LLM+retrieval, post-LLM cosine ≥ 0.80,
post-creation re-check, registry grounding, structural promotion check,
backfill sanity). First eval against 13 backfilled stories: 2 new active
theses, 11 duplicate/non-thesis suppressions, 0 errors. Gates work — but
we have no ongoing quality signal yet. Pay close attention here; thesis
quality is the product.

- [ ] **Hand-review the 6 Gate-2 suppressions** to measure false-negative
  rate. Use `scripts/inspect_thesis_match.py` (defaults to the 6 cases:
  story_027↔thesis_017, story_029↔thesis_011, story_032↔thesis_013,
  story_034↔thesis_020, story_036↔thesis_013, story_046↔thesis_001).
  For each: duplicate / distinct / borderline. If any are "distinct," the
  0.80 threshold may need to rise — but raise only on evidence, not vibes.
- [ ] **Periodically re-run `scripts/eval_discover_thesis.py`** as new
  stories accumulate, dump traces with `--out`, and watch for drift in:
  (a) per-gate firing rates, (b) max/median similarity at the 0.80
  boundary, (c) registry grounding firings (currently 0 — if it starts
  firing, our taxonomy or the LLM's symbol emission needs work).
- [ ] **Spot-check newly created theses** against the population of
  active theses they were judged distinct from. The eval tells us a
  thesis was admitted; only human review tells us it was *good*.
- [ ] **Three gates have never fired in production** (post-creation
  re-check, registry grounding, structural promotion). They're cheap
  backstops but their utility is unproven. Don't remove them; do flag in
  reviews if they fire so we capture the failure modes.
- [ ] **Sequential ordering effect.** Stories processed earlier see a
  smaller index, so admission/suppression depends on processing order.
  In `route_news_clusters --write` this is fine (one story at a time);
  in batch backfills it skews results. Note this in any future eval.

## News Ingestion — Phase 1 Gaps
- [ ] **Wikimedia image fallback** — when `og:image` scraping returns nothing, query Wikimedia API by story entity (e.g. "Nvidia") for a usable fallback image.

## News Ingestion — Phase 2 (when Phase 1 is stable)
- [ ] **Weighted clustering formula** — replace keyword overlap with `0.45 * text_similarity + 0.25 * entity_overlap + 0.15 * time_decay + 0.10 * location_overlap + 0.05 * source_pattern`; requires embeddings + NER in the stack.
- [ ] **StoryArticleEdge with article roles** — label each article in a cluster as primary / corroborating / angle / opinion; use roles to sample diverse sources for summary generation.
- [ ] **Grounding verification pass** — after Gemini synthesis, verify every generated sentence maps to at least one source article; regenerate if unsupported. Requires full body text (Phase 1 body extraction first).

## Docs
- [ ] SOP Step 3 template missing `## Images` and `## Raw Sources` sections — human template diverges from what the ingestion script actually writes
- [ ] SOP missing operational notes: destructive `init_db` warning, required env vars, `--dry-run` flag, <2 publisher failure behaviour, Mesh parser fragility

## Code Interpreter Observability — Deferred (follow-ups to the run-stats cut, 2026-05-25)

Run-stats capture (the `code_interpreter_runs` table, chart token cost in `agent_usage`, Langfuse `code_interpreter.run` span, OTel `heurist.code_interpreter.*` metrics, and the `hf_metrics.py charts` CLI) shipped 2026-05-25. See `docs/agent-observability.md`. Out of scope then, noted here:

- [ ] **Pull AgentCore-native sandbox metrics (CloudWatch / X-Ray).** Our run stats come from the Strands/agent layer, not AgentCore's own observability surface. If we ever need true sandbox wall-clock / resource numbers (vs. our agent-side `elapsed_ms`), wire CloudWatch. Low priority — the agent-layer signal answers the product questions today.
- [ ] **x402 / AgentCore payments observability.** Separate, unbuilt feature; would add `agent_payment_events` + link payment events to the Langfuse span. Blocked on the payments feature itself (see `docs/design-agentcore-strands-integration.md` §3).
- [ ] **Reuse `code_interpreter_runs` for the future `analyze_data` Phase-1 tool.** The table's `purpose` column already reserves space (`chart` today); when a research-phase Code Interpreter analysis tool lands, record it with `purpose='analysis'` through the same `record_ci_run` path.

## Instrument Registry — Deferred (medium priority, follow-up to v1)

The v1 schema, seed, resolver, and brief/discover wiring shipped 2026-04-26.
Items below close known gaps but were intentionally scoped out of the first cut.

- [ ] **Discover path: write to `pending_instruments` instead of rejecting whole thesis.** Today, an unknown LLM-emitted symbol in the discover/sharpen flow rejects the entire thesis. Catch the resolver miss, write `(symbol, source='discover', source_id=<thesis_id>)` to `pending_instruments`, and let the thesis save with the unknown ticker stripped (or surface a "we don't track that yet" warning to the user). Mirrors the firehose treatment but lower-trust.
- [ ] **Weekly pending-instruments review (operator workflow, no engineering).** The `pending_instruments.repeated_symbol` alert (`scripts/hf_health.py`, threshold `seen_count >= 10`) is the auto-signal for promotion. Workflow: when the alert fires (or on a weekly cadence), decide promote vs reject per symbol, append promotions to `INSTRUMENTS` in `src/instruments/seed.py`, re-run `uv run python -m src.instruments.seed`, then `UPDATE pending_instruments SET keep=0/1` for reviewed rows. Last pass: 2026-05-05 (87 → 201 seeded; 292 → 185 unreviewed). No auto-promotion logic yet — revisit if the manual cadence becomes load-bearing.
- [ ] **Auto-promote on `seen_count >= N` (deferred until manual cadence is painful).** Extend `src/news/persist.py` (or a periodic job) to auto-insert into `instruments` once a pending symbol crosses a threshold across N distinct news items / M distinct days. Risk: pollutes the registry with biotechs nobody cares about, but reversible via `keep=0`. Don't build until weekly review is demonstrably the bottleneck.
- [ ] **Discovery-prompt registry block cost.** The full registry is pasted into every discover call. Switch to a tool-calling `lookup_instrument(query)` interface once registry prompt size starts eating measurable tokens.
- [ ] **Display naming consistency pass.** `short` and `display` were filled in seed-time best-effort. Audit: are similar instruments named consistently (`Sony` ADR vs Tokyo, `Samsung` ADR vs Korea, etc.)? Codify a brief style note once a few inconsistencies surface in product surfaces.
- [ ] **Movers/MoverSpec cleanup.** With `MoverSpec.label` removed, `daily_movers.label` is now derived data (re-computable from `symbol` via the registry). Either drop the column on the next schema rebuild, or leave it as a denormalized snapshot for historical briefs. Decide; don't leave it ambiguous.
- [ ] **Forward-looking: NER (`extract()`).** When a "highlight tickers in news body" feature is built, implement `extract(text)` on the resolver. Reads `aliases_json`, writes to `entity_tickers` with `entity_type='news'`. Add `requires_context` column to `instruments` at that time to handle edge cases like single-letter tickers, acronym collisions, and bare-uppercase false positives (`OPEC`/`CEO`).
- [ ] **Forward-looking: chat agent grounding.** When the in-chat research agent ships, expose `lookup_instrument(query)` as a tool, instruct the system prompt to refuse off-registry symbols, surface `ambiguous_matches` (e.g. `BA` → Boeing or BAE Systems), and route directional views on non-tradable rates/indices to their `proxy_for`-linked tradable proxies.

## Alpaca / Price Router — Deferred (follow-ups to Phase A+B, 2026-04-30)

Phase A+B shipped 2026-04-30 (see `src/clients/alpaca.py` + `src/clients/prices.py`). US equities and ETFs route to Alpaca (free IEX feed); crypto, indices, FX, futures, foreign listings stay on Mesh→Yahoo. Items below are known follow-ups, not blockers.

- [ ] **Prune duplicate alias rows in the registry.** With `canonical_symbol` now in place, the duplicates `USDJPY` (→ `JPY=X`), `BTC` (→ `BTC-USD`), and `DXY` (→ `DX-Y.NYB`) can be removed from `INSTRUMENTS` in `src/instruments/seed.py`. Audit `entity_tickers` and any other writer first to confirm nothing references the alias `symbol` directly. Until then both rows coexist; the resolver handles either input.
- [ ] **Provider-parity drift audit.** `scripts/check_provider_parity.py` shows 1.0–2.5pp window-return drift between Alpaca (IEX) and Mesh (Yahoo) on liquid US names — same direction every time, different magnitudes. Tailwind composite-score impact is ≤2 points per thesis (clamping at ±10% absorbs the drift), so this isn't blocking. Drivers likely include window endpoint alignment (calendar 31d vs. Yahoo `1mo`) and IEX vs. consolidated-tape last prices. Investigate only if a moment surfaces a number whose drift is visible to the user.
- [ ] **Drop legacy mover cache files.** `db/mesh_cache/2026-04-{24,25,26,27}_movers.json` are in the old Mesh raw shape and will produce all-None readings if re-read by the new `fetch_movers`. Either purge them or one-shot regenerate. Not blocking — only matters if the brief is replayed for those dates.
- [ ] **Phase C: Alpaca corporate-actions / earnings calendar.** Wrap `data.alpaca.markets/v1/corporate-actions` in `src/clients/alpaca.py` only when Tier 2 #6 (upcoming-event tripwire) is the active moment. Don't pre-build.
- [ ] **MoverSpec.asset_class denormalization.** Already noted under Instrument Registry; calling out here so it's not lost — `daily_movers.label` and `MoverSpec.asset_class` both duplicate registry data. Drop on the next schema rebuild or document explicitly as a snapshot.

## Thesis ↔ Story Matching — Deferred (low priority, noted so we don't re-debate)

These came out of the matching spike and the post-implementation review. None
are blocking; pick up only if the linked pain shows up in practice.

- [ ] **Migrate prediction-market eval script.** `agents/eval_prediction_markets.py` was deleted alongside the news-markdown corpus; rebuild it against story IDs if regression coverage is needed.
- [ ] **Parallelize `match_thesis_for_story` judge calls.** Thesis→story direction already runs a 4-worker `ThreadPoolExecutor`. Story→thesis is still sequential because per-ingest latency hasn't hurt yet. One-liner with `ThreadPoolExecutor`. Revisit when ingest throughput hits a wall.
- [ ] **Expose structured chunk-win fields on `ThesisMatch` return.** `thesis_story_links` already stores `retrieval_score` and `best_chunk_key`, so backfill callers see this. The story→thesis matcher only surfaces chunk-win data via stderr — promote to the return type if a live consumer (digest, UI) needs it programmatically.
- [ ] **Handle dual-direction stories** (both supports and stresses the same thesis). Single-relation schema can't express it; spike open question #3. Revisit only if digest misses something obvious.
- [ ] **Reintroduce "Mixed" chip state in the feed UI** — deferred from v1 in favor of a binary Supports / Stresses chip (see `docs/mock-ux-walkthrough.md`). Revisit when (a) the pipeline can emit multi-verdict output per (thesis, story) pair — see dual-direction item above — AND (b) real ingest data shows mixed cases >5–10% of links, OR users report "why didn't the feed tell me this story was contradictory?" Today the story detail page carries the nuance via the NET ASSESSMENT prose.
- [ ] **Automated eval runner over `docs/ref/matching-eval-set.md`.** Explicitly deprioritized: schemas still churn, ad-hoc spot checks are the current gate. Revisit once schemas settle or if silent quality regressions show up in the live digest.
- [ ] **Confidence calibration — reliability-bin spot-check.** MVP ships one floor only (`SUPPORT_STRONG_CONF` in `src/scoring_config.py`), gating the feed-card chip. Once `thesis_story_links` has ~100+ rows, manually bucket them into 0.1-wide confidence bins and hand-label each bin's precision (do you agree with the call?). Retune the floor based on what you see. Prerequisite for enabling auto stress-flip (see below). Do it once per schema or judge-prompt change.
- [ ] **`judge_version` column on `thesis_story_links`.** Stamp each row with the model + prompt hash + temperature at judge time. Lets you tell which rows came from which judge on re-run and refresh selectively when the prompt changes. Only worth the migration once there are multiple judge versions in flight (i.e., after the first meaningful prompt tuning pass).
- [ ] **Judge-prompt tuning for bidirectional stories.** Touch the prompt in `src/thesis/story_judge.py` when there's a real regression case to test against.

## Daily Brief — Deferred (nice-to-have, not MVP)

Pick up once the brief is shipped, stable, and we have reviewer feedback on
what's missing.

- [ ] **"Calendar Ahead" block.** 1-line teaser of upcoming macro/earnings events below the themes — e.g. "CPI Tuesday · Retail earnings Thursday · FOMC May 7". Data: FRED release calendar + an earnings API. Rendered as a third visible block on the homepage brief, below Themes and Market Movers. Defer until the first three blocks feel polished.
- [ ] **Theme drift markers.** Per-theme continuity flags (`↑ strengthening`, `↓ fading`, `→ continuing`, `✦ new`) emitted by the synthesis LLM using yesterday's themes as context. Cheap to add (prompt-only), but only meaningful once we have several consecutive days of briefs to compare. Revisit if reviewers say "the brief reads the same day over day."
- [ ] **Per-day "interesting movers" replacing the fixed 8.** LLM-curated mover set based on the day's action. Trades reviewability for relevance. Hold until the fixed-8 set proves limiting.
- [ ] **Intraday brief updates.** Today: one brief per day, pre-market. If market moves materially midday, the brief doesn't update. Revisit only if a desk or user flags the staleness.
- [ ] **Historical brief browsing UI.** Markdown archives exist in `global/briefs/`; no browse route in MVP.
- [ ] **Brief → news backlinks on the news detail page** ("this article fed Theme 02 of today's brief"). Adds two-way sync complexity; defer.
- [ ] **Stable cross-day theme IDs.** Today: per-day ordinals (`01`, `02`). Not comparable across days. Revisit only if "Theme X persisted across N days" becomes a feature request.
- [ ] **`hf_health` sharp-lane terminology cleanup.** After the brief migrated from `news` to `story` (2026-05-08), `scripts/hf_health.py` still reports `sharp` vs `firehose` lane splits at lines ~155/177/222 from the legacy news rearchitecture. Sharp lane is gone; rename windows/snapshots to story-vs-firehose semantics or drop the lane breakout entirely. Low urgency — the dead `news.no_sharp_24h` finding has been removed; remaining refs are diagnostic only.

## Auto stress-flip (deferred)

Dropped from the scoring MVP on 2026-04-24. MVP ships Freshness + the binary
`Supports` / `Stresses` feed chip; scoring never writes `user_theses.status`.
The user sees stressing articles in the feed and decides themselves.

**Why deferred — three uncalibrated inputs stacked:**
1. **`confidence` is not a calibrated probability.** The Gemini judge self-reports it; we have never reliability-binned it against our corpus. A threshold of 0.85 vs. 0.90 could swing flip rate by an order of magnitude and we have no way to tell which end is right.
2. **`matched_invalidation` is free text, not verified.** The judge writes prose like *"Brent below $80 for 30+ days"*. The original gate was `IS NOT NULL`, which treats any paraphrase or judge hallucination as a valid invalidation — weaker than it reads. The thesis markdown has a declared list of invalidations; the judge's string is not checked against it.
3. **One row, one flip, no un-flip.** A single judge call could move a thesis to `stressed`, and MVP explicitly does not flip back. High-leverage, user-visible status change on top of inputs 1 and 2.

**Bar for turning it on:**
- [ ] **Confidence calibration pass done** (see `Thesis ↔ News Matching — Deferred` → "Confidence calibration" above). Need ~100+ judged rows and a retuned `SUPPORT_STRONG_CONF` based on hand-labelled precision bins before adding a second floor.
- [ ] **Tighten `matched_invalidation` semantics.** Either (a) judge returns an index into the thesis's declared invalidations list, or (b) we post-check the free-text string against that list with a literal / fuzzy match. Prose-only is not enough.
- [ ] **Soak the chip-only UX for a week.** Watch how often a would-have-flipped article is actually wrong when you see it in the feed. If the chip is reliably "this would have been a bad flip," that is evidence we need the floor higher than the chip's.
- [ ] **Add a second config knob** (`STRESS_FLIP_CONF`, separate from `SUPPORT_STRONG_CONF`) and re-enable the status write in `agents/score_theses.py`. Start stricter than gut: `STRESS_FLIP_CONF >= 0.85` *and* verified invalidation match *and* (ideally) two stressing links within the horizon rather than one.
- [ ] **Decide on un-flip semantics** before turning this on, not after. MVP's "no un-flip, only user resolves" is fine while the flip never happens; once it can happen, a stale-stress mode becomes a real product question.

Chip code can absorb a status read (`user_theses.status='stressed'` → render a red lifecycle badge next to the thesis label) without scoring writing that column, which gives the UI side something to point at for later.

## short_belief on theses (deferred)

Surfaced from the UI gap audit on 2026-04-25 (see `~/hf-ui/UI_BACKEND_GAPS.md`). The home feed and digest both read better with a single declarative line per thesis ("Fed pivot delayed strengthened today") than with the 2–4-sentence `## Core Thesis` paragraph. Deferred because the natural place to write it is the in-chat thesis creation surface (Sage `sharpen-thesis` chip — see `docs/design-thesis-creation.md` Caller #2, NOT STARTED). Until that surface ships, the UI falls back to the title line (`# Thesis: <statement>` — already short) and the digest reads fine.

**Bar for picking this up:**
- [ ] Pick up only when wiring the Sage `sharpen-thesis` chip. Add `short_belief` to the chip prompt in `src/agent/prompt_manager.py` alongside tickers / horizon / invalidations. Constraint: ≤ 18 words, declarative, present tense, no hedging.
- [ ] Storage: inline metadata line `- **Short Belief**: <line>` in the thesis markdown header block (matches the existing `- **ID**:` / `- **Label**:` convention; no YAML frontmatter — none of the 12 existing theses use it).
- [ ] Parser: extend `ThesisDocument` (`src/thesis/docs.py:15`) with `short_belief: str | None = None` and add a regex line in `parse_thesis_markdown()` (line 103). Optional field; callers fall back to `title` when absent.
- [ ] Backfill the 12 existing theses with a one-shot script (`scripts/backfill_short_belief.py`) that prompts Claude with each thesis's title + core_thesis and edits one line in. Idempotent (skip files that already have the line).
- [ ] Update `docs/sop-add-new-thesis.md` template (line 109–155) to include the line.

## Collapse duplicate FX index rows in `instruments` (deferred)

`instruments` has paired rows for the dollar index and yen — `DXY` → canonical `DX-Y.NYB` and `USDJPY` → canonical `JPY=X`. The canonical-collapse already works in `_load_alias_index` (see `src/news/ticker_candidates.py:214`), so the news slate is clean. The remaining duplication is registry-side: two rows per asset, which can confuse downstream consumers that key by raw `symbol` rather than canonical.

**Bar for picking this up:** when a price provider, thesis tailwind calc, or UI ticker chip surfaces the non-canonical symbol where the canonical was expected. Pick one as canonical (whichever the price feed in `src/prices/` reliably resolves) and either (a) drop the redundant row, or (b) keep it but ensure every read path follows `canonical_symbol`.

## Watchlist — Deferred (follow-ups to v1, 2026-06-03)

From `docs/design-watchlist.md` (shipped: `user_watchlist` table, `/api/v1/watchlist` GET/POST/DELETE, agent read path via `load_stored_profile`, `/feed` right rail + `/watchlist` page). Deferred:

- **Agent `update_profile` tool** for chat-driven watchlist edits ("add NVDA to my watchlist"). The `add_symbol`/`remove_symbol` gate in `src/personalization/watchlist.py` is built to be reused when this lands.
- **Rest of `user_preferences`** (sectors/risk/experience/asset_classes) moving from markdown to DB.
- **`user_profile_events` audit log** for watchlist mutations — no reviewer/undo flow demands it yet.
- **Bulk add** (wants onboarding, which isn't built).
- **Drag-to-reorder** (custom ordering): one `sort_order` column + one move endpoint when the product wants it.
- **Per-entry `note`** field.
- **Wiring the right-rail "Trending" section** to `/api/trending` (still mock).

## Structured research handoff between Phase 1 and Phase 2 (deferred)

Surfaced 2026-05-20 during the system-improvements review after the profile-scaffolding spike. Today `ResearchPackage.raw_text` (`src/agent/research.py`) is a serialized tool-call history that Phase 2 consumes as prose via `<research_evidence>`. The response phase re-parses that prose to figure out what to anchor on, what to cite, and what evidence is structured (numbers from `price_summary` / `search_macro`) vs. what is citable narrative (`story_id`s from `search_evidence` / `search_stories`).

Most citation discipline is currently maintained by prompt rules in `_RESEARCH_HANDOFF_RULES` and the `<response_rules>` block (~25 lines of "cite only X, never Y"). The trailing-JSON citation regex in `src/agent/response.py` then has to detect whether the model emitted the right shape. Drift between what Phase 1 actually fetched and what Phase 2 reconstructs is the source of most citation bugs.

**Proposed shape:** replace `raw_text: str` with a typed handoff:

```python
@dataclass
class ResearchHandoff:
    evidence_rows: list[EvidenceRow]      # story_id, headline, relation, confidence, rationale
    price_snapshots: list[PriceSnapshot]  # ticker, last, change_pct, as_of
    macro_facts: list[MacroFact]          # series_id, value, as_of, label
    filings: list[FilingRecord]           # accession, form, primary_document_url, as_of
    web_pages: list[WebPage]              # url, title, snippet
    summary_for_response: str             # short narration the response phase can scan first
    gaps_acknowledged: list[str]          # things research tried and couldn't ground
```

Phase 2's `<research_evidence>` block then renders deterministically from the typed handoff (one template per section), and the trailing `citations` array maps 1:1 to entries in `evidence_rows.story_id` ∪ `web_pages.url` ∪ `filings.primary_document_url` — no regex inference. Phase 2 prompt rules collapse from "remember which sources are citable" to "use the citations array; entries must come from the handoff."

This is independent of personalization but it is the natural prerequisite for several adjacent improvements:

- Reliable citation provenance (the JSON array is constructed from a typed source set, not inferred from prose).
- Cheaper Phase 2 prompts (the structured shape is more compact than the current tool-history dump).
- The same shape is what a future "Phase 2 callback budget" would consume — if Phase 2 ever gets a 1–2-call extra budget for "I'm answering and need X," the handoff format has to be typed for the callback's new rows to merge in cleanly.

**Why deferred — three things have to be true before this is worth the migration cost:**

1. **Citation defect rate has to be high enough to justify the work.** The eval rubric grades citation discipline; pull the last 30 runs of `runs/*/verdict.json` and count the citation-related failures. If <10% of failures cite "wrong source class" or "hallucinated URL" or "missing citation for a numeric claim," the prose handoff is fine.
2. **The Phase 2 prompt has to be stable enough to justify a shape change.** If we're still iterating on the response rules week-to-week, swapping the handoff format under it is gratuitous churn.
3. **Strands tool-call records have to expose what we need.** `tool_call_records` in `ResearchPackage` is already a structured list; the question is whether the rows carry enough metadata to populate the typed handoff without re-parsing prose. Spike: write a `_build_typed_handoff(messages, tool_call_records)` against the current message format on 5 traces; if it round-trips losslessly, the migration is mechanical.

**Bar for picking this up:**
- [ ] Pull the last ~30 graded runs from `~/hf-evals/runs/` and tally citation-shape failures. Need ≥10% to justify the work.
- [ ] Spike `_build_typed_handoff` against 5 real traces; confirm it produces non-empty, well-typed records for `evidence_rows`, `price_snapshots`, and `macro_facts` without prose parsing.
- [ ] Decide the migration cutover shape — flag-flipped Phase 2 that reads either string-or-typed handoff for one release, then drop the string path.
- [ ] Update the eval rubric's citation criterion to reference the typed handoff IDs rather than the prose handoff. The rubric is currently written against the prose shape; a typed handoff invalidates several of its fail markers.
- [ ] Update the AI SDK route — the trailing-JSON citation block in `src/agent/response.py` is currently inferred via `find_trailing_json_block_start`; with a typed handoff it can be emitted directly from the structured citations set rather than re-detected in the stream.
- [ ] Confirm chart agent (`src/agent/chart.py`) consumes whichever shape it needs from `ResearchPackage`; today it reads `tool_call_records`, which the typed handoff preserves.

Independent of personalization, but if both this and Phase 1 personalization land, the read path becomes much cleaner: Phase 1 sees `<user_holdings>` + tool definitions + question; Phase 1 emits a typed handoff; Phase 2 sees `<user_profile>` + typed handoff + question. No prose intermediate.

## Social-topic dedupe: upgrade title-overlap to embedding similarity (2026-06-04)

`find_live_social_topic` matches by normalized-title token overlap (≥0.55).
Observed live on the first same-day AVGO re-run: Grok re-phrased the same
earnings-selloff discussion as "AVGO Drops on Mixed Guidance" vs the live
"AVGO Plunges 12% Post Earnings" — overlap 0.25, so a duplicate row was
inserted instead of a refresh (cleaned up by hand: story_1318 removed, the
newer phrasing kept). Titles are not stable run-to-run, so the dedupe
under-matches exactly when the discussion stays hot across runs.

This is the design's pre-registered watch item
(`docs/design-social-ingestion.md` § watch items). Fix when refresh-vs-new
ratios in `social_run` metrics confirm it recurs: compare embeddings of
title+summary against live topics (the clustering pass-2 machinery exists),
threshold ~0.80 cosine, same cross-ticker-skip / same-ticker-refresh policy.

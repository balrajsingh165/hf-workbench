# Design: Thesis Creation System

**Status:** `discover_thesis()` (single-context) and `discover_story_theses()` (multi-candidate story proposals) shipped, including the story-detail surfacing (`build_story_suggestions`, frontend `suggestions[]`). Interactive user-facing creation will live as a Sage chip in the AI SDK chat surface — no separate CLI is planned.
**Depends on:** Matching pipeline (shipped), scoring (shipped), news ingestion (shipped).

---

## Problem

Theses today are hand-written by humans following `docs/sop-add-new-thesis.md`. There is no automated path from "the system sees something interesting" to "a thesis exists for users to track." This blocks two things:

1. **Cold start.** New users see an empty Discover section. Nothing to adopt until a human curator writes something.
2. **Responsiveness.** A strong emerging narrative (e.g., "hyperscaler ASIC programs are converging on Broadcom") should become a trackable thesis within hours of the news landing, not days.

The `design-macro-context-seeded-theses.md` proposal addresses cold start with hand-curated seeds. This design addresses the dynamic, automated path.

---

## Core Primitive: `discover_thesis`

One shared function. Every surface that might produce a thesis calls it.

```python
# src/thesis/discover.py

def discover_thesis(
    context: str,
    db_path: Path,
    *,
    similarity_threshold: float = 0.80,
    overlap_top_k: int = 20,
) -> DiscoverResult:
    """
    Context-in, thesis-out.

    context:           Raw unstructured text. Could be anything —
                       a synthesized story, a chat transcript,
                       a research report, multiple articles concatenated,
                       a user question + macro frames. The model sees
                       the full picture and finds connections.

    db_path:           Path to hf.db. Used to retrieve the nearest
                       existing theses via embedding search (not a
                       full corpus dump — see Overlap Retrieval below).

    Returns:           DiscoverResult with either a new thesis, a pointer
                       to an existing thesis that already covers this
                       belief, or None (no thesis-worthy belief found).
    """
```

### `DiscoverResult`

```python
@dataclass(slots=True)
class DiscoverResult:
    action: Literal["new", "existing", "none"]

    # Set when action == "new"
    thesis: ThesisDocument | None

    # Set when action == "existing"
    existing_thesis_id: str | None
    similarity_score: float | None

    # Always set when action != "none"
    rationale: str | None          # why this belief is thesis-worthy (or why existing covers it)
```

### Why one function

Every caller — news ingestion, chatbot, research agent, future Discover UI, bulk seeding scripts — wants the same thing: "given this blob of context, is there a thesis here, and is it new?" Splitting this across callers guarantees drift in quality bar, dedupe logic, and thesis formatting.

---

## How It Works

### Step 0 — Overlap Retrieval (context → nearby theses)

Before calling the LLM, retrieve the theses most likely to overlap with this context. Embed the context text via `embed_content` (task_type `RETRIEVAL_QUERY`), then `search_dense` against `thesis_match_chunks` with `top_k=overlap_top_k` (default 20). This returns the 20 nearest thesis chunks — typically covering 10–15 distinct theses.

Only these retrieved thesis statements are passed to the LLM prompt (Step 1). This is critical for scaling: at 12 theses the full list fits; at 5,000 it doesn't, and model behavior drifts as the prompt grows. Retrieval keeps the prompt stable and relevant regardless of corpus size.

The same embedding result is reused in Step 2 for the deterministic similarity check — no extra API call.

### Step 1 — LLM Discovery (context → candidate)

One call to a powerful model with the context + the retrieved nearby theses (from Step 0, not the full corpus). The prompt does NOT ask for a pre-structured belief or direction — it asks the model to read the context and decide if a durable, actionable market conviction is hiding in it that is not already covered by the nearby theses.

The model returns structured JSON:

```json
{
  "has_thesis": true,
  "thesis_statement": "Competing hyperscaler AI ASIC programs make Broadcom the dominant custom silicon partner.",
  "label": "ASIC arms race",
  "tickers": [
    {"symbol": "AVGO", "direction": "bullish", "rationale": "..."},
    {"symbol": "NVDA", "direction": "bearish", "rationale": "..."}
  ],
  "core_thesis": "Google's TPU 8t/8i split marks the first time...",
  "invalidation_conditions": [
    "AVGO AI revenue growth decelerates below 25% YoY in two consecutive reports.",
    "A major hyperscaler announces moving ASIC design away from Broadcom."
  ],
  "horizon_days": 42,
  "rejection_reason": null
}
```

If `has_thesis` is false, `rejection_reason` explains why (not finance-relevant, too reactive, no clear tickers, no testable invalidation). The function returns `DiscoverResult(action="none")`.

### Step 2 — Similarity Check (candidate → keep / merge / drop)

If Step 1 produces a candidate, embed the `thesis_statement + core_thesis` and run `search_dense()` against `thesis_match_chunks` (the existing thesis embedding index).

| Top match score | Action |
|---|---|
| **> similarity_threshold (0.80)** | Return `action="existing"` with the matched thesis ID. The caller can recommend the existing thesis instead of creating a duplicate. |
| **≤ similarity_threshold** | Proceed to Step 3. |

This is the same `search_dense` in `src/thesis/match_index.py` — no new infrastructure.

### Step 3 — Write (candidate → thesis)

Generate the next `thesis_XXX` ID, write the markdown to `global/theses/`, insert the DB row with `owner_count=0`.

No separate `thesis_candidates` table — candidates live in the same `theses` table. But system-generated theses are **not immediately recommendable**. They enter with `review_status='candidate'` and must be promoted to `active` before appearing in any user-facing surface. Manual-origin theses skip this and enter as `active` directly.

This is the smallest possible safety net: one column, not a workflow. A bad LLM output becomes a `candidate` row and a markdown file, but never reaches recommendations, never appears in Discover, and never gets backfilled until promoted.

---

## Schema Changes

### `theses` table — add `origin` column

```python
"theses": [
    ("id",               "TEXT PRIMARY KEY"),
    ("origin",           "TEXT NOT NULL DEFAULT 'manual'"),    # manual | system | chat
    ("source_context",   "TEXT"),                              # what triggered creation: story_id, chat turn, etc.
    ("review_status",    "TEXT NOT NULL DEFAULT 'active'"),    # candidate | active | rejected
    ("owner_count",      "INTEGER NOT NULL DEFAULT 0"),
    ("created_at",       "TEXT NOT NULL DEFAULT (datetime('now'))"),
],
```

- `origin`: who created this thesis. `manual` = human via SOP. `system` = automated discovery from news ingestion. `chat` = conversational surface (Sage). Extensible — add values as new surfaces ship.
- `source_context`: free-text breadcrumb. For system-origin theses: the `story_id` that triggered discovery. For chat-origin: a conversation reference. For manual: null. Not indexed, not queried in product paths — exists for debugging and auditing only.
- `review_status`: the quality gate.
  - `active` — visible in recommendations, eligible for user adoption. Default for `origin='manual'`.
  - `candidate` — written to DB and markdown, but invisible to users. Default for `origin='system'` and `origin='chat'`. Promotion to `active` happens after automated validation (see Candidate Promotion below).
  - `rejected` — failed validation. Kept for debugging, never shown to users.

### No other schema changes

`thesis_match_chunks`, `thesis_story_links` work as-is. An unowned thesis (`owner_count = 0`) is identical in shape to a user-owned one — it just has no `user_theses` rows, and because the score lives on `theses` (not `user_theses`), an unowned thesis is fully scoreable. The Scoring Agent scores every *live* thesis — non-resolved owners plus active proposals — and `run_post_creation_pipeline` scores a new thesis the moment it is created.

---

## Callers

### 1. News Ingestion (automated, batch) — multi-candidate

Wired into `agents/route_news_clusters.py` (`_match_and_maybe_discover_for_story`)
after `write_cluster_story` succeeds. A single story can be read several ways, so
ingestion calls the **multi-candidate** entry point `discover_story_theses` — not
the singular `discover_thesis` — which yields **0–3 distinct, strong proposals**
per story. For each promoted story:

```
match_thesis_for_story(story)          # link existing theses to the new evidence first
covered_angles = titles of matched theses
if story has tagged tickers:           # the one cheap pre-filter (NO theme_tag gate)
    results = discover_story_theses(story_markdown, DB_PATH,
                                    source_context=story_id,
                                    covered_angles=covered_angles)
    for r in results where action == "new":   # sequentially:
        run_post_creation_pipeline(r.thesis)   # embed → backfill → score → promote
```

Key behaviors:

- **No skip on strong match.** A story that strongly supports an existing thesis
  no longer short-circuits discovery. The matched theses' titles are passed as
  `covered_angles` so the generator proposes only *new, distinct* angles (and
  links the story to the existing thesis for any `action="existing"` candidate).
- **No `theme_tag` gate.** `theme_tag="other"` only means "no tracked macro
  narrative" — it was silently killing strong single-name seeds (an earnings
  surprise, etc.), so it was dropped. The **one remaining pre-filter is the
  ticker gate** (`≥1` tagged ticker): the promotion gate rejects ticker-less
  theses anyway, so a ticker-less story can only burn a Pro call. This makes the
  firehose ticker-tagger's precision load-bearing (tracked separately).
- **Strength is inlined, not a separate critic.** One Gemini 3.1 Pro (medium)
  call emits a candidate only if it clears the strength rubric in the prompt
  (`DISCOVER_STORY_SYSTEM_PROMPT`); the objective backstop stays the structural
  promotion gate. Prefer fewer — zero is a common, valid outcome; never pad.
- **Sequential persist+promote** gives candidate-vs-candidate dedup for free:
  each new thesis is embedded into `thesis_match_chunks` before the next
  promotes, so the promotion similarity recheck sees already-persisted siblings.

The route layer only calls discovery on finished stories; raw `news` rows never
trigger thesis discovery directly. Duplicate gating lives inside
`discover_story_theses()` (candidate-vs-existing) and the post-creation promotion
check (candidate-vs-sibling + structural). Runs inside the existing pipeline
scheduler cycle; cost scales with the number of tagged stories. `discover_thesis`
(singular) is retained as a thin wrapper for single-context callers (re-promotion,
future chat).

### 2. Sage chat — interactive user-facing creation (not started)

The user-facing creation path lives inside the existing Sage chat surface (`POST /api/v1/ai-sdk/chat/completions`), not in a standalone CLI. This avoids a parallel LLM stack (Bedrock+Strands vs Claude API) and reuses Sage's prompt assembly, tool layer, persistence, and streaming.

**Shape (when built):**
- New `ChipId` value (e.g. `"sharpen-thesis"`) added to `src/agent/models.py`.
- Chip prompt block in `src/agent/prompt_manager.py` enforcing the same quality bar as the discovery gate: one declarative sentence, specific Yahoo tickers, bullish/bearish per ticker, 2–3 concrete invalidation conditions. Horizon is **inferred silently** from the belief (stored on `theses.horizon_days`); never challenge the user for it.
- A Strands tool that wraps `discover_thesis`'s write path. On user confirmation, writes markdown + DB row with `origin='chat'`, `review_status='active'` (the user explicitly approved it), then calls `run_post_creation_pipeline()`.

**Origin distinction:** `origin='manual'` (SOP) and user-confirmed `origin='chat'` theses enter as `active` directly. `origin='system'` theses enter as `candidate` and must clear the promotion bar. Same markdown format, same post-creation pipeline, different trust level.

**Done when:** Three vague chat inputs ("tech is hot", "oil will spike from Iran", "Fed will hold longer") each produce a thesis a human reviewer calls sharp — specific, testable, non-obvious invalidation conditions.

### 3. Future Surfaces

Same function. A "What am I missing?" feature that runs `discover_thesis` against the user's recent reading history. A bulk seeder that processes a week of unmatched articles. All callers get the same quality bar and dedupe.

---

## Surfacing: Story Proposals (shipped)

Discovered system theses are surfaced on the **story detail page** (`/feed/[id]`)
as read-only proposals — global, not bound to any user. `build_story_suggestions(conn, story_id)`
in `api.py` returns up to 3 of them:

```sql
SELECT t.id, t.horizon_days, l.confidence
FROM thesis_story_links l JOIN theses t ON t.id = l.thesis_id
WHERE l.story_id = ?
  AND t.review_status = 'active' AND t.origin = 'system'
  AND l.relation = 'supports' AND l.confidence >= 0.85   -- STORY_SUGGESTION_MIN_CONF
ORDER BY l.confidence DESC LIMIT 3
```

There is **no user predicate** — these are global proposals, surfaced before any
adoption. Each row resolves to `{thesisId, belief, tickers, direction, horizon}`
(belief = thesis title from markdown; direction = dominant ticker direction).
`build_feed_stories` attaches the result as `suggestions[]` on each story item, so
both `/api/home` and the detail page carry it. The frontend renders the
suggestions as read-only cards (no `[+ Track]` — adoption is a later, user-bound
feature). Only `active` system theses appear; `candidate` proposals stay hidden
until they clear promotion.

---

## Recommendation Filtering (Discover Section)

When showing recommendable theses to a user:

1. Filter to theses this user does not already own AND that are promoted: `review_status = 'active' AND NOT EXISTS (SELECT 1 FROM user_theses WHERE user_id = ? AND thesis_id = t.id)`. Do NOT use `owner_count = 0` — that would hide a thesis from everyone once one user adopts it. `owner_count` is a popularity signal, not an ownership predicate.
2. For each candidate thesis, compute embedding similarity against each of the user's theses via the existing match index.
3. If max similarity > 0.80 with any of the user's theses: **skip** — they already track this belief.
4. Otherwise: show it. If similarity is 0.60–0.80, optionally label it "related to [their thesis label]."

This is a **read-time, per-user filter**. The thesis stays in the global pool for other users.

---

## Thesis Quality Gate (Prompt Spec)

The LLM discovery prompt enforces the same quality bar as `docs/sop-add-new-thesis.md`. Key constraints baked into the system prompt:

1. **One declarative sentence.** ≤ 20 words. No hedging. No "might" or "could."
2. **Specific tickers.** Yahoo canonical symbols. At least one, max eight.
3. **Clear direction.** Each ticker is bullish or bearish. No neutral-only theses.
4. **Durable theme, not a one-off event.** The belief must rest on a force that keeps acting over the holding horizon — a sector or macro trend, an ongoing geopolitical situation, a multi-month supply-chain dynamic, a rates/inflation/currency regime, or a real shift in customer behavior. A single company's own one-off catalyst is rejected: M&A/buyouts/takeovers, IPOs, a product launch or promo, one quarter's earnings or guidance, a trial readout, a regulatory action, or a strategic pivot. These get priced in once and stop moving the stock. A company may be named only as a second-order beneficiary of a durable theme ("a SpaceX IPO pulls capital into the space sector"), never as the subject of its own idiosyncratic story ("Greg Abel's first buyout lifts Berkshire"). "Oil went up today" is rejected; "OPEC+ surplus pushes Brent below $60" is accepted.
5. **Concrete invalidation conditions.** 2–3 specific, testable conditions. Not prose — events that can be observed.
6. **Horizon.** 10–120 days, inferred from the belief and stored on `theses.horizon_days` — shorter for tactical catalyst trades, longer for structural macro. Inferred and clamped at creation (`clamp_horizon`, default 45); never asked of the user or shown in any UI.

The prompt receives the top-K nearest existing thesis statements (retrieved in Step 0 via embedding search, not the full corpus) so the model can avoid semantic overlap before we even hit the deterministic embedding check.

---

## Candidate Promotion

System-generated theses enter as `review_status='candidate'`. Promotion to `active` is automated — no manual approval — but must clear a validation bar:

1. **Structural check.** The markdown parses cleanly via `parse_thesis_markdown`: has a title, at least one ticker with direction, a Core Thesis section, and ≥2 invalidation conditions. If parsing fails → `rejected`.
2. **Similarity re-check.** Re-run `search_dense` against the current thesis index (which may have changed since Step 2 if multiple theses were created in the same pipeline cycle). If top match > 0.80 → `rejected` (near-duplicate slipped through).
3. **Backfill sanity.** Run `match_story_for_thesis` with a 14-day window. If zero story links land, the thesis is too niche or too vague to be useful right now — keep as `candidate`, don't promote. The re-promotion sweep (below) re-evaluates it later.

If all three pass → set `review_status='active'`. The thesis is now recommendable.

The promotion step runs immediately after post-creation pipeline (embed → backfill → score → promote). It's a few DB reads and one embedding query — cheap.

### `review_status` lifecycle

`review_status` is the **discovery/quality** axis of a thesis, distinct from the user-facing status (`active`/`stressed`/`resolved`) that the scoring agent owns. A system-discovered thesis moves through:

```
candidate ──(gates pass + ≥1 story link)──▶ active
    │
    ├──(fails structural / similarity gate)──▶ rejected
    └──(zero links past max-stale-days)──────▶ rejected
```

- **candidate** — well-formed and non-duplicate, but not yet earning news support. Embedded in the match index and recommendable to no one; it just waits.
- **active** — cleared all three gates. Recommendable in Discover.
- **rejected** — terminal. Failed a gate, or aged out with no evidence.

Manual (`origin='manual'`) and chat (`origin='chat'`) theses are born `active`; only `origin='system'` theses pass through `candidate`.

### Re-promotion sweep

Promotion runs **once** at creation, but the ingest-time matcher (`match_thesis_for_story`) keeps linking new stories to candidates afterward — there is no `review_status` filter on the index it searches. So a candidate born with zero links can earn supporting stories days later and would otherwise sit stuck forever (this trips the `thesis.stuck_candidates` health alarm).

`agents/repromote_candidates.py` closes the gap. It re-runs the promotion gates over every `candidate` against the links **already in `thesis_story_links`** — no re-embed, no re-backfill, since ingest matching already populates them. `repromote_candidate()` in `discover.py` reuses the shared candidate-promotion gate with explicit evidence presence and adds the terminal aging rule: a candidate still link-less past `--max-stale-days` (default 30) is `rejected` so dead candidates don't accumulate.

It runs as its own scheduler job (`hf_repromotion`, default every 6h) in the `hf-pipeline` process — **independent of the main pipeline cycle and the firehose**, so a failure in one never blocks the others. It also runs once at boot (after the pipeline writes fresh ingest links). Knobs: `HF_REPROMOTION_INTERVAL_HOURS`, `HF_REPROMOTION_MAX_STALE_DAYS`, `HF_REPROMOTION_DISABLED`. Trigger manually: `uv run python -m agents.repromote_candidates`.

---

## Post-Creation Pipeline

A newly created thesis is not useful until it has signals and scores. The post-creation steps are identical to `sop-add-new-thesis.md` Steps 5–7, automated:

1. **Embed into match index (incremental).** Build chunks for the new thesis via `build_thesis_chunks`, embed them via `embed_content`, and INSERT into `thesis_match_chunks`. Append-only — do not call `rebuild_thesis_match_index` (that's a dev script that wipes all rows).
2. **Backfill story links.** `match_story_for_thesis(thesis_id, window_days=14)` populates the signal timeline.
3. **Score.** `score_theses` computes freshness + tailwind. A thesis with no backfill matches scores 0 — that's fine.

All three steps use existing shipped code. No new agents needed.

---

## What This Does NOT Do

- **Auto-assign theses to users.** System-generated theses have `owner_count = 0`. They appear in Discover. The user adopts or ignores.
- **Replace human thesis creation.** Users and curators can still create theses manually via the SOP (`origin = 'manual'`) or via the Sage chat surface once the chip ships (`origin = 'chat'`).
- **Multi-article aggregation in v1.** Each `discover_thesis` call processes one context blob. If a durable thesis requires connecting three separate news stories, it won't emerge from a single-article gate. That's a known limitation — the chatbot/research surface is better positioned for multi-source synthesis because the context blob is richer. V2 could add a batch pass over recent unmatched articles.
- **Retire link-less *active* theses.** The re-promotion sweep retires *candidates* that never earn evidence, but a thesis that promoted to `active` and later goes quiet (signals dry up) is the scoring agent's concern via freshness decay, not this pipeline's. Hard deletion of stale `origin='system' AND owner_count=0` theses remains out of scope.

---

## File Layout

```
src/thesis/
  discover.py          # discover_thesis() + discover_story_theses() (multi-candidate) — shipped
  docs.py              # ThesisDocument, parse/write — shipped
  match_index.py       # embedding index, search_dense — shipped
  scoring.py           # compute_freshness, compute_tailwind, chip_for — shipped
  prices.py            # Mesh price wrapper, ticker canonicalization — shipped
  story_links.py       # load_links_for_thesis — shipped
agents/
  route_news_clusters.py # wires discover_story_theses after write_cluster_story — shipped
  repromote_candidates.py # re-promotion sweep (hf_repromotion job) — shipped
  score_theses.py      # freshness + tailwind + composite — shipped
api.py                 # build_story_suggestions() → story `suggestions[]` payload — shipped
src/agent/
  prompt_manager.py    # add `sharpen-thesis` chip prompt — NOT STARTED
  models.py            # add `sharpen-thesis` ChipId — NOT STARTED
  tools.py             # add Strands tool wrapping discover_thesis write path — NOT STARTED
```

---

## Testing: System-Generated Thesis Quality

System-generated theses go directly into the global pool (gated by `review_status`). There is no manual approval step. This means the LLM gate + similarity check + candidate promotion pipeline is the entire quality bar. **Before shipping, test thoroughly against these scenarios:**

### Scenarios that must produce `has_thesis: false` (gate rejects)
- **Non-finance article.** Entertainment, sports, celebrity news. The Michael Jackson biopic article (`news_051`) must not produce a thesis.
- **Finance-adjacent but no clear ticker.** "The economy is uncertain" — directionally vague, no actionable position.
- **Pure headline reaction.** "Stock X went up 5% today" — no durable belief, just a price move.
- **Single-company one-off event.** An M&A/buyout, IPO, product launch, one earnings or guidance print, a trial readout, or a regulatory action against one company, with no durable theme behind it. "GlobalFoundries' acquisition lifts its stock" must reject; a story whose only content is one buyout (e.g. Berkshire acquiring Taylor Morrison) must reject unless a durable sector theme survives the deal.
- **Already covered.** Article about Fed rate decisions when `thesis_001` (Fed pivot delayed) already exists. The model should recognize overlap from the existing thesis titles passed in the prompt.

### Scenarios that must produce `has_thesis: true` but get caught by similarity check
- **Near-duplicate framing.** Article about nuclear power demand → generates a thesis very close to `thesis_005` (nuclear renaissance). Similarity check should return `action="existing"` with `thesis_005`.
- **Same belief, different tickers.** "AI infra spend broadens to power" — close to `thesis_003` (AI power bottleneck). Should match despite different ticker emphasis.

### Scenarios that must produce a genuinely new thesis
- **Uncovered sector.** An article about rare earth supply chain disruptions when no existing thesis covers rare earths.
- **Novel connection.** Article combining two themes (e.g., GLP-1 drugs + insurance cost impact) that no existing thesis addresses.

### Scenarios that pass the gate but fail candidate promotion
- **Parses but is structurally weak.** Model returns a thesis with no invalidation conditions or no tickers. `parse_thesis_markdown` should fail → `rejected`.
- **Too niche for current news.** Thesis about an obscure micro-cap with no matching news in the 14-day window. Stays `candidate`, not promoted.
- **Race condition duplicate.** Two articles in the same pipeline cycle generate near-identical theses. The second one's similarity re-check catches it.

### Multi-candidate story scenarios (`discover_story_theses`)
- **Single-topic story → 0–1 proposals.** A narrow earnings beat must not be padded to 3.
- **Multi-angle story → 2–3 distinct proposals.** e.g. a chip-export-control story → (supplier bull) + (macro/FX read) + (contrarian). The surviving set must be mutually dissimilar (sequential-promote sibling dedup).
- **Strong-match story still yields a new angle.** A story that strongly supports an existing thesis must (a) link to it and (b) still produce a *different* angle if one exists — and **zero** new theses if it doesn't (`covered_angles` passthrough).
- **`theme_tag="other"` story with tickers → proposals.** A registry-backed single-name catalyst bucketed as `"other"` (e.g. Apple's record Q2, `AAPL`) must reach discovery, not be skipped. Regression guard for the dropped `theme_tag` gate.
- **No-ticker story → zero**, via the one ticker pre-filter.

### Quality bar reminder
Weak suggestions are worse than no suggestion. A system-generated thesis that says "markets may be volatile" or "tech companies face headwinds" actively damages trust in the Discover section. The gate prompt must enforce the same quality bar as `docs/sop-add-new-thesis.md` — one declarative sentence, specific tickers, concrete invalidations, durable beyond the headline. When in doubt, reject.

---

## Resolved Decisions

1. **Model: `gemini-3.1-pro-preview` with `thinking_level="medium"`.** Discovery requires finding non-obvious connections in dense financial text — Flash isn't enough. Pro with medium reasoning balances quality and cost. The call uses `generate_text_with_retry` from `src/clients/gemini.py` with `model=GEMINI_3_1_PRO_PREVIEW`.
2. **Incremental index update, not rebuild.** `rebuild_thesis_match_index` is a dev convenience script that wipes and recreates all chunks — never call it in the live pipeline. The post-creation step must append-only: embed the new thesis's chunks and INSERT them into `thesis_match_chunks` without touching existing rows. Same `embed_content` + INSERT pattern, scoped to the new thesis.
3. **Cleanup cadence for unowned theses.** Decide when the pool gets noisy. Starting proposal: delete `origin='system' AND owner_count=0` theses older than 30 days via a simple cron/script. Not blocking for v1.

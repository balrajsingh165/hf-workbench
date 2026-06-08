# Design: Profile as Prompt Scaffolding

**Status:** v0 shipped (2026-05-22). Read-only personalization is live behind `HF_PERSONALIZATION=on`.
**Depends on:** matching pipeline (shipped), scoring (shipped), `user_theses` (shipped), `entity_tickers` (shipped).
**Scope:** This is the single personalization design doc. It owns the read path (`<user_profile>` block, derived signal from theses, the three voice rules), the write path (storage schemas, the `update_profile` tool surface, the out-of-band `discover_trait` worker), and the feed-personalization pieces that share the same `user_preferences` row (source-tier visibility, the relevance score, the issuer rate-limit).

### Current status (2026-05-22)

v0 read-only personalization shipped: `src/personalization/` package with a `profile.md` parser, a derived-profile builder (`derive_profile` from `user_theses ⨝ entity_tickers`), and a conditional `<user_profile>` block renderer. All three personalization rules are in the Phase 2 system prompt. The pipeline scheduler was refactored into `src/pipeline/` with cooperative SIGTERM shutdown. A pre-existing bug was fixed where Phase 2 (response) never received conversation history, breaking follow-up questions like "explain the last sentence." Four seeded users exist (user_1 AI-hardware, user_2 energy/gold, user_3 rates/duration opposite-profile, user_4 sparse/degraded crypto). A 10-scenario paired eval (baseline vs. personalized) produced 5 wins, 4 no-regressions, 1 mixed — clearing the design's pass bar. The feature is gated on `HF_PERSONALIZATION=on`; production runs with it off by default. No new schema tables, no LLM calls in the read path, no write path, no discovery, no decay.

> **No personas, no archetypes, no segmentation.** The product premise is "a live expert evolving around every user", not a classification engine that drops users into one of N buckets on day 1. This design rejects persona archetypes, persona seeds, and any user-facing persona label outright. A real user is often a mix of preferences, or has no clear preference at all on a given dimension; forcing a single persona claim onto that reality is the failure mode that turns "feels read" into "feels mislabeled".

---

## Before the personalization feature

 The result, visible in the hf-evals run `runs/20260520_174622`:

- A user with NVDA on their watchlist asks "what's up with nvda" and gets a third-person sell-side note that opens "Earnings are today (May 20)." instead of "Your NVDA is into tonight's print at ~$225."
- A user with an AI-hardware-concentrated watchlist asks "where should I put cash" and gets a textbook tour of Treasuries / equities / gold instead of an anchored view ("your book is concentrated AI hardware; cash here is either adding to that or hedging it").
- A user with no rate-sensitive exposure asks "so what" on a Fed-independence story and gets a 3-way menu of hypothetical exposures instead of the read that fits their actual book.

Two failures cause this:

1. **No profile read path.** `users/{id}/profile.md` exists with watchlist + sectors + style; nothing loads it.
2. **No model of profile as an *evolving* artifact.** Even a baseline "load JSON, render into prompt" design treats profile as data the user fills via onboarding and the agent occasionally mutates via a tool call. There is no contract for what to do when the profile is sparse, when it is contradicted, when the user's *behavior* (the theses they track, adopt, close) implies things the profile doesn't say, or when a turn should not trigger any discovery at all.

The product premise is much higher than "load a JSON blob into a prompt." The system should feel like an expert who has been working with this user for months — has watched them open and close positions, knows their style well enough to skip context they don't need, knows their constraints well enough to silently avoid bad suggestions, and gets sharper over time without ever announcing that it's learning.

---

## North star

A profile is **prompt scaffolding**, not a content engine.

This phrase carries the entire design. Four implications, each load-bearing:

1. **Profile affects *how* an answer is shaped, not *what* is answered.** Second-person framing for held tickers. Skipping context the user obviously already has. Respecting stated constraints. Anchoring open-ended advice. It does **not** mean re-routing every question through the profile. Pure factoids ("what is the SRF") stay generic. Forced relevance ("since you hold NVDA, here's a take on the SRF…") is a hard failure.

2. **No buckets. No archetypes. No personas.** A persona is a classification engine. The product is a live expert. These are different products. Persona seeds work for theses because a thesis is genuinely shared — many users can adopt the same belief and the belief is the same belief. A profile is irreducibly per-user. A real user is often a mix of preferences, or has no clear preference at all on a given dimension. Forcing a single persona claim onto that reality is the failure mode that turns "feels read" into "feels mislabeled".

3. **Profile is sparse by default and grows by accretion.** Most slots will be empty for most users. The prompt must render gracefully at every level of completeness — from zero-knowledge cold start to two-month-mature mid-conversation refinement. The agent must never wait for a complete profile to feel personal, and must never invent traits to fill gaps.

4. **When in doubt, render less.** A wrong personalization is much worse than a generic answer. Wrong-by-confident-inference reads as creepy and damages trust in everything else the system says. The prompt-rendering layer must be biased toward dropping uncertain slots rather than narrating them. This applies especially on day 1, when the system has the least evidence and the user has the highest sensitivity to overreach.

---

## What a profile actually is

### Anti-definition first

A profile is **not**:
- A risk-tolerance enum the agent name-drops.
- A demographic record.
- A persona / archetype / segment.
- A stable identity the model commits to.
- A complete object. Most slots stay empty for most users for a long time.

### Working definition

A profile is a **sparse, per-user, evolving set of trait slots with provenance**, plus a **derived view** computed at read time from the user's tracked theses.

Two distinct surfaces feed the same `<user_profile>` block in the Phase 2 system prompt. The diagram below is **the read path only** — what gets fetched, from where, at the moment the prompt is built. How those underlying rows get populated in the first place is a separate concern, covered in the next section ("How profile fields are collected, discovered, and updated").

```
                build_phase2_system_prompt(user_id, ...)
                              │
                              ▼
                ╭─────────────────────────────╮
                │   <user_profile> block      │
                │   (rendered conditionally,  │
                │    may be omitted entirely) │
                ╰──────────────┬──────────────╯
                               │
            ┌──────────────────┴──────────────────┐
            │                                     │
      STORED SLOTS                          DERIVED SLOTS
   (read at prompt-build time)         (computed at prompt-build time;
                                          never stored, never cached
                                          beyond the request)
            │                                     │
            ▼                                     ▼
   ╭──────────────────╮             ╭────────────────────────────╮
   │ user_preferences │             │ user_theses                │
   │ WHERE user_id=?  │             │   ⨝ entity_tickers         │
   │ (one SELECT)     │             │   (filtered to entity_     │
   ╰──────────────────╯             │    type='thesis')          │
                                    │ + last-N user_theses       │
                                    │   mutation events          │
                                    ╰────────────────────────────╯
```

Stored slots have provenance per slot — `source`, `confidence`, `last_confirmed_at`, `last_contradicted_at`, and an `evidence` quote for the chat-inferred ones. Derived slots have no storage; they are recomputed on every request. The renderer applies the four-shape conditional logic (EMPTY / THESES-ONLY / STORED-ONLY / RICH) and the first-day-credibility filters (confidence floor, sparse-profile fallback) before emitting the final block.

---

## Slot registry

The slot registry is the single source of truth — schema validation, prompt rendering, and tool surface all read it. Lives in `src/personalization/slots.py`.

### Stored slots (in `user_preferences`)

Deliberately small. Each slot has either an unambiguous structural definition or is excluded. Narrative / qualitative impressions are handled separately via `agent_notes` (below) — observation-only, not loaded into the prompt.

| Slot | Type | Sources | Influence on voice |
|---|---|---|---|
| `experience` | enum {beginner, intermediate, advanced} | onboarding, chat-inferred (rare) | jargon density only — never recite |
| `risk_tolerance` | enum {conservative, moderate, aggressive} | onboarding, chat-inferred | sizing language; never quote back |
| `asset_classes` | list[asset_class] | onboarding, chat-inferred | tool-call routing in Phase 1 |
| `sectors_of_interest` | list[CANONICAL_SECTORS] | onboarding, chat-inferred | which sectors get the "your" framing |
| `watchlist` | list[symbol] | onboarding, watchlist edits, chat-inferred | second-person framing for these tickers |
| `excluded_strategies` | list[free text] | chat-inferred (primary) | hard exclusion of suggestions |
| `agent_notes` | text (≤1000 chars), observation-only | chat-inferred | NOT rendered into any prompt in v1. Model writes free-form impressions of the user; we observe over the first cohort of users to see if a useful, non-cringey pattern emerges. Promotion to in-prompt is a separate decision gated on observed quality. |

**Deliberately excluded in v1:**

- **`horizon`** — a real user has mixed horizons across positions (a DCA core + a 2-week swing on one name). Forcing a single global enum either lies or pushes the user to over-commit. Re-evaluate as a per-ticker or per-thesis field later, not as a global slot.
- **`recent_concerns`** — too ambiguous; the line between "a concern the user actually has" and "a thing the user mentioned once" is too noisy for the model to draw reliably.
- **`trading_style_narrative` / `goals_narrative`** — collapsed into `agent_notes` as a single field, and even that is observation-only for v1. We do not have evidence yet that any narrative slot is useful in production. Ship the observation pipeline; do not ship the prompt injection. Decide after we have early-user data.

### Derived slots (computed; not stored)

The derived layer surfaces only **plain behavioral facts** — things that are true by direct observation of `user_theses` and related tables. It does **not** emit classifications, summary scores, or persona labels. A user's actual book is usually a mix; reducing it to a single tag ("concentrated AI investor", "macro-leaning trader") is the exact mistake the north star rules out.

| Slot | Computation |
|---|---|
| `implicit_watchlist` | `union(t.tickers for t in active user_theses) − explicit_watchlist` |
| `implicit_sectors` | `union(t.sectors for t in active user_theses) − explicit_sectors_of_interest` |
| `recent_thesis_activity` | last ~5 thesis events (adopted, closed_correct, closed_wrong, let_decay) with timestamps |
| `engagement_recency` | days since last `user_theses` mutation |

**Deliberately excluded — these all smell like personas in disguise:**

- `concentration_score` — a single number that implies the user *is* a concentrated investor. Same user with 4 AI theses + 2 macro theses is concentrated *today* and balanced *next month*. The number creates a stable identity claim where there isn't one.
- `macro_lens` — picking a "top frame" forces a classification onto a multi-frame book.
- `inferred_horizon` — already excluded as a stored slot for the same mixed-horizon reason; the derived form has the same problem and is dropped.
- `conviction_pattern` aggregate stats ("3 active, 1 closed_correct") — narrating the user's track record back at them is exactly the recite-as-data antipattern. The raw recent events render as breadcrumbs the model can use silently; the aggregate label does not.

Derived slots render with an `(observed from your tracked theses)` annotation. The annotation does two things: (a) tells the model these are facts about behavior, not user-stated preferences, so it weights them as observations not declarations; (b) makes it harder for the model to quote them back at the user as if the user had said them.

### Why slots, not free narrative

A free-form profile blob is hard to validate, hard to render selectively, hard to decay, and hard to update without overwriting. Slots give us per-field provenance, per-field rendering rules, per-field decay timers, and per-field promotion gates. The one narrative-shaped field in v1 (`agent_notes`) is observation-only and never enters a prompt; if it earns its way in later, it does so as a single, capped, well-defined slot with its own influence rule — not as an open blob.

---

## How profile fields are collected, discovered, and updated

The previous section was about the *read path* — what gets fetched at prompt-build time. This section is about the *write path* — how the `user_preferences` row gets populated and mutated in the first place. Stored slots have four independent write surfaces. Derived slots have none (they are recomputed at every read).

```
       USER-INITIATED                              AGENT/SYSTEM-INITIATED
       ──────────────                              ──────────────────────

   ╭────────────────────╮                          ╭────────────────────╮
   │   onboarding       │                          │  update_profile    │
   │   (one-time, opt   │                          │  tool (in-chat,    │
   │    sections        │                          │  model-initiated,  │
   │    skippable)      │                          │  rate-limited)     │
   ╰─────────┬──────────╯                          ╰──────────┬─────────╯
             │                                                │
   ╭─────────┴──────────╮                          ╭──────────┴─────────╮
   │  direct UI edits   │                          │  discover_trait    │
   │  (watchlist add/   │                          │  (out-of-band      │
   │   remove, future   │                          │   post-SSE worker, │
   │   sector edits)    │                          │   gated by lexicon)│
   ╰─────────┬──────────╯                          ╰──────────┬─────────╯
             │                                                │
             └───────────────────────┬────────────────────────┘
                                     │
                                     ▼
                          ╭───────────────────────╮
                          │  user_preferences row │   ← every write also
                          │  (one row per user)   │     appends a row to
                          ╰───────────────────────╯     user_profile_events
                                                        for audit + undo
```

### The four write surfaces

| Surface | Initiator | Trigger | Slots it can write | Validation | Audit |
|---|---|---|---|---|---|
| **Onboarding** | user | one-time signup flow; any section is skippable | any explicit slot (`experience`, `risk_tolerance`, `asset_classes`, `sectors_of_interest`, `watchlist`) | slot-registry validators (sectors in `CANONICAL_SECTORS`, symbols in `instruments`, scalars in enum) | `source = onboarding` |
| **Direct UI edits** | user | dedicated widget (watchlist add/remove in v1; sectors + asset-classes later) | the slot the widget owns | same | `source = user_edit` |
| **`update_profile` tool** | response agent | model calls the Strands tool during a chat turn — typically in response to an explicit user statement ("add NVDA to my watchlist", "I avoid leveraged ETFs") | any explicit slot, plus `append_agent_notes`; full command table in the "`update_profile` tool surface" section below | slot-registry validators; rate-limited to ≤10 mutations per session | `source = tool_call` |
| **`discover_trait`** | post-stream worker | runs out-of-band after SSE close on the ~10–20% of turns that pass the lexicon gate | any explicit slot via promoted `TraitDelta`; also `append_agent_notes` | promotion gates (structural + similarity + repeat-evidence for scalars + no-silent-contradiction) | `source = chat_inferred`, with `evidence` quote and `confidence` |

The four surfaces share one table (`user_preferences`) and one audit log (`user_profile_events`). The `source` enum on each audit row is the *only* place these writes are distinguished after the fact — for reviewer filtering, for the discovery promotion-quality gate, and for undo flows.

### What is deliberately NOT a write surface in v1

- **Reading a thesis the user adopted does not auto-write `sectors_of_interest`.** Adoption is read at request time via `derive_profile()`. We do not mutate stored state from inferred-from-action signal — that would entangle the read and write paths and make decay rules ambiguous.
- **Closing a thesis as "wrong" does not auto-write to `excluded_strategies`.** A closed-wrong thesis is *data*, not a stated preference. If the user wants the lesson recorded, they say so and the response agent calls `update_profile`.
- **Time decay is not "the system writing".** Decay flips a slot to `ambient` (rendering-suppressed); the row stays. The user never sees a value disappear. The audit log records the decay event with `source = system_decay`.

### Why this is the *only* write taxonomy in v1

Every additional write surface adds an audit boundary, a validation path, a failure mode, and a possible source of "where did this preference come from?" mystery. Four surfaces is enough to cover (a) what the user typed deliberately, (b) what the user mutated through the UI, (c) what the user said in chat that the model chose to formalize, and (d) what the system extracted from the conversation behind the user's back. Any new write path proposed later must justify why it doesn't fit one of these four.

### Read vs. write summary

| Concern | Read path (Phase 2 prompt build) | Write path |
|---|---|---|
| Stored slots | one indexed `SELECT` from `user_preferences` | four surfaces above; every write also appends to `user_profile_events` |
| Derived slots | computed at request time from `user_theses ⨝ entity_tickers` + recent mutation events | **none — never written** |
| `agent_notes` | **not read** in v1 (observation-only field) | `update_profile.append_agent_notes` and `discover_trait` promotions; observed but inert |
| Audit | not consulted at render time | written on every mutation by every surface |

---

## Trait-conditioned prompt rendering

The Phase 2 system prompt's `<user_profile>` block is **conditional on which slots have signal**, not a fixed template with empty placeholders. Four shapes:

| Profile state | `<user_profile>` block contents |
|---|---|
| EMPTY (no stored slots, no theses) | Block omitted entirely. Agent answers as it does today. |
| THESES-ONLY (no stored slots, has theses) | Derived slots only, prefixed with `(observed from your tracked theses)` |
| STORED-ONLY (has slots, no theses) | Stored slots only, with their `source` tags |
| RICH (both) | Both sections, derived rendered second |

Within each section, **each populated slot has its own rendering snippet**. An empty slot renders nothing — no `(none)` placeholder, no `null`. Empty placeholders teach the model that "profile is mostly empty" is a state worth narrating. It isn't.

Sketch of a RICH block for the seeded user_1 today (post-derive):

```
<user_profile>
stored:
  watchlist: NVDA, TSMC, BTC, AAPL  (source: onboarding)
  sectors_of_interest: semiconductors, AI infrastructure  (source: onboarding)
  asset_classes: stocks, crypto  (source: onboarding)
  experience: intermediate  (source: onboarding)
  risk_tolerance: moderate  (source: onboarding)

observed from your tracked theses:
  implicit_watchlist: AVGO, ASML, AMD  (appear in tracked theses, not in explicit watchlist)
  implicit_sectors: power, nuclear  (appear in tracked theses, not in explicit sectors)
  recent_thesis_activity:
    - adopted thesis_003 (AI capex broadening) 2 days ago
    - adopted thesis_005 (nuclear renaissance) 5 days ago
    - closed_correct thesis_007 (TSMC supplier tailwind) 11 days ago
  engagement_recency: 2 days
</user_profile>
```

For a brand-new user with one thesis and no onboarding answers:

```
<user_profile>
observed from your tracked theses:
  implicit_watchlist: CCJ, CEG
  implicit_sectors: utilities, nuclear
  recent_thesis_activity:
    - adopted thesis_005 (nuclear renaissance) today
</user_profile>
```

For a brand-new user with nothing at all: no block.

Notice what is **not** rendered in either example: no concentration score, no macro-lens label, no aggregate close stats, no inferred horizon. Just the facts the model needs to use second-person framing on the right tickers and to not be surprised by what shows up in the user's question.

The renderer lives in `src/personalization/prompt_block.py` and is the single place that decides per-slot inclusion. Phase 1 gets a much terser version — see "Phase 1 vs Phase 2 read paths" below.

---

## Phase 1 vs Phase 2 read paths

The profile is read at two distinct points in a chat turn, with two different shapes for two different jobs. They are intentionally separate; the same renderer module produces both.

| | Phase 1 (research) | Phase 2 (response) |
|---|---|---|
| Job profile influences | Which tools to call, which tickers to call them on, which evidence to fetch | Voice, framing, anchoring |
| What's rendered | One-line `<user_holdings>` hint: explicit watchlist ∪ sector-summarized implicit holdings + 1–2 top sectors of interest | Full `<user_profile>` block per the four-shape renderer |
| What's NOT rendered | Voice rules, `experience`, `risk_tolerance`, `excluded_strategies`, `agent_notes` | Anything that doesn't influence voice |
| When omitted | Sparse-profile fallback applies the same way — no hint if no signal | Sparse-profile fallback omits the entire block |

### Why Phase 1 also reads the profile

The spike (`docs/spike-profile-scaffolding-findings.md`) ran the same advice question under user_1 (AI-hardware) and user_3 (rates/gold) with personalization on. Both runs fired 7 research tool calls — identical shape, dominated by `market_overview` + broad macro searches — because Phase 1 had no visibility into the user. The response phase then painted anchoring on top of an evidence corpus that wasn't tuned to the user's book.

Wiring a terse holdings hint into Phase 1 closes that gap. Concretely:

```
<user_holdings>
Tracked exposure: NVDA, TSMC, BTC, AAPL (watchlist); AI infrastructure (8), nuclear (3), rates (3) (tracked theses).
</user_holdings>
```

The research prompt then carries one line of guidance: *"When the question is open-ended (advice, allocation, 'what should I do'), prefer tool calls on tickers in this set or in these sectors before broad-market calls. Factoid and definition questions ignore this hint."*

### Why Phase 1 does NOT get the voice block

The voice rules (`<personalization>` + the three rules) are response-shaping discipline. The research agent emits no user-facing prose; passing it the voice rules increases prompt tokens and confuses the tool-routing job for zero gain. The research agent is also forbidden by `_RESEARCH_HANDOFF_RULES` from writing inline citations or final-shape output — voice rules would never fire there even if present.

### Failure modes Phase 1 personalization can introduce

| Failure | Guard |
|---|---|
| Research over-focuses on holdings and misses generic context the answer needs | The hint is advisory; the research prompt's existing tool-discipline rules already require `market_overview` for "state of tape" questions. Personalization biases the priors, not the floor. |
| Research collapses to "only fetch what's already in the user's book" | The hint is one line and explicitly conditioned on open-ended questions. Factoid and definition questions ignore it. |
| Cold-start user with no holdings gets a less-targeted search | They get the current behavior (broad-market + macro). The sparse-profile fallback applies symmetrically. |

This single change is the highest-leverage one in the v1 sequence (see "Post-spike v1 priorities" below): it converts personalization from a voice-only feature into an evidence-shape feature.

---

## The three personalization rules

These three rules replace the catch-all `<personalization>` block I sketched in the prior thread. They are scaffolding rules, not content rules:

1. **Frame-shift when overlap exists.** If the question's tickers, sectors, or asset classes overlap any slot in the profile (stored or derived), use second-person framing for those positions (`your NVDA`, `your AI-hardware book`) and skip restating context the user obviously has. Otherwise, answer generically and stay silent about the profile.

2. **Respect stated and observed constraints, never recite them.** If `excluded_strategies` says "avoids earnings plays", do not propose an earnings entry — and do not say "since you avoid earnings plays". Silent steering, not labeled compliance. Same rule for `risk_tolerance`.

3. **Anchor open-ended advice to ground-truth facts only.** For questions like "where should I put cash", "what should I do", "what's a good idea", anchor on the user's actual watchlist, tracked theses, and recent thesis activity — facts you can point to in the profile block. Do **not** anchor on inferred labels ("since you're a concentrated AI investor…", "given your macro lens…"). If the profile is sparse, ask one tight clarifying question instead of confabulating a frame.

Three matching hard fails the rubric must catch:
- Inventing slots the user does not have ("your energy hedge", "since you're short duration") when nothing in the profile supports it.
- Reciting the slot back to the user as data ("Your watchlist is NVDA, TSMC, BTC, AAPL, and your sectors are…").
- Forced relevance — connecting an unrelated topic to a held ticker via a thin pretext.

---

## First-day credibility: guarding against confident wrongness

The most dangerous moment in the whole personalization story is **day 1**, when the system has the least evidence and the user is forming their first impression. A wrong personalization here ("you seem to be an AI-hardware concentrator — here are some adjacent ideas…") does long-term damage that no amount of correctness later recovers. This section is the discipline that prevents that.

The core rule: **on the first day, the only personalization signals we trust are the ones the user explicitly gave us.** No persona inference. No "based on what you've told me so far" sentences. Cold-start personalization is allowed to be limited; it is not allowed to be wrong-but-confident.

### What counts as "ground truth" for day-1 rendering

| Signal | Use on day 1? | Why |
|---|---|---|
| Explicit `watchlist` (from onboarding or watchlist edit) | Yes | The user typed these symbols. |
| Explicit `sectors_of_interest` (from onboarding) | Yes | Same. |
| Explicit `asset_classes` | Yes | Same. |
| Explicit `risk_tolerance`, `experience` | Yes, but quietly | Influence voice only; never name back. |
| `implicit_watchlist` from tracked theses | Yes | The user actively adopted these theses; the tickers are factually associated with their book. |
| `implicit_sectors` from tracked theses | Yes | Same. |
| `recent_thesis_activity` | Yes, but only as breadcrumbs | The model sees them; never narrated back unless directly asked. |
| `agent_notes` | No | Observation-only in v1 regardless of day. |
| Any inferred classification or summary score | **No, ever** | Not stored; not rendered. |

### Single-persona-by-default is fine; *fixed*-persona-for-every-experience is not

It is acceptable for the system, on day 1, to operate against a single best-guess of the user (effectively a single "persona of one"). What is **not** acceptable is to let that guess become a fixed frame that every subsequent answer is shaped around. The renderer must allow the persona-of-one to be **partial** (some slots present, others empty) and **dissolved at any time** (any slot can be cleared without rebuilding a persona). There is no `persona_id` to flip; there are only slots, and they grow and shrink independently.

This is the practical difference between "live expert" and "classification engine": the live expert can hold "you mostly do X, but today you're asking about Y, so I'll answer about Y" without losing coherence. A classification engine cannot.

### The render-less-when-uncertain bias

Three concrete defaults that bias toward silence:

1. **Confidence floor for inclusion.** A stored slot with `confidence < 0.7` (chat-inferred, single evidence, not yet repeat-confirmed) is **not** rendered into the prompt. It still lives in the table — it can be promoted on the next confirming evidence — but it does not influence voice until it is solid.
2. **Sparse-profile fallback to generic voice.** If the rendered `<user_profile>` block ends up with `≤ 1 populated slot` after all filters, omit the block entirely. The agent answers as it would for a brand-new user. Personalizing on a single thin slot is worse than not personalizing.
3. **No first-day claims that combine slots into an inference.** "You seem to be focused on AI hardware" is not a slot — it's an inference combining `sectors_of_interest` + `implicit_sectors` + `watchlist`. Even when all three agree, the model is forbidden from narrating the *conclusion*. It can use the *components* (second-person framing on the specific tickers) without ever stating the synthesized label.

### Why first-day creep is worse than first-day genericism

A new user who gets a competent-but-generic answer thinks "fine, this works like other tools, I'll see if it gets better." A new user who gets a confidently-personalized answer that's wrong thinks "this thing doesn't know me and is pretending it does." The first failure is recoverable on the next reply; the second poisons the whole relationship. The discipline above is what keeps the product on the recoverable side.

---

## Discovery loop: `discover_trait`

Structural mirror of `discover_thesis` ([`src/thesis/discover.py`](../src/thesis/discover.py)) — context in, candidates out, similarity-checked against existing values, gated by quality bar, promoted by validation.

Two hard constraints that frame everything else in this section:

- **Always out-of-band.** The discovery pass NEVER runs inside the SSE chat stream. It is dispatched after the stream closes, against a queue, by a separate worker. The user's response latency is the same whether discovery runs or not. The chat path must remain unable to block on discovery — that decoupling is what allows discovery to be wrong, slow, or rate-limited without any user-visible cost.
- **Runs on a small minority of turns.** Most chats do not reveal a durable preference. The cheap-heuristic gate below is the mechanism; without it, discovery degenerates into "summarize every chat into a memory blob", which is the failure mode that produces noisy, weird-feeling profiles.

### Gating heuristics (the cheap filter that keeps cost flat)

Before any LLM call, the post-stream worker runs a series of fast filters. The pass call only fires when **at least one** of the following is true:

| Trigger | Examples |
|---|---|
| Preference-signal lexicon hit | "i like", "i hold", "i'm avoiding", "i don't touch", "not interested in", "my style", "i prefer", "add to my watchlist", "remove", "watch", "track" |
| Ticker mutation phrase | "add X", "drop X", "watch X", "track X" — where X parses as a known symbol |
| Constraint phrase | "i never", "i avoid", "i won't", "i can't hold", "tax-loss", "401k", "no leveraged", "no options" |
| First N turns of a new session | First 3 turns get a discovery pass regardless — onboarding-like signal density is highest at session start |
| Explicit `update_profile` tool call by the agent | The model already decided to write; record the discovery event for audit |

And **none** of the suppressors fire:
- Message is `< 4` words and contains no ticker/sector keyword.
- Message is a pure factoid (`what is`, `define`, `price of`, `compare X and Y`, `explain`) and contains no preference lexicon.
- The last successful discovery for this user fired within the previous K turns (default K=3) and no new lexicon-hit appears.

Expected hit rate from a sample of `runs/20260520_174622` scenarios: 0/7 of the canonical eval prompts would trigger discovery. That is correct — they are factoids and chip-followups, not preference reveals. The gating is calibrated against the eval corpus, not the other way around.

### The discovery call (when triggered)

A single Flash-tier call (`gemini-3.1-flash` or `claude-haiku`). Input:
- The user's message + the agent's reply for that turn (the reply matters because it disambiguates what the user was responding to).
- The current stored slot values (so the model knows what is already known).
- The slot registry schema (so the model can only emit valid slot/op pairs).

Output: a list of `TraitDelta` candidates:

```json
{
  "deltas": [
    {
      "slot": "watchlist",
      "op": "add",
      "value": "PLTR",
      "evidence": "should i be adding to PLTR here",
      "confidence": 0.85
    },
    {
      "slot": "excluded_strategies",
      "op": "add",
      "value": "leveraged ETFs",
      "evidence": "i don't touch leveraged products",
      "confidence": 0.95
    }
  ]
}
```

If no deltas surface, the model emits `{"deltas": []}` — cheaper than no call.

### Promotion gates

Mirror of candidate → active in `discover_thesis`:

1. **Structural** — slot/op/value validates against the registry. Symbols resolve via `instruments`; sectors via `CANONICAL_SECTORS`; scalars are in their enum. Fail → `rejected`.
2. **Similarity to existing value** — for slots that already have a value, the candidate must either reinforce (bumps `confidence` and `last_confirmed_at`) or differ meaningfully. A confirmation is the most common outcome and is the entire point of accretive personalization.
3. **Repeat-evidence for scalars** — for the two scalar slots (`experience`, `risk_tolerance`), require two candidate hits in distinct sessions before flipping. Single-mention scalars are too noisy.
4. **Additive ops promote on first evidence** — `watchlist add`, `excluded_strategies add`, `sectors add` are easy to undo and cheap to over-include. Single-mention promotion with one-tap "undo" affordance in the UI.
5. **No silent contradiction** — if a new high-confidence candidate contradicts an existing high-confidence value, do **not** overwrite. Two paths: (a) a non-blocking system-side toast in the UI ("I changed your risk tolerance to moderate — undo"); (b) if no natural moment to confirm arises, flag the contradiction in the audit table and leave the trait stale. The response model never gets to inline contradictions into the chat reply — that would turn the personalization layer into a content layer and violates the north star.
6. **`agent_notes` is write-only in v1** — promoted `agent_notes` candidates write to the field for observation, but the field is never loaded into any prompt. Audit the write quality first; decide later whether to wire it into the renderer.

### Why out-of-band is non-negotiable

Three reasons, stacked:

- **Latency.** A Flash-tier call adds 300–800ms. The chat already pays Phase 1 + Phase 2 LLM latency; adding a third sequential call to record-keeping is unjustifiable.
- **Failure isolation.** Discovery can fail (API down, rate limit, parse error, low confidence) without the user ever knowing. If it's in-stream, every discovery failure becomes a chat failure.
- **Inferred-trait creep.** When discovery is in-stream, the model is tempted to *acknowledge* what it just inferred ("noted — I'll remember you avoid earnings"). That breaks the scaffolding-not-content rule. Out-of-band makes the temptation structurally impossible: by the time the trait is recorded, the reply is already sent.

Cost to the system: one Flash-tier call per ~10–20% of turns (post-gating). At seeded user volume that is rounding noise.

### Audit

Every candidate decision writes a row to `user_profile_events` (full schema in the "Storage schema" section below; the discovery-specific columns are `decision`, `evidence`, and `confidence`):

```python
"user_profile_events": [
    ("id",            "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("user_id",       "TEXT NOT NULL"),
    ("session_id",    "TEXT"),
    ("source",        "TEXT NOT NULL"),    # onboarding | watchlist_edit | chat_inferred | tool_call
    ("command",       "TEXT NOT NULL"),    # add_watchlist | append_memory | ...
    ("args_json",     "TEXT NOT NULL"),
    ("decision",      "TEXT NOT NULL"),    # promoted | rejected | confirmed | contradicted
    ("before_json",   "TEXT"),
    ("after_json",    "TEXT"),
    ("evidence",      "TEXT"),             # quote from the user turn (chat_inferred only)
    ("confidence",    "REAL"),             # 0..1 (chat_inferred only)
    ("created_at",    "TEXT NOT NULL DEFAULT (datetime('now'))"),
],
```

Every mutation, whether user-initiated (onboarding, UI edit), agent-tool-initiated (`update_profile`), or discover-promoted, lands in one log. The `source` column distinguishes the surface; `decision`, `evidence`, and `confidence` are populated only for chat-inferred rows.

---

## Derived profile (the cheap insight that ships first)

`derive_profile(user_id, conn) -> DerivedProfile` is a read-time computation, no storage. It is the highest-leverage piece of this entire design because **it ships in a day, requires no LLM call, requires no onboarding, and works the moment a user adopts their first thesis**.

```python
# src/personalization/derived.py

@dataclass(slots=True)
class ThesisEvent:
    thesis_id:  str
    label:      str           # short, derived from thesis title
    kind:       str           # adopted | closed_correct | closed_wrong | let_decay
    days_ago:   int


@dataclass(slots=True)
class DerivedProfile:
    implicit_watchlist:     list[str]
    implicit_sectors:       list[str]
    recent_thesis_activity: list[ThesisEvent]    # last ~5 events, most recent first
    engagement_recency:     int                  # days since last user_theses mutation


def derive_profile(user_id: str, conn: sqlite3.Connection) -> DerivedProfile:
    theses_active = active_user_theses(user_id, conn)
    return DerivedProfile(
        implicit_watchlist     = _union_tickers(theses_active),
        implicit_sectors       = _union_sectors(theses_active),
        recent_thesis_activity = _recent_events(user_id, conn, limit=5),
        engagement_recency     = _days_since_last_mutation(user_id, conn),
    )
```

Cached per request, never persisted. Re-derived every time the prompt is built. The cost is a handful of indexed reads — bounded and predictable.

This is the lever that makes the personalization story land *before* `discover_trait` exists and *before* onboarding is meaningfully complete. The day a user adopts one seed thesis, their `<user_profile>` block has signal.

### Render summarizer

`derive_profile` returns plain facts: raw ticker unions and raw thesis events. Plain facts are the right *storage* shape, but they are not always the right *prompt-render* shape. The spike confirmed this for engaged users:

- `user_1` has 9 active theses spanning 33 distinct tickers across semis, nuclear, pharma, FX, rates, autos, consumer. The derived `implicit_watchlist` rendered as `ARM, AVGO, CCJ, CEG, CRM, DELL, DXJ, EMR, EWJ, FANUY, FXY, HIMS (+20 more)`. That ships as noise — the model can't tell the AI-hardware concentration from the rates hedge from the consumer-staples mix.
- For users with ≤ ~6 active theses, the raw ticker union renders cleanly and the model can anchor on individual symbols. The signal degrades sharply above that count.

A deterministic summarizer sits between `derive_profile` and the renderer. Its inputs are the raw `DerivedProfile`; its outputs are still plain facts, just at a coarser granularity when the raw shape would be unusable.

```python
# src/personalization/summarize.py

def summarize_derived(d: DerivedProfile, *, ticker_render_cap: int = 8) -> DerivedProfileForRender:
    if len(d.implicit_watchlist) <= ticker_render_cap:
        return DerivedProfileForRender(
            implicit_tickers=d.implicit_watchlist,
            implicit_sector_counts=[],          # ticker form is enough
            recent_thesis_activity=d.recent_thesis_activity,
        )
    # Roll up to "<sector>: <ticker_count>" pairs, sorted by count desc.
    counts = _tickers_to_sector_counts(d.implicit_watchlist)       # uses instruments + taxonomies
    return DerivedProfileForRender(
        implicit_tickers=[],                    # collapsed to sector counts
        implicit_sector_counts=counts[:6],      # top-6 sectors only
        recent_thesis_activity=d.recent_thesis_activity,
    )
```

Rendered for user_1 with summarizer:

```
observed (from your tracked theses):
  tracked exposure (by sector): AI infrastructure (8), nuclear (3), rates (3), consumer (2), pharma (2), FX (2)
  recent_thesis_activity:
    - adopted thesis_003 (AI capex broadening) 2 days ago
    ...
```

Notes:

- Still plain facts; no aggregate label like "concentrated AI investor." Sector counts are observations, not classifications.
- The ticker→sector map lives next to `CANONICAL_SECTORS` in `src/news/taxonomies.py` so it's the same vocabulary the news pipeline uses. Tickers that don't map (FX pairs, futures) stay tickers in a small `unmapped` tail rather than getting forced into a sector.
- The `ticker_render_cap` (8 here) is a starting guess. The right value depends on observed slot-fill distributions; defer the tune until there's traffic.
- This is also where the load-bearing audit finding goes: `thesis_match_chunks.sectors_json` is empty for every row today. Until the thesis-creation pipeline populates sectors directly on the thesis row, the summarizer computes them ticker-side via `instruments` → sector mapping. That is acceptable for v1; it loses hierarchy fidelity (`semiconductor` and `ai_infrastructure` both roll up to `technology` when computed from tickers) but it's better than an empty slot.

The summarizer is also the natural place for any future per-slot decay: if `recent_thesis_activity` should weigh recent events more, that's a `summarize_derived` concern, not a `derive_profile` concern.

---

## Storage schema

Two new tables. Validation lives in `src/personalization/prefs.py`, not in schema constraints.

```python
# db/schema.py — TABLES additions

"user_preferences": [
    ("user_id",            "TEXT PRIMARY KEY REFERENCES users(id)"),
    ("experience",         "TEXT"),                          # beginner | intermediate | advanced
    ("risk_tolerance",     "TEXT"),                          # conservative | moderate | aggressive
    ("asset_classes_json", "TEXT NOT NULL DEFAULT '[]'"),    # subset of instruments.asset_class values
    ("sectors_json",       "TEXT NOT NULL DEFAULT '[]'"),    # subset of CANONICAL_SECTORS
    ("watchlist_json",     "TEXT NOT NULL DEFAULT '[]'"),    # subset of instruments.symbol
    ("excluded_strategies_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("agent_notes",        "TEXT"),                          # write-only-pending-review; see self-review for cut recommendation
    # per-slot provenance for chat-inferred fields:
    ("provenance_json",    "TEXT NOT NULL DEFAULT '{}'"),    # {slot: {source, confidence, last_confirmed_at, evidence}}
    ("updated_at",         "TEXT NOT NULL DEFAULT (datetime('now'))"),
],

"user_profile_events": [
    ("id",            "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("user_id",       "TEXT NOT NULL"),
    ("session_id",    "TEXT"),
    ("source",        "TEXT NOT NULL"),    # onboarding | user_edit | tool_call | chat_inferred | system_decay | observation
    ("command",       "TEXT NOT NULL"),    # add_watchlist | append_agent_notes | set_scalar | ...
    ("args_json",     "TEXT NOT NULL"),
    ("decision",      "TEXT"),             # promoted | rejected | confirmed | contradicted (chat_inferred only)
    ("before_json",   "TEXT"),
    ("after_json",    "TEXT"),
    ("evidence",      "TEXT"),             # quote from the user turn (chat_inferred only)
    ("confidence",    "REAL"),             # 0..1 (chat_inferred only)
    ("created_at",    "TEXT NOT NULL DEFAULT (datetime('now'))"),
],
```

Notes:
- Sectors validate against `CANONICAL_SECTORS` (mirrors `normalize_sectors` in `src/news/sectors.py`).
- Watchlist symbols validate against `instruments` (alias-aware). Unknown symbols from onboarding bubble back to the UI for confirmation rather than silently disappearing.
- `excluded_strategies_json` is free-text-per-entry, not enum-validated. The slot is meant to catch open-ended constraints ("no leveraged ETFs", "no earnings plays").
- **`horizon` is deliberately absent** from `user_preferences`. Real users have mixed horizons across positions; a single global enum either lies or forces over-commitment. Re-evaluate as a per-thesis or per-ticker field later.
- JSON arrays are fine because v1 only ever queries `WHERE user_id = ?`. The day we need "which users care about sector X" (Phase 7-shaped surfaces), split into N:M tables.

The seeded `users/{1,2}/profile.md` structured headers are **rewritten** into `user_preferences` rows in a one-shot migration. The narrative sections of `profile.md` (`## Trading Style`, `## Goals`, `## Memory`) are dropped. `agent_notes` starts empty; whether it gets populated at all depends on the self-review decision to cut it from v1.

---

## Feed personalization

The same `user_preferences` row also drives news-feed ranking and visibility. This is a separate surface from voice scaffolding (one is what gets shown in `/api/home`, the other is how the response agent talks), but it shares storage and validation. Keeping them in one doc avoids the read/write drift that would otherwise appear if a second doc owned the table.

### Source-tier visibility

Every news source is tagged at ingest time with a visibility tier. New column `news.source_tier TEXT NOT NULL DEFAULT 'global_high'`.

| Tier | Examples | Default visibility |
|---|---|---|
| `global_high` | major wires (Reuters, Bloomberg, FT), curated finance press, the synthesizer's sharp lane, the daily brief | Always on the global home strip. Personalization affects rank, not inclusion. |
| `personal` | PRNewswire, GlobeNewswire, BusinessWire, AccessWire, BMCNews, regional wires — issuer-paid press wires by definition | Hidden from the global strip. Surfaces only when the named ticker is in the user's watchlist OR a tracked thesis's tickers. |
| `discover` | reserved for cross-user/sector "trending" surface and the discover-path scraper | Never on the home strip; opt-in via Discover surface only. |

`build_news(conn, user_id)` filter:

```sql
WHERE materiality_score IS NULL OR materiality_score >= ?    -- L2 gate
  AND (
    source_tier = 'global_high'
    OR (
      source_tier = 'personal'
      AND id IN (
        SELECT entity_id FROM entity_tickers
        WHERE entity_type = 'story'
          AND symbol IN (<watchlist ∪ thesis_tickers>)
      )
    )
  )
```

The watchlist-or-thesis-overlap subquery is the same set the relevance scorer needs, so it costs no extra round trip. Empty-profile users (no watchlist, no theses) see only `global_high`. This is the correct cold-start: the strip becomes the curated + brief surface, not a wire dump.

**Why this is a hard filter, not a downrank.** Issuer-paid PR is overwhelmingly dominant in the `personal` tier; ranking it 100 places lower than Reuters still leaves it at position 101. The product promise is "your conviction-driven feed", not "everything that happened". Issuers the user has zero interest in are deferred to the per-ticker page (`/api/news?ticker=XYZ`), where the user is already in context.

### Relevance score

For story *s* and user *u*:

```
score(s, u) =
    W_WATCH    * |tickers(s) ∩ watchlist(u)|
  + W_THESIS_T * |tickers(s) ∩ tickers(theses_tracked(u))|
  + W_SECTOR   * |sectors(s) ∩ sectors(u)|
  + W_LINKED   * (1 if any thesis(s) ∈ theses_tracked(u) else 0)
  + W_ASSET    * (1 if any ticker(s).asset_class ∈ asset_classes(u) else 0)
```

Starting weights (integers, easy to read in a debug payload):

| Constant | Value | Why |
|---|---|---|
| `W_WATCH` | 5 | The user typed this ticker. Strongest explicit signal. |
| `W_THESIS_T` | 3 | Tickers from theses they track. Strong, but indirect. |
| `W_SECTOR` | 2 | Topical interest, less specific than a ticker. |
| `W_LINKED` | 4 | Article already judged to support/stress a thesis they hold. Load-bearing for the digest. |
| `W_ASSET` | 1 | Coarse asset-class filter; tiebreaker. |

Lives in `src/personalization/scoring.py` as named constants. Hand-tuned after sample-article review (not auto-learned).

Ranking rule: within a recency window (default last 72h for the feed, last 24h for the digest), sort by `score(s,u) DESC, story.created_at DESC`. Stories with `score = 0` fall through to a separate "Other" pool, recency-ordered, surfaced only if the personalized pool is sparse.

### Issuer rate-limit

A single issuer in the user's watchlist can still flood the strip when it publishes 4 press releases in one day. Stack-rank within `(user_id, primary_ticker, day)`:

- Group home-feed candidates by `(primary_ticker(n), date(published_at))`. The "primary ticker" is the first non-macro symbol in `entity_tickers` for the row (already what the chip strip displays).
- Render at most one card per group. The card surfaces the highest-materiality item; siblings collapse into a `+N more` affordance that expands inline.
- Cap at 3 cards per issuer per 24h window, post-collapse, to bound a single issuer's footprint.

Storage stays one-row-per-PR; collapse is a UI-layer rule in `build_news`.

### Endpoint impact

| Endpoint | Change |
|---|---|
| `/api/home` (existing) | `build_news(conn)` → `build_news(conn, user_id)`. Apply ranking, per-user chip filter to theses in `user_theses`, source-tier gate, issuer-day collapse. |
| `GET /api/news` | Same `build_news(user_id, page, limit)`; pagination over the personalized story order. |
| `GET /api/news/{id}` | `thesis_connections[]` filtered to `user_theses` of the caller. `suggested_theses[]` ranked by `score(thesis, u)` reusing the same overlap formula on thesis-side fields. |
| `GET /api/news?ticker=XYZ` | Per-ticker page surfacing `personal`-tier items the home strip suppressed. |
| `agents/daily_digest.py` | "Orphan stories" section reads stories with `score(s,u) > 0` AND `story_id NOT IN thesis_story_links` — user-relevant stories that no thesis is catching. |

---

## `update_profile` tool surface

A single Strands tool wired into the response agent. Operates only on the *current authenticated user's* profile — no `user_id` parameter; the orchestrator scopes it via session. The tool ignores any model-supplied user identifier.

| Command | Args | Effect | Validation |
|---|---|---|---|
| `view` | — | Returns the current `user_preferences` row + the derived profile. Model **must** call this before any mutation if it has not seen the current state in this turn. | — |
| `add_watchlist` | `symbols: list[str]` | Appends to `watchlist_json`. | Each symbol must resolve via `instruments` (alias-aware). Unknown symbols return an error; the model can confirm with the user or drop them. |
| `remove_watchlist` | `symbols: list[str]` | Removes from `watchlist_json`. | No-op for absent symbols; returns the removed set. |
| `add_sectors` | `sectors: list[str]` | Appends to `sectors_json`. | Must be in `CANONICAL_SECTORS` (alias-normalized via `normalize_sectors`). |
| `remove_sectors` | `sectors: list[str]` | Removes from `sectors_json`. | — |
| `add_excluded_strategies` | `items: list[str]` | Appends to `excluded_strategies_json`. | Length cap per entry; deduped. |
| `set_scalar` | `field: enum, value: str` | Sets one of `experience | risk_tolerance`. | Value must be in the enum for that field. |
| `set_asset_classes` | `classes: list[str]` | Replaces `asset_classes_json`. | Each must be a valid `instruments.asset_class` value. |
| `append_agent_notes` | `line: str` | Appends a dated line to `agent_notes`. | Length cap; plaintext only. **Disabled if `agent_notes` is cut per the self-review.** |

Deliberately omitted: `delete` of the whole profile, free-form file write, anything that touches another user's data, `set_horizon` (slot is excluded), `set_trading_style` / `set_goals` (narrative slots are excluded).

### When the model should call it

Response-agent system prompt directive:

> When the user expresses a durable preference, intention to track an asset, change in conviction, or a stated constraint that affects how we should personalize, call `update_profile` to record it. Use the structured commands (`add_watchlist`, `add_sectors`, `set_scalar`, `add_excluded_strategies`) for anything we will filter or rank by. Read state with `view` before writing if you are unsure of the current value. Do **not** mutate the profile on inferred preferences — those are the job of the out-of-band `discover_trait` worker.

We do not ask the model to save every conversation summary. The cost of a noisy memory is worse than the cost of missing one (the discover_trait pipeline plus its promotion gates exists for exactly that signal).

### How saved state surfaces in future turns

At the start of every response-agent run, the orchestrator pre-loads the full `user_preferences` row (always — it's tiny, ~10 fields) plus the derived profile into the system context. The model never has to call `view` to *read* prefs in the common path; `view` exists for cases where it has just written and wants to confirm.

### Security and safety

- **No path I/O exposed to the model.** All writes go through validated semantic commands; the model cannot pass a path.
- **User scope is enforced by the orchestrator**, not by tool args.
- **Validation is mandatory** on every command — same gates as the API layer.
- **Rate limit** on writes per session (≤10 mutations / session) bounds cost and damage from a runaway loop.
- **No secret/PII heuristics yet.** Add a deny-list if testing shows the model trying to memorize sensitive content.

---

## What this design explicitly does NOT do

- **No persona archetypes.** No "AI-hardware concentrator" bucket, no "yield hunter" bucket, no segmentation. Profile is per-user accretion. Period.
- **No persona seeds.** The seeded-thesis pattern transfers as a *mechanic* (`discover_trait` mirrors `discover_thesis`), not as a *content layer*. There is no `persona_seeds` table, no `global/personas/` directory.
- **No user-facing persona label.** The user is never told "your style is X" by the system. The agent demonstrates it understands the style; it does not announce a classification.
- **No `horizon` storage in v1.** Users have mixed horizons across positions; a single global enum lies or forces over-commitment. Re-evaluate as a per-thesis or per-ticker field later.
- **No narrative slots in product in v1.** `agent_notes` exists as an observation-only field — the model can write into it, the prompt does not read from it. We watch the writes for a cohort of early users before deciding if any narrative content earns its way into the prompt.
- **No inferred classifications in the derived layer.** No concentration score, no macro-lens label, no aggregate conviction stats. Derived slots are plain facts (specific tickers, specific sectors, specific recent events) — never single-number summaries that read as identity claims.
- **No personalization on sparse profiles.** If after filtering only ≤1 slot would render, the `<user_profile>` block is omitted entirely. Generic answer beats half-confident personalization.
- **No always-on discovery.** ~80% of turns should skip the `discover_trait` LLM call via cheap heuristics. The eval corpus is the calibration set: zero of the seven scenarios in `runs/20260520_174622` should trigger a discovery pass.
- **No in-stream discovery.** `discover_trait` is always out-of-band, always post-SSE. The chat path cannot block on it; the chat reply cannot reference what it just learned.
- **No silent scalar overwrite.** A contradiction triggers a confirmation toast or a stale flag, not a flip. Additive ops (`watchlist add`, `excluded add`) can promote on first evidence because they are easy to undo.
- **No backwards compatibility.** Pre-launch. The structured header in `users/{1,2}/profile.md` (the seeded profiles) gets rewritten into `user_preferences` rows. The narrative sections in those files are dropped — `agent_notes` is the (observation-only) replacement.
- **No narrative or voice context in Phase 1.** The Phase 1 read path is intentionally limited to a one-line `<user_holdings>` hint (tickers + top sector summaries) — see "Phase 1 vs Phase 2 read paths" above. Voice rules, scalar slots, and `excluded_strategies` never reach the research agent.

---

## Personalization telemetry

Prompt-only personalization is leaky by construction: the model can ignore the rules block, recite a slot back, or invent a label. The spike caught a recite-as-data slip in 1 of 10 hand-graded scenarios — a 10% leak rate against carefully-written prompts on a hand-graded corpus. The leak rate in production will be the only number that matters for tuning, and we cannot tune what we cannot measure.

The telemetry surface is a post-stream worker (same lifecycle host as `discover_trait`; runs after SSE close so it adds no user-visible latency) that records one row per turn to `personalization_events`:

```python
"personalization_events": [
    ("id",                        "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("user_id",                   "TEXT NOT NULL"),
    ("session_id",                "TEXT"),
    ("request_id",                "TEXT NOT NULL"),
    ("turn_index",                "INTEGER NOT NULL"),
    ("profile_block_rendered",    "INTEGER NOT NULL"),     # 0/1 — was <user_profile> in the Phase 2 prompt?
    ("populated_slots_json",      "TEXT NOT NULL"),         # which slots had values at render time
    ("holdings_hint_rendered",    "INTEGER NOT NULL"),     # 0/1 — was the Phase 1 <user_holdings> hint in scope?
    ("recite_hits_json",          "TEXT NOT NULL"),         # list of slot values that appeared verbatim in final_text
    ("second_person_hits_json",   "TEXT NOT NULL"),         # list of (ticker, span) where "your <ticker>" or similar fired
    ("forced_relevance_flag",     "INTEGER NOT NULL"),     # heuristic: did the answer staple a holding onto an unrelated question?
    ("question_category",         "TEXT"),                  # rough bucket: factoid | advice | holder | story_followup | other
    ("created_at",                "TEXT NOT NULL DEFAULT (datetime('now'))"),
],
```

The heuristics behind each field are deliberately cheap and deterministic — no LLM calls. The point is signal sufficient to spot rate changes week-over-week, not a perfect grader.

| Field | How computed |
|---|---|
| `populated_slots_json` | Inspect what the renderer would emit for this user at request time. Recorded even when the block is suppressed by the sparse-profile rule. |
| `recite_hits_json` | For each populated slot value (sector strings, enum scalars, watchlist symbols), regex-search `final_text` for verbatim substring matches in slot-reciting contexts (e.g. "your watchlist is", "as a {experience} trader", "given your {risk_tolerance}"). |
| `second_person_hits_json` | Regex for `\byour ([A-Z]{1,5}|<known ticker>)\b` and similar, restricted to tickers in the user's stored ∪ derived sets. This is the *positive* signal — it tells us the frame-shift rule is firing. |
| `forced_relevance_flag` | Fires when `question_category = factoid` AND any `second_person_hits` overlap the user's holdings. The factoid trap from the spike. |
| `question_category` | A small classifier in the post-stream worker — string-match on user_text against the same lexicon `discover_trait` uses for gating, plus a fallback. Cheap. |

### What this unlocks

- **Production leak rate for gate 3.26.** Right now the gate is graded by sample; with telemetry it's a per-day rolling number that catches regressions within hours of a prompt edit.
- **Second-person framing as a positive signal.** The spike showed that `pers_nvda_user_1` (the textbook frame-shift case) did NOT shift to "your NVDA" — a near-miss that's invisible in pass/fail eval grading. Counting second-person framing hits tells you which question categories are under-using the rule.
- **Sparse-profile fallback validation.** Are we omitting the block too aggressively? Too rarely? Slot-fill distribution over real users decides this; today's `≤1` threshold is a guess.
- **Drift detection.** Six weeks after a prompt edit, recite rate quietly creeps from 1% to 5%. Telemetry catches it; sample grading does not.

### Why this lives in a separate worker, not the chat path

Same reasons as `discover_trait` (latency, failure isolation, no in-stream side effects). The chat path emits the events into the same queue that drives `discover_trait`; the worker reads, computes, writes. Failure of telemetry never touches the user-visible response.

### Cost

One small table. One post-SSE worker callback. ~150 LOC of regex + a tiny classifier. The schema is additive — no impact on the read or write paths.

This is a non-negotiable v1 dependency for any further prompt iteration on the rules block. Without it, every prompt change is being graded by ten hand-read scenarios.

---

## Failure modes and guards

| Failure mode | Guard |
|---|---|
| **Confident wrongness on day 1** (the headline risk) | First-day-credibility section: confidence floor for inclusion, sparse-profile fallback to generic voice, derived layer renders facts not classifications |
| Inferred-trait creep ("noted, I'll remember…") | Discovery is out-of-band; model cannot reference what it just learned in the same reply |
| Mislabeling a mixed-preference user as a single persona | No classification slots in derived layer; no persona archetypes anywhere; `concentration_score` and `macro_lens` explicitly rejected |
| Memory bloat from over-write | Rate limit: ≤10 mutations per session; reviewer audits `user_profile_events` weekly for the first month (gate 3.24 below) |
| Stale traits influencing voice | Decay timer: slots not re-confirmed in 90 days demote to "ambient" — kept in storage, not rendered into prompt |
| Cross-user leakage | Orchestrator scopes all reads/writes by session; tool ignores any model-supplied `user_id` |
| Forced relevance in voice | Three personalization rules + rubric criterion + two-user diff probe |
| Discovery noise | Gating heuristics + repeat-evidence for scalars + audit decision column |
| Contradicting an explicit user statement | No silent overwrite rule; confirmation toast or stale flag |
| Profile becomes a vector for prompt injection | Validation gate on every write; values are interpolated, not executed; no path I/O exposed to the model |
| Derived profile lies about user intent | Annotated as `(observed from your tracked theses)` so the model weights it differently and never quotes it back as user-stated |
| `agent_notes` becomes a junk drawer | Write-only in v1; reviewed by a human before any prompt wiring is considered |

---

## Quality gates (add to masterplan Phase 3)

- **3.20 Profile-conditional prompt rendering** — for a user with zero stored slots and zero theses, the `<user_profile>` block is *absent* from the Phase 2 prompt (not present-and-empty). Smoke test on `build_phase2_system_prompt`.
- **3.21 Theses-only derived profile** — a user with one tracked thesis and no stored slots produces a `<user_profile>` block containing only the derived section. Snapshot test.
- **3.22 Two-user diff probe** — run the same scenario body under user_1 (AI-hardware watchlist) and user_2 (different watchlist). For personalization-relevant scenarios (advice, holder-frame, open-ended) the `final_text`s must differ on voice/anchoring. For factoid scenarios (`qa_pure_price_compare`, `qa_repo_facility_explainer`) they must NOT differ on personalization grounds. Lives in `hf-evals`.
- **3.23 Discovery gating rate** — sample 100 production turns; ≤25% triggered the `discover_trait` LLM call. Higher than that means the lexicon is too loose.
- **3.24 Discovery promotion quality** — sample 30 promoted `TraitDelta`s; reviewer marks each `correct | noisy | wrong`. Gate: ≥80% correct, <5% wrong. Wrong writes trigger a prompt revision; noisy writes trigger a lexicon tightening. Weekly during the first month.
- **3.25 Trait decay** — a confirmed slot demotes to ambient after 90 days of no confirmation. Verify via test that sets `last_confirmed_at` back 91 days and confirms the slot is dropped from the rendered block.
- **3.26 No archetype leak** — sample 100 `final_text`s from production; zero mentions of stored slot labels back to the user as data ("your watchlist is…", "your risk tolerance is…", "as an intermediate trader…"). Hard fail on any hit.
- **3.27 Sparse-profile fallback** — a user with only one populated slot (and nothing else) gets the same generic `<user_profile>`-omitted treatment as a user with zero slots. Unit test on the renderer.
- **3.28 Confidence floor** — chat-inferred slots below `confidence < 0.7` and not yet repeat-confirmed do not appear in the rendered block. Unit test.
- **3.29 No in-stream discovery side effects** — verify that a single chat round emits at most one Phase 2 LLM call (no in-stream `discover_trait` invocation). Trace-level assertion in hf-evals.
- **3.30 `agent_notes` observation-only** — verify that `agent_notes` content is never included in any system or user prompt rendered by `build_phase{1,2}_system_prompt` or `build_phase{1,2}_user_prompt`. Snapshot test on a user with populated `agent_notes`.

---

## Open questions

1. **When (if ever) does `agent_notes` get promoted from observation-only to in-prompt?** v1 ships the field as write-only. We need a concrete promotion criterion before flipping it on: e.g., reviewer-graded write quality ≥80% useful across a 50-sample reviewer pass + a separate two-user diff probe showing the field changes voice for the right cases. Open until we have early-user data.

2. **Where does the post-SSE discovery worker live — inside the Sage Bedrock+Strands stack, or as a separate Flask/queue worker?** Both work. The Strands route keeps tooling consistent; the separate-worker route makes the out-of-band guarantee structurally enforceable. Lean: separate worker reading from a queue written by the orchestrator at SSE close.

3. **Should the response agent's `update_profile` tool calls and `discover_trait`'s candidates compete for the same audit row, or be distinct?** They produce the same effect (a row in `user_profile_events`) but different `source` values. Proposal: one table, one log, two `source` enum values. Reviewer can filter.

4. **Confidence floor calibration.** The proposal sets `0.7` as the inclusion floor for chat-inferred slots. That number is a starting guess; we will need to retune after the first 50 reviewer-graded `TraitDelta`s. If too strict, derived signal carries too much of the load; if too loose, first-day-creep gate 3.26 catches it.

5. **Sparse-profile threshold.** The proposal omits the `<user_profile>` block when ≤1 slot would render. Should the threshold be lower (always render anything we have) or higher (require ≥3 slots before personalizing)? Lean: ≤1 is correct for the cold-start risk profile; revisit only if eval data shows it suppresses too aggressively.

6. **Should `derive_profile` cache per-request or per-session?** Per-request is simpler and the cost is small (3–5 indexed reads). Per-session would let us avoid recomputing during a long chat. Proposal: per-request for v1; add session cache only if profiling shows it matters.

7. **Migrating the seeded `users/1/` and `users/2/` profiles.** The structured header in their `profile.md` gets parsed into a `user_preferences` row. The narrative sections (`## Trading Style`, `## Goals`, `## Memory`) are dropped. `agent_notes` starts empty and may get populated by `discover_trait` over time. Mechanical migration script run once before launch.

---

## Implementation sketch (sequence)

Each step is independently shippable. Steps 1–3 land the personalization win without writing any new LLM code.

1. **Slot registry** — `src/personalization/slots.py`. Single source of truth: slot definitions, types, validators, rendering snippets, decay rules. Encodes the v1 stored slots (no `horizon`, no narrative-in-prompt) and the derived-slot shape.
2. **Derived profile** — `src/personalization/derived.py`. `derive_profile(user_id, conn)` reads `user_theses ⨝ entity_tickers`. Pure computation, no LLM, no writes. Returns facts only — no scores, no labels.
3. **Prompt rendering** — `src/personalization/prompt_block.py`. Conditional `<user_profile>` block per the four shapes above, with the confidence floor and sparse-profile fallback built in. Wire into `build_phase2_system_prompt`; tight version into `build_phase1_system_prompt` (watchlist union only).
4. **Three personalization rules + first-day-credibility hardening** — edit `PHASE2_SYSTEM_PROMPT_BASE` in [`src/agent/prompt_manager.py`](../src/agent/prompt_manager.py) to add the rules block + one contrastive example + the explicit "no first-day inference narration" prohibition.
5. **Storage** — schema additions per the "Storage schema" section above (`user_preferences` + `user_profile_events`), minus `horizon`. One-shot migration of the seeded `profile.md` structured headers into rows; narrative sections dropped.
6. **`update_profile` tool** — per the "`update_profile` tool surface" section above, scoped against the slot registry from step 1.
7. **Discovery gating** — `src/personalization/gating.py`. Lexicon, suppressors, ticker-mutation parser. Pure function: `should_run_discovery(turn) -> bool`. Unit-tested against the hf-evals corpus before any LLM call exists.
8. **`discover_trait`** — `src/personalization/discover.py`. Flash-tier call, candidate emission, promotion gates, audit writes. Runs out-of-band via a post-SSE worker reading a queue. The chat path is structurally unable to invoke it.
9. **Decay job** — daily cron: any slot with `last_confirmed_at` > 90 days marks as ambient (rendering-suppressed but stored). Cheap; one UPDATE per user.
10. **Eval coverage** — new `personalization/` category in `hf-evals` with the diff probe (gate 3.22), sparse-profile fallback (gate 3.27), confidence floor (gate 3.28), no-in-stream-discovery (gate 3.29), and a scenario for each personalization rule.
11. **Rubric criterion** — new `Personalization fit` criterion in `rubrics/response_agent.md` per the prior thread.

The critical-path slice (the part that converts the seven generic answers in `runs/20260520_174622` into personalized ones) is steps 1–4. The rest is what makes the system live up to "evolving expert" instead of "loaded JSON blob."

---

## Self-review: what this doc is sized for vs. where we actually are

This section is a deliberate counter-read of the rest of the doc. Treat everything above as *the eventual product surface*, not *the next thing to ship*. The system today has two seeded users, no onboarding flow, no production traffic, and one calibration corpus of seven scenarios. The design above is sized for a system with hundreds of real users producing thousands of turns. Most of the discipline it encodes (confidence floors, sparse-profile fallbacks, decay timers, promotion gates, the four-shape renderer) is *predictive guard rail* against failure modes we have not yet seen. Predictive guard rail is the most common form of over-engineering in prompt systems, because each guard reads as obviously prudent on its own and only becomes a problem in aggregate.

What this section argues:

1. The doc bundles three different products (a read-path scaffolder, a discovery worker, a memory store) under one cover and proposes shipping them together. They have very different risk profiles and very different value-per-line-of-code. Unbundle them.
2. The minimum viable pilot is **smaller than steps 1–4**. Steps 1–3, with step 4 reduced to a single rule and no first-day hardening, is enough to falsify the core hypothesis ("does prompt scaffolding from existing data change voice on the eval corpus?"). Ship that, look at the diff, then decide whether the rest is justified.
3. Several specific numbers and rules in the doc are confident-looking without data behind them. Calling that out before they ossify into "the design".

### Where I think the doc is right

- **Scaffolding-not-content north star.** Holds up under stress. Every time I try to talk myself into a content-layer exception ("but what if the user asks…"), the failure mode lands somewhere on the recite-or-confabulate spectrum. Keep.
- **Rejecting personas and segmentation.** Right call. The seeded-thesis transfer would have shipped a classification engine wearing a personalization mask.
- **Derived profile from `user_theses`.** Highest leverage idea in the doc. It is the only piece that produces signal *without any new user input*. Ship it first, independent of everything else.
- **Out-of-band discovery.** Architecturally correct. If discovery ever ships, it must be out-of-band. The three reasons stacked (latency, failure isolation, inferred-trait creep) are all real.
- **One audit table, one source enum.** Right factoring; resists the temptation to grow a write surface per feature.

### Where the doc is over-built for the current state

| Concern | What the doc proposes | What's actually justified now |
|---|---|---|
| Renderer shapes | Four (EMPTY / THESES-ONLY / STORED-ONLY / RICH) plus a sparse-profile fallback that omits the block | Two: present or absent. "Present" renders whatever slots have values, no per-shape logic. Sparse-profile fallback can be the *only* present-vs-absent rule for v0. |
| Confidence floor | `0.7` numeric threshold on chat-inferred slots, plus repeat-evidence for scalars | No chat-inferred slots exist yet (no `discover_trait`). The floor is regulating a population of zero. Defer the number until there is a calibration sample of ≥30 promoted candidates. |
| Decay | Daily cron at 90 days, `ambient` state, audit row per decay | No user has a slot old enough to decay. Defer entirely until the audit log shows real slot-age distribution. |
| `agent_notes` write-only field | New stored field, plus `append_agent_notes` op on the tool, plus a "we'll review later" plan | Do not ship the field. Write-only-pending-review with no named owner and no review cadence is the literal definition of a junk drawer. If we want narrative observation, log it to `user_profile_events` with `source = observation` and grep it weekly. No new schema column. |
| `update_profile` tool | Full Strands tool surface (commands table above) | Defer until derived-only personalization is visibly leaving signal on the table. The model has no business mutating profile state before we have evidence that profile state moves outputs. |
| Discovery loop | Lexicon gate + LLM call + promotion gates + audit + worker | Defer the entire chapter. Discovery is the most expensive, most failure-prone, and most user-visible-when-wrong piece of the design. Earn the right to ship it by first showing that the read path even matters. |
| Quality gates 3.20–3.30 | Eleven gates referenced against "masterplan Phase 3" | The masterplan structure is not verified in this doc. The eleven gates also outnumber the actual surface they're gating. Collapse to three: render-conditional, two-user diff, no-recite. The rest get written when their feature lands. |
| First-day-credibility section | A whole section of rules for a population of two users | The rules are not *wrong*, they are *premature*. Keep the section as written but mark it explicitly as "applies the moment we have onboarded users", not as a v0 gate. |

### What I'd actually ship first: v0 ("read-only personalization")

The smallest change that can falsify the hypothesis that personalization moves outputs on this product. No new tables, no LLM calls, no tool, no discovery, no decay, no agent_notes, no onboarding dependency.

```
v0 SCOPE                                  EXPLICITLY DEFERRED
────────                                  ───────────────────
✓ slot registry (read-only,               ✗ user_preferences schema additions
  enumerates the shape)                   ✗ user_profile_events extensions
✓ derive_profile from user_theses         ✗ update_profile tool
✓ load seeded profile.md as the only      ✗ discover_trait worker
  stored-slot source (parser, not         ✗ lexicon gating
  migration; read at request time)        ✗ confidence floor / decay / ambient
✓ <user_profile> block: present or        ✗ agent_notes field
  absent, one shape                       ✗ four-shape renderer
✓ ONE personalization rule in the         ✗ first-day-credibility hardening
  prompt: "frame-shift when overlap         (re-enable when onboarding ships)
  exists; otherwise answer generically"   ✗ rubric criterion (write after we see
✓ rerun the 7-scenario corpus under         the first diff)
  user_1 and user_2; diff the outputs
```

Time budget: ~1–2 days of work. The whole point is to learn whether the cheap thing moves the needle before committing to the expensive thing.

Decision criterion at end of v0: of the seven scenarios in `runs/20260520_174622`, how many move from "generic" to "anchored" in a way a human reviewer judges as an improvement, with zero forced-relevance regressions on the factoid scenarios? If ≥3 improve and 0 regress, the read path is worth investing in and we proceed to v1. If <3 improve, the bottleneck is not the scaffolding layer and we should not build the rest of this doc yet.

### v1, *after* v0 lands

v0 landed on the `spike/profile-scaffolding` branch (2026-05-20). Read-only personalization shipped with one rule, four seeded users (two original + opposite-profile + degraded), and 10 paired evals. Outcome: 5 wins / 4 no-regressions / 1 mixed at the design's pass bar. The spike findings (`docs/spike-profile-scaffolding-findings.md`) reordered the v1 queue.

#### Post-spike v1 priorities

Ordered by leverage. Each item below is justified by a specific observation in the spike.

1. **Phase 1 personalization read path** (the highest-ROI single change). Detailed in "Phase 1 vs Phase 2 read paths" above. Spike evidence: `pers_cash_user_1` and `pers_cash_user_3` both fired 7 research tool calls with identical shapes despite holding wildly different books. Until Phase 1 sees the user, personalization is a voice-only feature on top of a generic evidence corpus. Wiring one `<user_holdings>` line into the research prompt converts personalization into an evidence-shape feature.

2. **Personalization telemetry** (the prerequisite for every prompt iteration after v0). Detailed in "Personalization telemetry" above. Spike evidence: caught a recite-as-data slip in 1 of 10 hand-graded scenarios. Production leak rate is the only number that matters for tuning the rules block, and we cannot tune what we cannot measure. Land before any further prompt edits.

3. **Render summarizer for `implicit_watchlist`**. Detailed in "Render summarizer" under the Derived profile section above. Spike evidence: `user_1`'s 33-ticker implicit watchlist rendered as noise — the AI-hardware concentration signal got buried under a generic 33-ticker list. Without the summarizer, the derived layer collapses to "user has many theses, here are tickers."

#### Other v1 items (below the post-spike priorities)

- `user_preferences` table (real storage, not read-from-profile.md). Triggered the moment we have two slots that came from somewhere other than the seed file.
- Onboarding flow. Still the biggest gap in the user-facing story; without it, stored slots only exist for users we hand-seeded. **This outranks `discover_trait` in priority — earn the right to discover by first having something to confirm or contradict.**
- The `update_profile` tool, scoped to the slots onboarding doesn't cover.
- Second and third personalization rules (constraint-respect, anchor-open-ended). Added one at a time, each justified by a specific failed scenario surfaced in telemetry data.

### v2, only if v1's update-rate justifies it

- `discover_trait`, in the exact shape the doc proposes (out-of-band, lexicon-gated, promotion-gated).
- Decay.
- Confidence floor, with the threshold derived from the first 30+ promoted candidates rather than picked a priori.

### Specific positions the rest of the doc should change

1. **Drop `agent_notes` from v1 entirely.** It is the single field with the highest risk-to-value ratio in the design: it requires a schema column, a tool surface, a write path from `discover_trait`, and an undefined review process, in exchange for *no* in-prompt effect. The doc admits we'll "decide later"; that decision is "no, not until we know what we want it for." If observation is genuinely useful, it lives in `user_profile_events` as a `source = observation` row, queryable but with no schema impact.

2. **Drop the `0.7` confidence floor as a named constant.** Replace with a binary "explicit_only / inferred_allowed" mode on the renderer, controlled by a feature flag, defaulting to `explicit_only` until we have calibration data. A number that looks tunable when nothing has ever been tuned against it will calcify into "the threshold" and we will lose the ability to argue about it.

3. **Collapse the four-shape renderer to two.** Per-shape branching is solving a problem (templating cleanliness) that does not exist yet. A single render function that emits whichever slots have values, plus a single check "is the result ≤1 slot? omit the block" covers every shape the design actually needs.

4. **The masterplan Phase 3 reference is unverified.** The doc cites "Phase 3 gates 3.20–3.30" as if there is a masterplan with reserved slot numbers. Either link the masterplan file from this doc, or rename the gates to local IDs (`PAS-1` through `PAS-N`) and avoid cross-document drift.

5. **The two-user diff probe (3.22) needs a third user before it is a real probe.** Two users with two profiles is a sample size of one. Spike: hand-author a third seeded user with a deliberately *opposite* profile (e.g., yield-and-duration instead of AI hardware) and confirm at least one scenario produces three meaningfully different answers. If we can't tell three apart, the renderer isn't doing enough work.

6. **Add a "derive_profile fidelity" spike to step 0.** Before shipping any of v0, audit `user_theses` and `entity_tickers` for the seeded users: is `sectors` populated, is `tickers` populated, do they actually union into useful sets? If not, the entire derived layer is empty and v0 collapses to "load profile.md", which is much weaker. Half a day of audit before half a week of building.

7. **Re-order the implementation sketch.** Today the list reads "slot registry → derived → rendering → rules → storage → tool → discovery → discover → decay → eval → rubric." The honest ordering is: **0. derive_profile fidelity audit. 1. derived layer. 2. profile.md parser. 3. renderer (one shape). 4. one rule. 5. eval rerun + manual diff.** Everything else is v1+ and should be filed accordingly.

### Risks the doc currently understates

| Risk | Why it's understated | What to do about it |
|---|---|---|
| Onboarding does not exist | Doc assumes onboarding is *a* source of stored slots; in reality it is the *only* user-initiated source besides watchlist edits, and it isn't built. v0 has to work without it. | v0 reads from seeded `profile.md` and from `user_theses` only. Stored-slot mutation surfaces are out of scope until onboarding is real. |
| `derive_profile` is empty when upstream data is sparse | `implicit_watchlist` and `implicit_sectors` require `user_theses.tickers` and `user_theses.sectors` to be populated. If real-user theses lack these, the cheap insight collapses. | The fidelity-audit spike above. If it fails, we need to fix the thesis-creation path before derive_profile is worth anything. |
| The eval corpus is also the calibration corpus | We are tuning the lexicon, the prompt rules, and the rubric against the same seven scenarios. Overfit risk is high. | Reserve at least 2–3 of the eventual scenarios as held-out; never look at them during prompt iteration, only at evaluation time. |
| `agent_notes` as junk drawer | Acknowledged in the doc but only mitigated by "we'll review it later". No named owner, no cadence, no kill switch. | Cut from v1 (see above). |
| First-day-credibility section is doing a lot of work before there is a first day | The whole section regulates a population (new users with single-slot inferences from `discover_trait`) that does not exist yet. | Mark the section as forward-looking. Do not gate v0 on any of its rules; re-enable when onboarding + discovery land. |
| Sparse profile threshold (≤1) is a guess | Picked without any data on slot-fill distributions. | Same treatment as the confidence floor: feature flag, no named constant, calibrate later. |

### Risks the doc gets right and should keep emphasizing

- The recite-as-data antipattern. The strongest single failure mode and the easiest to catch in eval.
- Out-of-band discovery as non-negotiable. Worth repeating in any future doc that touches it.
- Mixed-preference users break personas. Worth restating every time someone proposes a `concentration_score`-shaped field.

### What this self-review does NOT do

- Does not relitigate the north star. The scaffolding-not-content framing survives review.
- Does not propose a different schema. Storage decisions are deferred, not redesigned.
- Does not argue against `discover_trait` on principle. Only argues against shipping it in the same release as the read path.
- Does not change the rubric strategy. The new criterion still belongs in `rubrics/response_agent.md`; it just gets written after v0 produces a real diff to grade against.

### One-paragraph TL;DR

Ship v0: derived profile + profile.md read + single-shape renderer + one rule + an eval rerun, on seeded users, with zero new schema and zero LLM calls. Use the result to decide whether the rest of this doc is justified. Drop `agent_notes` from v1. Stop naming numeric thresholds (`0.7`, `≤1`, `90d`) as if they were tuned; replace them with feature flags until there is data to tune against. Audit `user_theses` fidelity before anything else, because if the derived layer is empty in practice, the whole personalization story collapses to onboarding and we have a different problem to solve first.

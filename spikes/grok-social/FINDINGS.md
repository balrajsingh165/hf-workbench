# Findings: Grok X Search for the "Social" section (2026-06-03 live run)

Live runs against SPCE / MU / MSTR (trial harnesses and raw outputs removed after the spike settled; the winning prompt lives in `social_topics.py`).

## Approaches tested

| | Approach | Model | Shape |
|---|---|---|---|
| A | `pulse` | grok-4.20-0309-reasoning | free-form prose, per ticker |
| B | `topics` | grok-4.20-0309-reasoning | structured JSON (Social-tab schema), per ticker |
| C | `sweep` | multi-agent variant | same schema, all 3 tickers in ONE call |

## Numbers

| run | secs | $ | x_searches | citations | json | topics |
|---|---|---|---|---|---|---|
| A_pulse_SPCE | 50.8 | 0.073 | 7 | 6 | — | — |
| A_pulse_MU | 34.6 | 0.083 | 7 | 8 | — | — |
| A_pulse_MSTR | 29.9 | 0.069 | 6 | 6 | — | — |
| B_topics_SPCE | 33.0 | 0.056 | 5 | 30 | ✓ | 3 |
| B_topics_MU | 46.7 | 0.056 | 4 | 18 | ✓ | 3 |
| B_topics_MSTR | 39.0 | 0.055 | 5 | 28 | ✓ | 3 |
| C_sweep_ALL | 52.6 | 0.367 | 29 | 114 | ✓ | 7 |

## Verdict: B wins — structured per-ticker on the reasoning model

- **B is the cheapest AND most structured.** ~$0.056/ticker, 3 topics each
  correctly typed (debate/event/info/discussion), heat-ranked, bull/bear angles
  in house voice, 3–6 tweets per topic with handle/stance/claim/engagement.
  Asking for JSON *reduced* cost vs free-form prose (less rambling output).
- **A (free-form)** reads well and surfaces the same topics, but gives fewer
  per-post citations and would need a second parsing pass to render UI
  components. No reason to use it for product data.
- **C (multi-agent)** is impressive — 29 X searches in 52s, all 21 tweet URLs
  annotation-backed — but 2.2× the per-ticker cost ($0.122 vs $0.056) and
  *shallower*: 2–3 topics/ticker with fewer tweets each. Per-ticker calls in a
  thread pool beat it on depth and cost at equal wall-clock. Reserve
  multi-agent for a future market-wide "what's hot today" sweep where one call
  must rank across many tickers.
- **Cross-validation for free:** A, B, and C independently surfaced the same
  top topics per ticker (MSTR's first BTC sale since 2022, MU CEO's $38M sale
  + HBM sold out into 2027, SPCE's SpaceX-typo squeeze + Delta-class tests) —
  the topic layer is stable across runs, not prompt-noise.

## Tweet verifiability (the load-bearing finding)

Tweet URLs inside the model-written JSON are **verifiable against the API's
`url_citation` annotations by status ID** (annotations use `x.com/i/status/{id}`,
the JSON uses `x.com/{handle}/status/{id}` — join on the numeric ID):

- B: SPCE 12/12, MSTR 13/13, MU **9/12** backed. C: 21/21.
- Production rule, mirroring `src/news/verifier.py`: **drop any tweet whose
  status ID is absent from annotations**. Deterministic, free, kills
  hallucinated sources. The 3 unbacked MU tweets are exactly the failure mode
  this catches.

## Angle-readability prompt test (V0–V7)

The original one-sentence angle rule produced dense clause-chains. Four
variants run on MSTR + MU (8 calls, all JSON-valid, same ~$0.05-0.09/call):

- **V0 one-sentence (baseline)** — dense, over-compressed. The complaint.
- **V1 claim-then-reason** — *winner*. "1-2 sentences: first states the
  conviction in plain words, second gives the strongest evidence; one idea per
  sentence, never chain facts into one clause." Consistently readable across
  every topic AND kept specificity (10b5-1 rule, supply-constraint detail).
- **V2 analyst-voice** — ignored its own "2 short sentences" rule and emitted
  40+-word triple-clause sentences. Style-by-example underspecifies; the model
  mimics the register, not the length.
- **V3 hard limits (≤20 words, ≤1 number/sentence)** — over-constrained:
  staccato fragments, lost the concrete numbers that make angles credible.

Round 2 — blend V1 structure with V2's quantitative grounding (user call:
numbers improve accuracy, clause-chains don't):

- **V4 quant, no guard** — "anchor in the concrete numbers traders cite"
  alone let clause-stacking creep back (MU bull chained 3 facts).
- **V5 quant + density guard** — "single most telling number, introduced with
  context; at most two numbers per sentence" stayed clean AND quantified.
  Only tic: redundant "Bulls declare…/Bears state…" prefixes.
- **V6 = V5 + "never open with attribution"** — *final*. Consistent across
  all topics on both tickers: plain-words claim, then one quantified evidence
  sentence ("It sold just 32 of its 843000 BTC at $77135 each.").

Round 3 — V6 was overfit to number-rich treasury/insider topics (and embedded
an MSTR exemplar). Re-test on a deliberately diverse set — BA (qualitative
safety/exec), TSLA (narrative), GLD (TA/macro debate), UNH (regulatory) —
V1 vs V6 vs **V7** ("evidence = the most telling concrete detail traders
actually cite: a figure, a dated event, a named actor, or a specific claim;
use a number only when the discussion genuinely turns on it; never force or
invent precision"). 22 angles per variant:

| | zero-number angles | attrib prefixes | forced/absurd numbers |
|---|---|---|---|
| V1 | 15/22 (too vague: "Strong order backlog drives revenue growth") | 13 | — |
| V6 | 3/22 (forces numbers everywhere) | 6 | yes: "Elon referenced safety paranoia 7 times", "RSI 5 … 2 attempts", a random account's "$6300 in realized gains" as evidence |
| V7 | 6/22 (numbers where they matter) | **0** | none; qualitative topics get named actors + dated events ("CEO Kelly Ortberg stated on May 27 that roughly 80% of flight tests stand complete") |

**V7 is final** (now in `social_topics.py` STYLE). Known residuals, at low rate in
all variants, to be enforced by a deterministic verifier rather than more
prompt tuning: occasional >2-numbers sentences on valuation-heavy topics
(UNH), occasional hedge words ("may reflect"). Checks are trivial
(regex count per sentence; hedge-word list).

Lesson matching `feedback_define_policy_before_tuning`: structural rules
("one idea per sentence", numeric density caps) beat both vibes (V2) and hard
counters (V3). Encouraging numbers WITHOUT a density cap (V4) regresses to
clause-chains; MANDATING a number (V6) fabricates significance on
qualitative topics. Make evidence-type conditional on the discussion (V7).

## Cost model

~$0.056/ticker/refresh. 60 tickers × 1 refresh/day ≈ **$3.40/day**; hourly
refresh of a 20-ticker hot list ≈ $27/day. Usage carries exact per-call cost
(`usage.cost_in_usd_ticks` / 1e10) — log it like the pipeline logs Gemini cost.

## Production sketch (not built — next step if the spike graduates)

1. `social_topics(ticker)` = approach-B prompt + schema, reasoning model,
   `from_date` = today-3.
2. Verifier: JSON parses; kinds/heat in enum; every tweet status-ID
   annotation-backed; ≥1 tweet per topic survives, else drop topic.
3. Ticker selection: reuse the trending lane's Tier-1 list (`ticker_trends`)
   — the demand signal already exists.
4. Persist like stories: `social_topic` row + tweets, refreshed per cycle;
   timeline = topics ordered by (date, heat).

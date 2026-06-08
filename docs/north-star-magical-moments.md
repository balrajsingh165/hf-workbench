# North Star: Magical Moments

**Purpose.** This doc is not a plan — it is the list of felt experiences we are trying to earn. Every plan in `docs/plan-*.md` exists to make one of these moments land. If a proposed feature doesn't move a moment on this list closer to reality, it is probably the wrong feature.

**How to use this doc.**
- Read before starting a non-trivial plan, to test that the plan ladders up.
- Each moment is described from the **user's perspective**, in the present tense, with no hedging. That's the bar: we have earned the right to say it exists only when it feels like this.
- Rankings are opinionated. Tier 1 is the product's identity — if we ship everything except Tier 1, we have a worse Bloomberg terminal. If we ship Tier 1 well and nothing else, we still have a real product.
- Update the list as we learn. Moving an item up or down a tier is normal. Removing an item means we consciously decided it's not us.

---

## What makes a moment "magical" here

Not all delight is equal. A moment belongs on this list only if it does three things at once:

1. **Shows the system read the thesis.** Everything we do is downstream of a user-authored belief. If the moment could have been triggered by keyword or ticker alone, it is not magical — every app does that.
2. **Lands in seconds, not hours.** Magic decays with latency. A correct answer in five minutes is a report; a correct answer in five seconds is a presence.
3. **Talks like a partner, not a terminal.** Confident, specific, direct. No "may", "could", "might consider". If the system has an opinion, state it. The user's theses are biased — that is the point. Ours can be too.

If a moment fails any of these three, fix the moment before shipping.

---

## Tier 1 — Must-feel moments. These are the product's identity.

### 1. Thesis sharpening + instant backfill

**The moment.** The user types a fuzzy belief into the thesis creation prompt — "oil will spike because of Iran." The agent pushes back: asks for a horizon, proposes tickers the user hadn't considered, forces two or three concrete invalidation conditions. Within thirty seconds the thesis is created, and before the user has finished reading the confirmation:

> Created. I already found 2 supporting signals from the past week and 0 stresses. Your timeline is seeded.

**Why it works.** This is the on-ramp. Every user hits this moment on Day 1, and the quality of this minute decides whether they come back. What makes it differentiating: the AI isn't paraphrasing. It is injecting market knowledge the user didn't have (ticker choice, horizon realism, invalidation sharpness) and then *connecting the result to the live world* immediately. An empty thesis list after creation is inert. A timeline seeded with real signals is alive.

**Dependencies.** Thesis creation flow with AI challenge/sharpen loop. `match_story_for_thesis` backfill on creation (shipped). Rapid feedback in the CLI or UI after insert.

**Counterweight.** If the sharpener is hedgy or the backfill returns zero signals on most new theses, this moment goes from magic to frustrating. Quality of the creation dialogue is not optional.

**Shipping surface.** CLI first (text dialogue), UI later (same flow).

---

### 2. The next-day digest that is personal, confident, and brief

**The moment.** The user opens the app once a day. They get something like:

> **Fed pivot delayed** strengthened today. PCE revisions confirm your view. Score 82 → 88.
>
> **Energy crunch** is now **STRESSED**. The Iran ceasefire removes the Hormuz premium you named as an invalidation. Score 78 → 41. Review it.
>
> You're missing a conviction: nuclear renaissance news hit three times this week. Here's a sharp thesis on CCJ/CEG — say `add` to adopt it.

**Why it works.** This is the retention moment — Tier 1's recurring half. What separates it from every other market summary: every sentence is anchored to *this user's* beliefs. The tone does not hedge. The digest is short enough that reading it is not a task. And it proactively names a gap, which signals that the system understands both the market and the user's portfolio shape, not just what the user typed.

**Dependencies.** Scoring system (MVP shipped spec, code pending — see `docs/plan-scoring-system.md`). UX label derivation layer. Digest composer (unwritten). The "missing conviction" line depends on a thesis-generation surface that produces sharp, shared theses on demand — see `docs/design-thesis-creation.md`; until that surface exists, drop the line and ship the digest without it.

**Counterweight.** A digest that leads with a weak line will lose the user's trust for the rest of it. Rank items hard. If the best line is "Score 61 → 62, stable", either say nothing or surface the underlying signal instead of the number.

**Shipping surface.** CLI first (markdown or ANSI). Email and mobile later. The digest must read well as plain text.

---

### 3. Semantic stress flip without shared tickers

**The moment.** A news article that never mentions the user's ticker flips a thesis to `STRESSED` because it hits an invalidation condition semantically:

> Trump extended the Iran ceasefire → your **Energy supply crunch** thesis is now `STRESSED`. The ceasefire removes the Hormuz premium, matching your invalidation "geopolitical supply risk abates." Confidence 0.87.

**Why it works.** Users are trained by every other app to expect keyword or ticker alerts. Ticker-overlap alerts are noisy and mostly uninteresting. What lands here is the inverse: the system connected an abstract user-authored condition to a concrete news event through meaning, not strings. That is the first moment the user stops thinking of the system as a feed and starts thinking of it as a reader.

**Dependencies.** Matching pipeline with invalidations as first-class retrieval targets (shipped — `docs/ref/thesis-news-matching-system.md`). Stress flip rule in scoring (planned). Delivery path: digest is fine; push is better when we have one.

**Counterweight.** A false-positive stress is catastrophic here because the user is primed to trust it. The two-tier ladder (`STRESSED` only when `confidence >= STRESS_FLIP_CONF` + named invalidation; `TENSION` below that) exists precisely to keep this moment honest. Under-trigger beats over-trigger for this one.

**Shipping surface.** Mostly lands through the digest. Real-time surfaces come later.

---

## Tier 2 — Supporting magic. Ship after Tier 1 is steady.

### 4. Resolution ceremony

**The moment.** The user types `close` on a thesis. Instead of a silent state transition, they get a two-paragraph narrative written back to them:

> You opened **TSMC supplier tailwind** on 2026-02-14 at score 71. It strengthened through Q1 as Apple's hardware reshuffle confirmed the leg you named. The invalidation you worried about — a foundry-capacity glut — never tripped. You closed it today as `correct`, score 84. Average freshness over its life: 62.
>
> You'll likely want to know: the thesis that most often appeared alongside it in your digest was **AI infrastructure capex**. That one is still active.

**Why it works.** Most finance apps let you close a position and move on. The narrative makes the thesis feel like it was *lived through*, which gives the user a reason to close theses intentionally instead of letting them rot. This is also where the product's accumulated data pays off: only we know what happened to the thesis because only we watched it the whole time.

**Dependencies.** Thesis close command. A lightweight narrative generator that reads `thesis_story_links` history + score trajectory. No new model capability needed.

**Counterweight.** If the narrative is generic ("your thesis was active for N days and then you closed it") it reads worse than silence. Write the prompt as if it were a trading journal entry by a human who cared.

**Shipping surface.** CLI text output on close. Archived as a markdown file under the user's directory so the user keeps a journal they own.

---

### 5. Tension insight — one thesis strengthening, another straining on the same signal

**The moment.** Even when everything looks green, the digest says:

> Your **AI adoption gap** thesis strengthened — ServiceNow beat. But NVIDIA at record highs creates tension with your **Semiconductor mean-reversion** thesis. The enabler leg isn't cooperating with the adopter leg. Worth a second look.

**Why it works.** It shows the system can hold two competing ideas at once — which is what a skilled analyst does and what most automated tools cannot. This is also how we differentiate from single-thesis tools: once a user has three or four theses, the relationships *between* theses are where the interesting reasoning lives, and no competitor can replicate this without the full user-thesis graph.

**Dependencies.** Cross-thesis analysis pass — given two theses a user owns and a shared signal, does one's implied direction contradict the other's? Small LLM pass; the data is already in `thesis_story_links`.

**Counterweight.** If every digest has a tension line, tension becomes noise. Rule of thumb: at most one tension per digest, and only when the contradiction is concrete enough to name.

**Shipping surface.** Digest section, gated behind "user owns 3+ theses."

---

### 6. Upcoming-event tripwire at thesis creation

**The moment.** The user creates a thesis. Ten seconds in, after the backfill, they also see:

> By the way — CPI prints next Tuesday. Two of your invalidations are tied to CPI surprises. I'll watch that release and ping you if either trips.

**Why it works.** The backfill in moment #1 connects the thesis to the past. This one connects it to the *future*. It turns the thesis creation from a one-shot setup into a standing arrangement. "I'll watch" is the voice of a partner, not a dashboard.

**Dependencies.** Economic calendar integration (small — FRED/BEA release calendar is public). Invalidation-to-event semantic match (same pipeline as stress detection, different query side).

**Counterweight.** Only works if the follow-through actually happens. If we say "I'll watch CPI" and the CPI print arrives and nothing pings, the user never trusts a promise like this again. Do not ship this moment before the scheduling mechanism behind it is reliable.

**Shipping surface.** Thesis-creation confirmation screen, then push on the event day.

---

## Tier 3 — Exploratory. Worth keeping on file; do not plan around yet.

### 7. Accountability loop

**The moment.** A thesis has been in `STRESSED` state for twelve days and the user hasn't acted. The digest says directly:

> Your **Energy crunch** thesis has been stressed for 12 days. In your profile you said you close stressed theses within 5. What's going on?

**Why it's on the list.** The product's thesis is that most retail investors silently ignore broken beliefs. A system that calls out the silence — gently but directly — is something users have never experienced. It also closes the loop with the resolution ceremony (#4): theses don't rot if something asks about them.

**Why Tier 3.** Risk of being annoying. Needs a lot of tone calibration and per-user configurability. Worth experimenting with once engagement data exists.

---

### 8. Community adoption — "other traders like you hold this thesis"

**The moment.** The user opens the app and the digest surfaces a globally-seeded macro thesis with adoption signal:

> 47 traders with horizons like yours adopted **Dollar debasement — gold over Treasuries** in the last two weeks. Here's the thesis. Say `adopt` to take it.

**Why it's on the list.** This is how the N:M ownership model pays off — a thesis is a shared object, and adoption is a signal a solo-user app can't produce. See `docs/design-macro-context-seeded-theses.md`.

**Why Tier 3.** Needs a non-trivial user base for the adoption number to mean anything. Cold-start problem. Until we have thousands of users, the "47 traders" line is a lie.

---

### 9. Stance briefing — one-paragraph read-aloud of where each thesis stands

**The moment.** Before market open, the user runs one command and gets a 30-second read:

> Fed pivot delayed — strong. Last support: yesterday's PCE print. Next event to watch: CPI Tuesday.
>
> Energy crunch — stressed. The ceasefire hit your invalidation. Score at 41. You should look at this.
>
> TSMC supplier tailwind — quiet. Nothing meaningful in a week. Score decaying — currently 58.

**Why it's on the list.** A pre-market briefing in the user's own voice back to them is something every serious trader already does in their head. Doing it for them turns the system into a morning ritual, which is the strongest retention hook available.

**Why Tier 3.** Overlaps heavily with the next-day digest (Tier 1 #2). The question is whether briefing and digest should be two surfaces or one. Revisit once #2 has been in users' hands for a few weeks.

---

## Live data beyond news — the Mesh access path

News alone validates the matching pipeline and the freshness leg of scoring, but four of the nine moments above (Tier 1 #2 digest, Tier 2 #4 resolution, Tier 2 #5 tension, Tier 3 #9 stance briefing) only become *felt* once the system can speak about current price action, earnings context, macro prints, and filings — i.e. the live market, not just its coverage.

We have that access path. `src/clients/mesh.py` is a thin sync/async client over the Heurist Mesh REST API (`MESH_API_ENDPOINT` / `MESH_SCHEMA_ENDPOINT` in `src/config.py`), with convenience wrappers for:

- **Yahoo Finance** — `quote_snapshot`, `price_history`, `technical_snapshot`, `market_overview`, `equity_overview`, etc. Powers `score_tailwind`, the retrospective price arc in resolution, and cross-thesis tension detection.
- **FRED** — macro series (CPI, PCE, unemployment, Treasury yields). Powers the "Fed pivot delayed — PCE revisions confirm" kind of digest line, and upcoming-event tripwires.
- **SEC** — filings access. Powers thesis-grounding on company-level events (8-K / 10-Q).
- **Exa digest tools** — richer web context when news coverage is thin.

Staging rule of thumb: **do not wire live price data before Freshness-only scoring ships.** The planned first use is a narrow `weekly_return(tickers, as_of)` pull driving `score_tailwind` — that alone unlocks Tier 1 #2 and makes #4 and #5 reachable. Everything beyond that (real-time intraday, earnings calendar integration, macro release hooks) is staged against specific moments as they get built, not wired speculatively.

Practical implication: the matching pipeline is feature-complete on news. The north-star moments are not. Mesh-backed live data is what closes the gap between "impressive demo" and "product."

---

## What deliberately doesn't belong here

- **Generic market summaries, sector dashboards, ticker-level alerts.** Those are not our job. Every other app does them.
- **Portfolio accounting** (P&L, position tracking, tax lots). Adjacent but not us. A thesis is not a position; a position is not a thesis.
- **Social features** (follow other traders, share theses publicly). Possible later, but they are not the north star. The magic above is a single user's belief system being taken seriously.
- **Backtesting over historical data.** Tempting, but the product premise is that a thesis is a *living* bet, not a historical curve. Backtesting would push us toward quantifying things that our whole point is to keep qualitative.

---

## Maintenance

- When a plan in `docs/plan-*.md` ships a meaningful step toward one of these moments, note the progress inline under that moment's **Dependencies** line (e.g., "invalidation backfill shipped 2026-04-24").
- When we think of a new moment, add it at the bottom of the most appropriate tier and argue the ranking in the commit message. Do not renumber — slot as 10, 11, etc.
- This doc is allowed to be wrong. It is not allowed to be stale.

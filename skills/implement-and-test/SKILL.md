---
name: implement-and-test
description: "Quality-driven implementation loop for hf-workbench features. Use when given a masterplan item or any feature to build. Use for any item from docs/masterplan-production.md. Trigger phrase: `use implement skill`"
---

# Implement and Test

## Overview

This skill drives a three-phase loop: discover → implement → test qualitatively. It exists because quality problems in this system are *textual* — a bad judge prompt produces confident-sounding wrong rationale; a bad synthesis prompt produces vague bullets; a bad digest reads like a dashboard. You can only catch these by reading the actual output. No unit test tells you the digest is declarative enough.

Read `docs/masterplan-production.md` first for the full feature context, then follow the phases below.

---

## Soft launch context and prioritization

**Target:** a few hundred external users. Not internal-only, not a private beta with 5 people — real users who don't know the codebase and have no tolerance for silent failures, broken auth, or confusing output.

Before implementing anything, classify the item:

### Soft launch blockers — must ship before any external user touches the product

A blocker is anything where a real user hitting it would either:
- **Lose trust immediately** — broken auth, user_id query param leaking across users, CORS exposing the API to anyone
- **See corrupted or nonsensical output** — unmapped tickers, null scores when there should be numbers, empty digest
- **Lose their data silently** — ingest writes that don't persist, thesis creation that completes but never saves, score deltas that never update
- **Break the core loop** — thesis creation, daily digest, and scoring are the product; if any of these is broken or absent, there is no product

### Nice-to-haves — can ship post-soft-launch

Features where a real user either won't notice the absence yet, or whose absence degrades experience without breaking it:
- Redis resumable streams (dropped connections degrade gracefully)
- Auto stress-flip (explicitly needs calibration data that won't exist pre-launch)
- Resolution ceremony narrative (close works; the ceremony is enrichment)
- Tension insight, upcoming-event tripwire, accountability loop (Tier 2/3)
- Full test suite, S3 migration, dedup scaling validation
- Macro context frames (thesis creation works without them; quality is lower)
- Historical brief browsing, thesis sharing
- LLM cost telemetry (useful for the team, invisible to users)

### When you're not sure — stop and ask

If you're looking at an item and cannot confidently answer "would a real user notice or be harmed if this were missing at launch?" — **do not make the call yourself**. State what you found, explain the tradeoff, and ask the user explicitly:

> "This item [X] affects [Y]. My read is [blocker / nice-to-have] because [reason]. Do you want me to include it in this sprint or defer it?"

Do not silently deprioritize something that might be a blocker. Do not silently implement something that might be a nice-to-have and consume time that should go to blockers. The user decides scope; you implement.

**The same rule applies mid-implementation.** If you discover during implementation that a feature requires a non-trivial dependency you didn't see during discovery (a new table, a new LLM call, a new client), stop and report it before building it. Do not expand scope without explicit approval.

---

## Phase 1 — Discovery (do this before writing a single line. You should enter plan mode)

**Goal:** understand what already exists that you can build on or must not break.

1. **Read the item's design doc.** Every masterplan item links to a design doc or plan doc. Read it fully. If there is no link, read the relevant section of `docs/plan-p0-p2-slice.md`, `docs/north-star-magical-moments.md`, or `docs/mock-ux-walkthrough.md` to understand the product intent.

2. **Find the closest existing analog.** Before implementing anything, grep for a similar pattern:
   - New agent? Read the most similar one in `agents/` first.
   - New API endpoint? Read an existing endpoint in `app.py`.
   - New LLM prompt? Read a working prompt in `agents/prompts/` or `src/agent/prompt_manager.py`.
   - New DB table? Read `db/schema.py` and `docs/sop-schema-change.md`.

3. **Audit the relevant data.** Read the most recent 3–5 sample markdown files that the feature will read or write:
   - `global/theses/thesis_001.md` — thesis format
   - `global/stories/story_001.md` — story format
   - `users/user_1/` — user context

4. **Check `TODO.md`** for deferred items that touch this feature. Don't accidentally build something that was deliberately deferred with a known reason.

5. **Note what must not change.** Grep for callers of any function you plan to modify. List them before touching the function.

---

## Phase 2 — Minimal implementation

**Goal:** smallest change set that meets the spec. No more.

### Rules (non-negotiable)

- **No new DB columns** unless the design doc explicitly calls for them. The schema is the source of truth for structured data; narrative data lives in markdown.
- **No new tables** unless the design doc shows the schema.
- **No backwards compat shims.** The product is not launched. If something old is wrong, change it directly.
- **No feature flags** for things that should just be on.
- **No new dependencies** without checking if an existing client or helper already does the job (`src/clients/`, `src/thesis/`, `src/news/`).
- **No config knobs** for things that shouldn't be configurable yet. A constant is fine; a settings file is not.
- **Match the file's existing style.** Same import order, same error handling pattern, same logging style. Don't introduce a new pattern just because you prefer it.

### For prompts specifically

- Start with the simplest prompt that could plausibly work. Longer is not better.
- Enforce the product tone: declarative, no hedging ("may", "could", "might"), no neutral stances.
- Bake constraints into the prompt as hard rules, not soft suggestions.
- If the design doc has a schema for JSON output, use it exactly — don't invent fields.

### For new API endpoints

- Copy the shape of the closest existing endpoint in `app.py`.
- Return only the fields the frontend mock shows. Don't add speculative fields.
- Use the existing `db()` context manager. No new DB abstraction.

---

## Phase 3 — Qualitative testing and iteration

**Goal:** read the actual output and verify it meets the product bar. Iterate on the root cause until it does.

Run `uv run python` for all scripts (correct venv and dependencies).

---

### News synthesis quality

```bash
# Preview cluster route decisions without synthesis.
uv run python -m agents.route_news_clusters --dry-run --limit 50
# Then promote a small batch for real.
uv run python -m agents.route_news_clusters --write --top 3 --limit 50
```

Read the generated `global/stories/story_NNN.md`. Check each criterion:

| Criterion | Pass | Fail |
|---|---|---|
| Headline | One declarative sentence, no hedging | "Markets may react to..." |
| Bullets | Each bullet is one fact with a source citation number | Vague generalization with no source |
| Tickers | Story tickers exist in `instruments` or `pending_instruments` | Unmapped malformed symbol |
| Body length | 3–6 bullets | 1 bullet (too thin) or 10+ (too verbose) |

Verify tickers:
```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('db/hf.db')
rows = conn.execute(\"SELECT entity_id, symbol FROM entity_tickers WHERE entity_type='story' ORDER BY entity_id DESC LIMIT 20\").fetchall()
for r in rows: print(r)
"
```

---

### Thesis match quality

```bash
# Run matching for a specific thesis
uv run python -m agents.match_story_for_thesis --thesis thesis_001
```

Read the rationale in `thesis_story_links`:
```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('db/hf.db')
rows = conn.execute('''
  SELECT tnl.story_id, tnl.relation, tnl.confidence, tnl.rationale
  FROM thesis_story_links tnl
  WHERE tnl.thesis_id = 'thesis_001'
  ORDER BY tnl.confidence DESC LIMIT 10
''').fetchall()
for r in rows:
    print(f'[{r[1].upper()} {r[2]:.2f}] {r[0]}')
    print(f'  {r[3]}')
    print()
"
```

Check each rationale:

| Criterion | Pass | Fail |
|---|---|---|
| Specificity | Names a fact from the story | "This story is related to the thesis" |
| Relation direction | Relation matches the rationale's argument | Rationale says "supports" but judge returned "stresses" |
| Confidence calibration | High confidence (≥0.80) only when rationale is unambiguous | 0.85 confidence with "maybe" in the rationale |

---

### Thesis discovery quality

```bash
# Dry-run discovery on sample stories (no DB writes)
uv run python scripts/test_discover_titles.py
```

Apply the quality bar from `docs/design-thesis-creation.md`:

| Criterion | Pass | Fail |
|---|---|---|
| Length | Statement ≤ 20 words | "Given recent developments, there may be..." |
| Declarative | Present tense, no hedging | "Tech stocks might benefit..." |
| Tickers | At least one, direction per ticker | Missing direction, generic sector name |
| Invalidations | 2–3 concrete, observable events | "Things get worse" |
| Durability | Belief survives beyond the headline | "Stock X went up today" |
| Not a duplicate | Passes the similarity check | Near-identical to an existing thesis |

Non-finance stories must produce `has_thesis: false`. Test:
```bash
# Should reject: sports, entertainment, non-market topics
uv run python -c "
from src.thesis.discover import discover_thesis
from pathlib import Path
result = discover_thesis('Taylor Swift sells out world tour for third year.', Path('db/hf.db'))
assert result.action == 'none', f'Should have rejected: got {result.action}'
print('PASS: non-finance article rejected')
"
```

---

### Agent / Sage response quality

```bash
# Start server in protocol-smoke mode (no Bedrock needed)
HF_AGENT_PROTOCOL_SMOKE=1 uv run uvicorn app:app --host 0.0.0.0 --port 8088 &
# Run the chat smoke test
uv run python scripts/smoke_ai_sdk_chat.py
```

For live Bedrock mode, run without `HF_AGENT_PROTOCOL_SMOKE=1`.

Read the streamed response text. Apply the product tone rules:

| Criterion | Pass | Fail |
|---|---|---|
| Declarative | "The thesis is under pressure." | "The thesis may be under some pressure." |
| No hedging | No "may", "could", "might", "perhaps" | "You might want to consider..." |
| Specific | Names the story, the ticker, the invalidation | "Recent stories are mixed" |
| Chip-appropriate | Response matches the chip's stated purpose | Counterpoints chip that agrees with everything |
| Length | Tight enough to read in 30 seconds | 5-paragraph essay |

---

### Score quality

```bash
uv run python -m agents.score_theses
```

Read the scores and verify 3 theses manually:
```bash
uv run python -c "
import sqlite3
conn = sqlite3.connect('db/hf.db')
rows = conn.execute('''
  SELECT t.id, t.score, t.score_freshness, t.score_tailwind, t.status
  FROM user_theses t WHERE t.user_id = 'user_1'
  ORDER BY t.score DESC
''').fetchall()
for r in rows: print(r)
"
```

For each of the 3 highest and 3 lowest scoring theses, read `global/theses/thesis_NNN.md` and `global/stories/story_NNN.md` for its most recent linked story. Verify:

| Check | Pass |
|---|---|
| High freshness (≥ 70) | Last support arrived within the thesis horizon window |
| Low freshness (≤ 30) | Last signal is older than the horizon |
| High tailwind (≥ 70) | Price action on tickers agrees with thesis direction |
| Low tailwind (≤ 30) | Price moving against thesis |
| Composite band | Numbers match what the UI would say ("Strong" / "Active" / "Critical") |

---

### Digest quality (cold-read test)

```bash
uv run python -m agents.daily_digest --user user_1
cat users/user_1/digests/$(date +%Y-%m-%d).md
```

Apply the cold-read test from `docs/masterplan-production.md §3.7`: read the digest with no prior context and answer:

1. What does this user believe about the market right now?
2. What happened to those beliefs today?
3. What is one action the user should take?

If you cannot answer all three clearly within 60 seconds, the digest fails. Iterate on the prompt.

Additional checks:
- Every thesis line leads with Strength + prescription. "Fed pivot — Strong at 88. Holding." Not "Your Fed pivot score is 88."
- Score deltas present (`+7 since yesterday`).
- At least one orphan news item in the "what conviction am I missing?" section.
- No "market conditions are complex" or similar filler.

---

## Iteration decision tree

When the output fails a quality check, identify the root cause before changing anything:

```
Output is wrong →
  Is the input data wrong?
    Yes → fix the upstream writer (e.g., synthesis prompt, ticker extraction)
    No → Is the prompt wrong?
      Yes → edit the prompt; re-run the same input
      No → Is the logic wrong (scoring formula, aggregation, threshold)?
        Yes → fix the logic
        No → Is the test case unusual? → add it to the known edge cases; do not special-case in code
```

**Never add complexity to paper over a quality problem.** If the judge is returning wrong relations, fix the judge prompt — don't add a post-hoc filter that corrects its output.

**Never add a hack to make one test case pass.** The fix must generalize.

---

## Done criteria

An item is done when:

1. The feature runs end-to-end without errors on real data.
2. All qualitative checks for the relevant output type pass (section above).
3. No new warnings, silent failures, or unmapped tickers introduced.
4. If you added a prompt: it was tested on at least 5 real inputs, including at least one that should *fail* the quality bar (to verify the rejection case works).
5. If you changed a scoring formula or threshold: you spot-checked the before/after on 3+ real theses and the numbers moved in the right direction.
6. For soft launch blockers: the user-facing failure mode has been eliminated. A real external user hitting this path gets a correct result, not a silent failure or a confusing output.

**Do not mark done based on "it runs."** The product bar is whether the output could appear in `docs/mock-ux-walkthrough.md` and feel non-obvious.

**Do not mark a nice-to-have done and claim it unblocks launch.** If you built a nice-to-have, report it as such and confirm with the user that the adjacent blocker items are still the next priority.

---

## Key reference files

| File | Read for |
|---|---|
| `docs/masterplan-production.md` | Item context, dependencies, what "done" means |
| `docs/north-star-magical-moments.md` | The product bar for each output type |
| `docs/mock-ux-walkthrough.md` | Exact UI text the output feeds into |
| `docs/plan-scoring-system.md` | Scoring spec, band thresholds, prescription text rules |
| `docs/design-thesis-creation.md` | Thesis quality bar, discovery rejection scenarios |
| `db/schema.py` | Authoritative data model |
| `CLAUDE.md` | Architecture principles (markdown-first, no overlap, no backcompat) |
| `TODO.md` | Deferred items — don't accidentally build what was deliberately deferred |

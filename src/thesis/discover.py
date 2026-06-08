"""Thesis discovery: extract actionable market beliefs from raw context.

Given freeform text (news summary, user note, macro commentary), the discovery
pipeline determines whether a novel thesis exists, checks for duplicates
against the existing index, writes the markdown file, inserts the DB row,
and runs the post-creation pipeline (embedding, news backfill, promotion).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.clients.gemini import (
    GEMINI_3_1_PRO_PREVIEW,
    GEMINI_3_FLASH_PREVIEW,
    batch_embed_contents,
    embed_content,
    generate_text_with_retry,
)
from src.embedding import cosine_similarity
from src.instruments.resolver import all_active as all_active_instruments
from src.instruments.resolver import exists as instrument_exists
from src.i18n_translate import write_translation_sidecars
from src.thesis.docs import (
    ThesisChunk,
    ThesisDocument,
    build_thesis_chunks,
    parse_thesis_markdown,
)
from src.thesis.match_index import (
    THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
    THESIS_MATCH_EMBEDDING_MODEL,
    ensure_match_index_schema,
    get_db_connection,
    search_dense,
)
from src.thesis.scoring import clamp_horizon
from src.thesis.story_links import ThesisStoryLink


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DiscoverResult:
    action: Literal["new", "existing", "none"]
    thesis: ThesisDocument | None = None
    existing_thesis_id: str | None = None
    similarity_score: float | None = None
    rationale: str | None = None


PromotionStatus = Literal["active", "candidate", "rejected"]


@dataclass(slots=True)
class _PromotionDecision:
    status: PromotionStatus
    log_message: str | None = None


# Cosine in [RERANK_LOW_THRESHOLD, DUPLICATE_SIMILARITY_THRESHOLD) is a
# borderline match — routed to LLM rerank because dense-embedding cosine
# clusters by topic adjacency, not actionable-belief identity (eval 2026-05-07
# found 3/6 false positives at the prior 0.80 cutoff).
RERANK_LOW_THRESHOLD = 0.80
DUPLICATE_SIMILARITY_THRESHOLD = 0.88


DUPLICATE_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_duplicate": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["is_duplicate", "rationale"],
}


DUPLICATE_JUDGE_SYSTEM_PROMPT = """\
You judge whether two market theses describe the same actionable belief.

POLICY: Two theses are duplicates only if they describe the same actionable \
trade with the same direction. Sharing a macro topic, sector, or even a \
ticker overlap ALONE does NOT make them duplicates.

DUPLICATE example:
- "Strait of Hormuz tensions sustain crude risk premium" vs.
  "Stalled Mideast diplomacy keeps oil bid"
  → SAME trade (long oil on Mideast supply risk), same direction.

DISTINCT examples (despite topic adjacency):
- "Mega-cap layoffs fund the AI capex cycle" vs.
  "CPU-side enterprise inference re-rates server OEMs"
  → Both AI infra; different layers and tickers. DISTINCT.
- "Russia-Ukraine war disrupts global energy" vs.
  "Mideast crude risk premium"
  → Both energy supply; different conflicts and trades. DISTINCT.
- "Open-source frontier models compress hyperscaler margins" vs.
  "Chinese autonomous EV market accelerates"
  → Both Chinese tech; different sub-trades. DISTINCT.

Return JSON: {"is_duplicate": bool, "rationale": "<one sentence>"}.
"""


def _log_discovery_llm_call(
    db_path: Path,
    *,
    entity_type: str,
    entity_id: str,
    caller: str,
    result: object,
) -> None:
    """Record one discovery Gemini call (tokens + cost) in ``llm_calls``,
    mirroring the synthesis path (`src/news/persist.py:_log_synth_llm_call`) so
    the daily health review sees discovery spend, not just synthesis.

    Telemetry must never take down the pipeline — any failure here is swallowed.
    """
    usage = getattr(result, "usage", None)
    try:
        conn = get_db_connection(db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO llm_calls (
                      entity_type, entity_id, caller, model_id, latency_seconds,
                      input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
                      total_tokens, cost_usd, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    """,
                    (
                        entity_type,
                        entity_id,
                        caller,
                        getattr(result, "model", "") or "",
                        getattr(result, "latency_seconds", 0.0),
                        getattr(usage, "input_tokens", 0),
                        getattr(usage, "output_tokens", 0),
                        getattr(usage, "thinking_tokens", 0),
                        getattr(usage, "cache_read_tokens", 0),
                        getattr(usage, "total_tokens", 0),
                        float(getattr(result, "cost_usd", 0.0) or 0.0),
                    ),
                )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — telemetry must not break discovery
        print(f"discover: failed to log llm_call ({caller}): {exc}", file=sys.stderr)


def _judge_duplicate_via_llm(
    candidate_statement: str,
    candidate_core: str,
    existing_thesis_id: str,
    db_path: Path,
) -> tuple[bool, str]:
    """Ask Gemini Flash whether two theses describe the same actionable belief.

    Used as a precision rerank inside the borderline cosine band. Falls back
    to ``True`` (treat as duplicate) if the existing thesis markdown can't be
    loaded — preserves Gate 2's old conservative behavior on system errors.
    """
    root = db_path.parent.parent
    existing_path = root / "global" / "theses" / f"{existing_thesis_id}.md"
    try:
        existing_doc = parse_thesis_markdown(existing_path)
    except (ValueError, OSError, FileNotFoundError) as exc:
        return True, f"existing thesis {existing_thesis_id} unreadable: {exc}"

    invalidations_block = "\n".join(f"- {ic}" for ic in existing_doc.invalidations)
    existing_text = (
        f"{existing_doc.title}\n\n"
        f"{existing_doc.core_thesis}\n\n"
        f"Invalidations:\n{invalidations_block}"
    )
    prompt = (
        "PROPOSED THESIS:\n"
        f"{candidate_statement}\n\n"
        f"{candidate_core}\n\n"
        "EXISTING THESIS:\n"
        f"{existing_text}\n"
    )
    res = generate_text_with_retry(
        contents=prompt,
        model=GEMINI_3_FLASH_PREVIEW,
        system_instruction=DUPLICATE_JUDGE_SYSTEM_PROMPT,
        thinking_level="low",
        response_mime_type="application/json",
        response_json_schema=DUPLICATE_JUDGE_SCHEMA,
    )
    _log_discovery_llm_call(
        db_path,
        entity_type="thesis",
        entity_id=existing_thesis_id,
        caller="discover_dedup_judge",
        result=res,
    )
    data = json.loads(res.text)
    return bool(data.get("is_duplicate", True)), str(data.get("rationale") or "").strip()


# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------

_TICKER_ITEMS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["bullish", "bearish"]},
            "rationale": {"type": "string"},
        },
        "required": ["symbol", "direction", "rationale"],
    },
}


DISCOVER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "has_thesis": {"type": "boolean"},
        "thesis_statement": {"type": "string"},
        "tickers": _TICKER_ITEMS_SCHEMA,
        "core_thesis": {"type": "string"},
        "invalidation_conditions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "horizon_days": {"type": "integer"},
        "rejection_reason": {"type": "string"},
    },
    "required": ["has_thesis"],
}


# Story path: one Pro call returns 0-3 distinct, strong-by-construction
# candidates. Same per-candidate shape as the single schema plus angle_label.
DISCOVER_STORY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "angle_label": {"type": "string"},
                    "thesis_statement": {"type": "string"},
                    "tickers": _TICKER_ITEMS_SCHEMA,
                    "core_thesis": {"type": "string"},
                    "invalidation_conditions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "horizon_days": {"type": "integer"},
                },
                "required": [
                    "angle_label",
                    "thesis_statement",
                    "tickers",
                    "core_thesis",
                    "invalidation_conditions",
                    "horizon_days",
                ],
            },
        },
        "rejection_reason": {"type": "string"},
    },
    "required": ["candidates"],
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_DISCOVER_INTRO = """\
You are the thesis discovery agent for Heurist Finance.

Your job: decide whether incoming context contains a novel, actionable market \
thesis. If it does, extract it as structured JSON. If it does not, reject it \
with a clear reason.\
"""


# Story path intro: read one story, propose 0-3 DISTINCT strong theses.
_DISCOVER_STORY_INTRO = """\
You are the thesis discovery agent for Heurist Finance.

Your job: read one news story and propose every DISTINCT, strong, actionable \
market thesis it supports. Usually that is zero or one. Only a genuinely \
multi-angle story yields two or three, and you never force them. Each proposal \
is a separate position a trader could hold on its own.\
"""


# Shared thesis-craft rules — identical for single-context and story
# multi-candidate discovery. Single source of truth; both system prompts
# compose it. Mode-specific phrasing (output contract, how many to emit) lives
# in each path's tail, never here.
_THESIS_QUALITY_RULES = """\
## What is a thesis?

A thesis is a single declarative sentence expressing a durable, actionable \
market belief. It is NOT a summary of news. It is a position derived from \
news, macro context, and price behavior. It should be specific enough to be \
testable and broad enough to outlive the headline that inspired it.

## Title rules (`thesis_statement`, becomes the H1 line)

The title is a working *name* for the belief. The user will see it again \
and again in digests, lists, and stress alerts. Weeks from now, encountering \
the title alone, they should instantly recall the belief it stands for.

The reader is a retail trader, not a portfolio manager or policy researcher. \
They want to understand the bet at a glance, in the same plain English they'd \
use talking to a friend over coffee. They should NOT have to mentally \
translate sell-side jargon. If a sentence reads like a Goldman client deck, \
it is wrong for this product. Rewrite it.

**The digest test.** The title will appear inside sentences like:
- "Your **{title}** strengthened today."
- "**{title}** is now stressed. Review it."
If those don't read as natural English about a recognizable belief, the title \
is wrong. Rewrite it.

### Hard rules
1. **8 to 12 words. Count them.**
2. **One claim, one direction.** Forbidden words: `while`, `despite`, `but`, \
`although`. Also forbidden: `and` joining two different verbs or two opposing \
directional claims (e.g. "widen crude *and* compress logistics"). Pick the \
dominant pattern; the bullish/bearish ticker mix lives in the `tickers` array.
3. **Name what readers will recognize.** Use the names readers actually see \
in their news feed: Tesla, Nvidia, OPEC, the Fed, China, Trump, Powell, the \
ECB, the BOE. Faceless abstractions like "executive attacks on central bank \
independence" or "depressed equity valuations" hide the actual story; "Trump \
pressuring the Fed" or "cheap UK stocks" tells the reader instantly what \
this is about. Save fully abstract framing only for genuinely cross-cutting \
themes (e.g. "rare earth export controls", "the AI capex cycle") where no \
single name captures it. When in doubt, name the recognizable thing.
4. **Use accepted abbreviations.** AI (not "artificial intelligence"), EV \
(not "electric vehicles"), Fed, BOJ, ECB, OPEC, CPI, GDP, EPS, SaaS. The \
spelled-out forms read like a press release, not a thesis name.
5. **Plain trading-floor English.** Use everyday verbs that a retail trader \
would say out loud: "pushes", "lifts", "drags", "squeezes", "caps", "boosts", \
"hits", "knocks down", "drives up", "weighs on", "fuels", "keeps", "breaks", \
"holds", "stalls". AVOID this banned vocabulary entirely: "compresses", \
"re-rates", "repricing", "structural", "valuations", "outperformance", \
"divergence", "spillover", "convergence", "premium" (except in the fixed \
phrase "risk premium"), "elevated", "headwinds", "tailwinds", "secular", \
"multiples", "broadens", "unwind", "compress", "compresses", "compressed". \
No hedging language: no "might", "could", "may", "possibly", "looks set to".
6. **Falsifiable.** A reader should be able to imagine the chart, print, or \
event that would confirm or deny it. If the title is so abstract you can't \
picture it being wrong, it's a vibe, not a thesis.
7. **No em-dashes.** Do not use the long horizontal em-dash character (—) \
anywhere in `thesis_statement`, `core_thesis`, or `invalidation_conditions`. \
Use commas, periods, or semicolons instead. (Plain ASCII hyphens inside \
compound words like "high-yield" or "long-bond" are fine; this rule bans \
only the em-dash punctuation mark.)
8. **No predicted price targets in the title or `core_thesis`.** Do NOT \
write specific price predictions like "oil past $95", "gold past $3,500", \
"SPX above 7,000", "10Y to 5.5%". Price levels are noisy and brittle and \
they make the belief feel like a forecast instead of a position. State the \
direction and the catalyst, not the number. Specific thresholds DO belong \
in `invalidation_conditions` (those need to be testable; e.g. "WTI below \
$78 for five trading days"). This rule applies only to the title and the \
core_thesis narrative.

### Title gold standard, match this register
- "The Fed won't cut rates this year, sticky inflation keeps yields high."
- "Trump pressuring the Fed pushes long-bond yields higher."
- "Cheap UK stocks attract a takeover wave from foreign buyers."
- "Rising bond yields drag crypto miners and exchanges down."
- "OPEC keeps cuts in place, oil stays bid through summer driving season."
- "AI server demand lifts Dell, HPE, and memory makers."
- "Central banks keep buying gold, the rally has more room to run."
- "China's rare earth controls boost Western mining stocks."
- "Big Tech layoffs fund the AI spending boom."

### Title rewrites, study every one of these

| BAD | Why | BETTER |
|---|---|---|
| Surging bond yields compress high-beta crypto asset valuations. | "compress" and "valuations" are banned analyst jargon; faceless. | Rising bond yields drag crypto miners and exchanges down. |
| Executive attacks on central bank independence trigger structural term premium repricing. | "structural" and "repricing" banned; hides that this is Trump vs. the Fed. | Trump pressuring the Fed pushes long-bond yields higher. |
| Depressed equity valuations trigger a structural wave of cross-border takeovers. | "valuations" and "structural" banned; vague about which market. | Cheap UK stocks attract a takeover wave from foreign buyers. |
| DeepSeek's ultra-low-cost open-source AI disrupts proprietary pricing models, compressing US hyperscaler margins while boosting early domestic integrators. | 18 words; two clauses joined by "while"; "compressing" banned. | Cheap Chinese open-source AI squeezes OpenAI and Anthropic margins. |
| Geopolitical supply shocks widen crude spreads and compress logistics margins. | `and` joins one bullish and one bearish claim; "compress" banned. | Hormuz tensions keep oil bid through the summer. |
| Aggressive technology headcount reductions fund massive artificial intelligence infrastructure cycles. | "artificial intelligence" not abbreviated; jargon-heavy. | Big Tech layoffs fund the AI spending boom. |
| Enterprise workflow platforms monetize artificial intelligence integrations to accelerate subscription growth. | double-verb chain; "artificial intelligence" not abbreviated. | AI add-ons lift enterprise SaaS subscription growth. |

## Other quality bar

1. **`core_thesis` is the long form (2 to 4 sentences).** Same plain \
trading-floor English as the title (Hard rule 5 applies here too). It should \
read like a smart trader explaining the bet to a peer, not like an analyst \
writing a client note. Name the catalyst (who and what), the mechanism (why \
prices move), the tickers, and the timeline. The full banned-vocabulary list \
applies: no "compresses", "re-rates", "structural", "valuations", \
"outperformance", "divergence", "premium" (except "risk premium"), \
"elevated", "headwinds", "tailwinds", "secular", "multiples", "broadens", \
"unwind". No em-dashes; use commas, periods, or semicolons. Read each \
sentence as if speaking it aloud to a friend. If it sounds like a research \
report, rewrite it.
2. **Tickers must come from the supplied registry.** Each `tickers[].symbol` \
MUST exactly match a `symbol` in the registry block below. Do NOT invent \
symbols, do NOT guess Yahoo's punctuation, do NOT use plain English names. \
If the instrument you want isn't in the registry, leave it out; the thesis \
will be rejected if any emitted symbol is unknown.
3. **Each ticker must be bullish or bearish.** No neutral. If you cannot take \
a side, the thesis is not ready.
4. **Durable theme, not a one-off event.** The thesis must rest on a force \
that keeps acting over the whole holding horizon: a sector or macro trend, an \
ongoing geopolitical situation, a multi-month supply-chain dynamic (upstream \
and downstream), a regime in rates, inflation, or currencies, or a real shift \
in customer behavior. Apply one test: "will this force still be moving prices \
in `horizon_days` days?" If the only thing holding the thesis up is a single \
company's own one-off event, the answer is no, and you must reject it. \
Disqualifying one-off catalysts include:
- M&A, takeovers, or buyouts a company makes or receives.
- IPOs and one-time financing or deal announcements.
- a single product launch, promo, or event (e.g. one Prime Day).
- one quarter's earnings or guidance beat or miss.
- a single trial readout, or a regulatory action against one company.
- a one-time strategic pivot.
These are headlines that get priced in once and then stop moving the stock; \
they are not durable beliefs. You MAY name a single company, but ONLY as a \
second-order beneficiary of a durable theme, never as the subject of its own \
idiosyncratic story. GOOD: "rising semiconductor demand lifts Nvidia's \
suppliers", "a SpaceX IPO pulls capital into the space sector". BAD: "Greg \
Abel's first buyout lifts Berkshire", "GlobalFoundries expanding into AI \
hardware lifts its stock", "Cava's raised guidance pushes shares higher".
5. **2 to 3 concrete, testable invalidation conditions.** Each condition \
should be a specific, observable event or data point that would falsify the \
thesis. Same plain-English register, no em-dashes.
6. **Horizon 10 to 120 days.** Set horizon_days to the number of calendar \
days you expect the thesis to remain relevant: shorter for tactical \
catalyst trades, longer for structural macro positions.\
"""


# Single-context tail: reject-or-emit-one contract.
_DISCOVER_SINGLE_TAIL = """\
## Existing theses

You will receive a list of nearby existing theses. Do NOT create a thesis \
that duplicates or substantially overlaps an existing one. If the belief is \
already covered, reject it.

## Rejection criteria

Set has_thesis=false and provide a rejection_reason when:
- The context is non-finance content.
- The context is finance-adjacent but names no clear ticker.
- The context is a pure headline reaction with no durable belief.
- The catalyst is a single company's one-off event (M&A, IPO, buyout, product \
launch or promo, one earnings or guidance print, a trial readout, a regulatory \
action, a strategic pivot) with no durable theme behind it. See quality-bar \
rule 4.
- The belief is already covered by an existing thesis.

## Output format

Return a single JSON object matching the provided schema. If rejecting, only \
has_thesis and rejection_reason are required. If accepting, all fields except \
rejection_reason are required.\
"""


# Story tail: multi-candidate selection, the inlined strength bar (this is the
# strength review — folded into generation, no separate critic call), and the
# multi-candidate output contract.
_DISCOVER_STORY_TAIL = """\
## Existing theses and already-covered angles

You will receive a list of nearby existing theses and a list of angles already \
covered by theses linked to this story. Do NOT emit a candidate that \
duplicates, substantially overlaps, or merely restates any of them. Emit only \
genuinely new angles the covered set misses.

## How many to emit

Most stories yield ZERO or ONE thesis. Return two or three ONLY when the story \
genuinely supports DISTINCT, non-overlapping beliefs a trader would hold as \
separate positions. Never invent a second angle to fill the list; fewer is \
better, and an empty list is a common, correct outcome. If two candidates \
would move on the same catalyst in the same direction, they are one angle: \
keep the stronger and drop the other.

## Strength bar (apply to every candidate before emitting)

Emit a candidate ONLY if it clears every test. If it fails any, drop it.
- Declarative and non-hedged.
- Names specific tickers from the registry.
- States one clear direction.
- Built on a durable theme (sector, macro, geopolitics, a multi-month supply \
chain, or a real behavior shift), NOT a single company's one-off event (M&A, \
IPO, buyout, product promo, one earnings print, a trial readout, a strategic \
pivot). Naming one company is fine ONLY as a second-order beneficiary of such \
a theme. See quality-bar rule 4.
- Carries two or three concrete, testable invalidation conditions.
A weak thesis that happens to parse is still a reject. When unsure, drop it.

## Output format

Return a single JSON object: {"candidates": [...], "rejection_reason": ...}. \
Each candidate carries angle_label (a short human label for the angle, e.g. \
"TSMC supplier bull"), thesis_statement, tickers, core_thesis, \
invalidation_conditions, and horizon_days. Return an empty candidates list \
with a short rejection_reason when the story supports no strong, new thesis.\
"""


# Shared closing tone — appended to both prompts.
_DISCOVER_TONE = """\
## Tone

Confident and direct. No hedging language. No neutral stances. The thesis IS \
the bias; that is the point. Plain trading-floor English, no analyst jargon, \
no em-dashes.\
"""


DISCOVER_SYSTEM_PROMPT = "\n\n".join(
    [_DISCOVER_INTRO, _THESIS_QUALITY_RULES, _DISCOVER_SINGLE_TAIL, _DISCOVER_TONE]
)

DISCOVER_STORY_SYSTEM_PROMPT = "\n\n".join(
    [_DISCOVER_STORY_INTRO, _THESIS_QUALITY_RULES, _DISCOVER_STORY_TAIL, _DISCOVER_TONE]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _embed_and_search(
    text: str,
    db_path: Path,
    top_k: int,
) -> tuple[list[float], list[tuple[str, str, float]]]:
    """Embed *text* and search thesis_match_chunks.

    Returns ``(embedding, [(thesis_id, chunk_text, score), ...])``.
    """
    embedding = embed_content(
        text,
        model=THESIS_MATCH_EMBEDDING_MODEL,
        output_dimensionality=THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
        task_type="RETRIEVAL_QUERY",
    ).embeddings[0]

    conn = get_db_connection(db_path)
    try:
        ensure_match_index_schema(conn)
        rows = conn.execute(
            "SELECT thesis_id, chunk_key, chunk_text, embedding_json "
            "FROM thesis_match_chunks"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return embedding, []

    scored: list[tuple[str, str, str, float]] = []
    for row in rows:
        score = cosine_similarity(embedding, json.loads(row["embedding_json"]))
        scored.append((row["thesis_id"], row["chunk_key"], row["chunk_text"], score))

    # Dedupe to best per thesis
    best: dict[str, tuple[str, float]] = {}
    for thesis_id, chunk_key, chunk_text, score in scored:
        prev = best.get(thesis_id)
        if prev is None or score > prev[1]:
            best[thesis_id] = (chunk_text, score)

    ranked = sorted(
        [(tid, text, score) for tid, (text, score) in best.items()],
        key=lambda x: x[2],
        reverse=True,
    )[:top_k]

    return embedding, ranked


def _next_thesis_id(db_path: Path) -> str:
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM theses ORDER BY CAST(SUBSTR(id, 8) AS INTEGER) DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    best = 0
    if row:
        try:
            best = int(row["id"].split("_", 1)[1])
        except (IndexError, ValueError):
            pass
    return f"thesis_{best + 1:03d}"


def _format_thesis_markdown(
    thesis_id: str,
    statement: str,
    core_thesis: str,
    invalidation_conditions: list[str],
) -> str:
    """Render the thesis markdown body. Tickers are stored in `entity_tickers`,
    not the markdown — see docs/design-instrument-registry.md."""
    lines = [f"# Thesis: {statement}", ""]
    lines.append("## Core Thesis")
    lines.append(core_thesis)
    lines.append("")
    lines.append("## Invalidation Conditions")
    for ic in invalidation_conditions:
        lines.append(f"- {ic}")
    lines.append("")
    return "\n".join(lines)


def _format_registry_block(max_rows: int = 200) -> str:
    """Render the instrument registry as a TSV block for the discovery prompt.

    Columns: symbol, short, display, asset_class. Tradable-only rows would be
    too restrictive (the agent should know about ^TNX even though it's not
    tradable), so we include everything active.
    """
    instruments = all_active_instruments()
    rows = ["symbol\tshort\tdisplay\tasset_class"]
    for inst in instruments[:max_rows]:
        rows.append(
            f"{inst.symbol}\t{inst.short}\t{inst.display}\t{inst.asset_class}"
        )
    return "\n".join(rows)


def _set_review_status(db_path: Path, thesis_id: str, status: str) -> None:
    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE theses SET review_status = ? WHERE id = ?",
                (status, thesis_id),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main discovery entry point
# ---------------------------------------------------------------------------


def discover_thesis(
    context: str,
    db_path: Path,
    *,
    similarity_threshold: float = DUPLICATE_SIMILARITY_THRESHOLD,
    overlap_top_k: int = 20,
) -> DiscoverResult:
    """Discover a thesis from freeform context text.

    Returns a ``DiscoverResult`` indicating whether a new thesis was created,
    an existing thesis already covers the belief, or no thesis was found.
    """
    # ------------------------------------------------------------------
    # Step 0 — Overlap retrieval: find nearby existing theses
    # ------------------------------------------------------------------
    context_embedding, nearby = _embed_and_search(context, db_path, overlap_top_k)

    nearby_block = ""
    if nearby:
        lines = ["Nearby existing theses (do NOT duplicate these):"]
        for thesis_id, chunk_text, score in nearby:
            lines.append(f"- [{thesis_id}] (similarity {score:.2f}): {chunk_text}")
        nearby_block = "\n".join(lines)

    # ------------------------------------------------------------------
    # Step 1 — LLM discovery
    # ------------------------------------------------------------------
    user_prompt_parts = [
        "## Context\n",
        context,
        "\n\n## Instrument Registry\n",
        "Use ONLY symbols from this table. Unknown symbols → thesis rejected.\n",
        "```\n" + _format_registry_block() + "\n```",
    ]
    if nearby_block:
        user_prompt_parts.append(f"\n\n## Existing Theses\n\n{nearby_block}")

    user_prompt = "\n".join(user_prompt_parts)

    result = generate_text_with_retry(
        contents=user_prompt,
        model=GEMINI_3_1_PRO_PREVIEW,
        system_instruction=DISCOVER_SYSTEM_PROMPT,
        thinking_level="medium",
        response_mime_type="application/json",
        response_json_schema=DISCOVER_JSON_SCHEMA,
    )
    _log_discovery_llm_call(
        db_path,
        entity_type="discover_context",
        entity_id=context[:64],
        caller="discover_single_generate",
        result=result,
    )

    parsed = json.loads(result.text)

    if not parsed.get("has_thesis", False):
        return DiscoverResult(
            action="none",
            rationale=parsed.get("rejection_reason", "LLM rejected — no thesis found"),
        )

    return _finalize_candidate(
        parsed,
        db_path,
        source_context=context,
        similarity_threshold=similarity_threshold,
    )


def _finalize_candidate(
    candidate: dict,
    db_path: Path,
    *,
    source_context: str,
    similarity_threshold: float = DUPLICATE_SIMILARITY_THRESHOLD,
) -> DiscoverResult:
    """Registry-ground, dedup against the existing index, and persist one
    parsed candidate. Shared by single-context discovery and the story
    multi-candidate path.

    ``candidate`` carries thesis_statement, tickers, core_thesis,
    invalidation_conditions, horizon_days (the story path adds angle_label,
    which is ignored here). ``source_context`` is written to
    ``theses.source_context`` (the originating text or story_id).
    """
    # Registry grounding — every emitted symbol must exist
    emitted = candidate.get("tickers", []) or []
    unknown_symbols = [
        (t.get("symbol") or "").strip().upper()
        for t in emitted
        if (t.get("symbol") or "").strip()
        and not instrument_exists((t.get("symbol") or "").strip().upper())
    ]
    if unknown_symbols:
        return DiscoverResult(
            action="none",
            rationale=(
                "Rejected — emitted symbol(s) not in instrument registry: "
                + ", ".join(unknown_symbols)
            ),
        )

    # Similarity check against existing index
    candidate_text = (
        candidate.get("thesis_statement", "")
        + "\n\n"
        + candidate.get("core_thesis", "")
    )
    _candidate_embedding, candidate_nearby = _embed_and_search(
        candidate_text, db_path, top_k=5,
    )

    if candidate_nearby:
        top_id, top_text, top_score = candidate_nearby[0]
        if top_score >= similarity_threshold:
            return DiscoverResult(
                action="existing",
                existing_thesis_id=top_id,
                similarity_score=top_score,
                rationale=(
                    f"Existing thesis {top_id} covers this belief "
                    f"(similarity {top_score:.2f})"
                ),
            )
        if top_score >= RERANK_LOW_THRESHOLD:
            is_dup, judge_rationale = _judge_duplicate_via_llm(
                candidate.get("thesis_statement", ""),
                candidate.get("core_thesis", ""),
                top_id,
                db_path,
            )
            if is_dup:
                return DiscoverResult(
                    action="existing",
                    existing_thesis_id=top_id,
                    similarity_score=top_score,
                    rationale=(
                        f"LLM rerank: duplicates {top_id} (cosine {top_score:.2f}) — "
                        f"{judge_rationale}"
                    ),
                )
            print(
                f"discover: rerank kept {top_id} match as distinct "
                f"(cosine {top_score:.2f}) — {judge_rationale}",
                file=sys.stderr,
            )

    # Write thesis markdown + DB row
    root = db_path.parent.parent
    thesis_id = _next_thesis_id(db_path)

    markdown = _format_thesis_markdown(
        thesis_id=thesis_id,
        statement=candidate["thesis_statement"],
        core_thesis=candidate.get("core_thesis", ""),
        invalidation_conditions=candidate.get("invalidation_conditions", []),
    )

    thesis_path = root / "global" / "theses" / f"{thesis_id}.md"
    thesis_path.parent.mkdir(parents=True, exist_ok=True)
    thesis_path.write_text(markdown, encoding="utf-8")

    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO theses "
                "(id, origin, source_context, review_status, owner_count, horizon_days) "
                "VALUES (?, 'system', ?, 'candidate', 0, ?)",
                (thesis_id, source_context[:200], clamp_horizon(candidate.get("horizon_days"))),
            )
            for ticker in candidate.get("tickers", []):
                symbol = ticker.get("symbol", "").strip().upper()
                if not symbol:
                    continue
                conn.execute(
                    """INSERT INTO entity_tickers (entity_type, entity_id, symbol, direction)
                    VALUES ('thesis', ?, ?, ?)
                    ON CONFLICT(entity_type, entity_id, symbol)
                    DO UPDATE SET direction = excluded.direction""",
                    (thesis_id, symbol, ticker.get("direction")),
                )
    finally:
        conn.close()

    try:
        write_translation_sidecars(
            thesis_path,
            entity_type="thesis",
            entity_id=thesis_id,
            db_path=db_path,
        )
    except Exception as exc:
        print(f"discover: failed to translate {thesis_id} sidecars: {exc}", file=sys.stderr)

    doc = parse_thesis_markdown(thesis_path)
    return DiscoverResult(
        action="new",
        thesis=doc,
        rationale=candidate.get("core_thesis", ""),
    )


def discover_story_theses(
    context: str,
    db_path: Path,
    *,
    source_context: str,
    covered_angles: list[str] | None = None,
    max_candidates: int = 3,
    overlap_top_k: int = 20,
    similarity_threshold: float = DUPLICATE_SIMILARITY_THRESHOLD,
) -> list[DiscoverResult]:
    """Generate 0–``max_candidates`` distinct, strong thesis candidates from one
    story and persist the survivors.

    One Pro call (multi-candidate, strength rubric inlined) produces the
    candidate set; each candidate then reuses the same registry-grounding +
    dedup-vs-existing + persist path as single-context discovery
    (``_finalize_candidate``). Returns the surviving ``action="new"`` results
    plus any ``action="existing"`` (so the caller can link the story to the
    matched thesis). ``action="none"`` candidates are dropped.

    Candidate-vs-candidate dedup is intentionally NOT done here — it falls out
    of the caller running ``run_post_creation_pipeline`` on each new thesis
    sequentially, whose promotion gate re-checks similarity against siblings
    already embedded into the index.
    """
    # Step 0 — Overlap retrieval: nearby existing theses
    _embedding, nearby = _embed_and_search(context, db_path, overlap_top_k)
    nearby_block = ""
    if nearby:
        lines = ["Nearby existing theses (do NOT duplicate these):"]
        for thesis_id, chunk_text, score in nearby:
            lines.append(f"- [{thesis_id}] (similarity {score:.2f}): {chunk_text}")
        nearby_block = "\n".join(lines)

    covered_block = ""
    if covered_angles:
        covered_block = "\n".join(
            ["These angles are already covered by theses linked to this story:"]
            + [f"- {a}" for a in covered_angles]
        )

    # Step 1 — One Pro call, multi-candidate
    user_prompt_parts = [
        "## Story\n",
        context,
        "\n\n## Instrument Registry\n",
        "Use ONLY symbols from this table. Unknown symbols → thesis rejected.\n",
        "```\n" + _format_registry_block() + "\n```",
    ]
    if nearby_block:
        user_prompt_parts.append(f"\n\n## Existing Theses\n\n{nearby_block}")
    if covered_block:
        user_prompt_parts.append(f"\n\n## Already-Covered Angles\n\n{covered_block}")
    user_prompt = "\n".join(user_prompt_parts)

    result = generate_text_with_retry(
        contents=user_prompt,
        model=GEMINI_3_1_PRO_PREVIEW,
        system_instruction=DISCOVER_STORY_SYSTEM_PROMPT,
        thinking_level="medium",
        response_mime_type="application/json",
        response_json_schema=DISCOVER_STORY_JSON_SCHEMA,
    )
    _log_discovery_llm_call(
        db_path,
        entity_type="story",
        entity_id=source_context,
        caller="discover_story_generate",
        result=result,
    )

    parsed = json.loads(result.text)
    candidates = parsed.get("candidates", []) or []
    if not candidates:
        reason = parsed.get("rejection_reason") or "no strong, new thesis"
        print(f"discover-story: 0 candidates — {reason}", file=sys.stderr)
        return []

    # Step 2 — Per candidate, reuse registry grounding + dedup + persist
    results: list[DiscoverResult] = []
    for candidate in candidates[:max_candidates]:
        outcome = _finalize_candidate(
            candidate,
            db_path,
            source_context=source_context,
            similarity_threshold=similarity_threshold,
        )
        if outcome.action == "none":
            print(
                f"discover-story: candidate dropped — {outcome.rationale}",
                file=sys.stderr,
            )
            continue
        results.append(outcome)
    return results


# ---------------------------------------------------------------------------
# Post-creation pipeline
# ---------------------------------------------------------------------------


def run_post_creation_pipeline(
    root: Path,
    thesis_id: str,
    db_path: Path,
) -> str:
    """Embed into match index (incremental), backfill story links, run candidate promotion.

    Returns final review_status: ``'active'``, ``'candidate'``, or ``'rejected'``.
    """
    # 1. Embed thesis chunks incrementally (append-only, NOT rebuild)
    doc = parse_thesis_markdown(root / "global" / "theses" / f"{thesis_id}.md")
    chunks = build_thesis_chunks(doc)

    text_batches = [[chunk.chunk_text] for chunk in chunks]
    batch_results = batch_embed_contents(
        text_batches,
        model=THESIS_MATCH_EMBEDDING_MODEL,
        output_dimensionality=THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
        task_type="RETRIEVAL_DOCUMENT",
    )
    embeddings = [emb for br in batch_results for emb in br.embeddings]

    conn = get_db_connection(db_path)
    try:
        ensure_match_index_schema(conn)
        with conn:
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                conn.execute(
                    """
                    INSERT INTO thesis_match_chunks (
                        thesis_id, chunk_key, chunk_kind, chunk_text,
                        tickers_json, sectors_json, embedding_model, embedding_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    ON CONFLICT(thesis_id, chunk_key) DO UPDATE SET
                        chunk_kind=excluded.chunk_kind,
                        chunk_text=excluded.chunk_text,
                        tickers_json=excluded.tickers_json,
                        sectors_json=excluded.sectors_json,
                        embedding_model=excluded.embedding_model,
                        embedding_json=excluded.embedding_json,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    """,
                    (
                        chunk.thesis_id,
                        chunk.chunk_key,
                        chunk.chunk_kind,
                        chunk.chunk_text,
                        json.dumps(chunk.tickers),
                        json.dumps(chunk.sectors),
                        THESIS_MATCH_EMBEDDING_MODEL,
                        json.dumps(embedding),
                    ),
                )
    finally:
        conn.close()

    print(f"discover: embedded {len(chunks)} chunks for {thesis_id}", file=sys.stderr)

    # 2. Backfill story links
    sys.path.insert(0, str(root))
    from agents.match_story_for_thesis import match_story_for_thesis

    links = match_story_for_thesis(root, thesis_id, window_days=14)
    print(f"discover: backfilled {len(links)} story links for {thesis_id}", file=sys.stderr)

    # 3. Score now so the thesis carries a freshness/tailwind/composite the
    #    moment it exists — even while unowned (score lives on the thesis row,
    #    not user_theses). The scheduled score_theses run keeps it updated.
    from agents.score_theses import score_theses

    score_theses(root, thesis_id=thesis_id)

    # 4. Candidate promotion
    review_status = _promote_candidate(thesis_id, db_path, root, links)
    print(f"discover: {thesis_id} promotion result: {review_status}", file=sys.stderr)

    return review_status


# ---------------------------------------------------------------------------
# Candidate promotion
# ---------------------------------------------------------------------------


def _promote_candidate(
    thesis_id: str,
    db_path: Path,
    root: Path,
    backfill_links: list,
) -> str:
    """Run the promotion gates after creation backfill. Returns final status."""
    return _apply_candidate_promotion(
        thesis_id,
        db_path,
        root,
        has_story_evidence=bool(backfill_links),
        no_evidence_message="stays candidate — zero story links in 14-day window",
    )


def _candidate_quality_decision(
    thesis_id: str,
    db_path: Path,
    root: Path,
) -> _PromotionDecision | None:
    """Return a rejection decision if structural or similarity gates fail."""
    # 1. Structural check — parse_thesis_markdown must succeed
    path = root / "global" / "theses" / f"{thesis_id}.md"
    try:
        doc = parse_thesis_markdown(path)
        if len(doc.tickers) == 0:
            return _PromotionDecision("rejected", "rejected — no tickers")
        if len(doc.invalidations) < 2:
            return _PromotionDecision(
                "rejected", "rejected — fewer than 2 invalidations"
            )
    except ValueError as exc:
        return _PromotionDecision("rejected", f"rejected — parse failed: {exc}")

    # 2. Similarity re-check against current index — same threshold + rerank
    # logic as Gate 2 in discover_thesis.
    matches = search_dense(
        db_path,
        f"{doc.title}\n\n{doc.core_thesis}",
        top_k=5,
        min_score=0.0,
    )
    matches = [m for m in matches if m.thesis_id != thesis_id]
    if matches:
        top = matches[0]
        if top.score >= DUPLICATE_SIMILARITY_THRESHOLD:
            return _PromotionDecision(
                "rejected",
                f"rejected — near-duplicate of {top.thesis_id} "
                f"(similarity {top.score:.3f})",
            )
        if top.score >= RERANK_LOW_THRESHOLD:
            is_dup, judge_rationale = _judge_duplicate_via_llm(
                doc.title, doc.core_thesis, top.thesis_id, db_path,
            )
            if is_dup:
                return _PromotionDecision(
                    "rejected",
                    f"rejected — LLM rerank judged duplicate of "
                    f"{top.thesis_id} (cosine {top.score:.3f}) — {judge_rationale}",
                )
            print(
                f"discover: rerank kept {thesis_id} as distinct from {top.thesis_id} "
                f"(cosine {top.score:.3f}) — {judge_rationale}",
                file=sys.stderr,
            )

    return None


def _candidate_promotion_decision(
    thesis_id: str,
    db_path: Path,
    root: Path,
    *,
    has_story_evidence: bool,
    no_evidence_message: str,
) -> _PromotionDecision:
    rejection = _candidate_quality_decision(thesis_id, db_path, root)
    if rejection is not None:
        return rejection
    if not has_story_evidence:
        return _PromotionDecision("candidate", no_evidence_message)
    return _PromotionDecision("active")


def _apply_candidate_promotion(
    thesis_id: str,
    db_path: Path,
    root: Path,
    *,
    has_story_evidence: bool,
    no_evidence_message: str,
) -> PromotionStatus:
    """Evaluate promotion gates, update review_status when terminal, and log."""
    decision = _candidate_promotion_decision(
        thesis_id,
        db_path,
        root,
        has_story_evidence=has_story_evidence,
        no_evidence_message=no_evidence_message,
    )
    if decision.status in {"active", "rejected"}:
        _set_review_status(db_path, thesis_id, decision.status)
    if decision.log_message:
        print(f"discover: {thesis_id} {decision.log_message}", file=sys.stderr)
    return decision.status


# ---------------------------------------------------------------------------
# Candidate re-promotion (post-creation sweep)
# ---------------------------------------------------------------------------


def repromote_candidate(
    root: Path,
    thesis_id: str,
    db_path: Path,
    *,
    max_stale_days: int,
) -> str:
    """Re-run the promotion gates over a stuck candidate using links accrued so far.

    ``_promote_candidate`` runs once at discovery time. A system thesis born
    with zero matched stories stays ``candidate`` even though the ingest-time
    matcher (``match_thesis_for_story``) keeps linking new stories to it — so
    one that earns its evidence late never gets promoted. This re-reads the
    links already in ``thesis_story_links`` (no re-embed, no re-backfill) and
    re-applies the gates. Outcomes:

    - ``'active'``    — passes structural + similarity gates and now has ≥1 link.
    - ``'rejected'``  — fails a gate, or has zero links and is older than
      ``max_stale_days`` (terminal exit so dead candidates don't accumulate).
    - ``'candidate'`` — well-formed, unique, no links yet, not yet stale.
    """
    conn = get_db_connection(db_path)
    try:
        link_rows = conn.execute(
            "SELECT story_id FROM thesis_story_links WHERE thesis_id = ?",
            (thesis_id,),
        ).fetchall()
        age_row = conn.execute(
            "SELECT CAST(julianday('now') - julianday(created_at) AS INTEGER) "
            "FROM theses WHERE id = ?",
            (thesis_id,),
        ).fetchone()
    finally:
        conn.close()

    existing_links = [row[0] for row in link_rows]
    age_days = int(age_row[0]) if age_row and age_row[0] is not None else 0

    # A 'candidate' return means structural + similarity gates passed but zero
    # evidence exists — the only case the age policy acts on.
    status = _apply_candidate_promotion(
        thesis_id,
        db_path,
        root,
        has_story_evidence=bool(existing_links),
        no_evidence_message="stays candidate — zero story links on re-promotion sweep",
    )
    if status == "candidate" and age_days >= max_stale_days:
        _set_review_status(db_path, thesis_id, "rejected")
        print(
            f"discover: {thesis_id} rejected — stale candidate ({age_days}d) "
            f"with zero story links, past {max_stale_days}d limit",
            file=sys.stderr,
        )
        return "rejected"
    return status


__all__ = [
    "DiscoverResult",
    "discover_thesis",
    "repromote_candidate",
    "run_post_creation_pipeline",
]

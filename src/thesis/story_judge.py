"""Shared (thesis, story) pair judge.

Both directions of the matching pipeline — story→thesis retrieval
(agents/match_thesis_for_story.py) and thesis→story backfill
(agents/match_story_for_thesis.py) — call into the same Gemini judge here.
Keep retrieval logic out of this module; it only scores one pair.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Literal, TypedDict

from src.clients.gemini import GEMINI_3_FLASH_PREVIEW, generate_text_with_retry
from src.story.docs import StoryDocument
from src.thesis.docs import ThesisDocument


RELATIONS = ("supports", "stresses", "unrelated")

Relation = Literal["supports", "stresses", "unrelated"]


JUDGE_SCHEMA = {
    "type": "object",
    "required": ["relation", "confidence", "matched_invalidation", "rationale"],
    "properties": {
        "relation": {
            "type": "string",
            "enum": list(RELATIONS),
        },
        "confidence": {
            "type": "number",
        },
        "matched_invalidation": {
            "type": ["string", "null"],
        },
        "rationale": {
            "type": "string",
        },
    },
}

JUDGE_SYSTEM_PROMPT = """You are classifying whether one news story affects one market thesis.

Return exactly one JSON object with:
- relation: supports | stresses | unrelated
- confidence: 0.0 to 1.0, calibrated per the scale below
- matched_invalidation: the exact text of a named invalidation the story literally triggers, otherwise null
- rationale: one short sentence, direct and concrete, with correct market terminology

Decision rules:
- supports: the story materially reinforces the thesis.
- stresses: the story materially weakens the thesis or literally triggers a named invalidation.
- unrelated: the story is adjacent but does not meaningfully change the thesis.

Be strict. Do not force a support or stress call from weak thematic overlap.

Confidence calibration — score how directly the story moves the thesis's own
mechanism, not how related the topic is:
- 0.85-0.95: the story directly names the thesis's driver or mechanism (the
  specific actor, level, or price the thesis is built on).
- 0.65-0.80: the story moves the thesis only through an indirect or
  second-order chain — same broad theme, but not the named mechanism.
- below 0.60: weak or speculative link.
A shared "rates are moving" or "the dollar is moving" theme that does not name
the thesis's specific driver is second-order — cap it at 0.80.

matched_invalidation — populate ONLY when the story's facts literally satisfy a
named invalidation, including its stated direction, magnitude, and threshold.
Being on the same topic as an invalidation is not enough. (Example: an
invalidation reading "the dollar drops sharply" is not triggered by a 0.3%
dollar move.) If no named invalidation is literally satisfied, return null —
even when relation is stresses. If relation is not stresses, matched_invalidation
must be null.

Market terminology: bond prices and yields move inversely. "Treasuries rallied"
or "bond prices rose" means yields fell; "yields rose" means prices fell. State
yield direction correctly in the rationale.
"""


@dataclass(slots=True)
class BestChunk:
    """The thesis chunk that best matched the story.

    Used only to show the judge which part of the thesis retrieval latched
    onto. Callers in either direction populate it from their own retrieval.
    """

    chunk_key: str
    chunk_kind: str
    chunk_text: str
    score: float


class JudgeVerdict(TypedDict):
    relation: Relation
    confidence: float
    matched_invalidation: str | None
    rationale: str


def _build_judge_prompt(
    story: StoryDocument,
    thesis: ThesisDocument,
    best_chunk: BestChunk,
) -> str:
    invalidations = "\n".join(f"- {item}" for item in thesis.invalidations)
    tickers = ", ".join(thesis.tickers) if thesis.tickers else "None"
    story_overview = "\n".join(f"- {bullet}" for bullet in story.overview_bullets)

    return f"""Story
ID: {story.story_id}
Title: {story.title}
Overview:
{story_overview}

Thesis
ID: {thesis.thesis_id}
Title: {thesis.title}
Tickers: {tickers}
Core Thesis:
{thesis.core_thesis}

Invalidation Conditions:
{invalidations}

Best Retrieved Chunk:
- key: {best_chunk.chunk_key}
- kind: {best_chunk.chunk_kind}
- text: {best_chunk.chunk_text}

Classify whether this story supports, stresses, or is unrelated to the thesis.
"""


def _coerce_confidence(value: object) -> float:
    if isinstance(value, bool):
        confidence = 0.0
    elif isinstance(value, (int, float)):
        confidence = float(value)
    else:
        confidence = 0.0
    return max(0.0, min(1.0, confidence))


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".,;:!?").casefold()


def _resolve_invalidation(candidate: str, invalidations: list[str]) -> str | None:
    normalized_candidate = _normalize_for_match(candidate)
    if not normalized_candidate:
        return None
    for invalidation in invalidations:
        if _normalize_for_match(invalidation) == normalized_candidate:
            return invalidation
    return None


def _normalize_verdict(invalidations: list[str], raw: dict[str, object]) -> JudgeVerdict:
    relation_raw = str(raw.get("relation", "unrelated")).strip().lower()
    relation: Relation = relation_raw if relation_raw in RELATIONS else "unrelated"  # type: ignore[assignment]

    matched_invalidation: str | None = None
    raw_invalidation = raw.get("matched_invalidation")
    if relation == "stresses" and isinstance(raw_invalidation, str):
        matched_invalidation = _resolve_invalidation(raw_invalidation, invalidations)

    rationale = str(raw.get("rationale", "")).strip()
    if not rationale:
        rationale = "No rationale returned."

    return {
        "relation": relation,
        "confidence": _coerce_confidence(raw.get("confidence")),
        "matched_invalidation": matched_invalidation,
        "rationale": rationale,
    }


def judge_pair(
    story: StoryDocument,
    thesis: ThesisDocument,
    best_chunk: BestChunk,
) -> JudgeVerdict | None:
    """Score one (thesis, story) pair. Returns None if Gemini response is unparseable."""
    prompt = _build_judge_prompt(story, thesis, best_chunk)
    # Medium thinking on Flash — the same tier as the sibling story judge
    # (agents/judge_stories.py) and the daily brief. `low` under-thought bond
    # direction and confidence calibration. Two same-tier attempts give
    # parse-failure robustness without escalating to Pro. max_output_tokens is
    # headroom for thinking tokens, not a cost floor — the JSON itself is tiny.
    attempts = ((4096, "medium"), (4096, "medium"))
    last_text = ""
    for max_tokens, thinking in attempts:
        result = generate_text_with_retry(
            prompt,
            model=GEMINI_3_FLASH_PREVIEW,
            system_instruction=JUDGE_SYSTEM_PROMPT,
            max_output_tokens=max_tokens,
            thinking_level=thinking,
            response_mime_type="application/json",
            response_json_schema=JUDGE_SCHEMA,
        )
        last_text = result.text
        try:
            raw = json.loads(result.text)
        except json.JSONDecodeError:
            continue
        return _normalize_verdict(thesis.invalidations, raw)

    print(
        f"warning: could not parse judge response for story={story.story_id} "
        f"thesis={thesis.thesis_id}; skipping. last_text={last_text[:120]!r}",
        file=sys.stderr,
    )
    return None


def log_chunk_win(
    *,
    story_id: str,
    thesis_id: str,
    chunk_key: str,
    retrieval_score: float,
    verdict: JudgeVerdict,
) -> None:
    print(
        f"chunk-win: story={story_id} thesis={thesis_id} "
        f"chunk={chunk_key} score={retrieval_score:.3f} "
        f"relation={verdict['relation']} conf={verdict['confidence']:.2f}",
        file=sys.stderr,
    )


__all__ = [
    "BestChunk",
    "JUDGE_SCHEMA",
    "JUDGE_SYSTEM_PROMPT",
    "JudgeVerdict",
    "RELATIONS",
    "Relation",
    "judge_pair",
    "log_chunk_win",
]

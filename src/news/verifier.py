"""Deterministic verifier for cluster-level story synthesis."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.news.themes import ALL_TAGS

YAHOO_SYMBOL_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-]{0,12}(?:=[A-Z])?$")

_WS_RE = re.compile(r"\s+")
_QUOTE_TRAILING_PUNCT = ".,;:"

# Sentence-boundary split for the multi-fragment quote rescue. A period/?/!
# followed by whitespace and either an opening quote or capital letter is the
# canonical break between two adjacent quoted sentences that the LLM may join
# with a single space (dropping the journalistic attribution between them).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[\"A-Z])")

# Generic commodity nouns the verifier accepts as evidence_span for the
# corresponding futures contracts and (broad) commodity-tracking ETFs.
# Keyed by the canonical Yahoo symbol. "crude oil" is the *correct*
# attribution for CL=F — rejecting it because it lacks an uppercase letter
# kills every legitimate commodity story. ETFs are included because the
# slate's commodity-ETF tickers (USO, UNG, GLD, …) get picked by the synth
# when an article discusses the underlying commodity.
#
# The rescue accepts evidence_span when any whitelist term is a word-bounded
# substring of `evidence.lower()` AND evidence_span verbatim appears in the
# cited body. So "oil prices", "WTI crude oil", "U.S. crude" all rescue CL=F,
# but "boil" or "spoiler" don't (no word boundary), and a hallucinated phrase
# never in the body still fails (body containment).
COMMODITY_GENERIC_SPANS: dict[str, frozenset[str]] = {
    "CL=F": frozenset({"crude oil", "oil", "wti", "wti crude", "u.s. crude", "us crude"}),
    "USO":  frozenset({"crude oil", "oil"}),
    "BZ=F": frozenset({"brent", "brent crude"}),
    "NG=F": frozenset({"natural gas", "gas"}),
    "UNG":  frozenset({"natural gas"}),
    "GC=F": frozenset({"gold"}),
    "GLD":  frozenset({"gold"}),
    "SI=F": frozenset({"silver"}),
    "SLV":  frozenset({"silver"}),
    "HG=F": frozenset({"copper"}),
    "PA=F": frozenset({"palladium"}),
    "PL=F": frozenset({"platinum"}),
    "ZC=F": frozenset({"corn"}),
    "ZW=F": frozenset({"wheat"}),
    "ZS=F": frozenset({"soybeans", "soybean"}),
}
# Unify the typographic variants Gemini and scraped HTML disagree on:
# curly quotes/apostrophes -> straight, en/em dashes -> hyphen, ellipsis -> '...'.
_PUNCT_FOLD = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    "…": "...",
    " ": " ",
})


def _normalize_for_quote_match(text: str) -> str:
    """Loose normalization for the verbatim-quote substring check.

    Real failures cluster around reversible differences between LLM
    output and stored body: NBSP vs space, curly vs straight quotation
    marks, collapsed vs raw whitespace, trailing sentence punctuation
    that the LLM adds when the source uses journalistic comma-before-
    closing-quote style, and headline-vs-prose capitalization (LLM
    writes "the clock is ticking" when the source says "The Clock is
    Ticking"). Casefolding both sides keeps the intent — quotes must
    come from a cited body — without rejecting case drift that doesn't
    change the content.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_PUNCT_FOLD)
    text = _WS_RE.sub(" ", text).strip()
    return text.casefold()


def _commodity_term_in_evidence(evidence_lc: str, term: str) -> bool:
    """Word-bounded substring match for the per-symbol commodity whitelist.

    "oil" must match "oil prices" / "WTI crude oil" but not "boil" or
    "spoiler". `\\b` is Unicode-aware in `re`, which is what we want.
    """
    if not term:
        return False
    return re.search(rf"\b{re.escape(term)}\b", evidence_lc) is not None


@dataclass(slots=True)
class VerificationResult:
    ok: bool
    errors: list[str]
    # Indices into payload["quotes"] whose text doesn't verbatim-match any
    # cited body. Per "ticker identity sound; prose is the model's", a
    # paraphrased quote scrubs from the payload but doesn't reject the
    # whole story. Citation-integrity quote failures (missing speaker,
    # missing source_doc_ids, non-member cite) still land in `errors`.
    quote_scrub_indices: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.quote_scrub_indices is None:
            self.quote_scrub_indices = []


def _source_ids(item: dict) -> list[str]:
    value = item.get("source_doc_ids")
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if str(v).strip()]


def verify_story_payload(
    payload: dict,
    *,
    member_ids: set[str],
    member_bodies: dict[str, str],
    ticker_aliases: dict[str, set[str]] | None = None,
    allowed_symbols: set[str] | None = None,
) -> VerificationResult:
    """Validate the LLM synth payload against cluster evidence.

    `allowed_symbols` (optional): hard slate membership. When provided,
    every emitted ticker `symbol` must be a member of this set or the
    payload is rejected. This is the closed-universe enforcement that
    makes the synth prompt's "pick ONLY symbols on this slate" claim
    actually true at the verifier layer — without it, an off-slate
    hallucination only had to pass the substring-in-body check.

    `ticker_aliases` (optional): when provided, every emitted ticker whose
    symbol appears in this mapping must have an `evidence_span` that
    case-insensitively contains one of the listed aliases (one-directional
    substring — the alias must appear inside the evidence span). This is
    the deterministic guard against name-collision hallucinations
    (e.g. evidence_span="Powell" → POWL when the body actually meant
    Jerome Powell). When `ticker_aliases` is None or doesn't contain the
    symbol, only the legacy substring-in-body check applies — preserves
    backwards-compatible behavior for callers that haven't migrated.
    """
    errors: list[str] = []
    quote_scrub_indices: list[int] = []

    for idx, bullet in enumerate(payload.get("overview") or []):
        source_ids = _source_ids(bullet)
        if not source_ids:
            errors.append(f"overview[{idx}] missing source_doc_ids")
        for source_id in source_ids:
            if source_id not in member_ids:
                errors.append(f"overview[{idx}] cites non-member {source_id}")

    for section in ("claims",):
        for idx, item in enumerate(payload.get(section) or []):
            for source_id in _source_ids(item):
                if source_id not in member_ids:
                    errors.append(f"{section}[{idx}] cites non-member {source_id}")

    for idx, quote in enumerate(payload.get("quotes") or []):
        source_ids = _source_ids(quote)
        if not source_ids:
            errors.append(f"quotes[{idx}] missing source_doc_ids")
            continue
        text = str(quote.get("text") or "").strip()
        if not text:
            continue
        if not str(quote.get("speaker") or "").strip():
            errors.append(f"quotes[{idx}] missing speaker")
        normalized_bodies = [
            _normalize_for_quote_match(member_bodies.get(source_id) or "")
            for source_id in source_ids
        ]
        needle = _normalize_for_quote_match(text).rstrip(_QUOTE_TRAILING_PUNCT)
        whole_match = bool(needle) and any(
            needle in body for body in normalized_bodies
        )
        if not whole_match:
            # Sentence-split rescue: LLMs sometimes concatenate two
            # adjacent quoted sentences from the same speaker, dropping
            # the journalistic attribution between them ("'X,' he said.
            # 'Y.'" → "X. Y."). Accept when every non-trivial fragment
            # appears verbatim in at least one cited body. The MIN_LEN
            # guard keeps a stray "Yes." from rescuing junk.
            fragments = [
                _normalize_for_quote_match(frag).rstrip(_QUOTE_TRAILING_PUNCT)
                for frag in _SENTENCE_SPLIT_RE.split(text)
            ]
            fragments = [f for f in fragments if len(f) >= 12]
            fragment_match = bool(fragments) and all(
                any(frag in body for body in normalized_bodies)
                for frag in fragments
            )
            if not fragment_match:
                quote_scrub_indices.append(idx)
        for source_id in source_ids:
            if source_id not in member_ids:
                errors.append(f"quotes[{idx}] cites non-member {source_id}")

    relevance = payload.get("market_relevance") or {}
    tickers = relevance.get("tickers") or payload.get("tickers") or []
    for idx, item in enumerate(tickers):
        if isinstance(item, dict):
            symbol = str(item.get("symbol") or "").strip().upper()
            source_id = str(item.get("source_doc_id") or "").strip()
            evidence = str(item.get("evidence_span") or "").strip()
            if not YAHOO_SYMBOL_RE.match(symbol):
                errors.append(f"tickers[{idx}] invalid Yahoo-form ticker: {item.get('symbol')}")
            elif allowed_symbols is not None and symbol not in allowed_symbols:
                # Hard slate gate: the synth prompt advertises the candidate
                # slate as a closed list. Enforce that here so an off-slate
                # symbol can't slip through on substring evidence alone.
                errors.append(
                    f"tickers[{idx}] symbol {symbol} is not on the candidate slate"
                )
            if not source_id:
                errors.append(f"tickers[{idx}] missing source_doc_id")
            elif source_id not in member_ids:
                errors.append(f"tickers[{idx}] cites non-member {source_id}")
            # Canonical "Company (TICKER)" form is the gold-standard
            # attribution — when the body literally writes "Strategy (MSTR)"
            # the symbol identity is unambiguous. Accept without firing the
            # uppercase or alias gates below.
            parenthesized = f"({symbol})"
            body_text = member_bodies.get(source_id) or ""
            paren_attribution = (
                parenthesized in evidence
                and (not source_id or source_id not in member_bodies or parenthesized in body_text)
            )

            # Bare-ticker rescue: when evidence_span IS the ticker symbol
            # itself ("SPY", "MSTR", "QQQ", "CL=F") and the body contains
            # it verbatim, accept without the alias-substring gate. The
            # registry's same-name alias gets filtered by _MIN_ALIAS_LEN=4
            # in ticker_candidates, so the alias gate would otherwise
            # reject the most unambiguous attribution the LLM can emit.
            bare_ticker = (
                evidence.strip().upper() == symbol
                and (not source_id or source_id not in member_bodies or evidence in body_text)
            )

            # Commodity-noun rescue: "crude oil" → CL=F, "natural gas" →
            # NG=F, "oil prices" → CL=F, "WTI crude oil futures" → CL=F.
            # Restricted to a small symbol whitelist so company tickers
            # can't slip through with stop-word evidence, and word-bounded
            # so "oil" doesn't rescue "boil" / "spoiler". Body containment
            # still applies — a phrase the LLM invented never passes.
            evidence_lc = evidence.lower()
            commodity_match = (
                symbol in COMMODITY_GENERIC_SPANS
                and any(
                    _commodity_term_in_evidence(evidence_lc, term)
                    for term in COMMODITY_GENERIC_SPANS[symbol]
                )
                and (not source_id or source_id not in member_bodies
                     or evidence_lc in body_text.lower())
            )

            if len(evidence) < 3:
                errors.append(f"tickers[{idx}] missing or too-short evidence_span")
            elif paren_attribution or commodity_match or bare_ticker:
                # All three rescues already validated body containment.
                pass
            elif not any(c.isupper() for c in evidence):
                # Company names contain an uppercase letter ("Meta", "Eli Lilly",
                # "MP Materials", "BYD"). All-lowercase spans like "the" or
                # "shares" are stop-words masquerading as evidence.
                errors.append(f"tickers[{idx}] evidence_span must contain an uppercase letter")
            elif source_id in member_bodies and evidence not in body_text:
                errors.append(f"tickers[{idx}] evidence_span not found in cited body")
            elif ticker_aliases is not None and symbol in ticker_aliases:
                # Alias gate: evidence_span must CONTAIN one of the
                # instrument's known long-form aliases (one-directional —
                # the alias must be a substring of evidence_span, not the
                # reverse). This deterministically blocks the name-
                # collision class: for POWL with alias "Powell Industries",
                # evidence_span "Powell Industries" passes, "Powell
                # Industries Inc." passes (longer form contains alias),
                # but bare "Powell" (referring to Jerome Powell) FAILS
                # because "Powell Industries" is not a substring of
                # "Powell".
                evidence_lc = evidence.lower()
                alias_match = any(
                    alias.lower() in evidence_lc
                    for alias in ticker_aliases.get(symbol, set())
                )
                if not alias_match:
                    errors.append(
                        f"tickers[{idx}] evidence_span '{evidence}' does not match any "
                        f"registry alias for {symbol}"
                    )
        else:
            symbol_text = str(item or "").strip().upper()
            if not YAHOO_SYMBOL_RE.match(symbol_text):
                errors.append(f"invalid Yahoo-form ticker: {item}")

    for kind in ("sectors", "regions"):
        for idx, item in enumerate(relevance.get(kind) or []):
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_doc_id") or "").strip()
            if not source_id:
                errors.append(f"{kind}[{idx}] missing source_doc_id")
            elif source_id not in member_ids:
                errors.append(f"{kind}[{idx}] cites non-member {source_id}")

    theme_tag = payload.get("theme_tag")
    if theme_tag is not None and str(theme_tag).strip() not in ALL_TAGS:
        errors.append(f"invalid theme_tag: {theme_tag}")

    return VerificationResult(
        ok=not errors,
        errors=errors,
        quote_scrub_indices=quote_scrub_indices,
    )


__all__ = [
    "VerificationResult",
    "YAHOO_SYMBOL_RE",
    "verify_story_payload",
]

"""Tests for src.news.verifier — evidence-anchored market_relevance.

Synth output now ships tickers/sectors/regions in object form
({symbol, source_doc_id, evidence_span} / {tag, source_doc_id}). The
verifier rejects any entry whose source_doc_id is not a cluster member or
whose evidence_span is not a verbatim substring of the cited body.

Flat string tickers (stored stories, eval scripts) still pass the legacy
Yahoo-form check — the shape-aware verifier dispatches per-entry.
"""

from __future__ import annotations

from src.news.verifier import verify_story_payload


def _base_payload(market_relevance: dict) -> dict:
    return {
        "headline": "h",
        "what_changed": "wc",
        "overview": [],
        "claims": [],
        "quotes": [],
        "market_relevance": market_relevance,
        "theme_tag": "other",
    }


def test_object_ticker_with_evidence_in_body_passes():
    payload = _base_payload(
        {
            "tickers": [
                {"symbol": "META", "source_doc_id": "n1", "evidence_span": "Meta"}
            ],
            "sectors": [{"tag": "tech.software", "source_doc_id": "n1"}],
            "regions": [{"tag": "north_america", "source_doc_id": "n1"}],
        }
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Meta announced a new product yesterday."},
    )
    assert result.ok, result.errors


def test_object_ticker_evidence_not_in_body_rejected():
    payload = _base_payload(
        {
            "tickers": [
                {"symbol": "NVDA", "source_doc_id": "n1", "evidence_span": "Nvidia"}
            ],
        }
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Boston Fed flagged inflation risks and possible rate hikes."},
    )
    assert not result.ok
    assert any("evidence_span not found" in e for e in result.errors)


def test_object_ticker_non_member_source_rejected():
    payload = _base_payload(
        {"tickers": [{"symbol": "META", "source_doc_id": "n_bogus", "evidence_span": "Meta"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Meta announced."},
    )
    assert not result.ok
    assert any("cites non-member" in e for e in result.errors)


def test_object_ticker_missing_source_doc_id_rejected():
    payload = _base_payload(
        {"tickers": [{"symbol": "META", "source_doc_id": "", "evidence_span": "Meta"}]}
    )
    result = verify_story_payload(
        payload, member_ids={"n1"}, member_bodies={"n1": "Meta announced."}
    )
    assert not result.ok
    assert any("missing source_doc_id" in e for e in result.errors)


def test_object_ticker_lowercase_stopword_evidence_rejected():
    # A lowercase common word ("the", "and", "shares") is technically a
    # substring of nearly every body — but company names contain at least
    # one uppercase letter. Require that to block trivial pass-throughs.
    payload = _base_payload(
        {"tickers": [{"symbol": "NVDA", "source_doc_id": "n1", "evidence_span": "the"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Boston Fed flagged inflation risks; rate hikes possible if the trend persists."},
    )
    assert not result.ok
    assert any("must contain an uppercase letter" in e for e in result.errors)


def test_object_ticker_short_evidence_rejected():
    payload = _base_payload(
        {"tickers": [{"symbol": "META", "source_doc_id": "n1", "evidence_span": "M"}]}
    )
    result = verify_story_payload(
        payload, member_ids={"n1"}, member_bodies={"n1": "M announced."}
    )
    assert not result.ok
    assert any("too-short evidence_span" in e for e in result.errors)


def test_invalid_yahoo_symbol_rejected_object_form():
    payload = _base_payload(
        {
            "tickers": [
                {"symbol": "not a ticker!", "source_doc_id": "n1", "evidence_span": "X"}
            ],
        }
    )
    result = verify_story_payload(
        payload, member_ids={"n1"}, member_bodies={"n1": "X exists."}
    )
    assert not result.ok
    assert any("invalid Yahoo-form ticker" in e for e in result.errors)


def test_sector_missing_source_doc_id_rejected():
    payload = _base_payload(
        {
            "tickers": [],
            "sectors": [{"tag": "tech.software", "source_doc_id": ""}],
        }
    )
    result = verify_story_payload(payload, member_ids={"n1"}, member_bodies={"n1": "x"})
    assert not result.ok
    assert any("sectors[0] missing source_doc_id" in e for e in result.errors)


def test_region_non_member_source_rejected():
    payload = _base_payload(
        {
            "tickers": [],
            "regions": [{"tag": "middle_east", "source_doc_id": "n_bogus"}],
        }
    )
    result = verify_story_payload(payload, member_ids={"n1"}, member_bodies={"n1": "x"})
    assert not result.ok
    assert any("regions[0] cites non-member" in e for e in result.errors)


def _payload_with_quote(text: str, speaker: str = "Speaker", source_id: str = "n1") -> dict:
    base = _base_payload({
        "tickers": [],
        "sectors": [{"tag": "tech.software", "source_doc_id": source_id}],
        "regions": [{"tag": "north_america", "source_doc_id": source_id}],
    })
    base["quotes"] = [{
        "text": text,
        "speaker": speaker,
        "source_doc_ids": [source_id],
    }]
    return base


def test_quote_trailing_period_passes_when_body_has_comma_before_close_quote():
    # cluster_6764-style: body uses journalistic close-quote-with-comma
    # style; LLM emits the same span terminated with a period. The quote is
    # truly from the body — only the terminal punctuation differs.
    body = (
        'Acting AG said, "Law enforcement officers risk their lives every '
        'day to keep Americans safe, and they do not deserve to be doxed '
        'or harassed simply for carrying out their duties," in a statement.'
    )
    quote = (
        "Law enforcement officers risk their lives every day to keep "
        "Americans safe, and they do not deserve to be doxed or "
        "harassed simply for carrying out their duties."
    )
    result = verify_story_payload(
        _payload_with_quote(quote),
        member_ids={"n1"},
        member_bodies={"n1": body},
    )
    assert result.ok, result.errors


def test_quote_with_nbsp_in_body_still_matches():
    # Pre-c62de8c bodies contain U+00A0 inside attributions; LLM normalizes
    # to plain space when emitting the quote.
    body = "He told reporters, “Rates are heading higher,” in a note."
    quote = "Rates are heading higher"
    result = verify_story_payload(
        _payload_with_quote(quote),
        member_ids={"n1"},
        member_bodies={"n1": body},
    )
    assert result.ok, result.errors


def test_quote_curly_apostrophe_matches_straight_in_body():
    body = "She said the Federal Reserve's next move would be a hike."
    quote = "the Federal Reserve’s next move would be a hike"
    result = verify_story_payload(
        _payload_with_quote(quote),
        member_ids={"n1"},
        member_bodies={"n1": body},
    )
    assert result.ok, result.errors


def test_quote_truly_not_in_body_is_scrubbed():
    # Loose normalization must not turn the check into a no-op: a quote
    # the body never said is removed from the payload (per-index scrub)
    # rather than rejecting the entire story. Citation-integrity checks
    # (missing speaker / non-member cite) still hard-fail.
    result = verify_story_payload(
        _payload_with_quote("We will buy the dip aggressively this quarter."),
        member_ids={"n1"},
        member_bodies={"n1": "Markets fell on inflation worries; rate cuts look unlikely."},
    )
    assert result.ok, result.errors
    assert result.quote_scrub_indices == [0]


def test_quote_case_insensitive_substring_match():
    # cluster_7183 / 7543 / 5335 class: LLM emits the prose form of a
    # quote that appears in headline-case in the body. The content is
    # identical; only casing differs. Casefolding both sides lets the
    # match succeed without weakening structural verification.
    body = "The headline reads: 'The Clock is Ticking' on the energy outlook."
    result = verify_story_payload(
        _payload_with_quote("the clock is ticking"),
        member_ids={"n1"},
        member_bodies={"n1": body},
    )
    assert result.ok, result.errors


def test_quote_missing_speaker_still_rejected():
    result = verify_story_payload(
        _payload_with_quote("rates are heading higher", speaker=""),
        member_ids={"n1"},
        member_bodies={"n1": "Powell said rates are heading higher."},
    )
    assert not result.ok
    assert any("missing speaker" in e for e in result.errors)


def test_flat_string_ticker_only_checks_yahoo_shape():
    # Stored stories / eval scripts pass tickers as plain strings — only the
    # Yahoo regex check applies, no evidence anchoring is enforced.
    payload = _base_payload({"tickers": ["META", "INVALID SYM"]})
    result = verify_story_payload(payload, member_ids=set(), member_bodies={})
    assert not result.ok
    assert any("invalid Yahoo-form ticker" in e for e in result.errors)
    # The valid META is not flagged
    assert not any("META" in e and "invalid" in e for e in result.errors)


def test_alias_gate_rejects_short_form_evidence_for_known_symbol():
    """`evidence_span="Powell"` must NOT satisfy POWL when the registry
    only lists "Powell Industries" as the alias. This is the deterministic
    fix for the name-collision class (Jerome Powell → POWL, etc.).
    """
    payload = _base_payload(
        {"tickers": [{"symbol": "POWL", "source_doc_id": "n1", "evidence_span": "Powell"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Jerome Powell raised rates at the Fed today."},
        ticker_aliases={"POWL": {"Powell Industries"}},
    )
    assert not result.ok
    assert any("does not match any registry alias for POWL" in e for e in result.errors)


def test_alias_gate_accepts_long_form_evidence_for_known_symbol():
    payload = _base_payload(
        {"tickers": [{"symbol": "POWL", "source_doc_id": "n1", "evidence_span": "Powell Industries"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Powell Industries reported a strong quarter."},
        ticker_aliases={"POWL": {"Powell Industries"}},
    )
    assert result.ok, result.errors


def test_alias_gate_accepts_evidence_longer_than_alias():
    """Alias "Eli Lilly" must accept evidence_span "Eli Lilly and Company"
    — the LLM emitted a longer form that contains the alias. The check is
    one-directional: alias ∈ evidence_span (not the reverse).
    """
    aliases = {"LLY": {"Eli Lilly"}}
    long_emission = _base_payload(
        {"tickers": [{"symbol": "LLY", "source_doc_id": "n1", "evidence_span": "Eli Lilly and Company"}]}
    )
    result = verify_story_payload(
        long_emission,
        member_ids={"n1"},
        member_bodies={"n1": "Eli Lilly and Company posted Q1 results today."},
        ticker_aliases=aliases,
    )
    assert result.ok, result.errors


def test_alias_gate_silent_when_symbol_not_in_aliases_map():
    """If the symbol is absent from `ticker_aliases`, only the legacy
    substring-in-body check applies. Preserves backwards-compat for
    callers that haven't migrated.
    """
    payload = _base_payload(
        {"tickers": [{"symbol": "NEWCO", "source_doc_id": "n1", "evidence_span": "NewCo"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "NewCo announced a strategic pivot."},
        ticker_aliases={"OTHER": {"Other Inc"}},  # NEWCO not in this map
    )
    assert result.ok, result.errors


def test_alias_gate_none_disables_check_entirely():
    payload = _base_payload(
        {"tickers": [{"symbol": "POWL", "source_doc_id": "n1", "evidence_span": "Powell"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Jerome Powell raised rates."},
        ticker_aliases=None,
    )
    # Without the alias gate, only substring-in-body applies: "Powell" is
    # in the body, so the legacy check passes. New callers should always
    # pass ticker_aliases.
    assert result.ok, result.errors


def test_slate_gate_rejects_off_slate_symbol():
    """An off-slate hallucination must be rejected even if `evidence_span`
    is a verbatim substring of the cited body. This is the closed-universe
    enforcement that makes the synth prompt's "pick ONLY symbols on this
    slate" claim true at the verifier layer.
    """
    payload = _base_payload(
        {"tickers": [{"symbol": "FAKE", "source_doc_id": "n1", "evidence_span": "FakeCo"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "FakeCo announced a strategic pivot today."},
        allowed_symbols={"META", "AAPL"},  # FAKE is not here
    )
    assert not result.ok
    assert any("not on the candidate slate" in e for e in result.errors)


def test_slate_gate_accepts_on_slate_symbol():
    payload = _base_payload(
        {"tickers": [{"symbol": "META", "source_doc_id": "n1", "evidence_span": "Meta"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Meta announced a strategic pivot today."},
        allowed_symbols={"META", "AAPL"},
    )
    assert result.ok, result.errors


def test_slate_gate_empty_set_rejects_everything():
    """Empty slate must reject every emitted symbol. Macro stories with
    no equity candidates rely on this — the LLM is told to return [], but
    if it disobeys, the verifier holds the line.
    """
    payload = _base_payload(
        {"tickers": [{"symbol": "META", "source_doc_id": "n1", "evidence_span": "Meta"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Meta announced a strategic pivot today."},
        allowed_symbols=set(),
    )
    assert not result.ok
    assert any("not on the candidate slate" in e for e in result.errors)


def test_commodity_rescue_accepts_oil_prices_for_crude_futures():
    # cluster_7183 class: "oil prices" is the right attribution for
    # CL=F / USO even though the exact whitelist term is "oil"; the
    # word-bounded substring rescue picks it up as long as the phrase
    # also appears in the body.
    body = (
        "Crude markets churned as swings in oil prices set the tempo, "
        "with traders watching US-Iran peace talks."
    )
    payload = _base_payload(
        {
            "tickers": [
                {"symbol": "CL=F", "source_doc_id": "n1", "evidence_span": "oil prices"},
                {"symbol": "USO", "source_doc_id": "n1", "evidence_span": "oil prices"},
            ],
            "sectors": [{"tag": "energy", "source_doc_id": "n1"}],
            "regions": [{"tag": "global", "source_doc_id": "n1"}],
        }
    )
    result = verify_story_payload(
        payload, member_ids={"n1"}, member_bodies={"n1": body}
    )
    assert result.ok, result.errors


def test_commodity_rescue_word_boundary_blocks_boil():
    # The word-boundary guard must reject "boil"/"spoiler"-style spans
    # that happen to contain "oil" as a substring. Otherwise the rescue
    # opens a hole big enough for the LLM to attribute random energy
    # spans to CL=F.
    body = "The water began to boil rapidly under the sun."
    payload = _base_payload(
        {"tickers": [{"symbol": "CL=F", "source_doc_id": "n1", "evidence_span": "boil"}]}
    )
    result = verify_story_payload(
        payload, member_ids={"n1"}, member_bodies={"n1": body}
    )
    assert not result.ok
    assert any("uppercase letter" in e or "evidence_span" in e for e in result.errors)


def test_commodity_rescue_requires_body_containment():
    # Even when "oil prices" is in the per-symbol whitelist (as a word-
    # bounded match), the evidence_span must still appear in the cited
    # body. A hallucinated phrase about oil never passes.
    body = "Inflation eased modestly in the latest CPI print."
    payload = _base_payload(
        {"tickers": [{"symbol": "CL=F", "source_doc_id": "n1", "evidence_span": "oil prices"}]}
    )
    result = verify_story_payload(
        payload, member_ids={"n1"}, member_bodies={"n1": body}
    )
    assert not result.ok


def test_commodity_rescue_accepts_wti_crude_oil_futures():
    # "WTI crude oil futures" should rescue CL=F via word-bounded "crude
    # oil" / "wti" terms — the LLM is allowed to be slightly more
    # specific than the bare whitelist entry.
    body = "WTI crude oil futures settled above $90 amid supply concerns."
    payload = _base_payload(
        {
            "tickers": [
                {"symbol": "CL=F", "source_doc_id": "n1", "evidence_span": "WTI crude oil futures"}
            ],
            "sectors": [{"tag": "energy", "source_doc_id": "n1"}],
            "regions": [{"tag": "global", "source_doc_id": "n1"}],
        }
    )
    result = verify_story_payload(
        payload, member_ids={"n1"}, member_bodies={"n1": body}
    )
    assert result.ok, result.errors


def test_slate_gate_none_disables_check():
    """When `allowed_symbols=None`, the gate is off — backwards compat for
    callers that haven't migrated. Legacy substring-in-body still applies.
    """
    payload = _base_payload(
        {"tickers": [{"symbol": "META", "source_doc_id": "n1", "evidence_span": "Meta"}]}
    )
    result = verify_story_payload(
        payload,
        member_ids={"n1"},
        member_bodies={"n1": "Meta announced a strategic pivot today."},
        allowed_symbols=None,
    )
    assert result.ok, result.errors

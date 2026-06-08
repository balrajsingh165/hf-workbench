import pytest
from pydantic import ValidationError

from src.agent.chat_models import ChatCompletionRequest
from src.agent.prompt_manager import (
    build_phase1_system_prompt,
    build_phase2_system_prompt,
)


def _request_payload(
    params: dict | None = None, subject: dict | None = None
) -> dict:
    return {
        "session_id": "finance:user_1:test_mode",
        "messages": [{"role": "user", "content": "Where does this thesis stand?"}],
        "params": params or {},
        "subject": subject or {},
    }


def test_request_params_mode_defaults_to_quick() -> None:
    req = ChatCompletionRequest.model_validate(_request_payload())

    assert req.params.mode == "quick"
    assert req.subject.thesis_ids == []


def test_request_params_reject_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(_request_payload({"mode": "slow"}))


def test_subject_accepts_ambient_ids() -> None:
    """Ambient subject IDs are typed fields on `ChatSubject`."""
    req = ChatCompletionRequest.model_validate(
        _request_payload(
            subject={
                "active_thesis_id": "thesis_abc",
                "active_story_id": "story_xyz",
            },
        )
    )
    assert req.subject.active_thesis_id == "thesis_abc"
    assert req.subject.active_story_id == "story_xyz"


def test_subject_accepts_explicit_thesis_ids() -> None:
    req = ChatCompletionRequest.model_validate(
        _request_payload(subject={"thesis_ids": ["thesis_001", "thesis_002"]})
    )
    assert req.subject.thesis_ids == ["thesis_001", "thesis_002"]


def test_subject_accepts_explicit_story_ids() -> None:
    req = ChatCompletionRequest.model_validate(
        _request_payload(subject={"story_ids": ["story_001", "story_002"]})
    )
    assert req.subject.story_ids == ["story_001", "story_002"]


def test_resolve_story_ids_prefers_explicit_over_ambient() -> None:
    from src.interfaces.ai_sdk_compat.api import _resolve_story_ids

    subject = ChatCompletionRequest.model_validate(
        _request_payload(
            subject={
                "story_ids": ["story_explicit"],
                "active_story_id": "story_ambient",
            },
        )
    ).subject
    assert _resolve_story_ids(subject) == ["story_explicit"]


def test_resolve_story_ids_falls_back_to_ambient() -> None:
    from src.interfaces.ai_sdk_compat.api import _resolve_story_ids

    subject = ChatCompletionRequest.model_validate(
        _request_payload(subject={"active_story_id": "story_ambient"}),
    ).subject
    assert _resolve_story_ids(subject) == ["story_ambient"]


def test_phase2_prompt_embeds_story_context() -> None:
    from src.agent.models import LinkedThesis, StoryContext

    story = StoryContext(
        id="story_test",
        headline="Test headline",
        published_at="2026-05-17 12:00:00",
        body="Story body.",
        linked_theses=[
            LinkedThesis(
                thesis_id="thesis_linked",
                thesis_title="Linked thesis belief.",
                relation="supports",
                confidence=0.81,
                rationale="Headline names the same mechanism.",
            )
        ],
    )
    prompt = build_phase2_system_prompt("quick", [], [story])
    assert "selected_story" in prompt
    assert "story_test" in prompt
    assert "Test headline" in prompt
    assert "Story body." in prompt
    # linked_theses preview surfaces the relation and the human-readable title
    # so the agent can refer to the thesis without printing its slug.
    assert "linked_theses" in prompt
    assert "thesis_linked" in prompt
    assert "Linked thesis belief." in prompt
    assert "supports" in prompt
    assert "confidence 0.81" in prompt


def test_phase2_story_block_when_no_linked_theses() -> None:
    """No links on file → the preview line tells the agent to confirm via search_evidence."""
    from src.agent.models import StoryContext

    story = StoryContext(id="story_orphan", headline="Orphan headline", body="x")
    prompt = build_phase2_system_prompt("quick", [], [story])
    assert "linked_theses: (none on file" in prompt


def test_phase2_what_ui_shows_covers_stories() -> None:
    """Phase 2 base prompt must restrain story-id slugs the same way it restrains thesis slugs."""
    prompt = build_phase2_system_prompt("quick", [])
    assert "story_001" in prompt
    assert "When a story is the subject" in prompt


def test_multi_story_label_in_prompt() -> None:
    """Multi-story selection uses the plural selected_stories label."""
    from src.agent.models import StoryContext

    stories = [
        StoryContext(id="story_a", headline="A"),
        StoryContext(id="story_b", headline="B"),
    ]
    prompt = build_phase2_system_prompt("quick", [], stories)
    assert "selected_stories (2 stories" in prompt
    assert "story_a" in prompt
    assert "story_b" in prompt


def test_phase2_length_budget_is_mode_based() -> None:
    quick = build_phase2_system_prompt("quick", [])
    deep = build_phase2_system_prompt("deep", [])

    assert "Mode: quick" in quick
    assert "1–3 short paragraphs" in quick
    assert "Mode: deep" in deep
    assert "comprehensive answer" in deep


def test_phase1_prompt_includes_mode_and_tool_budget() -> None:
    prompt = build_phase1_system_prompt("deep", [])

    assert "<mode>\ndeep\n</mode>" in prompt
    # Deep mode lets the research agent decide how many sequential rounds are
    # needed instead of the orchestrator forcing an exact minimum.
    assert "Decide autonomously how many sequential rounds are needed" in prompt
    assert "no hard cap" in prompt
    # Mode-conditional injection: quick-mode rules must not leak into deep.
    assert "Prefer one tight batch" not in prompt
    assert "DONE must be the entire final response" in prompt


def test_phase1_quick_prompt_prefers_tight_autonomous_research() -> None:
    prompt = build_phase1_system_prompt("quick", [])

    assert "Prefer one tight batch" in prompt
    assert "batch" in prompt
    assert "in parallel" in prompt
    assert "successful tool batches" in prompt
    assert "Continue only when the previous output is empty" in prompt
    assert "DONE must be the entire final response" in prompt
    # Quick mode must not carry the deep-mode runbook / multi-round mandate.
    assert "AT LEAST 2 tool rounds" not in prompt
    assert "AT LEAST 2 SEQUENTIAL rounds" not in prompt
    assert "Deep-mode tool playbook" not in prompt
    assert "Stop rule (quick)" not in prompt
    assert "Tool-round budget (quick)" not in prompt


def test_phase1_selected_story_prompt_uses_context_before_fetching() -> None:
    from src.agent.models import StoryContext

    story = StoryContext(
        id="story_context",
        headline="Full selected story headline",
        body="Full selected story body with enough detail.",
    )
    for mode in ("quick", "deep"):
        prompt = build_phase1_system_prompt(mode, [], [story])

        assert "Selected-story discipline:" in prompt, mode
        assert "full headline and body" in prompt, mode
        assert "Hard ban:" in prompt, mode
        assert (
            "do NOT call `fetch_story`, `search_stories`, or `web_search`"
            in prompt
        ), mode
        assert "`fetch_story(selected_id)`" in prompt, mode
        assert "quick only" not in prompt.lower(), mode
        assert "deep only" not in prompt.lower(), mode


def test_phase1_selected_story_discipline_only_with_hydrated_story() -> None:
    for mode in ("quick", "deep"):
        prompt = build_phase1_system_prompt(mode, [], [])
        assert "Selected-story discipline:" not in prompt, mode


def test_phase1_deep_prompt_carries_runbook_and_no_repeat() -> None:
    prompt = build_phase1_system_prompt("deep", [])

    # The flexible playbook + hidden-link follow-up framing.
    assert "Tool playbook" in prompt
    assert "HIDDEN LINKS" in prompt
    # The orthogonal-angle no-repeat rule for parallel narrative searches.
    assert "Query-diversity rule:" in prompt
    assert "ORTHOGONAL angle" in prompt
    # The mode-specific stop rule is injected without exposing mode labels.
    assert "Stop rule:" in prompt
    assert "Stop as soon as enough evidence has been gathered" not in prompt
    assert "Stop rule (deep)" not in prompt
    assert "Tool-round budget (deep)" not in prompt


def test_phase1_prompt_excludes_response_shape_content() -> None:
    """Phase 1 must not carry the response phase's citation schema, voice
    rules, or answer-structure rubric.

    Those produce user-facing prose and Phase 1's handoff says it doesn't
    write prose — they were left over from when a single prompt drove both
    phases. The strings below are the giveaways from each leaked section
    (final JSON template fields, the voice block, the "lead with the
    verdict" rubric). If one reappears in Phase 1, the structural split has
    been undone.
    """
    for mode in ("quick", "deep"):
        prompt = build_phase1_system_prompt(mode, [])
        # Final JSON-block schema giveaways:
        assert '"index": 1' not in prompt
        assert '"tool": "search_evidence"' not in prompt
        assert '"citations": [' not in prompt
        # Inline-marker / footnote rules (response-side):
        assert "[^N]" not in prompt
        assert "footnote-definition" not in prompt
        # Voice rules (response-side):
        assert "Plain, simple, everyday words" not in prompt
        assert "jargon-as-decoration" not in prompt
        # Answer-structure rubric (response-side):
        assert "Lead with the conclusion or verdict" not in prompt


def test_phase2_prompt_carries_citation_discipline() -> None:
    """Citation rules and the final-JSON-block schema live only in Phase 2."""
    prompt = build_phase2_system_prompt("quick", [])
    assert '"citations": [' in prompt
    assert "story_276" in prompt
    assert "server hydrates" in prompt
    assert "suggested_strength_delta" not in prompt
    assert "prescription" not in prompt


def test_phase2_system_prompt_embeds_thesis_context() -> None:
    """Phase 2 must see thesis context in its OWN system prompt — not by
    wrapping the entire Phase 1 system prompt into `<user_request>`."""
    from src.agent.models import ThesisContext

    thesis = ThesisContext(
        id="thesis_test",
        statement="Test thesis statement",
        tickers=["AAPL"],
    )
    prompt = build_phase2_system_prompt("quick", [thesis])
    assert "selected_thesis" in prompt
    assert "thesis_test" in prompt
    assert "Test thesis statement" in prompt

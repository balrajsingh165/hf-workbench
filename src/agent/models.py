"""Internal Pydantic models for one Strands-backed Sage turn.

The public interface is `/api/v1/ai-sdk/chat/completions`. `AgentRunRequest`
is an internal adapter shape used by the AI SDK route to drive the existing
two-phase Strands pipeline.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


AllowedModel = Literal["sonnet", "opus", "haiku"]
Theme = Literal["dark", "light"]
ResponseMode = Literal["quick", "deep"]


class ThesisContext(BaseModel):
    """Thesis hydrated by the chat route and passed into the Strands pipeline."""

    id: str
    statement: str
    belief: str | None = None
    tickers: list[str] = Field(default_factory=list)
    # Subset of `tickers` with a known direction (bullish/bearish). Neutral
    # entries are omitted by the parser. A ticker present in `tickers` but
    # absent here means the thesis md left direction unspecified.
    ticker_directions: list[tuple[str, str]] = Field(default_factory=list)
    score: int | float | None = None
    state: str | None = None
    trend: str | None = None
    supporting_evidence: str = ""
    contrasting_evidence: str = ""


class LinkedThesis(BaseModel):
    """One `thesis_story_links` row, scoped to the user's tracked theses."""

    thesis_id: str
    # First-sentence belief from the thesis md so the prompt can refer to the
    # thesis by a descriptive phrase instead of the slug. None when the md
    # can't be loaded.
    thesis_title: str | None = None
    relation: str
    confidence: float
    rationale: str | None = None


class StoryContext(BaseModel):
    """Story hydrated by the chat route, mirroring `ThesisContext`.

    `linked_theses` surfaces existing `thesis_story_links` rows for the user's
    tracked theses so the agent doesn't have to rediscover them via tool calls
    when the user asks "what does this mean for my theses".
    """

    id: str
    headline: str
    published_at: str | None = None
    body: str = ""
    linked_theses: list[LinkedThesis] = Field(default_factory=list)


class ToolCallRecord(BaseModel):
    """Structured record of one Phase 1 tool call. Consumed by the chart agent."""

    tool_use_id: str
    tool: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any | None = None
    status: str = "unknown"


class AgentRunRequest(BaseModel):
    """Internal request passed from the AI SDK route into the Strands pipeline.

    The route hands the phases the raw ingredients — selected theses, selected
    stories, the user's verbatim text, recent history — and each phase builds
    its own system + user prompts via prompt_manager. No pre-baked composite
    system prompt rides on this object; that hack made Phase 2's
    `<user_request>` wrap the entire system prompt verbatim.
    """

    request_id: str = Field(..., description="UUID generated for this turn")
    user_id: str = Field(
        ...,
        description="Active user; bound into user-scoped tool calls.",
    )
    session_id: str | None = Field(
        None,
        description="Chat session id for usage and trace correlation.",
    )
    mode: ResponseMode = "quick"
    theses: list[ThesisContext] = Field(
        default_factory=list,
        description="Hydrated thesis context — explicit thesis_ids, or the single ambient active_thesis_id when none are explicit.",
    )
    user_text: str = Field(
        ...,
        description="Verbatim latest user message; for the chip flow this is the chip sentence (+ optional typed extras).",
    )
    recent_history: str = Field(
        default="",
        description="Pre-formatted recent-turn history for Phase 1's user prompt. Empty string when no prior messages.",
    )
    stories: list[StoryContext] = Field(
        default_factory=list,
        description="Hydrated story context — explicit story_ids, or the single ambient active_story_id when none are explicit.",
    )
    model: AllowedModel = "sonnet"
    enable_charts: bool = False
    theme: Theme = "dark"
    language: str = "en"

    @property
    def primary_thesis(self) -> ThesisContext | None:
        """Chart agent + trace context need a single thesis to frame against."""
        return self.theses[0] if self.theses else None

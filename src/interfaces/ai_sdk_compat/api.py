from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse

from src.agent.ai_sdk_stream import (
    HEADERS_UI_STREAM,
    UIStreamCapture,
    convert_legacy_sse_to_ui_stream,
    protocol_smoke_stream,
    smoke_mode_enabled,
)
from src.agent.chat_models import ChatCompletionRequest
from src.agent.models import AgentRunRequest, LinkedThesis, StoryContext, ThesisContext
from src.agent.orchestrator import run_chip_streaming
from src.chat.message_utils import extract_latest_user_message, parse_session_id
from src.chat.session_store import append_message, ensure_session, list_messages

router = APIRouter(prefix="/api/v1/ai-sdk", tags=["AI SDK"])


def _allowed_model(model: str) -> str:
    return model if model in {"sonnet", "opus", "haiku"} else "haiku"


def _recent_history_text(session_id: str, limit: int = 8) -> str:
    rows = list_messages(session_id)
    recent = rows[-limit:]
    lines: list[str] = []
    for row in recent:
        content = " ".join(str(row["content_text"] or "").split())
        if content:
            lines.append(f"{row['role']}: {content[:800]}")
    return "\n".join(lines) if lines else "(no prior messages)"


_HYDRATE_TOP_SUPPORTS = 5
_HYDRATE_TOP_STRESSES = 3


def _format_evidence_rows(rows: list[Any]) -> str:
    lines: list[str] = []
    for item in rows:
        headline = (item["headline"] or item["story_id"]).rstrip(".")
        rationale = (item["rationale"] or "").strip()
        line = f"- (confidence {item['confidence']:.2f}) {headline}."
        if rationale:
            line += f" {rationale}"
        lines.append(line)
    return "\n".join(lines)


def _hydrate_thesis(thesis_id: str, user_id: str) -> ThesisContext:
    from api import db, first_sentence, load_thesis_md, trend_for

    doc = load_thesis_md(thesis_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Thesis {thesis_id} not found")

    with db() as conn:
        row = conn.execute(
            """
            SELECT ut.status, t.score,
                   (SELECT score FROM thesis_snapshots s
                     WHERE s.thesis_id = ut.thesis_id
                       AND s.snapshot_date < date('now')
                     ORDER BY s.snapshot_date DESC LIMIT 1) AS prev_score
            FROM user_theses ut
            JOIN theses t ON t.id = ut.thesis_id
            WHERE ut.user_id = ? AND ut.thesis_id = ?
            """,
            (user_id, thesis_id),
        ).fetchone()
        supports_rows = conn.execute(
            """
            SELECT l.story_id, l.relation, l.confidence, l.rationale,
                   s.created_at, s.headline
            FROM thesis_story_links l
            JOIN story s ON s.id = l.story_id
            WHERE l.thesis_id = ? AND l.relation = 'supports'
            ORDER BY s.created_at DESC, l.confidence DESC
            LIMIT ?
            """,
            (thesis_id, _HYDRATE_TOP_SUPPORTS),
        ).fetchall()
        stresses_rows = conn.execute(
            """
            SELECT l.story_id, l.relation, l.confidence, l.rationale,
                   s.created_at, s.headline
            FROM thesis_story_links l
            JOIN story s ON s.id = l.story_id
            WHERE l.thesis_id = ? AND l.relation = 'stresses'
            ORDER BY s.created_at DESC, l.confidence DESC
            LIMIT ?
            """,
            (thesis_id, _HYDRATE_TOP_STRESSES),
        ).fetchall()

    # IMPORTANT: when the user does not own this thesis, leave score/state/trend
    # as None so the prompt formatter can omit them entirely. Defaulting to
    # 0/"active" silently lies to the model and produces score-recap prose for
    # an unowned thesis.
    if row is not None:
        score = row["score"]
        prev = row["prev_score"] if row["prev_score"] is not None else score
        state = row["status"]
        trend = trend_for(score, prev, state) if score is not None else None
    else:
        score = None
        state = None
        trend = None

    return ThesisContext(
        id=thesis_id,
        statement=doc.core_thesis,
        belief=first_sentence(doc.core_thesis),
        tickers=doc.tickers,
        ticker_directions=list(doc.ticker_directions),
        score=int(score) if score is not None else None,
        state=state,
        trend=trend,
        supporting_evidence=_format_evidence_rows(supports_rows),
        contrasting_evidence=_format_evidence_rows(stresses_rows),
    )


_LINKED_THESES_LIMIT = 5


def _hydrate_story(story_id: str, user_id: str) -> StoryContext:
    """Structured story context for the prompt.

    Mirrors `_hydrate_thesis`: raises HTTPException(404) when the story isn't
    on file so the safe wrapper can catch and skip. The full story title and
    markdown body are injected so story-scoped research can use the selected
    story directly instead of spending a tool call to refetch it.

    `linked_theses` is scoped to the requesting user's tracked theses, mirroring
    how thesis hydration only emits score/state/trend when `user_theses` has a
    row for this (user_id, thesis_id) pair.
    """
    from api import ROOT, db, first_sentence, load_thesis_md
    import re as _re

    if not story_id.startswith("story_"):
        raise HTTPException(status_code=404, detail=f"Story {story_id} not found")

    with db() as conn:
        row = conn.execute(
            "SELECT id, headline, created_at FROM story WHERE id = ?",
            (story_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Story {story_id} not found")
        links_rows = conn.execute(
            """
            SELECT tsl.thesis_id, tsl.relation, tsl.confidence, tsl.rationale
            FROM thesis_story_links tsl
            JOIN user_theses ut
              ON ut.thesis_id = tsl.thesis_id AND ut.user_id = ?
            WHERE tsl.story_id = ?
            ORDER BY tsl.confidence DESC
            LIMIT ?
            """,
            (user_id, story_id, _LINKED_THESES_LIMIT),
        ).fetchall()

    md_path = ROOT / "global" / "stories" / f"{story_id}.md"
    body = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    body = _re.sub(r"^#\s+.+?\n", "", body, count=1).strip()

    linked: list[LinkedThesis] = []
    for lrow in links_rows:
        doc = load_thesis_md(lrow["thesis_id"])
        title = first_sentence(doc.core_thesis) if doc else None
        rationale = (lrow["rationale"] or "").strip() or None
        linked.append(
            LinkedThesis(
                thesis_id=lrow["thesis_id"],
                thesis_title=title,
                relation=lrow["relation"],
                confidence=float(lrow["confidence"]),
                rationale=rationale,
            )
        )

    return StoryContext(
        id=story_id,
        headline=(row["headline"] or story_id).strip(),
        published_at=row["created_at"] or None,
        body=body,
        linked_theses=linked,
    )


def _resolve_thesis_ids(subject: Any) -> list[str]:
    """Explicit thesis_ids win; ambient active_thesis_id is a single-thesis fallback.

    Mirrors the precedence the frontend enforces in `buildChatRequestContext`.
    """
    explicit = list(subject.thesis_ids)
    if explicit:
        return explicit
    if subject.active_thesis_id:
        return [subject.active_thesis_id]
    return []


def _resolve_story_ids(subject: Any) -> list[str]:
    """Explicit story_ids win; ambient active_story_id is a single-story fallback.

    Mirrors the precedence the frontend enforces in `buildChatRequestContext`.
    """
    explicit = list(subject.story_ids)
    if explicit:
        return explicit
    if subject.active_story_id:
        return [subject.active_story_id]
    return []


def _safe_hydrate_thesis(thesis_id: str, user_id: str) -> ThesisContext | None:
    """Hydrate or skip — never 500 the chat because a stale ambient id pointed
    at a deleted thesis."""
    try:
        return _hydrate_thesis(thesis_id, user_id)
    except HTTPException:
        return None


def _safe_hydrate_story(story_id: str, user_id: str) -> StoryContext | None:
    """Hydrate or skip — never 500 the chat because a stale ambient id pointed
    at a deleted story."""
    try:
        return _hydrate_story(story_id, user_id)
    except HTTPException:
        return None


@router.post("/chat/completions")
async def chat_completions(raw_req: dict[str, Any]) -> StreamingResponse:
    try:
        req = ChatCompletionRequest.model_validate(raw_req)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        platform, user_id, _short_session_id = parse_session_id(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        user_text = extract_latest_user_message(req.messages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Persist a flat snapshot so analytics doesn't have to know the params/subject
    # split. Only the call site that reads it back (none today; planned for
    # session-level telemetry) needs to care about the shape.
    persisted_metadata = {
        "params": req.params.model_dump(exclude_none=True),
        "subject": req.subject.model_dump(exclude_none=True),
    }
    effective_thesis_ids = _resolve_thesis_ids(req.subject)
    effective_story_ids = _resolve_story_ids(req.subject)

    ensure_session(
        session_id=req.session_id,
        platform=platform,
        user_id=user_id,
        metadata=persisted_metadata,
    )
    append_message(
        session_id=req.session_id,
        role="user",
        content_text=user_text,
        parts=[{"type": "text", "text": user_text, "state": "done"}],
    )

    message_id = f"msg_{uuid.uuid4().hex}"
    capture = UIStreamCapture()

    async def stream_and_persist() -> AsyncIterator[bytes]:
        cancelled = False
        try:
            if smoke_mode_enabled():
                async for chunk in protocol_smoke_stream(
                    message_id=message_id,
                    user_text=user_text,
                    capture=capture,
                ):
                    yield chunk
            else:
                theses: list[ThesisContext] = []
                for tid in effective_thesis_ids:
                    ctx = _safe_hydrate_thesis(tid, user_id)
                    if ctx is not None:
                        theses.append(ctx)
                stories: list[StoryContext] = []
                for sid in effective_story_ids:
                    ctx = _safe_hydrate_story(sid, user_id)
                    if ctx is not None:
                        stories.append(ctx)
                legacy_req = AgentRunRequest(
                    request_id=f"req_{uuid.uuid4().hex}",
                    user_id=user_id,
                    session_id=req.session_id,
                    mode=req.params.mode,
                    theses=theses,
                    user_text=user_text,
                    recent_history=_recent_history_text(req.session_id),
                    stories=stories,
                    model=_allowed_model(req.model),
                    enable_charts=req.params.enable_charts,
                    theme=req.params.theme,
                    language=req.params.language,
                )
                async for chunk in convert_legacy_sse_to_ui_stream(
                    run_chip_streaming(legacy_req),
                    message_id=message_id,
                    capture=capture,
                ):
                    yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            # Client aborted (Stop button / page navigation). Persist whatever
            # was streamed so refresh shows the partial answer with a marker.
            cancelled = True
            raise
        finally:
            parts = list(capture.parts)
            if capture.text:
                parts.append({"type": "text", "text": capture.text, "state": "done"})
            if cancelled:
                parts.append(
                    {"type": "data-cancelled", "data": {"reason": "client-abort"}}
                )
            append_message(
                session_id=req.session_id,
                role="assistant",
                content_text=capture.text,
                parts=parts,
                message_id=message_id,
            )

    return StreamingResponse(
        stream_and_persist(),
        media_type="text/event-stream",
        headers=HEADERS_UI_STREAM,
    )

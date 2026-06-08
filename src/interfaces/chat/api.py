from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.chat.message_utils import build_session_id, parse_session_id
from src.chat.session_store import (
    append_message,
    delete_session,
    ensure_session,
    get_session,
    list_messages,
    list_sessions,
)
from src.chat.shares import (
    get_share_by_session_id,
    get_share_by_share_id,
    set_share_public,
)
from src.chat.ui_messages import rows_to_ui_messages

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatSummary(BaseModel):
    id: str
    title: str | None = None
    updated_at: str | None = None


class ChatCreateRequest(BaseModel):
    platform: str = "finance"
    user_id: str
    metadata: dict[str, Any] | None = None


class ChatCreateResponse(BaseModel):
    id: str
    session_id: str
    title: str | None = None
    updated_at: str | None = None


class ChatMessagesResponse(BaseModel):
    title: str | None = None
    preview_question: str | None = None
    messages: list[dict[str, Any]]


class ShareStatusResponse(BaseModel):
    public: bool
    share_id: str | None = None


class ShareToggleRequest(BaseModel):
    public: bool


class DuplicateFromShareRequest(BaseModel):
    platform: str = "finance"
    user_id: str


class ChatDuplicateResponse(BaseModel):
    id: str
    session_id: str


class ChatDeleteResponse(BaseModel):
    id: str
    deleted: bool


@router.post("/chats", response_model=ChatCreateResponse)
async def create_chat(req: ChatCreateRequest) -> ChatCreateResponse:
    short_id = str(uuid.uuid4())
    try:
        session_id = build_session_id(req.platform, req.user_id, short_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = ensure_session(
        session_id=session_id,
        platform=req.platform,
        user_id=req.user_id,
        metadata=req.metadata or {},
    )
    return ChatCreateResponse(
        id=session_id,
        session_id=session_id,
        title=session["title"],
        updated_at=session["updated_at"],
    )


@router.get("/chats", response_model=list[ChatSummary])
async def get_chats(
    platform: str = Query("finance"),
    user_id: str = Query(...),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[ChatSummary]:
    rows = list_sessions(platform=platform, user_id=user_id, limit=limit, offset=offset)
    return [
        ChatSummary(
            id=row["session_id"],
            title=row["title"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


@router.get("/chats/{session_id}/messages", response_model=ChatMessagesResponse)
async def get_chat_messages(session_id: str) -> ChatMessagesResponse:
    try:
        parse_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return ChatMessagesResponse(
        title=session["title"],
        messages=rows_to_ui_messages(list_messages(session_id)),
    )


@router.delete("/chats/{session_id}", response_model=ChatDeleteResponse)
async def remove_chat(session_id: str) -> ChatDeleteResponse:
    try:
        parse_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ChatDeleteResponse(id=session_id, deleted=delete_session(session_id))


@router.get("/chats/{session_id}/share", response_model=ShareStatusResponse)
async def get_share_status(session_id: str) -> ShareStatusResponse:
    try:
        parse_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if get_session(session_id) is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    row = get_share_by_session_id(session_id)
    if row is None or not row["is_public"]:
        return ShareStatusResponse(public=False, share_id=None)
    return ShareStatusResponse(public=True, share_id=row["share_id"])


@router.put("/chats/{session_id}/share", response_model=ShareStatusResponse)
async def set_share_status(
    session_id: str,
    req: ShareToggleRequest,
) -> ShareStatusResponse:
    try:
        parse_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if get_session(session_id) is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    row = set_share_public(session_id, req.public)
    if row is None or not row["is_public"]:
        return ShareStatusResponse(public=False, share_id=None)
    return ShareStatusResponse(public=True, share_id=row["share_id"])


@router.get("/public/chats/{share_id}", response_model=ChatMessagesResponse)
async def get_public_chat(share_id: str) -> ChatMessagesResponse:
    row = get_share_by_share_id(share_id)
    if row is None or not row["is_public"]:
        raise HTTPException(status_code=404, detail="Shared chat not found")
    session = get_session(row["session_id"])
    if session is None:
        raise HTTPException(status_code=404, detail="Shared chat not found")
    return ChatMessagesResponse(
        title=session["title"],
        preview_question=row["preview_question"],
        messages=rows_to_ui_messages(list_messages(row["session_id"])),
    )


@router.post("/public/chats/{share_id}/duplicate", response_model=ChatDuplicateResponse)
async def duplicate_public_chat(
    share_id: str,
    req: DuplicateFromShareRequest,
) -> ChatDuplicateResponse:
    row = get_share_by_share_id(share_id)
    if row is None or not row["is_public"]:
        raise HTTPException(status_code=404, detail="Shared chat not found")
    source_session = get_session(row["session_id"])
    if source_session is None:
        raise HTTPException(status_code=404, detail="Shared chat not found")

    new_short_id = str(uuid.uuid4())
    new_session_id = build_session_id(req.platform, req.user_id, new_short_id)
    ensure_session(
        session_id=new_session_id,
        platform=req.platform,
        user_id=req.user_id,
        metadata={"duplicated_from_share_id": share_id},
    )
    for message in rows_to_ui_messages(list_messages(row["session_id"])):
        text = "\n\n".join(
            part.get("text", "")
            for part in message.get("parts", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        append_message(
            session_id=new_session_id,
            role=str(message.get("role") or "assistant"),
            content_text=text,
            parts=message.get("parts", []),
        )
    return ChatDuplicateResponse(id=new_session_id, session_id=new_session_id)

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.i18n import normalize_language


ResponseMode = Literal["quick", "deep"]


class ContentPart(BaseModel):
    type: Literal["text", "image", "file"]
    text: str | None = None
    image: str | None = None
    url: str | None = None
    mimeType: str | None = None
    mediaType: str | None = None


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart] | None = None


class ChatParams(BaseModel):
    """Conversation parameters — affect agent behaviour, not subject.

    `extra='allow'` so we don't 400 when an older client (or a future field
    we add to the FE first) sends a key we don't model yet.
    """

    mode: ResponseMode = "quick"
    enable_charts: bool = False
    theme: Literal["dark", "light"] = "dark"
    language: str = "en"

    model_config = ConfigDict(extra="allow")

    @field_validator("language", mode="before")
    @classmethod
    def normalize_language_param(cls, value: object) -> str:
        return normalize_language(value if isinstance(value, str) else None)


class ChatSubject(BaseModel):
    """What this turn is about.

    Two precedence-paired channels per kind (thesis, story):
      - `thesis_ids` / `story_ids` (explicit) win over ambient ids
      - `active_thesis_id` / `active_story_id` (ambient detail surfaces)

    The frontend enforces precedence before sending — when an explicit
    reference of a given kind is attached, the matching ambient id is
    dropped. The backend hydrates whichever channel arrived.
    """

    thesis_ids: list[str] = Field(default_factory=list)
    story_ids: list[str] = Field(default_factory=list)
    references: list[dict] = Field(default_factory=list)
    active_thesis_id: str | None = None
    active_story_id: str | None = None

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    model: str = "haiku"
    messages: list[ChatMessage] = Field(default_factory=list)
    session_id: str
    params: ChatParams = Field(default_factory=ChatParams)
    subject: ChatSubject = Field(default_factory=ChatSubject)
    stream: bool = True

    @model_validator(mode="after")
    def validate_messages(self) -> "ChatCompletionRequest":
        if not self.messages:
            raise ValueError("messages[] is required")
        return self

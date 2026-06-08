from __future__ import annotations

from typing import Any


def validate_session_components(
    platform: str,
    user_id: str,
    session_id: str,
) -> tuple[str, str, str]:
    p = str(platform).strip()
    uid = str(user_id).strip()
    sid = str(session_id).strip()
    if not p:
        raise ValueError("platform cannot be empty")
    if not uid:
        raise ValueError("user_id cannot be empty")
    if not sid:
        raise ValueError("session_id cannot be empty")
    if ":" in p:
        raise ValueError("platform cannot contain colons")
    if ":" in uid:
        raise ValueError("user_id cannot contain colons")
    if ":" in sid:
        raise ValueError("session_id cannot contain colons")
    return p, uid, sid


def build_session_id(platform: str, user_id: str, session_id: str) -> str:
    p, uid, sid = validate_session_components(platform, user_id, session_id)
    return f"{p}:{uid}:{sid}"


def parse_session_id(session_id: str) -> tuple[str, str, str]:
    parts = session_id.split(":", 2)
    if len(parts) != 3:
        raise ValueError("Invalid session_id format. Expected platform:user_id:session_id")
    return validate_session_components(parts[0], parts[1], parts[2])


def to_plain_dict(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump(exclude_none=True)
        except Exception:
            return None
    if hasattr(item, "dict"):
        try:
            return item.dict()
        except Exception:
            return None
    return None


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts: list[str] = []
        for raw in content:
            part = to_plain_dict(raw) or {}
            part_type = str(part.get("type") or "").lower()
            if part_type in {"text", "input_text", "output_text"}:
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())
        return "\n\n".join(text_parts).strip()
    return ""


def extract_latest_user_message(messages: list[Any]) -> str:
    for raw in reversed(messages):
        msg = to_plain_dict(raw) or {}
        if msg.get("role") != "user":
            continue
        text = extract_text_from_content(msg.get("content"))
        if text:
            return text
    raise ValueError("No user message found in messages array")


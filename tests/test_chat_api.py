from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.interfaces.chat import api as chat_api


@pytest.mark.anyio("asyncio")
async def test_create_chat_builds_full_session_id(monkeypatch):
    captured: dict = {}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    monkeypatch.setattr(chat_api.uuid, "uuid4", lambda: "short-session")

    def fake_ensure_session(**kwargs):
        captured.update(kwargs)
        return {"title": None, "updated_at": now}

    monkeypatch.setattr(chat_api, "ensure_session", fake_ensure_session)

    result = await chat_api.create_chat(
        chat_api.ChatCreateRequest(
            platform="finance",
            user_id="user_1",
            metadata={"surface": "commanddock"},
        )
    )

    assert result.id == "finance:user_1:short-session"
    assert result.session_id == "finance:user_1:short-session"
    assert result.updated_at == now
    assert captured == {
        "session_id": "finance:user_1:short-session",
        "platform": "finance",
        "user_id": "user_1",
        "metadata": {"surface": "commanddock"},
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"

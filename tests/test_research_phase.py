from __future__ import annotations

import json
from asyncio import Queue
from typing import Any

import pytest

from src.agent.research import _count_tool_calls, _run_phase1_stream


class _FakeResult:
    stop_reason = "end_turn"
    metrics = None


class _FakeStream:
    def __init__(self, agent: "_FakeAgent") -> None:
        self.agent = agent
        self.index = 0
        self.events = [
            self._tool_use("tool_1", "search_stories", {"query": "today"}),
            self._tool_result("tool_1", {"stories": [{"topic": "Treasuries"}]}),
            {"data": "Now I will search the web."},
            self._tool_use("tool_2", "web_search", {"query": "Treasuries"}),
            self._tool_result("tool_2", {"results": [{"url": "https://example.com"}]}),
            {"data": "DONE"},
            {"result": _FakeResult()},
        ]

    @staticmethod
    def _tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        return {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": tool_id,
                            "name": name,
                            "input": tool_input,
                        }
                    }
                ],
            }
        }

    @staticmethod
    def _tool_result(tool_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "message": {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": tool_id,
                            "status": "success",
                            "content": [{"text": json.dumps(payload)}],
                        }
                    }
                ],
            }
        }

    async def __anext__(self) -> dict[str, Any]:
        if self.agent.cancelled:
            raise StopAsyncIteration
        if self.index >= len(self.events):
            raise StopAsyncIteration
        event = self.events[self.index]
        self.index += 1
        if "message" in event:
            self.agent.messages.append(event["message"])
        return event

    async def aclose(self) -> None:
        return None


class _FakeAgent:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.cancelled = False
        self.cancel_calls = 0

    def stream_async(self, _prompt: str) -> _FakeStream:
        return _FakeStream(self)

    def cancel(self) -> None:
        self.cancelled = True
        self.cancel_calls += 1


@pytest.mark.anyio("asyncio")
async def test_research_text_does_not_cancel_follow_up_tool_round() -> None:
    agent = _FakeAgent()
    queue: Queue[bytes | None] = Queue()

    stop_reason, metrics = await _run_phase1_stream(agent, "research", queue)

    assert stop_reason == "end_turn"
    assert metrics == {}
    assert agent.cancel_calls == 0
    assert _count_tool_calls(agent.messages) == 2


@pytest.fixture
def anyio_backend():
    return "asyncio"

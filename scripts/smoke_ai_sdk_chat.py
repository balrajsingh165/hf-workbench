#!/usr/bin/env python3
"""Manual smoke test for the AI SDK chat protocol.

Run against a live workbench server:

    HF_AGENT_PROTOCOL_SMOKE=1 uv run uvicorn app:app --host 0.0.0.0 --port 8088
    uv run python scripts/smoke_ai_sdk_chat.py
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


REQUIRED_TYPES = {
    "start",
    "tool-input-start",
    "tool-input-available",
    "tool-output-available",
    "text-start",
    "text-delta",
    "text-end",
    "finish",
}


def iter_sse_json(resp: httpx.Response):
    for line in resp.iter_lines():
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            yield {"type": "[DONE]"}
            continue
        yield json.loads(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8088")
    parser.add_argument("--session-id", default="finance:user_1:smoke_ai_sdk_chat")
    parser.add_argument("--thesis-id", default="thesis_001")
    args = parser.parse_args()

    seen: set[str] = set()
    with httpx.stream(
        "POST",
        f"{args.base}/api/v1/ai-sdk/chat/completions",
        json={
            "session_id": args.session_id,
            "messages": [
                {"role": "user", "content": "Find the strongest counterpoints."}
            ],
            "params": {"mode": "quick"},
            "subject": {"thesis_ids": [args.thesis_id]},
            "stream": True,
        },
        timeout=httpx.Timeout(connect=5, read=120, write=10, pool=10),
    ) as resp:
        print(resp.status_code, resp.headers.get("x-vercel-ai-ui-message-stream"))
        resp.raise_for_status()
        if resp.headers.get("x-vercel-ai-ui-message-stream") != "v1":
            print("missing x-vercel-ai-ui-message-stream: v1", file=sys.stderr)
            return 1
        for event in iter_sse_json(resp):
            etype = event.get("type")
            seen.add(str(etype))
            print(json.dumps(event, default=str))

    missing = REQUIRED_TYPES - seen
    if missing:
        print(f"missing required chunks: {sorted(missing)}", file=sys.stderr)
        return 1
    print("AI SDK chat smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


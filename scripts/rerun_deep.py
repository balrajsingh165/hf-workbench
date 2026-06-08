#!/usr/bin/env python3
"""One-shot: re-run quick-mode chats in deep mode and dump the SSE trace.

Usage:
    uv run python scripts/rerun_deep.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://localhost:8088"

# (session-id slug for output filename, user prompts in order, story_ids?, thesis_ids?)
RUNS = [
    (
        "nvda_outlook",
        ["nvda outlook"],
        [],
        [],
    ),
    (
        "macro_outlook",
        ["macro outlook"],
        [],
        [],
    ),
    (
        "market_today_then_news",
        [
            "What's the most decision-relevant move in markets today?",
            "search related news",
        ],
        [],
        [],
    ),
]

OUT = Path("logs/rerun_deep")
OUT.mkdir(parents=True, exist_ok=True)


def run_turn(session_id: str, user_text: str, thesis_ids, story_ids, out_fp):
    body = {
        "session_id": session_id,
        "messages": [{"role": "user", "content": user_text}],
        "params": {"mode": "deep", "enable_charts": False, "theme": "dark"},
        "subject": {"thesis_ids": thesis_ids, "story_ids": story_ids, "references": []},
        "stream": True,
    }
    out_fp.write(f"\n========== USER: {user_text!r}  (deep) ==========\n")
    out_fp.flush()
    t0 = time.time()
    with httpx.stream(
        "POST",
        f"{BASE}/api/v1/ai-sdk/chat/completions",
        json=body,
        timeout=httpx.Timeout(connect=5, read=600, write=10, pool=10),
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            t = ev.get("type", "")
            if t == "tool-input-available":
                out_fp.write(
                    f"[tool-call] {ev.get('toolName')} input={json.dumps(ev.get('input'), default=str)}\n"
                )
            elif t == "tool-output-available":
                # keep output compact
                payload_out = ev.get("output")
                s = json.dumps(payload_out, default=str)
                if len(s) > 1500:
                    s = s[:1500] + f"... [+{len(s) - 1500} chars]"
                out_fp.write(f"[tool-out ] {ev.get('toolName')} output={s}\n")
            elif t == "text-delta":
                out_fp.write(ev.get("delta", ""))
            elif t == "text-end":
                out_fp.write("\n")
            elif t == "finish":
                out_fp.write(f"\n[finish in {time.time() - t0:.1f}s]\n")
            elif t == "error":
                out_fp.write(f"\n[error] {ev}\n")
            out_fp.flush()


def main() -> int:
    ts = time.strftime("%Y%m%dT%H%M%S")
    for slug, prompts, theses, stories in RUNS:
        sid = f"finance:user_1:rerun-deep-{slug}-{ts}"
        out_path = OUT / f"{slug}.deep.{ts}.txt"
        with out_path.open("w") as fp:
            fp.write(f"session_id={sid}\n")
            for prompt in prompts:
                run_turn(sid, prompt, theses, stories, fp)
        print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

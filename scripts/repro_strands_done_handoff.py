#!/usr/bin/env python3
"""Test research handoff strategy: natural stop with terminal text "DONE".

This is a standalone Strands/Bedrock probe. It does not import or patch the
production research agent. The test gives the model local fake tools and a
research-only system prompt:

- call tools autonomously until the research is complete
- do not write analysis
- when done, end with the terminal word: DONE

The script prints tool-call rounds, tool calls, final text, token usage, and
latency so we can decide whether terminal DONE is a viable stop strategy.

Usage:
    uv run python scripts/repro_strands_done_handoff.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import boto3
from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.executors import ConcurrentToolExecutor

from src.agent.config import get_agent_config
from src.agent.observability import summarize_agent_metrics


@tool
def get_trending_stories(query: str = "today", top_k: int = 5) -> dict[str, Any]:
    """Return today's trending finance stories."""
    return {
        "query": query,
        "stories": [
            {
                "headline": "Thirty-year Treasury yield breaks above 5.1%",
                "topic": "Treasury yields",
                "url": "https://example.com/treasury-yields",
            },
            {
                "headline": "Copper rallies as smelter disruptions tighten supply",
                "topic": "Copper supply",
                "url": "https://example.com/copper-supply",
            },
            {
                "headline": "AI power demand lifts utility capex forecasts",
                "topic": "AI power demand",
                "url": "https://example.com/ai-power",
            },
        ][:top_k],
    }


@tool
def web_search(query: str, top_k: int = 3) -> dict[str, Any]:
    """Search the web for background on a finance topic."""
    return {
        "query": query,
        "results": [
            {
                "title": "Why long bond yields are rising",
                "url": "https://example.com/long-bond-yields",
                "snippet": (
                    "The move reflects sticky inflation, larger Treasury "
                    "issuance, weak auction demand, and less foreign buying."
                ),
            },
            {
                "title": "Foreign demand for Treasuries weakens",
                "url": "https://example.com/foreign-demand",
                "snippet": (
                    "Japan and China have reduced purchases while currency "
                    "pressure increases reserve-management sales."
                ),
            },
        ][:top_k],
    }


@tool
def web_fetch(url: str) -> dict[str, Any]:
    """Fetch a web page by URL."""
    return {
        "url": url,
        "title": "Why long bond yields are rising",
        "content": (
            "Long-term Treasury yields are rising because investors are "
            "demanding more compensation for inflation risk and fiscal supply. "
            "Auction demand has weakened. Foreign official buyers are less "
            "reliable. The second-order concepts are term premium, breakeven "
            "inflation, duration risk, deficit financing, and dollar pressure."
        ),
    }


@tool
def concept_search(seed_concepts: list[str], top_k: int = 5) -> dict[str, Any]:
    """Search related concepts that deepen the research trail."""
    return {
        "seed_concepts": seed_concepts,
        "related": [
            "term premium",
            "breakeven inflation",
            "Treasury auction tail",
            "foreign reserve recycling",
            "duration risk",
        ][:top_k],
    }


SYSTEM_PROMPT = """You are a research-only financial agent.

Your job is to gather evidence with tools. A separate response agent writes
the user-facing answer.

Autonomy:
- Decide yourself when research is complete.
- Use as many sequential tool rounds as needed.
- Each round may include multiple tool calls in parallel.
- After each tool result, inspect the output and decide whether another tool
  round is needed.
- For chained requests, later rounds must use specifics from earlier tool
  outputs such as a topic, URL, title, or concept.

Output rule:
- Your assistant turns may contain only tool calls, short private transition
  text needed to continue tool use, or terminal DONE.
- If the next action is research, emit tool calls only. No text before the
  tool calls. No text after the tool calls.
- If research is complete and no more tools are needed, output exactly:
DONE
- DONE must be the final terminal word.
- Do not write analysis, conclusions, citations, markdown, or a user-facing
  answer. Keep any transition text short because it is private handoff text.
"""


DEFAULT_PROMPT = (
    "Get todays trending stories first, and then search web about one topic "
    "of it, and then based on the web search result, read the web page and "
    "search for related concepts in a deep way."
)


def _count_tool_rounds(messages: list[dict[str, Any]]) -> int:
    return sum(
        1
        for msg in messages
        if msg.get("role") == "assistant"
        and any("toolUse" in c for c in msg.get("content", []) if isinstance(c, dict))
    )


async def run_probe(prompt: str, max_tokens: int) -> int:
    cfg = get_agent_config()
    session = boto3.Session(
        profile_name=cfg.bedrock_profile,
        region_name=cfg.aws_region,
    )
    model = BedrockModel(
        boto_session=session,
        model_id=cfg.bedrock_model_id,
        temperature=0,
        max_tokens=max_tokens,
    )
    agent = Agent(
        system_prompt=SYSTEM_PROMPT,
        model=model,
        callback_handler=None,
        tool_executor=ConcurrentToolExecutor(),
        tools=[get_trending_stories, web_search, web_fetch, concept_search],
        agent_id="done_handoff_probe",
    )

    started = time.monotonic()
    final_text = ""
    tool_calls: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}

    async for event in agent.stream_async(prompt):
        if "message" in event:
            for content in event["message"].get("content", []):
                if not isinstance(content, dict):
                    continue
                if "toolUse" in content:
                    tool_use = content["toolUse"]
                    tool_calls.append(
                        {
                            "name": tool_use.get("name"),
                            "input": tool_use.get("input"),
                        }
                    )
        data = event.get("data")
        if isinstance(data, str):
            final_text += data
        if "result" in event:
            result = event["result"]
            metrics = summarize_agent_metrics(getattr(result, "metrics", None))

    elapsed = time.monotonic() - started
    print(f"model={cfg.bedrock_model_id}")
    print(f"elapsed_seconds={elapsed:.2f}")
    print(f"tool_rounds={_count_tool_rounds(agent.messages)}")
    print(f"tool_call_count={len(tool_calls)}")
    print("tool_calls=" + json.dumps(tool_calls, indent=2, default=str))
    final_ends_done = final_text.strip().endswith("DONE")
    print(f"final_text={final_text!r}")
    print("final_ends_done=" + str(final_ends_done))
    print(f"final_text_chars={len(final_text)}")
    print("usage=" + json.dumps(metrics, indent=2, default=str))
    return 0 if final_ends_done else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--max-tokens", type=int, default=512)
    args = parser.parse_args()
    return asyncio.run(run_probe(args.prompt, args.max_tokens))


if __name__ == "__main__":
    raise SystemExit(main())

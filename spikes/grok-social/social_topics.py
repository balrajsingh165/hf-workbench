"""Source today's heated social topics for a ticker via Grok X Search.

The settled spike outcome (see FINDINGS.md): structured per-ticker call on
grok-4.20-0309-reasoning, JSON in the Social-tab shape, V7 angle style.
Each tweet is verified against the API's url_citation annotations by status
ID — unverified tweets are flagged (production would drop them).

    uv run python spikes/grok-social/social_topics.py MSTR --name "MicroStrategy"
    uv run python spikes/grok-social/social_topics.py MU --days 7
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys

from grok_client import DEFAULT_MODEL, XSearchOptions, search_x

# House style distilled from docs/design-thesis-creation.md +
# docs/news-story-pipeline.md, with the angle rule settled by the V0–V7
# prompt comparison (FINDINGS.md): claim first, then the most telling concrete
# detail — a number only when the discussion genuinely turns on one.
STYLE = """\
Voice rules (strict):
- Declarative sentences only. No hedging words: never "might", "could", "may", \
"possibly", "it seems".
- Every topic states explicitly what changed and why traders care.
- Bull and bear angles are each 1-2 sentences expressing the actual conviction \
voiced on X, not a both-sides summary. The first sentence states the conviction \
in plain words. The second gives the strongest evidence behind it — the most \
telling concrete detail traders actually cite: a figure, a dated event, a named \
actor, or a specific claim — with enough context to stand alone. Use a number \
only when the discussion genuinely turns on it; never force or invent \
precision. One idea per sentence; at most two numbers per sentence; never chain \
multiple facts into one clause. Never open with attribution like "Bulls say" — \
the field already carries the stance.
- Quality bar: fewer strong topics beats padding. Skip low-signal chatter, \
follower-bait, and pure price cheerleading with no claim behind it."""

SCHEMA = """\
Return ONLY valid JSON (no markdown fences, no prose before/after):
{
  "ticker": "...",
  "topics": [
    {
      "title": "<=10 word punchy topic title",
      "kind": "discussion" | "debate" | "event" | "info",
      "heat": 1-5,
      "summary": "2-3 declarative sentences: what the topic is and what changed",
      "bull_angle": "the bull conviction being voiced",
      "bear_angle": "the bear conviction being voiced",
      "tweets": [
        {"handle": "@...", "url": "https://x.com/...", "stance": "bull"|"bear"|"neutral",
         "claim": "one-line paraphrase of the post's claim",
         "engagement": "rough likes/views if visible, else null"}
      ]
    }
  ]
}
Rules: 2-5 topics, ordered by heat desc. 2-6 tweets per topic with REAL post
URLs from your search results. "debate" means bulls and bears are actively
arguing; "event" means a dated catalyst; "info" means new facts (filings,
data); "discussion" is everything else with traction."""

PROMPT = """\
You are the data source for the "Social" tab of a thesis-driven trading app.
Users see a timeline of today's heated topics about {name} (${ticker}) — each
topic shows bull/bear angles and the source tweets behind it.

Search X thoroughly for recent discussion of ${ticker}. Identify the heated
topics, then emit them in the schema below.

{style}

{schema}"""


def parse_json_loose(text: str):
    """Best-effort: strip fences / citation markers, find first JSON value."""
    t = re.sub(r"```(?:json)?", "", text).strip()
    t = re.sub(r"\[\[\d+\]\]\(https?://[^)]+\)", "", t)
    start = min((i for i in (t.find("{"), t.find("[")) if i >= 0), default=-1)
    if start < 0:
        return None
    for end in range(len(t), start, -1):
        try:
            return json.loads(t[start:end])
        except json.JSONDecodeError:
            continue
    return None


def _status_ids(urls) -> set[str]:
    return {m.group(1) for u in urls if u for m in [re.search(r"status/(\d+)", u)] if m}


def social_topics(ticker: str, name: str, days: int = 3) -> dict:
    """Fetch heated topics for one ticker. Tweets get verified: True/False."""
    prompt = PROMPT.format(name=name, ticker=ticker, style=STYLE, schema=SCHEMA)
    opts = XSearchOptions(from_date=(dt.date.today() - dt.timedelta(days=days)).isoformat())
    result = search_x(prompt, model=DEFAULT_MODEL, options=opts, timeout=600)
    parsed = parse_json_loose(result.text)
    if not isinstance(parsed, dict) or "topics" not in parsed:
        raise ValueError(f"unparseable response: {result.text[:300]}")

    # Verify each tweet against API citations (annotations use x.com/i/status/ID;
    # the model writes x.com/handle/status/ID — join on the numeric status ID).
    cited = _status_ids(result.citations)
    for topic in parsed["topics"]:
        for tw in topic.get("tweets", []):
            ids = _status_ids([tw.get("url")])
            tw["verified"] = bool(ids) and ids <= cited
    parsed["usage"] = {
        "usd": round((result.usage.get("cost_in_usd_ticks") or 0) / 1e10, 4),
        "x_searches": (result.usage.get("server_side_tool_usage_details") or {}).get("x_search_calls"),
    }
    return parsed


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Heated social topics for a ticker via Grok X Search")
    p.add_argument("ticker")
    p.add_argument("--name", default=None, help="Company name (default: the ticker)")
    p.add_argument("--days", type=int, default=3, help="Look back this many days (default 3)")
    args = p.parse_args(argv)
    data = social_topics(args.ticker.upper(), args.name or args.ticker.upper(), args.days)
    json.dump(data, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

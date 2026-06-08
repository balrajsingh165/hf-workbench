"""Parse and verify Grok-generated social topics."""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any

from src.social.grok import DEFAULT_MODEL, GrokResult, XSearchOptions, search_x

# Settled V7 prompt/style/schema from spikes/grok-social/social_topics.py
# (FINDINGS.md V0-V7 lineage), with the two production edits from
# docs/design-social-ingestion.md § Generation: 3-6 tweets per topic (headroom
# for the >=3-survivors gate) and no topic-kind field.
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
Rules: 2-5 topics, ordered by heat desc. 3-6 tweets per topic with REAL post
URLs from your search results."""

PROMPT = """\
You are the data source for the "Social" tab of a thesis-driven trading app.
Users see a timeline of today's heated topics about {name} (${ticker}) — each
topic shows bull/bear angles and the source tweets behind it.

Search X thoroughly for recent discussion of ${ticker}. Identify the heated
topics, then emit them in the schema below.

{style}

{schema}"""

STANCE_VALUES = {"bull", "bear", "neutral"}
STATUS_ID_RE = re.compile(r"/status/(\d+)|/i/status/(\d+)")


@dataclass(slots=True)
class SocialFetchResult:
    ticker: str
    parsed: dict[str, Any]
    admitted: list[dict[str, Any]]
    rejections: list[dict[str, Any]]
    grok: GrokResult

    @property
    def topics_returned(self) -> int:
        topics = self.parsed.get("topics")
        return len(topics) if isinstance(topics, list) else 0

    @property
    def tweets_dropped(self) -> int:
        total = 0
        for topic in self.admitted:
            total += int(topic.get("_tweets_dropped") or 0)
        for rejection in self.rejections:
            total += int(rejection.get("tweets_dropped") or 0)
        return total

    @property
    def usd(self) -> float:
        ticks = self.grok.usage.get("cost_in_usd_ticks") or 0
        try:
            return float(ticks) / 1e10
        except (TypeError, ValueError):
            return 0.0

    @property
    def x_searches(self) -> int:
        details = self.grok.usage.get("server_side_tool_usage_details") or {}
        try:
            return int(details.get("x_search_calls") or 0)
        except (TypeError, ValueError):
            return 0


def build_prompt(ticker: str, name: str) -> str:
    return PROMPT.format(ticker=ticker, name=name, style=STYLE, schema=SCHEMA)


def parse_json_loose(text: str) -> Any:
    """Best-effort parse: strip fences/citation marks and find first JSON value."""
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    cleaned = re.sub(r"\[\[\d+\]\]\(https?://[^)]+\)", "", cleaned)
    start = min(
        (idx for idx in (cleaned.find("{"), cleaned.find("[")) if idx >= 0),
        default=-1,
    )
    if start < 0:
        return None
    for end in range(len(cleaned), start, -1):
        try:
            return json.loads(cleaned[start:end])
        except json.JSONDecodeError:
            continue
    return None


def _status_ids(urls: list[str]) -> set[str]:
    out: set[str] = set()
    for url in urls:
        if not url:
            continue
        for match in STATUS_ID_RE.finditer(str(url)):
            status_id = match.group(1) or match.group(2)
            if status_id:
                out.add(status_id)
    return out


def _reject(topic: Any, reason: str, *, tweets_dropped: int = 0) -> dict[str, Any]:
    title = ""
    if isinstance(topic, dict):
        title = str(topic.get("title") or "")
    return {
        "reason": reason,
        "title": title,
        "tweets_dropped": tweets_dropped,
    }


def _clean_tweet(tweet: dict[str, Any]) -> dict[str, Any] | None:
    handle = str(tweet.get("handle") or "").strip()
    url = str(tweet.get("url") or "").strip()
    stance = str(tweet.get("stance") or "").strip().lower()
    claim = str(tweet.get("claim") or "").strip()
    if not handle or not url or not claim or stance not in STANCE_VALUES:
        return None
    if not handle.startswith("@"):
        handle = f"@{handle}"
    # Normalize engagement to str | None here, at the untrusted-JSON boundary,
    # so every reader downstream gets a sound type. The model sometimes emits
    # the literal string "null" instead of JSON null (seen in production).
    engagement = tweet.get("engagement")
    engagement = str(engagement).strip() if engagement is not None else None
    if engagement and engagement.lower() in {"null", "none", "n/a"}:
        engagement = None
    return {
        "handle": handle,
        "url": url,
        "stance": stance,
        "claim": claim,
        "engagement": engagement or None,
    }


def verify_topics(
    parsed: Any,
    citations: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Structural admission gate: shape, enums, citation-verified tweets.

    Voice/style is the prompt's job and is reviewed via e2e quality runs,
    not rule-based text checks.
    """
    if not isinstance(parsed, dict):
        return [], [_reject(parsed, "parse_not_object")]
    raw_topics = parsed.get("topics")
    if not isinstance(raw_topics, list):
        return [], [_reject(parsed, "topics_not_list")]

    cited_status_ids = _status_ids(citations)
    admitted: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict):
            rejections.append(_reject(raw_topic, "topic_not_object"))
            continue
        title = str(raw_topic.get("title") or "").strip()
        summary = str(raw_topic.get("summary") or "").strip()
        bull_angle = str(raw_topic.get("bull_angle") or "").strip()
        bear_angle = str(raw_topic.get("bear_angle") or "").strip()
        try:
            heat = int(raw_topic.get("heat"))
        except (TypeError, ValueError):
            heat = 0
        if not title or not summary or not bull_angle or not bear_angle:
            rejections.append(_reject(raw_topic, "missing_required_fields"))
            continue
        if heat < 1 or heat > 5:
            rejections.append(_reject(raw_topic, "invalid_heat"))
            continue

        clean_tweets: list[dict[str, Any]] = []
        tweets_dropped = 0
        raw_tweets = raw_topic.get("tweets")
        if not isinstance(raw_tweets, list):
            rejections.append(_reject(raw_topic, "tweets_not_list"))
            continue
        for raw_tweet in raw_tweets:
            if not isinstance(raw_tweet, dict):
                tweets_dropped += 1
                continue
            tweet = _clean_tweet(raw_tweet)
            if tweet is None:
                tweets_dropped += 1
                continue
            ids = _status_ids([tweet["url"]])
            if not ids or not ids <= cited_status_ids:
                tweets_dropped += 1
                continue
            clean_tweets.append(tweet)

        if len(clean_tweets) < 3:
            rejections.append(
                _reject(
                    raw_topic,
                    "too_few_verified_tweets",
                    tweets_dropped=tweets_dropped,
                )
            )
            continue

        admitted.append({
            "title": title,
            "heat": heat,
            "summary": summary,
            "bull_angle": bull_angle,
            "bear_angle": bear_angle,
            "tweets": clean_tweets[:6],
            "_tweets_dropped": tweets_dropped,
        })

    return admitted, rejections


def fetch_social_topics(
    ticker: str,
    name: str,
    *,
    lookback_days: int = 2,
    timeout: float = 600.0,
) -> SocialFetchResult:
    prompt = build_prompt(ticker, name)
    from_date = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    grok = search_x(
        prompt,
        model=DEFAULT_MODEL,
        options=XSearchOptions(from_date=from_date),
        timeout=timeout,
    )
    parsed = parse_json_loose(grok.text)
    if not isinstance(parsed, dict):
        raise ValueError(f"unparseable social response for {ticker}: {grok.text[:300]}")
    admitted, rejections = verify_topics(parsed, grok.citations)
    return SocialFetchResult(
        ticker=ticker,
        parsed=parsed,
        admitted=admitted,
        rejections=rejections,
        grok=grok,
    )


__all__ = [
    "PROMPT",
    "SCHEMA",
    "STYLE",
    "SocialFetchResult",
    "fetch_social_topics",
    "parse_json_loose",
    "verify_topics",
]

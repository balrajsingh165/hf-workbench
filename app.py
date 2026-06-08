"""FastAPI server for hf-workbench.

Run (API only — story pipeline is a separate process; see ecosystem.config.cjs):
    uv run uvicorn app:app --host 0.0.0.0 --port 8088

Endpoints (frontend contract — see hf-frontend docs/AGENT-ARCHITECTURE.md):
    GET /api/home?user_id=user_1
    GET /api/thesis/{thesis_id}/evidence?direction=&days_back=
    GET /api/thesis/{thesis_id}/related
    GET /api/market/{ticker}?kind=&days_back=
    GET /api/macro?series_keys=&views=&limit=

Migrated from stdlib BaseHTTPRequestHandler in api.py. /api/home reuses the
existing build_home() helper for shape parity.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.format import format_count_short, format_usd_short
from src.sec_filings import primary_document_url_fetchable

# Must run before any module reads AWS_PROFILE / BEDROCK_PROFILE / etc.
# Strands' BedrockModel resolves credentials at agent construction, so the
# vars need to be in os.environ by the time the first chat request lands.
load_dotenv(Path(__file__).resolve().parent / ".env")

from api import (
    DB_PATH,
    THESES_DIR,
    build_home,
    build_story_suggestions,
    cited_news_ids,
    db,
    load_story_md,
    load_thesis_md,
)
from src.news.persist import quote_speaker_fallback
from src.agent.observability import (
    initialize_runtime_observability,
    setup_strands_telemetry,
)
from src.interfaces.ai_sdk_compat.api import router as ai_sdk_router
from src.interfaces.chat.api import router as chat_router
from src.interfaces.payments.api import router as payments_router
from src.interfaces.prices.api import router as prices_router
from src.i18n import (
    localized_markdown_path,
    markdown_path_language,
    normalize_language,
)
from src.story.docs import STORY_TITLE_RE
from scripts.apply_news_rearchitecture_schema import apply as apply_news_schema
from src.thesis.scoring import prescription_for

ROOT = Path(__file__).resolve().parent

initialize_runtime_observability()
setup_strands_telemetry()

app = FastAPI(title="hf-workbench API", version="0.2.0")
# GZip JSON responses ≥1 KiB. /api/home is the big one (~245 KB raw → ~14 KB
# gzipped after also trimming the card projection). Compression runs in the
# starlette middleware stack so SSE streams (which yield small chunks) aren't
# meaningfully affected.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(ai_sdk_router)
app.include_router(chat_router)
app.include_router(prices_router)
app.include_router(payments_router)

from src.api.metrics_router import router as metrics_router

app.include_router(metrics_router)


@app.on_event("startup")
def ensure_runtime_schema() -> None:
    apply_news_schema(DB_PATH)


# ── /api/home (parity with stdlib api.py) ────────────────────────────────
#
# Models below shape the build_home() dict for OpenAPI emission. Every model
# sets ``extra="allow"`` so any field present in the dict that we have not
# explicitly enumerated still passes through — defensive against legacy
# hf-ui consumers that depend on fields the new frontend doesn't use.


class BriefDiscoverThesis(BaseModel):
    """Item in ``brief.theses[]`` — discoverable theses, not the user's own."""
    model_config = ConfigDict(extra="allow")
    id: str
    title: str
    belief: str
    signal: str = "medium"
    signals: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    reasoning: str = ""
    tickers: list[str] = Field(default_factory=list)
    trackedThesisId: str


class TrendingTicker(BaseModel):
    """Row in the latest trending-ticker snapshot (``GET /api/trending``)."""
    symbol: str
    name: str
    rank: int
    effective_rank: int
    tier: int
    mentions_24h: int | None = None
    mentions_delta: int | None = None
    upvotes: int | None = None
    in_registry: bool
    snapshot_date: str


class BriefDay(BaseModel):
    """Top-of-feed daily brief: themes plus discoverable theses."""
    model_config = ConfigDict(extra="allow")
    date: str = ""
    themes: list[str] = Field(default_factory=list)
    theses: list[BriefDiscoverThesis] = Field(default_factory=list)
    language: str = "en"


class HomeThesisEvent(BaseModel):
    """``theses[].events[]`` — supports both news and price events; ``delta``
    can be null for price events. Distinct from ``ThesisEvent`` (used by
    /api/thesis/{id}), which has a Literal-restricted type and no delta."""
    model_config = ConfigDict(extra="allow")
    date: str
    title: str
    delta: int | None = None
    type: str
    # Present for news-sourced events (links to /feed/[id]); absent for
    # synthetic price events that don't correspond to a story.
    story_id: str | None = None


class HomeThesis(BaseModel):
    """User-tracked thesis as emitted by ``build_user_theses`` for the home
    payload. Distinct from ``ThesisDetail`` (used by /api/thesis/{id}), which
    is snake_case and exposes scoring sub-dimensions."""
    model_config = ConfigDict(extra="allow")
    id: str
    title: str
    belief: str
    shortBelief: str = ""
    support: int = 0
    prevSupport: int = 0
    trend: str
    status: str
    tickers: list[str] = Field(default_factory=list)
    created: str = ""
    updated: str = ""
    supportHistory: list[Any] = Field(default_factory=list)
    events: list[HomeThesisEvent] = Field(default_factory=list)
    evidence: list[Any] = Field(default_factory=list)


class FeedNewsOverviewBullet(BaseModel):
    """Single bullet inside ``news[].overview[]``."""
    model_config = ConfigDict(extra="allow")
    text: str = ""
    sourceDocIds: list[str] = Field(default_factory=list)


class FeedTweet(BaseModel):
    model_config = ConfigDict(extra="allow")
    handle: str = ""
    url: str = ""
    stance: Literal["bull", "bear", "neutral"] = "neutral"
    claim: str = ""
    engagement: str | None = None


class FeedNewsThumbnailVariant(BaseModel):
    """One R2-hosted small thumbnail in a single format (avif/webp/jpeg)."""
    model_config = ConfigDict(extra="allow")
    mime: str
    url: str


class FeedNewsThumbnail(BaseModel):
    """Small R2-hosted thumbnail set for home feed cards.

    Carries one URL per format so the card can render `<picture>` with a
    `<source type=...>` per format and a JPEG `<img src>` fallback. The
    variants list is ordered avif → webp → jpeg; the JPEG entry is guaranteed
    present (the backend skips images without one).
    """
    model_config = ConfigDict(extra="allow")
    width: int | None = None
    height: int | None = None
    variants: list[FeedNewsThumbnailVariant] = Field(default_factory=list)


class FeedNewsSuggestion(BaseModel):
    """A global, read-only thesis proposal surfaced on a story's detail page.

    Active system theses linked to the story as strong supports — not bound to
    any user. See docs/design-thesis-creation.md (§Surfacing: Story Proposals)."""
    model_config = ConfigDict(extra="allow")
    thesisId: str
    belief: str = ""
    angleLabel: str | None = None
    tickers: list[str] = Field(default_factory=list)
    direction: str = "bullish"
    horizon: int | None = None


class FeedNewsItem(BaseModel):
    """Story rendered in the home feed (camelCase, distinct from the
    snake_case ``StoryListItem`` exposed by /api/stories)."""
    model_config = ConfigDict(extra="allow")
    id: str
    kind: Literal["story", "x"] = "story"
    publishedAt: str = ""
    headline: str
    sources: list[str] = Field(default_factory=list)
    summary: str = ""
    thumbnail: FeedNewsThumbnail | None = None
    tickers: list[str] = Field(default_factory=list)
    matches: list[Any] = Field(default_factory=list)
    suggestions: list[FeedNewsSuggestion] = Field(default_factory=list)
    eventClass: str = ""
    sectors: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    independentPublishers: int = 0
    overview: list[FeedNewsOverviewBullet] = Field(default_factory=list)
    heat: int | None = None
    bullAngle: str | None = None
    bearAngle: str | None = None
    tweets: list[FeedTweet] = Field(default_factory=list)
    explain: dict[str, Any] | None = None


class FeedSourceMeta(BaseModel):
    """Source-color metadata keyed by slug under ``sources``."""
    model_config = ConfigDict(extra="allow")
    name: str
    color: str
    domain: str = ""


class HomeResponse(BaseModel):
    """/api/home response. Aggregates the daily brief, tracked theses,
    feed stories, and per-source color metadata."""
    model_config = ConfigDict(extra="allow")
    brief: BriefDay
    sources: dict[str, FeedSourceMeta] = Field(default_factory=dict)
    news: list[FeedNewsItem] = Field(default_factory=list)
    theses: list[HomeThesis] = Field(default_factory=list)
    trackedThesisIds: list[str] = Field(default_factory=list)
    language: str = "en"


@app.get("/api/home", response_model=HomeResponse)
def get_home(
    user_id: str = Query("user_1"),
    explain: bool = Query(False),
    locale: str | None = Query(None),
) -> HomeResponse:
    try:
        payload = build_home(
            user_id,
            explain=explain,
            language=normalize_language(locale),
        )
        return HomeResponse(**payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── /api/news/{id} (story body + source URLs) ───────────────────────────
#
# The path keeps the `/api/news/` prefix as the public-facing read route
# (frontend stability), but only resolves story IDs. There is no legacy
# raw-news fallback — stories are the unit of the home feed.


class NewsSourceLink(BaseModel):
    name: str
    url: str
    publisher: str | None = None


class NewsImageVariant(BaseModel):
    size: str
    url: str
    key: str
    width: int
    height: int
    mime: str
    sizeBytes: int


class NewsImage(BaseModel):
    sourceUrl: str | None = None
    variants: list[NewsImageVariant] = Field(default_factory=list)


class NewsQuote(BaseModel):
    text: str
    attribution: str | None = None


class NewsDetail(BaseModel):
    """Structured story-detail payload. The page renders designed sections from
    these fields — the storage markdown is never sent to the client."""

    id: str
    kind: Literal["story", "x"] = "story"
    language: str = "en"
    headline: str
    published_at: str | None = None
    overview: list[str] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)
    # kind='story' only
    quotes: list[NewsQuote] = Field(default_factory=list)
    sources: list[NewsSourceLink] = Field(default_factory=list)
    images: list[NewsImage] = Field(default_factory=list)
    # kind='x' only
    heat: int | None = None
    bullAngle: str | None = None
    bearAngle: str | None = None
    tweets: list[FeedTweet] = Field(default_factory=list)
    # Global thesis proposals linked to this story (both kinds; empty for x
    # until social thesis-matching is enabled).
    suggestions: list[FeedNewsSuggestion] = Field(default_factory=list)


def _json_dicts(raw: str | None) -> list[dict]:
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _parse_image_refs(raw: str | None) -> list[NewsImage]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    out: list[NewsImage] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            out.append(NewsImage.model_validate(item))
        except Exception:
            continue
    return out


class StoryListItem(BaseModel):
    id: str
    language: str = "en"
    headline: str
    created_at: str
    event_class: str | None = None
    independent_publishers: int = 0
    tickers: list[str] = Field(default_factory=list)
    sectors: list[str]
    regions: list[str]
    summary: str


@app.get("/api/news/{news_id}", response_model=NewsDetail)
def get_news_detail(
    news_id: str,
    locale: str | None = Query(None),
) -> NewsDetail:
    if not news_id.startswith("story_"):
        raise HTTPException(status_code=404, detail=f"Not found: {news_id}")
    requested_language = normalize_language(locale)
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, cluster_id, created_at, headline,
                   overview_json, claims_json, quotes_json, images_json,
                   COALESCE(kind, 'story') AS kind, heat, social_json
            FROM story
            WHERE id = ?
              AND (
                kind = 'x'
                OR id NOT IN (
                SELECT story_id FROM story_quality_label
                WHERE label IN ('unclear', 'no_value')
                )
              )
            """,
            (news_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Story {news_id} not found")
        kind = row["kind"] or "story"
        story_doc = load_story_md(news_id, requested_language) if kind == "story" else None
        tickers = [
            r["symbol"]
            for r in conn.execute(
                "SELECT symbol FROM entity_tickers"
                " WHERE entity_type='story' AND entity_id=? ORDER BY symbol",
                (news_id,),
            ).fetchall()
        ]
        overview = story_doc.overview_bullets if story_doc else [
            text
            for item in _json_dicts(row["overview_json"])
            if (text := str(item.get("text") or "").strip())
        ]

        quotes: list[NewsQuote] = []
        sources: list[NewsSourceLink] = []
        social: dict = {}
        if kind == "x":
            try:
                social = json.loads(row["social_json"] or "{}")
            except json.JSONDecodeError:
                social = {}
        else:
            cited_ids = cited_news_ids(
                row["overview_json"], row["claims_json"], row["quotes_json"]
            )
            cited_rows = []
            if cited_ids:
                placeholders = ",".join("?" * len(cited_ids))
                cited_rows = conn.execute(
                    f"""
                    SELECT id, headline, source_url, publisher
                    FROM news
                    WHERE id IN ({placeholders})
                      AND publisher IS NOT NULL AND publisher <> ''
                    ORDER BY COALESCE(published_at, '') DESC, id
                    """,
                    sorted(cited_ids),
                ).fetchall()
            publisher_by_news_id = {r["id"]: r["publisher"] for r in cited_rows}
            # One source per publisher (newest cited article), name = article
            # headline so the page can render a real source list, not just chips.
            seen_publishers: set[str] = set()
            for r in cited_rows:
                if r["publisher"] in seen_publishers or not r["source_url"]:
                    continue
                seen_publishers.add(r["publisher"])
                sources.append(
                    NewsSourceLink(
                        name=r["headline"] or r["publisher"],
                        url=r["source_url"],
                        publisher=r["publisher"],
                    )
                )
            for quote in _json_dicts(row["quotes_json"]):
                text = str(quote.get("text") or "").strip()
                if not text:
                    continue
                quotes.append(
                    NewsQuote(
                        text=text,
                        attribution=quote_speaker_fallback(quote, publisher_by_news_id)
                        or None,
                    )
                )
        suggestions = [
            FeedNewsSuggestion(**s)
            for s in build_story_suggestions(conn, news_id, requested_language)
        ]
    return NewsDetail(
        id=news_id,
        kind=kind,
        language=markdown_path_language(story_doc.path) if story_doc else "en",
        headline=story_doc.title if story_doc else row["headline"] or news_id,
        published_at=row["created_at"] or None,
        overview=overview,
        tickers=tickers,
        quotes=quotes,
        sources=sources,
        images=[] if kind == "x" else _parse_image_refs(row["images_json"]),
        heat=int(row["heat"] or 0) if kind == "x" else None,
        bullAngle=str(social.get("bull_angle") or "") if kind == "x" else None,
        bearAngle=str(social.get("bear_angle") or "") if kind == "x" else None,
        tweets=social.get("tweets") if isinstance(social.get("tweets"), list) else [],
        suggestions=suggestions,
    )


@app.get("/api/stories", response_model=list[StoryListItem])
def list_stories(
    sector: str | None = Query(None),
    region: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    locale: str | None = Query(None),
) -> list[StoryListItem]:
    requested_language = normalize_language(locale)
    where: list[str] = []
    params: list[object] = []
    if sector:
        where.append("s.sectors_json LIKE ?")
        params.append(f"%{sector}%")
    if region:
        where.append("s.regions_json LIKE ?")
        params.append(f"%{region}%")
    where.append(
        "s.id NOT IN ("
        "SELECT story_id FROM story_quality_label "
        "WHERE label IN ('unclear', 'no_value')"
        ")"
    )
    clause = "WHERE " + " AND ".join(where)
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT s.id, s.created_at, s.headline, s.overview_json,
                   s.sectors_json, s.regions_json,
                   c.event_class, c.independent_pub_count
            FROM story s
            JOIN news_cluster c ON c.id = s.cluster_id
            {clause}
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        story_ids = [row["id"] for row in rows]
        ticker_map: dict[str, list[str]] = {story_id: [] for story_id in story_ids}
        if story_ids:
            placeholders = ",".join("?" * len(story_ids))
            for ticker_row in conn.execute(
                f"""
                SELECT et.entity_id, et.symbol
                FROM entity_tickers et
                JOIN instruments i ON i.symbol = et.symbol
                WHERE et.entity_type='story'
                  AND et.entity_id IN ({placeholders})
                ORDER BY et.symbol
                """,
                story_ids,
            ).fetchall():
                ticker_map.setdefault(ticker_row["entity_id"], []).append(ticker_row["symbol"])
    out: list[StoryListItem] = []
    for row in rows:
        try:
            overview = json.loads(row["overview_json"] or "[]")
        except json.JSONDecodeError:
            overview = []
        try:
            sectors = json.loads(row["sectors_json"] or "[]")
            regions = json.loads(row["regions_json"] or "[]")
        except json.JSONDecodeError:
            sectors, regions = [], []
        story_doc = load_story_md(row["id"], requested_language)
        summary = " ".join(
            story_doc.overview_bullets[:3]
            if story_doc
            else [
                str(item.get("text") or "").strip()
                for item in overview[:3]
                if isinstance(item, dict)
            ]
        )
        out.append(StoryListItem(
            id=row["id"],
            language=markdown_path_language(story_doc.path) if story_doc else "en",
            headline=story_doc.title if story_doc else row["headline"],
            created_at=row["created_at"] or "",
            event_class=row["event_class"] or None,
            independent_publishers=int(row["independent_pub_count"] or 0),
            tickers=ticker_map.get(row["id"], []),
            sectors=[str(s) for s in sectors],
            regions=[str(r) for r in regions],
            summary=summary,
        ))
    return out


@app.get("/api/trending", response_model=list[TrendingTicker])
def list_trending(
    tier: int | None = Query(None, ge=1, le=2, description="Filter to one tier."),
    limit: int = Query(60, ge=1, le=200),
) -> list[TrendingTicker]:
    """Latest trending-ticker snapshot (social leaderboard) the retrieval lane
    runs on. Ordered by today's rank; each row carries its effective_rank
    (one-day residue) and the tier that residue selects. Empty until the
    trending lane has written its first snapshot."""
    from agents.trending import latest_trends

    with db() as conn:
        rows = latest_trends(conn, limit=limit)
    if tier is not None:
        rows = [r for r in rows if r["tier"] == tier]
    return [TrendingTicker(**r) for r in rows]


# ── /api/thesis/{id} (single-thesis detail) ──────────────────────────────

class ThesisEvent(BaseModel):
    date: str
    title: str
    type: Literal["confirming", "challenging", "neutral"]


class ThesisDetail(BaseModel):
    id: str
    language: str = "en"
    title: str
    belief: str
    short_belief: str
    tickers: list[str]
    support: int
    prev_support: int
    score_freshness: int | None
    score_tailwind: int | None
    trend: str
    status: str
    created: str
    events: list[ThesisEvent]
    prescription_line: str | None


@app.get("/api/thesis/{thesis_id}", response_model=ThesisDetail)
def get_thesis(
    thesis_id: str,
    user_id: str = Query("user_1"),
    locale: str | None = Query(None),
) -> ThesisDetail:
    """Lightweight thesis detail. Replaces the frontend's pattern of pulling
    the entire /api/home payload (188KB) just to .find() one thesis."""
    from api import build_thesis_events, first_sentence, trend_for

    doc = load_thesis_md(thesis_id, normalize_language(locale))
    if not doc:
        raise HTTPException(status_code=404, detail=f"Thesis {thesis_id} not found")

    with db() as conn:
        row = conn.execute(
            """
            SELECT ut.status, t.score, t.score_freshness, t.score_tailwind,
                   ut.created_at,
                   (SELECT score FROM thesis_snapshots s
                     WHERE s.thesis_id = ut.thesis_id
                       AND s.snapshot_date < date('now')
                     ORDER BY s.snapshot_date DESC LIMIT 1) AS prev_score
            FROM user_theses ut
            JOIN theses t ON t.id = ut.thesis_id
            WHERE ut.user_id = ? AND ut.thesis_id = ?
            """,
            (user_id, thesis_id),
        ).fetchone()
        events_raw = build_thesis_events(conn, thesis_id, user_id, limit=5)

    score = (row["score"] if row and row["score"] is not None else 0)
    prev = (row["prev_score"] if row and row["prev_score"] is not None else score)
    freshness = row["score_freshness"] if row else None
    tailwind = row["score_tailwind"] if row else None
    status = row["status"] if row else "active"
    created = (row["created_at"] or "")[:10] if row else ""
    most_recent_relation = (
        ("supports" if events_raw[0]["type"] == "confirming" else "stresses")
        if events_raw else None
    )

    return ThesisDetail(
        id=thesis_id,
        language=markdown_path_language(doc.path),
        title=doc.title,
        belief=doc.core_thesis,
        short_belief=first_sentence(doc.core_thesis),
        tickers=doc.tickers,
        support=int(score),
        prev_support=int(prev),
        score_freshness=int(freshness) if freshness is not None else None,
        score_tailwind=int(tailwind) if tailwind is not None else None,
        trend=trend_for(score, prev, status),
        status=status,
        created=created,
        events=[
            ThesisEvent(date=e["date"], title=e["title"], type=e["type"])
            for e in events_raw
        ],
        prescription_line=prescription_for(int(score), status, most_recent_relation),
    )


# ── /api/thesis/{id}/evidence (Counterpoints) ────────────────────────────

_RATIONALE_MAX_CHARS = 140


class EvidenceItem(BaseModel):
    story_id: str
    headline: str
    created_at: str | None = None
    relation: Literal["supports", "stresses"]
    confidence: float
    rationale: str


def _trim_rationale(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _RATIONALE_MAX_CHARS:
        return text
    return text[: _RATIONALE_MAX_CHARS - 1].rstrip() + "…"


class EvidenceSummary(BaseModel):
    supports: int = 0
    stresses: int = 0
    neutral: int = 0


class TickerDirection(BaseModel):
    symbol: str
    direction: Literal["bullish", "bearish"]


_INVALIDATION_FRAMING = (
    "Forward-looking trigger conditions named by the thesis author. None has "
    "fired unless a tool result this turn returned the matching specific "
    "number or date. To claim any has triggered, ground the claim in a "
    "`search_macro` / `search_evidence` / `web_search` result whose own text "
    "contains the value. Quoting a value from the conditions list as if it "
    "has happened — without that grounding — is fabrication."
)


class InvalidationWatchList(BaseModel):
    # Static kill-switch criteria authored on the thesis. The `framing` field
    # is restated in every response so the agent reads `conditions` as
    # hypothetical triggers, not as evidence rows — field-name-only signals
    # ("triggers", "watch_list") were not enough to keep the model from
    # quoting an entry as a fired event.
    framing: str = _INVALIDATION_FRAMING
    conditions: list[str] = Field(default_factory=list)


class EvidenceResponse(BaseModel):
    evidence: list[EvidenceItem]
    total_links: int = 0
    returned: int = 0
    summary: EvidenceSummary = Field(default_factory=EvidenceSummary)
    invalidation_watch_list: InvalidationWatchList = Field(
        default_factory=InvalidationWatchList
    )
    # Per-ticker direction (bullish/bearish) as authored on the thesis md. A
    # ticker absent from this list either has no direction specified or was
    # tagged neutral (which the parser drops). Surfaced so the agent can tell
    # which leg is a hedge vs. the main position when sizing or hedging advice
    # is requested.
    ticker_directions: list[TickerDirection] = Field(default_factory=list)
    note: str | None = None


@app.get("/api/thesis/{thesis_id}/evidence", response_model=EvidenceResponse)
def get_thesis_evidence(
    thesis_id: str,
    direction: Literal["supports", "stresses"] | None = None,
    days_back: int | None = Query(None, ge=1, le=365),
) -> EvidenceResponse:
    cutoff_date = (
        (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
        if days_back is not None
        else None
    )

    sql = [
        "SELECT s.id, l.story_id, l.relation, l.confidence, l.rationale,",
        "       s.created_at, s.headline",
        "FROM thesis_story_links l",
        "JOIN story s ON s.id = l.story_id",
        "WHERE l.thesis_id = ?",
    ]
    params: list[Any] = [thesis_id]
    if direction:
        sql.append("AND l.relation = ?")
        params.append(direction)
    sql.append("ORDER BY s.created_at DESC, l.confidence DESC")

    with db() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()
        post_filter_rows = rows
        if cutoff_date is not None:
            filtered = []
            for r in rows:
                raw = (r["created_at"] or "").strip()
                try:
                    row_date = datetime.strptime(raw[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if row_date >= cutoff_date:
                    filtered.append(r)
            post_filter_rows = filtered

        total_links_row = conn.execute(
            "SELECT relation, COUNT(*) AS cnt FROM thesis_story_links "
            "WHERE thesis_id = ? GROUP BY relation",
            (thesis_id,),
        ).fetchall()
        summary = EvidenceSummary()
        for r in total_links_row:
            rel = r["relation"]
            cnt = int(r["cnt"])
            if rel == "supports":
                summary.supports = cnt
            elif rel == "stresses":
                summary.stresses = cnt
            elif rel == "neutral":
                summary.neutral = cnt
        total_links = summary.supports + summary.stresses + summary.neutral

        items: list[EvidenceItem] = []
        for r in post_filter_rows:
            title = r["headline"]
            if not title:
                continue
            items.append(
                EvidenceItem(
                    story_id=r["story_id"],
                    headline=title,
                    created_at=(r["created_at"] or None),
                    relation=r["relation"],
                    confidence=float(r["confidence"]),
                    rationale=_trim_rationale(r["rationale"] or ""),
                )
            )

        thesis_doc = load_thesis_md(thesis_id)
        if thesis_doc is not None:
            watch_list = InvalidationWatchList(
                conditions=list(thesis_doc.invalidations),
            )
            ticker_directions = [
                TickerDirection(symbol=sym, direction=direction)
                for sym, direction in thesis_doc.ticker_directions
            ]
        else:
            watch_list = InvalidationWatchList()
            ticker_directions = []

        if items:
            return EvidenceResponse(
                evidence=items,
                total_links=total_links,
                returned=len(items),
                summary=summary,
                invalidation_watch_list=watch_list,
                ticker_directions=ticker_directions,
            )

        # Empty result — explain why so the agent can recover instead of
        # silently giving up. Distinguishes: thesis missing, thesis has no
        # links at all, direction filter excluded everything, or days_back
        # cutoff excluded everything.
        note = _diagnose_empty_evidence(
            conn,
            thesis_id=thesis_id,
            direction=direction,
            days_back=days_back,
            had_pre_filter_rows=bool(rows),
            had_post_cutoff_rows=bool(post_filter_rows),
        )

    return EvidenceResponse(
        evidence=[],
        total_links=total_links,
        returned=0,
        summary=summary,
        invalidation_watch_list=watch_list,
        ticker_directions=ticker_directions,
        note=note,
    )


def _diagnose_empty_evidence(
    conn,
    *,
    thesis_id: str,
    direction: str | None,
    days_back: int | None,
    had_pre_filter_rows: bool,
    had_post_cutoff_rows: bool,
) -> str:
    """One-sentence diagnostic leading with 'no <direction> evidence found' when
    a direction filter was set. Front-loads the bottom line so an LLM reading
    the note cannot miss it, then appends a short reason."""
    exists = conn.execute(
        "SELECT 1 FROM theses WHERE id = ?", (thesis_id,)
    ).fetchone()
    if not exists:
        return f"no evidence found: thesis_id '{thesis_id}' does not exist"

    total = conn.execute(
        "SELECT COUNT(*) FROM thesis_story_links WHERE thesis_id = ?",
        (thesis_id,),
    ).fetchone()[0]
    lead = f"no {direction} evidence found" if direction else "no evidence found"

    if total == 0:
        return f"{lead} for thesis '{thesis_id}' (0 linked stories of any kind)"

    if direction and not had_pre_filter_rows:
        counts_row = conn.execute(
            "SELECT relation, COUNT(*) AS cnt FROM thesis_story_links "
            "WHERE thesis_id = ? GROUP BY relation",
            (thesis_id,),
        ).fetchall()
        counts = ", ".join(f"{int(r['cnt'])} {r['relation']}" for r in counts_row)
        return (
            f"{lead} for thesis '{thesis_id}' "
            f"({total} linked stories on file: {counts})"
        )

    if days_back is not None and had_pre_filter_rows and not had_post_cutoff_rows:
        if direction:
            matching = conn.execute(
                "SELECT COUNT(*) FROM thesis_story_links "
                "WHERE thesis_id = ? AND relation = ?",
                (thesis_id, direction),
            ).fetchone()[0]
            scope = f"{matching} {direction}"
        else:
            scope = f"{total} total"
        return (
            f"{lead} for thesis '{thesis_id}' within days_back={days_back} "
            f"({scope} linked stories exist but all are older); widen the window or drop the filter"
        )

    # Rows existed after filtering but every one was missing a headline.
    return (
        f"{lead} for thesis '{thesis_id}': matching links exist but all "
        "articles are missing a headline"
    )


# ── /api/thesis/{id}/related (Compare Related Theses) ────────────────────

class RelatedThesis(BaseModel):
    thesis_id: str
    title: str
    similarity: float
    score: float
    state: str  # active | stressed | resolved
    # Heuristic: which source chunk type matched best. This does not claim
    # semantic agreement/disagreement with the other thesis.
    relation: Literal["related_to_statement", "related_to_invalidation"]


class RelatedResponse(BaseModel):
    related: list[RelatedThesis]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@app.get("/api/thesis/{thesis_id}/related", response_model=RelatedResponse)
def get_thesis_related(
    thesis_id: str,
    top_k: int = Query(8, ge=1, le=25),
    user_id: str = Query("user_1"),
) -> RelatedResponse:
    """Cosine similarity over thesis_match_chunks.

    Uses the source thesis's own pre-computed embeddings (no Gemini call).
    Picks the best-scoring chunk per other thesis, then ranks.
    """
    with db() as conn:
        chunks = conn.execute(
            "SELECT thesis_id, chunk_key, embedding_json FROM thesis_match_chunks"
        ).fetchall()
        scores_rows = conn.execute(
            """
            SELECT ut.thesis_id AS thesis_id, t.score AS score, ut.status AS status
            FROM user_theses ut
            JOIN theses t ON t.id = ut.thesis_id
            WHERE ut.user_id = ?
            """,
            (user_id,),
        ).fetchall()

    if not chunks:
        return RelatedResponse(related=[])

    # Keep (chunk_key, embedding) so we can tell whether a match traces back
    # to the source's statement or one of its invalidation conditions.
    source_chunks: list[tuple[str, list[float]]] = [
        (c["chunk_key"], json.loads(c["embedding_json"]))
        for c in chunks
        if c["thesis_id"] == thesis_id
    ]
    if not source_chunks:
        raise HTTPException(
            status_code=404,
            detail=f"No embeddings indexed for {thesis_id}",
        )

    score_lookup = {r["thesis_id"]: (r["score"], r["status"]) for r in scores_rows}

    # Per other thesis: best similarity, and the source chunk_key that produced it.
    best_per_thesis: dict[str, tuple[float, str]] = {}
    for c in chunks:
        if c["thesis_id"] == thesis_id:
            continue
        if c["thesis_id"] not in score_lookup:
            continue  # restrict to theses the active user owns
        emb = json.loads(c["embedding_json"])
        best_sim = -1.0
        best_src_key = "statement"
        for src_key, src_emb in source_chunks:
            sim = _cosine(src_emb, emb)
            if sim > best_sim:
                best_sim = sim
                best_src_key = src_key
        prev = best_per_thesis.get(c["thesis_id"])
        if prev is None or best_sim > prev[0]:
            best_per_thesis[c["thesis_id"]] = (best_sim, best_src_key)

    ranked = sorted(
        best_per_thesis.items(), key=lambda kv: kv[1][0], reverse=True
    )[:top_k]

    out: list[RelatedThesis] = []
    for tid, (sim, src_key) in ranked:
        doc = load_thesis_md(tid)
        if not doc:
            continue
        score, status = score_lookup[tid]
        relation = (
            "related_to_invalidation"
            if src_key.startswith("invalidation_")
            else "related_to_statement"
        )
        out.append(
            RelatedThesis(
                thesis_id=tid,
                title=doc.title,
                similarity=round(sim, 4),
                score=float(score) if score is not None else 0.0,
                state=status or "active",
                relation=relation,
            )
        )
    return RelatedResponse(related=out)


# ── /api/market/{ticker} (Watch Plan + Stress Test) ──────────────────────

class MarketResponse(BaseModel):
    ticker: str
    kind: Literal["price", "options", "insider", "filings"]
    snapshot: dict[str, Any]
    asof: str
    note: str | None = None


def _utc_now_seconds() -> str:
    """Second-precision UTC timestamp for tool `asof` fields.

    Microseconds add 7 chars per call and never matter at the analyst horizon.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_mesh_call(callable_, *args, **kwargs) -> dict[str, Any]:
    """Invoke a mesh helper, swallowing API errors into a structured note.

    The frontend's mock fallback is fine for missing fields; we return a
    successful 200 with `{note: ...}` so the agent sees real shape but knows
    the data is empty.
    """
    try:
        from src.clients.mesh import unwrap_results

        raw = callable_(*args, **kwargs)
        return unwrap_results(raw) or {}
    except Exception as exc:
        return {"note": f"mesh unavailable: {type(exc).__name__}: {exc}"}


@app.get("/api/market/{ticker}", response_model=MarketResponse)
def get_market(
    ticker: str,
    kind: Literal["price", "options", "insider", "filings"] = Query(...),
    days_back: int | None = Query(None, ge=1, le=365),
) -> MarketResponse:
    from src.clients.mesh import sec_tool, yahoo_price_history, yahoo_tool

    asof = _utc_now_seconds()
    snapshot: dict[str, Any]

    if kind == "price":
        period = _period_for(days_back, default="1mo")
        # Cap at ~1 trading year. The previous min(50, …) silently capped
        # 6mo/1y requests at ~10 weeks of bars, mismatching the period.
        snapshot = _safe_mesh_call(
            yahoo_price_history,
            ticker,
            interval="1d",
            period=period,
            limit_bars=min(252, days_back or 22),
        )
    elif kind == "options":
        snapshot = _safe_mesh_call(yahoo_tool, "options_chain", {"symbol": ticker})
    elif kind == "insider":
        snapshot = _safe_mesh_call(sec_tool, "insider_activity", {"query": ticker})
    elif kind == "filings":
        snapshot = _safe_mesh_call(sec_tool, "filing_timeline", {"query": ticker})
    else:  # pragma: no cover — Literal narrows above
        raise HTTPException(status_code=400, detail=f"Unsupported kind: {kind}")

    note = _diagnose_empty_market(snapshot, ticker=ticker, kind=kind)
    return MarketResponse(
        ticker=ticker.upper(), kind=kind, snapshot=snapshot, asof=asof, note=note
    )


_MARKET_LIST_KEYS: dict[str, tuple[str, ...]] = {
    # `price` is checked via mesh's status / summary fields, not a top-level list.
    "price": (),
    "options": ("expirations", "calls", "puts", "chain"),
    "insider": ("filings", "transactions", "items"),
    "filings": ("filings", "items", "documents"),
}


def _diagnose_empty_market(
    snapshot: dict[str, Any], *, ticker: str, kind: str
) -> str | None:
    """Return a recovery hint when a market snapshot is empty.

    `_safe_mesh_call` already converts mesh errors to {note: "mesh ..."}.
    This handles the other half: a successful call that returned no data.
    Mesh shapes vary by endpoint, so the check looks at three signals in
    order: top-level `status`, nested `summary.succeeded`, then known
    list-key population.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return f"no {kind} data returned for '{ticker}'; verify the ticker symbol"

    if "note" in snapshot:
        return None  # mesh-failure note already in payload

    if snapshot.get("status") in ("no_data", "error"):
        return f"'{ticker}' returned no {kind} data; the ticker may be unsupported"

    for envelope in (snapshot, snapshot.get("data")):
        if not isinstance(envelope, dict):
            continue
        summary = envelope.get("summary")
        if isinstance(summary, dict) and "succeeded" in summary:
            if summary.get("succeeded", 0) == 0:
                return (
                    f"'{ticker}' returned no {kind} data; "
                    f"the ticker may be unsupported"
                )
            break  # mesh confirmed success; trust it

    list_keys = _MARKET_LIST_KEYS.get(kind, ())
    if list_keys:
        populated = any(
            isinstance(snapshot.get(k), list) and snapshot.get(k)
            for k in list_keys
        )
        if not populated:
            return (
                f"'{ticker}' returned no {kind} records; the ticker may be "
                f"unsupported for this slice or have no recent activity"
            )
    return None


def _period_for(days_back: int | None, *, default: str) -> str:
    if days_back is None:
        return default
    if days_back <= 7:
        return "5d"
    if days_back <= 32:
        return "1mo"
    if days_back <= 100:
        return "3mo"
    if days_back <= 200:
        return "6mo"
    return "1y"


# ── /api/price/* (split out of /api/market for the agent tool surface) ──
#
# The legacy `/api/market/{ticker}?kind=price|...` route still exists for
# the frontend. The Strands tool layer no longer uses it; it dispatches to
# the four split handlers below so quick-mode prompts can pull a compact
# quote card without dragging full OHLCV bars into the response phase.


class PriceSummary(BaseModel):
    latest_close: float | None = None
    prev_close: float | None = None
    change_pct_1d: float | None = None
    open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: int | None = None
    year_high: float | None = None
    year_low: float | None = None
    market_cap: float | None = None
    market_cap_display: str | None = None
    eps_ttm: float | None = None
    pe_ttm: float | None = None
    forward_pe: float | None = None
    valuation_note: str | None = None
    window_change_pct: float | None = None
    window_high: float | None = None
    window_low: float | None = None
    window_start: str | None = None
    window_end: str | None = None
    quote_source: str | None = None
    asof: str
    note: str | None = None

    @model_validator(mode="after")
    def _fill_market_cap_display(self) -> "PriceSummary":
        if self.market_cap_display is None and self.market_cap is not None:
            object.__setattr__(
                self,
                "market_cap_display",
                format_usd_short(self.market_cap),
            )
        return self


class PriceBar(BaseModel):
    model_config = ConfigDict(extra="allow")
    timestamp: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


class PriceHistory(BaseModel):
    interval: str
    window_start: str | None = None
    window_end: str | None = None
    bars: list[PriceBar]
    asof: str
    note: str | None = None


def _extract_yahoo_history(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the first per-symbol `data` block out of a yahoo_price_history payload."""
    if not isinstance(snapshot, dict):
        return None
    if "note" in snapshot:
        return None
    results = snapshot.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    data = first.get("data")
    return data if isinstance(data, dict) else None


@app.get("/api/price/{ticker}/summary", response_model=PriceSummary)
def get_price_summary(
    ticker: str,
    days_back: int | None = Query(None, ge=1, le=365),
) -> PriceSummary:
    """Compact quote card: session OHLCV, 52-week range, valuation, window stats."""
    from src.clients.mesh import yahoo_price_history
    from src.prices.quote_summary import build_quote_card

    asof = _utc_now_seconds()
    sym = ticker.upper()
    card = build_quote_card(sym)

    period = _period_for(days_back, default="1mo")
    snapshot = _safe_mesh_call(
        yahoo_price_history,
        ticker,
        interval="1d",
        period=period,
        limit_bars=min(252, days_back or 22),
    )
    hist_note = _diagnose_empty_market(snapshot, ticker=ticker, kind="price")
    data = _extract_yahoo_history(snapshot)
    window = data.get("window_summary") or {} if data else {}
    resolved = (data.get("resolved") or {}) if data else {}

    latest_close = card.get("latest_close")
    prev_close = card.get("prev_close")
    change_pct_1d = card.get("change_pct_1d")
    if latest_close is None and data:
        latest = data.get("latest_completed_bar") or {}
        prev = data.get("previous_completed_bar") or {}
        latest_close = latest.get("close")
        prev_close = prev.get("close")
        if (
            isinstance(latest_close, (int, float))
            and isinstance(prev_close, (int, float))
            and prev_close
        ):
            change_pct_1d = round((latest_close - prev_close) / prev_close * 100, 4)

    notes: list[str] = []
    if hist_note:
        notes.append(hist_note)
    if latest_close is None:
        notes.append(f"no quote data returned for '{sym}'")
    note = "; ".join(notes) if notes else None

    return PriceSummary(
        latest_close=latest_close,
        prev_close=prev_close,
        change_pct_1d=change_pct_1d,
        open=card.get("open"),
        day_high=card.get("day_high"),
        day_low=card.get("day_low"),
        volume=card.get("volume"),
        year_high=card.get("year_high"),
        year_low=card.get("year_low"),
        market_cap=card.get("market_cap"),
        eps_ttm=card.get("eps_ttm"),
        pe_ttm=card.get("pe_ttm"),
        forward_pe=card.get("forward_pe"),
        valuation_note=card.get("valuation_note"),
        window_change_pct=window.get("open_close_change_pct"),
        window_high=window.get("window_high"),
        window_low=window.get("window_low"),
        window_start=resolved.get("start"),
        window_end=resolved.get("end"),
        quote_source=card.get("quote_source"),
        asof=asof,
        note=note,
    )


@app.get("/api/price/{ticker}/history", response_model=PriceHistory)
def get_price_history(
    ticker: str,
    days_back: int | None = Query(None, ge=1, le=365),
) -> PriceHistory:
    """Full OHLCV bars. Use sparingly — quick mode should call price_summary."""
    from src.clients.mesh import yahoo_price_history

    asof = _utc_now_seconds()
    period = _period_for(days_back, default="1mo")
    snapshot = _safe_mesh_call(
        yahoo_price_history,
        ticker,
        interval="1d",
        period=period,
        limit_bars=min(252, days_back or 22),
    )
    note = _diagnose_empty_market(snapshot, ticker=ticker, kind="price")
    data = _extract_yahoo_history(snapshot)
    if data is None:
        return PriceHistory(interval="1d", bars=[], asof=asof, note=note)
    resolved = data.get("resolved") or {}
    raw_bars = data.get("bars") or []
    bars: list[PriceBar] = []
    for b in raw_bars:
        if not isinstance(b, dict):
            continue
        ts = b.get("timestamp")
        if not ts:
            continue
        bars.append(
            PriceBar(
                timestamp=str(ts),
                open=b.get("open"),
                high=b.get("high"),
                low=b.get("low"),
                close=b.get("close"),
                volume=b.get("volume"),
            )
        )
    return PriceHistory(
        interval=str(data.get("interval") or "1d"),
        window_start=resolved.get("start"),
        window_end=resolved.get("end"),
        bars=bars,
        asof=asof,
        note=note,
    )


# ── /api/market/overview (cross-asset state-of-tape, one call) ──────────
#
# Built for "why did the market move today?"-shaped questions where a single
# ticker's price_summary is insufficient. Default basket spans equities, the
# 10Y yield, oil, gold, vol, and the dollar — enough to triangulate a session.

DEFAULT_OVERVIEW_SYMBOLS: tuple[str, ...] = (
    "SPY",        # S&P 500 ETF
    "QQQ",        # Nasdaq 100 ETF
    "^TNX",       # 10-year Treasury yield (index)
    "CL=F",       # WTI crude futures
    "GC=F",       # Gold futures
    "^VIX",       # CBOE volatility index
    "DX-Y.NYB",   # US Dollar Index
)


class MarketOverviewRow(BaseModel):
    ticker: str
    last: float | None = None
    change_pct_1d: float | None = None
    note: str | None = None


class MarketOverview(BaseModel):
    markets: list[MarketOverviewRow]
    asof: str
    note: str | None = None


@app.get("/api/market-overview", response_model=MarketOverview)
def get_market_overview(
    symbols: list[str] | None = Query(None),
) -> MarketOverview:
    """Compact one-call snapshot across mainstream cross-asset benchmarks.

    Returns last price and 1d change % per symbol — no OHLCV, no window stats.
    Use for "why did the market move today?" / cross-asset state-of-play
    scans. Defaults to a 7-symbol basket; pass `symbols` to override.
    """
    from src.clients.mesh import unwrap_results, yahoo_quote_snapshot

    asof = _utc_now_seconds()
    syms: list[str] = (
        [s.strip().upper() for s in symbols if isinstance(s, str) and s.strip()]
        if symbols
        else list(DEFAULT_OVERVIEW_SYMBOLS)
    )
    if not syms:
        return MarketOverview(markets=[], asof=asof, note="no symbols requested")

    try:
        raw = yahoo_quote_snapshot(syms)
        body = unwrap_results(raw) or {}
    except Exception as exc:
        return MarketOverview(
            markets=[MarketOverviewRow(ticker=s) for s in syms],
            asof=asof,
            note=f"mesh unavailable: {type(exc).__name__}: {exc}",
        )

    by_symbol: dict[str, dict[str, Any]] = {}
    for entry in body.get("results") or []:
        if not isinstance(entry, dict):
            continue
        sym = entry.get("symbol")
        if not isinstance(sym, str):
            continue
        if entry.get("status") != "success":
            continue
        data = entry.get("data")
        if isinstance(data, dict):
            by_symbol[sym.upper()] = data

    rows: list[MarketOverviewRow] = []
    missing: list[str] = []
    for sym in syms:
        data = by_symbol.get(sym)
        if not data:
            rows.append(MarketOverviewRow(ticker=sym, note="no data"))
            missing.append(sym)
            continue
        price_block = data.get("price") if isinstance(data.get("price"), dict) else data
        last = price_block.get("last_price")
        if not isinstance(last, (int, float)):
            last = price_block.get("price")
        prev = price_block.get("previous_close")
        chg: float | None = None
        if isinstance(last, (int, float)) and isinstance(prev, (int, float)) and prev:
            chg = round((float(last) - float(prev)) / float(prev) * 100, 4)
        rows.append(
            MarketOverviewRow(
                ticker=sym,
                last=float(last) if isinstance(last, (int, float)) else None,
                change_pct_1d=chg,
            )
        )

    note = f"no data for: {', '.join(missing)}" if missing else None
    return MarketOverview(markets=rows, asof=asof, note=note)


# ── /api/research/* (ad-hoc retrieval surfaces for the agent) ───────────
#
# Three sibling surfaces, source-typed:
#   - /api/research/stories  → curated synthesis, citable by story_id
#   - /api/research/web/search → open web, citable by URL
#   - /api/research/web/fetch  → one URL's body, citable by URL
# These are the non-thesis-scoped counterparts to `search_evidence`. The
# agent's discipline is in prompt_manager.py: curated first, web on
# miss/stale/out-of-corpus.


class StoryHit(BaseModel):
    story_id: str
    headline: str
    created_at: str | None = None
    snippet: str
    score: float
    # `source_url` of the cluster centroid. Real publisher URL — citable and
    # web_fetch-able. The `story_id` itself is an internal slug, not a URL;
    # construct a URL only from this field (or call `fetch_story`).
    url: str | None = None


class StoriesResponse(BaseModel):
    stories: list[StoryHit]
    asof: str
    note: str | None = None


def _isoformat_since(days_back: int | None) -> str | None:
    if not days_back or days_back <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.get("/api/research/stories", response_model=StoriesResponse)
def get_research_stories(
    query: str = Query(..., min_length=2),
    days_back: int | None = Query(None, ge=1, le=365),
    top_k: int = Query(8, ge=1, le=25),
) -> StoriesResponse:
    """Ad-hoc semantic search over curated stories.

    Sibling of `search_evidence` for the case where no thesis is in scope
    (e.g. "why did the market move today", "what's the latest in semis").
    """
    from src.story.match_index import search_dense_story

    asof = _utc_now_seconds()
    since = _isoformat_since(days_back)
    try:
        hits = search_dense_story(
            DB_PATH,
            query,
            top_k=top_k,
            min_score=0.0,
            since=since,
        )
    except Exception as exc:
        return StoriesResponse(
            stories=[], asof=asof, note=f"story index unavailable: {type(exc).__name__}: {exc}"
        )

    if not hits:
        return StoriesResponse(
            stories=[],
            asof=asof,
            note=(
                f"no curated stories matched '{query}'"
                + (f" within last {days_back}d" if days_back else "")
                + "; consider web_search for off-corpus topics"
            ),
        )

    meta_by_id: dict[str, tuple[str, str | None, str | None]] = {}
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT s.id, s.headline, s.created_at, n.source_url
            FROM story s
            LEFT JOIN news n ON n.id = s.centroid_news_id
            WHERE s.id IN ({",".join("?" * len(hits))})
              AND s.id NOT IN (
                SELECT story_id FROM story_quality_label
                WHERE label IN ('unclear', 'no_value')
              )
            """,
            tuple(h.story_id for h in hits),
        ).fetchall()
        for r in rows:
            meta_by_id[r["id"]] = (
                r["headline"] or r["id"],
                r["created_at"],
                r["source_url"],
            )

    out: list[StoryHit] = []
    for h in hits:
        meta = meta_by_id.get(h.story_id)
        if meta is None:
            continue  # filtered out by quality labels
        headline, created_at, url = meta
        snippet = h.chunk_text.strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277].rstrip() + "..."
        out.append(
            StoryHit(
                story_id=h.story_id,
                headline=headline,
                created_at=created_at,
                snippet=snippet,
                score=round(float(h.score), 4),
                url=url,
            )
        )
    return StoriesResponse(stories=out, asof=asof, note=None)


STORIES_DIR = Path(__file__).resolve().parent / "global" / "stories"


class StoryDetail(BaseModel):
    language: str = "en"
    headline: str | None = None
    created_at: str | None = None
    url: str | None = None
    markdown: str
    asof: str
    note: str | None = None


@app.get("/api/research/stories/{story_id}", response_model=StoryDetail)
def get_story_detail(
    story_id: str,
    locale: str | None = Query(None),
) -> StoryDetail:
    """Return the markdown body for one curated story (`story_<n>`).

    Markdown is the source of truth (overview bullets, quotes, sources with
    URLs). DB is queried only for headline / created_at / centroid URL.
    """
    asof = _utc_now_seconds()
    if not re.fullmatch(r"story_[A-Za-z0-9_-]+", story_id):
        raise HTTPException(
            status_code=400,
            detail="story_id must look like 'story_<n>'",
        )

    path, effective_language = localized_markdown_path(
        STORIES_DIR,
        story_id,
        normalize_language(locale),
    )
    markdown = ""
    if path.is_file():
        try:
            markdown = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return StoryDetail(
                language="en",
                markdown="",
                asof=asof,
                note=f"story file unreadable: {type(exc).__name__}: {exc}",
            )

    headline: str | None = None
    created_at: str | None = None
    url: str | None = None
    with db() as conn:
        row = conn.execute(
            """
            SELECT s.headline, s.created_at, n.source_url
            FROM story s
            LEFT JOIN news n ON n.id = s.centroid_news_id
            WHERE s.id = ?
            """,
            (story_id,),
        ).fetchone()
        if row is not None:
            headline = row["headline"]
            created_at = row["created_at"]
            url = row["source_url"]

    note: str | None = None
    if not markdown and headline is None:
        note = f"story '{story_id}' not found"
    elif not markdown:
        note = "story markdown file missing — DB row only"

    # `markdown` already holds the (possibly translated) file body — take the
    # localized headline from it instead of re-reading the sidecar.
    if effective_language != "en":
        title_match = STORY_TITLE_RE.search(markdown)
        if title_match:
            headline = title_match.group("title").strip()
    return StoryDetail(
        language=effective_language if markdown else "en",
        headline=headline,
        created_at=created_at,
        url=url,
        markdown=markdown,
        asof=asof,
        note=note,
    )


class WebSearchHit(BaseModel):
    url: str
    title: str
    snippet: str = ""
    published_date: str | None = None


class WebSearchResponse(BaseModel):
    results: list[WebSearchHit]
    asof: str
    note: str | None = None


@app.get("/api/research/web/search", response_model=WebSearchResponse)
def get_web_search(
    query: str = Query(..., min_length=2),
    num_results: int = Query(8, ge=1, le=15),
    days_back: int | None = Query(None, ge=1, le=365),
) -> WebSearchResponse:
    """Open-web search via Firecrawl/Exa (configured by WEB_SEARCH_PROVIDER).

    Use when the curated story index doesn't cover the topic (acronyms,
    definitions, specific facts, breaking events outside our synthesis
    cadence). Returns title + short snippet per hit; for full body call
    web_fetch on a chosen URL.
    """
    from src.clients.web_search import search_snippets

    asof = _utc_now_seconds()
    try:
        raw = search_snippets(
            query,
            num_results=num_results,
            days_back=days_back,
        )
    except Exception as exc:
        return WebSearchResponse(
            results=[],
            asof=asof,
            note=f"web search unavailable: {type(exc).__name__}: {exc}",
        )

    out: list[WebSearchHit] = [
        WebSearchHit(
            url=r.url,
            title=r.title or r.url,
            snippet=r.text,
            published_date=r.published_date,
        )
        for r in raw
    ]

    note: str | None = None
    if not out:
        note = f"no results for '{query}'" + (
            f" within last {days_back}d" if days_back else ""
        )
    return WebSearchResponse(results=out, asof=asof, note=note)


class WebFetchResponse(BaseModel):
    title: str
    text: str
    published_date: str | None = None
    asof: str
    note: str | None = None


@app.get("/api/research/web/fetch", response_model=WebFetchResponse)
def get_web_fetch(
    url: str = Query(..., min_length=8),
) -> WebFetchResponse:
    """Scrape one URL's main content. Use only with a URL already in hand
    (typically from a prior web_search row or a citation in another tool's
    output). Returns up to ~4000 chars of readable text — enough to extract
    a single number/quote without dragging the full page into context."""
    from src.clients.web_search import scrape as web_scrape

    asof = _utc_now_seconds()
    if not (url.startswith("http://") or url.startswith("https://")):
        return WebFetchResponse(
            title=url, text="", asof=asof,
            note="url must start with http:// or https://",
        )
    # Guard against the model fabricating a URL by appending an internal
    # story_id slug to a publisher base (e.g. https://cnbc.com/story_312).
    # `story_id` is an internal identifier, not a URL — direct the model to
    # `fetch_story` instead.
    if re.search(r"/story_[A-Za-z0-9_-]+/?$", url):
        return WebFetchResponse(
            title=url, text="", asof=asof,
            note=(
                "this URL looks fabricated from an internal story_id "
                "(e.g. story_312). Use fetch_story(story_id) to read the "
                "story's content; never construct a URL from a story_id."
            ),
        )
    try:
        result = web_scrape(url, text_max_characters=4000)
    except Exception as exc:
        return WebFetchResponse(
            title=url, text="", asof=asof,
            note=f"web fetch unavailable: {type(exc).__name__}: {exc}",
        )
    return WebFetchResponse(
        title=result.title or (result.url or url),
        text=(result.text or "").strip(),
        published_date=result.published_date,
        asof=asof,
        note=None if (result.text or "").strip() else "scrape returned empty body",
    )


# ── /api/filings/{ticker} + /api/insider/{ticker} (flat lists) ──────────


class FilingItem(BaseModel):
    filing_date: str
    form: str
    report_date: str | None = None
    items: list[str] = Field(default_factory=list)
    primary_document: str | None = None
    primary_doc_description: str | None = None
    accession_number: str | None = None
    # Canonical EDGAR URL for the filing's primary document, pre-built so the
    # agent can `web_fetch` it without guessing path components. Format:
    # https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no_dashes}/{primary_document}
    primary_document_url: str | None = None


class FilingsResponse(BaseModel):
    cik: str | None = None
    company: str | None = None
    counts_by_form: dict[str, int] = Field(default_factory=dict)
    filings: list[FilingItem]
    asof: str
    note: str | None = None


class InsiderTransaction(BaseModel):
    filing_date: str
    transaction_date: str | None = None
    reporting_person: str
    relationship: str | None = None
    transaction_code: str | None = None
    transaction_label: str | None = None
    security_title: str | None = None
    shares: str | None = None
    direction: str | None = None  # 'A' (acquired) or 'D' (disposed)
    price_per_share: str | None = None
    shares_owned_after: str | None = None


class InsiderResponse(BaseModel):
    cik: str | None = None
    company: str | None = None
    transactions: list[InsiderTransaction]
    asof: str
    note: str | None = None


def _company_fields(snapshot: dict[str, Any]) -> tuple[str | None, str | None]:
    company = snapshot.get("company") if isinstance(snapshot, dict) else None
    if isinstance(company, dict):
        return (
            str(company.get("cik")) if company.get("cik") else None,
            company.get("title") or company.get("name"),
        )
    return None, None


_SEC_USER_AGENT = "Heurist Finance team@heurist.xyz"


def _sec_accession_index(cik: str) -> dict[tuple[str, str], str]:
    """Fetch SEC EDGAR submissions JSON and return
    `{(filing_date, primary_document): accession_number}` for the recent set.

    The mesh `filing_timeline` tool doesn't expose accession numbers; without
    one we can't build the `/Archives/edgar/data/.../<accession>/...` URL.
    SEC's submissions endpoint is the canonical source and is free / rate-
    limited to ~10 req/s with the required UA header. Failure here returns
    an empty index — callers degrade to `accession_number=None`.
    """
    import httpx

    try:
        padded = str(cik).strip().lstrip("0").zfill(10)
    except Exception:
        return {}
    if not padded.isdigit():
        return {}
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    try:
        r = httpx.get(url, headers={"User-Agent": _SEC_USER_AGENT}, timeout=8.0)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {}
    recent = (data.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []
    index: dict[tuple[str, str], str] = {}
    for acc, fd, doc in zip(accessions, dates, docs):
        if acc and fd and doc:
            index[(str(fd), str(doc))] = str(acc)
    return index


def _build_primary_document_url(
    cik: str | None, accession: str | None, primary_document: str | None
) -> str | None:
    if not (cik and accession and primary_document):
        return None
    cik_int = str(cik).lstrip("0") or "0"
    acc_no_dashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{acc_no_dashes}/{primary_document}"
    )


@app.get("/api/filings/{ticker}", response_model=FilingsResponse)
def get_filings(ticker: str) -> FilingsResponse:
    """Flat list of recent SEC filings for `ticker` (one row per filing)."""
    from src.clients.mesh import sec_tool

    asof = _utc_now_seconds()
    snapshot = _safe_mesh_call(sec_tool, "filing_timeline", {"query": ticker})
    note = _diagnose_empty_market(snapshot, ticker=ticker, kind="filings")
    cik, company = _company_fields(snapshot)
    counts = snapshot.get("counts_by_form") if isinstance(snapshot, dict) else None
    raw = snapshot.get("filings") if isinstance(snapshot, dict) else None

    accession_index = _sec_accession_index(cik) if cik else {}

    filings: list[FilingItem] = []
    if isinstance(raw, list):
        for f in raw:
            if not isinstance(f, dict):
                continue
            fd = f.get("filing_date")
            form = f.get("form")
            if not fd or not form:
                continue
            items = f.get("items")
            primary_document = f.get("primary_document")
            accession = accession_index.get((str(fd), str(primary_document or "")))
            form_str = str(form)
            fetchable_url = (
                _build_primary_document_url(cik, accession, primary_document)
                if primary_document_url_fetchable(form_str)
                else None
            )
            filings.append(
                FilingItem(
                    filing_date=str(fd),
                    form=form_str,
                    report_date=f.get("report_date"),
                    items=[str(x) for x in items] if isinstance(items, list) else [],
                    primary_document=primary_document,
                    primary_doc_description=f.get("primary_doc_description"),
                    accession_number=accession,
                    primary_document_url=fetchable_url,
                )
            )
    return FilingsResponse(
        cik=cik,
        company=company,
        counts_by_form=counts if isinstance(counts, dict) else {},
        filings=filings,
        asof=asof,
        note=note,
    )


# ── /api/xbrl/{ticker} (single-metric XBRL trend with QoQ + YoY) ────────


# Alias → SEC mesh metric query string. Exact us-gaap concept names are passed
# verbatim so the mesh fuzzy resolver can't misroute (e.g. 'eps' → Basic, or
# 'operating margin' → OperatingIncomeLoss). Plain-English aliases are used
# only when the resolver's primary match is consistently correct across
# issuers (revenue, gross_profit, cash, net_income).
_XBRL_METRIC_ALIASES: dict[str, str] = {
    "revenue": "revenue",
    "net_income": "net income",
    "eps_diluted": "EarningsPerShareDiluted",
    "eps_basic": "EarningsPerShareBasic",
    "gross_profit": "gross profit",
    "operating_income": "OperatingIncomeLoss",
    "cash": "cash",
    "total_assets": "Assets",
    # Mesh's plain-English 'total debt' resolver fails; long-term debt is
    # the closest single-concept proxy. Documented in the tool description.
    "total_debt": "LongTermDebt",
    "operating_cash_flow": "NetCashProvidedByUsedInOperatingActivities",
    "shares_diluted": "WeightedAverageNumberOfDilutedSharesOutstanding",
}

# Latest filing must be within this many days of "now" or we attach a
# staleness note. One quarter + a buffer for late filers.
_XBRL_STALENESS_DAYS = 120

# YoY counterpart matching tolerance: same period one year prior, ±15 days
# to handle 13-week vs calendar-quarter drift.
_YOY_TOLERANCE_DAYS = 15


class XbrlObservation(BaseModel):
    start: str | None = None
    end: str | None = None
    filed: str | None = None
    form: str | None = None
    fy: int | None = None
    fp: str | None = None
    value: float | None = None
    # Pre-formatted human string ("$2.24B" / "1.23B shares"). Populated by
    # get_xbrl_fact based on the response unit so the agent can quote it
    # verbatim instead of parsing 10-digit floats.
    value_display: str | None = None


class XbrlSummary(BaseModel):
    latest_period: str | None = None
    latest_value: float | None = None
    latest_value_display: str | None = None
    latest_filed: str | None = None
    qoq_pct: float | None = None
    qoq_previous_period: str | None = None
    qoq_previous_value: float | None = None
    qoq_previous_value_display: str | None = None
    yoy_pct: float | None = None
    yoy_previous_period: str | None = None
    yoy_previous_value: float | None = None
    yoy_previous_value_display: str | None = None


class XbrlFactResponse(BaseModel):
    cik: str | None = None
    company: str | None = None
    concept: str | None = None
    concept_label: str | None = None
    unit: str | None = None
    summary: XbrlSummary | None = None
    observations: list[XbrlObservation] = Field(default_factory=list)
    asof: str
    note: str | None = None


def _parse_iso_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _dedupe_observations(raw: list[Any]) -> list[dict[str, Any]]:
    """Collapse XBRL re-filings: same (start, end) → keep the latest `filed`.

    The mesh tool returns the same period multiple times because each new
    10-Q/10-K embeds the prior-year comparative columns. Without dedup,
    `limit=8` quietly returns ~4 unique quarters and a confused agent.
    """
    by_period: dict[tuple[Any, Any], dict[str, Any]] = {}
    for obs in raw:
        if not isinstance(obs, dict):
            continue
        key = (obs.get("start"), obs.get("end"))
        existing = by_period.get(key)
        if existing is None:
            by_period[key] = obs
            continue
        prev_filed = _parse_iso_date(existing.get("filed"))
        this_filed = _parse_iso_date(obs.get("filed"))
        if this_filed and (not prev_filed or this_filed > prev_filed):
            by_period[key] = obs
    deduped = list(by_period.values())
    deduped.sort(
        key=lambda o: (_parse_iso_date(o.get("end")) or datetime.min),
        reverse=True,
    )
    return deduped


def _yoy_match(latest: dict[str, Any], deduped: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the same quarter one year prior, allowing ±15 days for fiscal drift."""
    latest_end = _parse_iso_date(latest.get("end"))
    if latest_end is None:
        return None
    target = latest_end - timedelta(days=365)
    best: dict[str, Any] | None = None
    best_delta = timedelta(days=_YOY_TOLERANCE_DAYS + 1)
    for obs in deduped:
        if obs is latest:
            continue
        end = _parse_iso_date(obs.get("end"))
        if end is None:
            continue
        delta = abs(end - target)
        if delta <= timedelta(days=_YOY_TOLERANCE_DAYS) and delta < best_delta:
            best = obs
            best_delta = delta
    return best


def _xbrl_error_to_note(message: str, ticker: str) -> str:
    """Translate raw mesh errors into actionable hints for the agent."""
    msg = message or ""
    if "Low-confidence" in msg or "No SEC issuer" in msg:
        return (
            f"no SEC XBRL coverage for '{ticker}' "
            f"(likely non-US filer, ETF, or unrecognized ticker); "
            f"try fundamentals_snapshot for ADRs or price_summary for ETFs"
        )
    if "No SEC XBRL metric matched" in msg:
        return (
            f"unsupported metric. Use one of: "
            f"{', '.join(sorted(_XBRL_METRIC_ALIASES))}"
        )
    return f"mesh error: {msg}"


@app.get("/api/xbrl/{ticker}", response_model=XbrlFactResponse)
def get_xbrl_fact(
    ticker: str,
    metric: str = Query(...),
    frequency: Literal["quarterly", "annual"] = Query("quarterly"),
    limit: int | None = Query(None, ge=1, le=40),
) -> XbrlFactResponse:
    """One financial line item, one ticker — deduped XBRL series with QoQ + YoY."""
    from src.clients.mesh import sec_tool

    asof = _utc_now_seconds()

    if metric not in _XBRL_METRIC_ALIASES:
        return XbrlFactResponse(
            asof=asof,
            note=(
                f"unsupported metric '{metric}'. Use one of: "
                f"{', '.join(sorted(_XBRL_METRIC_ALIASES))}"
            ),
        )

    args: dict[str, Any] = {
        "query": ticker,
        "metric": _XBRL_METRIC_ALIASES[metric],
        "frequency": frequency,
        # Over-fetch so dedup leaves us with `limit` unique periods.
        "limit": (limit or 8) * 2,
    }
    snapshot = _safe_mesh_call(sec_tool, "xbrl_fact_trends", args)

    if isinstance(snapshot, dict) and "note" in snapshot and "filings" not in snapshot:
        return XbrlFactResponse(
            asof=asof,
            note=_xbrl_error_to_note(snapshot["note"], ticker),
        )

    cik, company = _company_fields(snapshot)
    metric_meta = snapshot.get("metric") if isinstance(snapshot, dict) else None
    if not isinstance(metric_meta, dict):
        metric_meta = {}
    raw_obs = snapshot.get("observations") if isinstance(snapshot, dict) else None
    deduped = _dedupe_observations(raw_obs if isinstance(raw_obs, list) else [])

    requested_limit = limit or 8
    truncated = deduped[:requested_limit]

    unit = snapshot.get("unit") if isinstance(snapshot, dict) else None
    is_usd = isinstance(unit, str) and unit.upper() in {"USD", "USD/SHARES"}

    def _display(v: Any) -> str | None:
        if is_usd:
            return format_usd_short(v)
        return format_count_short(v)

    summary: XbrlSummary | None = None
    note: str | None = None
    if truncated:
        latest = truncated[0]
        prior = truncated[1] if len(truncated) > 1 else None
        yoy = _yoy_match(latest, deduped)

        latest_value = latest.get("value")
        latest_period = (
            f"{latest.get('start')} to {latest.get('end')}"
            if latest.get("start")
            else latest.get("end")
        )

        qoq_pct = qoq_prev_value = qoq_prev_period = None
        if prior and isinstance(prior.get("value"), (int, float)) and isinstance(latest_value, (int, float)) and prior.get("value"):
            qoq_prev_value = prior["value"]
            qoq_prev_period = (
                f"{prior.get('start')} to {prior.get('end')}"
                if prior.get("start")
                else prior.get("end")
            )
            qoq_pct = round(((latest_value - prior["value"]) / abs(prior["value"])) * 100, 2)

        yoy_pct = yoy_prev_value = yoy_prev_period = None
        if yoy and isinstance(yoy.get("value"), (int, float)) and isinstance(latest_value, (int, float)) and yoy.get("value"):
            yoy_prev_value = yoy["value"]
            yoy_prev_period = (
                f"{yoy.get('start')} to {yoy.get('end')}"
                if yoy.get("start")
                else yoy.get("end")
            )
            yoy_pct = round(((latest_value - yoy["value"]) / abs(yoy["value"])) * 100, 2)

        summary = XbrlSummary(
            latest_period=latest_period,
            latest_value=latest_value,
            latest_value_display=_display(latest_value),
            latest_filed=latest.get("filed"),
            qoq_pct=qoq_pct,
            qoq_previous_period=qoq_prev_period,
            qoq_previous_value=qoq_prev_value,
            qoq_previous_value_display=_display(qoq_prev_value),
            yoy_pct=yoy_pct,
            yoy_previous_period=yoy_prev_period,
            yoy_previous_value=yoy_prev_value,
            yoy_previous_value_display=_display(yoy_prev_value),
        )

        latest_filed_dt = _parse_iso_date(latest.get("filed"))
        if latest_filed_dt is not None:
            age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - latest_filed_dt).days
            if age_days > _XBRL_STALENESS_DAYS:
                note = (
                    f"latest filing is {age_days} days old; "
                    f"verify the resolved issuer ({company or 'unknown'}, "
                    f"CIK {cik or 'unknown'}) is the right entity for '{ticker}'"
                )
    else:
        note = f"no XBRL observations returned for '{ticker}' / {metric}"

    return XbrlFactResponse(
        cik=cik,
        company=company,
        concept=metric_meta.get("concept"),
        concept_label=metric_meta.get("label"),
        unit=snapshot.get("unit") if isinstance(snapshot, dict) else None,
        summary=summary,
        observations=[
            XbrlObservation(
                start=o.get("start"),
                end=o.get("end"),
                filed=o.get("filed"),
                form=o.get("form"),
                fy=o.get("fy"),
                fp=o.get("fp"),
                value=o.get("value"),
                value_display=_display(o.get("value")),
            )
            for o in truncated
        ],
        asof=asof,
        note=note,
    )


# ── /api/fundamentals/{ticker} (Yahoo equity_overview, fundamentals only) ──


def _attach_usd_display(self: BaseModel, fields: tuple[str, ...]) -> BaseModel:
    """Populate ``{field}_display`` siblings from raw float fields."""
    for field in fields:
        display_attr = f"{field}_display"
        if not hasattr(self, display_attr):
            continue
        if getattr(self, display_attr) is None:
            setattr(self, display_attr, format_usd_short(getattr(self, field, None)))
    return self


class FundamentalsIncomeStatement(BaseModel):
    as_of: str | None = None
    total_revenue: float | None = None
    total_revenue_display: str | None = None
    gross_profit: float | None = None
    gross_profit_display: str | None = None
    operating_income: float | None = None
    operating_income_display: str | None = None
    net_income: float | None = None
    net_income_display: str | None = None
    diluted_eps: float | None = None
    diluted_eps_display: str | None = None
    ebitda: float | None = None
    ebitda_display: str | None = None

    @model_validator(mode="after")
    def _fill_display(self) -> "FundamentalsIncomeStatement":
        return _attach_usd_display(
            self,
            (
                "total_revenue",
                "gross_profit",
                "operating_income",
                "net_income",
                "diluted_eps",
                "ebitda",
            ),
        )


class FundamentalsBalanceSheet(BaseModel):
    as_of: str | None = None
    cash_and_equivalents: float | None = None
    cash_and_equivalents_display: str | None = None
    total_assets: float | None = None
    total_assets_display: str | None = None
    total_liabilities: float | None = None
    total_liabilities_display: str | None = None
    total_debt: float | None = None
    total_debt_display: str | None = None
    shareholders_equity: float | None = None
    shareholders_equity_display: str | None = None
    working_capital: float | None = None
    working_capital_display: str | None = None

    @model_validator(mode="after")
    def _fill_display(self) -> "FundamentalsBalanceSheet":
        return _attach_usd_display(
            self,
            (
                "cash_and_equivalents",
                "total_assets",
                "total_liabilities",
                "total_debt",
                "shareholders_equity",
                "working_capital",
            ),
        )


class FundamentalsCashFlow(BaseModel):
    as_of: str | None = None
    operating_cash_flow: float | None = None
    operating_cash_flow_display: str | None = None
    capital_expenditure: float | None = None
    capital_expenditure_display: str | None = None
    free_cash_flow: float | None = None
    free_cash_flow_display: str | None = None
    investing_cash_flow: float | None = None
    investing_cash_flow_display: str | None = None
    financing_cash_flow: float | None = None
    financing_cash_flow_display: str | None = None
    dividends_paid: float | None = None
    dividends_paid_display: str | None = None

    @model_validator(mode="after")
    def _fill_display(self) -> "FundamentalsCashFlow":
        return _attach_usd_display(
            self,
            (
                "operating_cash_flow",
                "capital_expenditure",
                "free_cash_flow",
                "investing_cash_flow",
                "financing_cash_flow",
                "dividends_paid",
            ),
        )


class FundamentalsCalendar(BaseModel):
    earnings_date: list[str] = Field(default_factory=list)
    earnings_estimate: dict[str, float] | None = None
    revenue_estimate: dict[str, float] | None = None
    dividend_date: str | None = None
    ex_dividend_date: str | None = None


class FundamentalsCompany(BaseModel):
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    market_cap_display: str | None = None
    currency: str | None = None
    exchange: str | None = None
    asset_type: str | None = None

    @model_validator(mode="after")
    def _fill_display(self) -> "FundamentalsCompany":
        return _attach_usd_display(self, ("market_cap",))


class FundamentalsResponse(BaseModel):
    company: FundamentalsCompany | None = None
    income_latest_annual: FundamentalsIncomeStatement | None = None
    income_latest_quarterly: FundamentalsIncomeStatement | None = None
    balance_latest_quarterly: FundamentalsBalanceSheet | None = None
    cash_flow_latest_quarterly: FundamentalsCashFlow | None = None
    calendar: FundamentalsCalendar | None = None
    coverage: dict[str, bool] = Field(default_factory=dict)
    asof: str
    note: str | None = None


def _filter_keys(model_cls: type[BaseModel], data: Any) -> dict[str, Any]:
    """Project a mesh dict onto a Pydantic model's known fields, dropping extras."""
    if not isinstance(data, dict):
        return {}
    return {k: data.get(k) for k in model_cls.model_fields if k in data}


def _coverage_filled(model_obj: BaseModel | None) -> bool:
    """Coverage flag: at least one numeric field beyond `as_of` populated."""
    if model_obj is None:
        return False
    dump = model_obj.model_dump()
    for key, val in dump.items():
        if key == "as_of":
            continue
        if val is not None:
            return True
    return False


@app.get("/api/fundamentals/{ticker}", response_model=FundamentalsResponse)
def get_fundamentals(ticker: str) -> FundamentalsResponse:
    """Latest annual + quarterly fundamentals plus forward calendar."""
    from src.clients.mesh import yahoo_tool

    asof = _utc_now_seconds()
    snapshot = _safe_mesh_call(
        yahoo_tool,
        "equity_overview",
        {"symbols": [ticker], "sections": ["fundamentals"]},
    )

    if isinstance(snapshot, dict) and "note" in snapshot:
        raw = snapshot["note"]
        if "asset_type 'etf'" in raw or "asset_type 'fund'" in raw:
            note = (
                f"fundamentals_snapshot is equity-only; '{ticker}' is an "
                f"ETF/fund — use price_summary or price_history instead"
            )
        elif "asset_type" in raw:
            note = (
                f"fundamentals_snapshot is equity-only; '{ticker}' did not "
                f"resolve to a stock"
            )
        else:
            note = f"mesh error: {raw}"
        return FundamentalsResponse(asof=asof, note=note)

    results = snapshot.get("results") if isinstance(snapshot, dict) else None
    first = results[0] if isinstance(results, list) and results else None
    if not isinstance(first, dict) or first.get("status") != "success":
        err = (first or {}).get("error") if isinstance(first, dict) else None
        return FundamentalsResponse(
            asof=asof,
            note=f"no fundamentals data for '{ticker}'" + (f": {err}" if err else ""),
        )

    data = first.get("data") or {}
    company_raw = data.get("company") or {}
    fundamentals = data.get("fundamentals") or {}
    fs = fundamentals.get("financial_summary") or {}
    cal_raw = fundamentals.get("calendar") or {}

    income_annual = (fs.get("income_statement") or {}).get("latest_annual")
    income_q = (fs.get("income_statement") or {}).get("latest_quarterly")
    bal_q = (fs.get("balance_sheet") or {}).get("latest_quarterly")
    cf_q = (fs.get("cash_flow") or {}).get("latest_quarterly")

    company = FundamentalsCompany(
        name=company_raw.get("name"),
        sector=company_raw.get("sector"),
        industry=company_raw.get("industry"),
        market_cap=company_raw.get("market_cap"),
        currency=company_raw.get("currency"),
        exchange=company_raw.get("exchange"),
        asset_type=company_raw.get("asset_type"),
    )
    income_a = (
        FundamentalsIncomeStatement(**_filter_keys(FundamentalsIncomeStatement, income_annual))
        if isinstance(income_annual, dict) else None
    )
    income_qm = (
        FundamentalsIncomeStatement(**_filter_keys(FundamentalsIncomeStatement, income_q))
        if isinstance(income_q, dict) else None
    )
    bal_qm = (
        FundamentalsBalanceSheet(**_filter_keys(FundamentalsBalanceSheet, bal_q))
        if isinstance(bal_q, dict) else None
    )
    cf_qm = (
        FundamentalsCashFlow(**_filter_keys(FundamentalsCashFlow, cf_q))
        if isinstance(cf_q, dict) else None
    )
    calendar = FundamentalsCalendar(
        earnings_date=cal_raw.get("earnings_date") or [],
        earnings_estimate=cal_raw.get("earnings_estimate"),
        revenue_estimate=cal_raw.get("revenue_estimate"),
        dividend_date=cal_raw.get("dividend_date"),
        ex_dividend_date=cal_raw.get("ex_dividend_date"),
    )

    coverage = {
        "income_annual": _coverage_filled(income_a),
        "income_quarterly": _coverage_filled(income_qm),
        "balance_quarterly": _coverage_filled(bal_qm),
        "cash_flow_quarterly": _coverage_filled(cf_qm),
        "calendar": bool(cal_raw.get("earnings_date") or cal_raw.get("revenue_estimate")),
    }

    note: str | None = None
    filled = sum(1 for v in coverage.values() if v)
    if filled <= 2:
        note = (
            f"sparse fundamentals for '{ticker}' "
            f"({filled}/{len(coverage)} sections populated); "
            f"likely a non-US filer / ADR — revenue and margins may be unavailable"
        )
    else:
        as_of_q = (income_q or {}).get("as_of") if isinstance(income_q, dict) else None
        as_of_dt = _parse_iso_date(as_of_q)
        if as_of_dt is not None:
            age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - as_of_dt).days
            if age_days > _XBRL_STALENESS_DAYS:
                note = (
                    f"latest quarterly is {age_days} days old "
                    f"(as of {as_of_q}); data may be stale"
                )

    return FundamentalsResponse(
        company=company,
        income_latest_annual=income_a,
        income_latest_quarterly=income_qm,
        balance_latest_quarterly=bal_qm,
        cash_flow_latest_quarterly=cf_qm,
        calendar=calendar,
        coverage=coverage,
        asof=asof,
        note=note,
    )


@app.get("/api/insider/{ticker}", response_model=InsiderResponse)
def get_insider(ticker: str) -> InsiderResponse:
    """Flat list of insider transactions (one row per transaction, not per filing)."""
    from src.clients.mesh import sec_tool

    asof = _utc_now_seconds()
    snapshot = _safe_mesh_call(sec_tool, "insider_activity", {"query": ticker})
    note = _diagnose_empty_market(snapshot, ticker=ticker, kind="insider")
    cik, company = _company_fields(snapshot)

    transactions: list[InsiderTransaction] = []
    raw_filings = snapshot.get("filings") if isinstance(snapshot, dict) else None
    if isinstance(raw_filings, list):
        for f in raw_filings:
            if not isinstance(f, dict):
                continue
            filing_date = f.get("filing_date")
            person = f.get("reporting_person") or f.get("name") or "unknown"
            relationship = f.get("relationship")
            txs = f.get("transactions")
            if not filing_date:
                continue
            if not isinstance(txs, list) or not txs:
                # Form 3 / Form 5 sometimes have no per-row transactions —
                # surface one row so the agent doesn't miss the filing entirely.
                transactions.append(
                    InsiderTransaction(
                        filing_date=str(filing_date),
                        reporting_person=str(person),
                        relationship=relationship,
                    )
                )
                continue
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                transactions.append(
                    InsiderTransaction(
                        filing_date=str(filing_date),
                        transaction_date=tx.get("transaction_date"),
                        reporting_person=str(person),
                        relationship=relationship,
                        transaction_code=tx.get("transaction_code"),
                        transaction_label=tx.get("transaction_label"),
                        security_title=tx.get("security_title"),
                        shares=tx.get("shares"),
                        direction=tx.get("direction"),
                        price_per_share=tx.get("price_per_share"),
                        shares_owned_after=tx.get("shares_owned_after"),
                    )
                )
    return InsiderResponse(
        cik=cik,
        company=company,
        transactions=transactions,
        asof=asof,
        note=note,
    )


# ── /api/macro (Blind Spots & macro context) ────────────────────────────

_MACRO_PERCENT_UNITS = frozenset({"Percent", "pct", "percentage_points"})


def _format_macro_percent_value(value: float, unit: str) -> str:
    suffix = "pp" if unit == "percentage_points" else "%"
    return f"{value:g}{suffix}"


def _compact_macro_point(point: dict[str, Any]) -> dict[str, Any]:
    """Shrink one FRED observation for agent context: percent → '4.36%', drop unit."""
    unit = point.get("unit")
    out: dict[str, Any] = {
        key: val for key, val in point.items() if key not in {"unit", "basis_points"}
    }
    raw_value = out.get("value")
    if isinstance(unit, str) and unit in _MACRO_PERCENT_UNITS and isinstance(
        raw_value, (int, float)
    ) and not isinstance(raw_value, bool):
        out["value"] = _format_macro_percent_value(float(raw_value), unit)
    return out


def _compact_macro_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_compact_macro_point(obs) for obs in points]


class MacroResponse(BaseModel):
    series: list[dict[str, Any]]
    asof: str
    note: str | None = None


DEFAULT_MACRO_SERIES = (
    "fed_funds",
    "core_cpi",
    "unemployment_rate",
    "ust_10y",
    "curve_10y_minus_2y",
)


@app.get("/api/macro", response_model=MacroResponse)
def get_macro(
    series_keys: list[str] | None = Query(None),
    views: list[str | None] | None = Query(None),
    limit: int = Query(24, ge=5, le=60),
) -> MacroResponse:
    from src.clients.mesh import fred_tool

    asof = _utc_now_seconds()
    if not series_keys:
        raise HTTPException(
            status_code=400,
            detail=(
                "search_macro requires explicit series. Pass series keys like "
                "core_cpi, fed_funds, ust_10y, or curve_10y_minus_2y with the "
                "view needed for the question."
            ),
        )
    if len(series_keys) > 12:
        raise HTTPException(
            status_code=400,
            detail="search_macro accepts at most 12 series specs per call",
        )
    if not 5 <= limit <= 60:
        raise HTTPException(
            status_code=400,
            detail="limit must be between 5 and 60 observations",
        )
    if views and len(views) != len(series_keys):
        raise HTTPException(
            status_code=400,
            detail="views must be omitted or have the same length as series_keys",
        )

    def fetch_one(idx_key: tuple[int, str]) -> tuple[int, dict[str, Any] | None]:
        idx, key = idx_key
        requested_view = views[idx] if views else None
        args: dict[str, Any] = {"series_key": key, "limit": limit}
        if requested_view:
            args["view"] = requested_view
        payload = _safe_mesh_call(fred_tool, "macro_series_history", args)
        if isinstance(payload, dict) and payload and "note" not in payload:
            meta = payload.get("series") if isinstance(payload.get("series"), dict) else {}
            observations = _compact_macro_points(
                _coerce_list(payload, ("observations",))
            )
            if observations:
                latest = payload.get("latest_summary")
                return idx, {
                    "series_key": str(meta.get("key") or key),
                    "series_id": meta.get("series_id"),
                    "title": meta.get("title"),
                    "pillar": meta.get("pillar"),
                    "frequency": meta.get("frequency"),
                    "units": meta.get("units"),
                    "view": payload.get("view") or requested_view or "level",
                    "observations": observations,
                    "latest_summary": (
                        _compact_macro_point(latest)
                        if isinstance(latest, dict)
                        else latest
                    ),
                    "resolved_window": payload.get("resolved_window"),
                    "point_in_time_safe": payload.get("point_in_time_safe"),
                }
        return idx, None

    with ThreadPoolExecutor(max_workers=min(len(series_keys), 12)) as pool:
        fetched = list(pool.map(fetch_one, enumerate(series_keys)))

    fetched.sort(key=lambda item: item[0])
    series = [item for _idx, item in fetched if item is not None]
    series_failures = len(fetched) - len(series)

    failures: list[str] = []
    if series_failures:
        failures.append(f"{series_failures} of {len(series_keys)} series")

    note: str | None = None
    if failures:
        if series_failures == len(series_keys):
            note = "macro data unavailable: all FRED calls failed"
        else:
            note = "partial macro data: " + ", ".join(failures) + " unavailable"

    return MacroResponse(
        series=series,
        asof=asof,
        note=note,
    )


def _coerce_list(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


# ── Local entrypoint (parity with `python api.py`) ───────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8088"))
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )

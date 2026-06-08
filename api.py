"""Quick & dirty read API for the hf-ui Home feed.

    uv run python api.py     # listens on :8088

Single endpoint:
    GET /api/home?user_id=user_1
returns a JSON blob shaped like hf-ui's MockData.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.news.display_tickers import filter_for_display
from src.news.publishers import publisher_for_name
from src.i18n import (
    TRANSLATED_LANGUAGES,
    localized_markdown_path,
    markdown_path_language,
    normalize_language,
)
from src.story.docs import StoryDocument, parse_story_markdown
from src.thesis.docs import parse_thesis_markdown
from src.thesis.scoring import SUPPORT_STRONG_CONF

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "db" / "hf.db"
THESES_DIR = ROOT / "global" / "theses"
STORIES_DIR = ROOT / "global" / "stories"
BRIEFS_DIR = ROOT / "global" / "briefs"

PORT = int(__import__("os").environ.get("PORT", "8787"))

# Stable color palette for source slugs. Cycles by hash for unknown sources.
PALETTE = [
    "oklch(0.55 0.18 30)", "oklch(0.45 0.08 250)", "oklch(0.25 0 0)",
    "oklch(0.70 0.10 50)", "oklch(0.55 0.15 260)", "oklch(0.45 0.15 25)",
    "oklch(0.35 0.05 260)", "oklch(0.50 0.20 25)", "oklch(0.30 0.10 260)",
    "oklch(0.45 0.18 300)", "oklch(0.55 0.12 220)", "oklch(0.50 0.14 140)",
    "oklch(0.55 0.20 40)", "oklch(0.45 0.18 330)", "oklch(0.55 0.14 170)",
    "oklch(0.50 0.12 160)", "oklch(0.45 0.12 200)", "oklch(0.45 0.10 280)",
    "oklch(0.55 0.14 240)", "oklch(0.40 0.10 240)", "oklch(0.45 0.12 220)",
    "oklch(0.50 0.20 260)", "oklch(0.45 0.18 25)",  "oklch(0.45 0.10 200)",
    "oklch(0.40 0.14 160)", "oklch(0.50 0.14 80)",
]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "src"


def cited_news_ids(*payload_jsons: str | None) -> set[str]:
    """Union of ``source_doc_ids`` across one or more story-payload JSON arrays
    (overview / claims / quotes). Returns the news rows the synth actually
    grounded the story in, vs the looser set in `news_cluster_member` which
    includes unrelated articles that happened to cluster together.
    """
    out: set[str] = set()
    for raw in payload_jsons:
        try:
            items = json.loads(raw or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for sid in item.get("source_doc_ids") or []:
                sid = str(sid).strip()
                if sid:
                    out.add(sid)
    return out


def cited_publishers(
    conn: sqlite3.Connection, news_ids: set[str]
) -> list[dict]:
    """One row per distinct publisher among ``news_ids``. Each row carries the
    publisher name plus a representative source_url (most recent cited
    article, ties broken by news_id) so the frontend can render one
    publisher chip even when a story cites several articles from the same
    outlet.
    """
    if not news_ids:
        return []
    placeholders = ",".join("?" * len(news_ids))
    rows = conn.execute(
        f"""
        SELECT publisher, source_url, id
        FROM news
        WHERE id IN ({placeholders})
          AND publisher IS NOT NULL
          AND publisher <> ''
        ORDER BY publisher, COALESCE(published_at, '') DESC, id
        """,
        list(news_ids),
    ).fetchall()
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        pub = row["publisher"]
        if pub in seen:
            continue
        seen.add(pub)
        out.append({
            "publisher": pub,
            "source_url": row["source_url"] or "",
            "news_id": row["id"],
        })
    return out


def color_for(slug: str) -> str:
    h = sum(ord(c) for c in slug)
    return PALETTE[h % len(PALETTE)]


def domain_for(name: str, url: str = "") -> str:
    """Canonical favicon host for a publisher: the registry's first host for
    known outlets (so `Yahoo Finance` and the bare `Yahoo` URL fallback both
    collapse to `finance.yahoo.com`), else the host parsed from the article
    URL. Empty when neither resolves — the frontend falls back to a letter
    avatar."""
    pub = publisher_for_name(name, url=url or None)
    return pub.hosts[0] if pub.hosts else ""


def first_sentence(text: str) -> str:
    text = text.strip()
    m = re.search(r"[.!?](?:\s|$)", text)
    return (text[: m.end()].strip() if m else text).rstrip()


def trend_for(score: int | None, prev: int | None, status: str) -> str:
    if status == "stressed":
        return "stressed"
    if score is None or prev is None:
        return "stable"
    delta = score - prev
    if delta >= 3:
        return "strengthening"
    if delta <= -3:
        return "weakening"
    return "stable"


def signal_for(score: int | None) -> str:
    if score is None:
        return "medium"
    if score >= 75:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def format_brief_date(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return iso
    return d.strftime("%A, %B %-d")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_thesis_md(thesis_id: str, language: str | None = None):
    path, _effective_language = localized_markdown_path(THESES_DIR, thesis_id, language)
    for candidate in dict.fromkeys((path, THESES_DIR / f"{thesis_id}.md")):
        try:
            return parse_thesis_markdown(candidate)
        except (ValueError, OSError):
            continue
    return None


_BRIEF_THEME_RE = re.compile(r"^\*\*\d+\.\*\*\s+(?P<text>.+?)\s*$")


def _load_brief_themes_from_markdown(
    brief_date: str,
    language: str | None,
) -> tuple[list[str], str]:
    path, effective_language = localized_markdown_path(BRIEFS_DIR, brief_date, language)
    if effective_language == "en" or not path.exists():
        return [], "en"
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError:
        return [], "en"
    themes: list[str] = []
    for line in markdown.splitlines():
        match = _BRIEF_THEME_RE.match(line.strip())
        if match:
            themes.append(match.group("text").strip())
    return themes, effective_language


def load_story_md(story_id: str, language: str | None = None) -> StoryDocument | None:
    """Translated story sidecar for `language`, or None when the request is
    English or no sidecar exists — callers fall back to the English DB row."""
    if language not in TRANSLATED_LANGUAGES:
        return None
    path = STORIES_DIR / f"{story_id}.{language}.md"
    try:
        return parse_story_markdown(path)
    except (ValueError, OSError):
        return None


def build_brief(conn, language: str | None = None) -> dict:
    row = conn.execute(
        "SELECT brief_date, themes_json FROM daily_briefs ORDER BY brief_date DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {"date": "", "themes": [], "theses": []}
    themes, effective_language = _load_brief_themes_from_markdown(
        row["brief_date"],
        language,
    )
    if not themes:
        themes = [t["text"] for t in json.loads(row["themes_json"])]
        effective_language = "en"
    return {
        "date": format_brief_date(row["brief_date"]),
        "themes": themes,
        "theses": [],  # filled by caller (depends on user)
        "language": effective_language,
    }


def _discover_signal_tier(confidence: float) -> str:
    """Map a thesis's freshest supporting-link confidence to a UI tier.

    Mirrors the Strong/Medium/Emerging vocabulary of build_story_suggestions:
    the card's prominence reflects how strongly its most recent signal supports
    it. A global thesis-level score (freshness + tailwind) would supersede this
    — see docs follow-up — but until that exists, the freshest support is the
    best evidence-driven proxy we have for an unowned thesis.
    """
    if confidence >= STORY_SUGGESTION_MIN_CONF:  # 0.85
        return "high"
    if confidence >= 0.75:
        return "medium"
    return "low"


# A thesis whose tickers are trending on the social leaderboard (Tier-1) gets
# this much added to its support_count when ranking Discover — enough to break
# ties and lift a moderately-supported trending belief above a slightly better
# supported non-trending one, but not enough to bury a dominant narrative under
# a single-link trending thesis.
DISCOVER_TREND_WEIGHT = 2


def build_discover(conn, user_id: str, language: str | None = None) -> list[dict]:
    """Homepage Discover: unowned active theses about today's trendy topics.

    This is a *demand-side daily curation* surface, fundamentally different from
    the per-story discovery pipeline (which is supply: it generates thesis
    inventory). Its job is to surface beliefs that today's most salient news
    keeps reinforcing. So we rank by *trendiness* — how many of today's brief
    stories strongly support the thesis, blended with whether the thesis's
    tickers are trending on the social leaderboard — not by link recency or
    owner_count.

    "Today's salient stories" = daily_briefs.source_story_ids for the latest
    brief, the same curated set the brief's themes are built from. A thesis
    supported by 6 of today's 14 brief stories is the dominant narrative; one
    with a single fresh link is not. Both manual and system theses are eligible.
    """
    # Tier-1 trending symbols feed a per-thesis overlap bonus. Empty set (table
    # absent / lane disabled) collapses the blend back to pure support_count.
    from agents.trending import tier1_symbols

    trending = sorted(tier1_symbols(conn))
    if trending:
        placeholders = ",".join("?" * len(trending))
        trending_hit_sql = f"""
            (SELECT COUNT(*) FROM entity_tickers et
             WHERE et.entity_type='thesis' AND et.entity_id=t.id
               AND et.symbol IN ({placeholders}))
        """
        trending_params: tuple = tuple(trending)
    else:
        trending_hit_sql = "0"
        trending_params = ()

    rows = conn.execute(
        f"""
        WITH today AS (
            SELECT j.value AS story_id
            FROM daily_briefs b, json_each(b.source_story_ids) j
            WHERE b.brief_date = (SELECT MAX(brief_date) FROM daily_briefs)
        )
        SELECT t.id,
               COUNT(DISTINCT l.story_id) AS support_count,
               MAX(l.confidence) AS top_conf,
               CASE WHEN {trending_hit_sql} > 0 THEN 1 ELSE 0 END AS trending_hit
        FROM theses t
        JOIN thesis_story_links l ON l.thesis_id = t.id
        JOIN today td ON td.story_id = l.story_id
        WHERE t.review_status = 'active'
          AND l.relation = 'supports'
          AND l.confidence >= ?
          AND t.id NOT IN (SELECT thesis_id FROM user_theses WHERE user_id = ?)
        GROUP BY t.id
        ORDER BY (support_count + ? * trending_hit) DESC,
                 support_count DESC, top_conf DESC, t.id ASC
        LIMIT 3
        """,
        (*trending_params, SUPPORT_STRONG_CONF, user_id, DISCOVER_TREND_WEIGHT),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        doc = load_thesis_md(r["id"], language)
        if not doc:
            continue
        # Reasoning = the rationale of this thesis's single strongest supporting
        # link among today's brief stories — i.e. why today's news backs it.
        rationale_row = conn.execute(
            """
            SELECT l.rationale
            FROM thesis_story_links l
            JOIN daily_briefs b ON b.brief_date = (
                SELECT MAX(brief_date) FROM daily_briefs)
            WHERE l.thesis_id = ?
              AND l.relation = 'supports'
              AND l.story_id IN (
                  SELECT j.value FROM json_each(b.source_story_ids) j)
            ORDER BY l.confidence DESC
            LIMIT 1
            """,
            (r["id"],),
        ).fetchone()
        # The thesis title is the user-facing "belief" string. It was prompt-
        # engineered to be the digest-grade name of the position (see the
        # title rules in src/thesis/discover.py). Sending core_thesis's first
        # sentence here ends up surfacing analyst-deck prose like "Hotter-
        # than-expected consumer and producer inflation data..." which is
        # exactly the register the title was designed to replace.
        out.append({
            "id": f"discover-{r['id']}",
            "title": doc.title,
            "belief": doc.title,
            "signal": _discover_signal_tier(r["top_conf"]),
            "signals": [],   # not stored yet
            "risks": [],
            "reasoning": (rationale_row["rationale"] if rationale_row else "") or "",
            "tickers": doc.tickers,
            "trackedThesisId": r["id"],
        })
    return out


def build_user_theses(
    conn,
    user_id: str,
    language: str | None = None,
) -> tuple[list[dict], list[str]]:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        """
        SELECT ut.thesis_id, ut.status, t.score, ut.created_at,
               (SELECT score FROM thesis_snapshots s
                 WHERE s.thesis_id = ut.thesis_id
                   AND s.snapshot_date < ?
                 ORDER BY s.snapshot_date DESC LIMIT 1) AS prev_score
        FROM user_theses ut
        JOIN theses t ON t.id = ut.thesis_id
        WHERE ut.user_id = ?
        ORDER BY ut.created_at ASC
        """,
        (today, user_id),
    ).fetchall()

    theses: list[dict] = []
    tracked_ids: list[str] = []
    for r in rows:
        thesis_id = r["thesis_id"]
        doc = load_thesis_md(thesis_id, language)
        if not doc:
            continue
        score = r["score"] if r["score"] is not None else 0
        prev = r["prev_score"] if r["prev_score"] is not None else score

        events = build_thesis_events(conn, thesis_id, user_id, limit=5)

        theses.append({
            "id": thesis_id,
            "title": doc.title,
            "belief": doc.core_thesis,
            "shortBelief": first_sentence(doc.core_thesis),
            "support": score,
            "prevSupport": prev,
            "trend": trend_for(score, prev, r["status"]),
            "status": r["status"],
            "tickers": doc.tickers,
            "created": (r["created_at"] or "")[:10],
            "updated": "",  # placeholder
            "supportHistory": [],  # placeholder
            "events": events,
            "evidence": [],  # placeholder
        })
        tracked_ids.append(thesis_id)
    return theses, tracked_ids


def _delta_lookup_for_thesis(conn, thesis_id: str) -> dict[str, int]:
    """Map snapshot_date → delta-from-prior-snapshot for one thesis.

    Walk the snapshot series ascending; each row's delta is its score minus
    the previous snapshot's score. The first snapshot has delta 0 (no prior
    state to compare against). Returned by date string keyed exactly as
    stored ('YYYY-MM-DD').
    """
    rows = conn.execute(
        """
        SELECT snapshot_date, score
        FROM thesis_snapshots
        WHERE thesis_id = ?
        ORDER BY snapshot_date ASC
        """,
        (thesis_id,),
    ).fetchall()
    out: dict[str, int] = {}
    prev: int | None = None
    for r in rows:
        score = r["score"] if r["score"] is not None else 0
        out[r["snapshot_date"]] = 0 if prev is None else int(score) - int(prev)
        prev = score
    return out


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _delta_for_event_date(
    delta_by_date: dict[str, int], event_date: str
) -> int:
    """Look up the daily-snapshot delta for an event's published date.

    Only matches strict ISO 'YYYY-MM-DD' dates — `news.published_at` is a free
    text column with mixed shapes ('Oct 14, 20', '21 hours a'); lexicographic
    comparison would incorrectly assign the latest snapshot's delta to those
    rows because letters sort above digits in ASCII. If the exact date has a
    snapshot, use it; otherwise fall back to the most recent ISO snapshot
    date <= event_date (handles weekends / missed pipeline runs). Returns 0
    when nothing is parseable.
    """
    if not event_date or not _ISO_DATE_RE.match(event_date):
        return 0
    if event_date in delta_by_date:
        return delta_by_date[event_date]
    earlier = [d for d in delta_by_date if d <= event_date]
    if not earlier:
        return 0
    return delta_by_date[max(earlier)]


def build_thesis_events(
    conn, thesis_id: str, user_id: str, limit: int = 5
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT l.story_id, l.relation, s.created_at, s.headline
        FROM thesis_story_links l
        JOIN story s ON s.id = l.story_id
        WHERE l.thesis_id = ? AND l.confidence >= ?
        ORDER BY s.created_at DESC
        LIMIT ?
        """,
        (thesis_id, SUPPORT_STRONG_CONF, limit),
    ).fetchall()
    delta_by_date = _delta_lookup_for_thesis(conn, thesis_id)
    out: list[dict] = []
    for r in rows:
        title = r["headline"]
        if not title:
            continue
        date_str = (r["created_at"] or "")[:10]
        out.append({
            "date": date_str,
            "title": title,
            "delta": _delta_for_event_date(delta_by_date, date_str),
            "type": "confirming" if r["relation"] == "supports" else "challenging",
            "story_id": r["story_id"],
        })

    # Price-move evidence: thesis horizons are multi-week+, so we surface
    # ≥3% over 1w or ≥5% over 1mo on tagged tickers as confirming/challenging
    # events alongside news. Breakdown is written by agents/score_theses.py.
    out.extend(_price_events_for_thesis(thesis_id))
    # Always order by most recent first. Stable sort preserves insertion
    # order on date ties: stories arrive in created-DESC order; price events
    # arrive in salience-DESC order. Both make sense as the within-date
    # tiebreaker for their respective lanes.
    out.sort(key=lambda e: e.get("date") or "", reverse=True)
    return out[:limit]


# Price-event thresholds (per docs/plan-scoring-system.md tailwind window):
# theses are medium-to-multi-week horizons, so noise floor is set above
# typical daily wiggle but well below one-month directional moves.
PRICE_EVENT_5D_THRESHOLD = 3.0
PRICE_EVENT_1MO_THRESHOLD = 5.0
PRICE_EVENT_CAP = 2
MESH_CACHE_DIR = ROOT / "db" / "mesh_cache"


def _load_latest_tailwind_breakdown() -> dict:
    """Load the most recent tailwind breakdown file, or {} if none exists."""
    if not MESH_CACHE_DIR.exists():
        return {}
    files = sorted(MESH_CACHE_DIR.glob("*_tailwind_breakdown.json"), reverse=True)
    if not files:
        return {}
    try:
        return json.loads(files[0].read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _price_events_for_thesis(thesis_id: str) -> list[dict]:
    breakdown = _load_latest_tailwind_breakdown()
    entry = breakdown.get(thesis_id)
    if not entry:
        return []
    as_of = entry.get("as_of") or ""
    scored: list[tuple[float, dict]] = []
    for t in entry.get("tickers", []):
        direction = t.get("direction")
        if direction not in ("bullish", "bearish"):
            continue
        symbol = t.get("raw_symbol") or t.get("symbol")
        if not symbol:
            continue
        ret_5d = t.get("ret_5d")
        ret_1mo = t.get("ret_1mo")
        candidates: list[tuple[str, float, float]] = []
        if isinstance(ret_5d, (int, float)) and abs(ret_5d) >= PRICE_EVENT_5D_THRESHOLD:
            candidates.append(("1w", float(ret_5d), abs(ret_5d) / PRICE_EVENT_5D_THRESHOLD))
        if isinstance(ret_1mo, (int, float)) and abs(ret_1mo) >= PRICE_EVENT_1MO_THRESHOLD:
            candidates.append(("1mo", float(ret_1mo), abs(ret_1mo) / PRICE_EVENT_1MO_THRESHOLD))
        if not candidates:
            continue
        window_label, ret_pct, salience = max(candidates, key=lambda c: c[2])
        signed = ret_pct if direction == "bullish" else -ret_pct
        ev_type = "confirming" if signed >= 0 else "challenging"
        sign = "+" if ret_pct >= 0 else ""
        scored.append((salience, {
            "date": as_of,
            "title": f"{symbol} {sign}{ret_pct:.1f}% ({window_label})",
            "delta": None,
            "type": ev_type,
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [ev for _, ev in scored[:PRICE_EVENT_CAP]]


HOME_FEED_LIMIT = 200
FEED_WEIGHTS = {
    "candidate_window_h": {"story": 72, "x": 48},
    "half_life_h": {"story": 14, "x": 32},
    "story_base": 0.45,
    "story_pub_weight": 0.05,
    "story_pub_cap": 4,
    # Heat-5 x base (0.70) sits between an uncorroborated story (0.45-0.50) and
    # a judged-match story for the user (>=0.90 with affinity) — the design's
    # golden ordering. At the original 0.30 + 0.14*heat, a heat-5 base of 1.00
    # outscored every story for every user and social monopolized the feed top.
    "x_base": 0.25,
    "x_heat_weight": 0.09,
    "trend_weight": 0.25,
    "trend_rank_span": 60,
    "judged_match_bonus": 0.35,
    "ticker_affinity_weight": 0.25,
    # Saturates at 2 hits so single-ticker items (every x topic, most stories)
    # earn a meaningful 0.125 for an owned ticker; at /3 it was a wash (0.083).
    "ticker_affinity_cap_hits": 2,
    "sector_affinity_weight": 0.10,
    "sector_affinity_cap_hits": 2,
    "affinity_cap": 0.45,
    "social_window_slots": 5,
    "social_window_cap": 2,
    # Bounds total per-ticker presence (any kind) near the top — the adjacency
    # rule only spaces same-ticker items, so an event with a story plus X
    # topics about it could still stack 4-5 cards into the first page.
    "ticker_window_slots": 10,
    "ticker_window_cap": 2,
}

# Minimum support-link confidence for a system thesis to surface as a global
# proposal on the story detail page. Matches the strong-support bar; see
# docs/design-thesis-creation.md (§Surfacing: Story Proposals).
STORY_SUGGESTION_MIN_CONF = 0.85


def _dominant_direction(directions: list[str]) -> str:
    """Collapse a thesis's per-ticker directions to one label for the card.

    Majority wins; ties and empties fall back to 'bullish' (the system never
    emits a directionless thesis, so a tie just means a paired long/short)."""
    bearish = sum(1 for d in directions if d == "bearish")
    bullish = sum(1 for d in directions if d == "bullish")
    return "bearish" if bearish > bullish else "bullish"


def build_story_suggestions(
    conn,
    story_id: str,
    language: str | None = None,
) -> list[dict]:
    """Global, read-only thesis proposals for the story detail page.

    Up to 3 active *system* theses linked to this story as strong supports
    (confidence >= STORY_SUGGESTION_MIN_CONF). No user predicate — these are
    global proposals, not bound to any user. See
    docs/design-thesis-creation.md (§Surfacing: Story Proposals).
    """
    rows = conn.execute(
        """
        SELECT t.id AS thesis_id, t.horizon_days, l.confidence
        FROM thesis_story_links l
        JOIN theses t ON t.id = l.thesis_id
        WHERE l.story_id = ?
          AND t.review_status = 'active'
          AND t.origin = 'system'
          AND l.relation = 'supports'
          AND l.confidence >= ?
        ORDER BY l.confidence DESC
        LIMIT 3
        """,
        (story_id, STORY_SUGGESTION_MIN_CONF),
    ).fetchall()
    if not rows:
        return []

    suggestions: list[dict] = []
    for row in rows:
        tid = row["thesis_id"]
        ticker_rows = conn.execute(
            "SELECT symbol, direction FROM entity_tickers"
            " WHERE entity_type = 'thesis' AND entity_id = ?",
            (tid,),
        ).fetchall()
        doc = load_thesis_md(tid, language)
        belief = doc.title if doc else ""
        suggestions.append({
            "thesisId": tid,
            "belief": belief,
            "tickers": [tr["symbol"] for tr in ticker_rows],
            "direction": _dominant_direction(
                [tr["direction"] for tr in ticker_rows]
            ),
            "horizon": row["horizon_days"],
        })
    return suggestions


@dataclass(slots=True)
class AffinityContext:
    user_tickers: set[str]
    user_sectors: set[str]
    judged_matches: set[str]


def _json_list(raw: str | None) -> list:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _json_dict(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_feed_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _instrument_sectors(conn: sqlite3.Connection, symbols: set[str]) -> dict[str, set[str]]:
    if not symbols or "sectors_json" not in _table_columns(conn, "instruments"):
        return {}
    placeholders = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"SELECT symbol, sectors_json FROM instruments WHERE symbol IN ({placeholders})",
        sorted(symbols),
    ).fetchall()
    return {
        row["symbol"]: {str(s) for s in _json_list(row["sectors_json"])}
        for row in rows
    }


def _thesis_sectors(conn: sqlite3.Connection, user_id: str) -> set[str]:
    if "sectors_json" not in _table_columns(conn, "theses"):
        return set()
    rows = conn.execute(
        """
        SELECT t.sectors_json
        FROM theses t
        JOIN user_theses ut ON ut.thesis_id = t.id
        WHERE ut.user_id = ? AND ut.status != 'resolved'
        """,
        (user_id,),
    ).fetchall()
    return {str(s) for row in rows for s in _json_list(row["sectors_json"])}


def _affinity_context(conn: sqlite3.Connection, user_id: str) -> AffinityContext:
    user_tickers = {
        str(row["symbol"])
        for row in conn.execute(
            """
            SELECT et.symbol
            FROM entity_tickers et
            JOIN user_theses ut ON ut.thesis_id = et.entity_id
            WHERE et.entity_type = 'thesis'
              AND ut.user_id = ?
              AND ut.status != 'resolved'
            """,
            (user_id,),
        ).fetchall()
    }
    try:
        user_tickers.update(
            str(row["symbol"])
            for row in conn.execute(
                "SELECT symbol FROM user_watchlist WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        pass

    instrument_sector_map = _instrument_sectors(conn, user_tickers)
    user_sectors = {
        sector
        for sectors in instrument_sector_map.values()
        for sector in sectors
    } | _thesis_sectors(conn, user_id)

    judged_matches = {
        str(row["story_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT l.story_id
            FROM thesis_story_links l
            JOIN user_theses ut ON ut.thesis_id = l.thesis_id
            WHERE ut.user_id = ?
              AND ut.status != 'resolved'
            """,
            (user_id,),
        ).fetchall()
    }
    return AffinityContext(
        user_tickers=user_tickers,
        user_sectors=user_sectors,
        judged_matches=judged_matches,
    )


def _latest_trend_ranks(conn: sqlite3.Connection) -> dict[str, int]:
    try:
        from agents.trending import latest_trends

        return {
            str(row["symbol"]): int(row["effective_rank"])
            for row in latest_trends(conn, limit=None)
            if row.get("in_registry")
        }
    except Exception:
        return {}


def _feed_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.id, s.cluster_id, s.created_at, s.headline, s.overview_json,
               s.claims_json, s.quotes_json, s.sectors_json, s.regions_json,
               s.images_json, COALESCE(s.kind, 'story') AS kind,
               s.heat, s.social_json,
               c.event_class, c.independent_pub_count
        FROM story s
        LEFT JOIN news_cluster c ON c.id = s.cluster_id
        WHERE (
            COALESCE(s.kind, 'story') = 'story'
            AND s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-72 hours')
            AND s.id NOT IN (
              SELECT story_id FROM story_quality_label
              WHERE label IN ('unclear', 'no_value')
            )
        ) OR (
            s.kind = 'x'
            AND s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-48 hours')
        )
        ORDER BY s.created_at DESC
        LIMIT 500
        """
    ).fetchall()


def _feed_maps(
    conn: sqlite3.Connection,
    story_ids: list[str],
) -> tuple[dict[str, list[str]], dict[str, list[dict]]]:
    story_ticker_map: dict[str, list[str]] = {sid: [] for sid in story_ids}
    story_matches: dict[str, list[dict]] = {sid: [] for sid in story_ids}
    if not story_ids:
        return story_ticker_map, story_matches
    placeholders = ",".join("?" * len(story_ids))
    for tr in conn.execute(
        f"SELECT entity_id, symbol FROM entity_tickers"
        f" WHERE entity_type = 'story' AND entity_id IN ({placeholders})",
        story_ids,
    ).fetchall():
        story_ticker_map.setdefault(tr["entity_id"], []).append(tr["symbol"])
    for lr in conn.execute(
        f"SELECT story_id, thesis_id, relation FROM thesis_story_links"
        f" WHERE story_id IN ({placeholders})",
        story_ids,
    ).fetchall():
        story_matches.setdefault(lr["story_id"], []).append({
            "thesisId": lr["thesis_id"],
            "direction": lr["relation"],
        })
    return story_ticker_map, story_matches


def _score_candidate(
    candidate: dict,
    *,
    context: AffinityContext,
    trend_ranks: dict[str, int],
    now: datetime,
) -> None:
    kind = candidate["kind"]
    created_at = _parse_feed_ts(candidate["created_at"])
    age_h = max(0, math.floor((now - created_at).total_seconds() / 3600))
    half_life = FEED_WEIGHTS["half_life_h"][kind]
    freshness = 0.5 ** (age_h / half_life)
    if kind == "x":
        base = FEED_WEIGHTS["x_base"] + FEED_WEIGHTS["x_heat_weight"] * int(candidate.get("heat") or 0)
    else:
        pubs = min(int(candidate.get("independent_pub_count") or 0), FEED_WEIGHTS["story_pub_cap"])
        base = FEED_WEIGHTS["story_base"] + FEED_WEIGHTS["story_pub_weight"] * pubs
    trend_raw = max(
        (
            1 - (trend_ranks[symbol] - 1) / FEED_WEIGHTS["trend_rank_span"]
            for symbol in candidate["raw_tickers"]
            if symbol in trend_ranks
        ),
        default=0.0,
    )
    trend = FEED_WEIGHTS["trend_weight"] * max(0.0, trend_raw)
    ticker_hits = len(candidate["raw_tickers"] & context.user_tickers)
    sector_hits = len(candidate["sector_set"] & context.user_sectors)
    affinity = (
        (FEED_WEIGHTS["judged_match_bonus"] if candidate["id"] in context.judged_matches else 0.0)
        + FEED_WEIGHTS["ticker_affinity_weight"]
        * min(ticker_hits, FEED_WEIGHTS["ticker_affinity_cap_hits"])
        / FEED_WEIGHTS["ticker_affinity_cap_hits"]
        + FEED_WEIGHTS["sector_affinity_weight"]
        * min(sector_hits, FEED_WEIGHTS["sector_affinity_cap_hits"])
        / FEED_WEIGHTS["sector_affinity_cap_hits"]
    )
    affinity = min(affinity, FEED_WEIGHTS["affinity_cap"])
    score = (base + trend + affinity) * freshness
    candidate["score"] = score
    candidate["explain"] = {
        "kind": kind,
        "freshness": round(freshness, 4),
        "base": round(base, 4),
        "trend": round(trend, 4),
        "affinity": round(affinity, 4),
        "score": round(score, 4),
        "demotions": 0,
    }


def _lead_ticker(candidate: dict) -> str | None:
    tickers = sorted(candidate["raw_tickers"])
    return tickers[0] if tickers else None


def _violates_composition(selected: list[dict], candidate: dict) -> bool:
    lead = _lead_ticker(candidate)
    if selected:
        last = selected[-1]
        if lead and _lead_ticker(last) == lead:
            return True
    if lead:
        window = selected[-(FEED_WEIGHTS["ticker_window_slots"] - 1):]
        ticker_count = sum(1 for item in window if _lead_ticker(item) == lead) + 1
        if ticker_count > FEED_WEIGHTS["ticker_window_cap"]:
            return True
    if candidate["kind"] == "x":
        window = selected[-(FEED_WEIGHTS["social_window_slots"] - 1):]
        social_count = sum(1 for item in window if item["kind"] == "x") + 1
        if social_count > FEED_WEIGHTS["social_window_cap"]:
            return True
    return False


def _compose_candidates(candidates: list[dict], *, limit: int) -> list[dict]:
    pool = sorted(
        candidates,
        key=lambda item: (
            item["score"],
            _parse_feed_ts(item["created_at"]),
        ),
        reverse=True,
    )
    selected: list[dict] = []
    while pool and len(selected) < limit:
        pick_idx: int | None = None
        for idx, candidate in enumerate(pool):
            if _violates_composition(selected, candidate):
                candidate["explain"]["demotions"] += 1
                continue
            pick_idx = idx
            break
        if pick_idx is None:
            pick_idx = 0
            pool[pick_idx]["explain"]["demotions"] += 1
        selected.append(pool.pop(pick_idx))
    return selected


def build_feed(
    conn,
    user_id: str,
    *,
    explain: bool = False,
    now: datetime | None = None,
    language: str | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Rank and compose a mixed story/social home feed."""
    now = now or datetime.now(timezone.utc)
    rows = _feed_candidates(conn)
    story_ids = [row["id"] for row in rows]
    story_ticker_map, story_matches = _feed_maps(conn, story_ids)
    all_symbols = {
        symbol for symbols in story_ticker_map.values() for symbol in symbols
    }
    instrument_sectors = _instrument_sectors(conn, all_symbols)
    context = _affinity_context(conn, user_id)
    trend_ranks = _latest_trend_ranks(conn)
    asset_class_by_symbol: dict[str, str] = {
        r["symbol"]: r["asset_class"]
        for r in conn.execute("SELECT symbol, asset_class FROM instruments").fetchall()
    }

    candidates: list[dict] = []
    for row in rows:
        raw_tickers = set(story_ticker_map.get(row["id"], []))
        kind = row["kind"] or "story"
        if kind == "x":
            sector_set = {
                sector
                for symbol in raw_tickers
                for sector in instrument_sectors.get(symbol, set())
            }
        else:
            sector_set = {str(s) for s in _json_list(row["sectors_json"])}
        candidate = {
            "id": row["id"],
            "row": row,
            "kind": kind,
            "created_at": row["created_at"] or "",
            "raw_tickers": raw_tickers,
            "sector_set": sector_set,
            "heat": row["heat"],
            "independent_pub_count": int(row["independent_pub_count"] or 0),
        }
        _score_candidate(candidate, context=context, trend_ranks=trend_ranks, now=now)
        candidates.append(candidate)

    sources_meta: dict[str, dict] = {}

    def _attach_sources(entries: list[tuple[str, str]]) -> list[str]:
        slugs: list[str] = []
        for name, url in entries:
            slug = slugify(name)
            if slug not in sources_meta:
                sources_meta[slug] = {
                    "name": name,
                    "color": color_for(slug),
                    "domain": "x.com" if slug == "x" else domain_for(name, url),
                }
            slugs.append(slug)
        return slugs

    items: list[dict] = []
    for candidate in _compose_candidates(candidates, limit=HOME_FEED_LIMIT):
        row = candidate["row"]
        kind = candidate["kind"]
        overview = _json_list(row["overview_json"])
        story_doc = load_story_md(row["id"], language) if kind == "story" else None
        overview_texts = (
            story_doc.overview_bullets
            if story_doc
            else [
                str(b.get("text") or "").strip()
                for b in overview
                if isinstance(b, dict)
            ]
        )
        summary = " ".join(overview_texts[:3])
        raw_tickers = story_ticker_map.get(row["id"], [])
        display_tickers = filter_for_display(
            raw_tickers,
            asset_class_by_symbol=asset_class_by_symbol,
        )
        sectors = sorted(candidate["sector_set"]) if kind == "x" else [str(s) for s in _json_list(row["sectors_json"])]
        regions = [] if kind == "x" else [str(region) for region in _json_list(row["regions_json"])]
        if kind == "x":
            social = _json_dict(row["social_json"])
            item = {
                "id": row["id"],
                "kind": "x",
                "language": "en",
                "publishedAt": row["created_at"] or "",
                "headline": row["headline"],
                "sources": _attach_sources([("X", "https://x.com")]),
                "summary": summary,
                "tickers": display_tickers,
                "matches": story_matches.get(row["id"], []),
                "suggestions": [],
                "eventClass": "social",
                "sectors": sectors,
                "regions": regions,
                "independentPublishers": 0,
                "overview": [
                    {
                        "text": str(b.get("text") or "").strip(),
                        "sourceDocIds": [],
                    }
                    for b in overview
                    if isinstance(b, dict)
                ],
                "heat": int(row["heat"] or 0),
                "bullAngle": str(social.get("bull_angle") or ""),
                "bearAngle": str(social.get("bear_angle") or ""),
                "tweets": social.get("tweets") if isinstance(social.get("tweets"), list) else [],
            }
        else:
            cited_ids = cited_news_ids(
                row["overview_json"], row["claims_json"], row["quotes_json"]
            )
            source_entries = [
                (entry["publisher"], entry["source_url"])
                for entry in cited_publishers(conn, cited_ids)
            ]
            item = {
                "id": row["id"],
                "kind": "story",
                "language": markdown_path_language(story_doc.path)
                if story_doc
                else "en",
                "publishedAt": row["created_at"] or "",
                "headline": story_doc.title if story_doc else row["headline"],
                "sources": _attach_sources(source_entries),
                "summary": summary,
                "thumbnail": _first_small_thumbnail(row["images_json"]),
                "tickers": display_tickers,
                "matches": story_matches.get(row["id"], []),
                "suggestions": build_story_suggestions(conn, row["id"], language),
                "eventClass": row["event_class"] or "",
                "sectors": sectors,
                "regions": regions,
                "independentPublishers": int(row["independent_pub_count"] or 0),
                "overview": [
                    {
                        "text": text,
                        "sourceDocIds": [
                            str(x)
                            for x in (
                                overview[idx].get("source_doc_ids")
                                if idx < len(overview) and isinstance(overview[idx], dict)
                                else []
                            )
                            or []
                        ],
                    }
                    for idx, text in enumerate(overview_texts)
                    if text
                ],
            }
        if explain:
            item["explain"] = candidate["explain"]
        items.append(item)
    return items, sources_meta


# mime preference for <picture>/<source>: best compression first, JPEG last as
# the universally-decodable <img src> fallback.
_THUMBNAIL_MIME_ORDER = ("image/avif", "image/webp", "image/jpeg")


def _first_small_thumbnail(raw: str | None) -> dict | None:
    """Build the home-feed card thumbnail from the first image's `small` variants.

    Returns a `{width, height, variants:[{mime, url}, …]}` dict ordered avif →
    webp → jpeg, mirroring the shape `/api/news/{id}` uses for the detail page.
    The card renders this as `<picture>` with `<source type=...>` per format
    and a JPEG `<img src>` fallback — every browser gets the best format it
    can decode without risking a broken `<img>` on AVIF-incapable clients.

    Returns None when no image has a usable small JPEG (the required fallback).
    """
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    for image in payload:
        if not isinstance(image, dict):
            continue
        small_variants = [
            v
            for v in (image.get("variants") or [])
            if isinstance(v, dict)
            and v.get("size") == "small"
            and isinstance(v.get("url"), str)
            and v["url"].strip()
            and isinstance(v.get("mime"), str)
        ]
        # Require a JPEG fallback — without it the <img src=…> can't decode on
        # AVIF/WebP-only payloads.
        if not any(v["mime"] == "image/jpeg" for v in small_variants):
            continue
        # Intrinsic dims are identical across formats at the same size.
        dim_source = small_variants[0]
        ordered = sorted(
            small_variants,
            key=lambda v: _THUMBNAIL_MIME_ORDER.index(v["mime"])
            if v["mime"] in _THUMBNAIL_MIME_ORDER
            else len(_THUMBNAIL_MIME_ORDER),
        )
        return {
            "width": int(dim_source.get("width") or 0) or None,
            "height": int(dim_source.get("height") or 0) or None,
            "variants": [{"mime": v["mime"], "url": v["url"]} for v in ordered],
        }
    return None


def build_home(
    user_id: str,
    *,
    explain: bool = False,
    language: str = "en",
) -> dict:
    """`language` must already be normalized (`normalize_language`)."""
    with db() as conn:
        brief = build_brief(conn, language)
        brief["theses"] = build_discover(conn, user_id, language)
        theses, tracked_ids = build_user_theses(conn, user_id, language)
        stories, sources = build_feed(
            conn,
            user_id,
            explain=explain,
            language=language,
        )
    # The response key stays `news` for frontend stability — stories are
    # what the feed surfaces, but the public-facing key is the user's
    # mental model.
    return {
        "brief": brief,
        "sources": sources,
        "news": stories,
        "theses": theses,
        "trackedThesisIds": tracked_ids,
        "language": language,
    }


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/api/home":
            self.send_response(404)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return
        qs = parse_qs(url.query)
        user_id = (qs.get("user_id") or ["user_1"])[0]
        explain = (qs.get("explain") or ["0"])[0].lower() in {"1", "true", "yes"}
        language = normalize_language((qs.get("locale") or ["en"])[0])
        try:
            payload = build_home(user_id, explain=explain, language=language)
        except Exception as e:
            self.send_response(500)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[api] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    print(f"hf-workbench api → http://localhost:{PORT}/api/home")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

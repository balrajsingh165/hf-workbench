"""Daily Brief pipeline — fetch, synthesize, verify, persist.

See `docs/plan-daily-brief.md`. Pure functions (no CLI glue); the agent
entrypoint at `agents/daily_brief.py` orchestrates them.

Inputs come from the `story` table — synthesized cluster rows produced
by `agents.route_news_clusters` + `src/story/synthesis.py`. The brief
never reads raw firehose `news` rows directly.

Windows:
  • Fetch stories: 48h (asymmetric — carry overnight/weekend signal).
  • Rank theses against brief: 24h (see `ranking.py`).

LLM:
  • One `gemini-3-flash-preview` call, `thinking_level="medium"`,
    structured JSON output (Gemini 3 default temperature — not customized).
    Chosen over Pro after an A/B test (see `scripts/brief_model_ab.py`):
    same theme selection, same sentiment, same source groupings,
    ~3× faster / cheaper.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.brief.movers import MoverReading, fetch_movers
from src.clients.gemini import GEMINI_3_1_PRO_PREVIEW, GEMINI_3_FLASH_PREVIEW, generate_text_with_retry
from src.i18n_translate import write_translation_sidecars
from src.instruments.resolver import to_display


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "db" / "hf.db"
BRIEFS_DIR = ROOT / "global" / "briefs"
MESH_CACHE_DIR = ROOT / "db" / "mesh_cache"
PROMPT_PATH = ROOT / "agents" / "prompts" / "daily_brief.md"

FETCH_WINDOW_HOURS = 48
TOP_N_STORIES = 15
MACRO_SECTOR_BOOST = {
    "Macro", "Geopolitics", "Energy",
    "Government & Policy", "Defense", "National Security",
    "Artificial Intelligence", "Semiconductors", "Financial Services",
}


# ── Stage 1 · Fetch ────────────────────────────────────────────────


@dataclass(slots=True)
class StoryInput:
    id: str
    created_at: str
    headline: str
    overview: str
    tickers: list[str]
    sectors: list[str]


def _overview_text_from_json(overview_json: str) -> str:
    """Flatten the structured `story.overview_json` claim list into plain text.

    `overview_json` is `[{text, source_doc_ids, confidence}, ...]`. The brief
    only needs the human-readable claim lines for the LLM context block.
    """
    try:
        claims = json.loads(overview_json or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(claims, list):
        return ""
    lines: list[str] = []
    for c in claims:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            t = c["text"].strip()
            if t:
                lines.append(f"- {t}")
    return "\n".join(lines)


def fetch_story_inputs(
    target_date: date,
    *,
    db_path: Path = DB_PATH,
    top_n: int = TOP_N_STORIES,
    window_hours: int = FETCH_WINDOW_HOURS,
) -> list[StoryInput]:
    """Pull recent stories, rank by (recency → sector boost → link-count tiebreak).

    Freshness wins, sector weight boosts macro/geopolitics/AI/energy stories,
    thesis_story_links count breaks ties.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # Window: `target_date - window_hours` up to end of `target_date`.
        window_start_modifier = f"-{window_hours} hours"
        rows = conn.execute(
            """
            SELECT s.id, s.created_at, s.headline, s.overview_json, s.sectors_json,
                   COALESCE(link_counts.n_links, 0) AS link_count
              FROM story s
              LEFT JOIN (
                SELECT story_id, COUNT(*) AS n_links
                  FROM thesis_story_links
                 GROUP BY story_id
              ) link_counts ON link_counts.story_id = s.id
             WHERE datetime(s.created_at) >= datetime(?, ?)
               AND date(s.created_at) <= date(?)
               AND s.kind = 'story'
             ORDER BY s.created_at DESC
            """,
            (target_date.isoformat(), window_start_modifier, target_date.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        sectors = json.loads(row["sectors_json"] or "[]")
        sector_boost = sum(1 for s in sectors if s in MACRO_SECTOR_BOOST)
        days_ago = (target_date - date.fromisoformat(row["created_at"][:10])).days
        score = -days_ago * 10 + sector_boost + (row["link_count"] or 0) * 0.01
        scored.append((score, row))

    scored.sort(key=lambda t: t[0], reverse=True)

    selected = scored[:top_n]
    selected_ids = [row["id"] for _, row in selected]

    tickers_by_story: dict[str, list[str]] = {sid: [] for sid in selected_ids}
    if selected_ids:
        conn2 = sqlite3.connect(db_path, timeout=30)
        try:
            placeholders = ",".join("?" * len(selected_ids))
            tick_rows = conn2.execute(
                f"SELECT entity_id, symbol FROM entity_tickers"
                f" WHERE entity_type = 'story' AND entity_id IN ({placeholders})",
                selected_ids,
            ).fetchall()
            for eid, sym in tick_rows:
                tickers_by_story[eid].append(sym)
        finally:
            conn2.close()

    out: list[StoryInput] = []
    for _, row in selected:
        out.append(StoryInput(
            id=row["id"],
            created_at=row["created_at"],
            headline=row["headline"] or f"(headline missing for {row['id']})",
            overview=_overview_text_from_json(row["overview_json"]),
            tickers=tickers_by_story.get(row["id"], []),
            sectors=json.loads(row["sectors_json"] or "[]"),
        ))
    return out


def fetch_yesterday_themes(
    target_date: date,
    *,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return parsed `themes_json` from the previous day's brief, or []."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT themes_json FROM daily_briefs "
            "WHERE brief_date < ? ORDER BY brief_date DESC LIMIT 1",
            (target_date.isoformat(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return []
    try:
        return json.loads(row["themes_json"])
    except json.JSONDecodeError:
        return []


# ── Stage 2 · Synthesize ───────────────────────────────────────────


@dataclass(slots=True)
class SynthesizedBrief:
    themes: list[dict[str, Any]]             # [{id, text, source_story_ids}]
    model_version: str
    source_story_ids: list[str] = field(default_factory=list)


_THEMES_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "minItems": 4,
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "source_story_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "required": ["id", "text", "source_story_ids"],
            },
        },
    },
    "required": ["themes"],
}


def _format_story_block(items: list[StoryInput]) -> str:
    lines: list[str] = []
    for it in items:
        lines.append(f"### {it.id} · {it.created_at[:10]}")
        lines.append(f"**Headline:** {it.headline}")
        if it.tickers:
            lines.append(f"**Tickers:** {', '.join(it.tickers)}")
        if it.sectors:
            lines.append(f"**Sectors:** {', '.join(it.sectors)}")
        if it.overview:
            lines.append("")
            lines.append(it.overview)
        lines.append("")
    return "\n".join(lines).strip()


def _format_movers_block(movers: list[MoverReading]) -> str:
    rows: list[str] = []
    for r in movers:
        price = f"{r.price:.2f}" if r.price is not None else "—"
        pct = f"{r.pct_change:+.2f}%" if r.pct_change is not None else "—"
        label = to_display(r.spec.symbol, "short")
        rows.append(f"- {label} ({r.spec.asset_class}) · price={price} · daily={pct}")
    return "\n".join(rows)


def _format_yesterday_themes(themes: list[dict[str, Any]]) -> str:
    if not themes:
        return "_(no prior brief)_"
    lines: list[str] = []
    for t in themes:
        tid = t.get("id", "??")
        text = t.get("text", "")
        # Source IDs are needed by the model on quiet days to cite yesterday's
        # provenance for a carry-forward theme. The verifier whitelists these
        # same IDs in `verify_provenance(..., yesterday_source_ids=...)`.
        sids = t.get("source_story_ids") or t.get("source_news_ids") or []
        if sids:
            lines.append(f"- [{tid}] {text} (sources: {', '.join(sids)})")
        else:
            lines.append(f"- [{tid}] {text}")
    return "\n".join(lines)


def _load_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"Prompt not found at {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _normalize_story_id(raw: Any) -> str:
    """Accept either canonical `story_074` or bare `074` / `74` from the model.

    For backward provenance support, also pass through any `news_NNN` id
    untouched so carry-forward themes citing yesterday's brief (which may
    still reference the legacy news IDs) remain valid.
    """
    s = str(raw).strip()
    if s.startswith("story_") or s.startswith("news_"):
        return s
    if s.isdigit():
        return f"story_{int(s):03d}"
    return s


def synthesize(
    target_date: date,
    stories: list[StoryInput],
    movers: list[MoverReading],
    yesterday_themes: list[dict[str, Any]],
    *,
    model: str = GEMINI_3_FLASH_PREVIEW,
    thinking_level: str | None = "medium",
) -> SynthesizedBrief:
    """Single LLM call. Returns parsed themes.

    Default: Flash with `thinking_level="medium"` — strategist-quality
    synthesis at ~7s latency (vs ~22s for Pro). `thinking_level` is
    model-dependent: Flash supports minimal/low/medium/high; Pro supports
    low/medium/high.
    """
    prompt_body = _load_prompt()
    user_contents = (
        f"# Date\n{target_date.isoformat()}\n\n"
        f"# Yesterday's themes\n{_format_yesterday_themes(yesterday_themes)}\n\n"
        f"# Today's stories\n{_format_story_block(stories)}\n\n"
        f"# Today's market movers\n{_format_movers_block(movers)}\n"
    )
    attempts: list[tuple[str, str | None]] = [(model, thinking_level), (model, thinking_level)]
    if model != GEMINI_3_1_PRO_PREVIEW:
        # Final safety net for scheduled runs: Flash is the cheap default, but
        # a once-daily brief should not fail just because Flash emitted partial
        # JSON. Pro is only used after two parse-level failures.
        attempts.append((GEMINI_3_1_PRO_PREVIEW, None))

    last_text = ""
    last_error: json.JSONDecodeError | None = None
    result_model = model
    for attempt, (attempt_model, attempt_thinking) in enumerate(attempts, start=1):
        system_instruction = prompt_body
        if attempt > 1:
            system_instruction += (
                "\n\nRetry instruction: the previous response was not valid "
                "complete JSON. Return one complete JSON object only."
            )
        result = generate_text_with_retry(
            user_contents,
            model=attempt_model,
            system_instruction=system_instruction,
            thinking_level=attempt_thinking,
            response_mime_type="application/json",
            response_json_schema=_THEMES_SCHEMA,
            max_output_tokens=8192,
        )
        result_model = result.model
        last_text = result.text
        try:
            parsed = json.loads(result.text)
            break
        except json.JSONDecodeError as e:
            last_error = e
    else:
        raise RuntimeError(f"LLM returned non-JSON after retry: {last_error}\n---\n{last_text[:500]}")

    themes_raw = parsed.get("themes") or []
    if not isinstance(themes_raw, list) or not (4 <= len(themes_raw) <= 6):
        raise RuntimeError(f"LLM returned {len(themes_raw) if isinstance(themes_raw, list) else '?'} themes; must be 4–6")

    # Normalize theme IDs to `01`, `02`, ... regardless of what the LLM chose.
    themes: list[dict[str, Any]] = []
    all_source_ids: list[str] = []
    for i, t in enumerate(themes_raw, start=1):
        if not isinstance(t, dict):
            raise RuntimeError(f"Theme {i} is not an object: {t!r}")
        text = (t.get("text") or "").strip()
        sids = t.get("source_story_ids") or []
        if not text:
            raise RuntimeError(f"Theme {i} has empty text")
        if not isinstance(sids, list) or not sids:
            raise RuntimeError(f"Theme {i} has no source_story_ids")
        theme = {
            "id": f"{i:02d}",
            "text": text,
            "source_story_ids": [_normalize_story_id(s) for s in sids],
        }
        themes.append(theme)
        for sid in theme["source_story_ids"]:
            if sid not in all_source_ids:
                all_source_ids.append(sid)

    return SynthesizedBrief(
        themes=themes,
        model_version=result_model,
        source_story_ids=all_source_ids,
    )


# ── Stage 3 · Verify provenance ────────────────────────────────────


@dataclass(slots=True)
class ProvenanceIssue:
    theme_id: str
    kind: str                 # unknown_source | no_sources | unsupported_claim | temporal_anchor
    detail: str


# Calendar phrases that imply "what happened on day X" framing. Themes are
# meant to read as standing market dynamics, not a daily recap (see prompt
# Voice). Word-boundary regex; case-insensitive.
_TEMPORAL_ANCHOR_RE = re.compile(
    r"\b(today|yesterday|this morning|overnight|last night|tonight|this week|last week|earlier today)\b",
    re.IGNORECASE,
)


def verify_provenance(
    brief: SynthesizedBrief,
    stories: list[StoryInput],
    yesterday_source_ids: set[str] | None = None,
) -> list[ProvenanceIssue]:
    """Hard checks (1) and (2); soft check (3) returned as `unsupported_claim`
    so the caller can decide whether to warn or reject.

    `yesterday_source_ids` (optional): IDs from yesterday's brief sources that
    are also valid provenance for carry-forward themes on quiet days. The
    prompt grants the LLM autonomy to carry forward up to 2 themes when
    today's stories are thin (see `agents/prompts/daily_brief.md`).
    """
    fetched_ids = {s.id for s in stories}
    valid_ids = fetched_ids | (yesterday_source_ids or set())
    stories_by_id = {s.id: s for s in stories}
    issues: list[ProvenanceIssue] = []

    for theme in brief.themes:
        tid = theme["id"]
        sids = theme["source_story_ids"]
        if not sids:
            issues.append(ProvenanceIssue(tid, "no_sources", "theme cites no stories"))
            continue

        # (1) Every cited id must come from today's fetch OR yesterday's brief.
        unknown = [s for s in sids if s not in valid_ids]
        if unknown:
            issues.append(ProvenanceIssue(
                tid, "unknown_source",
                f"cited ids not in today's fetch: {unknown}",
            ))

        # (3) Soft: at least one ticker / numeric datapoint from the cited
        # stories must appear in the theme text.
        known_sids = [s for s in sids if s in stories_by_id]
        theme_text = theme["text"]
        substantive = False
        if re.search(r"\$?\d[\d,]*\.?\d*(?:%|bp|bps)?", theme_text):
            substantive = True
        if not substantive:
            for sid in known_sids:
                for ticker in stories_by_id[sid].tickers:
                    if ticker and ticker.replace("^", "") in theme_text:
                        substantive = True
                        break
                if substantive:
                    break
        if not substantive:
            issues.append(ProvenanceIssue(
                tid, "unsupported_claim",
                "theme contains no numeric datapoint or cited ticker",
            ))

        # (4) Soft: themes must read as standing market dynamics, not a daily
        # recap. The frontend renders `theme.text` verbatim, so calendar
        # phrasing surfaces directly to users.
        anchor = _TEMPORAL_ANCHOR_RE.search(theme_text)
        if anchor:
            issues.append(ProvenanceIssue(
                tid, "temporal_anchor",
                f"theme uses calendar phrasing ('{anchor.group(0)}'); rewrite as standing dynamic",
            ))

    return issues


# ── Stage 3 · Persist ──────────────────────────────────────────────


def _render_markdown(
    target_date: date,
    brief: SynthesizedBrief,
    movers: list[MoverReading],
    generated_at: datetime,
) -> str:
    generated_at_utc = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        f"# Daily Market Brief — {target_date.strftime('%A, %B %d, %Y')}",
        "",
        f"- **Generated At:** {generated_at_utc}",
        f"- **Model:** {brief.model_version}",
        "",
        "## Themes",
        "",
    ]
    for t in brief.themes:
        lines.append(f"**{t['id']}.** {t['text']}")
        lines.append(f"  _Sources: {', '.join(t['source_story_ids'])}_")
        lines.append("")
    lines.extend(["## Market Movers", "", "| Mover | Class | Price | Daily % |", "|---|---|---|---|"])
    for r in movers:
        price = f"{r.price:.2f}" if r.price is not None else "—"
        pct = f"{r.pct_change:+.2f}%" if r.pct_change is not None else "—"
        label = to_display(r.spec.symbol, "short")
        lines.append(f"| {label} | {r.spec.asset_class} | {price} | {pct} |")
    lines.append("")
    return "\n".join(lines)


def persist(
    target_date: date,
    brief: SynthesizedBrief,
    movers: list[MoverReading],
    *,
    db_path: Path = DB_PATH,
    briefs_dir: Path = BRIEFS_DIR,
) -> Path:
    """Upsert DB rows + write markdown archive. Returns archive path."""
    generated_at = datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO daily_briefs
                (brief_date, generated_at, themes_json,
                 source_story_ids, model_version)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(brief_date) DO UPDATE SET
                  generated_at = excluded.generated_at,
                  themes_json = excluded.themes_json,
                  source_story_ids = excluded.source_story_ids,
                  model_version = excluded.model_version
                """,
                (
                    target_date.isoformat(),
                    generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    json.dumps(brief.themes),
                    json.dumps(brief.source_story_ids),
                    brief.model_version,
                ),
            )
            # Full replace of movers for this date (no per-row reconciliation).
            conn.execute("DELETE FROM daily_movers WHERE brief_date = ?", (target_date.isoformat(),))
            for r in movers:
                conn.execute(
                    """
                    INSERT INTO daily_movers
                    (brief_date, rank, symbol, label, asset_class, pct_change, price)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_date.isoformat(),
                        r.spec.rank,
                        r.spec.symbol,
                        to_display(r.spec.symbol, "short"),
                        r.spec.asset_class,
                        r.pct_change,
                        r.price,
                    ),
                )
    finally:
        conn.close()

    briefs_dir.mkdir(parents=True, exist_ok=True)
    md_path = briefs_dir / f"{target_date.isoformat()}.md"
    md_path.write_text(_render_markdown(target_date, brief, movers, generated_at), encoding="utf-8")
    try:
        write_translation_sidecars(
            md_path,
            entity_type="brief",
            entity_id=target_date.isoformat(),
            db_path=db_path,
        )
    except Exception as exc:
        print(f"brief: failed to translate {target_date} sidecars: {exc}", file=sys.stderr)
    return md_path


__all__ = [
    "FETCH_WINDOW_HOURS",
    "TOP_N_STORIES",
    "StoryInput",
    "ProvenanceIssue",
    "SynthesizedBrief",
    "fetch_movers",
    "fetch_story_inputs",
    "fetch_yesterday_themes",
    "persist",
    "synthesize",
    "verify_provenance",
]

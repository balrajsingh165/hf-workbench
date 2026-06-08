#!/usr/bin/env python
"""Business health metrics over hf-workbench pipeline state.

This is intentionally read-only. It turns existing DB tables plus
`logs/hf-pipeline-metrics.jsonl` into a compact health payload and local
alert candidates. A later notifier can call this with `--json --fail-on-alert`.

Examples:
  uv run python scripts/hf_health.py
  uv run python scripts/hf_health.py --json
  uv run python scripts/hf_health.py --append --fail-on-alert
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline_metrics import METRICS_PATH

DB_PATH = Path(os.getenv("HF_DB_PATH") or ROOT / "db" / "hf.db")
PIPELINE_METRICS_PATH = METRICS_PATH
HEALTH_METRICS_PATH = ROOT / "logs" / "hf-health-metrics.jsonl"


@dataclass(slots=True)
class Finding:
    severity: str
    code: str
    message: str
    value: Any = None
    threshold: Any = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def age_minutes(value: Any) -> float | None:
    dt = parse_ts(value)
    if dt is None:
        return None
    return round((utc_now() - dt).total_seconds() / 60.0, 2)


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"DB not found at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return row[0]


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        one(
            conn,
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
    )


def read_jsonl(path: Path, limit: int = 50_000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                out.append(value)
                if len(out) > limit:
                    out = out[-limit:]
    return out


def latest_event(events: list[dict[str, Any]], event_name: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event") == event_name:
            return event
    return None


def latest_event_for_run(
    events: list[dict[str, Any]],
    event_name: str,
    run_id: str | None,
) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event") != event_name:
            continue
        if run_id is None or event.get("run_id") == run_id:
            return event
    return None


def normalize_route_funnel(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Latest route_funnel, merging legacy split events when needed."""
    funnel = latest_event(events, "route_funnel")
    if funnel is None:
        return None
    if "snapshot" in funnel and "sources" in funnel:
        return funnel
    run_id = funnel.get("run_id")
    if "sources" not in funnel:
        sources = latest_event_for_run(events, "route_sources", run_id)
        if sources:
            funnel = {**funnel, "sources": {"news_articles": sources.get("news_articles") or {}}}
    if "snapshot" not in funnel:
        snapshot = latest_event_for_run(events, "pipeline_snapshot", run_id)
        if snapshot:
            funnel = {
                **funnel,
                "snapshot": {
                    k: v
                    for k, v in snapshot.items()
                    if k not in ("event", "ts", "run_id")
                },
            }
    return funnel


def consecutive_failures(events: list[dict[str, Any]], event_name: str) -> int:
    count = 0
    for event in reversed(events):
        if event.get("event") != event_name:
            continue
        if event.get("ok") is False:
            count += 1
            continue
        break
    return count


def parse_json_array(value: Any) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def collect_news_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    lane_sql = """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN headline IS NULL THEN 1 ELSE 0 END) AS sharp,
          SUM(CASE WHEN headline IS NOT NULL THEN 1 ELSE 0 END) AS firehose
        FROM news
        WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
    """
    metrics: dict[str, Any] = {
        "total": one(conn, "SELECT COUNT(*) FROM news") or 0,
        "last_created_at": one(conn, "SELECT MAX(created_at) FROM news"),
        "windows": {},
    }
    for label, window in (("1h", "-1 hour"), ("24h", "-1 day"), ("7d", "-7 day")):
        row = conn.execute(lane_sql, (window,)).fetchone()
        metrics["windows"][label] = {
            "total": int(row["total"] or 0),
            "sharp": int(row["sharp"] or 0),
            "firehose": int(row["firehose"] or 0),
        }

    metrics["missing_tickers_24h"] = rows(
        conn,
        """
        SELECT
          CASE WHEN n.headline IS NULL THEN 'sharp' ELSE 'firehose' END AS lane,
          COUNT(*) AS count
        FROM news n
        WHERE n.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
          AND NOT EXISTS (
            SELECT 1 FROM entity_tickers e
            WHERE e.entity_type = 'news' AND e.entity_id = n.id
          )
        GROUP BY lane
        ORDER BY lane
        """,
    )
    metrics["stories_missing_embeddings_24h"] = one(
        conn,
        """
        SELECT COUNT(*)
        FROM story s
        WHERE s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
          AND s.kind = 'story'
          AND NOT EXISTS (
            SELECT 1 FROM story_match_chunks m WHERE m.story_id = s.id
          )
        """,
    ) or 0
    metrics["stories_24h"] = one(
        conn,
        "SELECT COUNT(*) FROM story WHERE kind='story' AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')",
    ) or 0
    # Newest story by its displayed timestamp (story.created_at — the value the
    # home feed sorts on). Used to alert when the feed has gone stale.
    metrics["last_story_created_at"] = one(conn, "SELECT MAX(created_at) FROM story WHERE kind='story'")
    metrics["publishers_24h"] = rows(
        conn,
        """
        SELECT COALESCE(publisher, 'sharp') AS publisher, COUNT(*) AS count
        FROM news
        WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
        GROUP BY COALESCE(publisher, 'sharp')
        ORDER BY count DESC
        LIMIT 10
        """,
    )

    sector_counts: Counter[str] = Counter()
    empty_sectors_by_lane: Counter[str] = Counter()
    sector_rows_by_lane: Counter[str] = Counter()
    sector_rows = conn.execute(
        """
        SELECT
          CASE WHEN headline IS NULL THEN 'sharp' ELSE 'firehose' END AS lane,
          sectors_json
        FROM news
        WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
        """
    ).fetchall()
    for row in sector_rows:
        lane = str(row["lane"])
        sector_rows_by_lane[lane] += 1
        sectors = [str(v) for v in parse_json_array(row["sectors_json"]) if str(v).strip()]
        if not sectors:
            empty_sectors_by_lane[lane] += 1
        sector_counts.update(sectors)
    metrics["sectors_24h"] = dict(sector_counts.most_common())
    metrics["sector_rows_by_lane_24h"] = dict(sector_rows_by_lane)
    metrics["empty_sector_rows_by_lane_24h"] = dict(empty_sectors_by_lane)
    return metrics


def collect_thesis_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "by_review_status": rows(
            conn,
            "SELECT review_status, COUNT(*) AS count FROM theses GROUP BY review_status ORDER BY review_status",
        ),
        "active_user_theses": one(
            conn,
            "SELECT COUNT(*) FROM user_theses WHERE status != 'resolved'",
        ) or 0,
        "active_distinct_theses": one(
            conn,
            """
            SELECT COUNT(DISTINCT thesis_id)
            FROM user_theses
            WHERE status != 'resolved'
            """,
        ) or 0,
        "candidate_stuck_gt_7d": one(
            conn,
            """
            SELECT COUNT(*)
            FROM theses
            WHERE review_status = 'candidate'
              AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now', '-7 day')
            """,
        ) or 0,
        "active_without_match_chunks": one(
            conn,
            """
            SELECT COUNT(*)
            FROM (
              SELECT DISTINCT ut.thesis_id
              FROM user_theses ut
              WHERE ut.status != 'resolved'
            ) active
            WHERE NOT EXISTS (
              SELECT 1 FROM thesis_match_chunks tmc
              WHERE tmc.thesis_id = active.thesis_id
            )
            """,
        ) or 0,
        "active_without_links": one(
            conn,
            """
            SELECT COUNT(*)
            FROM (
              SELECT DISTINCT ut.thesis_id
              FROM user_theses ut
              WHERE ut.status != 'resolved'
            ) active
            WHERE NOT EXISTS (
              SELECT 1 FROM thesis_story_links l
              WHERE l.thesis_id = active.thesis_id
            )
            """,
        ) or 0,
    }
    return metrics


def collect_match_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "total": one(conn, "SELECT COUNT(*) FROM thesis_story_links") or 0,
        "updated_24h": one(
            conn,
            "SELECT COUNT(*) FROM thesis_story_links WHERE updated_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')",
        ) or 0,
        "by_relation_24h": rows(
            conn,
            """
            SELECT relation, COUNT(*) AS count, AVG(confidence) AS avg_confidence
            FROM thesis_story_links
            WHERE updated_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
            GROUP BY relation
            ORDER BY count DESC
            """,
        ),
        "by_source_24h": rows(
            conn,
            """
            SELECT source, COUNT(*) AS count
            FROM thesis_story_links
            WHERE updated_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
            GROUP BY source
            ORDER BY count DESC
            """,
        ),
        "stories_without_links_24h": one(
            conn,
            """
            SELECT COUNT(*)
            FROM story s
            WHERE s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
              AND s.kind = 'story'
              AND NOT EXISTS (
                SELECT 1 FROM thesis_story_links l WHERE l.story_id = s.id
              )
            """,
        ) or 0,
    }


def collect_scoring_metrics(
    conn: sqlite3.Connection,
    *,
    expected_snapshot_date: str | None = None,
) -> dict[str, Any]:
    # Anchor to the date of the latest pipeline run, not UTC today: the
    # scheduler fires every 3h with no UTC-midnight alignment, so between
    # midnight and the next tick UTC-today has no snapshots by design.
    snapshot_date = expected_snapshot_date or utc_now().date().isoformat()
    # Every thesis carries a horizon (theses.horizon_days is NOT NULL, inferred
    # at creation), so every active user-thesis is scoreable. A null score or a
    # missing snapshot now means a real scorer failure, not horizon-less data.
    return {
        "snapshot_date": snapshot_date,
        "active_user_theses": one(
            conn,
            "SELECT COUNT(*) FROM user_theses WHERE status != 'resolved'",
        ) or 0,
        # The population that should be snapshotted each run: scoreable theses
        # (active review_status OR any non-resolved owner). This is the correct
        # denominator for the snapshots_missing gate — score is now per-thesis,
        # so the old per-owner `active_user_theses` count is a different set.
        "scoreable_theses": one(
            conn,
            """
            SELECT COUNT(*) FROM theses t
            WHERE t.review_status = 'active'
               OR EXISTS (SELECT 1 FROM user_theses ut
                          WHERE ut.thesis_id = t.id AND ut.status != 'resolved')
            """,
        ) or 0,
        "snapshots_today": one(
            conn,
            """
            SELECT COUNT(*)
            FROM thesis_snapshots s
            JOIN theses t ON t.id = s.thesis_id
            WHERE s.snapshot_date = ?
              AND (t.review_status = 'active'
                   OR EXISTS (SELECT 1 FROM user_theses ut
                              WHERE ut.thesis_id = t.id AND ut.status != 'resolved'))
            """,
            (snapshot_date,),
        ) or 0,
        "null_score_active": one(
            conn,
            """
            SELECT COUNT(*) FROM theses t
            WHERE t.score IS NULL
              AND (t.review_status = 'active'
                   OR EXISTS (SELECT 1 FROM user_theses ut
                              WHERE ut.thesis_id = t.id AND ut.status != 'resolved'))
            """,
        ) or 0,
        "null_tailwind_active": one(
            conn,
            """
            SELECT COUNT(*) FROM theses t
            WHERE t.score_tailwind IS NULL
              AND (t.review_status = 'active'
                   OR EXISTS (SELECT 1 FROM user_theses ut
                              WHERE ut.thesis_id = t.id AND ut.status != 'resolved'))
            """,
        ) or 0,
        "score_distribution": rows(
            conn,
            """
            SELECT
              MIN(score) AS min_score,
              AVG(score) AS avg_score,
              MAX(score) AS max_score
            FROM theses t
            WHERE t.score IS NOT NULL
              AND (t.review_status = 'active'
                   OR EXISTS (SELECT 1 FROM user_theses ut
                              WHERE ut.thesis_id = t.id AND ut.status != 'resolved'))
            """,
        )[0],
    }


def collect_brief_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    latest = conn.execute(
        "SELECT * FROM daily_briefs ORDER BY brief_date DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        return {"latest": None}
    themes = parse_json_array(latest["themes_json"])
    source_ids = parse_json_array(latest["source_story_ids"])
    return {
        "latest": {
            "brief_date": latest["brief_date"],
            "generated_at": latest["generated_at"],
            "age_minutes": age_minutes(latest["generated_at"]),
            "model_version": latest["model_version"],
            "theme_count": len(themes),
            "source_count": len(source_ids),
            "themes_without_sources": sum(
                1
                for theme in themes
                if isinstance(theme, dict) and not theme.get("source_story_ids")
            ),
        }
    }


def collect_pending_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "unreviewed": one(
            conn,
            "SELECT COUNT(*) FROM pending_instruments WHERE keep IS NULL",
        ) or 0,
        "new_24h": one(
            conn,
            "SELECT COUNT(*) FROM pending_instruments WHERE first_seen_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')",
        ) or 0,
        "seen_24h": one(
            conn,
            "SELECT COUNT(*) FROM pending_instruments WHERE last_seen_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')",
        ) or 0,
        "top_unreviewed": rows(
            conn,
            """
            SELECT symbol, source, seen_count, last_seen_at
            FROM pending_instruments
            WHERE keep IS NULL
            ORDER BY seen_count DESC, last_seen_at DESC
            LIMIT 10
            """,
        ),
    }


def collect_agent_usage_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    if not has_table(conn, "agent_usage"):
        return {"available": False}
    agg_24h = rows(
        conn,
        """
        SELECT
          COUNT(*) AS requests,
          COUNT(DISTINCT user_id) AS users,
          SUM(input_tokens) AS input_tokens,
          SUM(output_tokens) AS output_tokens,
          SUM(cache_read_tokens) AS cache_read_tokens,
          SUM(cache_write_tokens) AS cache_write_tokens,
          SUM(cost_usd) AS cost_usd,
          AVG(latency_ms) AS avg_latency_ms,
          MAX(latency_ms) AS max_latency_ms
        FROM agent_usage
        WHERE phase = 'aggregate'
          AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
        """,
    )[0]
    return {
        "available": True,
        "aggregate_24h": agg_24h,
        "errors_24h": one(
            conn,
            """
            SELECT COUNT(*)
            FROM agent_usage
            WHERE phase = 'aggregate'
              AND status != 'ok'
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
            """,
        ) or 0,
        "missing_session_id_24h": one(
            conn,
            """
            SELECT COUNT(*)
            FROM agent_usage
            WHERE phase = 'aggregate'
              AND (session_id IS NULL OR session_id = '')
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
            """,
        ) or 0,
        "zero_cost_nonzero_tokens_24h": one(
            conn,
            """
            SELECT COUNT(*)
            FROM agent_usage
            WHERE phase = 'aggregate'
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
              AND cost_usd = 0
              AND (input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) > 0
            """,
        ) or 0,
        "requests_missing_aggregate_24h": one(
            conn,
            """
            SELECT COUNT(*)
            FROM (
              SELECT request_id
              FROM agent_usage
              WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-1 day')
              GROUP BY request_id
              HAVING SUM(CASE WHEN phase = 'aggregate' THEN 1 ELSE 0 END) = 0
            )
            """,
        ) or 0,
    }


def collect_pipeline_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    run = latest_event(events, "run_finish")
    firehose = latest_event(events, "firehose_run")
    # Trending lane: the fetch is the no-fallback step, so any failure is
    # critical. Track the latest run overall (failure detection) and the latest
    # Tier-1 run (daily cadence drives staleness).
    trending = latest_event(events, "trending_run")
    trending_tier1 = next(
        (
            e for e in reversed(events)
            if e.get("event") == "trending_run" and int(e.get("tier") or 0) == 1
        ),
        None,
    )
    social = latest_event(events, "social_run")
    route_funnel = normalize_route_funnel(events)
    latest_step = latest_event(events, "step")
    latest_match = None
    latest_score = None
    latest_brief = None
    for event in reversed(events):
        if event.get("event") != "step_metrics":
            continue
        if event.get("step") == "match_story_for_thesis" and latest_match is None:
            latest_match = event
        if event.get("step") == "score_theses" and latest_score is None:
            latest_score = event
        if event.get("step") == "daily_brief" and latest_brief is None:
            latest_brief = event
    return {
        "metrics_path": str(PIPELINE_METRICS_PATH),
        "events_loaded": len(events),
        "latest_run_finish": run,
        "latest_run_age_minutes": age_minutes(run.get("ts")) if run else None,
        "consecutive_run_failures": consecutive_failures(events, "run_finish"),
        "latest_firehose_run": firehose,
        "latest_firehose_age_minutes": age_minutes(firehose.get("ts")) if firehose else None,
        "consecutive_firehose_failures": consecutive_failures(events, "firehose_run"),
        "latest_trending_run": trending,
        "latest_trending_tier1_run": trending_tier1,
        "latest_trending_tier1_age_minutes": age_minutes(trending_tier1.get("ts")) if trending_tier1 else None,
        "consecutive_trending_failures": consecutive_failures(events, "trending_run"),
        "latest_social_run": social,
        "latest_social_age_minutes": age_minutes(social.get("ts")) if social else None,
        "consecutive_social_failures": consecutive_failures(events, "social_run"),
        "latest_route_funnel": route_funnel,
        "latest_step": latest_step,
        "latest_match_metrics": latest_match,
        "latest_score_metrics": latest_score,
        "latest_brief_metrics": latest_brief,
    }


def add_finding(
    findings: list[Finding],
    severity: str,
    code: str,
    message: str,
    *,
    value: Any = None,
    threshold: Any = None,
) -> None:
    findings.append(Finding(severity, code, message, value, threshold))


def evaluate(metrics: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    pipeline = metrics["pipeline"]
    news = metrics["news"]
    thesis = metrics["thesis"]
    matches = metrics["matches"]
    scoring = metrics["scoring"]
    brief = metrics["brief"]
    pending = metrics["pending_instruments"]
    agent_usage = metrics["agent_usage"]

    if not pipeline["latest_run_finish"]:
        add_finding(findings, "critical", "pipeline.no_runs", "No full pipeline run_finish event found")
    elif pipeline["latest_run_age_minutes"] is not None and pipeline["latest_run_age_minutes"] > 24 * 60:
        add_finding(
            findings,
            "critical",
            "pipeline.stale",
            "Latest full pipeline run is older than 24h",
            value=pipeline["latest_run_age_minutes"],
            threshold=24 * 60,
        )
    if pipeline["consecutive_run_failures"] >= 2:
        add_finding(
            findings,
            "critical",
            "pipeline.consecutive_failures",
            "Full pipeline has failed at least twice consecutively",
            value=pipeline["consecutive_run_failures"],
            threshold=2,
        )

    if not pipeline["latest_firehose_run"]:
        add_finding(findings, "critical", "firehose.no_runs", "No firehose_run event found")
    elif pipeline["latest_firehose_age_minutes"] is not None and pipeline["latest_firehose_age_minutes"] > 30:
        add_finding(
            findings,
            "critical",
            "firehose.stale",
            "Latest firehose run is older than 30 minutes",
            value=pipeline["latest_firehose_age_minutes"],
            threshold=30,
        )
    if pipeline["consecutive_firehose_failures"] >= 2:
        add_finding(
            findings,
            "critical",
            "firehose.consecutive_failures",
            "Firehose has failed at least twice consecutively",
            value=pipeline["consecutive_firehose_failures"],
            threshold=2,
        )
    latest_firehose = pipeline.get("latest_firehose_run") or {}
    if latest_firehose and int(latest_firehose.get("raw_items") or 0) == 0:
        add_finding(findings, "critical", "firehose.zero_raw_items", "Latest firehose saw zero raw items")

    # Trending lane has no fallback source: when the 1ms ranking fetch/parse
    # fails there is nothing to fall back to, so even one failure is critical and
    # must surface on the /metrics page. Only evaluate once the lane has run at
    # least once (latest_trending_run is None when the lane is disabled or
    # freshly deployed — don't page on that).
    latest_trending = pipeline.get("latest_trending_run") or {}
    if latest_trending:
        if latest_trending.get("ok") is False or pipeline["consecutive_trending_failures"] >= 1:
            add_finding(
                findings,
                "critical",
                "trending.fetch_failed",
                "Trending-ticker ranking fetch/parse failed (no fallback source)",
                value=latest_trending.get("phase") or pipeline["consecutive_trending_failures"],
            )
        tier1_age = pipeline.get("latest_trending_tier1_age_minutes")
        if tier1_age is not None and tier1_age > 48 * 60:
            add_finding(
                findings,
                "warn",
                "trending.stale",
                "Latest Tier-1 trending run is older than 48h (daily cadence expected)",
                value=tier1_age,
                threshold=48 * 60,
            )

    latest_social = pipeline.get("latest_social_run") or {}
    if latest_social:
        if latest_social.get("ok") is False or pipeline["consecutive_social_failures"] >= 1:
            add_finding(
                findings,
                "critical",
                "social.fetch_failed",
                "Social-topic run failed or no-opped (no fallback source)",
                value=latest_social.get("phase") or pipeline["consecutive_social_failures"],
            )
        social_age = pipeline.get("latest_social_age_minutes")
        if social_age is not None and social_age > 48 * 60:
            add_finding(
                findings,
                "warn",
                "social.stale",
                "Latest social-topic run is older than 48h (daily cadence expected)",
                value=social_age,
                threshold=48 * 60,
            )

    route_funnel = pipeline.get("latest_route_funnel") or {}
    latest_run = pipeline.get("latest_run_finish") or {}
    if latest_run.get("ok") and not route_funnel:
        add_finding(
            findings,
            "warn",
            "route_funnel.missing",
            "Latest successful pipeline run has no route_funnel metrics event",
        )
    elif route_funnel:
        routes = route_funnel.get("routes") or {}
        synth = route_funnel.get("synth") or {}
        evaluated = int(route_funnel.get("evaluated") or 0)
        promotes = int(routes.get("sharp_promote") or 0)
        admitted = int(route_funnel.get("admitted") or 0)
        reject_rate = float(synth.get("reject_rate") or 0.0)
        if evaluated >= 100 and promotes == 0:
            add_finding(
                findings,
                "warn",
                "route_funnel.zero_promotes",
                "Route evaluated many clusters but found zero sharp_promote",
                value={"evaluated": evaluated, "promotes": promotes},
            )
        if admitted >= 5 and reject_rate >= 0.5:
            add_finding(
                findings,
                "warn",
                "route_funnel.high_synth_reject_rate",
                "Latest route run rejected at least half of admitted synth attempts",
                value=reject_rate,
                threshold=0.5,
            )
        snapshot = route_funnel.get("snapshot") or {}
        qual = snapshot.get("story_quality_24h") or {}
        stories_judged = int(qual.get("stories") or 0)
        good_rate = qual.get("good_rate")
        if stories_judged >= 5 and good_rate is not None and float(good_rate) < 0.4:
            add_finding(
                findings,
                "warn",
                "route_funnel.low_story_good_rate",
                "Auto-judge good-rate for stories in 24h is below 40%",
                value=good_rate,
                threshold=0.4,
            )

    news_24h = news["windows"]["24h"]
    if news_24h["total"] == 0:
        add_finding(findings, "critical", "news.no_news_24h", "No news rows created in 24h")
    sharp_empty_sectors = int(news["empty_sector_rows_by_lane_24h"].get("sharp") or 0)
    sharp_sector_rows = int(news["sector_rows_by_lane_24h"].get("sharp") or 0)
    sharp_empty_sector_rate = (
        sharp_empty_sectors / sharp_sector_rows if sharp_sector_rows else 0.0
    )
    if sharp_sector_rows and sharp_empty_sector_rate > 0.20:
        add_finding(
            findings,
            "warn",
            "news.sharp_empty_sector_rate_high",
            "Sharp-lane news sector coverage is low",
            value=round(sharp_empty_sector_rate, 3),
            threshold=0.20,
        )

    missing_ticker_total = sum(int(r["count"] or 0) for r in news["missing_tickers_24h"])
    if news_24h["total"] and missing_ticker_total / news_24h["total"] > 0.50:
        add_finding(
            findings,
            "warn",
            "news.ticker_coverage_low",
            "More than half of recent news rows have no ticker rows",
            value=round(missing_ticker_total / news_24h["total"], 3),
            threshold=0.50,
        )
    # The newest story's displayed timestamp should never trail the wall clock
    # by more than a few hours; if it does, the feed looks stale to users and
    # the pipeline is likely not producing (or surfacing) fresh stories.
    STORY_STALE_HOURS = 9
    last_story_age_min = age_minutes(news.get("last_story_created_at"))
    if last_story_age_min is None:
        add_finding(
            findings,
            "critical",
            "story.none",
            "No stories exist",
        )
    elif last_story_age_min > STORY_STALE_HOURS * 60:
        add_finding(
            findings,
            "critical",
            "story.stale",
            f"Most recent story (displayed time) is over {STORY_STALE_HOURS}h old",
            value={
                "last_story_created_at": news.get("last_story_created_at"),
                "age_hours": round(last_story_age_min / 60.0, 2),
            },
            threshold=STORY_STALE_HOURS,
        )

    missing_story_embeddings = int(news.get("stories_missing_embeddings_24h") or 0)
    if missing_story_embeddings > 0:
        add_finding(
            findings,
            "warn",
            "story.embedding_gap",
            "Stories created in 24h are missing story_match_chunks",
            value=missing_story_embeddings,
            threshold=0,
        )

    if thesis["active_without_match_chunks"] > 0:
        add_finding(
            findings,
            "critical",
            "thesis.missing_match_chunks",
            "Active theses are missing thesis_match_chunks",
            value=thesis["active_without_match_chunks"],
            threshold=0,
        )
    if thesis["active_without_links"] > 0:
        add_finding(
            findings,
            "warn",
            "thesis.no_links",
            "Active theses have no thesis_story_links",
            value=thesis["active_without_links"],
            threshold=0,
        )
    if thesis["candidate_stuck_gt_7d"] > 0:
        add_finding(
            findings,
            "warn",
            "thesis.stuck_candidates",
            "Candidate theses have been stuck for more than 7 days",
            value=thesis["candidate_stuck_gt_7d"],
            threshold=0,
        )

    stories_24h = int(news.get("stories_24h") or 0)
    if stories_24h > 0 and matches["updated_24h"] == 0:
        add_finding(
            findings,
            "critical",
            "matches.no_updates",
            "Stories exist but no thesis-story links were updated in 24h",
            value={"stories_24h": stories_24h, "links_updated_24h": 0},
        )
    latest_match = pipeline.get("latest_match_metrics") or {}
    if int(latest_match.get("judge_failures") or 0) > 0:
        add_finding(
            findings,
            "warn",
            "matches.judge_failures",
            "Latest match step had judge parse failures",
            value=latest_match.get("judge_failures"),
            threshold=0,
        )
    if latest_match.get("theses_failed"):
        add_finding(
            findings,
            "critical",
            "matches.theses_failed",
            "Latest match step failed for one or more theses",
            value=latest_match.get("theses_failed"),
        )

    if scoring["snapshots_today"] < scoring["scoreable_theses"]:
        add_finding(
            findings,
            "critical",
            "scoring.snapshots_missing",
            "Not all scoreable theses have score snapshots for today",
            value={
                "snapshots_today": scoring["snapshots_today"],
                "scoreable_theses": scoring["scoreable_theses"],
            },
        )
    if scoring["null_score_active"] > 0:
        add_finding(
            findings,
            "critical",
            "scoring.null_scores",
            "Scoreable theses have null composite scores",
            value=scoring["null_score_active"],
            threshold=0,
        )
    if scoring["null_tailwind_active"] > 0:
        add_finding(
            findings,
            "warn",
            "scoring.null_tailwind",
            "Scoreable theses have null tailwind scores",
            value=scoring["null_tailwind_active"],
            threshold=0,
        )

    latest_brief = brief.get("latest")
    # Anchor brief freshness to the latest pipeline run's date, not UTC
    # today — same rationale as scoring.snapshots_missing above. The brief
    # writer stamps `brief_date` from the host's date() at run time; a
    # 3h-interval scheduler with no UTC anchor produces a "yesterday"
    # brief between midnight and the next tick. Falls back to UTC today
    # when no run is recorded.
    expected_brief_date = metrics.get("expected_snapshot_date") or utc_now().date().isoformat()
    if not latest_brief:
        add_finding(findings, "critical", "brief.missing", "No daily brief row exists")
    else:
        if latest_brief["brief_date"] != expected_brief_date:
            add_finding(
                findings,
                "warn",
                "brief.not_today",
                "Latest daily brief is not dated for the latest pipeline run",
                value=latest_brief["brief_date"],
                threshold=expected_brief_date,
            )
        if not (4 <= latest_brief["theme_count"] <= 6):
            add_finding(
                findings,
                "critical",
                "brief.theme_count_invalid",
                "Latest daily brief theme count is outside 4-6",
                value=latest_brief["theme_count"],
                threshold="4..6",
            )
        if latest_brief["source_count"] == 0:
            add_finding(findings, "critical", "brief.no_sources", "Latest daily brief has no sources")
        if latest_brief["themes_without_sources"] > 0:
            add_finding(
                findings,
                "critical",
                "brief.themes_without_sources",
                "Latest daily brief has themes without sources",
                value=latest_brief["themes_without_sources"],
                threshold=0,
            )

    if pending["unreviewed"] > 250:
        add_finding(
            findings,
            "warn",
            "pending_instruments.backlog_high",
            "Pending instrument review backlog is high",
            value=pending["unreviewed"],
            threshold=250,
        )

    if agent_usage.get("available"):
        if agent_usage["errors_24h"] > 0:
            add_finding(
                findings,
                "critical",
                "agent_usage.errors",
                "Agent aggregate usage rows have non-ok statuses in 24h",
                value=agent_usage["errors_24h"],
                threshold=0,
            )
        if agent_usage["missing_session_id_24h"] > 0:
            add_finding(
                findings,
                "warn",
                "agent_usage.missing_session_id",
                "Agent aggregate usage rows are missing session_id",
                value=agent_usage["missing_session_id_24h"],
                threshold=0,
            )
        if agent_usage["zero_cost_nonzero_tokens_24h"] > 0:
            add_finding(
                findings,
                "critical",
                "agent_usage.zero_cost_tokens",
                "Agent usage rows have tokens but zero cost",
                value=agent_usage["zero_cost_nonzero_tokens_24h"],
                threshold=0,
            )
        if agent_usage["requests_missing_aggregate_24h"] > 0:
            add_finding(
                findings,
                "critical",
                "agent_usage.missing_aggregate",
                "Agent usage requests are missing aggregate rows",
                value=agent_usage["requests_missing_aggregate_24h"],
                threshold=0,
            )

    return findings


def status_from_findings(findings: list[Finding]) -> str:
    if any(f.severity == "critical" for f in findings):
        return "critical"
    if any(f.severity == "warn" for f in findings):
        return "warn"
    return "ok"


def collect(db_path: Path, pipeline_metrics_path: Path) -> dict[str, Any]:
    events = read_jsonl(pipeline_metrics_path)
    pipeline = collect_pipeline_metrics(events)
    latest_run = pipeline.get("latest_run_finish") or {}
    run_ts = parse_ts(latest_run.get("ts"))
    # Pipeline-anchored "today": the date the score/brief writers stamped
    # on rows during the most recent run. Falls back to UTC today when no
    # run is recorded yet (fresh DB).
    expected_date = (run_ts.date().isoformat() if run_ts else utc_now().date().isoformat())
    with connect(db_path) as conn:
        metrics = {
            "generated_at": iso_now(),
            "db_path": str(db_path),
            "pipeline": pipeline,
            "expected_snapshot_date": expected_date,
            "news": collect_news_metrics(conn),
            "thesis": collect_thesis_metrics(conn),
            "matches": collect_match_metrics(conn),
            "scoring": collect_scoring_metrics(conn, expected_snapshot_date=expected_date),
            "brief": collect_brief_metrics(conn),
            "pending_instruments": collect_pending_metrics(conn),
            "agent_usage": collect_agent_usage_metrics(conn),
        }
    findings = evaluate(metrics)
    metrics["status"] = status_from_findings(findings)
    metrics["findings"] = [asdict(f) for f in findings]
    return metrics


def print_pipeline_funnel(pipeline: dict[str, Any]) -> None:
    funnel = pipeline.get("latest_route_funnel") or {}
    firehose = pipeline.get("latest_firehose_run") or {}
    run = pipeline.get("latest_run_finish") or {}
    print("Pipeline funnel (from hf-pipeline-metrics.jsonl):")
    if firehose:
        print(
            f"  firehose ts={firehose.get('ts')} ins={firehose.get('inserted')} "
            f"raw={firehose.get('raw_items')} dup={firehose.get('duplicates')}"
        )
    trending = pipeline.get("latest_trending_run") or {}
    if trending:
        print(
            f"  trending ts={trending.get('ts')} tier={trending.get('tier')} "
            f"ok={trending.get('ok')} due={trending.get('symbols_due')} "
            f"ins={trending.get('inserted')} scrape_err={trending.get('scrape_errors')}"
        )
    social = pipeline.get("latest_social_run") or {}
    if social:
        print(
            f"  social ts={social.get('ts')} ok={social.get('ok')} "
            f"selected={social.get('tickers_selected')} called={social.get('tickers_called')} "
            f"admitted={social.get('topics_admitted')} refreshed={social.get('topics_refreshed')} "
            f"phase={social.get('phase')}"
        )
    if funnel:
        routes = funnel.get("routes") or {}
        synth = funnel.get("synth") or {}
        print(
            f"  route ts={funnel.get('ts')} eval={funnel.get('evaluated')} "
            f"promote={routes.get('sharp_promote')} admitted={funnel.get('admitted')} "
            f"synth_ok={synth.get('ok')} reject_rate={synth.get('reject_rate')}"
        )
        snapshot = funnel.get("snapshot") or {}
        qual = snapshot.get("story_quality_24h") or {}
        if qual:
            print(f"  story_quality_24h={qual}")
    else:
        print("  route_funnel: (none)")
    if run:
        print(
            f"  run_finish ts={run.get('ts')} ok={run.get('ok')} "
            f"dur={run.get('duration_s')}s"
        )
    print()


def print_human(payload: dict[str, Any]) -> None:
    print(f"HF health: {payload['status'].upper()} ({payload['generated_at']})")
    print()
    for finding in payload["findings"]:
        print(
            f"[{finding['severity'].upper()}] {finding['code']}: "
            f"{finding['message']}"
        )
        if finding.get("value") is not None:
            print(f"  value: {finding['value']}")
        if finding.get("threshold") is not None:
            print(f"  threshold: {finding['threshold']}")
    if not payload["findings"]:
        print("No findings.")
    print()
    news = payload["news"]["windows"]
    print(
        "News 24h: "
        f"total={news['24h']['total']} sharp={news['24h']['sharp']} "
        f"firehose={news['24h']['firehose']}"
    )
    print(
        "Matches 24h: "
        f"updated={payload['matches']['updated_24h']} "
        f"total={payload['matches']['total']}"
    )
    print(
        "Scoring: "
        f"snapshots_today={payload['scoring']['snapshots_today']}/"
        f"{payload['scoring']['scoreable_theses']}"
    )
    agent = payload["agent_usage"]
    if agent.get("available"):
        agg = agent["aggregate_24h"]
        print(
            "Agent 24h: "
            f"requests={agg['requests'] or 0} cost=${float(agg['cost_usd'] or 0):.4f} "
            f"avg_latency_ms={agg['avg_latency_ms'] or 0}"
        )
    print_pipeline_funnel(payload["pipeline"])


def append_health_metric(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect hf-workbench business health metrics")
    parser.add_argument("--json", action="store_true", help="Emit full JSON payload")
    parser.add_argument(
        "--pipeline-funnel",
        action="store_true",
        help="Print route/firehose funnel from pipeline metrics JSONL and exit",
    )
    parser.add_argument("--append", action="store_true", help=f"Append payload to {HEALTH_METRICS_PATH}")
    parser.add_argument("--fail-on-alert", action="store_true", help="Exit non-zero when status is warn/critical")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite DB path")
    parser.add_argument(
        "--pipeline-metrics",
        type=Path,
        default=PIPELINE_METRICS_PATH,
        help="Pipeline metrics JSONL path",
    )
    args = parser.parse_args()

    if args.pipeline_funnel:
        events = read_jsonl(args.pipeline_metrics)
        print_pipeline_funnel(collect_pipeline_metrics(events))
        return 0

    payload = collect(args.db, args.pipeline_metrics)
    if args.append:
        append_health_metric(payload, HEALTH_METRICS_PATH)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print_human(payload)
    if args.fail_on_alert and payload["status"] != "ok":
        return 2 if payload["status"] == "critical" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

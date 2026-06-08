"""Story routing / synthesis funnel payloads for hf-pipeline-metrics.jsonl."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Keep in sync with agents/route_news_clusters.py promote ranking.
_PROMOTE_RULE_PREFIXES: tuple[str, ...] = (
    "R0c",
    "R0b",
    "R0 ",
    "R1 ",
    "R2b",
    "R2 ",
    "R4 ",
    "R7 ",
    "R8 ",
    "R5 ",
    "R6 ",
    "R3 ",
)


def promote_rule_bucket(reason: str) -> str:
    for prefix in _PROMOTE_RULE_PREFIXES:
        if reason.startswith(prefix):
            return prefix.strip()
    return "other"


def top_counts(counter: Counter[str], *, limit: int = 15) -> dict[str, int]:
    return dict(counter.most_common(limit))


def member_count_histogram(counts: list[int]) -> dict[str, int]:
    hist = {"1": 0, "2": 0, "3-5": 0, "6+": 0}
    for n in counts:
        if n <= 1:
            hist["1"] += 1
        elif n == 2:
            hist["2"] += 1
        elif n <= 5:
            hist["3-5"] += 1
        else:
            hist["6+"] += 1
    return hist


@dataclass(slots=True)
class RouteFunnelSnapshot:
    run_id: str | None
    evaluated: int = 0
    route_discard: int = 0
    route_firehose_store: int = 0
    route_sharp_promote: int = 0
    admitted: int = 0
    overflow_synth_budget: int = 0
    overflow_diversity: int = 0
    synth_ok: int = 0
    synth_rejected: int = 0
    promote_rules: Counter[str] = field(default_factory=Counter)
    admitted_member_counts: list[int] = field(default_factory=list)
    synth_ok_cluster_ids: list[str] = field(default_factory=list)
    synth_rejected_cluster_ids: list[str] = field(default_factory=list)

    def funnel_body(self) -> dict[str, Any]:
        admitted_n = self.admitted or 0
        promote_n = self.route_sharp_promote or 0
        return {
            "evaluated": self.evaluated,
            "routes": {
                "discard": self.route_discard,
                "firehose_store": self.route_firehose_store,
                "sharp_promote": self.route_sharp_promote,
            },
            "promote_rules": dict(self.promote_rules),
            "admitted": self.admitted,
            "overflow": {
                "synth_budget": self.overflow_synth_budget,
                "diversity_quota": self.overflow_diversity,
            },
            "synth": {
                "ok": self.synth_ok,
                "rejected": self.synth_rejected,
                "reject_rate": round(self.synth_rejected / admitted_n, 3)
                if admitted_n
                else 0.0,
            },
            "promote_yield": round(self.synth_ok / promote_n, 3) if promote_n else 0.0,
            "admitted_member_hist": member_count_histogram(self.admitted_member_counts),
        }

    def to_metric(self) -> dict[str, Any]:
        return {
            "event": "route_funnel",
            "run_id": self.run_id,
            **self.funnel_body(),
        }


def collect_cluster_inventory(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
        FROM news_cluster
        GROUP BY status
        ORDER BY n DESC
        """
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def collect_story_quality_window(
    conn: sqlite3.Connection,
    *,
    hours: int = 24,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT l.label, COUNT(*) AS n
        FROM story s
        LEFT JOIN story_quality_label l
          ON l.story_id = s.id AND l.labeler = 'auto:gemini-judge'
        WHERE s.created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
          AND s.kind = 'story'
        GROUP BY l.label
        """,
        (f"-{hours} hours",),
    ).fetchall()
    by_label = {str(row[0] or "unlabeled"): int(row[1]) for row in rows}
    total = sum(by_label.values())
    good = by_label.get("good", 0)
    return {
        "hours": hours,
        "stories": total,
        "by_label": by_label,
        "good_rate": round(good / total, 3) if total else None,
    }


def collect_synth_rejections_window(
    conn: sqlite3.Connection,
    *,
    hours: int = 24,
    limit: int = 8,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
          CASE
            WHEN reason LIKE 'coherence gate%' THEN 'coherence_gate'
            WHEN reason LIKE '%verif%' OR reason LIKE '%citation%' THEN 'verifier'
            WHEN reason LIKE '%empty sectors%' OR reason LIKE '%empty regions%' THEN 'taxonomy'
            WHEN reason LIKE '%UNIQUE constraint failed: story.id%' THEN 'story_id_race'
            ELSE 'other'
          END AS bucket,
          COUNT(*) AS n
        FROM story_synth_rejected
        WHERE rejected_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
        GROUP BY bucket
        ORDER BY n DESC
        """,
        (f"-{hours} hours",),
    ).fetchall()
    return {
        "hours": hours,
        "by_reason_bucket": {str(row[0]): int(row[1]) for row in rows},
        "sample_limit": limit,
    }


def publisher_contribution_for_clusters(
    conn: sqlite3.Connection,
    cluster_ids: list[str],
    *,
    limit: int = 15,
) -> dict[str, int]:
    if not cluster_ids:
        return {}
    placeholders = ",".join("?" * len(cluster_ids))
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM(n.publisher), ''), 'unknown') AS publisher,
               COUNT(*) AS n
        FROM news_cluster_member m
        JOIN news n ON n.id = m.news_id
        WHERE m.cluster_id IN ({placeholders})
        GROUP BY publisher
        ORDER BY n DESC
        LIMIT ?
        """,
        [*cluster_ids, limit],
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def cluster_member_counts(conn: sqlite3.Connection, cluster_ids: list[str]) -> dict[str, int]:
    if not cluster_ids:
        return {}
    placeholders = ",".join("?" * len(cluster_ids))
    rows = conn.execute(
        f"""
        SELECT id, member_count
        FROM news_cluster
        WHERE id IN ({placeholders})
        """,
        cluster_ids,
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def route_sources_body(
    conn: sqlite3.Connection,
    *,
    synth_ok_cluster_ids: list[str],
    admitted_cluster_ids: list[str],
    promote_cluster_ids: list[str],
) -> dict[str, Any]:
    return {
        "news_articles": {
            "promoted_clusters": publisher_contribution_for_clusters(
                conn, promote_cluster_ids
            ),
            "admitted_clusters": publisher_contribution_for_clusters(
                conn, admitted_cluster_ids
            ),
            "synth_ok_clusters": publisher_contribution_for_clusters(
                conn, synth_ok_cluster_ids
            ),
        },
    }


def pipeline_snapshot_body(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "cluster_inventory": collect_cluster_inventory(conn),
        "story_quality_24h": collect_story_quality_window(conn),
        "synth_rejections_24h": collect_synth_rejections_window(conn),
    }


def build_route_run_metric(
    conn: sqlite3.Connection,
    funnel: RouteFunnelSnapshot,
    *,
    admitted_cluster_ids: list[str],
    promote_cluster_ids: list[str],
    synth_ok_cluster_ids: list[str],
) -> dict[str, Any]:
    """Single JSONL event: funnel counts, publisher mix, and DB snapshot."""
    return {
        "event": "route_funnel",
        "run_id": funnel.run_id,
        **funnel.funnel_body(),
        "sources": route_sources_body(
            conn,
            synth_ok_cluster_ids=synth_ok_cluster_ids,
            admitted_cluster_ids=admitted_cluster_ids,
            promote_cluster_ids=promote_cluster_ids,
        ),
        "snapshot": pipeline_snapshot_body(conn),
    }

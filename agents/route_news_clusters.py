#!/usr/bin/env python3
"""Route ready news clusters and optionally promote them to stories."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.cluster import (
    PROMOTION_MAX_AGE_H_RELAXED,
    ClusterDecisionInput,
    load_cluster_decision_input,
)
from src.news.persist import write_cluster_story
from src.news.publishers import PR_WIRE_PUBLISHER_NAMES
from src.news.routing import Decision, route_cluster, subject_key
from src.pipeline_metrics import (
    RouteFunnelSnapshot,
    append_metric,
    build_route_run_metric,
    cluster_member_counts,
    promote_rule_bucket,
)

DB_PATH = ROOT / "db" / "hf.db"
DEFAULT_ROUTE_EVAL_LIMIT = 1200
DEFAULT_SYNTH_BUDGET = 40
DEFAULT_SYNTH_WORKERS = 6
DIVERSITY_QUOTA_START = 20
DIVERSITY_MAX_SHARE = 0.60
# A cluster that has failed synthesis this many times at its current
# member_count is considered "stuck on the same evidence" — exclude from
# the candidate window until a new member arrives and bumps member_count.
SYNTH_REJECT_COOLDOWN = 2
DIVERSITY_EXEMPT_EVENT_CLASSES = frozenset({
    "fed_action",
    "macro_print",
    "commodity_move",
})
# Longer prefixes first so R0c/R0b win over the R0 institutional rule.
PROMOTE_RULE_PREFIXES: tuple[str, ...] = (
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


@dataclass(frozen=True, slots=True)
class _RoutedCluster:
    cluster_id: str
    cluster: ClusterDecisionInput
    decision: Decision


def _promote_sort_key(item: _RoutedCluster) -> tuple[int, int, int]:
    """Rank sharp_promote candidates: rule quality, then corroboration, then mat."""
    reason = item.decision.reason
    rule_rank = len(PROMOTE_RULE_PREFIXES)
    for i, prefix in enumerate(PROMOTE_RULE_PREFIXES):
        if reason.startswith(prefix):
            rule_rank = i
            break
    return (
        rule_rank,
        -item.cluster.independent_pub_count,
        -item.cluster.max_materiality,
    )


def _sort_sharp_promotes(items: list[_RoutedCluster]) -> list[_RoutedCluster]:
    return sorted(items, key=_promote_sort_key)


def _active_thesis_tickers(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT et.symbol
        FROM entity_tickers et
        JOIN user_theses ut ON ut.thesis_id = et.entity_id
        WHERE et.entity_type='thesis'
          AND ut.status='active'
        """
    ).fetchall()
    return {str(row[0]).upper() for row in rows if row[0]}


def _active_thesis_sectors(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT sectors_json
        FROM thesis_match_chunks c
        JOIN user_theses ut ON ut.thesis_id = c.thesis_id
        WHERE ut.status='active'
        """
    ).fetchall()
    sectors: set[str] = set()
    for row in rows:
        try:
            import json

            sectors.update(str(s) for s in json.loads(row[0] or "[]"))
        except Exception:
            pass
    return sectors


def _active_thesis_regions(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT i.region
        FROM entity_tickers et
        JOIN user_theses ut ON ut.thesis_id = et.entity_id
        LEFT JOIN instruments i ON i.symbol = et.symbol
        WHERE et.entity_type='thesis'
          AND ut.status='active'
          AND i.region IS NOT NULL
        """
    ).fetchall()
    return {str(row[0]) for row in rows if row[0]}


# Pre-built EXISTS subquery: cluster has at least one member whose publisher
# is not a PR wire. LIKE '...%' so the firehose's "-classaction" suffix is
# still matched as a PR wire. Built once at module load — PR_WIRE_PUBLISHER_NAMES
# is a fixed in-code frozenset so SQL injection isn't a concern, and the
# expression is reused on every candidate query.
_NON_PR_WIRE_MEMBER_EXISTS_SQL = (
    "EXISTS ("
    " SELECT 1"
    " FROM news_cluster_member m"
    " JOIN news n ON n.id = m.news_id"
    " WHERE m.cluster_id = c.id AND NOT ("
    + " OR ".join(
        f"COALESCE(n.publisher, '') LIKE '{name}%'"
        for name in sorted(PR_WIRE_PUBLISHER_NAMES)
    )
    + "))"
)


def _candidate_cluster_ids(conn: sqlite3.Connection, limit: int) -> list[str]:
    # Synthesis-failure cooldown: exclude clusters that have ≥
    # SYNTH_REJECT_COOLDOWN rejections logged at their current member_count.
    # The moment a new member is attached, member_count moves up and the
    # cluster is eligible again.
    #
    # Recency bound: route_cluster's most lenient rule requires
    # min_member_age_h ≤ PROMOTION_MAX_AGE_H_RELAXED, so clusters whose
    # last_seen_at is older than that window cannot promote under any rule.
    # Filtering them here keeps stale high-materiality wires from crowding
    # the LIMIT window and starving fresh promote-eligible candidates.
    #
    # Promote-signal pre-filter: deprioritize PR-only / single-source junk
    # (mat=100 GlobeNewswire M&A) so corroborated or tier-1 clusters are
    # actually evaluated within the LIMIT window.
    #
    # The first OR block is the legacy "minimally interesting" floor
    # (independent_pub_count >= 3 OR max_materiality >= 30). The second OR
    # block is the new promote-signal filter and is strictly tighter — it
    # cannot make a cluster eligible that the first block excludes, but it
    # can reject mat=100 PR-wire-only clusters that the first block would
    # otherwise let through. Both are kept so the legacy floor stays
    # auditable; ORDER BY then ranks within the survivors.
    rows = conn.execute(
        f"""
        SELECT c.id
        FROM news_cluster c
        LEFT JOIN (
          SELECT cluster_id, member_count_at_reject, COUNT(*) AS n_stuck
          FROM story_synth_rejected
          WHERE member_count_at_reject IS NOT NULL
          GROUP BY cluster_id, member_count_at_reject
        ) r
          ON r.cluster_id = c.id
         AND r.member_count_at_reject = c.member_count
         AND r.n_stuck >= ?
        WHERE c.status IN ('open', 'firehose', 'ambiguous')
          AND c.member_count >= 1
          AND c.last_seen_at > strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)
          AND (
            c.independent_pub_count >= 3
            OR c.max_materiality >= 30
          )
          AND r.cluster_id IS NULL
          AND (
            c.has_tier1_primary = 1
            OR c.independent_pub_count >= 2
            OR (c.max_materiality >= 50 AND {_NON_PR_WIRE_MEMBER_EXISTS_SQL})
          )
        ORDER BY c.has_tier1_primary DESC,
                 c.independent_pub_count DESC,
                 c.max_materiality DESC,
                 c.last_seen_at DESC
        LIMIT ?
        """,
        (SYNTH_REJECT_COOLDOWN, f"-{PROMOTION_MAX_AGE_H_RELAXED} hours", limit),
    ).fetchall()
    return [str(row[0]) for row in rows]


@dataclass(frozen=True, slots=True)
class _RouteRunSummary:
    evaluated: int
    promotes_found: int
    admitted: int
    synth_ok: int
    synth_rejected: int


def _admit_promotes(
    promotes: list[_RoutedCluster],
    synth_budget: int,
) -> tuple[list[_RoutedCluster], list[_RoutedCluster], list[_RoutedCluster]]:
    """Promote-first admit: diversity quota applied after rule-ranked promotes."""
    admitted: list[_RoutedCluster] = []
    overflow_budget: list[_RoutedCluster] = []
    overflow_diversity: list[_RoutedCluster] = []
    accepted_subjects: Counter[tuple[str, str]] = Counter()
    for item in promotes:
        if len(admitted) >= synth_budget:
            overflow_budget.append(item)
            continue
        if not _admit_with_diversity(
            item.cluster,
            accepted_subjects,
            len(admitted),
        ):
            overflow_diversity.append(item)
            continue
        accepted_subjects[subject_key(item.cluster.sectors, item.cluster.regions)] += 1
        admitted.append(item)
    return admitted, overflow_budget, overflow_diversity


def _admit_with_diversity(
    cluster,
    accepted_subjects: Counter[tuple[str, str]],
    accepted_count: int,
) -> bool:
    if cluster.has_institutional_primary:
        return True
    if (
        cluster.event_class in DIVERSITY_EXEMPT_EVENT_CLASSES
        and cluster.max_materiality >= 50
    ):
        return True
    key = subject_key(cluster.sectors, cluster.regions)
    if accepted_count < DIVERSITY_QUOTA_START:
        return True
    max_for_key = max(1, int((accepted_count + 1) * DIVERSITY_MAX_SHARE))
    return accepted_subjects[key] < max_for_key


def _story_has_tickers(story_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        row = conn.execute(
            """
            SELECT 1 FROM entity_tickers
            WHERE entity_type='story' AND entity_id=?
            LIMIT 1
            """,
            (story_id,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _matched_thesis_angles(matches: list) -> list[str]:
    """Collect titles of the matched existing theses to feed the generator as
    already-covered angles, so it proposes only genuinely new ones."""
    from src.thesis.docs import parse_thesis_markdown

    angles: list[str] = []
    for match in matches:
        path = ROOT / "global" / "theses" / f"{match['thesis_id']}.md"
        try:
            angles.append(parse_thesis_markdown(path).title)
        except (ValueError, OSError):
            continue
    return angles


def _match_and_maybe_discover_for_story(story_id: str) -> None:
    """Fan out ingest-side links for the new story, then run discovery.

    Run the story→theses matcher first so any existing theses pick up the new
    evidence the moment the story lands. Then run multi-candidate discovery on
    the story, telling the generator which angles the matched theses already
    cover so it proposes only new, distinct ones. A strong existing match no
    longer skips discovery — the story can still carry a different angle.
    """
    from agents.match_thesis_for_story import match_thesis_for_story

    covered_angles: list[str] = []
    try:
        output = match_thesis_for_story(ROOT, story_id)
        covered_angles = _matched_thesis_angles(output["matches"])
    except Exception as exc:
        print(f"  [match] thesis matching failed for {story_id}: {exc}")

    # The one cheap pre-filter: a thesis needs >=1 ticker, so a ticker-less
    # story can only yield a doomed candidate or a wasted Pro call. (We do NOT
    # gate on theme_tag — "other" just means "no tracked macro narrative" and
    # silently kills strong single-name seeds like an earnings surprise.)
    if not _story_has_tickers(story_id):
        print(f"  [discover] {story_id} has no tagged tickers, skipping discovery.")
        return

    story_path = ROOT / "global" / "stories" / f"{story_id}.md"
    if not story_path.exists():
        print(f"  [discover] {story_id} markdown missing, skipping discovery.")
        return

    from src.thesis.discover import discover_story_theses, run_post_creation_pipeline

    try:
        results = discover_story_theses(
            story_path.read_text(encoding="utf-8"),
            DB_PATH,
            source_context=story_id,
            covered_angles=covered_angles,
        )
    except Exception as exc:
        print(f"  [discover] thesis discovery failed for {story_id}: {exc}")
        return

    new_results = [r for r in results if r.action == "new" and r.thesis]
    existing_results = [r for r in results if r.action == "existing"]
    if not results:
        print(f"  [discover] {story_id}: no new thesis proposed.")
    for result in existing_results:
        print(
            f"  [discover] {story_id} candidate overlaps {result.existing_thesis_id} "
            f"(similarity {result.similarity_score:.2f}); story already linked by matcher."
        )
    # Persist + promote sequentially: each new thesis is embedded before the
    # next promotes, so the promotion gate dedups near-twin siblings for free.
    for result in new_results:
        print(
            f"  [discover] new thesis candidate: {result.thesis.thesis_id} "
            f"— {result.thesis.title}"
        )
        status = run_post_creation_pipeline(ROOT, result.thesis.thesis_id, DB_PATH)
        print(f"  [discover] {result.thesis.thesis_id} -> {status}")


def _route_pool(
    conn: sqlite3.Connection,
    cluster_ids: list[str],
    *,
    thesis_tickers: set[str],
    thesis_sectors: set[str],
    thesis_regions: set[str],
) -> list[_RoutedCluster]:
    routed: list[_RoutedCluster] = []
    for cluster_id in cluster_ids:
        cluster = load_cluster_decision_input(conn, cluster_id)
        decision = route_cluster(
            cluster,
            active_thesis_tickers=thesis_tickers,
            active_thesis_sectors=thesis_sectors,
            active_thesis_regions=thesis_regions,
            has_macro_keyword=cluster.event_class in {"fed_action", "macro_print"},
        )
        routed.append(_RoutedCluster(cluster_id, cluster, decision))
    return routed


def _mark_clusters_firehose(conn: sqlite3.Connection, cluster_ids: list[str]) -> None:
    if not cluster_ids:
        return
    with conn:
        conn.executemany(
            "UPDATE news_cluster SET status='firehose', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
            [(cid,) for cid in cluster_ids],
        )


def _synth_admitted_clusters(
    admitted: list[_RoutedCluster],
    *,
    synth_workers: int,
) -> list[tuple[str, str | None]]:
    """Run write_cluster_story; each call opens its own DB connection."""
    if not admitted:
        return []

    def _one(item: _RoutedCluster) -> tuple[str, str | None]:
        try:
            story_id = write_cluster_story(ROOT, item.cluster_id)
        except Exception as exc:
            print(
                f"  synth error cluster={item.cluster_id}: {exc}",
                file=sys.stderr,
            )
            return item.cluster_id, None
        return item.cluster_id, story_id

    if synth_workers <= 1 or len(admitted) <= 1:
        return [_one(item) for item in admitted]

    results: list[tuple[str, str | None]] = []
    with ThreadPoolExecutor(max_workers=synth_workers) as pool:
        futures = {pool.submit(_one, item): item for item in admitted}
        for future in as_completed(futures):
            results.append(future.result())
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--route-eval-limit",
        "--limit",
        type=int,
        default=DEFAULT_ROUTE_EVAL_LIMIT,
        dest="route_eval_limit",
        help="Max clusters to route_cluster() per run (cheap).",
    )
    parser.add_argument(
        "--synth-budget",
        "--top",
        type=int,
        default=DEFAULT_SYNTH_BUDGET,
        dest="synth_budget",
        help="Max clusters to synthesize after promote-first admit (expensive).",
    )
    parser.add_argument(
        "--synth-workers",
        type=int,
        default=DEFAULT_SYNTH_WORKERS,
        help="Parallel Firecrawl+Gemini workers for --write.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true", help="Run synthesis and write story rows.")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Pipeline run id for metrics correlation (hf-pipeline-metrics.jsonl).",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(DB_PATH, timeout=30)
    synth_ok = 0
    synth_rejected = 0
    funnel = RouteFunnelSnapshot(run_id=args.run_id)
    synth_ok_ids: list[str] = []
    synth_rejected_ids: list[str] = []
    try:
        thesis_tickers = _active_thesis_tickers(conn)
        # The trending lane retrieves news for hot tickers nobody holds a thesis
        # on yet. Treat Tier-1 trending symbols as live interest so their
        # clusters survive the discard gate the same way owned-thesis tickers do.
        from agents.trending import tier1_symbols

        thesis_tickers |= tier1_symbols(conn)
        thesis_sectors = _active_thesis_sectors(conn)
        thesis_regions = _active_thesis_regions(conn)
        ids = _candidate_cluster_ids(conn, args.route_eval_limit)
        routed = _route_pool(
            conn,
            ids,
            thesis_tickers=thesis_tickers,
            thesis_sectors=thesis_sectors,
            thesis_regions=thesis_regions,
        )

        discards = [item for item in routed if item.decision.route == "discard"]
        firehoses = [item for item in routed if item.decision.route == "firehose_store"]
        promotes = _sort_sharp_promotes(
            [item for item in routed if item.decision.route == "sharp_promote"]
        )
        admitted, overflow_budget, overflow_diversity = _admit_promotes(
            promotes, args.synth_budget
        )
        promote_overflow = overflow_budget + overflow_diversity

        funnel.evaluated = len(routed)
        funnel.route_discard = len(discards)
        funnel.route_firehose_store = len(firehoses)
        funnel.route_sharp_promote = len(promotes)
        funnel.admitted = len(admitted)
        funnel.overflow_synth_budget = len(overflow_budget)
        funnel.overflow_diversity = len(overflow_diversity)
        for item in promotes:
            funnel.promote_rules[promote_rule_bucket(item.decision.reason)] += 1

        for item in discards:
            cluster_id = item.cluster_id
            if not args.dry_run:
                with conn:
                    conn.execute(
                        "UPDATE news_cluster SET status='discarded', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (cluster_id,),
                    )
                    conn.execute(
                        """
                        INSERT INTO news_cluster_dropped (cluster_id, reason)
                        VALUES (?, ?)
                        ON CONFLICT(cluster_id) DO UPDATE SET
                          dropped_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                          reason=excluded.reason
                        """,
                        (cluster_id, item.decision.reason),
                    )
            print(f"{cluster_id}: discard — {item.decision.reason}")

        for item in firehoses:
            cluster_id = item.cluster_id
            if not args.dry_run:
                with conn:
                    conn.execute(
                        "UPDATE news_cluster SET status='firehose', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                        (cluster_id,),
                    )
            print(f"{cluster_id}: firehose — {item.decision.reason}")

        if promote_overflow and not args.dry_run:
            _mark_clusters_firehose(conn, [item.cluster_id for item in promote_overflow])
        for item in overflow_budget:
            print(f"{item.cluster_id}: firehose — synth budget")
        for item in overflow_diversity:
            print(f"{item.cluster_id}: firehose — diversity quota")

        for item in admitted:
            print(f"{item.cluster_id}: sharp_promote — {item.decision.reason}")

        admitted_ids = [item.cluster_id for item in admitted]
        member_counts = cluster_member_counts(conn, admitted_ids)
        funnel.admitted_member_counts = [
            member_counts.get(cid, 0) for cid in admitted_ids
        ]

        if args.write and not args.dry_run:
            synth_results = _synth_admitted_clusters(
                admitted,
                synth_workers=args.synth_workers,
            )
            for cluster_id, story_id in synth_results:
                print(f"  -> {cluster_id} {story_id or 'rejected'}")
                if story_id:
                    synth_ok += 1
                    synth_ok_ids.append(cluster_id)
                    _match_and_maybe_discover_for_story(story_id)
                else:
                    synth_rejected += 1
                    synth_rejected_ids.append(cluster_id)
        elif not args.dry_run:
            with conn:
                conn.executemany(
                    "UPDATE news_cluster SET status='open', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    [(item.cluster_id,) for item in admitted],
                )

        funnel.synth_ok = synth_ok
        funnel.synth_rejected = synth_rejected
        funnel.synth_ok_cluster_ids = synth_ok_ids
        funnel.synth_rejected_cluster_ids = synth_rejected_ids

        summary = _RouteRunSummary(
            evaluated=funnel.evaluated,
            promotes_found=funnel.route_sharp_promote,
            admitted=funnel.admitted,
            synth_ok=funnel.synth_ok,
            synth_rejected=funnel.synth_rejected,
        )
        print(
            f"[route] evaluated={summary.evaluated} promotes={summary.promotes_found} "
            f"admitted={summary.admitted} synth_ok={summary.synth_ok} "
            f"synth_rejected={summary.synth_rejected}",
            file=sys.stderr,
        )

        if not args.dry_run:
            append_metric(
                lambda: build_route_run_metric(
                    conn,
                    funnel,
                    admitted_cluster_ids=admitted_ids,
                    promote_cluster_ids=[item.cluster_id for item in promotes],
                    synth_ok_cluster_ids=synth_ok_ids,
                ),
                event_name="route_funnel",
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

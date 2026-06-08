#!/usr/bin/env python
"""Read-only CLI over `agent_usage`.

Subcommands:
  today                       — totals for today (UTC)
  user <user_id> [--days N]   — per-user breakdown
  model [--days N]            — totals grouped by model_id
  top-spenders [--days N]     — users ranked by cost
  endpoint <name> [--days N]  — totals for one endpoint (chat / chip name)
  request <request_id>        — per-phase breakdown for one request
  charts [--days N]           — Code Interpreter (chart) run stats + cost

All commands accept `--json` for a machine-readable payload (Claude Code
can pipe it into ad-hoc analytics). Without `--json`, output is a small
ASCII table.

Usage:
  uv run python scripts/hf_metrics.py today
  uv run python scripts/hf_metrics.py user user_1 --days 7
  uv run python scripts/hf_metrics.py top-spenders --days 30 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("HF_DB_PATH") or Path(__file__).resolve().parent.parent / "db" / "hf.db")


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"DB not found at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _since_clause(days: int | None) -> tuple[str, list[Any]]:
    if not days:
        return "", []
    return "AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?)", [f"-{int(days)} days"]


def _format_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "(no data)"
    widths = {c: max(len(c), max(len(_fmt(r.get(c))) for r in rows)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    sep = "  ".join("-" * widths[c] for c in columns)
    body_lines = [
        "  ".join(_fmt(r.get(c)).ljust(widths[c]) for c in columns) for r in rows
    ]
    return "\n".join([header, sep, *body_lines])


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def cmd_today(args: argparse.Namespace) -> dict[str, Any]:
    sql = """
        SELECT
          COUNT(DISTINCT request_id) AS requests,
          COUNT(DISTINCT user_id)    AS users,
          SUM(input_tokens)          AS input_tokens,
          SUM(output_tokens)         AS output_tokens,
          SUM(cache_read_tokens)     AS cache_read_tokens,
          SUM(cache_write_tokens)    AS cache_write_tokens,
          SUM(cost_usd)              AS cost_usd
        FROM agent_usage
        WHERE phase = 'aggregate'
          AND date(created_at) = date('now')
    """
    with _connect() as conn:
        row = conn.execute(sql).fetchone()
    return dict(row) if row else {}


def cmd_user(args: argparse.Namespace) -> dict[str, Any]:
    since, params = _since_clause(args.days)
    sql = f"""
        SELECT
          endpoint,
          COUNT(*) AS requests,
          SUM(input_tokens)       AS input_tokens,
          SUM(output_tokens)      AS output_tokens,
          SUM(cache_read_tokens)  AS cache_read_tokens,
          SUM(cache_write_tokens) AS cache_write_tokens,
          SUM(cost_usd)           AS cost_usd
        FROM agent_usage
        WHERE phase = 'aggregate'
          AND user_id = ?
          {since}
        GROUP BY endpoint
        ORDER BY cost_usd DESC
    """
    with _connect() as conn:
        rows = conn.execute(sql, [args.user_id, *params]).fetchall()
    return {
        "user_id": args.user_id,
        "days": args.days,
        "by_endpoint": [dict(r) for r in rows],
        "totals": _row_totals(rows),
    }


def cmd_model(args: argparse.Namespace) -> dict[str, Any]:
    since, params = _since_clause(args.days)
    sql = f"""
        SELECT
          model_id,
          COUNT(*) AS requests,
          SUM(input_tokens)       AS input_tokens,
          SUM(output_tokens)      AS output_tokens,
          SUM(cache_read_tokens)  AS cache_read_tokens,
          SUM(cache_write_tokens) AS cache_write_tokens,
          SUM(cost_usd)           AS cost_usd
        FROM agent_usage
        WHERE phase = 'aggregate'
          {since}
        GROUP BY model_id
        ORDER BY cost_usd DESC
    """
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"days": args.days, "by_model": [dict(r) for r in rows]}


def cmd_top_spenders(args: argparse.Namespace) -> dict[str, Any]:
    since, params = _since_clause(args.days)
    sql = f"""
        SELECT
          user_id,
          COUNT(*) AS requests,
          SUM(input_tokens)  AS input_tokens,
          SUM(output_tokens) AS output_tokens,
          SUM(cost_usd)      AS cost_usd
        FROM agent_usage
        WHERE phase = 'aggregate'
          {since}
        GROUP BY user_id
        ORDER BY cost_usd DESC
        LIMIT ?
    """
    with _connect() as conn:
        rows = conn.execute(sql, [*params, args.limit]).fetchall()
    return {"days": args.days, "limit": args.limit, "spenders": [dict(r) for r in rows]}


def cmd_endpoint(args: argparse.Namespace) -> dict[str, Any]:
    since, params = _since_clause(args.days)
    sql_summary = f"""
        SELECT
          COUNT(DISTINCT request_id) AS requests,
          COUNT(DISTINCT user_id)    AS users,
          SUM(input_tokens)          AS input_tokens,
          SUM(output_tokens)         AS output_tokens,
          SUM(cost_usd)              AS cost_usd,
          AVG(cost_usd)              AS avg_cost_usd,
          AVG(latency_ms)            AS avg_latency_ms
        FROM agent_usage
        WHERE phase = 'aggregate'
          AND endpoint = ?
          {since}
    """
    sql_status = f"""
        SELECT status, COUNT(*) AS n
        FROM agent_usage
        WHERE phase = 'aggregate'
          AND endpoint = ?
          {since}
        GROUP BY status
    """
    with _connect() as conn:
        summary = conn.execute(sql_summary, [args.endpoint, *params]).fetchone()
        status = conn.execute(sql_status, [args.endpoint, *params]).fetchall()
    return {
        "endpoint": args.endpoint,
        "days": args.days,
        "summary": dict(summary) if summary else {},
        "by_status": [dict(r) for r in status],
    }


def cmd_request(args: argparse.Namespace) -> dict[str, Any]:
    sql = """
        SELECT phase, model_id, input_tokens, output_tokens, cache_read_tokens,
               cache_write_tokens, cost_usd, latency_ms, status, created_at
        FROM agent_usage
        WHERE request_id = ?
        ORDER BY id ASC
    """
    with _connect() as conn:
        rows = conn.execute(sql, [args.request_id]).fetchall()
    return {"request_id": args.request_id, "phases": [dict(r) for r in rows]}


def cmd_charts(args: argparse.Namespace) -> dict[str, Any]:
    """Code Interpreter (Phase 2b chart) run stats from `code_interpreter_runs`,
    with token/cost for the same runs joined from `agent_usage` (phase='chart')."""
    since, params = _since_clause(args.days)
    runs_cte = f"""
        WITH runs AS (
            SELECT *
            FROM code_interpreter_runs
            WHERE 1=1 {since}
        )
    """
    with _connect() as conn:
        outcomes = conn.execute(
            f"""
            {runs_cte}
            SELECT outcome,
                   COUNT(*)           AS n,
                   AVG(elapsed_ms)    AS avg_ms,
                   AVG(execute_count) AS avg_exec,
                   AVG(write_count)   AS avg_write
            FROM runs
            GROUP BY outcome
            ORDER BY n DESC
            """,
            params,
        ).fetchall()
        skips = conn.execute(
            f"""
            {runs_cte}
            SELECT COALESCE(skip_reason, '(none)') AS skip_reason, COUNT(*) AS n
            FROM runs
            WHERE outcome = 'skip'
            GROUP BY skip_reason
            ORDER BY n DESC
            """,
            params,
        ).fetchall()
        failures = conn.execute(
            f"""
            {runs_cte}
            SELECT COALESCE(failure_stage, '(none)') AS failure_stage, COUNT(*) AS n
            FROM runs
            WHERE outcome IN ('error', 'timeout')
            GROUP BY failure_stage
            ORDER BY n DESC
            """,
            params,
        ).fetchall()
        elapsed = [
            int(r["elapsed_ms"] or 0)
            for r in conn.execute(
                f"{runs_cte} SELECT elapsed_ms FROM runs ORDER BY elapsed_ms",
                params,
            ).fetchall()
        ]
        cost = conn.execute(
            f"""
            {runs_cte}
            SELECT SUM(input_tokens)  AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cost_usd)      AS cost_usd,
                   COUNT(au.id)       AS rows
            FROM runs r
            LEFT JOIN agent_usage au
              ON au.request_id = r.request_id
             AND au.phase = 'chart'
            """,
            params,
        ).fetchone()

    total = sum(int(r["n"]) for r in outcomes)
    plotted = sum(int(r["n"]) for r in outcomes if r["outcome"] == "plot")
    p50 = elapsed[len(elapsed) // 2] if elapsed else None
    avg = sum(elapsed) / len(elapsed) if elapsed else None
    return {
        "days": args.days,
        "total_runs": total,
        "render_rate": (plotted / total) if total else None,
        "by_outcome": [dict(r) for r in outcomes],
        "skip_reasons": [dict(r) for r in skips],
        "failure_stages": [dict(r) for r in failures],
        "latency_ms": {"p50": p50, "avg": avg},
        "cost": dict(cost) if cost else {},
    }


def _row_totals(rows: list[sqlite3.Row]) -> dict[str, Any]:
    totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for r in rows:
        totals["requests"] += int(r["requests"] or 0)
        totals["input_tokens"] += int(r["input_tokens"] or 0)
        totals["output_tokens"] += int(r["output_tokens"] or 0)
        totals["cost_usd"] += float(r["cost_usd"] or 0)
    return totals


def _print_human(name: str, payload: dict[str, Any]) -> None:
    if name == "today":
        if not payload or payload.get("requests") is None:
            print("No requests today.")
            return
        print("Today (UTC):")
        for k, v in payload.items():
            print(f"  {k:<20} {_fmt(v)}")
        return
    if name == "user":
        rows = payload["by_endpoint"]
        print(f"User {payload['user_id']} (last {payload['days'] or 'all'} days):")
        if rows:
            print(_format_table(rows, ["endpoint", "requests", "input_tokens", "output_tokens", "cost_usd"]))
        totals = payload["totals"]
        print(f"\nTotal: {totals['requests']} requests, ${totals['cost_usd']:.4f}")
        return
    if name == "model":
        print(f"By model (last {payload['days'] or 'all'} days):")
        print(_format_table(
            payload["by_model"],
            ["model_id", "requests", "input_tokens", "output_tokens", "cost_usd"],
        ))
        return
    if name == "top-spenders":
        print(f"Top {payload['limit']} spenders (last {payload['days'] or 'all'} days):")
        print(_format_table(
            payload["spenders"],
            ["user_id", "requests", "input_tokens", "output_tokens", "cost_usd"],
        ))
        return
    if name == "endpoint":
        print(f"Endpoint {payload['endpoint']} (last {payload['days'] or 'all'} days):")
        for k, v in payload["summary"].items():
            print(f"  {k:<20} {_fmt(v)}")
        if payload["by_status"]:
            print("\nBy status:")
            print(_format_table(payload["by_status"], ["status", "n"]))
        return
    if name == "request":
        print(f"Request {payload['request_id']}:")
        if payload["phases"]:
            print(_format_table(
                payload["phases"],
                ["phase", "input_tokens", "output_tokens", "cost_usd", "latency_ms", "status"],
            ))
        else:
            print("(not found)")
        return
    if name == "charts":
        rate = payload["render_rate"]
        rate_str = f"{rate * 100:.0f}%" if rate is not None else "—"
        print(f"Code Interpreter / chart runs (last {payload['days'] or 'all'} days):")
        print(f"  total runs: {payload['total_runs']}   render rate: {rate_str}")
        if payload["by_outcome"]:
            print(_format_table(
                payload["by_outcome"],
                ["outcome", "n", "avg_ms", "avg_exec", "avg_write"],
            ))
        if payload["skip_reasons"]:
            print("\nSkip reasons:")
            print(_format_table(payload["skip_reasons"], ["skip_reason", "n"]))
        if payload["failure_stages"]:
            print("\nFailure stages:")
            print(_format_table(payload["failure_stages"], ["failure_stage", "n"]))
        lat = payload["latency_ms"]
        print(f"\nLatency ms: p50={_fmt(lat['p50'])}  avg={_fmt(lat['avg'])}")
        c = payload["cost"]
        print(
            f"Chart cost: ${_fmt(c.get('cost_usd'))}  "
            f"tokens in={_fmt(c.get('input_tokens'))} out={_fmt(c.get('output_tokens'))}"
        )
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="hf-workbench agent metrics CLI")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit JSON instead of a table",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("today", help="Today's totals", parents=[common])

    p_user = sub.add_parser("user", help="Per-user breakdown", parents=[common])
    p_user.add_argument("user_id")
    p_user.add_argument("--days", type=int, default=30)

    p_model = sub.add_parser("model", help="Group by model_id", parents=[common])
    p_model.add_argument("--days", type=int, default=30)

    p_top = sub.add_parser("top-spenders", help="Users ranked by cost", parents=[common])
    p_top.add_argument("--days", type=int, default=30)
    p_top.add_argument("--limit", type=int, default=10)

    p_ep = sub.add_parser("endpoint", help="Per-endpoint summary", parents=[common])
    p_ep.add_argument("endpoint")
    p_ep.add_argument("--days", type=int, default=30)

    p_req = sub.add_parser("request", help="Per-phase breakdown for one request_id", parents=[common])
    p_req.add_argument("request_id")

    p_charts = sub.add_parser("charts", help="Code Interpreter (chart) run stats", parents=[common])
    p_charts.add_argument("--days", type=int, default=30)

    args = parser.parse_args()

    handlers = {
        "today": cmd_today,
        "user": cmd_user,
        "model": cmd_model,
        "top-spenders": cmd_top_spenders,
        "endpoint": cmd_endpoint,
        "request": cmd_request,
        "charts": cmd_charts,
    }
    payload = handlers[args.cmd](args)

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_human(args.cmd, payload)


if __name__ == "__main__":
    main()

"""Re-promotion sweep for stuck candidate theses (pm2 `hf-pipeline`, own job).

System-discovered theses (``origin='system'``) enter as ``review_status='candidate'``
and are promoted to ``active`` at creation only if a matched story link already
exists. The ingest-time matcher (``match_thesis_for_story``) keeps linking new
stories to candidates after creation, but promotion never re-runs — so a
candidate that earns its evidence late stays stuck forever. That stuck state is
what trips the ``thesis.stuck_candidates`` health alarm.

This sweep re-runs the promotion gates over every candidate against the links
accrued so far: it promotes those that now qualify, and retires ones that are
both well past their window and still evidence-less. It owns no other pipeline
concern — it reads/writes only ``review_status`` — and is registered as an
independent scheduler job, so a pipeline-cycle failure never blocks promotion
and vice versa.

Run standalone:  uv run python -m agents.repromote_candidates
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.pipeline_metrics import append_metric
from src.thesis.discover import repromote_candidate

DB_PATH = ROOT / "db" / "hf.db"
DEFAULT_MAX_STALE_DAYS = int(os.getenv("HF_REPROMOTION_MAX_STALE_DAYS", "30"))


@dataclass
class SweepResult:
    candidates_seen: int = 0
    promoted: int = 0
    rejected: int = 0
    still_candidate: int = 0
    transitions: list[dict[str, str]] = field(default_factory=list)


def _candidate_ids(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        rows = conn.execute(
            "SELECT id FROM theses WHERE review_status = 'candidate' "
            "ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def repromote_candidates(
    root: Path,
    db_path: Path,
    *,
    max_stale_days: int,
) -> SweepResult:
    """Re-run promotion gates over every candidate thesis; return a tally."""
    result = SweepResult()
    for thesis_id in _candidate_ids(db_path):
        result.candidates_seen += 1
        try:
            status = repromote_candidate(
                root, thesis_id, db_path, max_stale_days=max_stale_days
            )
        except Exception as exc:  # one bad thesis must not abort the sweep
            print(
                f"repromote: {thesis_id} skipped — {exc!r}",
                file=sys.stderr,
            )
            result.still_candidate += 1
            continue
        if status == "active":
            result.promoted += 1
        elif status == "rejected":
            result.rejected += 1
        else:
            result.still_candidate += 1
        if status != "candidate":
            result.transitions.append({"thesis_id": thesis_id, "to": status})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-run promotion gates over stuck candidate theses."
    )
    parser.add_argument(
        "--max-stale-days",
        type=int,
        default=DEFAULT_MAX_STALE_DAYS,
        help=(
            "Reject candidates older than this with still zero story links "
            f"(default: {DEFAULT_MAX_STALE_DAYS})."
        ),
    )
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    result = repromote_candidates(
        ROOT, DB_PATH, max_stale_days=args.max_stale_days
    )
    duration = round(time.perf_counter() - started, 3)

    append_metric({
        "event": "repromotion_run",
        "run_id": run_id,
        "duration_s": duration,
        "max_stale_days": args.max_stale_days,
        **{k: v for k, v in asdict(result).items() if k != "transitions"},
        "transitions": result.transitions,
    })
    print(json.dumps({"run_id": run_id, "duration_s": duration, **asdict(result)}, indent=2))


if __name__ == "__main__":
    main()

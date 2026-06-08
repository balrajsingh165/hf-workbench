"""Score theses: write score_freshness + score_tailwind + composite to theses.

The score is intrinsic to the belief, not the holder — freshness derives from
the global `thesis_story_links` timeline, tailwind from market price action on
the tagged tickers. Neither has any per-user input, so the score lives on the
`theses` row and is computed once per thesis (not once per owner).

Freshness uses the `thesis_story_links` timeline. Tailwind uses the
`src.clients.prices` scheduled-job router: thesis tickers are canonicalised
through the instruments registry (`USDJPY` → `JPY=X`, `BTC` → `BTC-USD`),
then routed to Alpaca (US equities/ETFs) or Mesh→Yahoo (everything else) for
window returns. `compute_tailwind` maps the 1-month return to 0–100.

Composite = average(freshness, tailwind) when tailwind is non-null; else
freshness alone. `status` is never written here — auto stress-flip is deferred
(see TODO.md → "Auto stress-flip (deferred)").

Scope: every *live* thesis is scored — any thesis with at least one
non-resolved owner, plus unowned `review_status='active'` theses (the system
proposals surfaced in Discover / on story pages). Pass `--thesis` to score one
thesis regardless of ownership or review_status (used by the post-creation
pipeline to score a freshly created thesis immediately).

Re-runnable and idempotent. Call after matching so the links table is current.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients.prices import canonicalize, window_return_pcts
from src.thesis.docs import parse_thesis_markdown
from src.thesis.story_links import load_links_for_thesis
from src.thesis.scoring import compute_freshness, compute_tailwind


@dataclass(slots=True)
class _Target:
    thesis_id: str
    horizon_days: int  # from theses.horizon_days (NOT NULL); always positive


def _load_targets(db_path: Path, thesis_id: str | None) -> list[_Target]:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if thesis_id is not None:
            # Single-thesis mode: score it regardless of ownership /
            # review_status (post-creation pipeline scores a brand-new,
            # still-unowned candidate this way).
            rows = conn.execute(
                "SELECT id AS thesis_id, horizon_days FROM theses WHERE id = ?",
                (thesis_id,),
            ).fetchall()
        else:
            # A thesis is "live" — and therefore worth scoring — if at least
            # one user still tracks it (non-resolved owner) OR it is an active
            # system proposal (review_status='active', possibly unowned).
            rows = conn.execute(
                "SELECT t.id AS thesis_id, t.horizon_days "
                "FROM theses t "
                "WHERE t.review_status = 'active' "
                "   OR EXISTS (SELECT 1 FROM user_theses ut "
                "              WHERE ut.thesis_id = t.id AND ut.status != 'resolved') "
                "ORDER BY t.id"
            ).fetchall()
    finally:
        conn.close()
    return [
        _Target(thesis_id=r["thesis_id"], horizon_days=r["horizon_days"])
        for r in rows
    ]


def _write_score(
    db_path: Path,
    target: _Target,
    freshness: int,
    tailwind: int | None,
    composite: int,
    *,
    touch_tailwind: bool,
    snapshot_date: str,
) -> None:
    """Persist scores to theses, then mirror the row into thesis_snapshots
    keyed by snapshot_date (idempotent same-day overwrite).

    When `touch_tailwind=False`, leave `theses.score_tailwind` alone
    (e.g. `--no-prices` run shouldn't wipe a previously-computed value);
    the snapshot still records whatever is in the row after the update,
    so the snapshot row and the live row always agree.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            if touch_tailwind:
                conn.execute(
                    "UPDATE theses "
                    "SET score_freshness = ?, score_tailwind = ?, score = ? "
                    "WHERE id = ?",
                    (freshness, tailwind, composite, target.thesis_id),
                )
            else:
                conn.execute(
                    "UPDATE theses "
                    "SET score_freshness = ?, score = ? "
                    "WHERE id = ?",
                    (freshness, composite, target.thesis_id),
                )
            row = conn.execute(
                "SELECT score, score_freshness, score_tailwind FROM theses "
                "WHERE id = ?",
                (target.thesis_id,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT INTO thesis_snapshots "
                    "(thesis_id, snapshot_date, score, score_freshness, score_tailwind, created_at) "
                    "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
                    "ON CONFLICT(thesis_id, snapshot_date) DO UPDATE SET "
                    "score = excluded.score, "
                    "score_freshness = excluded.score_freshness, "
                    "score_tailwind = excluded.score_tailwind",
                    (target.thesis_id, snapshot_date, row[0], row[1], row[2]),
                )
    finally:
        conn.close()


def _load_thesis_directions(root: Path, thesis_id: str) -> list[tuple[str, str]]:
    """Parse per-ticker directions from the thesis markdown. Empty list if absent."""
    path = root / "global" / "theses" / f"{thesis_id}.md"
    if not path.exists():
        return []
    try:
        return parse_thesis_markdown(path).ticker_directions
    except ValueError:
        return []


def _canonicalize_directions(
    directions: list[tuple[str, str]],
    canonical_map: dict[str, str],
) -> list[tuple[str, str]]:
    """Replace raw tickers with canonical Yahoo symbols; drop unresolvable ones."""
    out: list[tuple[str, str]] = []
    for raw, direction in directions:
        canonical = canonical_map.get(raw)
        if canonical:
            out.append((canonical, direction))
    return out


def score_theses(
    root: Path,
    *,
    thesis_id: str | None = None,
    as_of: date | None = None,
    persist: bool = True,
    use_prices: bool = True,
) -> list[dict]:
    db_path = root / "db" / "hf.db"
    today = as_of or datetime.now(timezone.utc).date()

    targets = _load_targets(db_path, thesis_id)
    if not targets:
        print(
            f"score-theses: no targets (thesis={thesis_id or 'ALL'})",
            file=sys.stderr,
        )
        return []

    # Phase 1: parse ticker directions from every thesis so we can batch-
    # resolve canonical symbols and batch-fetch prices before the per-thesis
    # loop. One pass over the disk, one Mesh resolve_symbol per novel raw
    # symbol, one Mesh price_history batch.
    thesis_ids = sorted({t.thesis_id for t in targets})
    directions_by_thesis: dict[str, list[tuple[str, str]]] = {
        tid: _load_thesis_directions(root, tid) for tid in thesis_ids
    }

    canonical_map: dict[str, str] = {}
    returns: dict[str, float | None] = {}
    returns_5d: dict[str, float | None] = {}
    if use_prices:
        raw_symbols = sorted({
            raw
            for directions in directions_by_thesis.values()
            for raw, _ in directions
        })
        if raw_symbols:
            canonical_map = {raw: canonicalize(raw) for raw in raw_symbols}
            canonical_syms = sorted({s for s in canonical_map.values() if s})
            returns = window_return_pcts(canonical_syms, period="1mo")
            returns_5d = window_return_pcts(canonical_syms, period="5d")
    breakdown_by_thesis: dict[str, dict] = {}

    results: list[dict] = []
    for target in targets:
        # horizon_days is NOT NULL DEFAULT on theses, so every target carries a
        # positive horizon — no skip needed.
        links = load_links_for_thesis(db_path, target.thesis_id)
        freshness = compute_freshness(links, target.horizon_days, today)
        supports = sum(1 for l in links if l.relation == "supports")

        if use_prices:
            canonical_dirs = _canonicalize_directions(
                directions_by_thesis.get(target.thesis_id, []),
                canonical_map,
            )
            tailwind = compute_tailwind(canonical_dirs, returns)
            # Per-ticker breakdown so the API can synthesize price-move events
            # alongside news. Only one entry per (thesis, canonical symbol);
            # keep raw_symbol so the UI can render the user-facing ticker.
            breakdown_entries: list[dict] = []
            seen_canonical: set[str] = set()
            for raw, direction in directions_by_thesis.get(target.thesis_id, []):
                canonical = canonical_map.get(raw)
                if not canonical or canonical in seen_canonical:
                    continue
                seen_canonical.add(canonical)
                breakdown_entries.append({
                    "symbol": canonical,
                    "raw_symbol": raw,
                    "direction": direction,
                    "ret_5d": returns_5d.get(canonical),
                    "ret_1mo": returns.get(canonical),
                })
            breakdown_by_thesis[target.thesis_id] = {
                "as_of": today.isoformat(),
                "tickers": breakdown_entries,
            }
        else:
            tailwind = None

        if tailwind is not None:
            composite = round((freshness + tailwind) / 2)
        else:
            # No tailwind this run — read the existing value from the DB so the
            # composite doesn't regress to freshness-only.
            conn = sqlite3.connect(db_path, timeout=30)
            try:
                existing_tw = conn.execute(
                    "SELECT score_tailwind FROM theses WHERE id = ?",
                    (target.thesis_id,),
                ).fetchone()
            finally:
                conn.close()
            if existing_tw and existing_tw[0] is not None:
                composite = round((freshness + existing_tw[0]) / 2)
            else:
                composite = freshness

        if persist:
            _write_score(
                db_path,
                target,
                freshness,
                tailwind,
                composite,
                touch_tailwind=use_prices,
                snapshot_date=today.isoformat(),
            )

        results.append(
            {
                "thesis_id": target.thesis_id,
                "horizon_days": target.horizon_days,
                "supports": supports,
                "score_freshness": freshness,
                "score_tailwind": tailwind,
                "score": composite,
            }
        )
        tailwind_fmt = f"{tailwind:>3}" if tailwind is not None else "  -"
        print(
            f"score-theses: thesis={target.thesis_id} "
            f"horizon={target.horizon_days}d supports={supports} "
            f"freshness={freshness:>3} tailwind={tailwind_fmt} score={composite:>3}"
            + ("" if persist else " [dry-run]"),
            file=sys.stderr,
        )

    if persist and use_prices and breakdown_by_thesis:
        cache_path = root / "db" / "mesh_cache" / f"{today.isoformat()}_tailwind_breakdown.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(breakdown_by_thesis, indent=2, sort_keys=True))

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Score theses: read thesis_story_links, compute Freshness + "
            "Tailwind, write score_freshness + score_tailwind + score to theses."
        )
    )
    parser.add_argument(
        "--thesis",
        default=None,
        help="Thesis id like thesis_003. Omit to score every live thesis "
             "(non-resolved owners + active proposals).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute + print, but do not write to theses.",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Override 'today' (YYYY-MM-DD) for testing decay curves.",
    )
    parser.add_argument(
        "--no-prices",
        action="store_true",
        help=(
            "Skip Mesh price calls. Tailwind is left unchanged in DB row, "
            "composite falls back to freshness. Useful for offline iteration."
        ),
    )
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    results = score_theses(
        ROOT,
        thesis_id=args.thesis,
        as_of=as_of,
        persist=not args.dry_run,
        use_prices=not args.no_prices,
    )
    print(json.dumps({"as_of": (as_of or datetime.now(timezone.utc).date()).isoformat(), "scored": results}, indent=2))


if __name__ == "__main__":
    main()

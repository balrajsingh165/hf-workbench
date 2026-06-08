#!/usr/bin/env python3
"""
Rebuild users, global theses, and sample user-thesis ownership rows.

Usage:
    uv run python scripts/seed_theses_from_markdown.py
    uv run python scripts/seed_theses_from_markdown.py --dry-run
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.thesis.docs import parse_thesis_markdown


DB_PATH = ROOT / "db" / "hf.db"
USERS_DIR = ROOT / "users"
THESES_DIR = ROOT / "global" / "theses"

PROFILE_ID_RE = re.compile(r"^- \*\*ID\*\*:\s*(?P<id>\S+)\s*$", re.MULTILINE)
PROFILE_TITLE_RE = re.compile(r"^# User Profile(?::\s*(?P<name>.+?))?\s*$", re.MULTILINE)

# Default decay clock for any thesis without a curated value below. Keep in
# sync with src.thesis.scoring.HORIZON_DEFAULT_DAYS and theses.horizon_days.
DEFAULT_HORIZON_DAYS = 45

# Bootstrap ownership for the current prototype data. These relationships are
# product state, not thesis narrative, so they belong in DB seeding rather than
# in the markdown thesis files.
USER_THESIS_IDS = {
    "user_1": [
        "thesis_001",
        "thesis_002",
        "thesis_004",
        "thesis_005",
        "thesis_008",
        "thesis_009",
        "thesis_010",
        "thesis_011",
        "thesis_012",
    ],
    "user_2": [
        "thesis_003",
        "thesis_006",
        "thesis_007",
        "thesis_010",
    ],
}

# Per-thesis natural horizons (the thesis decay clock). Curated where the
# thesis has a clear time anchor (earnings cycle, season, policy meeting);
# anything not listed seeds at DEFAULT_HORIZON_DAYS. Horizon is intrinsic to
# the belief and never user-entered.
THESIS_HORIZON_DAYS: dict[str, int] = {
    "thesis_001": 90,   # Fed pivot delayed past Q3 — ~3mo policy cycle
    "thesis_002": 60,   # Onshoring CapEx broadening — 2mo earnings catalyst window
    "thesis_003": 45,   # Energy crunch through summer driving season — ~6 weeks
    "thesis_004": 90,   # Nuclear buildout outruns uranium supply — structural, 3mo slice
    "thesis_005": 60,   # Oral GLP-1 market expansion — FDA/commercial cycle
    "thesis_006": 90,   # NATO 5% GDP re-rating — multi-year, 3mo MVP slice
    "thesis_007": 90,   # Central bank gold buying structural — structural macro
    "thesis_008": 60,   # BOJ carry unwind — event-driven, 2mo window
    "thesis_009": 60,   # AI adoption gap — quarterly earnings cycle
    "thesis_010": 60,   # Fiscal term premium repricing — fiscal cycle
    "thesis_011": 45,   # CPU inference re-rates OEMs — quarterly earnings
    "thesis_012": 90,   # Broadcom custom silicon dominance — structural
}


@dataclass(slots=True)
class UserSeedRow:
    user_id: str
    display_name: str


def _load_users(users_dir: Path = USERS_DIR) -> list[UserSeedRow]:
    users: list[UserSeedRow] = []
    for path in sorted(users_dir.glob("*/profile.md")):
        markdown = path.read_text(encoding="utf-8")
        id_match = PROFILE_ID_RE.search(markdown)
        if not id_match:
            raise ValueError(f"Missing user id in {path}")

        user_id = id_match.group("id")
        title_match = PROFILE_TITLE_RE.search(markdown)
        display_name = (
            title_match.group("name").strip()
            if title_match and title_match.group("name")
            else user_id
        )
        users.append(
            UserSeedRow(
                user_id=user_id,
                display_name=display_name,
            )
        )
    return users


def _load_thesis_ids(theses_dir: Path = THESES_DIR) -> list[str]:
    return [
        parse_thesis_markdown(path).thesis_id
        for path in sorted(theses_dir.glob("thesis_*.md"))
    ]


def seed_thesis_tables(
    db_path: Path = DB_PATH,
    *,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    users = _load_users()
    thesis_ids = _load_thesis_ids()
    known_user_ids = {user.user_id for user in users}
    known_thesis_ids = set(thesis_ids)

    for user_id, owned_thesis_ids in USER_THESIS_IDS.items():
        if user_id not in known_user_ids:
            raise ValueError(f"Ownership map references missing user: {user_id}")
        missing = sorted(set(owned_thesis_ids) - known_thesis_ids)
        if missing:
            raise ValueError(f"Ownership map references missing theses for {user_id}: {missing}")

    ownership_rows = [
        (user_id, thesis_id)
        for user_id, owned_thesis_ids in USER_THESIS_IDS.items()
        for thesis_id in owned_thesis_ids
    ]
    if dry_run:
        return len(users), len(thesis_ids), len(ownership_rows)

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            # Idempotent upserts — safe to re-run over a populated DB without
            # running init_db() first, and FK-safe against downstream tables
            # like thesis_match_chunks that reference theses(id). If you need
            # a clean slate, call init_db(tables=[...]) first.
            conn.execute("DELETE FROM user_theses")

            for user in users:
                conn.execute(
                    """
                    INSERT INTO users (id, display_name, created_at)
                    VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name
                    """,
                    (user.user_id, user.display_name),
                )

            for thesis_id in thesis_ids:
                # Horizon is intrinsic to the thesis; seed it here (curated or
                # default) and refresh it on re-run so curated edits take.
                horizon = THESIS_HORIZON_DAYS.get(thesis_id, DEFAULT_HORIZON_DAYS)
                conn.execute(
                    """
                    INSERT INTO theses (id, owner_count, horizon_days, created_at)
                    VALUES (?, 0, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    ON CONFLICT(id) DO UPDATE SET horizon_days=excluded.horizon_days
                    """,
                    (thesis_id, horizon),
                )

            for user_id, thesis_id in ownership_rows:
                conn.execute(
                    """
                    INSERT INTO user_theses (user_id, thesis_id, status, created_at)
                    VALUES (?, ?, 'active', strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    """,
                    (user_id, thesis_id),
                )

            conn.execute(
                """
                UPDATE theses
                SET owner_count = (
                    SELECT COUNT(*)
                    FROM user_theses
                    WHERE user_theses.thesis_id = theses.id
                )
                """
            )
    finally:
        conn.close()

    return len(users), len(thesis_ids), len(ownership_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize users/theses/user_theses from prototype markdown data."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files and report counts without writing to SQLite.",
    )
    args = parser.parse_args()

    user_count, thesis_count, ownership_count = seed_thesis_tables(dry_run=args.dry_run)
    action = "Parsed" if args.dry_run else "Seeded"
    print(
        f"{action} {user_count} users, {thesis_count} theses, "
        f"{ownership_count} user-thesis rows."
    )


if __name__ == "__main__":
    main()

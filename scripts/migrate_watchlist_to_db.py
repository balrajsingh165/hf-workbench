"""One-shot migration: `## Watchlist` in users/{id}/profile.md → user_watchlist.

See docs/design-watchlist.md §Migration. Dry-run by default; pass --write to
insert. Unresolved symbols are reported and skipped — fix aliases in
src/instruments/seed.py and re-run (inserts are idempotent via the PK).

Usage:
    uv run python scripts/migrate_watchlist_to_db.py            # dry-run report
    uv run python scripts/migrate_watchlist_to_db.py --write    # insert rows
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.schema import DB_PATH  # noqa: E402
from src.personalization.parser import PROFILE_ROOT, parse_profile_md  # noqa: E402
from src.personalization.watchlist import add_symbol, resolve_symbol  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="insert rows (default: dry-run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    any_skipped = False
    try:
        user_ids = [
            row[0]
            for row in conn.execute("SELECT id FROM users ORDER BY id").fetchall()
        ]
        for user_id in user_ids:
            profile = parse_profile_md(user_id, root=PROFILE_ROOT)
            if not profile.watchlist:
                print(f"{user_id}: no markdown watchlist — nothing to migrate")
                continue
            resolved: list[tuple[str, str]] = []
            skipped: list[str] = []
            for raw in profile.watchlist:
                canonical = resolve_symbol(raw, conn)
                if canonical is None:
                    skipped.append(raw)
                    continue
                resolved.append((raw, canonical))
                if args.write:
                    add_symbol(user_id, raw, conn)
            any_skipped = any_skipped or bool(skipped)
            verb = "migrated" if args.write else "would migrate"
            pairs = ", ".join(
                raw if raw == canon else f"{raw}→{canon}" for raw, canon in resolved
            )
            print(f"{user_id}: {verb} {len(resolved)} [{pairs}]")
            if skipped:
                print(
                    f"{user_id}: SKIPPED {len(skipped)} (no instrument): "
                    f"{', '.join(skipped)}"
                )
    finally:
        conn.close()

    if not args.write:
        print("\nDry-run only. Re-run with --write to insert.")
    if any_skipped:
        print(
            "\nSome symbols did not resolve — add instrument rows/aliases "
            "(src/instruments/seed.py) and re-run; inserts are idempotent."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

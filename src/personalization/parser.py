"""Parse the seeded ``users/{id}/profile.md`` into a structured record.

The spike reads from the markdown file directly — no DB migration. Format
is the structured header used by ``users/1/profile.md`` and ``users/2/profile.md``:

    # User Profile
    - **ID**: user_1
    - **Experience**: intermediate
    - **Risk Tolerance**: moderate

    ## Preferences
    - **Asset Classes**: stocks, crypto
    - **Sectors of Interest**: semiconductors, AI infrastructure

    ## Watchlist
    - NVDA
    - TSMC

The parser is tolerant: missing sections render as empty lists / None. Per
the design's first-day-credibility rule, we deliberately do not try to
infer missing values; an empty slot stays empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


PROFILE_ROOT = Path(__file__).resolve().parents[2] / "users"


@dataclass(slots=True)
class StoredProfile:
    user_id: str
    experience: str | None = None
    risk_tolerance: str | None = None
    asset_classes: list[str] = field(default_factory=list)
    sectors_of_interest: list[str] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)
    excluded_strategies: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (
                self.experience,
                self.risk_tolerance,
                self.asset_classes,
                self.sectors_of_interest,
                self.watchlist,
                self.excluded_strategies,
            )
        )

    def populated_slot_count(self) -> int:
        return sum(
            bool(v)
            for v in (
                self.experience,
                self.risk_tolerance,
                self.asset_classes,
                self.sectors_of_interest,
                self.watchlist,
                self.excluded_strategies,
            )
        )


_KV_RE = re.compile(r"^-\s*\*\*([^*]+)\*\*\s*:\s*(.+?)\s*$")


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_profile_md(user_id: str, root: Path | None = None) -> StoredProfile:
    """Parse ``users/{user_id}/profile.md``. Missing file → empty profile."""

    root = root or PROFILE_ROOT
    profile = StoredProfile(user_id=user_id)

    candidates = [user_id]
    if user_id.startswith("user_"):
        candidates.append(user_id.removeprefix("user_"))
    path: Path | None = None
    for name in candidates:
        candidate = root / name / "profile.md"
        if candidate.exists():
            path = candidate
            break
    if path is None:
        return profile

    section: str | None = None
    list_buffer: list[str] = []

    def flush_list() -> None:
        nonlocal list_buffer
        if section == "Watchlist":
            profile.watchlist.extend(s for s in list_buffer if s)
        list_buffer = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        if line.startswith("## "):
            flush_list()
            section = line.removeprefix("## ").strip()
            continue
        if line.startswith("# "):
            flush_list()
            section = None
            continue

        kv = _KV_RE.match(line)
        if kv:
            key = kv.group(1).strip().lower()
            value = kv.group(2).strip()
            if key == "experience":
                profile.experience = value.lower()
            elif key == "risk tolerance":
                profile.risk_tolerance = value.lower()
            elif key == "asset classes":
                profile.asset_classes = _split_csv(value.lower())
            elif key == "sectors of interest":
                profile.sectors_of_interest = _split_csv(value.lower())
            elif key == "excluded strategies":
                profile.excluded_strategies = _split_csv(value)
            continue

        if line.lstrip().startswith("- ") and section == "Watchlist":
            symbol = line.lstrip()[2:].strip()
            if symbol:
                list_buffer.append(symbol)

    flush_list()
    return profile

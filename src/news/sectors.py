"""Compatibility exports for the news sector taxonomy."""

from __future__ import annotations

from src.news.taxonomies import CANONICAL_SECTORS, normalize_sectors

ALIAS_MAP: dict[str, str] = {
    sector: sector for sector in CANONICAL_SECTORS
}

__all__ = ["CANONICAL_SECTORS", "ALIAS_MAP", "normalize_sectors"]

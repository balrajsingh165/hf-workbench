"""Canonical sector and region taxonomy for news clusters and stories."""

from __future__ import annotations


CANONICAL_REGIONS: tuple[str, ...] = (
    "north_america",
    "europe",
    "uk",
    "japan",
    "china",
    "asia_ex_china",
    "australasia",
    "latam",
    "middle_east",
    "africa",
    "global",
)

SECTOR_HIERARCHY: dict[str, tuple[str, ...]] = {
    "technology": (
        "semiconductor",
        "software",
        "internet",
        "consumer_electronics",
        "ai_infrastructure",
        "cybersecurity",
    ),
    "financials": ("banks", "insurance", "asset_managers", "exchanges", "fintech"),
    "energy": ("oil_gas", "renewables", "utilities", "uranium"),
    "industrials": (
        "aerospace_defense",
        "transportation",
        "machinery",
        "construction",
    ),
    "healthcare": ("biotech", "pharma", "medtech", "healthcare_services"),
    "materials": ("metals_mining", "chemicals", "critical_minerals"),
    "consumer": (
        "consumer_discretionary",
        "consumer_staples",
        "luxury",
        "autos",
    ),
    "real_estate": ("reits", "homebuilders"),
    "communication": ("telecom", "media", "gaming"),
    "crypto": ("bitcoin", "ethereum", "defi", "stablecoins"),
    "macro": ("rates", "fx", "commodities", "trade_policy", "sovereign_credit"),
}

CANONICAL_SECTORS: tuple[str, ...] = tuple(
    f"{parent}.{leaf}"
    for parent, leaves in SECTOR_HIERARCHY.items()
    for leaf in leaves
)

SECTOR_PARENT_BY_LEAF: dict[str, str] = {
    f"{parent}.{leaf}": parent
    for parent, leaves in SECTOR_HIERARCHY.items()
    for leaf in leaves
}

_OLD_SECTOR_ALIASES: dict[str, tuple[str, ...]] = {
    "aerospace": ("industrials.aerospace_defense",),
    "agriculture": ("consumer.consumer_staples",),
    "artificial intelligence": ("technology.ai_infrastructure",),
    "automotive": ("consumer.autos",),
    "aviation": ("industrials.transportation",),
    "commodities": ("macro.commodities",),
    "consumer discretionary": ("consumer.consumer_discretionary",),
    "consumer electronics": ("technology.consumer_electronics",),
    "consumer staples": ("consumer.consumer_staples",),
    "cryptocurrency": ("crypto.bitcoin", "crypto.ethereum"),
    "cybersecurity": ("technology.cybersecurity",),
    "defense": ("industrials.aerospace_defense",),
    "energy": ("energy.oil_gas",),
    "oil & gas": ("energy.oil_gas",),
    "oil and gas": ("energy.oil_gas",),
    "financial services": ("financials.banks",),
    "geopolitics": ("macro.trade_policy",),
    "government & policy": ("macro.trade_policy",),
    "healthcare": ("healthcare.healthcare_services",),
    "industrials": ("industrials.machinery",),
    "international trade": ("macro.trade_policy",),
    "legal": ("macro.trade_policy",),
    "macro": ("macro.rates",),
    "materials": ("materials.metals_mining",),
    "media & entertainment": ("communication.media",),
    "national security": ("industrials.aerospace_defense",),
    "real estate": ("real_estate.reits",),
    "retail": ("consumer.consumer_discretionary",),
    "semiconductors": ("technology.semiconductor",),
    "technology": ("technology.software",),
    "transportation & logistics": ("industrials.transportation",),
    "travel & tourism": ("consumer.consumer_discretionary",),
}

_SECTOR_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    **{sector: (sector,) for sector in CANONICAL_SECTORS},
    **{sector.split(".", 1)[1]: (sector,) for sector in CANONICAL_SECTORS},
    **_OLD_SECTOR_ALIASES,
}


def normalize_sectors(raw: list[str] | tuple[str, ...]) -> list[str]:
    """Return canonical `parent.leaf` sector ids, deduped in input order."""
    seen: set[str] = set()
    out: list[str] = []
    for label in raw or ():
        key = str(label or "").strip().lower().replace(" ", "_")
        key_spaced = str(label or "").strip().lower()
        matches = _SECTOR_ALIAS_MAP.get(key) or _SECTOR_ALIAS_MAP.get(key_spaced) or ()
        for canonical in matches:
            if canonical in seen:
                continue
            seen.add(canonical)
            out.append(canonical)
    return out


def normalize_regions(raw: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for label in raw or ():
        key = str(label or "").strip().lower().replace(" ", "_")
        if key not in CANONICAL_REGIONS or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def sector_parent(sector: str) -> str | None:
    return SECTOR_PARENT_BY_LEAF.get(sector)


__all__ = [
    "CANONICAL_REGIONS",
    "CANONICAL_SECTORS",
    "SECTOR_HIERARCHY",
    "SECTOR_PARENT_BY_LEAF",
    "normalize_regions",
    "normalize_sectors",
    "sector_parent",
]

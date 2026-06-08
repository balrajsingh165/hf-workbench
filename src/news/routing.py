"""Cluster-level routing and promotion gate."""

from __future__ import annotations

from dataclasses import dataclass

from src.news.cluster import (
    PROMOTION_MAX_AGE_H_RELAXED,
    PROMOTION_MAX_AGE_H_STRICT,
    ClusterDecisionInput,
)
from src.news.taxonomies import sector_parent

SHARP_EVENT_CLASSES: frozenset[str] = frozenset({
    "fed_action",
    "macro_print",
    "m_a",
    "regulatory",
})

# Event classes where the institutional primary source IS the event:
# Fed/BLS/BEA/ECB releases for fed_action and macro_print are inherently
# market-moving and don't need cross-publisher corroboration to be a story.
INSTITUTIONAL_AUTOPROMOTE_CLASSES: frozenset[str] = frozenset({
    "fed_action",
    "macro_print",
})

MAINSTREAM_ASSET_SYMBOLS: frozenset[str] = frozenset({
    # Market indexes, rates, FX, commodities, and volatility.
    "^BSESN", "^DJI", "^GSPC", "^IXIC", "^NSEI", "^TNX", "^VIX",
    "BZ=F", "CL=F", "DX-Y.NYB", "DXY", "EURUSD=X", "GC=F", "JPY=X",
    "NG=F", "USDJPY",
    # Broad ETF and asset proxies.
    "ARKB", "DFEN", "DXJ", "EWJ", "EWU", "FBTC", "FXY", "GBTC", "GDX",
    "GLD", "IBIT", "PFIX", "QQQ", "SMH", "SPY", "TLT", "TMV", "UNG",
    "URA", "USO", "XLE",
    # Crypto majors and liquid crypto proxies.
    "BTC", "BTC-USD", "ETH-USD", "COIN",
    # Large/liquid equities and direct thesis-universe anchors.
    "005930.KS", "600346.SS", "9684.T", "AAPL", "ADP", "AEM", "AMD",
    "AMZN", "APD", "APLD", "ARM", "AVAV", "AVGO", "BA", "BA.L", "BR",
    "BRK-B", "BSX", "CAG", "CAVA", "CCI", "CCJ", "CEG", "CNI", "CNR",
    "CP", "CRM", "CRSP", "CSCO", "CTRA", "CTSH", "DE", "DELL",
    "DIS", "DNOW", "DUOL", "DVN", "EBAY", "EFX", "EMR", "ESLT",
    "EXPO", "F", "FANG", "FANUY", "FHN", "FTAI", "GEV", "GIB", "GIB.A",
    "GM", "GME", "GOOG", "GOOGL", "HIMS", "HLAG.DE", "HPE", "IBM",
    "INTC", "JBLU", "KBR", "KO", "KR", "KTOS", "LLY", "LMT",
    "LUV", "LYSDY", "META", "MHK", "MP", "MSFT", "MU",
    "NCLH", "NEM", "NNE", "NOC", "NOW", "NTDOY", "NVDA", "NVO", "NYT",
    "ON", "ORCL", "OXY", "PANW", "PEP", "PINS", "PLTR", "POWL", "PSN",
    "RHM.DE", "RIG", "ROK", "SONY", "SSNLF", "STZ", "TCOM", "TD", "TEAM",
    "TRI", "TSLA", "TSM", "TSN", "V", "VNO", "VTRS", "WBD",
    "WYNN", "XOM",
})

MAINSTREAM_SINGLE_SOURCE_EVENT_CLASSES: frozenset[str] = frozenset({
    "analyst_action",
    "commodity_move",
    "corporate_action",
    "earnings",
    "fed_action",
    "financing",
    "geopolitical",
    "guidance",
    "m_a",
    "macro_print",
    "market_flow",
    "regulatory",
    "trade_policy",
})

MAINSTREAM_SINGLE_SOURCE_MIN_MATERIALITY = 45

MACRO_COMMENTARY_EVENT_CLASSES: frozenset[str] = frozenset({
    "fed_action",
    "macro_print",
})

MACRO_COMMENTARY_MIN_MATERIALITY = 55


@dataclass(frozen=True, slots=True)
class Decision:
    route: str
    reason: str


def _is_fresh(cluster: ClusterDecisionInput, max_age_h: float) -> bool:
    """True when the freshest cluster member is within `max_age_h` hours.

    Clusters with no parseable member timestamp pass — we don't want to
    silently discard legacy/backfill rows that lack a published_at value.
    """
    return cluster.min_member_age_h is None or cluster.min_member_age_h <= max_age_h


def route_cluster(
    cluster: ClusterDecisionInput,
    *,
    active_thesis_tickers: set[str] | None = None,
    active_thesis_sectors: set[str] | None = None,
    active_thesis_regions: set[str] | None = None,
    has_macro_keyword: bool = False,
) -> Decision:
    thesis_tickers = active_thesis_tickers or set()
    thesis_sectors = active_thesis_sectors or set()
    thesis_regions = active_thesis_regions or set()

    if (
        cluster.max_materiality == 0
        and not (cluster.tickers & thesis_tickers)
        and not has_macro_keyword
        and not cluster.has_tier1_primary
        and not cluster.has_institutional_primary
    ):
        return Decision("discard", "zero materiality with no thesis, macro, or primary-source signal")

    fresh_strict = _is_fresh(cluster, PROMOTION_MAX_AGE_H_STRICT)
    fresh_relaxed = _is_fresh(cluster, PROMOTION_MAX_AGE_H_RELAXED)

    # R0: institutional primary-source events. The Fed publishing an FOMC
    # statement IS the event; the BLS releasing CPI IS the event. No
    # corroboration needed — the publisher is the source of truth.
    if (
        cluster.has_institutional_primary
        and cluster.event_class in INSTITUTIONAL_AUTOPROMOTE_CLASSES
        and cluster.max_materiality >= 50
        and fresh_strict
    ):
        return Decision("sharp_promote", "R0 institutional primary (fed/macro)")
    # R0b: regulator actions (FTC/SEC/FDA enforcement) at very high
    # materiality. Bar is higher than R0 because not every regulatory
    # filing is thesis-grade.
    if (
        cluster.has_institutional_primary
        and cluster.event_class == "regulatory"
        and cluster.max_materiality >= 70
        and fresh_relaxed
    ):
        return Decision("sharp_promote", "R0b regulator primary action")

    # R0c: heavy independent corroboration overrides the headline-regex
    # materiality score. A cluster that 3+ independent publisher groups
    # bothered to cover IS material — that's the strongest external signal
    # we have. Tier-1 anchor required so PR-wire echo chambers can't sneak
    # through. This catches geopolitical/macro stories where the headline
    # phrasing escapes the materiality regex (e.g. "Trump's Latest Iran
    # Threat Pushes Oil Prices Higher" — 6 publishers, Tier-1, mat=15).
    if (
        cluster.independent_pub_count >= 3
        and cluster.has_tier1_primary
        and fresh_relaxed
    ):
        return Decision("sharp_promote", "R0c heavy corroboration + tier1 (mat override)")

    # R1: tier-1 news org reports a high-materiality story. Real news
    # outlets (Reuters/AP/WSJ/FT/Bloomberg/etc.) only — PR wires don't
    # qualify as tier1_primary anymore.
    if cluster.max_materiality >= 30 and cluster.has_tier1_primary and fresh_relaxed:
        return Decision("sharp_promote", "R1 materiality>=30 and tier1 news")
    # R2: heavy corroboration without tier-1 — three independent groups
    # at moderate materiality is enough to ship.
    if cluster.independent_pub_count >= 3 and cluster.max_materiality >= 25 and fresh_relaxed:
        return Decision("sharp_promote", "R2 >=3 independent + mat>=25")
    # R2b: partially-corroborated tier-1 — a Tier-1 outlet reports at
    # moderate materiality with at least one cross-publisher pickup.
    if (
        cluster.independent_pub_count >= 2
        and cluster.has_tier1_primary
        and cluster.max_materiality >= 25
        and fresh_relaxed
    ):
        return Decision("sharp_promote", "R2b >=2 independent + tier1 + mat>=25")
    if (
        cluster.tickers & thesis_tickers
        and cluster.max_materiality >= 25
        and fresh_relaxed
    ):
        return Decision("sharp_promote", "R3 active thesis ticker overlap")
    # R4: sharp event classes (M&A, regulatory, macro) — but require some
    # source-quality signal so a lone PR-wire announcement doesn't auto-
    # promote. Real major M&A gets Reuters/Bloomberg pickup; lone-wire M&A
    # announcements are rumor-grade until corroborated.
    if (
        cluster.event_class in SHARP_EVENT_CLASSES
        and cluster.max_materiality >= 35
        and (
            cluster.has_tier1_primary
            or cluster.has_institutional_primary
            or cluster.independent_pub_count >= 2
        )
        and fresh_strict
    ):
        return Decision("sharp_promote", "R4 sharp event class with corroboration")
    # R5/R6: thesis sector or region overlap. Both are coarse signals — a
    # thesis tagged `technology.semiconductor` shouldn't auto-promote every
    # tech PR wire, and a thesis on `north_america` shouldn't promote every
    # US-listed earnings release. Require some corroboration so a lone
    # PR-wire announcement doesn't slip through on overlap alone.
    if (
        cluster.sectors & thesis_sectors
        and cluster.max_materiality >= 25
        and (
            cluster.has_tier1_primary
            or cluster.has_institutional_primary
            or cluster.independent_pub_count >= 2
        )
        and fresh_relaxed
    ):
        return Decision("sharp_promote", "R5 active thesis sector overlap with corroboration")
    if (
        cluster.regions & thesis_regions
        and cluster.max_materiality >= 25
        and (
            cluster.has_tier1_primary
            or cluster.has_institutional_primary
            or cluster.independent_pub_count >= 2
        )
        and fresh_relaxed
    ):
        return Decision("sharp_promote", "R6 active thesis region overlap with corroboration")

    # R7: mainstream asset, single-source. This is the high-volume lane for
    # market-wide assets and large/liquid equities when the story is material
    # but lacks tier-1/institutional/corroboration signals. The whitelist keeps
    # one-source long-tail PR wires from becoming product stories. Strict
    # recency cap because a single source has no corroboration to fall back on
    # if the story is stale or speculative.
    if (
        cluster.tickers & MAINSTREAM_ASSET_SYMBOLS
        and cluster.event_class in MAINSTREAM_SINGLE_SOURCE_EVENT_CLASSES
        and cluster.max_materiality >= MAINSTREAM_SINGLE_SOURCE_MIN_MATERIALITY
        and cluster.has_non_pr_news_primary
        and not cluster.has_press_wire_primary
        and fresh_strict
    ):
        return Decision("sharp_promote", "R7 mainstream asset single-source")

    # R8: macro/Fed commentary that is mainstream-by-subject (CPI, FOMC, rates)
    # but never gets ticker-tagged, so R7 cannot see it. Strict materiality and
    # non-PR-wire gate keeps the firehose noise out.
    if (
        cluster.event_class in MACRO_COMMENTARY_EVENT_CLASSES
        and cluster.max_materiality >= MACRO_COMMENTARY_MIN_MATERIALITY
        and cluster.has_non_pr_news_primary
        and not cluster.has_press_wire_primary
        and fresh_strict
    ):
        return Decision("sharp_promote", "R8 macro commentary single-source")

    return Decision("firehose_store", "default firehose route")


def subject_key(sectors: set[str], regions: set[str]) -> tuple[str, str]:
    sector = sorted(sectors)[0] if sectors else "none"
    region = sorted(regions)[0] if regions else "global"
    return (sector_parent(sector) or sector, region)


__all__ = [
    "Decision",
    "MACRO_COMMENTARY_EVENT_CLASSES",
    "MAINSTREAM_ASSET_SYMBOLS",
    "MAINSTREAM_SINGLE_SOURCE_EVENT_CLASSES",
    "SHARP_EVENT_CLASSES",
    "route_cluster",
    "subject_key",
]

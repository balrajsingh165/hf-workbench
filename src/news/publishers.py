"""Publisher registry for feed metadata and cluster independence scoring."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from src.news.taxonomies import normalize_regions, normalize_sectors

Tier = str
Kind = str
EditorialStance = str


@dataclass(frozen=True, slots=True)
class Publisher:
    name: str
    tier: Tier
    kind: Kind
    independence_group: str
    primary_sectors: tuple[str, ...] = ()
    primary_regions: tuple[str, ...] = ()
    editorial_stance: EditorialStance = "neutral"
    language: str = "en"
    hosts: tuple[str, ...] = ()

    @property
    def counts_as_independent(self) -> bool:
        return self.editorial_stance != "state_media"

    @property
    def is_tier1_news(self) -> bool:
        """Real tier-1 news/wire reporting outlet — Reuters, AP, WSJ, FT,
        Bloomberg, MarketWatch, NYT, etc. Excludes PR wires and institutional
        primary sources (Fed/BLS/regulators)."""
        return self.kind == "news" and self.tier in {"wire", "tier1"}

    @property
    def is_institutional_primary(self) -> bool:
        """Primary-source institutions whose press release IS the event:
        regulators, central banks, exchanges. Auto-promotion candidate for
        macro_print / fed_action / regulatory event classes."""
        return self.kind in {"regulator", "central_bank", "exchange"}

    @property
    def is_tier1_primary(self) -> bool:
        """Legacy alias retained for callers that check 'is this an
        authoritative source'. True for tier1 news OR institutional primary."""
        return self.is_tier1_news or self.is_institutional_primary


def _pub(
    name: str,
    tier: Tier,
    kind: Kind,
    independence_group: str,
    *,
    sectors: tuple[str, ...] = (),
    regions: tuple[str, ...] = (),
    editorial_stance: EditorialStance = "neutral",
    language: str = "en",
    hosts: tuple[str, ...] = (),
) -> Publisher:
    return Publisher(
        name=name,
        tier=tier,
        kind=kind,
        independence_group=independence_group,
        primary_sectors=tuple(normalize_sectors(sectors)),
        primary_regions=tuple(normalize_regions(regions)),
        editorial_stance=editorial_stance,
        language=language,
        hosts=hosts,
    )


PUBLISHERS: tuple[Publisher, ...] = (
    _pub("Reuters", "wire", "news", "reuters", hosts=("reuters.com",)),
    _pub("AP", "wire", "news", "ap", hosts=("apnews.com", "ap.org")),
    _pub("AFP", "wire", "news", "afp", hosts=("afp.com",)),
    _pub("Yahoo Finance", "aggregator", "news", "yahoo", hosts=("finance.yahoo.com",)),
    _pub("MarketWatch", "tier1", "news", "dow-jones", hosts=("marketwatch.com",)),
    # dj.com is the Dow Jones syndication domain (feeds.a.dj.com pipes WSJ
    # articles). The feed URL is what publisher_for_url sees during ingest;
    # without this host dj.com feeds fall through to a generic publisher.
    _pub("WSJ", "tier1", "news", "dow-jones", hosts=("wsj.com", "dj.com")),
    _pub("Barron's", "tier1", "news", "dow-jones", hosts=("barrons.com",)),
    _pub("Financial Times", "tier1", "news", "ft", hosts=("ft.com",)),
    _pub("CNBC", "tier1", "news", "cnbc", hosts=("cnbc.com",)),
    _pub("Bloomberg", "tier1", "news", "bloomberg", hosts=("bloomberg.com",)),
    _pub("The Economist", "tier1", "news", "economist", hosts=("economist.com",)),
    _pub("NYT", "tier1", "news", "nyt", hosts=("nytimes.com",)),
    _pub("The Washington Post", "tier1", "news", "washington-post", hosts=("washingtonpost.com",)),
    # bbci.co.uk is the BBC's RSS host (feeds.bbci.co.uk).
    _pub("BBC", "tier1", "news", "bbc", hosts=("bbc.com", "bbc.co.uk", "bbci.co.uk")),
    _pub("The Guardian", "tier1", "news", "guardian", hosts=("theguardian.com",)),
    _pub("Axios", "tier1", "news", "axios", hosts=("axios.com",)),
    _pub("Politico", "tier1", "news", "politico", hosts=("politico.com",)),
    # Retail-investor-focused US financial press. tier2 — below WSJ/FT for
    # institutional reporting but a strong distinct editorial group for the
    # swing/multi-week trader audience.
    _pub("Investor's Business Daily", "tier2", "news", "ibd", regions=("north_america",), hosts=("investors.com",)),
    _pub("Benzinga", "tier2", "news", "benzinga", regions=("north_america",), hosts=("benzinga.com",)),
    _pub("The Information", "trade", "news", "the-information", sectors=("technology.ai_infrastructure",), hosts=("theinformation.com",)),
    _pub("Nikkei Asia", "tier1", "news", "nikkei", regions=("japan", "asia_ex_china"), hosts=("asia.nikkei.com",)),
    _pub("Nikkei", "tier1", "news", "nikkei", regions=("japan",), hosts=("nikkei.com",)),
    _pub("South China Morning Post", "tier1", "news", "scmp", regions=("china", "asia_ex_china"), hosts=("scmp.com",)),
    _pub("PR Newswire", "wire", "pr_wire", "prnewswire", hosts=("prnewswire.com",)),
    _pub("GlobeNewswire", "wire", "pr_wire", "globenewswire", hosts=("globenewswire.com",)),
    _pub("Business Wire", "wire", "pr_wire", "businesswire", hosts=("businesswire.com",)),
    _pub("ACCESS Newswire", "wire", "pr_wire", "accesswire", hosts=("accesswire.com", "accessnewswire.com")),
    _pub("Federal Reserve", "wire", "central_bank", "federal-reserve", sectors=("macro.rates",), regions=("north_america",), hosts=("federalreserve.gov",)),
    _pub("BEA", "wire", "regulator", "bea", sectors=("macro.rates",), regions=("north_america",), hosts=("bea.gov",)),
    _pub("BLS", "wire", "regulator", "bls", sectors=("macro.rates",), regions=("north_america",), hosts=("bls.gov",)),
    _pub("ECB", "wire", "central_bank", "ecb", sectors=("macro.rates",), regions=("europe",), hosts=("ecb.europa.eu",)),
    _pub("EIA", "wire", "regulator", "eia", sectors=("energy.oil_gas", "macro.commodities"), regions=("north_america",), hosts=("eia.gov",)),
    _pub("Bank of England", "wire", "central_bank", "bank-of-england", sectors=("macro.rates",), regions=("uk",), hosts=("bankofengland.co.uk",)),
    _pub("RBA", "wire", "central_bank", "rba", sectors=("macro.rates",), regions=("australasia",), hosts=("rba.gov.au",)),
    _pub("FDA", "wire", "regulator", "fda", sectors=("healthcare.pharma", "healthcare.biotech"), regions=("north_america",), hosts=("fda.gov",)),
    _pub("SEC", "wire", "regulator", "sec", sectors=("financials.exchanges",), regions=("north_america",), hosts=("sec.gov",)),
    _pub("FTC", "wire", "regulator", "ftc", sectors=("macro.trade_policy",), regions=("north_america",), hosts=("ftc.gov",)),
    _pub("DOJ", "wire", "regulator", "doj", sectors=("macro.trade_policy",), regions=("north_america",), hosts=("justice.gov",)),
    _pub("DigiTimes Asia", "trade", "news", "digitimes", sectors=("technology.semiconductor",), regions=("asia_ex_china",), hosts=("digitimes.com",)),
    _pub("EE Times", "trade", "news", "ee-times", sectors=("technology.semiconductor",), hosts=("eetimes.com",)),
    _pub("AnandTech", "trade", "news", "anandtech", sectors=("technology.consumer_electronics", "technology.semiconductor"), hosts=("anandtech.com",)),
    _pub("Defense News", "trade", "news", "defense-news", sectors=("industrials.aerospace_defense",), hosts=("defensenews.com",)),
    _pub("Breaking Defense", "trade", "news", "breaking-defense", sectors=("industrials.aerospace_defense",), hosts=("breakingdefense.com",)),
    _pub("USNI News", "trade", "news", "usni", sectors=("industrials.aerospace_defense",), hosts=("news.usni.org",)),
    _pub("STAT News", "trade", "news", "stat", sectors=("healthcare.biotech",), regions=("north_america",), hosts=("statnews.com",)),
    _pub("FiercePharma", "trade", "news", "fiercepharma", sectors=("healthcare.pharma",), hosts=("fiercepharma.com",)),
    _pub("BioPharma Dive", "trade", "news", "biopharma-dive", sectors=("healthcare.pharma", "healthcare.biotech"), hosts=("biopharmadive.com",)),
    _pub("OilPrice", "trade", "news", "oilprice", sectors=("energy.oil_gas", "macro.commodities"), hosts=("oilprice.com",)),
    _pub("Mining.com", "trade", "news", "mining-com", sectors=("materials.metals_mining", "materials.critical_minerals"), hosts=("mining.com",)),
    _pub("The Block", "trade", "news", "the-block", sectors=("crypto.bitcoin", "crypto.ethereum", "crypto.defi"), hosts=("theblock.co",)),
    _pub("CoinDesk", "trade", "news", "coindesk", sectors=("crypto.bitcoin", "crypto.ethereum"), hosts=("coindesk.com",)),
    _pub("Decrypt", "trade", "news", "decrypt", sectors=("crypto.bitcoin", "crypto.ethereum", "crypto.defi"), hosts=("decrypt.co",)),
    _pub("Automotive News", "trade", "news", "automotive-news", sectors=("consumer.autos",), hosts=("autonews.com",)),
    _pub("Electrek", "trade", "news", "electrek", sectors=("consumer.autos", "energy.renewables"), hosts=("electrek.co",)),
    _pub("InsideEVs", "trade", "news", "insideevs", sectors=("consumer.autos",), hosts=("insideevs.com",)),
    _pub("CnEVPost", "trade", "news", "cnevpost", sectors=("consumer.autos",), regions=("china",), hosts=("cnevpost.com",)),
    _pub("FreightWaves", "trade", "news", "freightwaves", sectors=("industrials.transportation",), hosts=("freightwaves.com",)),
    _pub("Game Developer", "trade", "news", "game-developer", sectors=("communication.gaming",), hosts=("gamedeveloper.com",)),
    _pub("China Daily", "tier2", "news", "china-daily", regions=("china",), editorial_stance="state_media", hosts=("chinadaily.com.cn",)),
)

_HOST_MAP: dict[str, Publisher] = {
    host: publisher
    for publisher in PUBLISHERS
    for host in publisher.hosts
}
_NAME_MAP: dict[str, Publisher] = {
    publisher.name.lower(): publisher for publisher in PUBLISHERS
}
_NAME_MAP.update({
    "prnewswire": _NAME_MAP["pr newswire"],
    "globenewswire": _NAME_MAP["globenewswire"],
    "businesswire": _NAME_MAP["business wire"],
    "accesswire": _NAME_MAP["access newswire"],
    "federal-reserve": _NAME_MAP["federal reserve"],
    "bea": _NAME_MAP["bea"],
    "ecb": _NAME_MAP["ecb"],
    "eia": _NAME_MAP["eia"],
    "bank-of-england": _NAME_MAP["bank of england"],
    "rba": _NAME_MAP["rba"],
    "fda": _NAME_MAP["fda"],
    "sec": _NAME_MAP["sec"],
    "ftc": _NAME_MAP["ftc"],
    "doj": _NAME_MAP["doj"],
    "bls": _NAME_MAP["bls"],
})

TIER1_PUBLISHER_NAMES: frozenset[str] = frozenset(
    publisher.name for publisher in PUBLISHERS if publisher.is_tier1_news
)

PR_WIRE_PUBLISHER_NAMES: frozenset[str] = frozenset(
    publisher.name for publisher in PUBLISHERS if publisher.kind == "pr_wire"
)


def normalize_publisher_name(name: str) -> str:
    """Strip the ``-classaction`` lawyer-spam suffix the firehose adds to
    PR-wire items so registry lookups resolve to the underlying wire."""
    return (name or "").strip().split("-classaction", 1)[0].strip()


def is_pr_wire_publisher_name(name: str) -> bool:
    """True for PR Newswire / GlobeNewswire and their -classaction suffixes."""
    base = normalize_publisher_name(name)
    if not base:
        return False
    return publisher_for_name(base).kind == "pr_wire"


def publisher_for_url(url: str) -> Publisher:
    host = urlparse(url).netloc.lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host in _HOST_MAP:
        return _HOST_MAP[host]
    for suffix, publisher in _HOST_MAP.items():
        if host.endswith("." + suffix):
            return publisher
    label = host.split(".")[0].title() if host else "unknown"
    group = host or label.lower()
    return Publisher(
        name=label,
        tier="tier2",
        kind="news",
        independence_group=group,
        hosts=(host,) if host else (),
    )


def publisher_for_name(name: str, url: str | None = None) -> Publisher:
    key = (name or "").strip().lower()
    if key in _NAME_MAP:
        return _NAME_MAP[key]
    if url:
        return publisher_for_url(url)
    group = key.replace(" ", "-") or "unknown"
    return Publisher(
        name=(name or "unknown").strip() or "unknown",
        tier="tier2",
        kind="news",
        independence_group=group,
    )


__all__ = [
    "PUBLISHERS",
    "PR_WIRE_PUBLISHER_NAMES",
    "TIER1_PUBLISHER_NAMES",
    "Publisher",
    "is_pr_wire_publisher_name",
    "normalize_publisher_name",
    "publisher_for_name",
    "publisher_for_url",
]

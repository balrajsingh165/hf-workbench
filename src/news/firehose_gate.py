"""Three-tier ticker-tagging gate for the news firehose, plus the
materiality scorer that downstream surfaces use to keep low-value PR
out of the home feed.

Shared between `scripts/smoke_press_wires.py` (validation) and
`agents/firehose.py` (production). Pure-Python, no LLM.

Tiers:
  T1 — structural exchange-tagged tickers, e.g. "(NASDAQ: ABCD)" / "(NYSE: XYZ)".
       Carries the gate per the May 2026 smoke (~91% of passes).
  T2 — alias index against the `instruments` registry, split case-sensitive
       (ticker-shape) vs case-insensitive (multi-word + ≥6-char names) to
       suppress body-text false positives like "team", "arm", "f", "de".
  T3 — macro keyword list (Fed, CPI, payrolls, ...).

A row passes the gate if any tier produces at least one tag.

Materiality (`score_materiality`) runs *after* the gate. The gate decides
"is anything tradeable in here", materiality decides "is this worth
showing on /api/home". Stored as `news.materiality_score`; the home
endpoint filters at a threshold.
"""
from __future__ import annotations

import html
import re

from src.instruments import resolver
from src.news.publishers import (
    is_pr_wire_publisher_name,
    normalize_publisher_name,
    publisher_for_name,
)

MACRO_KEYWORDS: tuple[str, ...] = (
    # US Fed + macro
    "fed ", "fomc", "federal reserve", "federal open market committee",
    "rate cut", "rate hike",
    "interest rate", "monetary policy", "discount rate", "balance sheet",
    "cpi", "ppi", "inflation",
    "payrolls", "nonfarm", "jobs report", "jobless claims", "unemployment",
    "gdp", "recession", "pce inflation", "personal income", "personal outlays",
    "treasury yield", "10-year yield", "treasuries", "bond yield",
    "us dollar", "dollar index",
    # Foreign central banks
    "ecb", "european central bank", "governing council",
    "bank of england", "boe ", "bank of japan", "boj ",
    "people's bank of china", "pboc",
    # Commodities & energy
    "oil prices", "crude oil", "wti crude", "brent crude",
    "gold prices", "spot gold", "natural gas prices",
    "petroleum", "energy outlook", "opec", "production cut",
    "energy information administration", "eia ",
    # Trade & geopolitics
    "tariff", "section 301", "countervailing duty", "trade deficit",
    "sanctions", "ofac", "entity list", "export controls",
    "executive order", "presidential memorandum",
    # Tier A regulatory — agency-action keywords. These open the T3 gate
    # for FDA/SEC/FTC/DOJ press items that don't name a public issuer in
    # the headline but are still market-moving (rule changes, new FDA
    # guidance, sector-wide enforcement). Phrasing here mirrors the
    # `regulatory` HIGH_VALUE patterns; keep them in lockstep.
    "fda approves", "fda clears", "fda authorizes", "fda grants",
    "fda accepts", "fda rejects", "fda warning letter",
    "complete response letter", "advisory committee meeting",
    "sec charges", "sec settles", "sec fines", "sec sues", "sec bars",
    "sec adopts", "sec proposes", "ftc sues", "ftc bans", "ftc blocks",
    "ftc challenges", "ftc orders", "ftc takes action",
    "justice department", "doj indicts", "doj charges", "doj sues",
    "antitrust suit", "antitrust complaint", "consent decree",
    "deferred prosecution", "fcpa",
    "class i recall", "medwatch", "safety communication",
    # Tier A BLS prints — canonical headline phrasings from the Bureau
    # of Labor Statistics RSS (Empsit/CPI/PPI/JOLTS). Each release uses
    # a near-deterministic title pattern: "Payroll employment increases
    # by 178,000 in March; unemployment rate ...", "CPI for all items
    # rises 0.9% in March", "PPI for final demand advances 0.5%", "Job
    # openings and total separations change little".
    "payroll employment", "cpi for all items", "ppi for final demand",
    "job openings", "total separations", "consumer price index",
    "producer price index",
)

STOPWORDS = frozenset(
    "de la le les el los et and or the of in on a an as is at to for by from "
    "und der die das los las".split()
)

# Aliases blocked from the T2 ci_index regardless of seed contents. Bare
# exchange labels live inside `(NASDAQ: TICKER)` markup on every press
# release, so indexing them as ^IXIC/^GSPC/^DJI causes every issuer-tagged
# story to also tag its host index. T1 already handles the structural form;
# the precise multi-word forms ("Nasdaq Composite", "S&P 500") still pass.
ALIAS_DENYLIST_CI = frozenset(
    {"nasdaq", "nyse", "amex", "otcqb", "otcqx", "tsx", "tsxv", "lse",
     "asx", "hkex", "jse",
     # Ambiguous single-word issuer short-names that double as common
     # surnames in body text. The full multi-word alias (e.g.
     # "Parsons Corporation") still indexes; the bare surname does not.
     #   parsons — US Attorney "Ron Parsons" appears in DOJ bodies and
     #             would otherwise tag every DOJ release with PSN.
     "parsons"}
)

# Bare-ticker literals that double as common acronyms in non-finance
# contexts. Removed from the cs_index so they don't false-positive on
# body text — the structural exchange detector (T1) still catches them
# when they appear in legitimate "(NYSE: PSN)" form.
#
#   PSN — Parsons Corporation. DOJ press releases include the boilerplate
#         "Project Safe Neighborhoods (PSN)", which would otherwise tag
#         every drug/firearm sentencing item with PSN.
SYMBOL_DENYLIST_CS = frozenset({"PSN"})

PROXY_ASSET_CLASSES = frozenset({"commodity", "fx", "rate"})

# Commodity proxy tagging. When a commodity word appears near a price /
# move keyword in the headline+body, also tag these proxy ETF + futures
# symbols so commodity-themed theses (gold via GLD/GDX, oil via USO/XLE)
# get ticker overlap on price-move stories that carry no issuer ticker.
# Word boundaries avoid the "Goldman"/"Goldsmith" false positives that a
# single-word substring match would create; the price-context modifier
# avoids tagging award/sponsorship headlines that happen to say "gold".
COMMODITY_PROXY_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\bgold\b.{0,40}\b(?:price|prices|spot|surges?|slips?|slipped|falls?|fell|rises?|rose|drops?|dropped|jumps?|jumped|steady|tumbles?|hits?|climbs?|climbed|hovers?|holds?|held|above|below|ounce|bullion)\b", re.I), ("GC=F", "GLD", "GDX")),
    (re.compile(r"\b(?:crude\s+oil|oil\s+prices?|wti\s+crude|wti\b)\b", re.I), ("CL=F", "USO", "XLE")),
    (re.compile(r"\bbrent\s+(?:crude|oil)\b", re.I), ("BZ=F",)),
    (re.compile(r"\b(?:natural\s+gas|natgas)\b", re.I), ("NG=F", "UNG")),
)

# Whitelisted exchanges; ticker token is conservative (1-6 alnum + . - /).
EXCHANGE_TICKER = re.compile(
    r"\b(?:NASDAQ|Nasdaq|NYSE|AMEX|NYSE\s*American|OTCQB|OTCQX|"
    r"TSX|TSXV|TSX-V|CSE|LSE|FWB|HKEX|ASX|JSE|FRA|EPA|BVMF|BME|SIX)"
    r"\s*[:\-]\s*([A-Z][A-Z0-9\.\-]{0,6})",
)

# Class-action / lead-plaintiff press releases: pass the gate (because they
# name a ticker) but drown the strip if shown to users. Caller marks them
# with a distinct publisher so the UI can hide by default while leaving them
# queryable.
LAWYER_SPAM = re.compile(
    r"\b(class action|securities fraud|lead plaintiff|investors? have opportunity|"
    r"investors who lost|deadline:|encourages .* investors?|rosen,|robbins geller|"
    r"glancy prongay|bragar eagel|schall law)\b",
    re.IGNORECASE,
)


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def build_alias_index() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Returns (ci_index, cs_index).

    ci_index — case-INSENSITIVE word-boundary matches. Multi-word aliases
        AND single-word aliases of length ≥ 6 (rare/distinctive enough to be
        safe — "Atlassian", "Microsoft", "Boeing").
    cs_index — case-SENSITIVE matches against the original text. Holds
        literal ticker symbols and ticker-shape uppercase tokens. Suppresses
        common-word collisions like "team"/"arm"/"f"/"de".
    """
    cs_index: dict[str, set[str]] = {}
    ci_index: dict[str, set[str]] = {}
    for inst in resolver.all_active():
        candidates: list[str] = [inst.symbol]
        candidates.extend(inst.aliases)
        if inst.short:
            candidates.append(inst.short)
        for raw in candidates:
            tok = raw.strip()
            if not tok or tok.lower() in STOPWORDS:
                continue
            # Single-word aliases for commodity/fx/rate proxies collide with
            # everyday English ("Gold", "Natural Gas"). Drop them.
            if (
                inst.asset_class in PROXY_ASSET_CLASSES
                and " " not in tok
                and tok != inst.symbol
            ):
                continue
            multiword = " " in tok
            is_symbol = tok == inst.symbol
            ticker_shape = (
                not multiword
                and tok.isupper()
                and any(c.isalpha() for c in tok)
                and 2 <= len(tok) <= 6
            )
            # 1-char tickers (F, V, T, X) are too dangerous in body text —
            # the structural exchange detector still catches them.
            if not multiword and len(tok) < 2:
                continue
            if is_symbol or ticker_shape:
                if tok in SYMBOL_DENYLIST_CS:
                    continue
                cs_index.setdefault(tok, set()).add(inst.symbol)
                continue
            if multiword:
                key = tok.lower()
                if key in ALIAS_DENYLIST_CI:
                    continue
                ci_index.setdefault(key, set()).add(inst.symbol)
                continue
            if len(tok) >= 6:
                key = tok.lower()
                if key in ALIAS_DENYLIST_CI:
                    continue
                ci_index.setdefault(key, set()).add(inst.symbol)
    return ci_index, cs_index


def tag_text(
    title: str,
    body: str,
    ci_index: dict[str, set[str]],
    cs_index: dict[str, set[str]],
) -> tuple[set[str], set[str], set[str]]:
    """Returns (exchange_tickers, registry_symbols, macro_keywords).

    Exchange tickers are the verbatim symbols extracted from "(EXCHANGE: TICKER)"
    and may not be present in the `instruments` registry (Phase 3 reconciles).
    Registry symbols are canonical Yahoo-style symbols.
    """
    full = f"{title}  ||  {body[:400]}"
    text_lower = full.lower()
    exchange_tickers = {m.group(1) for m in EXCHANGE_TICKER.finditer(full)}
    symbols: set[str] = set()
    for alias, syms in ci_index.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text_lower):
            symbols |= syms
    for alias, syms in cs_index.items():
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", full):
            symbols |= syms
    macros = {kw.strip() for kw in MACRO_KEYWORDS if kw in text_lower}
    for pattern, proxies in COMMODITY_PROXY_PATTERNS:
        if pattern.search(full):
            symbols.update(proxies)
    return exchange_tickers, symbols, macros


# ── Materiality scorer (post-gate) ────────────────────────────────────
#
# Press-wire firehose passes the gate on any (EXCHANGE: TICKER) tag, so it
# admits a long tail of corporate-housekeeping PR (board appointments,
# "to present at conference", routine debt rollovers, retail product
# announcements, listicles using a marketplace name). Materiality scores
# each item 0–100 based on event-class keyword evidence; the home feed
# filters at MATERIALITY_HOME_THRESHOLD.
#
# Score is clamped to [0, 100]. Higher = more likely to move the stock or
# inform a thesis. Order matters: NOISE_PATTERNS short-circuit to 0
# *unless* a HIGH_VALUE pattern is also present (a "Q3 earnings + board
# appointment" combo is still earnings).

MATERIALITY_HOME_THRESHOLD = 25

# Press-wire M&A/earnings regexes can score 50–100 on issuer PR alone.
# Cap per-item and per-cluster (when uncorroborated) so they do not
# crowd the route_news_clusters candidate window.
PR_WIRE_MATERIALITY_CAP = 45

# Tier-1 wires (Bloomberg, CNBC, …) often publish macro/rates headlines whose
# body is a stub; bump to R1 threshold when the headline is clearly macro.
TIER1_MACRO_MATERIALITY_FLOOR = 30

TIER1_MACRO_HEADLINE_RE = re.compile(
    r"\b(?:"
    r"bond\s+yields?|treasury\s+yields?|sovereign\s+yields?|"
    r"(?:10|30|2)[-\s]year\s+(?:yields?|treasur(?:y|ies))|"
    r"(?:fed|federal\s+reserve|fomc|ecb|bank\s+of\s+england|boj|"
    r"inflation|cpi|ppi|monetary\s+policy)|"
    r"rate\s+(?:cut|hike|decision|expectations?)|"
    r"us\s+(?:rates?|yields?)|"
    r"gdp|payrolls|nonfarm"
    r")\b",
    re.I,
)

# (regex, weight, label). Headline + first 600 chars of body. Weights are
# additive but capped at 100. Patterns that look weak in isolation
# (e.g. "buyback") are scored low so a single match doesn't push junk
# through.
# Earnings discriminator: "Reports/Posts Q1 results" is real, "Announces Q1
# Earnings Call/Release Date" is scheduling. The earnings HIGH_VALUE
# patterns below use post-print verbs (`reports`, `posts`) or require
# substantive result keywords (`Financial and Operating Results`). The
# `EARNINGS_SCHEDULE_VETO` regex below catches the scheduling phrasings;
# when matched, all earnings hits are stripped from the score and
# `earnings_schedule` is logged as a noise label.
EARNINGS_SCHEDULE_VETO = re.compile(
    r"\b(?:to (?:report|release|announce|host|discuss)|"
    r"will (?:report|release|host|discuss|announce)|"
    r"schedules?|sets? date|"
    r"earnings (?:call|release|announcement)\s+(?:date|and|on|scheduled)|"
    r"earnings release and (?:conference )?call|"
    r"announces?(?:\s+\w+){0,5}\s+(?:earnings|conference|investor)\s+(?:call|release|webcast)|"
    r"conference call (?:date|on|scheduled|to discuss))\b",
    re.I,
)

HIGH_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    # Post-print earnings verbs only ("Reports/Posts"). "Announces" is too
    # ambiguous — it matches both real Q1 releases and scheduling notices.
    (re.compile(r"\b(?:reports?|posts?)\s+(?:q[1-4]|first|second|third|fourth)\s+quarter\b", re.I), 45, "earnings"),
    (re.compile(r"\b(?:reports?|posts?)\s+(?:fy|fiscal|full[- ]year|annual)\s+\d{4}\b", re.I), 40, "earnings"),
    # Substantive "Announces" form: must include the year + a result keyword
    # ("Announces First Quarter 2026 Financial and Operating Results").
    (re.compile(r"\bannounces?\s+(?:q[1-4]|first|second|third|fourth)\s+quarter\s+\d{4}\s+(?:financial|operating|operational|results)\b", re.I), 40, "earnings"),
    (re.compile(r"\b(?:eps|earnings per share)\s+of\s*\$", re.I), 40, "earnings"),
    (re.compile(r"\brevenue\s+(?:of|grew|rose|fell|declined|up|down)\b", re.I), 35, "earnings"),
    (re.compile(r"\b(?:sees?|reports?|posts?)\s+(?:sales|revenue|net income|net loss|operating income)\s+(?:decline|growth|grew|fell|rose|up|down|of)\b", re.I), 35, "earnings"),
    (re.compile(r"\b(?:beats?|misses?|tops?)\s+(?:estimates?|expectations?|consensus)\b", re.I), 40, "earnings"),
    (re.compile(r"\b(?:to acquire|will acquire|agrees? to acquire|to be acquired|acquires?|acquisition of)\b", re.I), 50, "m_a"),
    (re.compile(r"\b(?:merger|merge with|stock-for-stock|all-cash deal|tender offer|takeover bid|definitive agreement|plan of arrangement)\b", re.I), 45, "m_a"),
    (re.compile(r"\b(?:divest(?:s|iture)?|spins? off|spin-off|carve-out)\b", re.I), 35, "m_a"),
    (re.compile(r"\b(?:raises?|lifts?|narrows?|lowers?|cuts?)\s+(?:full[- ]year|fy|annual|fiscal|q[1-4])?\s*(?:guidance|outlook|forecast)\b", re.I), 45, "guidance"),
    (re.compile(r"\b(?:withdraws?|suspends?|reaffirms?)\s+guidance\b", re.I), 35, "guidance"),
    (re.compile(r"\b(?:fda)\s+(?:approv|reject|grants?|clears?|clearance|accepts?|authoriz|denies?|withdraws?)", re.I), 50, "regulatory"),
    (re.compile(r"\b(?:fda|ema|mhra|pmda)\s+(?:issues?\s+(?:warning letter|complete response letter|crl)|warning letter to)\b", re.I), 55, "regulatory"),
    (re.compile(r"\b(?:phase\s*[123]|topline)\s+(?:trial|results|data|readout)\b", re.I), 40, "regulatory"),
    (re.compile(r"\b(?:doj|department of justice|sec|ftc|antitrust|consent decree)\s+(?:charges?|sues?|files?|investigation|probe)", re.I), 40, "regulatory"),
    # Tier A — agency-action verbs at the start of headline. Gov press
    # releases follow predictable forms: "FDA Approves...", "SEC Charges...",
    # "FTC Takes Action...", "Justice Department Sues...". These score
    # without needing an issuer alias because the action itself is the news.
    (re.compile(r"\bsec\s+(?:charges?|fines?|sanctions?|settles?|sues?|bars?|orders?\s+(?:disgorgement|penalty))\b", re.I), 55, "regulatory"),
    (re.compile(r"\bftc\s+(?:sues?|bans?|blocks?|challenges?|takes?\s+action|orders?|files?\s+(?:complaint|suit)|secures?)\b", re.I), 55, "regulatory"),
    (re.compile(r"\b(?:doj|justice department|department of justice)\s+(?:sues?|indicts?|charges?|files?\s+(?:civil|criminal|antitrust|complaint)|secures?\s+(?:guilty plea|conviction|settlement))\b", re.I), 55, "regulatory"),
    (re.compile(r"\b(?:antitrust\s+(?:suit|complaint|investigation|probe|enforcement)|monopolization (?:claim|case)|merger challenge|consent decree|deferred prosecution agreement|non-prosecution agreement|fcpa)\b", re.I), 55, "regulatory"),
    # Class I (highest-severity) FDA recalls and drug-safety alerts. Most
    # FDA recall feed items are food/produce; the medical-device + drug
    # alerts are the ones that move issuers (Insulet, B. Braun, Trividia).
    (re.compile(r"\bclass\s+i\s+recall\b", re.I), 50, "regulatory"),
    (re.compile(r"\b(?:fda|medwatch)\s+safety\s+(?:alert|communication)\b", re.I), 45, "regulatory"),
    (re.compile(r"\b(?:files? for|prices?|launches?)\s+ipo\b", re.I), 35, "ipo"),
    (re.compile(r"\b(?:secondary offering|direct offering|registered offering)\b.{0,40}(?:million|billion)", re.I), 25, "offering"),
    (re.compile(r"\$\s?\d+(?:\.\d+)?\s*(?:million|billion)\s+(?:contract|order|award|deal|backlog|investment)", re.I), 30, "contract"),
    (re.compile(r"\bawarded\b.{0,40}\$\s?\d+(?:\.\d+)?\s*(?:million|billion)", re.I), 30, "contract"),
    (re.compile(r"\b(?:activist|proxy fight|13d|13d/a|takes?\s+stake|builds?\s+stake)\b", re.I), 35, "activist"),
    (re.compile(r"\b(?:bankruptcy|chapter 11|chapter 7|going concern|delist|delisting|cessation of operations|ceases? operations)\b", re.I), 45, "distress"),
    (re.compile(r"\b(?:recall|investigates?|class\s*i\s+recall|safety alert)\b", re.I), 25, "operational"),
    (re.compile(r"\b(?:stock split|reverse split|share buyback|buyback program|repurchase program)\b.{0,60}(?:million|billion|\$)", re.I), 25, "capital_return"),
    # Tier A — central-bank actions. FOMC statements, rate decisions, and
    # policy speeches drive every asset class. The keyword path (T3) gates
    # them into the firehose; this scoring path puts them at the top.
    (re.compile(r"\b(?:fomc|federal open market committee)\s+(?:statement|decision|minutes|holds?|cuts?|raises?|leaves?)\b", re.I), 55, "fed_action"),
    (re.compile(r"\b(?:fed|federal reserve|ecb|bank of england|bank of japan|pboc)\b.{0,60}\b(?:cuts?|raises?|holds?|hikes?|decision|statement|policy meeting)\b", re.I), 50, "fed_action"),
    (re.compile(r"\b(?:rate (?:cut|hike|decision|hold)|interest rate (?:cut|hike|decision))\b", re.I), 45, "fed_action"),
    # Central-bank governor + policy statement (Lagarde, de Guindos,
    # Powell, Bailey, Ueda, Macklem, Lowe). The headline often leads with
    # the speaker's name rather than the institution.
    (re.compile(r"\b(?:lagarde|de guindos|powell|bailey|ueda|macklem|lowe|bullock)\b.{0,40}\b(?:monetary policy|policy statement|rate decision|press conference)\b", re.I), 45, "fed_action"),
    (re.compile(r"\bmonetary policy (?:statement|decision|summary)\b", re.I), 40, "fed_action"),
    # BOE rate decision phrasing
    (re.compile(r"\bbank rate\s+(?:maintained|held|cut|raised|increased|decreased|at\s+\d)", re.I), 45, "fed_action"),
    # Tier A — macro data prints. Headlines that name a release name +
    # report verb. The CPI/PPI/NFP/GDP/PCE prints carry numbers in the
    # body; we score on the *release* signal rather than parsing numbers.
    (re.compile(r"\b(?:cpi|ppi|jolts|empsit|nonfarm payrolls|jobs report)\b.{0,80}\b(?:reports?|report|released|prints?|rises?|falls?|up|down|of\s+\d|grew|declined)\b", re.I), 50, "macro_print"),
    # GDP/PCE — headline-level release names only. The BEA RSS retains
    # historical state-level re-publications ("Real Personal Income by
    # State, 2019") whose bodies say "GDP grew" generically. Restrict to
    # the canonical quarterly release-verb forms ("Advance Estimate",
    # "Personal Income and Outlays, March 2026") that name the headline
    # series and a recent month/quarter.
    (re.compile(r"\b(?:gdp|pce|personal consumption expenditures)\b.{0,80}\b(?:advance estimate|second estimate|third estimate|preliminary estimate)\b", re.I), 50, "macro_print"),
    (re.compile(r"\bpersonal income and outlays\b", re.I), 45, "macro_print"),
    (re.compile(r"\b(?:cpi|ppi|inflation|unemployment rate|jobless claims|consumer price index|producer price index)\s+(?:rose|fell|grew|declined|up|down|at|of)\b", re.I), 50, "macro_print"),
    (re.compile(r"\b(?:annual energy outlook|petroleum status report|weekly petroleum)\b", re.I), 35, "macro_print"),
    # BLS canonical headline forms. These trigger directly on the BLS
    # RSS title shape ("Payroll employment increases by 178,000 in
    # March", "CPI for all items rises 0.9% in March", "PPI for final
    # demand advances 0.5%", "Job openings ... change little"). Score
    # high because the print itself is the news — first-source numbers
    # before secondary outlets re-wrap them.
    (re.compile(r"\b(?:payroll employment|nonfarm payrolls)\b.{0,80}\b(?:increase|decrease|rise|fall|gain|loss|change|by\s+[\d,]+)\b", re.I), 55, "macro_print"),
    (re.compile(r"\bcpi for all items\b.{0,40}\b(?:rises?|falls?|increases?|decreases?|unchanged|up|down|advances?|declines?)\b", re.I), 55, "macro_print"),
    (re.compile(r"\bppi for final demand\b.{0,40}\b(?:rises?|falls?|increases?|decreases?|unchanged|up|down|advances?|declines?)\b", re.I), 55, "macro_print"),
    (re.compile(r"\bjob openings\b.{0,80}\b(?:increase|decrease|rise|fall|change|little|up|down)\b", re.I), 50, "macro_print"),
    # Tier D — political actions with direct issuer/asset impact.
    (re.compile(r"\b(?:tariff|section 301|countervailing duty)\b.{0,80}\b(?:imposes?|imposed|lifts?|lifted|increases?|raises?|paused?|exemption|on imports)\b", re.I), 45, "tariff_action"),
    (re.compile(r"\b(?:imposes?|adds?|expands?|lifts?|removes?|extends?)\b.{0,40}\bsanctions?\b", re.I), 45, "sanctions"),
    (re.compile(r"\b(?:added to|placed on|removed from)\s+(?:the\s+)?entity list\b", re.I), 45, "sanctions"),
    (re.compile(r"\b(?:executive order|presidential memorandum)\b.{0,80}\b(?:signed|issued|signs?|issues?|directs?|on)\b", re.I), 35, "executive_order"),
    # Sell-side actions: analyst PT/forecast/rating changes. A concrete
    # broker move affects order flow on otherwise quiet news days. Three
    # forms: verb-led ("Goldman raises target"), passive ("target raised
    # to $X"), and rating actions ("upgrades to Buy").
    (re.compile(r"\b(?:raises?|lifts?|cuts?|lowers?|sets?|ups|resets?|revamps?|trims?|boosts?|bumps?)\b.{0,30}\b(?:price\s+target|stock\s+(?:target|forecast)|target|forecast|estimate|outlook|pt)\b", re.I), 30, "analyst_action"),
    (re.compile(r"\b(?:price target|stock\s+forecast|pt|target)\s+(?:raised|lifted|lowered|cut|trimmed|boosted|set|reset|hiked)\b", re.I), 30, "analyst_action"),
    (re.compile(r"\b(?:upgrades?|downgrades?)\b.{0,40}\b(?:to|from)\s+(?:buy|sell|hold|outperform|underperform|overweight|underweight|neutral|market\s+perform|equal\s+weight)\b", re.I), 30, "analyst_action"),
    (re.compile(r"\b(?:initiates?\s+coverage|reiterates?)\b.{0,40}\b(?:buy|sell|hold|outperform|underperform|overweight|underweight)\b", re.I), 25, "analyst_action"),
    # Product/platform launches and AI/industrial deployments. Captures
    # concrete capability rollouts (Emerson industrial AI platform; Rockwell-
    # Actemium AI refrigeration). Verb-led forms cover deployment headlines;
    # the noun-phrase form catches titles that lead with the modified
    # product name ("New X Industrial AI Platform").
    (re.compile(r"\b(?:launches?|unveils?|introduces?|debuts?|deploys?|deploy|rolls?\s+out|rolling\s+out)\b.{0,60}\b(?:platform|robots?|operating system|automation|ai\s+(?:platform|system|model))\b", re.I), 25, "product_launch"),
    (re.compile(r"\b(?:industrial|enterprise|autonomous|cloud|edge|next[-\s]?gen(?:eration)?)\s+ai\s+platform\b", re.I), 25, "product_launch"),
    # AI/autonomous + product context: catches "Deploy AI Refrigeration
    # System" / "Deploy Fully Autonomous AI-Powered Robots" where the
    # AI modifier is separated from the product noun by descriptive words.
    (re.compile(r"\b(?:deploys?|deploy|launches?|rolls?\s+out)\b.{0,30}\b(?:ai|autonomous|ai[-\s]powered)\b.{0,60}\b(?:system|platform|robots?|automation|solution|refrigeration|warehouse|factory|fleet)\b", re.I), 25, "product_launch"),
    # Operational disruptions: production halts, fires, floods, cyberattacks
    # at named facilities. Distinct from class-I-recall regulatory hits —
    # these are unplanned operational events that move single-name producers.
    (re.compile(r"\b(?:halts?|shuts?\s+down|pauses?|suspends?|idles?)\b.{0,40}\b(?:operations?|production|mining|drilling|refining|output|activity)\b", re.I), 35, "operational_disruption"),
    (re.compile(r"\b(?:fire|explosion|flood|earthquake|hurricane|outage|cyberattack|ransomware|breach)\b.{0,80}\b(?:plant|facility|operations?|production|refinery|mine|terminal|pipeline|data\s+center)\b", re.I), 30, "operational_disruption"),
    # Commodity price moves: gold/oil/natgas/silver headlines with
    # directional verbs. These are the daily freshness signal for
    # commodity theses — without them, gold/oil/uranium positions get no
    # routine evidence trail.
    (re.compile(r"\b(?:gold|silver|copper|platinum|palladium|brent|wti|crude\s+oil|natural\s+gas|natgas|uranium|oil)\b.{0,40}\b(?:rises?|falls?|surges?|slumps?|drops?|jumps?|climbs?|tumbles?|gains?|loses?|advances?|retreats?|holds?|steady|hovers?|soars?|plunges?|spikes?|rallies|rally|sinks?|trades?\s+(?:up|down|higher|lower)|pushe?[ds]?\s+(?:up|down|higher|lower)|move[ds]?\s+(?:up|down|higher|lower)|hits?\s+(?:high|low|record))\b", re.I), 25, "commodity_move"),
    (re.compile(r"\b(?:gold|silver|copper|brent|wti|crude\s+oil|natural\s+gas)\b.{0,80}\babove\s+\$\s?\d", re.I), 25, "commodity_move"),
    (re.compile(r"\b(?:gold|silver|copper|brent|wti|crude\s+oil|natural\s+gas)\s+prices?\s+(?:today|fall|rise|jump|drop|surge|slump|gain|hold|steady)\b", re.I), 25, "commodity_move"),
    # Central-bank structural actions: reserve diversification, swap-line
    # expansion, sovereign gold accumulation. These are the de-dollarization
    # and reserve-flow signals that drive multi-week gold/FX theses but
    # don't fit the existing fed_action (rate decisions) bucket.
    (re.compile(r"\b(?:central banks?|pboc|boj|ecb|federal reserve|reserve bank)\s+(?:tap|taps|tapped|expand|expands?|raise|raises?|reduce|reduces?|increases?|adds?|withdraws?|drains?|accumulate|accumulates?)\b", re.I), 25, "central_bank_action"),
    (re.compile(r"\b(?:de[-\s]?dollarization|currency\s+swap\s+line|swap\s+line|reserve\s+accumulation|sovereign\s+gold|reserve\s+(?:diversification|reallocation))\b", re.I), 25, "central_bank_action"),
    # Geopolitical/Summit Events: state visits, bilateral summits, trade
    # negotiations. Trump-Xi, G7/G20 outcomes, major diplomatic events.
    (re.compile(r"\b(?:summit|state visit|g7|g20|apec|asean)\b.{0,80}\b(?:concludes?|wraps?|signed|reached|agreement|deal|ends?)\b", re.I), 40, "geopolitical_summit"),
    (re.compile(r"\b(?:trade deal|trade agreement|trade pact|free trade)\b.{0,60}\b(?:signed|reached|announced|collapses?|fails?|stalls?)\b", re.I), 45, "geopolitical_summit"),
    (re.compile(r"\b(?:president|prime minister|chancellor|premier)\b.{0,40}\b(?:meets?|visits?|holds?\s+talks)\b.{0,60}\b(?:trade|tariff|sanctions?|cooperation|alliance)\b", re.I), 35, "geopolitical_summit"),
    (re.compile(r"\b(?:diplomatic\s+(?:breakthrough|crisis|tension)|(?:diplomatic|bilateral|trade|us[-\s]china|sino[-\s]us|us[-\s]russia)\s+relations?\s+(?:deteriorate|improve|normalize|sour|warm|thaw|collapse)|embassy\s+(?:closure|reopening))\b", re.I), 35, "geopolitical_summit"),
    # Geopolitical-energy nexus: state-actor headlines linked to oil/gas/
    # nuclear/sanctions/tankers/pipelines. The "summit" patterns above
    # require diplomatic phrasing; this catches the shock side — strikes,
    # threats, attacks, ultimatums that move CL=F/USO/XLE directly even
    # when the headline never says "summit" or "trade deal".
    (re.compile(r"\b(?:iran|russia|israel|ukraine|north\s+korea|venezuela|opec|saudi|houthi|hezbollah)\b.{0,80}\b(?:oil|crude|gas|energy|tanker|pipeline|refinery|nuclear|sanctions?|strike|threat|attack|targets?|ultimatum)\b", re.I), 35, "geopolitical_energy"),
    (re.compile(r"\b(?:oil|crude|gas|energy)\s+prices?\b.{0,80}\b(?:iran|russia|israel|ukraine|opec|saudi|houthi|geopolitical|tension|conflict|war|sanctions?)\b", re.I), 35, "geopolitical_energy"),
    # Bond-yield shocks: sovereign yields surging/spiking on inflation,
    # fiscal-deficit, geopolitical risk. The macro freshness signal for
    # rates/TLT/PFIX/TMV theses — currently invisible to the scorer
    # because no commodity/macro pattern matches "10-year yield" phrasing.
    (re.compile(r"\b(?:bond|treasury|sovereign|jgb|gilt|bund)\s+yields?\b.{0,60}\b(?:surge|spike|jump|soar|plunge|tumble|rise|fall|climb|drop|gain|gains|edge|flirt|hit\s+(?:high|low|record|multi[-\s]year)|reset|unhinged)\b", re.I), 35, "yield_shock"),
    (re.compile(r"\b(?:10[-\s]year|30[-\s]year|2[-\s]year|long[-\s]term)\s+(?:yields?|bonds?|treasur(?:y|ies))\b.{0,60}\b(?:surge|spike|jump|soar|plunge|tumble|rise|fall|gain|gains|flirt|hit\s+(?:high|low|record|multi[-\s]year))\b", re.I), 35, "yield_shock"),
    (re.compile(r"\b(?:us|u\.s\.)\s+(?:yields?|treasur(?:y|ies))\b.{0,50}\b(?:gain|gains|rise|fall|flirt|surge|drop|climb)\b", re.I), 35, "yield_shock"),
    (re.compile(r"\bglobal\s+bond\s+(?:selloff|sell[-\s]off|rout|crash|tantrum)\b", re.I), 40, "yield_shock"),
    # Supply Chain Disruptions: port strikes, chip shortages, export bans,
    # shipping disruptions. Material impact on production and pricing.
    (re.compile(r"\b(?:port|dock|longshoremen|maritime|shipping)\b.{0,60}\b(?:strike|walkout|work stoppage|shuts?\s+down|closes?|disruption|congestion)\b", re.I), 40, "supply_chain_disruption"),
    (re.compile(r"\b(?:semiconductor|chip|wafer)\b.{0,60}\b(?:shortage|supply\s+(?:constraint|crunch)|allocation|rationing)\b", re.I), 35, "supply_chain_disruption"),
    (re.compile(r"\b(?:export\s+(?:bans?|restrictions?|controls?)|import\s+(?:bans?|restrictions?))\b.{0,60}\b(?:rare\s+earths?|semiconductors?|chips?|wafers?|critical\s+(?:minerals?|materials?)|lithium|cobalt|gallium|germanium|graphite)\b", re.I), 45, "supply_chain_disruption"),
    # Reverse order ("rare earths ... export controls") + noun-phrase form
    # ("rare-earth controls", "semiconductor export curbs"). Catches body
    # paragraphs where the commodity is named first, and headlines that
    # compress the action into a hyphenated modifier.
    (re.compile(r"\b(?:rare\s+earths?|rare[-\s]earth|semiconductors?|chips?|critical\s+(?:minerals?|materials?)|lithium|cobalt|gallium|germanium|graphite)\b.{0,80}\b(?:export\s+(?:bans?|restrictions?|controls?|curbs?)|import\s+(?:bans?|restrictions?))\b", re.I), 45, "supply_chain_disruption"),
    (re.compile(r"\brare[-\s]earths?\s+(?:export\s+)?(?:controls?|curbs?|restrictions?|bans?|sanctions?)\b", re.I), 45, "supply_chain_disruption"),
    (re.compile(r"\b(?:semiconductor|chip|wafer|lithium|cobalt|gallium|germanium|graphite|critical[-\s]mineral)\s+export\s+(?:controls?|curbs?|restrictions?|bans?|sanctions?)\b", re.I), 45, "supply_chain_disruption"),
    (re.compile(r"\b(?:suez|panama\s+canal|strait\s+of\s+hormuz|malacca)\b.{0,60}\b(?:blocked|closure|disruption|suspended|restricted)\b", re.I), 50, "supply_chain_disruption"),
    (re.compile(r"\b(?:logistics|freight|container|cargo)\b.{0,60}\b(?:crisis|shortage|disruption|delays?|backlog)\b", re.I), 30, "supply_chain_disruption"),
    # Energy/Infrastructure Events: pipeline decisions, refinery capacity,
    # SPR releases, major outages. Direct commodity price drivers.
    (re.compile(r"\b(?:keystone(?:\s+xl)?|nord\s+stream|dakota\s+access|trans[-\s]?mountain|colonial\s+pipeline|(?:oil|gas|natural\s+gas|crude|petroleum|hydrogen|lng)\s+pipeline)\b.{0,60}\b(?:approved|rejected|cancelled|suspended|blocked|operational|online|shutdown|sabotaged|ruptured|leak)\b", re.I), 40, "energy_infrastructure"),
    (re.compile(r"\b(?:refinery|refineries)\b.{0,60}\b(?:shutdown|outage|fire|explosion|capacity|offline|restart|maintenance)\b", re.I), 35, "energy_infrastructure"),
    (re.compile(r"\b(?:strategic\s+petroleum\s+reserve|spr)\b.{0,60}\b(?:release|tap|draw|replenish|purchase)\b", re.I), 45, "energy_infrastructure"),
    (re.compile(r"\b(?:power\s+(?:outage|grid)|blackout|brownout|grid\s+(?:failure|collapse))\b.{0,60}\b(?:affects?|impacts?|millions?|widespread|major)\b", re.I), 35, "energy_infrastructure"),
    (re.compile(r"\b(?:lng|liquefied\s+natural\s+gas)\b.{0,60}\b(?:terminal|facility|export|capacity)\b.{0,60}\b(?:approved|online|operational|delayed|cancelled)\b", re.I), 35, "energy_infrastructure"),
    # Credit Events: rating changes, defaults, covenant breaches. Direct
    # impact on borrowing costs and equity valuations.
    (re.compile(r"\b(?:s&p|moody'?s|fitch|dbrs)\b.{0,60}\b(?:upgrades?|downgrades?|affirms?|cuts?|raises?|lowers?)\b.{0,60}\b(?:rating|outlook|credit)\b", re.I), 40, "credit_event"),
    (re.compile(r"\b(?:credit\s+rating|debt\s+rating)\b.{0,60}\b(?:upgraded|downgraded|cut|raised|lowered|affirmed)\b", re.I), 40, "credit_event"),
    (re.compile(r"\b(?:debt\s+default|payment\s+default|missed\s+(?:bond|coupon|interest|principal)\s+payment|covenant\s+breach|technical\s+default|defaults?\s+on\s+(?:\S+\s+){0,3}(?:debt|bonds?|notes?|loans?|payments?|obligations?|debentures?))\b", re.I), 50, "credit_event"),
    (re.compile(r"\b(?:cut|reduced|lowered|raised|upgraded|downgraded|falls?|drops?|loses?|slips?)\s+to\s+(?:junk(?:\s+status)?|speculative\s+grade|investment\s+grade)\b", re.I), 35, "credit_event"),
    # Capacity/Production Changes: factory openings/closings, capacity
    # expansions, manufacturing relocations. Supply-side structural shifts.
    (re.compile(r"\b(?:opens?|opening|inaugurates?)\b.{0,60}\b(?:factory|plant|facility|manufacturing|production)\b.{0,60}\b(?:billion|million|\$\s?\d)", re.I), 35, "capacity_change"),
    (re.compile(r"\b(?:closes?|closing|shuts?\s+down|shuttering)\b.{0,60}\b(?:factory|plant|manufacturing|production|assembly\s+(?:line|plant)|(?:chemical|semiconductor|fab|foundry|smelter|refinery|mill)\s+facility)\b", re.I), 40, "capacity_change"),
    (re.compile(r"\b(?:expands?|expansion|increases?)\b.{0,60}\b(?:production\s+capacity|manufacturing\s+capacity|output|capacity)\b.{0,60}\b(?:by\s+\d+%|billion|million)", re.I), 30, "capacity_change"),
    (re.compile(r"\b(?:reshoring|onshoring|nearshoring|offshoring|relocat(?:es?|ing))\b.{0,60}\b(?:production|manufacturing|operations?|facility|plant)\b", re.I), 35, "capacity_change"),
    (re.compile(r"\b(?:groundbreaking|breaks\s+ground)\b.{0,60}\b(?:factory|plant|facility|manufacturing|fab|foundry)\b", re.I), 30, "capacity_change"),
    # Commodity Supply Shocks: OPEC decisions, crop failures, mining
    # disruptions, strategic reserve actions. Direct price drivers.
    (re.compile(r"\b(?:opec|opec\+|organization\s+of\s+petroleum)\b.{0,60}\b(?:cuts?|increases?|raises?|lowers?|maintains?|agrees?|decision|output|production|quota)\b", re.I), 50, "commodity_supply_shock"),
    (re.compile(r"\b(?:production\s+cut|output\s+cut|supply\s+cut)\b.{0,60}\b(?:opec|oil|crude|barrel)", re.I), 45, "commodity_supply_shock"),
    (re.compile(r"\b(?:crop\s+(?:failure|damage|loss)|harvest|drought|flood|freeze)\b.{0,60}\b(?:corn|wheat|soybean|coffee|cocoa|sugar|cotton|rice)\b", re.I), 40, "commodity_supply_shock"),
    (re.compile(r"\b(?:mine|mining)\b.{0,60}\b(?:closure|shutdown|strike|accident|collapse|suspended|halted)\b.{0,60}\b(?:copper|gold|silver|lithium|cobalt|nickel|zinc|iron\s+ore)\b", re.I), 40, "commodity_supply_shock"),
    (re.compile(r"\b(?:usda|department\s+of\s+agriculture)\b.{0,60}\b(?:crop\s+report|production\s+estimate|harvest|yield)\b", re.I), 35, "commodity_supply_shock"),
    (re.compile(r"\b(?:reserves?|stockpile)\b.{0,60}\b(?:release|draw|tap|depleted|replenish)\b.{0,60}\b(?:oil|petroleum|grain|wheat|corn)\b", re.I), 35, "commodity_supply_shock"),
)

NOISE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bto\s+(?:present|attend|participate|host|speak)\s+at\b.{0,80}\b(?:conference|summit|symposium|forum|day|investor (?:day|event))\b", re.I), "conference"),
    (re.compile(r"\bwill\s+(?:present|attend|participate)\s+at\b.{0,80}\bconference\b", re.I), "conference"),
    (re.compile(r"\bappoints?\b.{0,80}\bto\b.{0,15}\bboard\b", re.I), "board_change"),
    (re.compile(r"\b(?:appoints?|names?)\s+(?:new\s+)?(?:chief|president|cfo|ceo|cto|coo|chairman|chairperson|chairwoman|director|head of)\b", re.I), "officer_change"),
    (re.compile(r"\b(?:declares?|announces?)\s+(?:quarterly\s+)?(?:cash\s+)?dividend\b", re.I), "dividend_routine"),
    (re.compile(r"\b(?:announces?|announced)\s+pricing of\b.{0,60}\b(?:notes?|debentures?|bonds?)\b", re.I), "debt_routine"),
    (re.compile(r"\bprices?\b.{0,40}\bsenior\s+(?:secured\s+|unsecured\s+)?notes?\b", re.I), "debt_routine"),
    (re.compile(r"\btop\s+\d+\s+amazon\b", re.I), "listicle"),
    (re.compile(r"\b(?:amazon|kindle)\s+(?:bestseller|best[- ]selling|kdp|kindle store)\b", re.I), "listicle"),
    # book_promo: parenthesized alternation (the old form's first alt was
    # bare `\bnovel`, leaving the rest unanchored — every biotech body that
    # said "novel therapeutic" tripped it). Also requires a book-context
    # anchor in the same window so generic "novel" usage doesn't fire.
    (re.compile(r"\b(?:novel|fiction|memoir|paperback|audiobook)\b.{0,80}\b(?:available|launches?|debuts?|publishes?)\b(?=.{0,400}\b(?:author|publisher|book|kindle|amazon|goodreads|isbn|hardcover|chapters?)\b)", re.I), "book_promo"),
    (re.compile(r"\b(?:wins?|named|recognized as|honored with|receives?)\b.{0,80}\b(?:award|honor|recognition|partner of the (?:year|month|quarter)|best of|top \d+)\b", re.I), "award"),
    (re.compile(r"\bis now available (?:on|at|in|for)\b", re.I), "product_routine"),
    (re.compile(r"\b(?:announces?|launches?)\s+(?:new\s+)?(?:rebate|coupon|promotion|loyalty|sweepstakes|gift card)\b", re.I), "promo"),
    (re.compile(r"\b(?:supports?|sponsors?|partners with|teams up with)\b.{0,80}\b(?:charity|nonprofit|foundation|community|gala|fundraiser)\b", re.I), "charity"),
    (re.compile(r"\b(?:hosts?|holds?)\s+(?:annual\s+)?(?:meeting of (?:the )?(?:share|stock)holders|annual meeting)\b", re.I), "agm_routine"),
    (re.compile(r"\bnotice of (?:annual|special)\s+meeting\b", re.I), "agm_routine"),
    (re.compile(r"\bfiles?\s+(?:form\s+)?(?:10-q|10-k|8-k|s-1|proxy statement)\b", re.I), "filing_routine"),
    # Regulatory admin: DOJ FOIA training reminders, "FY26 Q4 Data Due",
    # advisory-committee scheduling, agency podcast launches, chairman
    # tenure announcements. Common in DOJ justice-news + SEC press feeds.
    (re.compile(r"^(?:fy\s*\d{2,4}\s*q\d|continuing\s+foia|foia\s+(?:education|compliance|training)|administrative\s+appeals|exemption\s+\d|privacy\s+considerations\s+training|procedural\s+requirements)\b", re.I), "regulatory_admin"),
    (re.compile(r"\b(?:advisory committee|small business advisory|investor advisory)\s+to\s+(?:meet|explore|discuss|consider)\b", re.I), "regulatory_admin"),
    (re.compile(r"\bto\s+(?:host|hold)\s+(?:a\s+)?(?:workshop|fireside chat|public meeting|roundtable)\b", re.I), "regulatory_admin"),
    (re.compile(r"\b(?:launches?|introduces?)\s+(?:new\s+)?(?:podcast|newsletter|blog\s+series)\b", re.I), "regulatory_admin"),
    (re.compile(r"\b(?:to\s+)?conclude\s+(?:his|her|their)\s+tenure\b", re.I), "regulatory_admin"),
    (re.compile(r"\bseeks?\s+public\s+comment\s+on\b", re.I), "regulatory_admin"),
    # Individual federal criminal cases. DOJ justice-news is dominated by
    # drug/immigration/CSAM/local-fraud sentencings of named individuals
    # (no corporate referent). Pattern matches the "City Man Sentenced /
    # Pleads Guilty / Charged" headline form. Corporate enforcement uses
    # different phrasing ("Company X Agrees to Pay", "Settles With", etc.)
    # so this is safe to short-circuit.
    (re.compile(r"\b(?:man|woman|national|alien|resident)\b.{0,80}\b(?:sentenced|pleads?\s+guilty|convicted|charged|arrested|indicted)\b", re.I), "individual_crime"),
    (re.compile(r"\b(?:previously\s+deported|illegal\s+alien|former\s+(?:university\s+)?(?:professor|student|teacher|coach))\b.{0,80}\b(?:sentenced|charged|convicted|guilty)\b", re.I), "individual_crime"),
    (re.compile(r"\bsentenced\s+to\s+\d+\s+(?:months?|years?)\s+(?:in|for)\b", re.I), "individual_crime"),
    # Crypto-promo: presale token launches and "price prediction" listicles
    # are syndicated PR/affiliate content, not market-moving news for the
    # named coins. Hits both the unlicensed-token presale headlines
    # (Pepeto-style) and the "BTC could reach $X" speculation pieces.
    (re.compile(r"\b(?:presale|pre-sale)\b.{0,80}\b(?:announces?|raises?|raised|launches?|update|advancement)\b", re.I), "crypto_promo"),
    (re.compile(r"\bprice prediction\b", re.I), "crypto_promo"),
    (re.compile(r"\b(?:might|could|may|expected to|set to|projected to)\s+(?:reach|hit|surge to|rally to|cross|surpass)\s+\$\s?\d", re.I), "crypto_promo"),
    (re.compile(r"^crypto news:\s", re.I), "crypto_promo"),
)


def _apply_publisher_materiality_adjustments(
    score: int,
    labels: list[str],
    *,
    title: str,
    publisher: str | None,
) -> tuple[int, list[str]]:
    base_name = normalize_publisher_name(publisher or "")
    if not base_name:
        return score, labels

    if is_pr_wire_publisher_name(base_name):
        return min(score, PR_WIRE_MATERIALITY_CAP), labels

    pub = publisher_for_name(base_name)
    if (
        pub.is_tier1_news
        and TIER1_MACRO_HEADLINE_RE.search(title or "")
        and score < TIER1_MACRO_MATERIALITY_FLOOR
    ):
        return TIER1_MACRO_MATERIALITY_FLOOR, sorted(
            {*labels, "tier1_macro_commentary"}
        )
    return score, labels


def score_materiality(
    title: str,
    body: str,
    *,
    publisher: str | None = None,
) -> tuple[int, list[str]]:
    """Returns (score 0-100, list of matched class labels).

    Title is weighted heavier than body — the headline is the strongest
    signal of editorial intent. Body scan is bounded so the function is
    O(1) per item.

    Optional ``publisher`` applies PR-wire caps and tier-1 macro headline floors.
    """
    head = title or ""
    excerpt = (body or "")[:600]
    full = f"{head}  ||  {excerpt}"

    # Lawyer-spam class actions (already publisher-suffixed at the gate)
    # are pure noise on the home feed. Short-circuit for cleaner labels:
    # a zero materiality score lets surfaces filter on the column directly.
    if LAWYER_SPAM.search(head):
        return 0, ["lawyer_spam"]

    high_hits: list[tuple[int, str]] = []
    for pat, weight, label in HIGH_VALUE_PATTERNS:
        if pat.search(full):
            bonus = 5 if pat.search(head) else 0
            high_hits.append((weight + bonus, label))

    noise_hits: list[str] = []
    for pat, label in NOISE_PATTERNS:
        if pat.search(head) or pat.search(excerpt):
            noise_hits.append(label)

    # "Save the date" earnings-scheduling notices. The earnings HIGH_VALUE
    # patterns above can't perfectly avoid these without false negatives on
    # legit "Reports Q1 Results" headlines, so the veto runs after scoring
    # and strips earnings hits when scheduling phrasing is present. The
    # label is always attached when the veto matches so the column reads
    # consistently whether or not a high-value pattern also fired.
    if EARNINGS_SCHEDULE_VETO.search(head):
        high_hits = [(w, l) for w, l in high_hits if l != "earnings"]
        noise_hits.append("earnings_schedule")

    high_score = sum(w for w, _ in high_hits)
    labels = sorted({l for _, l in high_hits} | set(noise_hits))

    if high_hits:
        # Real event present. Cap and return; noise tags coexist (a board
        # appointment buried in an earnings release shouldn't kill the score).
        score = min(100, high_score)
        return _apply_publisher_materiality_adjustments(
            score, labels, title=head, publisher=publisher
        )

    if noise_hits:
        # Only noise patterns matched. Hard 0 — these are the items the
        # home feed is meant to suppress.
        return _apply_publisher_materiality_adjustments(
            0, labels, title=head, publisher=publisher
        )

    # No pattern hit at all. Default 15 — passable on the home feed but
    # ranks below anything with real evidence. Rare in practice; press
    # wires almost always trip something.
    return _apply_publisher_materiality_adjustments(
        15, [], title=head, publisher=publisher
    )


__all__ = [
    "ALIAS_DENYLIST_CI",
    "EARNINGS_SCHEDULE_VETO",
    "EXCHANGE_TICKER",
    "HIGH_VALUE_PATTERNS",
    "LAWYER_SPAM",
    "MACRO_KEYWORDS",
    "MATERIALITY_HOME_THRESHOLD",
    "NOISE_PATTERNS",
    "PR_WIRE_MATERIALITY_CAP",
    "TIER1_MACRO_MATERIALITY_FLOOR",
    "SYMBOL_DENYLIST_CS",
    "build_alias_index",
    "score_materiality",
    "strip_html",
    "tag_text",
]

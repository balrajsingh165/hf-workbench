"""Closed taxonomy of theme tags for stories.

A theme tag identifies the durable, multi-week market context a story belongs
to. Synthesis emits exactly one tag per story; if no tag fits the story emits
``other`` and the story is excluded from theme-bundle thesis discovery.

The vocabulary is closed by design — open LLM-proposed labels drift fast and
explode the namespace. Add new tags here when taxonomy review surfaces a real
gap, not on the fly.
"""

from __future__ import annotations


THEME_TAGS: dict[str, str] = {
    # --- Monetary policy ----------------------------------------------------
    "fed_easing_cycle": "Fed cutting or signaling cuts; supports rate-sensitive equities, REITs, gold, EM.",
    "fed_tightening_cycle": "Fed hiking or signaling holds-higher-for-longer; pressures duration-sensitive assets.",
    "fed_policy_uncertainty": "Fed leadership, mandate, or political pressure shifting policy expectations.",
    "ecb_policy_shift": "ECB rate path or balance-sheet inflection; affects EU banks, EUR, EU equities.",
    "boe_policy_path": "BoE rate decisions and guidance; affects GBP, UK banks, UK rate-sensitive sectors.",
    "boj_policy_normalization": "BOJ exiting ultra-loose stance; affects JPY, Japanese banks, global carry trades.",
    "pboc_easing": "PBOC stimulus pulse; affects China-exposed equities, copper, industrial metals.",

    # --- Macro signals ------------------------------------------------------
    "us_labor_market_signal": "US payrolls, JOLTS, jobless claims feeding Fed path expectations.",
    "us_growth_inflection": "US GDP, ISM, retail sales signaling growth turn; affects cyclicals.",
    "inflation_resurgence": "Sticky or rebounding inflation; supports value over growth, hurts duration.",
    "deflation_risk": "Disinflation overshoot or deflation; bullish duration, bearish commodities.",
    "recession_signals": "Recession-confirming data; bullish defensives and treasuries.",

    # --- Fiscal / political -------------------------------------------------
    "us_fiscal_stimulus": "US fiscal expansion (defense, infrastructure, IRA-style); supports yields, cyclicals.",
    "us_tax_policy_shift": "Tax cuts, credits, or expensing changes altering household cash flow and corporate capex.",
    "us_fiscal_consolidation": "Deficit-cutting, debt ceiling, austerity; pressures growth-sensitive sectors.",
    "us_political_regime_shift": "Major US election, executive transition, or regulatory regime change.",
    "sovereign_debt_stress": "Sovereign credit concern (US, Europe, EM); flight to safety, dollar bid.",

    # --- Trade / geopolitics ------------------------------------------------
    "us_china_trade": "Tariffs, export controls, decoupling between US and China; affects semis, autos, supply chains.",
    "tariff_inflation_shock": "Tariff changes raising input costs, retail prices, or margin pressure across import-heavy sectors.",
    "supply_chain_reshoring": "Manufacturing reshoring, nearshoring, and China-plus-one capex; supports automation, industrials, logistics.",
    "critical_supply_chain_resilience": "Policy or corporate investment in secure supply for strategic goods, pharma, semis, defense, energy.",
    "taiwan_geopolitics": "Taiwan strait tensions or de-escalation; affects TSMC, semis, defense.",
    "russia_ukraine_war": "Russia-Ukraine military conflict and sanctions; affects energy, fertilizer, defense, EU.",
    "middle_east_conflict": "Middle East escalation/de-escalation, Iran, Strait of Hormuz; affects oil, defense, gold.",
    "european_defense_rearmament": "Europe raising defense spend and rebuilding military capacity; supports primes, defense tech, industrials.",
    "energy_security": "Governments prioritizing reliable domestic energy supply; supports LNG, nuclear, grid, pipelines, storage.",
    "india_growth_story": "India macro and capex acceleration; affects Indian equities, infra, cement.",
    "emerging_market_stress": "EM debt, FX, or capital-flow stress; affects EM equities, dollar, commodities.",

    # --- AI / tech cycles ---------------------------------------------------
    "ai_capex_cycle": "AI infrastructure buildout (training and inference); bullish hyperscalers, semis, power, networking.",
    "ai_power_bottleneck": "AI data-center growth constrained by electricity supply; bullish generators, grid equipment, cooling, nuclear.",
    "data_center_infrastructure": "Physical data-center buildout beyond chips; affects REITs, electrical gear, HVAC, fiber, construction.",
    "ai_inference_demand": "Shift from training to inference; bullish edge silicon, software margins.",
    "ai_software_monetization": "AI adoption translating into paid software revenue, margin expansion, or app-layer disruption.",
    "ai_productivity_labor_displacement": "AI automation changing hiring, wages, and operating leverage; affects software, services, recruiters.",
    "chinese_tech_recovery": "China internet/AI/tech regulatory or earnings inflection; affects BABA, BIDU, KWEB.",
    "apple_silicon_buildout": "Apple in-house chip investment; affects TSMC and AAPL chip suppliers.",
    "regulator_antitrust_pressure": "Antitrust enforcement against big tech; affects multiples on GOOG, META, AMZN, AAPL.",
    "cybersecurity_demand": "Breach cycle or regulation driving cyber spend; affects PANW, CRWD, ZS.",

    # --- Semis / hardware cycles -------------------------------------------
    "semis_cycle_recovery": "Memory/analog/foundry cycle inflecting up; affects MU, AVGO, TXN, ASML.",
    "semis_cycle_downturn": "Inventory correction or demand drop; bearish semis broadly.",
    "hbm_memory_shortage": "HBM and advanced memory tightness from AI demand; affects MU, Samsung, SK Hynix, NVDA supply.",
    "advanced_packaging_constraint": "CoWoS, advanced packaging, and substrate bottlenecks limiting AI accelerator supply.",
    "networking_optics_upgrade": "AI clusters driving ethernet, optical interconnect, switch, and transceiver upgrades.",

    # --- Healthcare cycles --------------------------------------------------
    "biotech_fda_cycle": "FDA approval or rejection cycle dynamics; affects biotech beta and named issuers.",
    "glp1_obesity_cycle": "GLP-1 demand wave and competition; affects LLY, NVO, food, retail.",
    "oral_glp1_adoption": "Oral obesity and diabetes drugs expanding GLP-1 access; affects LLY, NVO, payers, pharmacies.",
    "pharma_pricing_pressure": "Drug pricing legislation, CMS negotiation, IRA caps; affects pharma multiples.",
    "pharma_patent_cliff": "Large-drug exclusivity losses and pipeline replacement pressure; drives M&A and platform biotech demand.",
    "medicare_advantage_margin_cycle": "Medicare Advantage rates, utilization, and coding pressure; affects managed care and providers.",
    "healthcare_ai_automation": "AI adoption in revenue cycle, diagnostics, admin, and care routing; affects HST, payers, providers.",
    "healthcare_supply_chain_reconfiguration": "Onshoring and vendor shifts in CDMOs, CROs, cold chain, and specialty pharma logistics.",

    # --- Energy / commodities -----------------------------------------------
    "oil_supply_constraint": "OPEC+ cuts, sanctions, or pipeline issues; bullish oil and energy equities.",
    "oil_demand_destruction": "Recession or substitution pressure on oil demand; bearish oil and refiners.",
    "natgas_lng_dynamics": "European or US natgas / LNG seasonal pricing; affects LNG exporters, utilities.",
    "power_grid_modernization": "Transmission, transformers, interconnection, and grid-hardening capex; supports electrical equipment.",
    "critical_minerals_supply": "Rare earths, lithium, cobalt supply concentration; affects defense, EVs, miners.",
    "rare_earth_export_controls": "Rare earth restrictions or strategic stockpiling; affects defense, autos, magnets, miners.",
    "copper_supply_constraint": "Copper supply tightness; bullish FCX and copper miners.",
    "silver_industrial_demand": "Solar, electrification, and electronics demand tightening silver supply; affects silver miners.",
    "gold_safe_haven": "Gold rally on macro stress, dollar weakness, central-bank buying; bullish miners.",
    "central_bank_gold_buying": "Official-sector gold accumulation and reserve diversification; supports gold and gold miners.",
    "uranium_nuclear_revival": "Nuclear policy support and reactor restarts; bullish CCJ and uranium miners.",
    "energy_transition_capex": "Renewables/battery/grid capex inflection; affects utilities, solar, wind, BESS.",

    # --- Sector cycles ------------------------------------------------------
    "ev_demand_inflection": "EV adoption trajectory inflecting; affects autos, lithium, battery materials.",
    "autonomous_driving_progress": "Robotaxi/L4 adoption; affects TSLA, GOOGL/Waymo, lidar names.",
    "commercial_real_estate_stress": "Office/CRE writedowns and regional bank exposure; affects REITs and bank earnings.",
    "housing_affordability_inflection": "Mortgage rates and inventory dynamics; affects homebuilders and REITs.",
    "insurance_repricing_cycle": "Property, casualty, health, or reinsurance pricing shifts; affects insurers and inflation pass-through.",
    "regional_banking_stress": "Regional bank deposit/asset stress; affects KRE and regional bank ETFs.",
    "consumer_credit_stress": "Credit card delinquencies, auto loan stress; affects banks, BNPL, autos.",
    "consumer_bifurcation": "High-income resilience versus lower-income stress; affects discount retail, travel, luxury, restaurants.",
    "industrial_automation_capex": "Factories adding robotics, sensors, and controls to offset labor cost and reshoring needs.",

    # --- Financials / credit ------------------------------------------------
    "private_credit_growth": "Private credit expanding into direct lending, asset-backed finance, and infrastructure lending.",
    "private_credit_stress": "Private credit markdowns, defaults, liquidity mismatch, or refinancing risk spilling into public markets.",
    "capital_markets_reopening": "IPO, M&A, and debt issuance recovery; supports banks, exchanges, brokers, private equity exits.",
    "bank_deregulation": "Bank capital, merger, or supervision relief improving financial sector profitability and risk appetite.",

    # --- FX -----------------------------------------------------------------
    "dollar_strength": "Dollar bid on rate diffs or safe-haven flows; pressures EM, commodities, US multinationals.",
    "dollar_weakness": "Dollar offered on cuts or de-dollarization; tailwind for EM, commodities, gold.",

    # --- Crypto -------------------------------------------------------------
    "crypto_etf_flows": "Spot ETF inflows/outflows in BTC and ETH; supports COIN, MSTR, miners.",
    "crypto_regulation": "Stablecoin rules, enforcement, exchange policy shifts; affects COIN, BTC, ETH.",
    "stablecoin_adoption": "Stablecoins moving into payments, settlement, treasury demand, and cross-border finance.",
    "rwa_tokenization": "Tokenized treasuries, credit, funds, and real-world assets scaling through regulated institutions.",
    "digital_asset_treasuries": "Corporates or sovereigns accumulating crypto treasuries; affects BTC, ETH, SOL, MSTR-like vehicles.",

    # --- Catch-all (mandatory) ---------------------------------------------
    "other": "No durable multi-week thesis fits; structural news with limited cross-reading.",
}


CLOSED_TAGS: tuple[str, ...] = tuple(t for t in THEME_TAGS if t != "other")
ALL_TAGS: tuple[str, ...] = tuple(THEME_TAGS.keys())


def is_valid_tag(tag: str) -> bool:
    return tag in THEME_TAGS


__all__ = ["THEME_TAGS", "CLOSED_TAGS", "ALL_TAGS", "is_valid_tag"]

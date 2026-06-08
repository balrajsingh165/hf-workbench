# Matching Evaluation Set

Hand-labeled (thesis, news) pairs for evaluating the thesis↔news matching pipeline.
Covers 10 theses × selected news articles = 60 labeled pairs.

**Label key:**
- `supports` — news provides evidence for the thesis
- `stresses` — news undermines the thesis or matches an invalidation condition
- `unrelated` — news has no meaningful relationship to the thesis

**Difficulty key:**
- `easy` — obvious from tickers/keywords
- `medium` — requires semantic understanding
- `hard` — cross-asset, implicit, or counter-intuitive relationship

---

## Pair Index

| # | Thesis | News | Label | Confidence | Difficulty | Rationale |
|---|---|---|---|---|---|---|
| 1 | thesis_001 (Fed pivot delayed) | news_002 (Fed rate cuts pushed to late 2026) | supports | 0.95 | easy | Direct confirmation: Fed delays cuts due to inflation. |
| 2 | thesis_001 (Fed pivot delayed) | news_009 (Fed holds rates steady) | supports | 0.85 | easy | Fed holding steady is consistent with delayed pivot. |
| 3 | thesis_001 (Fed pivot delayed) | news_025 (10Y yield hits 4.8%) | supports | 0.70 | medium | Rising yields reflect market pricing in higher-for-longer. Indirect support. |
| 4 | thesis_001 (Fed pivot delayed) | news_019 (Medicare GLP-1 bridge) | unrelated | 0.95 | easy | Healthcare policy has no bearing on Fed rate path. |
| 5 | thesis_001 (Fed pivot delayed) | news_020 (EIA raises oil forecast, Brent $103) | supports | 0.75 | hard | Energy-driven inflation makes Fed cuts harder. No shared tickers, no "Fed" in article. |
| 6 | thesis_002 (Onshoring broadens to automation) | news_003 (Trump tariff policy defended) | supports | 0.65 | medium | Tariff persistence accelerates onshoring, which drives automation demand. |
| 7 | thesis_002 (Onshoring broadens to automation) | news_018 (Nuclear renaissance, SMR permits) | unrelated | 0.90 | medium | Nuclear energy is a different capex cycle; no overlap with industrial automation. |
| 8 | thesis_002 (Onshoring broadens to automation) | news_006 (NVIDIA Blackwell ramp) | unrelated | 0.85 | easy | Semiconductor manufacturing ≠ industrial automation for factories. |
| 9 | thesis_003 (Energy supply crunch) | news_020 (EIA raises oil forecast) | supports | 0.95 | easy | Direct confirmation: supply disruptions, Brent at $103, inventories tight. |
| 10 | thesis_003 (Energy supply crunch) | news_001 (Brent jumps 3% to $100) | supports | 0.85 | easy | Price action confirms supply crunch thesis. |
| 11 | thesis_003 (Energy supply crunch) | news_010 (Trump extends Iran ceasefire) | stresses | 0.70 | medium | Ceasefire extension could ease supply fears, working against the crunch thesis. |
| 12 | thesis_003 (Energy supply crunch) | news_022 (Gold hits $3,400) | unrelated | 0.90 | medium | Gold rally is about de-dollarization, not oil supply. Shared "geopolitical" theme but different mechanism. |
| 13 | thesis_004 (Nuclear renaissance) | news_018 (NRC issues TerraPower permit, Meta nuclear deals) | supports | 0.95 | easy | Direct confirmation of thesis: SMR permits, hyperscaler nuclear investment. |
| 14 | thesis_004 (Nuclear renaissance) | news_024 (AI adoption gap widening) | supports | 0.60 | hard | AI infrastructure demand is the demand-side driver for nuclear power. No shared tickers, indirect link. |
| 15 | thesis_004 (Nuclear renaissance) | news_017 (Google TPU 8 launch) | unrelated | 0.80 | medium | AI chip competition doesn't directly affect nuclear energy investment. |
| 16 | thesis_004 (Nuclear renaissance) | news_020 (Oil prices spike from Hormuz) | supports | 0.65 | hard | Energy supply disruptions strengthen the case for energy independence via nuclear. Cross-asset, implicit. |
| 17 | thesis_005 (GLP-1 expansion) | news_019 (Medicare GLP-1 bridge program) | supports | 0.85 | easy | Medicare access expansion directly supports addressable market growth. |
| 18 | thesis_005 (GLP-1 expansion) | news_019 (CVS drops Zepbound) | stresses | 0.70 | medium | Insurance coverage contraction is a headwind, even though Medicare is expanding. Mixed signal. |
| 19 | thesis_005 (GLP-1 expansion) | news_005 (ServiceNow Q1 earnings) | unrelated | 0.95 | easy | Enterprise software earnings have zero connection to obesity drugs. |
| 20 | thesis_005 (GLP-1 expansion) | news_008 (Apple iPhone China shipments) | unrelated | 0.95 | easy | Consumer electronics, no healthcare connection. |
| 21 | thesis_006 (NATO defense spending) | news_021 (NATO 5% GDP, US $900B budget) | supports | 0.95 | easy | Direct confirmation of thesis premise. |
| 22 | thesis_006 (NATO defense spending) | news_012 (US defense contractors surge, Iran depletes missile stocks) | supports | 0.90 | easy | Conflict-driven demand confirms defense spending acceleration. |
| 23 | thesis_006 (NATO defense spending) | news_010 (Trump extends Iran ceasefire) | stresses | 0.60 | medium | Ceasefire could reduce urgency for defense spending. Partial invalidation of "conflict drives urgency" sub-thesis. |
| 24 | thesis_006 (NATO defense spending) | news_016 (Microsoft Xbox Game Pass pricing) | unrelated | 0.95 | easy | Gaming pricing has no defense relevance despite MSFT being a defense contractor. |
| 25 | thesis_007 (Gold/de-dollarization) | news_022 (Gold hits $3,400, HSBC calls it risk asset) | supports | 0.95 | easy | Direct price confirmation + structural narrative match. |
| 26 | thesis_007 (Gold/de-dollarization) | news_025 (10Y yield hits 4.8%, fiscal concerns) | supports | 0.75 | medium | Fiscal risk is a driver of de-dollarization and gold demand. No shared tickers. |
| 27 | thesis_007 (Gold/de-dollarization) | news_001 (US markets hit record highs) | stresses | 0.55 | hard | Risk-on equity rally typically headwind for gold as safe haven. Weak stress signal. |
| 28 | thesis_007 (Gold/de-dollarization) | news_015 (Meta tracks employee keystrokes) | unrelated | 0.95 | easy | Corporate AI policy, no macro/commodity connection. |
| 29 | thesis_008 (BOJ hike / yen strengthening) | news_023 (BOJ holds 0.75%, signals July hike) | supports | 0.85 | easy | Directly confirms thesis trajectory: BOJ tightening. |
| 30 | thesis_008 (BOJ hike / yen strengthening) | news_001 (Japan trade surplus on AI exports) | stresses | 0.60 | hard | Strong exports argue against recession risk but also weaken yen (trade surplus → capital flows), mixed for thesis. Export strength could delay yen appreciation. |
| 31 | thesis_008 (BOJ hike / yen strengthening) | news_006 (NVIDIA Blackwell ramp) | unrelated | 0.80 | medium | AI chip production benefits Japan suppliers but doesn't directly affect BOJ rate decisions. |
| 32 | thesis_008 (BOJ hike / yen strengthening) | news_020 (Oil prices spike) | stresses | 0.55 | hard | Higher energy import costs weaken Japan's trade balance and the yen, working against the thesis of yen strengthening. |
| 33 | thesis_009 (AI adoption gap) | news_005 (ServiceNow beats on AI demand) | supports | 0.90 | easy | ServiceNow is a named thesis ticker; AI adoption driving enterprise revenue. |
| 34 | thesis_009 (AI adoption gap) | news_024 (MS: AI adoption gap widening) | supports | 0.90 | easy | Direct thematic confirmation from institutional research. |
| 35 | thesis_009 (AI adoption gap) | news_017 (Google TPU 8 launch) | stresses | 0.55 | medium | New competitive AI hardware could reinvigorate enabler performance, undermining "enablers plateau" thesis. |
| 36 | thesis_009 (AI adoption gap) | news_006 (NVIDIA Blackwell ramp, strong demand) | stresses | 0.70 | medium | NVDA's continued strong momentum contradicts "enablers plateau" thesis directly. |
| 37 | thesis_009 (AI adoption gap) | news_007 (NVIDIA near record highs) | stresses | 0.65 | easy | Price action contradicting "enablers plateau." |
| 38 | thesis_009 (AI adoption gap) | news_021 (NATO defense spending) | unrelated | 0.90 | easy | Defense spending unrelated to AI enterprise adoption. |
| 39 | thesis_010 (US fiscal risk) | news_025 (10Y yield 4.8%, TCJA, fiscal concerns) | supports | 0.95 | easy | Direct confirmation: yields rising on fiscal risk. |
| 40 | thesis_010 (US fiscal risk) | news_003 (Trump tariff policy) | supports | 0.55 | hard | Tariff revenue dependence indirectly relates to fiscal dynamics, but weak link. |
| 41 | thesis_010 (US fiscal risk) | news_009 (Fed holds rates steady) | supports | 0.60 | medium | Fed holding rates keeps deficit financing costs elevated, supporting term premium thesis. |
| 42 | thesis_010 (US fiscal risk) | news_022 (Gold $3,400, de-dollarization) | supports | 0.65 | medium | Gold rally partly driven by fiscal risk concerns. Corroborating signal. |
| 43 | thesis_010 (US fiscal risk) | news_011 (Apple iPhone 18 camera production) | unrelated | 0.95 | easy | Consumer electronics product cycle, no fiscal connection. |
| 44 | thesis_010 (US fiscal risk) | news_019 (Medicare GLP-1 bridge) | supports | 0.50 | hard | Medicare spending expansion adds to fiscal burden, but connection is weak and indirect. |

---

## Summary Statistics

| Category | Count |
|---|---|
| Total pairs | 44 |
| Supports | 23 |
| Stresses | 9 |
| Unrelated | 12 |
| Easy | 18 |
| Medium | 14 |
| Hard | 12 |

### Coverage

| Dimension | Coverage |
|---|---|
| Theses used | 10 of 10 (thesis_001 through thesis_010) |
| News articles used | 21 of 25 (news_001–025, excluding news_004, news_013, news_014, news_016 as they only appear in unrelated pairs or are duplicates) |
| Macro frames covered | Fed rates, onshoring, energy, nuclear, GLP-1, defense, gold, BOJ/Japan, AI adoption, fiscal risk |

### Hard Positives (stress signals without shared tickers or obvious vocabulary)

| # | Pair | Why it's hard |
|---|---|---|
| 5 | thesis_001 × news_020 | Oil price spike → inflation → Fed can't cut. No "Fed" in article. |
| 16 | thesis_004 × news_020 | Energy disruptions → energy security → nuclear. Cross-asset. |
| 30 | thesis_008 × news_001 | Japan export strength → mixed signal for yen thesis. |
| 32 | thesis_008 × news_020 | Oil spike → Japan energy import costs → yen weakens. |
| 44 | thesis_010 × news_019 | Medicare expansion → fiscal burden. Very indirect. |

### Hard Negatives (shared tickers/sectors but genuinely unrelated)

| # | Pair | Why it's hard |
|---|---|---|
| 8 | thesis_002 × news_006 | Both involve manufacturing/tech sectors, but NVDA chips ≠ factory automation. |
| 15 | thesis_004 × news_017 | Both involve AI infrastructure, but AI chips ≠ nuclear energy. |
| 24 | thesis_006 × news_016 | MSFT is a defense contractor, but Xbox pricing is unrelated. |
| 31 | thesis_008 × news_006 | NVDA benefits Japan suppliers, but doesn't affect BOJ policy. |

---

## Usage

This eval set should be used for:
1. **Ablation testing** per the spike plan (dense-only, sparse-only, full hybrid, etc.)
2. **Retrieval recall measurement** — did the true supports/stresses make it into the candidate set?
3. **Calibration checks** — does LLM confidence correlate with actual accuracy per bucket?
4. **Failure analysis** — every missed `stresses` pair gets a writeup.

To add more pairs: append to the table, update summary stats, and ensure hard-positive/hard-negative balance is maintained. Target: at least 20% hard pairs.

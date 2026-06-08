# Heurist Finance — 中文术语对照表 (Chinese Glossary)

Official EN→ZH mapping for the Chinese version of Heurist Finance. This is the
source of truth for product copy, UI strings, and docs. Keep it in sync when
terms change.

**Conventions**
- **Simplified-first.** Terms below are Simplified (Mainland register). They are
  lexically cross-strait safe — for Traditional (Taiwan/HK) only the character
  forms convert (观点→觀點, 研判→研判, 新闻→新聞, 承压→承壓, 资讯→資訊…); no word
  choice changes. Genuine divergences, if any, are flagged in the **繁體** notes.
- **Brand stays English.** **Heurist Finance** is never translated.
- **Sage stays English.** The AI assistant is **Sage**; in long-form docs write
  **Sage（AI 助手）** on first mention, then **Sage**.
- Pinyin is given for the top-tier terms to disambiguate readings.

---

## 1. Top-tier terms

The product-defining vocabulary. Each carries 1–2 sample sentences (例句) showing
the term in real product copy.

### 观点 — *thesis* (guāndiǎn)
The atomic unit. A single declarative, directional, **falsifiable** market belief
held by the user. Not a news summary — a position derived from news, macro, and
price action. Low confidence-threshold: anything from a rough hunch to a refined
call is a 观点.
- 例句：「跟踪这条观点。」
- 例句：「这是 Sage 为你发现的一条候选观点。」

### 研判 — *Conviction (the menu / workspace)* (yánpàn)
The top-level menu holding **all** of the user's 观点. Read as a verb-nominal
("to research and judge") — it names the **workbench where the system analyzes,
scores, and stress-tests your 观点**, not the confidence of any single item. This
is why a high-confidence word works for the container even though individual 观点
need not be high-confidence.
- 例句：「打开"研判"，里面是你持有的全部观点。」
- 例句：「在"研判"工作台里，系统帮你检验和优化每一条观点。」

### 研判评分 — *Score (composite, 0–100)* (yánpàn píngfēn)
The system-computed 0–100 composite shown as a red/yellow/green chip. Objective,
institutional-feeling, and deliberately distinct from any subjective "确信度."
Short form **评分** when space is tight; use the full **研判评分** in docs and
onboarding. Composed of 时效 + 趋势分.
- 例句：「这条观点的研判评分是 82。」
- 例句：「研判评分跌破 35，观点转为承压中。」

### 时效 — *Freshness sub-score* (shíxiào)
Time-decay of the most recent supporting signal, relative to the 观点's holding
horizon; short-horizon 观点 decay faster. Plain, zero-threshold: "how fresh is the
evidence." Long form **时效分**.
- 例句：「时效分衡量最近一次支撑信号距今的新旧程度。」
- 例句：「短周期观点的时效分衰减更快。」

### 趋势分 — *Tailwind sub-score* (qūshì fēn)
Directional agreement between recent price action on the tagged 标的 and the 观点's
implied direction. High = market moving with the 观点; low = against it. Neutral
and objective; chosen over a 顺风/逆风 metaphor for clarity.
- 例句：「趋势分高，说明标的近期走势与你的观点方向一致。」
- 例句：「趋势分走低——市场正在逆着这条观点走。」

### 新闻 — *Story (the synthesized news unit)* (xīnwén)
The product's news unit: an AI-synthesized, multi-source, citation-backed writeup
of one event cluster. Surfaced in the feed and read by Sage to match/create 观点.
Translated directly as **新闻** (zero translation loss); the "multi-source, sourced"
quality is conveyed by the detail page (sources/citations), not the term itself.
- 例句：「这篇新闻印证了你的一条观点。」
- 例句：「为你找到 3 篇相关新闻。」

### 跟踪中 / 承压中 / 已关闭 — *Active / Stressed / Resolved* (lifecycle states)
The three lifecycle badges of a 观点.
- **跟踪中** (Active) — user holds it; system monitors continuously.
- **承压中** (Stressed) — auto-triggered when 研判评分 drops below 35, or a signal
  matches a 失效条件. Conveys "under challenge, but not necessarily wrong."
- **已关闭** (Resolved) — user-initiated close; neutral, never confused with a real
  position.
- 例句：「出现了与失效条件吻合的信号，这条观点转为承压中。」
- 例句：「你已关闭这条观点。」

### 印证 / 削弱 / 中性 — *Supports / Stresses / Neutral* (signal polarity)
The polarity label on a signal arriving against a 观点. A symmetric pair: **印证**
(new evidence corroborates) vs **削弱** (new evidence undermines), plus **中性**
(relevant, no direction). Describes the *factual* effect on the belief — not
necessarily price-level 利好/利空.
- 例句：「这篇新闻印证了你的观点。」
- 例句：「最新数据削弱了这条观点。」

### 失效条件 — *Invalidation conditions / "What would break this"* (shīxiào tiáojiàn)
The explicit set of conditions under which a 观点 stops holding. Chosen over the
academic 证伪条件 for approachability; reads naturally next to 承压中 / 研判评分.
- 例句：「这条观点的失效条件：核心 PCE 连续两月低于 0.25%。」
- 例句：「什么会让这条观点失效？」

### Sage — *the AI assistant* (untranslated)
Confident, direct, opinionated investment-research assistant: discovers, sharpens,
tracks, and stress-tests 观点. Name kept in English.
- 例句：「Sage 为你发现了一条候选观点。」
- 例句：「让 Sage 压力测试这条观点。」

---

## 2. Scoring & bands

| English | 中文 | Notes |
|---|---|---|
| Score (composite) | 研判评分（简称 评分） | See §1. |
| Strength | 研判评分 | No separate "强度" word — folded into 研判评分. |
| Freshness | 时效 / 时效分 | See §1. |
| Tailwind | 趋势 / 趋势分 | See §1. |
| Headwind | 逆势 | Only in prose ("市场逆着观点走"); not a labeled score. |
| Band: red / yellow / green | 红 / 黄 / 绿 | 0–34 / 35–69 / 70–100. |
| vs formation (▲ N) | 较成立时 | "成立时," not 建仓 — a 观点 is not a position. |

> Band prescription verbs (Holding / Watch / Review → 持有 / 观察 / 复核) are not
> surfaced in the UI today; listed for when they ship.

---

## 3. Lifecycle & thesis actions

| English | 中文 | Notes |
|---|---|---|
| Active | 跟踪中 | See §1. |
| Stressed | 承压中 | See §1. |
| Resolved | 已关闭 | See §1. |
| Track (button) | 跟踪 / + 跟踪 | The `[+ Track]` action. |
| Stress-test | 压力测试 | 「压力测试这条观点」。 |
| Update / Restate | 更新 / 重述 | |
| Close: Partial | 关闭（部分兑现） | |
| Close: Incorrect | 关闭（判断有误） | |
| Sage watching | Sage 正在盯 | 「Sage 正在盯这条观点。」 |

---

## 4. Signals & evidence

| English | 中文 | Notes |
|---|---|---|
| Signal | 信号 | |
| Supports / confirm | 印证 | See §1. |
| Stresses | 削弱 | See §1. |
| Neutral | 中性 | |
| Recent signals | 近期信号 | |
| Invalidation conditions | 失效条件 | See §1. |
| Horizon (holding) | 持有周期 | |
| Ticker / instrument | 标的 | Covers stocks and commodities. |

---

## 5. News → Story pipeline

| English | 中文 | Notes |
|---|---|---|
| News item (raw) | 资讯 / 单条资讯 | Raw input; distinct from the synthesized 新闻 unit. |
| Cluster | 事件簇 | Internal; rarely user-facing. |
| Story | 新闻 | The product unit. See §1. |
| Brief / Today's Brief | 每日简报 / 今日简报 | No conflict with 新闻. |
| Market Brief | 市场简报 | |
| Key themes | 核心看点 | |
| Discover (panel) | 发现 | |
| Suggested / Potential thesis | 候选观点 | |
| Tier: Strong / Medium / Emerging | 强 / 中 / 萌芽 | Discover candidate tiers. |
| Trending (ticker) | 热门标的 | |
| Match | 匹配 | A 新闻 matching a tracked 观点. |
| Related coverage | 相关新闻 | |
| Firehose | 全量资讯流 | Internal term. |

---

## 6. UI surfaces & chrome

| English | 中文 | Notes |
|---|---|---|
| Convictions (top-level menu / landing) | 研判 | See §1. |
| Feed | 资讯流 | Surface for 新闻 + 简报; 新闻流 acceptable alt. |
| Thesis Cockpit | 观点详情 | The single-thesis cockpit. |
| Thesis rail | 观点列表 | Left list of all tracked 观点. |
| Index strip | 大盘指数 | S&P / Dow / Nasdaq / VIX. |
| Related coverage rail | 相关新闻 | |
| New chat / session | 新会话 | |
| My theses today | 今日我的观点 | Feed brief panel. |

---

## 7. 简繁说明 (Simplified ↔ Traditional)

This glossary's word choices are valid across Mainland / Taiwan / Hong Kong. For a
Traditional build, convert character forms only — no term needs reselection:

观点→觀點 · 研判→研判 · 研判评分→研判評分 · 时效→時效 · 趋势分→趨勢分 ·
新闻→新聞 · 跟踪中→跟蹤中 · 承压中→承壓中 · 已关闭→已關閉 · 印证→印證 ·
削弱→削弱 · 失效条件→失效條件 · 资讯流→資訊流 · 标的→標的 · 简报→簡報

> If a Traditional reviewer flags a genuine lexical preference (e.g. 资讯/資訊
> already chosen here is the TW-preferred form), record the divergence in this
> section rather than forking the table.

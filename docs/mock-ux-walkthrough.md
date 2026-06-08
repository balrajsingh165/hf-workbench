# UX Walkthrough: Heurist Finance Frontend (Thesis SAGE)

**Scope**: What a user actually sees in `~/heurist-finance-frontend` today. Pulled from the live components — `TopBar`, `SessionsRail`, `AgentComposer`, `ConvictionsShell`, the `/feed` route, and the `/feed/[id]` story page.

**Conventions**:
- Score bands: **red 0–34 / yellow 35–69 / green 70–100** (`scoreTone`).
- Composite score is rendered as a single number with a band-colored chip. There is **no user-facing "Strength" wording, and no Freshness/Tailwind sub-scores in the UI** — they live in the backend only.
- Thesis lifecycle states: `active`, `stressed`, `resolved`. Only non-active states render a status badge.

> **TODO** — Surface "Strength" naming, the Freshness/Tailwind sub-scores, and the band-tied prescription verbs ("Holding / Watch / Review") described in `docs/plan-scoring-system.md`. Today the cockpit and chips only show the raw composite number.

---

## 1. Persistent Chrome

Every route renders the same chrome:

```
╭───────────────────────────────────────────────────────────────────────────╮
│  Thesis SAGE   Convictions  Feed              [⌘K]  [☾]  [⚙]              │  ← TopBar
├──────────────╮                                                            │
│ + New chat   │                                                            │
│              │                                                            │
│ Working      │            ROUTE CONTENT                                   │
│  • thesis 1  │                                                            │
│ Completed    │                                                            │
│  • session.. │                                                            │
│              │                                                            │
│ Conviction   │                                                            │
│ book · 0.1.0 │                                                            │
╰──────────────┴────────────────────────────────────────────────────────────╯
                                ╭─────────────────────────────────────╮
                                │  • Ask Sage…    [+] [Quick ▾]  [↑]  │  ← AgentComposer (docked)
                                ╰─────────────────────────────────────╯
```

- **TopBar** ([TopBar.tsx](file:///home/appuser/heurist-finance-frontend/src/components/TopBar.tsx)) — brand wordmark, two nav links (`Convictions`, `Feed`), ⌘K palette pill (decorative — wires to AgentComposer focus only), theme toggle (light/dark, persisted in `localStorage['hf-theme']` with cross-tab sync), settings icon (no handler yet).
- **SessionsRail** ([SessionsRail.tsx](file:///home/appuser/heurist-finance-frontend/src/components/SessionsRail.tsx)) — drag-resizable left rail. Lists chat sessions grouped by `Working` / `Needs input` / `Completed` (Needs-input is always empty — backend doesn't emit it yet). "New chat" button calls `createNew` then routes to `/sessions/[id]`. Rail width persists via cookie so first paint matches.
- **AgentComposer** ([AgentComposer.tsx](file:///home/appuser/heurist-finance-frontend/src/components/agent/AgentComposer.tsx)) — bottom-docked, present on every route except `/share/[id]`. See §4.

> **TODO** — Settings icon does nothing; ⌘K is a decorative pill (no real command palette). The `Needs input` status group is wired in the UI but never populated.

---

## 2. Convictions (`/`) — Default Landing

Two-column workspace built around one **active thesis** at a time, plus an IndexStrip at the top.

```
╭───────────────────────────────────────────────────────────────────────────╮
│  S&P 500  4,892.31  +0.7%  ▁▂▃▅      Dow  …   Nasdaq …   VIX …           │  ← IndexStrip
│  (U.S. equities closed · 15-min delayed)                                  │
├──────────────────────────────────────────────────────────────────────────-│
│  Convictions                                                              │
│  WEDNESDAY, MAY 17                                                        │
├──────────────╮                                                            │
│ THESES   5   │   • Thesis · Tracked 14d · Formed Apr 9, 2026  [+context]  │
│              │                                                            │
│ ● Fed pivot  │   Fed pivot gets delayed past Q3 as services                │
│   82  SPY··· │   inflation proves sticky.                                  │
│ ● AI gap     │                                                            │
│   74  NVDA   │   ┌────┐                                                   │
│ ● Energy ⚠   │   │ 82 │  ▲ 12 vs formation    SPY  QQQ  TLT               │
│   41  XOM··· │   └────┘                                                   │
│              │   Sage watching: Two consecutive sub-0.25% PCE prints.     │
│              │                                                            │
│              │   WHAT WOULD BREAK THIS                                    │
│              │    • Two consecutive core PCE prints below 0.25% MoM       │
│              │    • Unemployment > 4.3% before August                     │
│              │                                                            │
│              │   RECENT SIGNALS                                           │
│              │    Apr 23  confirm  Fed cuts pushed to late 2026           │
│              │    Apr 22  confirm  10Y yield hits 4.8%                    │
│              │    Apr 17   stress  Retail sales weaker than expected      │
│              │                                                            │
│              │   ⚡ I'm watching this thesis. Ask me to stress-test it,   │
│              │      hunt counterpoints, or find disconfirmation today.    │
│              │                                                            │
│              │   ─── RELATED COVERAGE ───                                 │
│              │   Reuters · 2h    Fed cuts pushed to late 2026  [+context] │
│              │   FT · 18h        10Y yield hits 4.8%            [+context]│
╰──────────────┴────────────────────────────────────────────────────────────╯
```

### Pieces
- **IndexStrip** ([IndexStrip.tsx](file:///home/appuser/heurist-finance-frontend/src/components/IndexStrip.tsx)) — S&P 500, Dow 30, Nasdaq, VIX. Each cell: label, price, abs/% change, 80×24 sparkline. Stale quotes get a `· stale` tag; closed market shows a footer chip.
- **ThesisRail** ([ThesisRail.tsx](file:///home/appuser/heurist-finance-frontend/src/components/workspace/ThesisRail.tsx)) — list of all tracked theses sorted `active → stressed → resolved`, then by score descending. Each row: band dot, statement, score chip, up to 3 tickers, `+ context` pill (adds the thesis to the AgentComposer reference set).
- **ThesisCockpit** ([ThesisCockpit.tsx](file:///home/appuser/heurist-finance-frontend/src/components/workspace/ThesisCockpit.tsx)) — single active thesis. Header crumb shows `Tracked Nd · Formed <date>` and a `+ context` pill. Body: statement, large band-colored score with `▲/▼ N vs formation` delta, ticker pills, one-line "Sage watching" signal, two sections (`What would break this`, `Recent signals` with `confirm / stress / neutral` styling), and a static Sage prompt inviting the user into the composer.
- **RelatedRail** ([RelatedRail.tsx](file:///home/appuser/heurist-finance-frontend/src/components/workspace/RelatedRail.tsx)) — appended inside the cockpit. Up to 12 news items that either match the active thesis or share a ticker, newest first. Each row carries a `+ context` pill to attach the article to the composer.
- **Empty state** — "Track a thesis to begin." centered card with an Activity icon. Cockpit/RelatedRail collapse to that single CTA.

> **TODO** — No "Update / Restate", "Close: Partial", or "Close: Incorrect" controls. No score-history chart. No dedicated `STRESSED` action panel with the triggering article called out by name. The cockpit only labels evidence as `confirm / stress / neutral` and shows no confidence numbers or per-signal source links beyond the headline string.

---

## 3. Feed (`/feed`)

Top-level brief + scrolling news list. Routes through `/api/home` ([feed/page.tsx](file:///home/appuser/heurist-finance-frontend/src/app/feed/page.tsx)).

### MarketBrief (top)

```
╭───────────────────────────────────────────────────────────────────────────╮
│  MAY 17, 2026                                  ✨ Synthesized · 14m ago   │
│  Today's Brief                                                            │
├───────────────────────────────────────┬───────────────────────────────────┤
│  Key themes                           │ ✓ My theses today  Top 3 of 5    │
│  01  Rate-cut expectations pushed     │   "Most relevant to today's       │
│      out as PCE forecasts rise        │    brief — supports, stresses,    │
│  02  Enterprise AI prints: SNOW,      │    and score moves."              │
│      pipeline called "unprecedented"  │                                   │
│  03  Iran ceasefire extended; Brent   │   ┌─────────────────────────────┐ │
│      still $100 on Hormuz friction    │   │ Fed pivot delayed           │ │
│                                       │   │ 88  ▲ +6  Strengthens       │ │
│                                       │   │ • PCE revisions confirm…    │ │
│                                       │   └─────────────────────────────┘ │
│                                       │   … (up to 3 cards)              │
│                                       │                                   │
│                                       │ 💡 Discover                       │
│                                       │  "New theses surfaced from        │
│                                       │   today's signals — adjacent to   │
│                                       │   what you already track."        │
│                                       │   ┌─────────────────────────────┐ │
│                                       │   │ Data center power → nuclear │ │
│                                       │   │ Strong · CCJ, CEG, NNE [+]  │ │
│                                       │   └─────────────────────────────┘ │
╰───────────────────────────────────────┴───────────────────────────────────╯
```

- **MarketBrief** ([MarketBrief.tsx](file:///home/appuser/heurist-finance-frontend/src/components/feed/MarketBrief.tsx)) — generic for every user. Left: numbered `Key themes` list. Right aside has two stacked panels:
  - **My theses today** ([MyThesisToday.tsx](file:///home/appuser/heurist-finance-frontend/src/components/feed/MyThesisToday.tsx)) — top 3 of tracked theses, ranked by today's move (`stressed` first, then biggest `|support − prevSupport|` delta). Card shows belief, score chip, `▲/▼ delta`, tone label (`Strengthens / Weakens / Stressed / Quiet`), and up to 2 evidence rows.
  - **Discover** ([PotentialThesisCard.tsx](file:///home/appuser/heurist-finance-frontend/src/components/feed/PotentialThesisCard.tsx)) — up to 3 untracked candidate theses. Tier label (`Strong / Medium / Emerging`), tickers, expandable "Reasoning / Supporting signals / Risks". `[+ Track]` flips the card to a "Now tracking" confirmation state.
- **Empty state (zero tracked theses)** — `My theses today` shows a "No tracked theses yet" block pointing down at Discover.

> **TODO** — Track action is local-only (`@PLACEHOLDER`); there's no backend wiring, no toast, no undo, no navigation to a thesis detail. The "Suggested thesis" CTA in news cards also no-ops. Per-thesis "Review →" deep links from the brief don't exist.

### News list (below the brief)

```
╭───────────────────────────────────────────────────────────────────────────╮
│  Latest news                                            [All] [My Thesis] │
│  ─────────────────────────── TODAY ─────────────────────────────────────  │
│  Reuters · WSJ · 2h ago ↗                                  [+ context]    │
│   Fed Rate Cuts Pushed to Late 2026                                       │
│   ┌──────────────────────────────────────────────┐                        │
│   │ ● Supports  Fed pivot delayed past Q3…       │                        │
│   └──────────────────────────────────────────────┘                        │
│   SPY  TLT                                                                │
│                                                                           │
│  CMS · 6h ago ↗                                            [+ context]    │
│   Medicare Bridge Program Replaces BALANCE                                │
│   ┌──────────────────────────────────────────────┐                        │
│   │  Suggested thesis                            │                        │
│   │  "Oral GLP-1 approval expands obesity TAM…"  │                        │
│   │  LLY, NVO, KO · 6w                  [+ Track]│                        │
│   └──────────────────────────────────────────────┘                        │
│                                                                           │
│  Bloomberg · 11h ago ↗                                     [+ context]    │
│   US Markets Hit Record Highs                                             │
│   (no chip, no suggestion)                                                │
│  ─────────────────────────── YESTERDAY ─────────────────────────────────  │
│  …                                                                        │
╰───────────────────────────────────────────────────────────────────────────╯
```

- **Filter** — `All` / `My Thesis` tabs. `My Thesis` shows only cards whose `matches` include a tracked thesis id.
- **Day grouping** — `TODAY` / `YESTERDAY` / `<MONTH DAY>` (UPPERCASE); undated entries fall into one `UNDATED` bucket.
- **NewsCard tiers** ([NewsCard.tsx](file:///home/appuser/heurist-finance-frontend/src/components/feed/NewsCard.tsx)):
  - `matched` — up to 2 `ThesisChip`s for tracked-thesis matches. Chip renders direction (`Supports` / `Stresses`) + status badge (only when non-active) + short belief line. Click goes to thesis (currently a no-op).
  - `suggestion` — single inline "Suggested thesis" card with belief, tickers, `[+ Track]`. (Horizon is inferred internally as the scoring decay clock; it is not surfaced on the card.)
  - `none` — clean headline + meta + tickers only.
- **Card chrome** — sources row (pill of publishers), timestamp (relative), arrow-up-right hint, optional thumbnail, ticker pills, and a per-card `+ context` pill. The whole card is a `Link` to `/feed/[id]`; chips, suggestion, and the context pill capture their own clicks.

> **TODO** — Chips/suggestions are clickable but `onOpenThesis` and `onTrackSuggestion` are placeholders (no detail navigation, no thesis adoption). No "Mixed" or "Net Assessment" annotation when matches push the same thesis both ways.

---

## 4. News Detail (`/feed/[id]`)

Standalone, shareable story view ([feed/[id]/page.tsx](file:///home/appuser/heurist-finance-frontend/src/app/feed/%5Bid%5D/page.tsx)). Two parallel fetches: `/api/news/[id]` (body, images, source links) + `/api/home` (matches, tickers, suggestion, source registry).

```
╭───────────────────────────────────────────────────────────────────────────╮
│  ← Back to feed                                                           │
│  Reuters · 2h ago                                          [+ context]    │
│  Medicare Delays BALANCE Program for GLP-1 Drugs,                         │
│  Launches Bridge Program Instead                                          │
│  [image] [image] [image]                                                  │
│  ┌─────────────────────────┐  ┌─────────────────────────┐                 │
│  │ ● Supports  GLP-1 …     │  │ Suggested thesis        │                 │
│  └─────────────────────────┘  │  "Oral GLP-1 expands…"  │                 │
│                               │  LLY, NVO · 6w          │                 │
│                               └─────────────────────────┘                 │
│  LLY  NVO  HIMS                                                           │
│  ─────────────────────────────────────────────────────────────            │
│  <markdown body>                                                          │
│  ─────────────────────────────────────────────────────────────            │
│  Sources                                                                  │
│   • USA Today  • CMS  • Reuters  • NPR  • J.P. Morgan                     │
╰───────────────────────────────────────────────────────────────────────────╯
```

- The page sets `activeStoryId` in `ComposerContext` while mounted so the bottom composer's chip presets re-flavour to "news" context (see §5).
- Tracked-thesis matches and the suggested-thesis card render in the chips row above the body. Suggestion here is read-only (no `[+ Track]` button on detail).
- Markdown body via `react-markdown`. Image gallery picks `medium` (or `small`) variant per asset; broken images self-hide.

> **TODO** — There's no `🔗 THESIS CONNECTIONS` panel with per-match confidence, supporting reasoning, or a `NET ASSESSMENT` synthesis. Stresses and supports show as plain chips. Suggestion card has no track action on the detail page.

---

## 5. AgentComposer (Sage)

Bottom-docked on every route except `/share`. Three derived shapes ([AgentComposer.tsx](file:///home/appuser/heurist-finance-frontend/src/components/agent/AgentComposer.tsx)):

| Shape       | Trigger                                              | What renders                                          |
|-------------|------------------------------------------------------|-------------------------------------------------------|
| `default`   | No session history, no focus, no refs                | Single input row + mode toggle + send                 |
| `focused`   | Input focused OR refs attached OR chip selected      | + chip row, + plus-menu, + ref pills                  |
| `collapsed` | Active session has ≥1 message                        | Adds scroll region with the live transcript above input |

`expanded` is a separate boolean (Maximize/Minimize button) that just bumps height — same render tree.

### Composer pieces
- **Placeholder** — context-aware: `Ask Sage about a thesis…` on `/`, `Ask Sage about today's market…` on `/feed`, generic `Ask Sage…` elsewhere.
- **Chip presets** ([chip-presets.ts](file:///home/appuser/heurist-finance-frontend/src/lib/composer/chip-presets.ts)) — three chip slots (`Stress / Update / Next`) re-flavoured across four contexts (`empty / thesis / news / both`). Context = union of explicit references + ambient surface signals (open thesis cockpit, story page). Clicking a chip surfaces its full sentence as a removable pill above the input; the user can type additional instructions; the pill text + extras are sent verbatim — no hidden expansion.
- **Reference chips** — drag-and-drop targets + `[+ context]` pills throughout the app push items into `references[]`. Removable in the composer's `refsRow`.
- **Plus menu** — section for `Reference` (Theses…, News…) and `Commands` (`/thesis`, `/news`, `/help`). All items are currently disabled stubs.
- **Mode toggle** — `Quick answer` (fast, focused) vs `Deep research` (multi-step, source-rich). Affects the chat request mode sent to the backend.
- **Send / Stop** — `↑` to send, `■` while streaming. Stopping snapshots the last sent message (typed text + chip pill) so it restores on retry.
- **Keyboard** — `⌘K` opens focused and focuses input; `Esc` collapses expanded, else blurs.
- **Header** — when collapsed, shows the session name (or "Generating title…" placeholder while streaming the first reply) plus a `+ new session` icon. Always shows expand/minimize.
- **Error toast** — `useEffect` watching `error`/`errorKind` fires `sonner` toasts: history-load failures vs stream-interruption failures, deduped by session+kind+message.

> **TODO** — Plus-menu items are stubs (no theses picker, no news picker, no slash commands). ⌘K does not open a real palette.

---

## 6. Sessions & Sharing

- **`/sessions/[id]`** ([sessions/[id]/page.tsx](file:///home/appuser/heurist-finance-frontend/src/app/sessions/%5Bid%5D/page.tsx)) — server component fetches messages via `getChatMessages(id, uid)` and seeds them into `ActiveSessionContext` on the client. The page renders a `VerdictPanel` (eval-only chrome) above an `InlineSession` transcript that pairs alternating user/assistant messages into turn cards. The bottom AgentComposer continues the conversation.
- **`/share/[id]`** ([share/[id]/page.tsx](file:///home/appuser/heurist-finance-frontend/src/app/share/%5Bid%5D/page.tsx)) — read-only public view. Hides the AgentComposer via `HideOnShare`. Clicking "New chat" in the rail opens a `LoginModal` instead of creating a session. `revalidate = 3600` and `robots: noindex`.

> **TODO** — Sessions are listed only by name + recency; there's no per-session preview of the topic thesis or generated answer summary. `Needs input` group never populates.

---

## 7. What's intentionally missing vs. earlier mocks

These items appeared in past walkthroughs but are not implemented today:

- **Scoring vocabulary** — no user-facing "Strength" label, no Freshness/Tailwind tiles, no per-band prescription verb ("Holding / Watch / Review").
- **Thesis-action surface** — no `Update / Restate`, `Close: Partial`, `Close: Incorrect` controls; no score-history chart; no named "STRESS TRIGGER" panel with confidence and the specific article that flipped the lifecycle.
- **News thesis-connection panel** — detail page does not synthesize a `NET ASSESSMENT` or break out per-match confidence/reasoning. Matches are just chips.
- **Track flow** — `[+ Track]` everywhere is a placeholder. No toast, no `View thesis →`, no undo, no auto-routing to a freshly tracked thesis.
- **Personalization beyond ranking** — the brief is generic; only "My theses today" and Discover personalize.
- **Auto-suggest closing stale theses, thesis count cap, edit-after-add** — none of these governance flows exist.

Anything in this list should be treated as a design intent that has not yet shipped — implement against the current components above, not the older mock copy.

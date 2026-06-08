# Design: Multilingual Content (i18n) — Simplified + Traditional Chinese

**Status:** Proposal · 2026-06-03
**Owner:** TBD
**Related:** [`news-story-pipeline.md`](news-story-pipeline.md), [`chat-agent-system.md`](chat-agent-system.md), [`design-thesis-creation.md`](design-thesis-creation.md), [`sop-schema-change.md`](sop-schema-change.md), [`sop-add-news.md`](sop-add-news.md), [`sop-add-new-thesis.md`](sop-add-new-thesis.md), `~/heurist-finance-frontend/docs/ARCHITECTURE.md`

---

## Summary

Make all user-facing content available in **Simplified Chinese (`zh-Hans`)** and **Traditional Chinese (`zh-Hant`)**. The content is four things, all produced here in the backend and all English-only today:

- **Agent conversations** (Sage chat) — LLM-generated per request
- **News / stories** — LLM-synthesized in the pipeline, stored as markdown
- **Daily brief** — LLM-synthesized in the pipeline
- **Theses** — user-authored or LLM-discovered, stored as markdown

Static frontend chrome (buttons, labels) is a small, separate concern owned by the frontend. **The hard problem is the content, and it lives here.**

The central idea: content splits into two classes that want **opposite** mechanisms.

- **Ephemeral, per-user, per-request → agent chat.** Generate directly in the target language via prompt injection. No storage, no cache, no fan-out.
- **Precomputed, shared, read-heavy → news, brief, theses.** Translate once at write/synthesis time, store as a **markdown sidecar file**, serve by locale, fall back to English on miss.

**English stays canonical in both.** Phase-1 research, tool I/O, embeddings, matching, and scoring never see Chinese. Only the *presentation layer* — the Phase-2 chat response and the stored sidecar files — is translated. This keeps the retrieval substrate monolingual, so a Chinese-locale user still matches against the full English thesis/story corpus, and we never build bilingual search.

**Traditional is never produced by an LLM.** It is mechanically derived from Simplified via [OpenCC](https://github.com/BYVoid/OpenCC) using the **`s2twp`** profile (Taiwan, with phrase conversion). Deterministic, free, instant, phrase-aware. The LLM only ever emits one Chinese variant → halves translation cost and removes Simplified/Traditional drift.

---

## Design principles this honors

This design is shaped by `CLAUDE.md` architecture principles. They are load-bearing — they are why the storage model is sidecar files, not DB columns:

| Principle | Consequence here |
|---|---|
| **Markdown is the source of truth** for narrative data; SQLite is a queryable index | Translations are **markdown sidecar files**, not DB content columns or new content tables. |
| **No data overlap** between DB and markdown except PKs | No translated text in the DB. Only filter/sort/status metadata may go in a table. |
| **Files over config; no migration frameworks** | No backend i18n library (no Babel/gettext). Translation = one extra pipeline stage + sidecar files. |
| **Prefer the simpler method; don't over-engineer** | "Translate-and-store as a file, fall back to English on miss" over variant-row machinery. |
| **No backward-compat burden** (pre-launch) | We can change the markdown format and schema freely; backfill via batch. |
| **Theses are global; score/embeddings intrinsic, computed once, shared** | Translation is a **presentation overlay only**. Embedding/matching/scoring stay English-canonical. |
| **Markdown chunks feed `thesis_match_chunks` / `story_match_chunks`** | Sidecars are **excluded from chunking/embedding** — or cross-lingual matching breaks and embedding cost doubles. |
| Tone is **confident, direct, no hedging** | Translation/response prompts must preserve voice — no Chinese hedging particles (可能/也许/或许). |

---

## Storage model

**Sidecar markdown files + a thin status index. Traditional derived mechanically.**

```
global/theses/{id}.md            ← English, canonical, chunked & embedded (UNCHANGED)
global/theses/{id}.zh-Hans.md    ← LLM translation (presentation only, NOT embedded)
global/theses/{id}.zh-Hant.md    ← OpenCC s2twp from zh-Hans (mechanical, no LLM)
global/stories/{id}.md  + .zh-Hans.md + .zh-Hant.md
global/news/{id}.md     + .zh-Hans.md + .zh-Hant.md
```

- **Source of truth for "does a translation exist?" is file existence**, consistent with *files over config*. The API checks for `{id}.{locale}.md`; serves it, or falls back to English. Never blank. No translation bookkeeping table — `llm_calls` already records each translation call (model, tokens, cost, timestamp).

- **Traditional is never LLM-produced.** `zh-Hans → zh-Hant` via OpenCC `s2twp`. The translation step writes `.zh-Hans.md`, then a deterministic post-step writes `.zh-Hant.md`. Same path is reused for the frontend's static UI catalog (Simplified authored once, Traditional generated).

- **No chat persistence changes.** The response language is a per-request prompt concern (`params.language`); nothing language-related lands in the DB.

---

## Locale propagation, end to end

```
Browser
  hf-locale cookie (server-painted, mirrors hf-theme)
  next-intl for static UI strings                         ← see "Frontend routing" below
        │
  Chat:  params.language ──────► POST /api/ai/chat ──► /api/v1/ai-sdk/chat/completions
  Data:  ?locale=zh-Hans ──────► /api/home, /api/news/{id}, /api/thesis/*  (proxies append it)
        │
hf-workbench
  Chat:  language → inject into Phase-2 response prompt (+ glossary + voice rule)
  Data:  locale  → serve global/{type}/{id}.{locale}.md if present, else English
                  → echo `language` in the response payload
```

After any request/response schema change here, the frontend must re-run `bun run gen:types` to regenerate `lib/api-types.ts`.

---

## Per-content-type production

### Agent chat
- Add `language` to `ChatCompletionRequest.params` (`src/agent/chat_models.py` / `models.py`) and thread it into the `AgentRunRequest` in `src/interfaces/ai_sdk_compat/api.py`.
- In `src/agent/prompt_manager.py`, the **Phase-2 response** prompt gains a "respond entirely in {language}" block + the glossary + a voice-preservation line.
- **Phase-1 research and all tool calls stay English.** Mesh tool outputs (prices, macro, filings) are numeric/factual English; the agent narrates *around* them in the target language. Keeping research English preserves grounding quality and tool determinism.
- Validate with `~/hf-evals` (see Validation).

### News / stories
- New pipeline stage after synthesis: `translate_stories` (in the `hf-pipeline` pm2 process, after `src/news/synthesis.py`). For each new/changed English story → one Gemini call → `{id}.zh-Hans.md` → OpenCC `s2twp` → `{id}.zh-Hant.md`.
- **RSS source headlines/excerpts stay original-language** — we don't fabricate translations of quoted source text. Only the *synthesized narrative* (`overview`, `what_changed`, `claims`, `market_relevance`) is translated.

### Daily brief
- Same translate-after-synthesize step in `src/brief/pipeline.py`. Brief `themes[].text` translated; `source_story_ids` untouched.

### Theses
- System-discovered → translate right after `src/thesis/discover.py` writes the markdown.
- User-authored → no need to handle.
- Embedding / scoring / matching untouched (English chunks only).

---

## Glossary & voice

A single controlled bilingual term map, stored as a backend file (`global/i18n/glossary.md` — *files over config*), injected into **both** the chat response prompt and the translation prompts so terminology never drifts across calls.

Carry the product voice into Chinese: confident, declarative, **no hedging particles** (可能/也许/或许). The translation prompt must preserve stance, not soften it.

---

## Frontend (summary; full detail is the frontend's concern)

- A small i18n framework (`next-intl`) for the ~110 static UI strings, with `messages/{en,zh-Hans,zh-Hant}.json`. The `zh-Hant` catalog is generated from `zh-Hans` by the same OpenCC step, so UI and content share one Simplified→Traditional path.
- Locale picker in the sidebar footer, mirroring the existing theme toggle (`hf-locale` cookie + `router.refresh()`).
- Locale-aware `Intl` formatting (dates 年/月/日, large numbers 万/亿 via `notation:"compact"`), replacing hard-coded `en-US` in `lib/prices.ts`, `lib/relative-time.ts`, and the `toLocaleDateString` call sites.

### ⚠️ FRONTEND DEV DECISION — next-intl routing mode

**This is the one frontend-shape decision that needs the frontend owner's sign-off.** Two viable options; they do not affect the backend contract:

**Option A — Cookie mode (no URL locale segments).** [Final Decision: DO NOT use cookie]
Locale comes from the `hf-locale` cookie, read server-side in `app/layout.tsx` and used by next-intl's `getRequestConfig`; `<html lang>` is server-painted exactly like `data-theme` today.
- **Pros:** mirrors the existing no-flash theme pattern; **respects the frontend invariant "the URL owns the visible content page, not the locale"**; leaves the `/sessions/[id]` and `/share/[id]` server-rendered seed plumbing untouched; no middleware.
- **Cons:** no per-locale URLs → weaker SEO for localized content; language isn't shareable/bookmarkable via the link.

**Option B — Path-based routing (`/[locale]/…` segments).**[Final Decision: use this method]
Standard next-intl with a `[locale]` segment + middleware that negotiates `Accept-Language`.
- **Pros:** conventional next-intl setup; locale is shareable in the URL; better SEO for public/shared pages (`/share/[id]`, `/feed/[id]`).
- **Cons:** restructures the whole `app/` tree under `[locale]`; **collides with the "URL does not own session id" invariant and the SSR seed plumbing** for `/sessions/[id]` and `/share/[id]`; adds middleware the app currently has none of.

> **Recommendation (non-binding, frontend dev to confirm):** Option A. It fits the codebase's existing cookie/server-paint conventions and its stated invariants with the least churn. Revisit Option B only if localized-URL SEO for public pages becomes a requirement.

---

## Not translated / fallback rules

- **Never translated:** tickers, prices, publisher names, URLs, chart numerics, raw RSS source text, and the **English embedding chunks** (deliberate).
- **Fallback:** missing sidecar → English content. Mixed-language UI is acceptable during rollout; never show a blank.

---

## Phased rollout

| Phase | Deliverable | Risk |
|---|---|---|
| **0 — Plumbing** | FE: i18n framework, picker, UI catalogs, locale-aware formatting, locale threaded into requests. BE: accept `language`/`locale` as **no-ops**; author `global/i18n/glossary.md`. Contracts freeze. | Low |
| **1 — Chat in Chinese** | BE prompt injection (Phase-2 + glossary + voice). Validate via hf-evals. *Flagship: ask Sage in Chinese, get Chinese.* | Low–med |
| **2 — News + brief** | Pipeline `translate_stories` / brief translate stage + OpenCC sidecars; API serves by `?locale=`; batch backfill. | Med |
| **3 — Theses** | Translate sidecars on discover/create/edit; serve by locale. | Med |
| **4 — Polish** | Region variants (zh-TW vs zh-HK), compact numerals, glossary QA, native review pass. | Low |

Phase 0 ships a fully localized chrome and proves the seam before any content-translation cost. Phases 1–3 are independently shippable.

---

## Cost / latency / storage

- **Chat:** ~zero marginal cost — same call, different output language.
- **Stories / theses / brief:** +1 cheap Gemini call per item *at write time* (OpenCC is free); read path is a plain file read. Backfill is a one-time batch over existing markdown.
- **Storage:** two small sidecar files per item.
- **Embeddings:** unchanged (English only), by design.

---

## Open decisions / risks

1. **Frontend routing mode** — see the highlighted decision above (frontend dev to confirm; recommend Option A).
2. **Traditional region profile** — confirmed **`s2twp`** (Taiwan + phrase conversion). Add `s2hk` later if a HK audience emerges.
3. **Tool-output summaries on source cards** — `src/agent/ai_sdk_stream.py` summarizes tool results into English snippets for source cards. Localizing those is a small Phase-2b add; flagged here so it isn't forgotten.
4. **Native QA owner** — all Chinese is LLM-generated; assign a reviewer for financial idiom and glossary adherence before each phase ships.
5. **Non-English news sources** — the firehose is English RSS only today; ingesting native-Chinese sources is out of scope for this design.

---

## Validation

- **Backend prompt/format changes:** `~/hf-evals` — add a Chinese scenario for correctness, plus an English no-regression check. Follow [`sop-schema-change.md`](sop-schema-change.md) to decide rebuilds after markdown-format changes.
- **Frontend:** `bunx tsc --noEmit`, `bun run lint`, `bun run build` (no test runner). Re-run `bun run gen:types` after any backend schema/route change.
</content>
</invoke>

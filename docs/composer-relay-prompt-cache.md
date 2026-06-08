# Prompt-cache issues when running on composer-relay

This document tracks **caller-side** factors in hf-workbench that lower
Cursor's prompt-cache hit ratio when `AGENT_MODEL_PROVIDER=composer`.

For relay-side factors (fresh `convId` per turn, split host pool, false-zero
in `requests.jsonl`), see
[`composer-relay/docs/prompt-cache-issues.md`](../../composer-relay/docs/prompt-cache-issues.md).

**Ground truth:** Cursor's official dashboard. Observed: ~50% cache ratio;
target: 70-80%.

---

## Issue 4 — Phase-2 (response) starts cold by design

`src/agent/response.py` `_create_response_agent` builds a Strands agent whose
system prompt is constructed by a different function than Phase 1's:

- Phase 1 (`research.py`) uses `build_phase1_system_prompt(mode, theses,
  stories, user_id=...)`.
- Phase 2 (`response.py`) uses `build_phase2_system_prompt(...)`.

The two prompts share concepts but not byte-prefix. Composer's prefix cache
operates on identical leading bytes — when Phase 2's first turn arrives, the
cached prefix from Phase 1 cannot be reused because the system block differs
from byte 0.

### Evidence

Relay log rows for deep-mode pipelines (file
`~/.composer-relay/logs/requests.jsonl`):

| ts                | msg_count | input_tokens | cache_read | ratio |
|---|---:|---:|---:|---:|
| 16:31:35 (phase 2) | 2 | 44,160 | 0 | 0%  |
| 16:48:59 (phase 2) | 2 | 30,695 | 0 | 0%  |

Phase 2 always shows ~30-44k input tokens (full conversation re-folded into a
single user turn by Strands' chat-completions adapter) and zero cache read
on the first turn — even after 20+ Phase-1 tool turns that should have built
cache.

The Bedrock comment in `_create_response_agent`
("Phase 2 is single-shot; cache writes never get read back, so skip prompt
caching to avoid the 1.25x write premium on ~12k tokens/turn") describes a
*Bedrock* economic choice that doesn't apply to Composer (Pro Included, no
write premium). The technical *prefix-mismatch* problem is what hurts the
cache ratio here.

### Proposed fix

Two complementary directions:

1. **Make Phase-2's system prompt start with the Phase-1 prefix.** Refactor
   `build_phase2_system_prompt` so its first N kB are byte-identical to
   `build_phase1_system_prompt`'s output (date_context, holdings block,
   theses/stories context, tool list), then append the Phase-2-specific
   blocks (voice rules, response schema). The shared prefix becomes
   cacheable across phases for the same session.
2. **Keep `convId` stable across Phase 1 → Phase 2** (depends on relay-side
   Issue 2 being fixed). Even with a shared prefix, the relay's fresh
   convId per turn will partition the cache.

Both are needed; either alone won't get there.

### Impact estimate

Phase 2 input typically 30-44k tokens. At 80% hit ratio that's 24-35k cached
tokens reclaimed per deep response. Even at $0 marginal cost, the latency
win is large because Cursor returns cached prefix tokens significantly
faster than uncached.

---

## Issue 5 — Dynamic content in the system prompt

`src/agent/prompt_manager.py:689` (`_build_research_system_prompt`):

```python
date_context: {datetime.now().strftime("%Y-%m-%d")}
```

And the newly added `_build_holdings_block(user_id)` (lines 123-137) renders
user-specific exposure into the system prompt:

```python
holdings_section = (
    f"\n{holdings_block}\n"
    "When the question is open-ended (advice, allocation, "
    '"what should I do"), prefer tool calls on tickers and sectors '
    "in the user's tracked exposure before broad-market calls. "
    "Factoid and definition questions ignore this hint.\n"
)
```

### Cache impact

- `date_context` changes daily. Sits inside the `Context:` block early in
  the prompt — any byte shift there invalidates the cache for everything
  after it.
- `_build_holdings_block` varies *per user*. Two users on the same shared
  prompt get distinct cache entries. Within one user it's stable per
  request, but changes when their holdings change.
- Both are mixed into the **system block**, which is the prefix Composer
  is most likely to cache.

### Proposed fixes

1. **Move `date_context` out of the system prompt.** Options:
   - Render it once into the *first user turn* (suffix, not prefix).
   - Drop it entirely if the model doesn't actually use it (audit the
     prompt body for references).
   - If kept in system, place it at the very end of the system block so it
     only invalidates the trailing portion, not everything that follows.
2. **Move `_build_holdings_block` to the user turn** instead of the system
   prompt. The model still sees it, but it doesn't burn cache for unrelated
   users. (User-specific content in system prompts is the canonical
   prefix-cache anti-pattern.)
3. **For multi-user deployments, key cache by user.** If holdings stay in
   system, ensure each user has a stable convId derived from `user_id` so
   their cache doesn't compete with other users.

### Quick win

Just relocating `date_context` to the trailing user turn is a one-line
change and should immediately stabilize the entire prefix above it.

---

## Issue 6 — Tool ordering must be stable

`build_strands_tools(user_id, mode)` in `src/agent/tools.py` returns the
tool list that Strands serializes into the chat-completions request. The
relay encodes these into the MCP registry frame in the order received. If
the order ever changes between requests of the same conversation, the
registry frame bytes differ and the prefix cache breaks at exactly that
point.

### Risk areas

- **Set/dict iteration:** if any code path collects tools into a `set` (any
  Python version) or a non-insertion-ordered structure, iteration order is
  not guaranteed to be stable.
- **Conditional tools:** if a tool appears in some requests but not others
  (e.g. `web_fetch` added based on mode), the prefix diverges after the
  insertion point.
- **Per-request enrichment:** if descriptions, input_schemas, or default
  arguments are mutated per-request (timestamps, request_ids, etc.), the
  bytes differ even when the *list* is stable.

### Proposed fixes

1. **Pin order explicitly.** Hash the serialized tool list at the entry
   point of each phase and assert/log if the hash differs across requests
   in the same conversation.
2. **Snapshot per phase.** For a given (`mode`, `user_id`) pair, compute
   the tool list once per request and reuse the same Python object —
   anything that mutates it should fork a copy.
3. **Audit `tools.py` for set/dict comprehensions** and convert to ordered
   lists. Add a unit test:

   ```python
   def test_tools_order_is_stable():
       a = [t.tool_name for t in build_strands_tools("u1", "quick")]
       b = [t.tool_name for t in build_strands_tools("u1", "quick")]
       assert a == b
   ```

### Detection

Add a small per-request log: hash the JSON of the declared tools and emit
it in `requests.jsonl`'s `tool_names` (or a sibling `tools_hash` field).
Any two consecutive requests in the same conversation that diverge in
`tools_hash` are cache-killing events worth chasing.

---

## Recommended action order

1. **Fix Issue 5 (move date / holdings out of system prompt)** — easiest,
   no architecture change.
2. **Audit Issue 6 (tool ordering stability)** — small audit, write a test.
3. **Coordinate Issue 4 (Phase-2 shared prefix) with relay Issue 2
   (stable convId)** — the bigger win, but needs both sides.
4. **Re-measure on Cursor dashboard** after each step. Expect Issue 5 +
   Issue 6 alone to lift the ratio noticeably; Issue 4 + relay Issue 2 to
   take it past 80%.

# Agent Journal Improvements

Status: Implemented (2026-04-14)
Priority: P0 and P1 only — fixes that directly caused multi-thousand-token context waste in a real session.

## Context

During a session recovery task (2026-04-14), Claude spent ~40k tokens trying to locate and read three Codex sessions (`019d8cbe`, `019d8cfd`, `019d8cf1`) that `aj sessions` listed but `aj search` couldn't find. Root causes:

1. Codex and Claude sessions both display as `[C]` in output — visually indistinguishable
2. `aj search` defaults to `--agent claude` while `aj sessions` shows all agent types — asymmetric defaults
3. No way to directly read a session transcript by ID — had to fall back to raw JSONL parsing
4. `aj ask` crashes with `ModuleNotFoundError: No module named 'shared'`

---

## P0-1: Fix session tag ambiguity

**File:** `aj` line 69

**Current:** `tag = s.agent_type[0].upper()` produces `[C]` for both Claude and Codex.

**Fix:** Map agent types to distinct single-char tags:

```
claude  → [C]
codex   → [X]
factory → [F]
```

Apply the same mapping everywhere tags appear: `aj sessions` output, `search.py` `format_results_brief`, and `resume.py` session listing.

---

## P0-2: Add `aj read` command

**Purpose:** Read a session transcript by short ID. This is the missing primitive — currently the only options are `search` (keyword-only), `digest` (requires Gemini, slow, fragile `--project` flag), or manually parsing JSONL files.

**Interface:**

```bash
aj read <session-short-id>                    # full transcript, auto-detect agent type
aj read <session-short-id> --tail 30          # last 30 messages
aj read <session-short-id> --role assistant    # filter to one role
aj read <session-short-id> --grep "alignment"  # filter messages containing keyword
aj read <session-short-id> --format json       # machine-readable output
```

**Behavior:**

1. Accept 8-char short ID (what `aj sessions` displays) or full UUID
2. Resolve across all agent types automatically — scan Claude, Codex, Factory roots, match on prefix
3. Use `extract_conversation_text()` from `session_io.py` as the base (already exists)
4. `--tail N` shows last N messages (most common use case: reading end of a session to find decisions)
5. `--role` filters to `user` or `assistant` messages only
6. `--grep` filters to messages containing the keyword (case-insensitive), still showing surrounding context
7. Default output: `[timestamp] ROLE: text` format, same as `extract_conversation_text()`
8. Print session metadata header: agent type, start/end time, message count, title

**Implementation notes:**

- Add a `resolve_session_by_prefix()` helper to `session_io.py` that scans all agent types and returns the `SessionInfo`. This is reusable by other commands too.
- The `--grep` flag is distinct from `aj search`: `search` finds sessions matching keywords across the entire history; `read --grep` filters messages within a single known session.

---

## P1-1: Make `aj search` search all agent types by default

**File:** `search.py`, `aj`

**Current:** `search` defaults to `--agent claude`. User sees a session in `aj sessions`, tries to search it, silently gets no results because the session is Codex.

**Fix options (pick one):**

**Option A (preferred): Search all agent types by default.** Remove the `--agent` default. When no `--agent` is specified, iterate over all three agent types and merge results. Add `--agent claude/codex/factory` to restrict to one type.

**Option B: Print a diagnostic when no results found.** If search returns 0 matches and `--agent` was not explicitly provided, print: `"No matches in N claude sessions. Try --agent codex (M sessions) or --agent factory (K sessions)."`

Either option would have caught the problem. Option A is simpler for the user.

**Impact on output:** When searching all agent types, prefix each result block with the resolved agent tag (using the P0-1 fix), so it's clear which agent type each match comes from.

---

## P1-2: Fix `aj ask` import crash

**File:** `ask.py` line 151

**Current:** `from shared.gemini import (` fails with `ModuleNotFoundError: No module named 'shared'`.

**Root cause:** `ask.py` imports from `shared.gemini` but doesn't add the parent directory to `sys.path`. Compare with `search.py` lines 42-43 which do:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
```

**Fix:** Add the same `sys.path` setup to `ask.py` before the `shared.gemini` import. Check `digest.py` and `resume.py` for the same issue.

---

## Validation

After implementing, this sequence should work without errors:

```bash
# Sessions show distinct tags for each agent type
aj sessions --limit 5
# Expected: [C] for claude, [X] for codex, [F] for factory

# Read a specific codex session by short ID
aj read 019d8cfd --tail 20
# Expected: last 20 messages from the codex session

# Search finds results across all agent types
aj search "alignment"
# Expected: results from both claude AND codex sessions

# Ask works without import errors
aj ask "what was decided about cross-SAE alignment?" --raw --recent 3
# Expected: Gemini-powered answer, no ModuleNotFoundError
```

---
name: agent-journal
description: Use when you need to search, summarize, or resume prior Claude, Factory, or Codex sessions with the local agent-journal CLI.
---

# Agent Journal

## Overview

The agent-journal system preserves context across agent sessions. It solves three problems:

1. **Session handoff** -- when a session ends (context limit, crash), nuance is lost.
2. **Discovery** -- past decisions, references, and eureka moments are buried in session files.
3. **State awareness** -- no single view of recent activity across agents and git.

The CLI tool is `ops/agent-journal/aj` in the project root. It has seven subcommands: `search`, `read`, `ask`, `topics`, `digest`, `resume`, `sessions`.

## No Manual Env Setup Required

All `aj` commands auto-load `config.env` (including GEMINI_API_KEY). Just run them directly:

```bash
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "steering"
python3 /home/appuser/hf-workbench/ops/agent-journal/aj digest --project=-home-appuser-hf-workbench --recent 1
```

## Raw session files / `find` (critical)

1. **Prefer CLI over shell:** Use `aj sessions`, `aj digest --project=... --session <id>`, and broader `aj search` / `aj ask` before touching the filesystem.
2. **Do not** run `find` (or similar full-tree walks) on `$HOME`, `/home/appuser`, or arbitrary project trees to locate session JSONL.
3. **Allowed roots only:** If you must locate a transcript path by session id, search **only** under `$HOME/.claude`, `$HOME/.codex`, and `$HOME/.factory` (agent-journal indexes the same trees in `ops/agent-journal/lib/session_io.py`).

```bash
find "$HOME/.claude" "$HOME/.codex" "$HOME/.factory" -path "*<session-id>*" 2>/dev/null | head -20
```

## Search Strategy (READ THIS FIRST)

Effective searching is the #1 skill for using this tool. The Haiku session below illustrates common mistakes:

**How search works:** All keywords must match within a SINGLE message (AND logic). More keywords = fewer results.

**Rules:**
1. **Start with 1-2 keywords**, not 3+. `"steering"` beats `"steering alpha contrastive"`.
2. **Search now defaults to all agent types** (Claude, Codex, Factory). Use `--agent claude` to restrict to one type if needed.
3. **If no results, broaden** -- remove keywords or try different synonyms. Do NOT try git, memory files, or other scattered sources.
4. **Use `--digests` for fast, high-signal results** when digests exist.
5. **Use `aj sessions`** first to see what sessions exist. Tags: `[C]`=Claude, `[X]`=Codex, `[F]`=Factory.

**Bad search flow (what to avoid):**
```
search "droid factory agent journal skill"   # 5 keywords, AND logic = nothing matches
search "build agent journal"                  # still too specific
search "session digest resume gemini"         # random keyword soup
# then thrashing: git log, memory files, SKILL.md, etc.
```

**Good search flow:**
```
aj sessions --limit 10                        # see what exists, note agent types
aj search "journal" --agent factory           # 1 keyword, right agent type
aj search "journal" --agent factory --context 5  # add context for detail
```

## When to Use `search` vs `ask`

- **`search`** = keyword matching. Fast, no LLM cost. Use when you know what words to look for: a model name, a file path, an error message.
- **`ask`** = LLM-powered Q&A. Use for questions that need reasoning across sessions: "what experiment setups did we align with?", "how many times did the server fail?", "what are the common training failures?"

Rule of thumb: if the answer is a specific string you could grep for, use `search`. If you need synthesis or counting or pattern recognition, use `ask`.

## Commands Reference

### 1. `aj search` -- Find past conversations

```bash
# Basic keyword search (default: all agent types)
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "steering"

# Restrict to a single agent type
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "bug" --agent factory
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "bug" --agent codex

# Search with surrounding context lines
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "paper" --context 10

# Time filters
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "experiment" --today
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "model" --last-hours 24

# Search pre-generated digests (fast, high-signal)
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "eureka" --digests

# Correlate matches with git commits
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "steering" --with-git
```

### 2. `aj read` -- Read a session transcript by ID

Read the full transcript of a specific session. Accepts the 8-char short ID shown by `aj sessions` or a full UUID. Auto-detects agent type.

```bash
# Full transcript
python3 /home/appuser/hf-workbench/ops/agent-journal/aj read 019d8cbe

# Last 30 messages (most common: checking end-of-session decisions)
python3 /home/appuser/hf-workbench/ops/agent-journal/aj read 019d8cbe --tail 30

# Filter to assistant messages only
python3 /home/appuser/hf-workbench/ops/agent-journal/aj read 019d8cbe --role assistant

# Filter messages containing a keyword
python3 /home/appuser/hf-workbench/ops/agent-journal/aj read 019d8cbe --grep "alignment"

# JSON output for programmatic use
python3 /home/appuser/hf-workbench/ops/agent-journal/aj read 019d8cbe --format json
```

### 3. `aj ask` -- LLM-powered Q&A over session history

Ask natural-language questions that require reasoning, synthesis, or pattern recognition across sessions.

**Default mode** searches pre-generated digests (fast, compact). Use `--raw` to search raw transcripts (deeper but slower -- does per-session extraction in parallel, then synthesizes).

```bash
# Ask across recent digests (default, fast)
python3 /home/appuser/hf-workbench/ops/agent-journal/aj ask "what experiment setups did we align with from referenced papers?"

# Ask across raw Factory session transcripts
python3 /home/appuser/hf-workbench/ops/agent-journal/aj ask "how many times did the server fail to start?" --raw --agent factory

# Scope to last 24 hours
python3 /home/appuser/hf-workbench/ops/agent-journal/aj ask "what were the key decisions today?" --today

# Ask across last 5 raw sessions
python3 /home/appuser/hf-workbench/ops/agent-journal/aj ask "common training run failures" --raw --recent 5
```

**How it works:**
- Digest mode: sends all digests + question in one Gemini call (digests are compact).
- Raw mode: per-session extraction in parallel (avoids stuffing huge contexts), then a synthesis call to combine extractions. Sessions with no relevant info are skipped.

### 4. `aj topics` -- Extract session topics with LLM

```bash
python3 /home/appuser/hf-workbench/ops/agent-journal/aj topics --recent 5
python3 /home/appuser/hf-workbench/ops/agent-journal/aj topics --agent factory --recent 3
python3 /home/appuser/hf-workbench/ops/agent-journal/aj topics --today
```

### 5. `aj digest` -- Generate structured session summaries

Produces a structured markdown digest using Gemini. Saved to `docs/digests/`.

```bash
# Digest most recent Claude session
python3 /home/appuser/hf-workbench/ops/agent-journal/aj digest --project=-home-appuser-hf-workbench --recent 1

# Digest Factory or Codex sessions
python3 /home/appuser/hf-workbench/ops/agent-journal/aj digest --project=-home-appuser-hf-workbench --agent factory --recent 1
python3 /home/appuser/hf-workbench/ops/agent-journal/aj digest --project=-home-appuser-hf-workbench --agent codex --recent 1

# Digest a specific session by ID
python3 /home/appuser/hf-workbench/ops/agent-journal/aj digest --project=-home-appuser-hf-workbench --session <session-id>

# Dry run -- see stats without calling LLM
python3 /home/appuser/hf-workbench/ops/agent-journal/aj digest --project=-home-appuser-hf-workbench --recent 1 --dry-run
```

### 6. `aj resume` -- Build context for resuming work

```bash
# Morning catchup -- last 12 hours
python3 /home/appuser/hf-workbench/ops/agent-journal/aj resume --overnight

# Resume a specific project
python3 /home/appuser/hf-workbench/ops/agent-journal/aj resume --project proj-2-rl

# Focus on the most recent session only
python3 /home/appuser/hf-workbench/ops/agent-journal/aj resume --last-session

# Custom time window
python3 /home/appuser/hf-workbench/ops/agent-journal/aj resume --since 2026-04-06
```

### 7. `aj sessions` -- List available sessions

```bash
# List recent sessions (all agent types)
python3 /home/appuser/hf-workbench/ops/agent-journal/aj sessions

# List sessions for a specific agent
python3 /home/appuser/hf-workbench/ops/agent-journal/aj sessions --agent factory

# Show more sessions
python3 /home/appuser/hf-workbench/ops/agent-journal/aj sessions --limit 15
```

## Agent Types

| Agent   | Tag  | Flag              | What it is                    |
|---------|------|-------------------|-------------------------------|
| Claude  | `[C]`| `--agent claude`  | Claude Code / CLI sessions    |
| Factory | `[F]`| `--agent factory` | Droid (Factory) sessions      |
| Codex   | `[X]`| `--agent codex`   | Codex sessions                |

**`search` now defaults to all agent types.** Use `--agent <type>` to restrict.

## Workflow Recipes

### Morning catchup

```bash
python3 /home/appuser/hf-workbench/ops/agent-journal/aj resume --overnight
```

### Digest a session that just ended

```bash
python3 /home/appuser/hf-workbench/ops/agent-journal/aj digest --project=-home-appuser-hf-workbench --recent 1
```

### Find a past decision

Start broad, narrow down:

```bash
python3 /home/appuser/hf-workbench/ops/agent-journal/aj sessions --limit 10
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "learning rate" --context 5
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "learning rate" --agent factory
python3 /home/appuser/hf-workbench/ops/agent-journal/aj search "learning rate" --digests
```

### Resume interrupted work

```bash
python3 /home/appuser/hf-workbench/ops/agent-journal/aj digest --project=-home-appuser-hf-workbench --recent 1
python3 /home/appuser/hf-workbench/ops/agent-journal/aj resume --project proj-2-rl --last-session
```

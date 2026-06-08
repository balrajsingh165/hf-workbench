# SOP: Add a New User

## Overview

Adding a user involves two steps: inserting a row into the SQLite database and creating a markdown profile under `users/{id}/`. The DB row is minimal (just identity + timestamp for lookups). All personalization lives in the markdown file — it's the rich context injected into every AI call.

---

## Step 1 — Generate User ID

Use a short, human-readable ID. Format: lowercase alphanumeric, underscores OK, no spaces.

Examples: `alice`, `trader_mike`, `user_003`

---

## Step 2 — Insert into SQLite

Table: `users` (defined in `db/schema.py`)

| Column | Type | Required | Default | Description |
|---|---|---|---|---|
| `id` | TEXT | ✓ | — | Unique user ID |
| `display_name` | TEXT | ✓ | — | Name shown in UI and AI context |
| `created_at` | TEXT | auto | `datetime('now')` | ISO timestamp |

Example:

```python
import sqlite3

conn = sqlite3.connect("db/hf.db")
conn.execute("INSERT INTO users (id, display_name) VALUES (?, ?)", ("alice", "Alice"))
conn.commit()
conn.close()
```

---

## Step 3 — Create User Directory and Profile Markdown

Path: `users/{id}/profile.md`

Create the directory and profile file. The profile markdown is the **single source of truth** for everything the AI needs to know about this user. Fields here will evolve — add new sections freely without touching the DB schema.

### Profile Template

```markdown
# User Profile: {display_name}

## Identity
- **ID**: {id}
- **Experience**: beginner | intermediate | advanced
- **Risk Tolerance**: conservative | moderate | aggressive

## Preferences
- **Asset Classes**: stocks, crypto, forex, commodities
- **Horizon**: short (1–7d) | medium (1–4w) | long (1–3mo)
- **Sectors of Interest**: semiconductors, energy, ...

## Watchlist
- AAPL
- BTC
- NVDA

## Trading Style
<!-- Self-described or AI-observed approach. -->
- Prefers momentum-based entries
- Avoids earnings plays
- Comfortable holding through 5–10% drawdowns

## Goals
<!-- What the user is trying to achieve. Updated as the AI learns more. -->
- Build a concentrated portfolio of 5–8 positions
- Focus on semiconductor supply chain for Q2 2026

## Memory
<!-- Append-only. Written by the AI during interactions. Never overwrite — only append. -->
<!-- Each entry is timestamped. This is how the AI remembers the user across sessions. -->

- [2026-04-23] User mentioned they exited TSMC too early last cycle and wants to hold longer this time.
- [2026-04-23] Prefers thesis framed around supply chain dynamics over pure price action.
- [2026-04-23] Responds well to direct, blunt feedback. Dislikes hedging language.
```

### Data Ownership — No Overlap Except Primary Key

| Data | DB | Markdown | Rule |
|---|---|---|---|
| `id` | ✓ | ✓ | Only field in both — the bridge |
| display_name, created_at | ✓ | — | DB-only |
| Experience, risk tolerance | — | ✓ | Markdown-only |
| Preferences, watchlist | — | ✓ | Markdown-only |
| Trading style, goals | — | ✓ | Markdown-only |
| Memory | — | ✓ | Markdown-only |

The DB exists for lookups and joins. Everything else lives in markdown — it changes frequently, is unstructured, and gets injected directly into AI prompts.

---

## Step 4 — Verify

```bash
# Check DB row
uv run python -c "
import sqlite3
conn = sqlite3.connect('db/hf.db')
row = conn.execute('SELECT * FROM users WHERE id = ?', ('alice',)).fetchone()
print(row)
"

# Check profile
cat users/alice/profile.md
```

---

## Notes

- **Memory** is append-only. The AI writes new entries during interactions. Never overwrite existing entries.
- Thesis files live in `global/theses/`, not under the user directory. Ownership is N:M — a user can own many theses and a thesis can be owned by many users — via the `user_theses` link table (PK `(user_id, thesis_id)`).
- The `users/{id}/` directory contains only `profile.md` for now. Future files (e.g., session logs) can be added here without schema changes.

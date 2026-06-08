"""
HF Workbench DB Schema

Edit the TABLES dict below to change the schema.
Run `python db/schema.py` to recreate the database.

WARNING: The default invocation drops and recreates ALL tables. Fine for
prototyping, but loses expensive-to-rebuild state (embeddings in
thesis_match_chunks / story_match_chunks, and judge output in
thesis_story_links).

To wipe only the tables you are changing, call `init_db(tables=[...])` or run:
    uv run python -c "from db.schema import init_db; init_db(tables=['user_theses'])"
See docs/sop-schema-change.md for the decision tree.
"""

import sqlite3
import os
from collections.abc import Iterable

DB_PATH = os.path.join(os.path.dirname(__file__), "hf.db")

# ── Schema Definition ──────────────────────────────────────────────
# Each table is a list of (column_name, column_type_and_constraints).
# Edit freely — just re-run the script to apply.

TABLES = {
    "users": [
        ("id",            "TEXT PRIMARY KEY"),
        ("display_name",  "TEXT NOT NULL"),
        ("created_at",    "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    "theses": [
        ("id",               "TEXT PRIMARY KEY"),
        ("origin",           "TEXT NOT NULL DEFAULT 'manual'"),
        ("source_context",   "TEXT"),
        ("review_status",    "TEXT NOT NULL DEFAULT 'active'"),
        ("owner_count",      "INTEGER NOT NULL DEFAULT 0"),
        # Intrinsic decay clock for scoring (half_life = horizon_days / 2). A
        # property of the belief, not the holder — every owner shares it.
        # Inferred at creation (never user-entered, never surfaced); the
        # NOT NULL DEFAULT guarantees a thesis is always scoreable.
        ("horizon_days",     "INTEGER NOT NULL DEFAULT 45"),
        # Composite score and its two sub-dimensions. Like horizon_days these
        # are intrinsic to the belief, not the holder: freshness derives from
        # the global thesis_story_links timeline, tailwind from market price
        # action on the tagged tickers — neither has any per-user input, so
        # every owner shares one value. Storing them here (not on user_theses)
        # also lets unowned proposal theses (owner_count=0) carry a score.
        # Written by agents/score_theses.py. NULL until first scored.
        ("score",            "INTEGER"),  # composite 1-100
        ("score_freshness",  "INTEGER"),  # 1-100, decay from last supporting signal
        ("score_tailwind",   "INTEGER"),  # 1-100, market/price agreement with thesis direction
        ("created_at",       "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    "user_theses": [
        ("user_id",          "TEXT NOT NULL REFERENCES users(id)"),
        ("thesis_id",        "TEXT NOT NULL REFERENCES theses(id)"),
        ("status",           "TEXT NOT NULL DEFAULT 'active'"),  # active | stressed | resolved
        # Score lives on `theses` (intrinsic to the belief). This table holds
        # only genuinely per-user state: the lifecycle status and resolution.
        ("created_at",       "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("resolved_at",      "TEXT"),
        ("outcome",          "TEXT"),  # correct | partial | incorrect
        ("PRIMARY KEY (user_id, thesis_id)", ""),
    ],

    # Explicit user watchlist — see docs/design-watchlist.md. Symbols are
    # canonical Yahoo symbols (instruments PK); writes normalize through the
    # alias-aware resolve gate in src/personalization/watchlist.py. The
    # composite PK is the dedup constraint and serves the user_id lookup.
    "user_watchlist": [
        ("user_id",    "TEXT NOT NULL REFERENCES users(id)"),
        ("symbol",     "TEXT NOT NULL REFERENCES instruments(symbol)"),
        ("added_at",   "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("PRIMARY KEY (user_id, symbol)", ""),
    ],

    # Daily score snapshot — drives day-over-day deltas in the digest / UI.
    # Per-thesis (not per-user): the score is intrinsic to the thesis, so the
    # snapshot is too. Idempotent same-day overwrite via PK + ON CONFLICT in
    # agents/score_theses.py.
    "thesis_snapshots": [
        ("thesis_id",        "TEXT NOT NULL REFERENCES theses(id) ON DELETE CASCADE"),
        ("snapshot_date",    "TEXT NOT NULL"),                       # ISO YYYY-MM-DD
        ("score",            "INTEGER"),
        ("score_freshness",  "INTEGER"),
        ("score_tailwind",   "INTEGER"),
        ("created_at",       "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("PRIMARY KEY (thesis_id, snapshot_date)", ""),
    ],

    # Raw news items (one per fetched URL). Synthesis output lives on
    # `story`; news rows are the firehose feed source and the citation
    # targets for story.overview bullets.
    "news": [
        ("id",                 "TEXT PRIMARY KEY"),
        ("sources_json",       "TEXT DEFAULT '[]'"),
        ("sectors_json",       "TEXT DEFAULT '[]'"),
        ("regions_json",       "TEXT DEFAULT '[]'"),
        ("published_at",       "TEXT"),
        ("created_at",         "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("headline",           "TEXT"),
        ("body_excerpt",       "TEXT"),                           # ~1000 chars
        ("source_url",         "TEXT"),                           # canonical link; firehose dedup key
        ("publisher",          "TEXT"),                           # 'prnewswire' | 'globenewswire' | ...
        ("materiality_score",  "INTEGER"),                        # 0-100
        ("event_classes",      "TEXT NOT NULL DEFAULT '[]'"),     # JSON list of matched scorer labels
        ("cluster_id",         "TEXT"),
        ("headline_hash",      "TEXT"),
        ("embedding",          "BLOB"),
        ("event_class",        "TEXT"),
    ],

    "news_cluster": [
        ("id",                          "TEXT PRIMARY KEY"),
        ("created_at",                  "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("updated_at",                  "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("status",                      "TEXT NOT NULL"),
        ("centroid_news_id",            "TEXT"),
        ("centroid_json",               "TEXT"),
        ("headline_norm",               "TEXT NOT NULL"),
        ("event_class",                 "TEXT"),
        ("max_materiality",             "INTEGER NOT NULL DEFAULT 0"),
        ("member_count",                "INTEGER NOT NULL DEFAULT 0"),
        ("independent_pub_count",       "INTEGER NOT NULL DEFAULT 0"),
        ("has_tier1_primary",           "INTEGER NOT NULL DEFAULT 0"),
        ("has_institutional_primary",   "INTEGER NOT NULL DEFAULT 0"),
        ("tickers_json",                "TEXT NOT NULL DEFAULT '[]'"),
        ("sectors_json",                "TEXT NOT NULL DEFAULT '[]'"),
        ("regions_json",                "TEXT NOT NULL DEFAULT '[]'"),
        ("sector_confidence",           "REAL DEFAULT 0.0"),
        ("region_confidence",           "REAL DEFAULT 0.0"),
        ("first_seen_at",               "TEXT NOT NULL"),
        ("last_seen_at",                "TEXT NOT NULL"),
    ],

    "news_cluster_member": [
        ("cluster_id",  "TEXT NOT NULL REFERENCES news_cluster(id)"),
        ("news_id",     "TEXT NOT NULL REFERENCES news(id)"),
        ("attached_at", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("similarity",  "REAL"),
        ("attach_pass", "TEXT NOT NULL"),
        ("PRIMARY KEY (cluster_id, news_id)", ""),
    ],

    "news_cluster_dropped": [
        ("cluster_id", "TEXT PRIMARY KEY REFERENCES news_cluster(id)"),
        ("dropped_at", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("reason",     "TEXT NOT NULL"),
    ],

    "story": [
        ("id",                    "TEXT PRIMARY KEY"),
        ("cluster_id",            "TEXT UNIQUE REFERENCES news_cluster(id)"),
        ("centroid_news_id",       "TEXT REFERENCES news(id)"),
        ("created_at",            "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("headline",              "TEXT NOT NULL"),
        ("what_changed",          "TEXT"),
        ("overview_json",         "TEXT NOT NULL"),
        ("claims_json",           "TEXT NOT NULL DEFAULT '[]'"),
        ("quotes_json",           "TEXT NOT NULL DEFAULT '[]'"),
        ("market_relevance_json",  "TEXT NOT NULL"),
        ("open_questions_json",    "TEXT NOT NULL DEFAULT '[]'"),
        ("sectors_json",          "TEXT NOT NULL DEFAULT '[]'"),
        ("regions_json",          "TEXT NOT NULL DEFAULT '[]'"),
        ("theme_tag",             "TEXT NOT NULL DEFAULT 'other'"),
        ("images_json",           "TEXT NOT NULL DEFAULT '[]'"),
        ("kind",                  "TEXT NOT NULL DEFAULT 'story'"),
        ("heat",                  "INTEGER"),
        ("social_json",           "TEXT"),
    ],

    "entity_tickers": [
        ("entity_type",  "TEXT NOT NULL"),           # thesis | news | story
        ("entity_id",    "TEXT NOT NULL"),            # FK-ish id; polymorphic
        ("symbol",       "TEXT NOT NULL"),            # canonical Yahoo symbol, always UPPER
        ("direction",    "TEXT"),                     # bullish | bearish | NULL (news has no direction)
        ("PRIMARY KEY (entity_type, entity_id, symbol)", ""),
    ],

    # Instrument registry — see docs/design-instrument-registry.md
    # Yahoo's symbol is the canonical id; this table is the metadata sidecar.
    "instruments": [
        ("symbol",            "TEXT PRIMARY KEY"),                 # Yahoo: 'AAPL', 'BZ=F', '^TNX', '005930.KS', 'BTC-USD'
        ("display",           "TEXT NOT NULL"),                    # 'Brent Crude'
        ("short",             "TEXT NOT NULL"),                    # 'Brent'
        ("asset_class",       "TEXT NOT NULL"),                    # equity | equity_index | fx | rate | commodity | vol | crypto | etf
        ("aliases_json",      "TEXT NOT NULL DEFAULT '[]'"),       # forward-looking: NER + chat lookup
        ("canonical_symbol",  "TEXT"),                             # alias rows point at the canonical Yahoo symbol; NULL = self
        ("eodhd_symbol",      "TEXT"),                             # explicit override; NULL = derive from Yahoo via suffix rules
        ("alpaca_symbol",     "TEXT"),                             # kept for reference; Alpaca no longer used on price-display surface
        ("fmp_symbol",        "TEXT"),                             # 'BZUSD' when Yahoo is 'BZ=F'; NULL = same as symbol
        ("tradable",          "INTEGER NOT NULL DEFAULT 1"),       # 0 for private companies / non-tradable concepts
        ("proxy_for",         "TEXT"),                             # nullable; e.g. TLT.proxy_for = '^TNX'
        ("region",            "TEXT"),
        ("active",            "INTEGER NOT NULL DEFAULT 1"),
        ("updated_at",        "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    "thesis_match_chunks": [
        ("id",              "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("thesis_id",       "TEXT NOT NULL REFERENCES theses(id)"),
        ("chunk_key",       "TEXT NOT NULL"),
        ("chunk_kind",      "TEXT NOT NULL"),
        ("chunk_text",      "TEXT NOT NULL"),
        ("tickers_json",    "TEXT NOT NULL DEFAULT '[]'"),
        ("sectors_json",    "TEXT NOT NULL DEFAULT '[]'"),
        ("embedding_model", "TEXT NOT NULL"),
        ("embedding_json",  "TEXT NOT NULL"),
        ("updated_at",      "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    "story_match_chunks": [
        ("story_id",        "TEXT PRIMARY KEY REFERENCES story(id)"),
        ("chunk_text",      "TEXT NOT NULL"),
        ("embedding_model", "TEXT NOT NULL"),
        ("embedding_json",  "TEXT NOT NULL"),
        ("updated_at",      "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    "thesis_story_links": [
        ("thesis_id",            "TEXT NOT NULL REFERENCES theses(id)"),
        ("story_id",             "TEXT NOT NULL REFERENCES story(id)"),
        ("relation",             "TEXT NOT NULL"),  # supports | stresses
        ("confidence",           "REAL NOT NULL"),
        ("matched_invalidation", "TEXT"),
        ("rationale",            "TEXT NOT NULL"),
        ("retrieval_score",      "REAL NOT NULL"),
        ("best_chunk_key",       "TEXT NOT NULL"),
        ("source",               "TEXT NOT NULL"),  # ingest | backfill
        ("updated_at",           "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),  # refreshed on conflict; judgment freshness
        ("PRIMARY KEY (thesis_id, story_id)", ""),
    ],

    # Daily Market Brief — see docs/plan-daily-brief.md
    "daily_briefs": [
        ("brief_date",       "TEXT PRIMARY KEY"),                # '2026-04-24'
        ("generated_at",     "TEXT NOT NULL"),                   # ISO timestamp
        ("themes_json",      "TEXT NOT NULL"),                   # [{id, text, source_story_ids:[...]}, ...]
        ("source_story_ids", "TEXT NOT NULL DEFAULT '[]'"),      # union of story IDs cited across themes
        ("model_version",    "TEXT NOT NULL"),                   # e.g. 'gemini-3.1-pro-preview'
    ],

    "daily_movers": [
        ("brief_date",  "TEXT NOT NULL"),
        ("rank",        "INTEGER NOT NULL"),                    # 1..8, editorial order
        ("symbol",      "TEXT NOT NULL"),                       # canonical Yahoo symbol
        ("label",       "TEXT NOT NULL"),                       # 'S&P 500', 'Brent Crude', ...
        ("asset_class", "TEXT NOT NULL"),                       # equity_index | rate | fx | commodity | vol
        ("pct_change",  "REAL"),                                # signed daily %
        ("price",       "REAL"),                                # last price (nullable)
        ("PRIMARY KEY (brief_date, symbol)", ""),
    ],

    "agent_sessions": [
        ("session_id",    "TEXT PRIMARY KEY"),                   # platform:user_id:short_session_id
        ("platform",      "TEXT NOT NULL"),
        ("user_id",       "TEXT NOT NULL"),
        ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("created_at",    "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("updated_at",    "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    "agent_messages": [
        ("id",           "TEXT PRIMARY KEY"),
        ("session_id",   "TEXT NOT NULL REFERENCES agent_sessions(session_id) ON DELETE CASCADE"),
        ("role",         "TEXT NOT NULL"),                      # user | assistant | system
        ("content_text", "TEXT NOT NULL DEFAULT ''"),
        ("parts_json",   "TEXT NOT NULL DEFAULT '[]'"),
        ("created_at",   "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    "chat_titles": [
        ("session_id", "TEXT PRIMARY KEY REFERENCES agent_sessions(session_id) ON DELETE CASCADE"),
        ("platform",   "TEXT NOT NULL"),
        ("user_id",    "TEXT NOT NULL"),
        ("title",      "TEXT NOT NULL"),
        ("created_at", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("updated_at", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    # Symbols emitted by ingest paths (firehose tag gate, thesis discovery)
    # that aren't in `instruments` yet. Reviewed weekly — real ones get
    # promoted into `seed.py`, garbage gets a 0 in `keep`.
    "pending_instruments": [
        ("symbol",        "TEXT NOT NULL"),
        ("source",        "TEXT NOT NULL"),                     # 'firehose' | 'discover' | ...
        ("source_id",     "TEXT"),                              # entity id (news_NNN, thesis_NNN, ...) — nullable
        ("first_seen_at", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("last_seen_at",  "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("seen_count",    "INTEGER NOT NULL DEFAULT 1"),
        ("keep",          "INTEGER"),                           # 1=adopt, 0=reject, NULL=unreviewed
        ("PRIMARY KEY (symbol, source)", ""),
    ],

    # Daily trending-ticker snapshot (1ms.news ranking). One row per
    # (day, source, symbol). Three uses, one table: (1) input to the
    # two-tier scrape cadence — effective_rank = min(rank across the two
    # most-recent snapshots) gives the one-day residue; (2) observability
    # record for the trending ingest lane; (3) data source for the
    # homepage trending surface. Written by agents/trending.py.
    "ticker_trends": [
        ("snapshot_date",   "TEXT NOT NULL"),                  # 'YYYY-MM-DD'
        ("source",          "TEXT NOT NULL"),                  # '1ms_stocks'
        ("symbol",          "TEXT NOT NULL"),                  # resolved canonical registry symbol (UPPER)
        ("raw_symbol",      "TEXT NOT NULL"),                  # as scraped, pre-resolution
        ("rank",            "INTEGER NOT NULL"),
        ("mentions_24h",    "INTEGER"),
        ("mentions_delta",  "INTEGER"),                        # velocity — the leading signal
        ("upvotes",         "INTEGER"),
        ("rank_trend",      "INTEGER"),                        # +N up / -N down / 0 same / NULL new
        ("in_registry",     "INTEGER NOT NULL DEFAULT 0"),     # 1 when symbol resolves in instruments
        ("created_at",      "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("PRIMARY KEY (snapshot_date, source, symbol)", ""),
    ],

    "story_quality_label": [
        ("story_id",            "TEXT NOT NULL REFERENCES story(id)"),
        ("labeler",             "TEXT NOT NULL"),
        ("label",               "TEXT NOT NULL"),                            # good | unclear | no_value
        ("rationale",           "TEXT"),
        ("labeled_at",          "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("PRIMARY KEY (story_id, labeler)", ""),
    ],

    "story_synth_rejected": [
        ("cluster_id",             "TEXT NOT NULL"),
        ("rejected_at",            "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("reason",                 "TEXT NOT NULL"),
        ("payload_json",           "TEXT"),
        # `member_count` snapshot at rejection time. The candidate selector
        # excludes a cluster after ≥2 rejections at the same member_count —
        # i.e. nothing new has joined the cluster since synthesis last failed.
        # Reactivates the moment a new member is attached.
        ("member_count_at_reject", "INTEGER"),
    ],

    "llm_calls": [
        ("id",              "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("entity_type",     "TEXT NOT NULL"),
        ("entity_id",       "TEXT NOT NULL"),
        ("caller",          "TEXT NOT NULL"),
        ("model_id",        "TEXT NOT NULL"),
        ("latency_seconds", "REAL"),
        ("input_tokens",       "INTEGER NOT NULL DEFAULT 0"),
        ("output_tokens",      "INTEGER NOT NULL DEFAULT 0"),
        ("thinking_tokens",    "INTEGER NOT NULL DEFAULT 0"),
        ("cache_read_tokens",  "INTEGER NOT NULL DEFAULT 0"),
        ("total_tokens",       "INTEGER NOT NULL DEFAULT 0"),
        ("cost_usd",        "REAL NOT NULL DEFAULT 0"),
        ("created_at",      "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    "shared_chats": [
        ("share_id",         "TEXT PRIMARY KEY"),
        ("session_id",       "TEXT NOT NULL UNIQUE REFERENCES agent_sessions(session_id) ON DELETE CASCADE"),
        ("is_public",        "INTEGER NOT NULL DEFAULT 0"),
        ("preview_question", "TEXT"),
        ("created_at",       "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ("updated_at",       "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],

    # Per-phase token + cost record for every agent turn.
    # One 'aggregate' row per request denormalizes the sum for fast SUMs.
    # Source of truth for the credits/billing system (docs/design-billing-credits.md).
    "agent_usage": [
        ("id",                  "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("request_id",          "TEXT NOT NULL"),                        # also the Langfuse trace id
        ("user_id",             "TEXT NOT NULL"),
        ("session_id",          "TEXT"),
        ("endpoint",            "TEXT NOT NULL"),                        # 'chat' | chip name | ...
        ("model_id",            "TEXT NOT NULL"),
        ("phase",               "TEXT NOT NULL"),                        # 'research' | 'response' | 'chart' | 'aggregate'
        ("input_tokens",        "INTEGER NOT NULL DEFAULT 0"),
        ("output_tokens",       "INTEGER NOT NULL DEFAULT 0"),
        ("cache_read_tokens",   "INTEGER NOT NULL DEFAULT 0"),
        ("cache_write_tokens",  "INTEGER NOT NULL DEFAULT 0"),
        ("cost_usd",            "REAL NOT NULL DEFAULT 0"),
        ("latency_ms",          "INTEGER"),
        ("status",              "TEXT NOT NULL DEFAULT 'ok'"),           # 'ok' | 'error' | 'cancelled'
        ("created_at",          "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],
    # One row per AgentCore Code Interpreter phase (today: the Phase 2b chart
    # agent — `purpose` reserves space for a future research-phase analysis use).
    # Run stats only; token/cost for the same run lives in agent_usage (phase='chart')
    # joined by request_id. See docs/agent-observability.md.
    "code_interpreter_runs": [
        ("id",             "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("request_id",     "TEXT NOT NULL"),                        # also the Langfuse trace id
        ("user_id",        "TEXT NOT NULL"),
        ("session_id",     "TEXT"),
        ("purpose",        "TEXT NOT NULL DEFAULT 'chart'"),        # 'chart' | future 'analysis'
        ("outcome",        "TEXT NOT NULL"),                        # 'plot' | 'skip' | 'error' | 'timeout' | 'unknown'
        ("failure_stage",  "TEXT"),                                 # 'init'|'agent'|'image_fetch'|'upload'|'r2' (null on plot/skip)
        ("skip_reason",    "TEXT"),                                 # set when outcome='skip'
        ("execute_count",  "INTEGER NOT NULL DEFAULT 0"),           # sandbox executeCode actions
        ("write_count",    "INTEGER NOT NULL DEFAULT 0"),           # sandbox writeFiles actions
        ("image_bytes",    "INTEGER"),                              # rendered PNG size, set on plot
        ("elapsed_ms",     "INTEGER NOT NULL DEFAULT 0"),
        ("model_id",       "TEXT NOT NULL"),
        ("created_at",     "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
    ],
}

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_user_theses_user "
    "ON user_theses (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_theses_thesis "
    "ON user_theses (thesis_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thesis_match_chunks_thesis_chunk "
    "ON thesis_match_chunks (thesis_id, chunk_key)",
    "CREATE INDEX IF NOT EXISTS idx_thesis_story_links_thesis "
    "ON thesis_story_links (thesis_id)",
    "CREATE INDEX IF NOT EXISTS idx_thesis_story_links_story "
    "ON thesis_story_links (story_id)",
    "CREATE INDEX IF NOT EXISTS idx_daily_movers_brief "
    "ON daily_movers (brief_date, rank)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_source_url "
    "ON news (source_url) WHERE source_url IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_news_headline_hash "
    "ON news (headline_hash)",
    "CREATE INDEX IF NOT EXISTS idx_news_cluster "
    "ON news (cluster_id)",
    "CREATE INDEX IF NOT EXISTS idx_cluster_sectors "
    "ON news_cluster (sectors_json)",
    "CREATE INDEX IF NOT EXISTS idx_cluster_regions "
    "ON news_cluster (regions_json)",
    "CREATE INDEX IF NOT EXISTS idx_cluster_status "
    "ON news_cluster (status, last_seen_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cluster_dropped_time "
    "ON news_cluster_dropped (dropped_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_story_cluster "
    "ON story (cluster_id)",
    "CREATE INDEX IF NOT EXISTS idx_story_kind_created "
    "ON story (kind, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_story_sectors "
    "ON story (sectors_json)",
    "CREATE INDEX IF NOT EXISTS idx_story_regions "
    "ON story (regions_json)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_entity "
    "ON llm_calls (entity_type, entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_created "
    "ON llm_calls (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_entity_tickers_symbol "
    "ON entity_tickers (symbol)",
    "CREATE INDEX IF NOT EXISTS idx_entity_tickers_entity "
    "ON entity_tickers (entity_type, entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_instruments_class "
    "ON instruments (asset_class)",
    "CREATE INDEX IF NOT EXISTS idx_instruments_tradable "
    "ON instruments (tradable)",
    "CREATE INDEX IF NOT EXISTS idx_agent_sessions_user "
    "ON agent_sessions (platform, user_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_messages_session "
    "ON agent_messages (session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_shared_chats_session "
    "ON shared_chats (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_pending_instruments_unreviewed "
    "ON pending_instruments (keep, last_seen_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_news_materiality_published "
    "ON news (materiality_score, published_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_usage_request "
    "ON agent_usage (request_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_usage_user_time "
    "ON agent_usage (user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_usage_time "
    "ON agent_usage (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_usage_model_time "
    "ON agent_usage (model_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_synth_rejected_cluster_count "
    "ON story_synth_rejected (cluster_id, member_count_at_reject)",
    # Trending tiering + discover join: pick the two most-recent snapshot
    # dates per source, then look up symbols within them.
    "CREATE INDEX IF NOT EXISTS idx_ticker_trends_source_date "
    "ON ticker_trends (source, snapshot_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ticker_trends_symbol "
    "ON ticker_trends (symbol, source, snapshot_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ci_runs_time "
    "ON code_interpreter_runs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ci_runs_request "
    "ON code_interpreter_runs (request_id)",
    "CREATE INDEX IF NOT EXISTS idx_ci_runs_outcome_time "
    "ON code_interpreter_runs (outcome, created_at DESC)",
)

def init_db(db_path=DB_PATH, tables: Iterable[str] | None = None):
    """Recreate tables.

    tables=None (default) drops and recreates every table in TABLES — wipes
    all data, including embeddings and judge output.

    tables=['user_theses', ...] drops and recreates only the named tables,
    preserving everything else. Use this when iterating on a single table's
    schema. Indexes are always (re)created idempotently via CREATE INDEX
    IF NOT EXISTS.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    all_tables = list(TABLES.keys())
    if tables is None:
        target_tables = all_tables
        # Clean up legacy virtual tables from earlier schema revisions only
        # on a full rebuild.
        cur.execute("DROP TABLE IF EXISTS thesis_match_fts")
    else:
        target_tables = [t for t in tables]
        unknown = [t for t in target_tables if t not in TABLES]
        if unknown:
            raise ValueError(
                f"Unknown tables requested: {unknown}. "
                f"Valid options: {all_tables}"
            )
        # Preserve schema iteration order for FK safety.
        target_tables = [t for t in all_tables if t in target_tables]

    # FK-safe drop (reverse schema order).
    for table in reversed(target_tables):
        cur.execute(f"DROP TABLE IF EXISTS {table}")

    # Create tables in schema order.
    for table in target_tables:
        cols = ", ".join(
            f"{name} {spec}".rstrip()
            for name, spec in TABLES[table]
        )
        cur.execute(f"CREATE TABLE {table} ({cols})")
        print(f"  ✓ {table} ({len(TABLES[table])} columns)")

    # Indexes are idempotent — always safe to re-run.
    for ddl in INDEXES:
        cur.execute(ddl)

    conn.commit()
    conn.close()
    scope = "all tables" if tables is None else f"{len(target_tables)} table(s): {target_tables}"
    print(f"\nDB ready at {db_path} ({scope})")


if __name__ == "__main__":
    print("Initializing HF database...\n")
    init_db()

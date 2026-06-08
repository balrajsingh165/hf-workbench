"""Daily Market Brief subpackage.

See `docs/plan-daily-brief.md`. The pipeline is three stages:
  1. fetch raw inputs (news, movers, yesterday's themes)
  2. synthesize themes + sentiment via one LLM call
  3. verify provenance → persist to DB + markdown archive

Public entrypoints live in `pipeline.py`. `ranking.py` holds the DB-only
queries that drive the homepage thesis column.
"""

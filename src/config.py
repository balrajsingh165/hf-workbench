"""Centralized environment configuration.

This module is the only place that loads environment variables for shared
service clients. It reads from ~/.env, then the repo .env, and exposes the
canonical values.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


ENV_FILE = Path.home() / ".env"
REPO_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

# Do not override explicitly exported shell variables; ~/.env is the fallback,
# then the repo .env (some keys live only there — e.g. XAI_API_KEY — and the
# pipeline/CLI processes don't go through app.py's own load_dotenv).
load_dotenv(ENV_FILE)
load_dotenv(REPO_ENV_FILE)

EXA_API_KEY = os.getenv("EXA_API_KEY")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
HEURIST_API_KEY = os.getenv("HEURIST_API_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")
WEB_SEARCH_PROVIDER = os.getenv("WEB_SEARCH_PROVIDER", "firecrawl").strip().lower()
HEURIST_MESH_API_ENDPOINT = os.getenv("MESH_API_ENDPOINT", "https://mesh.heurist.xyz")
HEURIST_MESH_SCHEMA_ENDPOINT = os.getenv(
    "MESH_SCHEMA_ENDPOINT",
    f"{HEURIST_MESH_API_ENDPOINT.rstrip('/')}/mesh_schema",
)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET")
ALPACA_DATA_BASE = os.getenv("ALPACA_DATA_BASE", "https://data.alpaca.markets")
ALPACA_STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "iex").strip().lower()

EODHD_API_KEYS = os.getenv("EODHD_API_KEYS", "")

DEFAULT_RSS_FEEDS = (
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.theguardian.com/world/rss",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
)


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# When true, every cluster admitted for synthesis runs Firecrawl on thin
# members even if another member already has a long body (RSS rescue path).
HF_ENRICH_ALL_PROMOTES = _env_truthy("HF_ENRICH_ALL_PROMOTES", default=True)


def require_env(name: str, value: str | None) -> str:
    if value and value.strip():
        return value
    raise RuntimeError(f"Missing required environment variable: {name}. Set it in {ENV_FILE}.")


__all__ = [
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "ALPACA_DATA_BASE",
    "ALPACA_STOCK_FEED",
    "DEFAULT_RSS_FEEDS",
    "EODHD_API_KEYS",
    "ENV_FILE",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "HF_ENRICH_ALL_PROMOTES",
    "GEMINI_API_KEY",
    "HEURIST_API_KEY",
    "HEURIST_MESH_API_ENDPOINT",
    "HEURIST_MESH_SCHEMA_ENDPOINT",
    "WEB_SEARCH_PROVIDER",
    "XAI_API_KEY",
    "require_env",
]

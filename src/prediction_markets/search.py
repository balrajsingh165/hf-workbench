"""Semantic search over Polymarket open markets.

Flow:
  1. Fetch all open markets with volume >= min_volume_usd from Polymarket.
  2. Embed each market's question/title text via Gemini.
  3. Cache markets + embeddings to db/pm_cache.json with a TTL.
  4. On query: embed the query, cosine-rank against cached embeddings,
     return top-k above a similarity floor, sorted by volume (descending).

This module exposes the whole workflow as a function — `find_markets()` for
free-text and `find_markets_for_article()` for thesis/news ids. There is no
agent loop; semantic search + ranking is a deterministic pipeline.

Cache lives at db/pm_cache.json.  Regenerated when:
  - The file is missing.
  - The file is older than cache_ttl_hours.
  - refresh=True is passed explicitly.

File I/O is delegated to `src.cache` so this module owns only the
payload schema (markets + embeddings + invalidation metadata).

Note on LLM re-ranking:
  An earlier iteration ran a Gemini batched relevance judge over 20 candidates
  at a relaxed 0.45 cosine floor.  An eval over the live thesis/news corpus
  (eval/relevance_labels.json) showed the rerank mode underperformed the
  dense-only path: 52% vs 92% average precision@5.  Mechanism: lowering the
  floor pulled in thematic adjacents (e.g. "DeepSeek best AI model" for an
  NVIDIA-chip news article) that the judge accepted, then volume-sort pushed
  the real matches out of top-5.  Removed; re-add only with better evidence.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.cache import age_hours, load_json, now_iso, save_json
from src.clients.gemini import (
    GEMINI_EMBEDDING_2_PREVIEW,
    batch_embed_contents,
    embed_content,
)
from src.clients.polymarket import PolymarketMarket
from src.clients.polymarket import fetch_open_markets as fetch_polymarket


EMBEDDING_MODEL = GEMINI_EMBEDDING_2_PREVIEW
EMBEDDING_DIM = 1536

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_PATH = ROOT / "db" / "pm_cache.json"

DEFAULT_MIN_SIMILARITY = 0.70

Source = Literal["polymarket"]

# How many candidates to consider for volume ranking.  Floor avoids degenerate
# tie-breaking when top_k is tiny; multiplier keeps the volume sort confined to
# the semantically strongest pool.
_SEMANTIC_CANDIDATE_MIN_K = 20
_SEMANTIC_CANDIDATE_MULTIPLIER = 3


@dataclass(slots=True)
class PredictionMarket:
    id: str
    source: Source
    question: str        # normalised question/title for display
    embed_text: str      # text actually embedded (question + description/subtitle)
    volume_usd: float
    url: str
    closes_at: str | None
    embedding: list[float]


@dataclass(slots=True)
class MarketMatch:
    market: PredictionMarket
    similarity: float

    @property
    def volume_usd(self) -> float:
        return self.market.volume_usd


# ── Cache I/O ──────────────────────────────────────────────────────────────


def _load_cache(cache_path: Path) -> list[PredictionMarket] | None:
    """Reconstruct PredictionMarket dataclasses from the cache file."""
    data = load_json(cache_path)
    if data is None:
        return None
    markets = data.get("markets")
    if not isinstance(markets, list):
        return None
    out: list[PredictionMarket] = []
    for m in markets:
        try:
            out.append(
                PredictionMarket(
                    id=m["id"],
                    source=m["source"],
                    question=m["question"],
                    embed_text=m["embed_text"],
                    volume_usd=float(m["volume_usd"]),
                    url=m["url"],
                    closes_at=m.get("closes_at"),
                    embedding=m["embedding"],
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out if out else None


def _save_cache(
    cache_path: Path,
    markets: list[PredictionMarket],
    sources: tuple[Source, ...],
    min_volume_usd: float,
) -> None:
    payload = {
        "fetched_at": now_iso(),
        "sources": sorted(set(sources)),                        # attempted — used for cache invalidation
        "sources_fetched": sorted({m.source for m in markets}),  # actually contributed markets
        "min_volume_usd": min_volume_usd,
        "count": len(markets),
        "markets": [
            {
                "id": m.id,
                "source": m.source,
                "question": m.question,
                "embed_text": m.embed_text,
                "volume_usd": m.volume_usd,
                "url": m.url,
                "closes_at": m.closes_at,
                "embedding": m.embedding,
            }
            for m in markets
        ],
    }
    save_json(cache_path, payload)


# ── Article → query ────────────────────────────────────────────────────────


def _thesis_query(thesis_id: str, root: Path = ROOT) -> str:
    from src.thesis.docs import parse_thesis_markdown

    path = root / "global" / "theses" / f"{thesis_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"Thesis not found: {path}")
    doc = parse_thesis_markdown(path)
    query = f"{doc.title}\n\n{doc.core_thesis}".strip()
    if doc.tickers:
        query += "\n\nTickers: " + " ".join(doc.tickers)
    return query


def _story_query(story_ref: str, root: Path = ROOT) -> str:
    from src.story.docs import parse_story_markdown

    candidate = Path(story_ref)
    if candidate.is_absolute() or candidate.exists():
        path = candidate
    else:
        path = root / "global" / "stories" / f"{story_ref}.md"
    if not path.exists():
        raise FileNotFoundError(f"Story not found: {path}")
    return parse_story_markdown(path).query_text


# ── Fetch + embed ──────────────────────────────────────────────────────────

def _poly_to_pm(m: PolymarketMarket) -> PredictionMarket:
    return PredictionMarket(
        id=f"polymarket:{m.id or m.slug}",
        source="polymarket",
        question=m.question,
        embed_text=m.embed_text,
        volume_usd=m.volume_usd,
        url=m.url,
        closes_at=m.closes_at,
        embedding=[],
    )


def _fetch_and_embed(
    sources: tuple[Source, ...],
    min_volume_usd: float,
) -> list[PredictionMarket]:
    raw: list[PredictionMarket] = []

    if "polymarket" in sources:
        try:
            markets = fetch_polymarket(min_volume_usd)
            raw.extend(_poly_to_pm(m) for m in markets)
            print(
                f"pm-search: polymarket {len(markets)} markets (vol>={min_volume_usd:,.0f})",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"pm-search: polymarket fetch failed: {exc}", file=sys.stderr)

    if not raw:
        return []

    # Embed all texts in parallel.
    text_batches = [[m.embed_text] for m in raw]
    batch_results = batch_embed_contents(
        text_batches,
        model=EMBEDDING_MODEL,
        output_dimensionality=EMBEDDING_DIM,
        task_type="RETRIEVAL_DOCUMENT",
        max_workers=8,
    )
    embeddings = [
        emb
        for br in batch_results
        for emb in br.embeddings
    ]

    for market, embedding in zip(raw, embeddings, strict=True):
        market.embedding = embedding

    return raw


# ── Similarity ─────────────────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _semantic_candidate_limit(top_k: int) -> int:
    return max(_SEMANTIC_CANDIDATE_MIN_K, top_k * _SEMANTIC_CANDIDATE_MULTIPLIER)


# Back-compat alias for tests that import the private symbol by its old name.
_cache_age_hours = age_hours


# ── Public API ─────────────────────────────────────────────────────────────

def find_markets(
    query_text: str,
    *,
    sources: tuple[Source, ...] = ("polymarket",),
    min_volume_usd: float = 50_000,
    top_k: int = 10,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    cache_ttl_hours: float = 1.0,
    refresh: bool = False,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> list[MarketMatch]:
    """Return the top-k related open markets, ranked by volume.

    Matching uses cosine similarity between the query embedding and each
    market's question/title embedding (Gemini 1536-dim). Only markets with
    similarity >= min_similarity are returned; among those, top candidates by
    similarity are then sorted by volume (descending) and the top-k returned.
    """
    # --- resolve cache ---
    markets: list[PredictionMarket] | None = None
    if not refresh:
        meta = load_json(cache_path)
        if meta is not None:
            age = age_hours(meta.get("fetched_at"))
            cached_sources = set(meta.get("sources", []))
            try:
                cached_min_vol = float(meta.get("min_volume_usd", float("inf")))
            except (TypeError, ValueError):
                cached_min_vol = float("inf")
            # Cache is valid only if it covers the requested sources AND was built
            # with a volume floor <= the current request (so no markets were excluded
            # that we'd now want to include).
            cache_ok = (
                age <= cache_ttl_hours
                and set(sources) <= cached_sources
                and cached_min_vol <= min_volume_usd
            )
            if cache_ok:
                markets = _load_cache(cache_path)
                if markets:
                    fetched_raw = meta.get("sources_fetched")
                    if isinstance(fetched_raw, list):
                        missing = set(sources) - set(fetched_raw)
                        if missing:
                            print(
                                f"pm-search: note — {sorted(missing)} returned 0 markets "
                                f"when cache was built",
                                file=sys.stderr,
                            )
                    print(
                        f"pm-search: loaded {len(markets)} markets from cache "
                        f"(age {age:.1f}h)",
                        file=sys.stderr,
                    )

    if markets is None:
        print("pm-search: refreshing market cache…", file=sys.stderr)
        markets = _fetch_and_embed(sources, min_volume_usd)
        if markets:
            _save_cache(cache_path, markets, sources, min_volume_usd)
            print(f"pm-search: cached {len(markets)} markets", file=sys.stderr)

    if not markets:
        return []

    # Filter to the requested sources and volume floor.
    pool = [
        m for m in markets
        if m.source in sources and m.volume_usd >= min_volume_usd
    ]

    # Embed query.
    query_emb = embed_content(
        query_text,
        model=EMBEDDING_MODEL,
        output_dimensionality=EMBEDDING_DIM,
        task_type="RETRIEVAL_QUERY",
    ).embeddings[0]

    # Pick semantically strongest candidates first; volume ranking happens only
    # inside that related set, so huge borderline markets cannot dominate.
    semantic_pool_size = _semantic_candidate_limit(top_k)
    scored = [
        MarketMatch(market=m, similarity=_cosine(query_emb, m.embedding))
        for m in pool
        if m.embedding
    ]
    above = [r for r in scored if r.similarity >= min_similarity]
    semantic_candidates = sorted(
        above,
        key=lambda r: r.similarity,
        reverse=True,
    )[:semantic_pool_size]
    candidates = sorted(
        semantic_candidates,
        key=lambda r: r.volume_usd,
        reverse=True,
    )

    return candidates[:top_k]


def find_markets_for_article(
    *,
    thesis_id: str | None = None,
    story_id: str | None = None,
    query: str | None = None,
    root: Path = ROOT,
    **kwargs,
) -> list[MarketMatch]:
    """Run the full prediction-market workflow for an input article.

    Provide exactly one of `thesis_id`, `story_id`, or `query`. Thesis/story ids
    are resolved against `global/theses/` and `global/stories/` markdown; the
    derived query is then handed to `find_markets`. Extra kwargs are forwarded
    (top_k, min_similarity, cache_ttl_hours, refresh, cache_path, sources,
    min_volume_usd).
    """
    provided = [name for name, val in (
        ("thesis_id", thesis_id), ("story_id", story_id), ("query", query),
    ) if val]
    if len(provided) != 1:
        raise ValueError(
            f"exactly one of thesis_id, story_id, query is required (got: {provided or 'none'})"
        )

    if thesis_id:
        query_text = _thesis_query(thesis_id, root=root)
    elif story_id:
        query_text = _story_query(story_id, root=root)
    else:
        assert query is not None
        query_text = query.strip()

    return find_markets(query_text, **kwargs)


__all__ = [
    "DEFAULT_CACHE_PATH",
    "DEFAULT_MIN_SIMILARITY",
    "MarketMatch",
    "PredictionMarket",
    "find_markets",
    "find_markets_for_article",
]

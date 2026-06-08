from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.embedding import cosine_similarity
from src.instruments.resolver import canonical as instrument_canonical
from src.instruments.resolver import exists as instrument_exists
from src.news.body_enrichment import enrich_member_bodies
from src.news.cluster import embed_news_item, next_story_id, recompute_cluster_features
from src.news.story_images import fetch_story_images
from src.news.synthesis import ClusterSynthesis, synthesize_cluster
from src.news.ticker_candidates import build_ticker_candidates
from src.news.types import ClusterSourceDoc
from src.news.verifier import YAHOO_SYMBOL_RE, verify_story_payload
from src.story.match_index import upsert_story_match_row
from src.i18n_translate import write_translation_sidecars


DB_REL = Path("db/hf.db")
logger = logging.getLogger("hf.scheduler")

# Minimum cosine similarity each cluster member must have with at least one
# peer member's embedding to survive the coherence gate. Members below this
# threshold are pruned from the synth input — they're the unrelated articles
# that cause "combined two completely unrelated events" Frankensteins.
# Gemini text-embedding-001 vectors are normalized; same-topic clusters
# typically score 0.65+ pairwise, distinct-topic noise drops to 0.40-0.50.
# 0.55 is the empirical sweet spot from the report's known-bad clusters.
COHERENCE_MIN_PEER_SIM: float = 0.55


def quote_speaker_fallback(
    quote: dict,
    publisher_by_news_id: dict[str, str],
) -> str:
    """Quote attribution must never be blank in rendered markdown. Prefer the
    LLM-emitted speaker; fall back to the publisher of the cited source so
    older stories that pre-date the speaker requirement still render with a
    source line."""
    speaker = str(quote.get("speaker") or "").strip()
    if speaker:
        return speaker
    for sid in quote.get("source_doc_ids") or []:
        pub = publisher_by_news_id.get(str(sid).strip())
        if pub:
            return pub
    return ""


def render_story_markdown(
    story_id: str,
    syn: ClusterSynthesis,
    members: list[ClusterSourceDoc],
) -> str:
    ordered = sorted(members, key=lambda m: (m.publisher, m.news_id))
    publisher_by_news_id = {m.news_id: m.publisher for m in ordered}

    lines: list[str] = [
        f"# {syn.headline}",
        "",
        "## Overview",
        "",
    ]
    for bullet in syn.overview:
        text = str(bullet.get("text") or "").strip()
        if text:
            lines.append(f"- {text}")

    if syn.quotes:
        lines.extend(["", "## Quotes", ""])
        for quote in syn.quotes:
            text = str(quote.get("text") or "").strip()
            if not text:
                continue
            speaker = quote_speaker_fallback(quote, publisher_by_news_id)
            suffix = f" — {speaker}" if speaker else ""
            lines.append(f"> {text}{suffix}")
            lines.append("")

    lines.extend(["", "## Sources", ""])
    for member in ordered:
        lines.append(f"- [{member.title}]({member.url}) — {member.publisher}")
    return "\n".join(lines).rstrip() + "\n"


def _cluster_members(conn: sqlite3.Connection, cluster_id: str, *, limit: int = 3) -> list[ClusterSourceDoc]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT n.id, n.headline, n.body_excerpt, n.source_url, n.publisher,
               n.published_at, n.sectors_json, n.regions_json,
               c.has_tier1_primary
        FROM news_cluster_member m
        JOIN news n ON n.id = m.news_id
        JOIN news_cluster c ON c.id = m.cluster_id
        WHERE m.cluster_id = ?
        ORDER BY
          CASE WHEN n.publisher IN ('Reuters', 'AP', 'Bloomberg', 'WSJ', 'Financial Times', 'CNBC') THEN 0 ELSE 1 END,
          n.materiality_score DESC,
          n.published_at DESC
        LIMIT ?
        """,
        (cluster_id, limit),
    ).fetchall()
    out: list[ClusterSourceDoc] = []
    for row in rows:
        tickers = [
            r[0] for r in conn.execute(
                "SELECT symbol FROM entity_tickers WHERE entity_type='news' AND entity_id=?",
                (row["id"],),
            ).fetchall()
        ]
        try:
            sectors = json.loads(row["sectors_json"] or "[]")
        except json.JSONDecodeError:
            sectors = []
        try:
            regions = json.loads(row["regions_json"] or "[]")
        except json.JSONDecodeError:
            regions = []
        out.append(ClusterSourceDoc(
            news_id=row["id"],
            title=row["headline"] or row["id"],
            url=row["source_url"] or "",
            publisher=row["publisher"] or "unknown",
            body=row["body_excerpt"] or "",
            published=row["published_at"],
            tickers=[str(t) for t in tickers],
            sectors=[str(s) for s in sectors],
            regions=[str(r) for r in regions],
        ))
    return out


def _parse_source_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_story_timestamp(ts: datetime) -> str:
    return (
        ts.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _latest_cluster_source_published_at(
    conn: sqlite3.Connection, cluster_id: str, *, now: datetime | None = None
) -> str:
    rows = conn.execute(
        """
        SELECT n.published_at, n.created_at
        FROM news_cluster_member m
        JOIN news n ON n.id = m.news_id
        WHERE m.cluster_id = ?
        """,
        (cluster_id,),
    ).fetchall()
    published = [
        ts
        for row in rows
        if (ts := _parse_source_timestamp(row["published_at"])) is not None
    ]
    source_created = [
        ts
        for row in rows
        if (ts := _parse_source_timestamp(row["created_at"])) is not None
    ]

    # Best available "real" event time: prefer the article publish time, then
    # fall back to when we ingested the news row.
    latest_source = None
    if published:
        latest_source = max(published)
    elif source_created:
        latest_source = max(source_created)

    # TRICK: the home feed sorts/displays by this value, so stamping a story
    # synthesized *now* with a 30h-old source time buries it dated "yesterday"
    # even though we just produced it. Only honor the true source time when it's
    # genuinely fresh (within the last 6h); otherwise use the generation time so
    # the freshly-written story surfaces at the top. This is a presentation
    # trick — it's not the real event time once the source is >6h stale.
    # `now` is injectable for deterministic testing of the freshness boundary.
    now = now or datetime.now(timezone.utc)
    if latest_source is not None and latest_source >= now - timedelta(hours=6):
        return _format_story_timestamp(latest_source)
    return _format_story_timestamp(now)


def _select_centroid(
    centroid_id: str | None,
    member_ids: set[str],
    members: list[ClusterSourceDoc],
) -> str:
    """Pick the centroid news id for a story.

    `news_cluster.centroid_news_id` is computed before the coherence gate
    drops outliers, so the stored value can reference a member we just
    pruned. Use it when it's still in `member_ids`; otherwise fall back
    to the first surviving member. Callers guarantee `members` is
    non-empty (the empty case short-circuits earlier in
    `write_cluster_story`).
    """
    if centroid_id and centroid_id in member_ids:
        return centroid_id
    return members[0].news_id


def _coherent_members(
    members: list[ClusterSourceDoc],
    embeddings: dict[str, list[float]],
    *,
    min_peer_sim: float = COHERENCE_MIN_PEER_SIM,
) -> tuple[list[ClusterSourceDoc], list[str]]:
    """Greedily prune members whose max cosine similarity to any peer is
    below `min_peer_sim`. Returns `(kept, dropped_ids)`.

    Algorithm: while ≥2 members remain, find the member with the lowest
    max-peer-similarity. If that score is below the threshold, drop it and
    repeat (removing one outlier can lift the remaining peers' coherence).
    Stops once all remaining members clear the threshold, or only one
    remains. Members whose embedding is missing are skipped by the outer
    `continue` and therefore can never be selected as the outlier — they
    are immune to dropping. This is the intended fail-safe: don't punish
    missing data; if the inlier(s) survive their peer check, the
    missing-embedding member rides along.

    Single-member clusters short-circuit unchanged.

    Cost: `O(N^3)` worst case (up to N-1 outer iterations, `O(N^2)` peer
    scan each). N is bounded by `_cluster_members(..., limit=3)` today, so
    the loop runs at most 27 cosine ops per cluster. If that cap rises
    (e.g. limit=8), revisit — at limit=16 this is still <5k ops, but
    above that consider memoizing peer sims across outer iterations.
    """
    if len(members) <= 1:
        return list(members), []

    surviving: list[ClusterSourceDoc] = list(members)
    dropped_ids: list[str] = []

    while len(surviving) >= 2:
        worst_idx = -1
        worst_score = 1.0
        for i, member in enumerate(surviving):
            vec_i = embeddings.get(member.news_id)
            if not vec_i:
                continue
            best_peer = 0.0
            for j, peer in enumerate(surviving):
                if i == j:
                    continue
                vec_j = embeddings.get(peer.news_id)
                if not vec_j:
                    continue
                sim = cosine_similarity(vec_i, vec_j)
                if sim > best_peer:
                    best_peer = sim
            if best_peer < worst_score:
                worst_score = best_peer
                worst_idx = i
        if worst_idx < 0 or worst_score >= min_peer_sim:
            break
        dropped_ids.append(surviving[worst_idx].news_id)
        surviving.pop(worst_idx)

    return surviving, dropped_ids


def _log_synth_llm_call(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    syn: ClusterSynthesis,
    caller: str,
) -> None:
    """Record one synthesis Gemini call (tokens + cost) in llm_calls.

    Emitted stories log as entity_type='story'; synths rejected after the
    Gemini call (verifier/taxonomy gates) still burned tokens, so they log as
    entity_type='cluster' with caller='synthesize_cluster_rejected'. Both feed
    the per-story cost gate — without the rejection rows it undercounts spend.
    """
    usage = getattr(syn, "usage", None)
    conn.execute(
        """
        INSERT INTO llm_calls (
          entity_type, entity_id, caller, model_id, latency_seconds,
          input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
          total_tokens, cost_usd, created_at
        )
        VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          strftime('%Y-%m-%dT%H:%M:%SZ','now')
        )
        """,
        (
            entity_type,
            entity_id,
            caller,
            syn.model_id,
            syn.latency_seconds,
            getattr(usage, "input_tokens", 0),
            getattr(usage, "output_tokens", 0),
            getattr(usage, "thinking_tokens", 0),
            getattr(usage, "cache_read_tokens", 0),
            getattr(usage, "total_tokens", 0),
            float(getattr(syn, "cost_usd", 0.0) or 0.0),
        ),
    )


def _log_synth_rejection(
    conn: sqlite3.Connection,
    cluster_id: str,
    reason: str,
    payload: dict | None = None,
    *,
    status: str = "firehose",
    synth: ClusterSynthesis | None = None,
) -> None:
    member_count_row = conn.execute(
        "SELECT member_count FROM news_cluster WHERE id=?", (cluster_id,)
    ).fetchone()
    member_count = int(member_count_row[0]) if member_count_row and member_count_row[0] is not None else None
    conn.execute(
        """
        INSERT INTO story_synth_rejected (cluster_id, rejected_at, reason, payload_json, member_count_at_reject)
        VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, ?)
        """,
        (cluster_id, reason, json.dumps(payload or {}), member_count),
    )
    conn.execute(
        "UPDATE news_cluster SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
        (status, cluster_id),
    )
    # Rejections that happen *after* synthesis already paid for the Gemini call;
    # log that spend so the per-story cost gate sees it. Pre-synth rejections
    # (e.g. the coherence gate) pass synth=None and cost nothing.
    if synth is not None:
        _log_synth_llm_call(
            conn,
            entity_type="cluster",
            entity_id=cluster_id,
            syn=synth,
            caller="synthesize_cluster_rejected",
        )


def _persist_story_row(
    conn: sqlite3.Connection,
    *,
    story_id: str,
    cluster_id: str,
    centroid_news_id: str,
    created_at: str,
    syn: ClusterSynthesis,
    images_json: str,
) -> None:
    conn.execute(
        """
        INSERT INTO story (
          id, cluster_id, centroid_news_id, created_at, headline, what_changed,
          overview_json, claims_json, quotes_json, market_relevance_json,
          open_questions_json, sectors_json, regions_json, theme_tag,
          images_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            story_id,
            cluster_id,
            centroid_news_id,
            created_at,
            syn.headline,
            syn.what_changed,
            json.dumps(syn.overview),
            json.dumps(syn.claims),
            json.dumps(syn.quotes),
            json.dumps(syn.market_relevance),
            "[]",
            json.dumps(syn.sectors),
            json.dumps(syn.regions),
            syn.theme_tag,
            images_json,
        ),
    )
    _log_synth_llm_call(
        conn,
        entity_type="story",
        entity_id=story_id,
        syn=syn,
        caller="synthesize_cluster",
    )
    emitted = [str(t).strip().upper() for t in syn.tickers if str(t).strip()]
    stored = 0
    for symbol_raw in emitted:
        if not YAHOO_SYMBOL_RE.match(symbol_raw):
            continue
        if instrument_exists(symbol_raw):
            symbol = instrument_canonical(symbol_raw)
        else:
            symbol = symbol_raw
            conn.execute(
                """INSERT INTO pending_instruments
                   (symbol, source, source_id, first_seen_at, last_seen_at)
                   VALUES (?, 'story', ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                   ON CONFLICT(symbol, source) DO UPDATE SET
                     last_seen_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                     seen_count = pending_instruments.seen_count + 1""",
                (symbol, story_id),
            )
        conn.execute(
            """INSERT INTO entity_tickers (entity_type, entity_id, symbol)
               VALUES ('story', ?, ?)
               ON CONFLICT(entity_type, entity_id, symbol) DO NOTHING""",
            (story_id, symbol),
        )
        stored += 1
    if emitted and stored < len(set(emitted)):
        logger.warning(
            "%s stored %d/%d synth-emitted ticker(s)",
            story_id,
            stored,
            len(set(emitted)),
        )
    conn.execute(
        """
        UPDATE news_cluster
        SET status='sharp_promoted',
            sectors_json=?,
            regions_json=?,
            updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
        WHERE id=?
        """,
        (json.dumps(syn.sectors), json.dumps(syn.regions), cluster_id),
    )


def write_cluster_story(
    root: Path,
    cluster_id: str,
    *,
    synth: ClusterSynthesis | None = None,
) -> str | None:
    dbp = root / DB_REL
    conn = sqlite3.connect(dbp, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        members = _cluster_members(conn, cluster_id, limit=3)
        if not members:
            return None
        # Body-enrichment rescue: scrape member URLs via Firecrawl when the
        # cluster has only thin RSS bodies. Synthesis grounds quotes/claims
        # against `member_bodies`, so we must capture them *after* enrichment.
        members = enrich_member_bodies(members, conn=conn, cluster_id=cluster_id)
        member_ids = {m.news_id for m in members}
        member_bodies = {m.news_id: m.body for m in members}
        row = conn.execute(
            "SELECT * FROM news_cluster WHERE id = ?",
            (cluster_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"cluster not found: {cluster_id}")

        member_embeddings: dict[str, list[float]] = {}
        with conn:
            for member in members:
                member_embeddings[member.news_id] = embed_news_item(conn, member.news_id)
            recompute_cluster_features(conn, cluster_id)
            row = conn.execute(
                "SELECT * FROM news_cluster WHERE id = ?",
                (cluster_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"cluster not found after recompute: {cluster_id}")

        # Coherence gate: drop members whose embeddings disagree with their
        # peers (the "combined unrelated events" failure mode in the report).
        # We do this BEFORE synth so synth never sees the unrelated article
        # and can't Frankenstein it into the narrative.
        original_count = len(members)
        members, dropped_ids = _coherent_members(members, member_embeddings)
        if dropped_ids:
            logger.info(
                "cluster %s: coherence gate dropped %d/%d members as outliers: %s",
                cluster_id, len(dropped_ids), original_count, dropped_ids,
            )
        if not members:
            with conn:
                _log_synth_rejection(
                    conn,
                    cluster_id,
                    f"coherence gate: all {original_count} members below peer-sim threshold",
                    {"dropped_ids": dropped_ids},
                    status="ambiguous",
                )
            return None
        # Refresh derived sets — verifier needs them to match the surviving
        # member ids exactly.
        member_ids = {m.news_id for m in members}
        member_bodies = {m.news_id: m.body for m in members}

        sector_prior = []
        region_prior = []
        try:
            sector_prior = json.loads(row["sectors_json"] or "[]")
            region_prior = json.loads(row["regions_json"] or "[]")
        except json.JSONDecodeError:
            pass

        # Build the per-cluster ticker candidate slate from member bodies +
        # the cluster's routing-attached tickers. The slate constrains the
        # LLM's symbol choice; post-synth backfills are gone — the LLM is
        # the discovery layer with this slate as its universe.
        cluster_tickers: list[str] = []
        try:
            cluster_tickers = list(json.loads(row["tickers_json"] or "[]"))
        except json.JSONDecodeError:
            pass
        ticker_candidates = build_ticker_candidates(
            members,
            cluster_tickers=cluster_tickers,
            sectors=sector_prior,
        )
        slate_symbols = {c["symbol"] for c in ticker_candidates}
        # Derive verifier alias map from the slate itself rather than the
        # registry. Sector-thematic seeds carry context aliases ("Treasury
        # yield", "Brent crude") that live only on the slate, so the
        # verifier needs the merged set to accept thematic evidence_spans.
        ticker_aliases = {c["symbol"]: set(c["aliases"]) for c in ticker_candidates}

        syn = synth or synthesize_cluster(
            members,
            sector_prior=sector_prior,
            region_prior=region_prior,
            event_class=row["event_class"],
            ticker_candidates=ticker_candidates,
        )
        # Verify the object-form market_relevance against cited bodies, the
        # candidate slate (hard membership check), AND the per-symbol alias
        # set. `allowed_symbols` makes the prompt's closed-list claim real;
        # `ticker_aliases` enforces evidence_span ∈ aliases(symbol), so
        # "Powell" alone can't satisfy POWL — the long-form "Powell
        # Industries" must appear.
        verification = verify_story_payload(
            syn.as_payload(),
            member_ids=member_ids,
            member_bodies=member_bodies,
            ticker_aliases=ticker_aliases,
            allowed_symbols=slate_symbols,
        )
        with conn:
            if not verification.ok:
                _log_synth_rejection(
                    conn,
                    cluster_id,
                    "; ".join(verification.errors),
                    syn.as_payload(),
                    synth=syn,
                )
                return None
        # Prose is the model's: quotes that don't verbatim-match a cited
        # body scrub from the payload, but don't reject the story. Citation
        # integrity (speaker, source_doc_ids, non-member cite) already
        # landed in `errors` above.
        if verification.quote_scrub_indices:
            scrub = set(verification.quote_scrub_indices)
            syn.quotes = [q for i, q in enumerate(syn.quotes) if i not in scrub]
        # Strip evidence anchors; downstream code (storage, joins) works on flat strings.
        syn.flatten_market_relevance()
        with conn:
            taxonomy_errors = []
            if not syn.sectors:
                taxonomy_errors.append("empty sectors")
            if not syn.regions:
                taxonomy_errors.append("empty regions")
            if taxonomy_errors:
                _log_synth_rejection(
                    conn,
                    cluster_id,
                    "; ".join(taxonomy_errors),
                    syn.as_payload(),
                    status="ambiguous",
                    synth=syn,
                )
                return None

        centroid_news_id = _select_centroid(
            row["centroid_news_id"], member_ids, members
        )
        story_created_at = _latest_cluster_source_published_at(conn, cluster_id)

        # next_story_id reads MAX(id)+1, which races when synth_workers > 1:
        # two threads can read the same MAX before either has inserted, then
        # both try to INSERT the same id and the loser dies with
        # "UNIQUE constraint failed: story.id". Allocate inside the INSERT's
        # transaction and retry the loser with a fresh MAX — the row from
        # the winning thread is now visible, so the next pick is unique.
        # Image fetch is moved AFTER the INSERT: it's a slow network call
        # that shouldn't hold a write lock, and the inserted row is the
        # source of truth for the id (a tentative pre-insert id could end
        # up uploaded to R2 with the wrong prefix on retry).
        story_id: str | None = None
        last_integrity: sqlite3.IntegrityError | None = None
        for _ in range(8):
            try:
                with conn:
                    candidate = next_story_id(conn)
                    _persist_story_row(
                        conn,
                        story_id=candidate,
                        cluster_id=cluster_id,
                        centroid_news_id=centroid_news_id,
                        created_at=story_created_at,
                        syn=syn,
                        images_json="[]",
                    )
                story_id = candidate
                break
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed: story.id" not in str(exc):
                    raise
                last_integrity = exc
                continue
        if story_id is None:
            raise last_integrity or sqlite3.IntegrityError(
                f"could not allocate story id for {cluster_id} after retries"
            )

        try:
            images = fetch_story_images(story_id, members, centroid_news_id)
        except Exception as exc:
            logger.warning(
                "failed to fetch images for %s: %s",
                story_id,
                exc,
            )
            images = []
        if images:
            with conn:
                conn.execute(
                    "UPDATE story SET images_json = ? WHERE id = ?",
                    (json.dumps(images), story_id),
                )

        story_dir = root / "global" / "stories"
        story_dir.mkdir(parents=True, exist_ok=True)
        story_path = story_dir / f"{story_id}.md"
        story_path.write_text(render_story_markdown(story_id, syn, members), encoding="utf-8")

        try:
            write_translation_sidecars(
                story_path,
                entity_type="story",
                entity_id=story_id,
                db_path=dbp,
            )
        except Exception as exc:
            logger.warning("failed to translate %s sidecars: %s", story_id, exc)

        try:
            upsert_story_match_row(root, story_id)
        except Exception as exc:
            logger.warning(
                "failed to embed %s for story_match_chunks: %s",
                story_id,
                exc,
            )

        return story_id
    finally:
        conn.close()


__all__ = [
    "write_cluster_story",
    "render_story_markdown",
]

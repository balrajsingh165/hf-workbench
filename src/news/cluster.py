"""Online cluster-as-event primitives for raw news rows."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.clients.gemini import embed_content
from src.embedding import cosine_similarity
from src.news.firehose_gate import PR_WIRE_MATERIALITY_CAP
from src.news.publishers import is_pr_wire_publisher_name, publisher_for_name
from src.news.taxonomies import normalize_regions, normalize_sectors

CHEAP_ATTACH_THRESHOLD = 0.60
EMBEDDING_ATTACH_THRESHOLD = 0.70
CLUSTER_WINDOW_HOURS = 72
HEADLINE_HASH_WINDOW_HOURS = 48
MAX_CENTROID_SCAN = 100

# Recency caps for the routing/promotion pipeline. The freshest member of a
# cluster must be no older than these caps for the cluster to leave firehose.
# Strict cap covers the lanes where a single source is enough to promote
# (institutional auto-promote, sharp event w/o full corroboration, mainstream
# single-source, macro single-source) — these need same-cycle freshness.
# Relaxed cap covers corroborated and overlap-driven lanes where a few extra
# days of latency is acceptable.
PROMOTION_MAX_AGE_H_STRICT = 48
PROMOTION_MAX_AGE_H_RELAXED = 168

# Hard ceiling on how old an incoming news item can be before it is allowed to
# attach to an *existing* cluster. Stale items are still ingested but get their
# own isolated firehose cluster so they cannot inflate `independent_pub_count`,
# refresh `last_seen_at`, or re-trigger promotion of an active cluster.
CLUSTER_ATTACHMENT_MAX_AGE_H = 48

EVENT_CLASS_PRIORITY: tuple[str, ...] = (
    "fed_action",
    "macro_print",
    "m_a",
    "regulatory",
    "earnings",
    "guidance",
    "product",
    "financing",
    "corporate_action",
    "market_flow",
    "trade_policy",
    "geopolitical",
)

EVENT_CLASS_MAP: dict[str, str] = {
    "central_bank": "fed_action",
    "rates": "fed_action",
    "macro": "macro_print",
    "cpi": "macro_print",
    "ppi": "macro_print",
    "payrolls": "macro_print",
    "gdp": "macro_print",
    "m&a": "m_a",
    "merger": "m_a",
    "acquisition": "m_a",
    "earnings": "earnings",
    "guidance": "guidance",
    "regulatory": "regulatory",
    "fda": "regulatory",
    "sec": "regulatory",
    "ftc": "regulatory",
    "doj": "regulatory",
    "financing": "financing",
    "layoffs": "corporate_action",
    "labor": "corporate_action",
    "etf_flow": "market_flow",
    "trade": "trade_policy",
    "tariff": "trade_policy",
    "geopolitical": "geopolitical",
}

EVENT_CLASS_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:fomc|federal reserve|ecb|bank of england|boj|pboc|rate (?:cut|hike|hold|decision)|monetary policy)\b", re.I), "fed_action"),
    (re.compile(r"\b(?:cpi|ppi|payrolls|nonfarm|gdp|pce|inflation|unemployment|jobless claims)\b", re.I), "macro_print"),
    (re.compile(r"\b(?:fda|ema|sec|ftc|doj|justice department|antitrust|probe|investigation|lawsuit|sues?|charges?|approv|reject|clearance)\b", re.I), "regulatory"),
    (re.compile(r"\b(?:earnings|eps|revenue|guidance|quarterly results|fiscal year)\b", re.I), "earnings"),
    (re.compile(r"\b(?:forecast|outlook|guidance|raises? (?:its )?forecast|cuts? (?:its )?forecast)\b", re.I), "guidance"),
    (re.compile(r"\b(?:loan|rescue package|financing|debt|equity deal|capital raise|offering|bailout|government owning)\b", re.I), "financing"),
    (re.compile(r"\b(?:layoffs?|job cuts?|freeze roles?|restructur|strike|union)\b", re.I), "corporate_action"),
    (re.compile(r"\b(?:etf|inflows?|outflows?|fund flows?|record assets?|open interest)\b", re.I), "market_flow"),
    (re.compile(r"\b(?:tariff|export controls?|export licen[cs]es?|sanctions?|trade deal|mofcom|customs)\b", re.I), "trade_policy"),
    (re.compile(r"\b(?:ceasefire|missile|drone|war|strikes?|attack|iran|russia|ukraine|strait of hormuz)\b", re.I), "geopolitical"),
    (re.compile(r"\b(?:merger|acquisition|takeover bid|to acquire|will acquire|acquires?|buyout|spin[- ]off|divest)\b", re.I), "m_a"),
    (re.compile(r"\b(?:launches?|releases?|unveils?|introduces?|preview|new model|product)\b", re.I), "product"),
)

STOPWORDS = frozenset(
    "a an and are as at be by for from has have in into is it its of on or "
    "that the this to with will after amid over under says said new update"
    .split()
)


@dataclass(slots=True)
class ClusterDecisionInput:
    cluster_id: str
    status: str
    max_materiality: int
    independent_pub_count: int
    has_tier1_primary: bool
    has_institutional_primary: bool
    tickers: set[str]
    sectors: set[str]
    regions: set[str]
    event_class: str | None
    has_press_wire_primary: bool = False
    has_non_pr_news_primary: bool = False
    # Age in hours of the freshest member's published_at (or created_at fallback)
    # at the time the decision input was loaded. None when no member has a
    # parseable timestamp; routing treats that as "unknown, allow" so legacy
    # rows without a published_at don't get falsely rejected.
    min_member_age_h: float | None = None


def _parse_iso_age_hours(value: str | None) -> float | None:
    """Return age in hours for an ISO-8601 timestamp, or None if unparseable.

    Handles both naive timestamps (assumed UTC) and timezone-aware ones, plus
    the `Z` suffix variant emitted by some feeds.
    """
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    return max(delta.total_seconds() / 3600.0, 0.0)


def normalize_headline(text: str) -> str:
    norm = re.sub(r"https?://\S+", " ", text or "")
    norm = re.sub(r"[^a-z0-9]+", " ", norm.lower())
    return re.sub(r"\s+", " ", norm).strip()


def headline_hash(text: str) -> str:
    norm = normalize_headline(text)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def headline_tokens(text: str) -> set[str]:
    return {
        tok for tok in normalize_headline(text).split()
        if len(tok) > 2 and tok not in STOPWORDS
    }


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def event_class_from_labels(labels: list[str] | tuple[str, ...]) -> str | None:
    mapped = {EVENT_CLASS_MAP.get(label.lower(), label.lower()) for label in labels if label}
    for priority in EVENT_CLASS_PRIORITY:
        if priority in mapped:
            return priority
    return sorted(mapped)[0] if mapped else None


def infer_event_class_from_text(headline: str, body: str = "") -> str | None:
    head = headline or ""
    text = f"{head}\n{body or ''}"
    for pattern, event_class in EVENT_CLASS_TEXT_PATTERNS:
        if pattern.search(head):
            return event_class
    for pattern, event_class in EVENT_CLASS_TEXT_PATTERNS:
        if pattern.search(text):
            return event_class
    return None


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _next_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    row = conn.execute(
        f"SELECT id FROM {table} ORDER BY CAST(SUBSTR(id, ?) AS INTEGER) DESC LIMIT 1",
        (len(prefix) + 2,),
    ).fetchone()
    best = 0
    if row:
        try:
            best = int(str(row[0]).split("_", 1)[1])
        except (IndexError, ValueError):
            best = 0
    return f"{prefix}_{best + 1:03d}"


def next_cluster_id(conn: sqlite3.Connection) -> str:
    return _next_id(conn, "news_cluster", "cluster")


def next_story_id(conn: sqlite3.Connection) -> str:
    return _next_id(conn, "story", "story")


def _fetch_news(conn: sqlite3.Connection, news_id: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
    if row is None:
        raise ValueError(f"news row not found: {news_id}")
    return row


def _news_tickers(conn: sqlite3.Connection, news_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT symbol FROM entity_tickers WHERE entity_type = 'news' AND entity_id = ?",
        (news_id,),
    ).fetchall()
    return {str(row[0]).upper() for row in rows if row[0]}


def _symbol_regions(symbols: set[str]) -> set[str]:
    out: set[str] = set()
    for sym in symbols:
        if sym.endswith(".AX"):
            out.add("australasia")
        elif sym.endswith(".T"):
            out.add("japan")
        elif sym.endswith(".L"):
            out.add("uk")
        elif sym.endswith((".PA", ".DE", ".MI", ".AS")):
            out.add("europe")
        elif sym.endswith((".HK", ".SS", ".SZ")):
            out.add("china")
        elif sym.endswith("=X"):
            out.update({"global", "north_america"})
        elif sym.endswith("=F") or sym.startswith("^"):
            out.add("global")
        else:
            out.add("north_america")
    return out


def _instrument_tags(conn: sqlite3.Connection, symbols: set[str]) -> tuple[set[str], set[str]]:
    sectors: set[str] = set()
    regions: set[str] = set()
    if not symbols:
        return sectors, regions
    placeholders = ",".join("?" for _ in symbols)
    try:
        rows = conn.execute(
            f"SELECT symbol, asset_class, region FROM instruments WHERE symbol IN ({placeholders})",
            tuple(symbols),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        region = row[2] if len(row) > 2 else None
        if region:
            regions.update(normalize_regions([region]))
        asset_class = str(row[1] or "")
        if asset_class == "crypto":
            sectors.add("crypto.bitcoin")
        elif asset_class == "fx":
            sectors.add("macro.fx")
        elif asset_class in {"commodity", "rate"}:
            sectors.add("macro.commodities" if asset_class == "commodity" else "macro.rates")
    regions.update(_symbol_regions(symbols))
    return sectors, regions


def _news_features(conn: sqlite3.Connection, news_id: str) -> dict:
    row = _fetch_news(conn, news_id)
    tickers = _news_tickers(conn, news_id)
    publisher = publisher_for_name(row["publisher"] or "", row["source_url"] or None)
    sectors = set(_json_list(row["sectors_json"]))
    regions = set(_json_list(row["regions_json"]))
    inst_sectors, inst_regions = _instrument_tags(conn, tickers)
    sectors.update(publisher.primary_sectors)
    sectors.update(inst_sectors)
    regions.update(publisher.primary_regions)
    regions.update(inst_regions)
    event_labels = _json_list(row["event_classes"])
    event_class = row["event_class"] or event_class_from_labels(event_labels)
    return {
        "row": row,
        "tickers": tickers,
        "sectors": set(normalize_sectors(tuple(sectors))),
        "regions": set(normalize_regions(tuple(regions))),
        "event_class": event_class,
        "publisher": publisher,
        "headline_norm": normalize_headline(row["headline"] or row["source_url"] or news_id),
        "headline_hash": row["headline_hash"] or headline_hash(row["headline"] or ""),
    }


def _cluster_candidates(conn: sqlite3.Connection, hours: int = CLUSTER_WINDOW_HOURS) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT * FROM news_cluster
        WHERE last_seen_at >= ?
          AND status IN ('open', 'firehose', 'ambiguous')
        ORDER BY last_seen_at DESC
        """,
        (cutoff,),
    ).fetchall()


def _cluster_feature_sets(row: sqlite3.Row) -> tuple[set[str], set[str], set[str]]:
    return (
        set(_json_list(row["tickers_json"])),
        set(_json_list(row["sectors_json"])),
        set(_json_list(row["regions_json"])),
    )


def cheap_attach_score(news_features: dict, cluster: sqlite3.Row) -> float:
    c_tickers, c_sectors, c_regions = _cluster_feature_sets(cluster)
    headline_score = jaccard(
        headline_tokens(news_features["row"]["headline"] or ""),
        headline_tokens(cluster["headline_norm"] or ""),
    )
    event_match = (
        1.0
        if news_features["event_class"]
        and news_features["event_class"] == cluster["event_class"]
        else 0.0
    )
    return (
        0.40 * jaccard(news_features["tickers"], c_tickers)
        + 0.20 * jaccard(news_features["sectors"], c_sectors)
        + 0.10 * jaccard(news_features["regions"], c_regions)
        + 0.20 * event_match
        + 0.10 * headline_score
    )


def _promotion_candidate(
    features: dict,
    active_thesis_tickers: set[str],
    cluster_member_count: int = 1,
    cluster_max_materiality: int = 0,
) -> bool:
    row = features["row"]
    materiality = int(row["materiality_score"] or 0)
    return (
        materiality >= 25
        or bool(features["tickers"] & active_thesis_tickers)
        or features["publisher"].tier == "tier1"
        or (cluster_member_count >= 2 and cluster_max_materiality >= 20)
    )


def _embedding_text(row: sqlite3.Row) -> str:
    return f"{row['headline'] or ''}\n{(row['body_excerpt'] or '')[:400]}".strip()


def embed_news_item(conn: sqlite3.Connection, news_id: str) -> list[float]:
    row = _fetch_news(conn, news_id)
    if row["embedding"]:
        raw = row["embedding"]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return [float(x) for x in json.loads(raw)]
    embedding = embed_content(_embedding_text(row), task_type="CLUSTERING").embeddings[0]
    conn.execute(
        "UPDATE news SET embedding = ? WHERE id = ?",
        (json.dumps(embedding).encode("utf-8"), news_id),
    )
    return embedding


def _attach_to_cluster(
    conn: sqlite3.Connection,
    *,
    cluster_id: str,
    news_id: str,
    similarity: float | None,
    attach_pass: str,
) -> str:
    conn.execute(
        """
        INSERT INTO news_cluster_member (cluster_id, news_id, attached_at, similarity, attach_pass)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?)
        ON CONFLICT(cluster_id, news_id) DO UPDATE SET
          similarity=excluded.similarity,
          attach_pass=excluded.attach_pass
        """,
        (cluster_id, news_id, similarity, attach_pass),
    )
    conn.execute(
        "UPDATE news SET cluster_id = ? WHERE id = ?",
        (cluster_id, news_id),
    )
    recompute_cluster_features(conn, cluster_id)
    return cluster_id


def _create_cluster(
    conn: sqlite3.Connection,
    news_id: str,
    features: dict,
    *,
    centroid_json: str | None = None,
    status: str = "firehose",
) -> str:
    row = features["row"]
    cluster_id = next_cluster_id(conn)
    first_seen = row["published_at"] or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO news_cluster (
          id, created_at, updated_at, status, centroid_news_id, centroid_json, headline_norm,
          event_class, max_materiality, tickers_json, sectors_json, regions_json,
          sector_confidence, region_confidence, first_seen_at, last_seen_at
        )
        VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cluster_id,
            status,
            news_id if centroid_json else None,
            centroid_json,
            features["headline_norm"],
            features["event_class"],
            int(row["materiality_score"] or 0),
            json.dumps(sorted(features["tickers"])),
            json.dumps(sorted(features["sectors"])),
            json.dumps(sorted(features["regions"])),
            0.3 if features["sectors"] else 0.0,
            0.3 if features["regions"] else 0.0,
            first_seen,
            first_seen,
        ),
    )
    return _attach_to_cluster(
        conn,
        cluster_id=cluster_id,
        news_id=news_id,
        similarity=None,
        attach_pass="embedding" if centroid_json else "cheap",
    )


def _embedding_attach(
    conn: sqlite3.Connection,
    news_id: str,
    features: dict,
    active_thesis_tickers: set[str],
) -> str | None:
    if not _promotion_candidate(features, active_thesis_tickers):
        return None
    embedding = embed_news_item(conn, news_id)
    rows = conn.execute(
        """
        SELECT * FROM news_cluster
        WHERE centroid_json IS NOT NULL
          AND last_seen_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', '-72 hours')
        ORDER BY last_seen_at DESC
        LIMIT ?
        """,
        (MAX_CENTROID_SCAN,),
    ).fetchall()
    best_id: str | None = None
    best_score = 0.0
    for row in rows:
        try:
            centroid = json.loads(row["centroid_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        ticker_overlap = jaccard(features["tickers"], set(_json_list(row["tickers_json"])))
        score = 0.6 * cosine_similarity(embedding, centroid) + 0.4 * ticker_overlap
        if score > best_score:
            best_score = score
            best_id = row["id"]
    if best_id and best_score >= EMBEDDING_ATTACH_THRESHOLD:
        return _attach_to_cluster(
            conn,
            cluster_id=best_id,
            news_id=news_id,
            similarity=best_score,
            attach_pass="embedding",
        )
    return _create_cluster(
        conn,
        news_id,
        features,
        centroid_json=json.dumps(embedding),
        status="open",
    )


def attach_news_item(
    conn: sqlite3.Connection,
    news_id: str,
    *,
    active_thesis_tickers: set[str] | None = None,
    allow_embedding: bool = False,
) -> str:
    """Attach a raw news row to a cluster.

    Firehose callers pass `allow_embedding=False`, so this function only runs
    the free headline-hash and cheap-feature passes for raw items.
    """
    active_tickers = active_thesis_tickers or set()
    features = _news_features(conn, news_id)
    conn.execute(
        """
        UPDATE news
        SET headline_hash = ?, event_class = ?, sectors_json = ?, regions_json = ?
        WHERE id = ?
        """,
        (
            features["headline_hash"],
            features["event_class"],
            json.dumps(sorted(features["sectors"])),
            json.dumps(sorted(features["regions"])),
            news_id,
        ),
    )

    # Recency guard: a news item older than CLUSTER_ATTACHMENT_MAX_AGE_H must
    # not attach to an existing cluster. Otherwise it would refresh the
    # cluster's last_seen_at, inflate independent_pub_count, and re-trigger
    # promotion of an active cluster from a stale source. Force it into its
    # own isolated firehose cluster instead. Items with no parseable timestamp
    # fall through to the normal attachment path.
    item_age_h = _parse_iso_age_hours(
        features["row"]["published_at"] or features["row"]["created_at"]
    )
    if item_age_h is not None and item_age_h > CLUSTER_ATTACHMENT_MAX_AGE_H:
        return _create_cluster(conn, news_id, features, status="firehose")

    hash_cutoff = (datetime.now(timezone.utc) - timedelta(hours=HEADLINE_HASH_WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    row = conn.execute(
        """
        SELECT c.id
        FROM news_cluster c
        JOIN news n ON n.cluster_id = c.id
        WHERE n.headline_hash = ?
          AND c.last_seen_at >= ?
          AND c.status IN ('open', 'firehose', 'ambiguous')
        ORDER BY c.last_seen_at DESC
        LIMIT 1
        """,
        (features["headline_hash"], hash_cutoff),
    ).fetchone()
    if row:
        return _attach_to_cluster(
            conn,
            cluster_id=row[0],
            news_id=news_id,
            similarity=1.0,
            attach_pass="headline_hash",
        )

    best_cluster: sqlite3.Row | None = None
    best_score = 0.0
    for candidate in _cluster_candidates(conn):
        score = cheap_attach_score(features, candidate)
        if score > best_score:
            best_score = score
            best_cluster = candidate
    if best_cluster is not None and best_score >= CHEAP_ATTACH_THRESHOLD:
        return _attach_to_cluster(
            conn,
            cluster_id=best_cluster["id"],
            news_id=news_id,
            similarity=best_score,
            attach_pass="cheap",
        )

    if allow_embedding:
        embedded_cluster = _embedding_attach(conn, news_id, features, active_tickers)
        if embedded_cluster:
            return embedded_cluster
    return _create_cluster(conn, news_id, features, status="firehose")


def recompute_cluster_features(conn: sqlite3.Connection, cluster_id: str) -> None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT n.*
        FROM news_cluster_member m
        JOIN news n ON n.id = m.news_id
        WHERE m.cluster_id = ?
        """,
        (cluster_id,),
    ).fetchall()
    if not rows:
        return

    tickers: set[str] = set()
    sectors: set[str] = set()
    regions: set[str] = set()
    groups: set[str] = set()
    has_tier1 = False
    has_institutional = False
    max_materiality = 0
    first_seen = None
    last_seen = None
    event_counts: dict[str, int] = {}
    embeddings: list[list[float]] = []
    centroid_news_id = None

    for row in rows:
        news_id = row["id"]
        tickers.update(_news_tickers(conn, news_id))
        sectors.update(_json_list(row["sectors_json"]))
        regions.update(_json_list(row["regions_json"]))
        publisher = publisher_for_name(row["publisher"] or "", row["source_url"] or None)
        if publisher.counts_as_independent:
            groups.add(publisher.independence_group)
        if publisher.is_tier1_news:
            has_tier1 = True
        if publisher.is_institutional_primary:
            has_institutional = True
        max_materiality = max(max_materiality, int(row["materiality_score"] or 0))
        seen_at = row["published_at"] or row["created_at"]
        first_seen = seen_at if first_seen is None else min(first_seen, seen_at)
        last_seen = seen_at if last_seen is None else max(last_seen, seen_at)
        if row["event_class"]:
            event_counts[row["event_class"]] = event_counts.get(row["event_class"], 0) + 1
        if row["embedding"]:
            raw = row["embedding"]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                embeddings.append([float(x) for x in json.loads(raw)])
                centroid_news_id = centroid_news_id or news_id
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

    centroid_json = None
    if embeddings:
        dims = len(embeddings[0])
        centroid = [
            sum(vec[i] for vec in embeddings if len(vec) == dims) / len(embeddings)
            for i in range(dims)
        ]
        norm = math.sqrt(sum(x * x for x in centroid))
        if norm:
            centroid = [x / norm for x in centroid]
        centroid_json = json.dumps(centroid)

    event_class = None
    if event_counts:
        event_class = sorted(event_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

    # PR-wire-only clusters (even when syndicated across PR Newswire +
    # GlobeNewswire, which look like two independent groups) can score
    # mat=100 on M&A regexes and starve promote-eligible candidates in the
    # route_news_clusters window. Cap them by issuer-PR origin, not by
    # independent_pub_count, since the wires are still the same release.
    if rows and all(
        is_pr_wire_publisher_name(str(row["publisher"] or ""))
        for row in rows
    ):
        max_materiality = min(max_materiality, PR_WIRE_MATERIALITY_CAP)

    conn.execute(
        """
        UPDATE news_cluster
        SET updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
            centroid_news_id=COALESCE(?, centroid_news_id),
            centroid_json=COALESCE(?, centroid_json),
            event_class=COALESCE(?, event_class),
            max_materiality=?,
            member_count=?,
            independent_pub_count=?,
            has_tier1_primary=?,
            has_institutional_primary=?,
            tickers_json=?,
            sectors_json=?,
            regions_json=?,
            sector_confidence=?,
            region_confidence=?,
            first_seen_at=?,
            last_seen_at=?
        WHERE id=?
        """,
        (
            centroid_news_id,
            centroid_json,
            event_class,
            max_materiality,
            len(rows),
            len(groups),
            1 if has_tier1 else 0,
            1 if has_institutional else 0,
            json.dumps(sorted(tickers)),
            json.dumps(sorted(normalize_sectors(tuple(sectors)))),
            json.dumps(sorted(normalize_regions(tuple(regions)))),
            0.7 if sectors else 0.0,
            0.7 if regions else 0.0,
            first_seen,
            last_seen,
            cluster_id,
        ),
    )


def load_cluster_decision_input(conn: sqlite3.Connection, cluster_id: str) -> ClusterDecisionInput:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM news_cluster WHERE id = ?", (cluster_id,)).fetchone()
    if row is None:
        raise ValueError(f"cluster not found: {cluster_id}")
    cols = row.keys()
    has_inst = bool(row["has_institutional_primary"]) if "has_institutional_primary" in cols else False
    member_rows = conn.execute(
        """
        SELECT n.publisher, n.source_url, n.published_at, n.created_at
        FROM news_cluster_member m
        JOIN news n ON n.id = m.news_id
        WHERE m.cluster_id = ?
        """,
        (cluster_id,),
    ).fetchall()
    member_publishers = [
        publisher_for_name(member["publisher"] or "", member["source_url"] or None)
        for member in member_rows
    ]
    has_press_wire = any(pub.kind == "pr_wire" for pub in member_publishers)
    has_non_pr_news = any(pub.kind == "news" for pub in member_publishers)
    member_ages = [
        age
        for member in member_rows
        if (age := _parse_iso_age_hours(member["published_at"] or member["created_at"])) is not None
    ]
    min_member_age_h = min(member_ages) if member_ages else None
    return ClusterDecisionInput(
        cluster_id=row["id"],
        status=row["status"],
        max_materiality=int(row["max_materiality"] or 0),
        independent_pub_count=int(row["independent_pub_count"] or 0),
        has_tier1_primary=bool(row["has_tier1_primary"]),
        has_institutional_primary=has_inst,
        tickers=set(_json_list(row["tickers_json"])),
        sectors=set(_json_list(row["sectors_json"])),
        regions=set(_json_list(row["regions_json"])),
        event_class=row["event_class"],
        has_press_wire_primary=has_press_wire,
        has_non_pr_news_primary=has_non_pr_news,
        min_member_age_h=min_member_age_h,
    )


def cluster_existing_firehose(db_path: Path, *, limit: int | None = None) -> int:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        rows = conn.execute(
            """
            SELECT id FROM news
            WHERE headline IS NOT NULL
              AND cluster_id IS NULL
            ORDER BY published_at DESC, id DESC
            LIMIT ?
            """,
            (limit or 1000000,),
        ).fetchall()
        with conn:
            for row in rows:
                attach_news_item(conn, row[0], allow_embedding=False)
        return len(rows)
    finally:
        conn.close()


__all__ = [
    "ClusterDecisionInput",
    "attach_news_item",
    "cheap_attach_score",
    "cluster_existing_firehose",
    "embed_news_item",
    "event_class_from_labels",
    "infer_event_class_from_text",
    "headline_hash",
    "load_cluster_decision_input",
    "next_cluster_id",
    "next_story_id",
    "normalize_headline",
    "recompute_cluster_features",
]

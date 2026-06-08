from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.clients.gemini import GEMINI_EMBEDDING_2_PREVIEW, batch_embed_contents, embed_content
from src.embedding import cosine_similarity
from src.thesis.docs import ThesisChunk, load_all_thesis_chunks


CHUNK_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thesis_match_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_id TEXT NOT NULL REFERENCES theses(id),
    chunk_key TEXT NOT NULL,
    chunk_kind TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    tickers_json TEXT NOT NULL DEFAULT '[]',
    sectors_json TEXT NOT NULL DEFAULT '[]',
    embedding_model TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
)
"""

CHUNK_UNIQUE_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_thesis_match_chunks_thesis_chunk
ON thesis_match_chunks (thesis_id, chunk_key)
"""

THESIS_MATCH_EMBEDDING_MODEL = GEMINI_EMBEDDING_2_PREVIEW
THESIS_MATCH_EMBEDDING_DIMENSIONALITY = 1536


@dataclass(slots=True)
class DenseMatch:
    thesis_id: str
    chunk_key: str
    chunk_kind: str
    score: float
    chunk_text: str


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_match_index_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CHUNK_TABLE_DDL)
    conn.execute(CHUNK_UNIQUE_INDEX_DDL)
    conn.commit()


def rebuild_thesis_match_index(
    root: Path,
    *,
    batch_size: int = 32,
    dry_run: bool = False,
) -> int:
    chunks = load_all_thesis_chunks(root / "global" / "theses")
    if dry_run:
        return len(chunks)

    text_batches = [
        [chunk.chunk_text]
        for chunk in chunks
    ]
    batch_results = batch_embed_contents(
        text_batches,
        model=THESIS_MATCH_EMBEDDING_MODEL,
        output_dimensionality=THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
        task_type="RETRIEVAL_DOCUMENT",
        max_workers=max(1, batch_size),
    )
    embeddings = [
        embedding
        for batch_result in batch_results
        for embedding in batch_result.embeddings
    ]

    db_path = root / "db" / "hf.db"
    conn = get_db_connection(db_path)
    try:
        ensure_match_index_schema(conn)
        with conn:
            conn.execute("DELETE FROM thesis_match_chunks")
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                conn.execute(
                    """
                    INSERT INTO thesis_match_chunks (
                        thesis_id,
                        chunk_key,
                        chunk_kind,
                        chunk_text,
                        tickers_json,
                        sectors_json,
                        embedding_model,
                        embedding_json,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    """,
                    (
                        chunk.thesis_id,
                        chunk.chunk_key,
                        chunk.chunk_kind,
                        chunk.chunk_text,
                        json.dumps(chunk.tickers),
                        json.dumps(chunk.sectors),
                        THESIS_MATCH_EMBEDDING_MODEL,
                        json.dumps(embedding),
                    ),
                )
    finally:
        conn.close()

    return len(chunks)


def search_dense(
    db_path: Path,
    query_text: str,
    *,
    top_k: int = 10,
    min_score: float = 0.0,
) -> list[DenseMatch]:
    conn = get_db_connection(db_path)
    try:
        ensure_match_index_schema(conn)
        rows = conn.execute(
            """
            SELECT thesis_id, chunk_key, chunk_kind, chunk_text, embedding_json
            FROM thesis_match_chunks
            ORDER BY thesis_id, chunk_key
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    query_embedding = embed_content(
        query_text,
        model=THESIS_MATCH_EMBEDDING_MODEL,
        output_dimensionality=THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
        task_type="RETRIEVAL_QUERY",
    ).embeddings[0]

    scored = [
        DenseMatch(
            thesis_id=row["thesis_id"],
            chunk_key=row["chunk_key"],
            chunk_kind=row["chunk_kind"],
            score=cosine_similarity(query_embedding, json.loads(row["embedding_json"])),
            chunk_text=row["chunk_text"],
        )
        for row in rows
    ]

    # Deduplicate to one result per thesis — keep the best-scoring chunk.
    best_per_thesis: dict[str, DenseMatch] = {}
    for match in scored:
        prev = best_per_thesis.get(match.thesis_id)
        if prev is None or match.score > prev.score:
            best_per_thesis[match.thesis_id] = match

    ranked = sorted(best_per_thesis.values(), key=lambda m: m.score, reverse=True)[:top_k]
    if min_score > 0:
        ranked = [m for m in ranked if m.score >= min_score]
    return ranked


__all__ = [
    "DenseMatch",
    "THESIS_MATCH_EMBEDDING_DIMENSIONALITY",
    "THESIS_MATCH_EMBEDDING_MODEL",
    "ensure_match_index_schema",
    "rebuild_thesis_match_index",
    "search_dense",
]

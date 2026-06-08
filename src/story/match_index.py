from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from src.clients.gemini import GEMINI_EMBEDDING_2_PREVIEW, batch_embed_contents, embed_content
from src.embedding import cosine_similarity
from src.i18n import is_i18n_sidecar
from src.story.docs import StoryDocument, parse_story_markdown


CHUNK_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS story_match_chunks (
    story_id TEXT PRIMARY KEY REFERENCES story(id),
    chunk_text TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
)
"""

STORY_MATCH_EMBEDDING_MODEL = GEMINI_EMBEDDING_2_PREVIEW
STORY_MATCH_EMBEDDING_DIMENSIONALITY = 1536


@dataclass(slots=True)
class DenseStoryMatch:
    story_id: str
    score: float
    chunk_text: str


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_story_match_index_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CHUNK_TABLE_DDL)
    conn.commit()


def _resolve_story_path(root: Path, story_id: str) -> Path:
    return root / "global" / "stories" / f"{story_id}.md"


def _story_kind_map(root: Path) -> dict[str, str]:
    db_path = root / "db" / "hf.db"
    if not db_path.exists():
        return {}
    conn = get_db_connection(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(story)")}
        if "kind" not in cols:
            return {}
        rows = conn.execute("SELECT id, kind FROM story").fetchall()
        return {str(row["id"]): str(row["kind"] or "story") for row in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _is_news_story(root: Path, story_id: str) -> bool:
    db_path = root / "db" / "hf.db"
    if not db_path.exists():
        return True
    conn = get_db_connection(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(story)")}
        if "kind" not in cols:
            return True
        row = conn.execute("SELECT kind FROM story WHERE id = ?", (story_id,)).fetchone()
        return row is None or str(row["kind"] or "story") == "story"
    except sqlite3.Error:
        return True
    finally:
        conn.close()


def _load_all_story_documents(root: Path) -> list[StoryDocument]:
    """Skip malformed files with a warning rather than aborting the whole rebuild."""
    story_dir = root / "global" / "stories"
    kind_by_id = _story_kind_map(root)
    documents: list[StoryDocument] = []
    for path in sorted(story_dir.glob("story_*.md")):
        if is_i18n_sidecar(path):
            continue
        if kind_by_id and kind_by_id.get(path.stem, "story") != "story":
            continue
        try:
            documents.append(parse_story_markdown(path))
        except (ValueError, OSError) as exc:
            print(f"warning: skipping {path.name}: {exc}", file=sys.stderr)
    return documents


def rebuild_story_match_index(
    root: Path,
    *,
    batch_size: int = 32,
    dry_run: bool = False,
) -> int:
    documents = _load_all_story_documents(root)
    if dry_run:
        return len(documents)

    text_batches = [[doc.query_text] for doc in documents]
    batch_results = batch_embed_contents(
        text_batches,
        model=STORY_MATCH_EMBEDDING_MODEL,
        output_dimensionality=STORY_MATCH_EMBEDDING_DIMENSIONALITY,
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
        ensure_story_match_index_schema(conn)
        with conn:
            conn.execute("DELETE FROM story_match_chunks")
            for doc, embedding in zip(documents, embeddings, strict=True):
                conn.execute(
                    """
                    INSERT INTO story_match_chunks (
                        story_id,
                        chunk_text,
                        embedding_model,
                        embedding_json,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                    """,
                    (
                        doc.story_id,
                        doc.query_text,
                        STORY_MATCH_EMBEDDING_MODEL,
                        json.dumps(embedding),
                    ),
                )
    finally:
        conn.close()

    return len(documents)


def upsert_story_match_row(root: Path, story_id: str) -> None:
    if not _is_news_story(root, story_id):
        return
    document = parse_story_markdown(_resolve_story_path(root, story_id))
    embedding = embed_content(
        document.query_text,
        model=STORY_MATCH_EMBEDDING_MODEL,
        output_dimensionality=STORY_MATCH_EMBEDDING_DIMENSIONALITY,
        task_type="RETRIEVAL_DOCUMENT",
    ).embeddings[0]

    db_path = root / "db" / "hf.db"
    conn = get_db_connection(db_path)
    try:
        ensure_story_match_index_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO story_match_chunks (
                    story_id,
                    chunk_text,
                    embedding_model,
                    embedding_json,
                    updated_at
                )
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                ON CONFLICT(story_id) DO UPDATE SET
                    chunk_text=excluded.chunk_text,
                    embedding_model=excluded.embedding_model,
                    embedding_json=excluded.embedding_json,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                """,
                (
                    document.story_id,
                    document.query_text,
                    STORY_MATCH_EMBEDDING_MODEL,
                    json.dumps(embedding),
                ),
            )
    finally:
        conn.close()


def search_dense_story(
    db_path: Path,
    query_text: str,
    *,
    top_k: int = 10,
    min_score: float = 0.0,
    since: str | None = None,
) -> list[DenseStoryMatch]:
    conn = get_db_connection(db_path)
    try:
        ensure_story_match_index_schema(conn)
        if since is None:
            rows = conn.execute(
                """
                SELECT c.story_id AS story_id, c.chunk_text AS chunk_text,
                       c.embedding_json AS embedding_json
                FROM story_match_chunks c
                ORDER BY c.story_id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.story_id AS story_id, c.chunk_text AS chunk_text,
                       c.embedding_json AS embedding_json
                FROM story_match_chunks c
                JOIN story s ON s.id = c.story_id
                WHERE s.created_at >= ?
                ORDER BY c.story_id
                """,
                (since,),
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    query_embedding = embed_content(
        query_text,
        model=STORY_MATCH_EMBEDDING_MODEL,
        output_dimensionality=STORY_MATCH_EMBEDDING_DIMENSIONALITY,
        task_type="RETRIEVAL_QUERY",
    ).embeddings[0]

    scored = [
        DenseStoryMatch(
            story_id=row["story_id"],
            score=cosine_similarity(query_embedding, json.loads(row["embedding_json"])),
            chunk_text=row["chunk_text"],
        )
        for row in rows
    ]

    ranked = sorted(scored, key=lambda m: m.score, reverse=True)[:top_k]
    if min_score > 0:
        ranked = [m for m in ranked if m.score >= min_score]
    return ranked


__all__ = [
    "DenseStoryMatch",
    "STORY_MATCH_EMBEDDING_DIMENSIONALITY",
    "STORY_MATCH_EMBEDDING_MODEL",
    "ensure_story_match_index_schema",
    "rebuild_story_match_index",
    "search_dense_story",
    "upsert_story_match_row",
]

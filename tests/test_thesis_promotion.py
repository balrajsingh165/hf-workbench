from __future__ import annotations

import sqlite3
from pathlib import Path

from src.thesis import discover


def _init_candidate(root: Path, thesis_id: str, *, with_ticker: bool = True) -> Path:
    db_path = root / "db" / "hf.db"
    db_path.parent.mkdir(parents=True)
    thesis_dir = root / "global" / "theses"
    thesis_dir.mkdir(parents=True)
    (thesis_dir / f"{thesis_id}.md").write_text(
        """# Thesis: Supply squeeze lifts copper miners

## Core Thesis

Refinery disruptions keep copper supply tight and lift copper miners.

## Invalidation Conditions

- Copper inventories rise for three straight weeks.
- Major smelters restart faster than expected.
""",
        encoding="utf-8",
    )

    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE theses (id TEXT PRIMARY KEY, review_status TEXT)"
            )
            conn.execute(
                "CREATE TABLE entity_tickers "
                "(entity_type TEXT, entity_id TEXT, symbol TEXT, direction TEXT)"
            )
            conn.execute(
                "INSERT INTO theses (id, review_status) VALUES (?, 'candidate')",
                (thesis_id,),
            )
            if with_ticker:
                conn.execute(
                    "INSERT INTO entity_tickers VALUES ('thesis', ?, 'FCX', 'bullish')",
                    (thesis_id,),
                )
    finally:
        conn.close()
    return db_path


def _review_status(db_path: Path, thesis_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT review_status FROM theses WHERE id = ?", (thesis_id,)
        ).fetchone()
    finally:
        conn.close()
    return str(row[0])


def test_candidate_promotion_stays_candidate_without_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _init_candidate(tmp_path, "thesis_001")
    monkeypatch.setattr(discover, "search_dense", lambda *args, **kwargs: [])

    status = discover._apply_candidate_promotion(
        "thesis_001",
        db_path,
        tmp_path,
        has_story_evidence=False,
        no_evidence_message="stays candidate -- no evidence",
    )

    assert status == "candidate"
    assert _review_status(db_path, "thesis_001") == "candidate"


def test_candidate_promotion_activates_with_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _init_candidate(tmp_path, "thesis_001")
    monkeypatch.setattr(discover, "search_dense", lambda *args, **kwargs: [])

    status = discover._apply_candidate_promotion(
        "thesis_001",
        db_path,
        tmp_path,
        has_story_evidence=True,
        no_evidence_message="stays candidate -- no evidence",
    )

    assert status == "active"
    assert _review_status(db_path, "thesis_001") == "active"


def test_candidate_promotion_rejects_failed_quality_gate(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = _init_candidate(tmp_path, "thesis_001", with_ticker=False)
    monkeypatch.setattr(discover, "search_dense", lambda *args, **kwargs: [])

    status = discover._apply_candidate_promotion(
        "thesis_001",
        db_path,
        tmp_path,
        has_story_evidence=True,
        no_evidence_message="stays candidate -- no evidence",
    )

    assert status == "rejected"
    assert _review_status(db_path, "thesis_001") == "rejected"

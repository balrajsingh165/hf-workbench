from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
import re

from src.i18n import canonical_markdown_id, is_i18n_sidecar


SECTION_RE = re.compile(r"^## (?P<name>.+?)\s*$", re.MULTILINE)
THESIS_TITLE_RE = re.compile(r"^# Thesis:\s*(?P<title>.+?)\s*$", re.MULTILINE)
TICKER_RE = re.compile(r"^- (?P<ticker>[A-Z^][A-Z0-9.=^-]*)\s*(?:\(|$)")
TICKER_DIRECTION_RE = re.compile(r"\b(bullish|bearish|neutral)\b", re.IGNORECASE)
SHORT_BELIEF_RE = re.compile(
    r"^- \*\*Short Belief\*\*:\s*(?P<line>.+?)\s*$", re.MULTILINE
)

# Default DB lookup path: <repo>/global/theses/<id>.md → <repo>/db/hf.db
def _default_db_path(thesis_path: Path) -> Path:
    return thesis_path.resolve().parents[2] / "db" / "hf.db"


def _load_tickers_from_db(thesis_id: str, db_path: Path) -> list[tuple[str, str | None]]:
    """Return (symbol, direction) pairs for a thesis, in deterministic order."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT symbol, direction FROM entity_tickers "
            "WHERE entity_type = 'thesis' AND entity_id = ? "
            "ORDER BY symbol",
            (thesis_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1]) for r in rows]


@dataclass(slots=True)
class ThesisDocument:
    thesis_id: str
    title: str
    core_thesis: str
    tickers: list[str]
    ticker_directions: list[tuple[str, str]]  # (raw_ticker, 'bullish'|'bearish'); 'neutral' omitted
    invalidations: list[str]
    path: Path
    short_belief: str | None = None


@dataclass(slots=True)
class ThesisChunk:
    thesis_id: str
    chunk_key: str
    chunk_kind: str
    chunk_text: str
    search_text: str
    tags_text: str
    tickers: list[str]
    sectors: list[str]


def _extract_sections(markdown: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(markdown))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections[match.group("name").strip()] = markdown[start:end].strip()
    return sections


def _extract_title(markdown: str, path: Path) -> str:
    match = THESIS_TITLE_RE.search(markdown)
    if not match:
        raise ValueError(f"Missing thesis title in {path}")
    return match.group("title").strip()


def _extract_tickers(section_text: str) -> list[str]:
    tickers: list[str] = []
    for line in section_text.splitlines():
        match = TICKER_RE.match(line.strip())
        if match:
            tickers.append(match.group("ticker"))
    return tickers


def _extract_ticker_directions(section_text: str) -> list[tuple[str, str]]:
    """Parse `- TICKER (bullish|bearish|neutral — prose)` lines.

    First direction token in the parenthetical wins (handles `neutral/bearish`
    by taking the leftmost call — consistent and predictable). `neutral` is
    dropped: it contributes no signal to tailwind.
    """
    out: list[tuple[str, str]] = []
    for line in section_text.splitlines():
        stripped = line.strip()
        match = TICKER_RE.match(stripped)
        if not match:
            continue
        direction_match = TICKER_DIRECTION_RE.search(stripped)
        if not direction_match:
            continue
        direction = direction_match.group(1).lower()
        if direction == "neutral":
            continue
        out.append((match.group("ticker"), direction))
    return out


def _extract_bullets(section_text: str) -> list[str]:
    bullets: list[str] = []
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            bullets.append(line[2:].strip())
    return bullets


def parse_thesis_markdown(
    path: Path, *, db_path: Path | None = None
) -> ThesisDocument:
    """Parse thesis markdown.

    Tickers live in `entity_tickers`; the markdown carries narrative only. When
    the markdown still has a legacy `## Tickers` section it is honored as a
    fallback. Pass `db_path` to override DB location (defaults to repo's
    `db/hf.db` derived from the markdown path).
    """
    markdown = path.read_text(encoding="utf-8")
    sections = _extract_sections(markdown)

    title = _extract_title(markdown, path)
    thesis_id = canonical_markdown_id(path)
    core_thesis = sections.get("Core Thesis", "").strip()
    if not core_thesis:
        raise ValueError(f"Missing Core Thesis section in {path}")

    invalidations = _extract_bullets(sections.get("Invalidation Conditions", ""))
    if not invalidations:
        raise ValueError(f"Missing invalidation conditions in {path}")

    short_belief_match = SHORT_BELIEF_RE.search(markdown)
    short_belief = short_belief_match.group("line").strip() if short_belief_match else None

    # Primary source: entity_tickers table. Fallback: legacy `## Tickers` block.
    db_path = db_path or _default_db_path(path)
    db_pairs = _load_tickers_from_db(thesis_id, db_path)
    if db_pairs:
        tickers = [sym for sym, _ in db_pairs]
        ticker_directions = [
            (sym, direction) for sym, direction in db_pairs
            if direction in ("bullish", "bearish")
        ]
    else:
        tickers_section = sections.get("Tickers", "")
        tickers = _extract_tickers(tickers_section)
        ticker_directions = _extract_ticker_directions(tickers_section)

    return ThesisDocument(
        thesis_id=thesis_id,
        title=title,
        core_thesis=core_thesis,
        tickers=tickers,
        ticker_directions=ticker_directions,
        invalidations=invalidations,
        path=path,
        short_belief=short_belief,
    )


def build_thesis_chunks(document: ThesisDocument) -> list[ThesisChunk]:
    tags_text = " ".join(document.tickers)
    chunks = [
        ThesisChunk(
            thesis_id=document.thesis_id,
            chunk_key="statement",
            chunk_kind="statement",
            chunk_text=f"{document.title}\n\n{document.core_thesis}".strip(),
            search_text=f"{document.title}\n\n{document.core_thesis}".strip(),
            tags_text=tags_text,
            tickers=document.tickers,
            sectors=[],
        )
    ]
    for index, invalidation in enumerate(document.invalidations, start=1):
        chunks.append(
            ThesisChunk(
                thesis_id=document.thesis_id,
                chunk_key=f"invalidation_{index}",
                chunk_kind="invalidation",
                chunk_text=invalidation,
                search_text=invalidation,
                tags_text=tags_text,
                tickers=document.tickers,
                sectors=[],
            )
        )
    return chunks


def load_all_thesis_chunks(theses_dir: Path) -> list[ThesisChunk]:
    chunks: list[ThesisChunk] = []
    for path in sorted(theses_dir.glob("thesis_*.md")):
        if is_i18n_sidecar(path):
            continue
        chunks.extend(build_thesis_chunks(parse_thesis_markdown(path)))
    return chunks


__all__ = [
    "ThesisChunk",
    "ThesisDocument",
    "build_thesis_chunks",
    "load_all_thesis_chunks",
    "parse_thesis_markdown",
]

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from src.i18n import canonical_markdown_id


STORY_TITLE_RE = re.compile(r"^# (?P<title>.+?)\s*$", re.MULTILINE)
SECTION_RE = re.compile(r"^## (?P<name>.+?)\s*$", re.MULTILINE)
CITATION_RE = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")
SOURCE_TAG_RE = re.compile(r"\s*\*\([^)]+\)\*")


@dataclass(slots=True)
class StoryDocument:
    story_id: str
    title: str
    overview_bullets: list[str]
    path: Path

    @property
    def query_text(self) -> str:
        parts = [self.title]
        if self.overview_bullets:
            parts.append("\n".join(f"- {bullet}" for bullet in self.overview_bullets))
        return "\n\n".join(parts).strip()


def _extract_sections(markdown: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(markdown))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections[match.group("name").strip()] = markdown[start:end].strip()
    return sections


def _clean_bullet(text: str) -> str:
    cleaned = SOURCE_TAG_RE.sub("", text)
    cleaned = CITATION_RE.sub("", cleaned)
    return cleaned.strip()


def _extract_bullets(section_text: str) -> list[str]:
    bullets: list[str] = []
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            cleaned = _clean_bullet(line[2:])
            if cleaned:
                bullets.append(cleaned)
    return bullets


def parse_story_markdown(path: Path) -> StoryDocument:
    markdown = path.read_text(encoding="utf-8")

    title_match = STORY_TITLE_RE.search(markdown)
    if not title_match:
        raise ValueError(f"Missing story title in {path}")

    sections = _extract_sections(markdown)
    overview_bullets = _extract_bullets(sections.get("Overview", ""))
    if not overview_bullets:
        raise ValueError(f"Missing Overview bullets in {path}")

    return StoryDocument(
        story_id=canonical_markdown_id(path),
        title=title_match.group("title").strip(),
        overview_bullets=overview_bullets,
        path=path,
    )


__all__ = [
    "StoryDocument",
    "parse_story_markdown",
]

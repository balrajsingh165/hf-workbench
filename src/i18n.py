from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_LANGUAGES = frozenset({"en", "zh-Hans", "zh-Hant"})
TRANSLATED_LANGUAGES = frozenset({"zh-Hans", "zh-Hant"})

_LANGUAGE_ALIASES = {
    "en": "en",
    "en-us": "en",
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-hant": "zh-Hant",
}

_SIDECAR_STEM_RE = re.compile(r"^(?P<entity>.+)\.(?P<language>zh-Hans|zh-Hant)$")


def normalize_language(raw: str | None) -> str:
    if raw is None:
        return "en"
    key = str(raw).strip().lower().replace("_", "-")
    return _LANGUAGE_ALIASES.get(key, "en")


def language_name(language: str | None) -> str:
    normalized = normalize_language(language)
    if normalized == "zh-Hans":
        return "Simplified Chinese"
    if normalized == "zh-Hant":
        return "Traditional Chinese"
    return "English"


def localized_markdown_path(
    directory: Path,
    entity_id: str,
    language: str | None,
) -> tuple[Path, str]:
    normalized = normalize_language(language)
    base = directory / f"{entity_id}.md"
    if normalized in TRANSLATED_LANGUAGES:
        sidecar = directory / f"{entity_id}.{normalized}.md"
        if sidecar.is_file():
            return sidecar, normalized
    return base, "en"


def is_i18n_sidecar(path: Path) -> bool:
    return _SIDECAR_STEM_RE.match(path.stem) is not None


def canonical_markdown_id(path: Path) -> str:
    match = _SIDECAR_STEM_RE.match(path.stem)
    return match.group("entity") if match else path.stem


def markdown_path_language(path: Path) -> str:
    match = _SIDECAR_STEM_RE.match(path.stem)
    return match.group("language") if match else "en"


def load_glossary() -> str:
    path = ROOT / "global" / "i18n" / "glossary.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return ""

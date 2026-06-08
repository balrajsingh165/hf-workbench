from __future__ import annotations

from types import SimpleNamespace

from src.interfaces.ai_sdk_compat import api as ai_sdk_api


def test_hydrate_story_keeps_full_markdown_body(monkeypatch, tmp_path) -> None:
    root = tmp_path
    story_dir = root / "global" / "stories"
    story_dir.mkdir(parents=True)
    long_body = "Full body sentence. " * 300
    (story_dir / "story_full.md").write_text(
        "# Full story title\n\n" + long_body,
        encoding="utf-8",
    )

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=()):
            if "FROM story WHERE id" in query:
                return SimpleNamespace(
                    fetchone=lambda: {
                        "id": "story_full",
                        "headline": "Full story title",
                        "created_at": "2026-05-19T00:00:00Z",
                    }
                )
            if "FROM thesis_story_links" in query:
                return SimpleNamespace(fetchall=lambda: [])
            raise AssertionError(query)

    monkeypatch.setattr(ai_sdk_api, "ROOT", root, raising=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "api",
        SimpleNamespace(
            ROOT=root,
            db=lambda: _Conn(),
            first_sentence=lambda text: text.split(".", 1)[0],
            load_thesis_md=lambda thesis_id: None,
        ),
    )

    story = ai_sdk_api._hydrate_story("story_full", "user_1")

    assert story.headline == "Full story title"
    assert story.body == long_body.strip()
    assert "truncated:" not in story.body

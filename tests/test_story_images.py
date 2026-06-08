from __future__ import annotations

import hashlib
import io

from PIL import Image

import src.news.story_images as story_images
from src.news.types import ClusterSourceDoc


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        content: bytes = b"",
        url: str = "https://source.test/article",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.content = content
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}


class FakeUpload:
    def __init__(self, *, key: str, body: bytes) -> None:
        self.key = key
        self.url = f"https://cdn.test/{key}"
        self.size_bytes = len(body)


def _image_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (40, 50, 60)).save(buf, format="PNG")
    return buf.getvalue()


def test_fetch_story_images_respects_disable_env(monkeypatch) -> None:
    monkeypatch.setenv("HF_DISABLE_STORY_IMAGES", "1")
    monkeypatch.setattr(story_images, "r2_configured", lambda cfg: True)

    def fail_get(*args, **kwargs):
        raise AssertionError("network should not be called when disabled")

    monkeypatch.setattr(story_images.curl_requests, "get", fail_get)

    out = story_images.fetch_story_images(
        "story_test",
        [ClusterSourceDoc("news_1", "Title", "https://source.test/a", "P", "")],
        "news_1",
    )

    assert out == []


def test_fetch_story_images_keeps_centroid_first_and_dedupes(monkeypatch) -> None:
    monkeypatch.delenv("HF_DISABLE_STORY_IMAGES", raising=False)
    monkeypatch.setattr(story_images, "r2_configured", lambda cfg: True)

    img = _image_bytes()

    def fake_get(url: str, **kwargs):
        if url == "https://source.test/a":
            return FakeResponse(
                url=url,
                text='<meta property="og:image" content="/shared.png">',
            )
        if url == "https://source.test/b":
            return FakeResponse(
                url=url,
                text='<meta property="og:image" content="/centroid.png">',
            )
        if url == "https://source.test/c":
            return FakeResponse(
                url=url,
                text='<meta property="og:image" content="/centroid.png">',
            )
        return FakeResponse(url=url, content=img)

    uploaded_keys: list[str] = []

    def fake_upload(cfg, *, key: str, body: bytes, content_type: str) -> FakeUpload:
        uploaded_keys.append(key)
        return FakeUpload(key=key, body=body)

    monkeypatch.setattr(story_images.curl_requests, "get", fake_get)
    monkeypatch.setattr(story_images, "upload_r2_object", fake_upload)

    out = story_images.fetch_story_images(
        "story_unit",
        [
            ClusterSourceDoc("news_a", "A", "https://source.test/a", "P", ""),
            ClusterSourceDoc("news_b", "B", "https://source.test/b", "P", ""),
            ClusterSourceDoc("news_c", "C", "https://source.test/c", "P", ""),
        ],
        "news_b",
    )

    assert [image["sourceUrl"] for image in out] == [
        "https://source.test/b",
        "https://source.test/a",
    ]
    # 6 variants per image: {small, medium} × {avif, webp, jpg}. The exact
    # ordering matters less than the set; the manifest stores all 6 so the
    # client `<picture>` can pick AVIF when supported and fall back to WebP
    # or JPEG otherwise.
    for image in out:
        sizes_by_mime = {(v["size"], v["mime"]) for v in image["variants"]}
        assert ("small", "image/jpeg") in sizes_by_mime
        assert ("small", "image/webp") in sizes_by_mime
        assert ("medium", "image/jpeg") in sizes_by_mime
        assert ("medium", "image/webp") in sizes_by_mime
    # Keys are content-addressed by sha256(source_bytes)[:16] so identical
    # source content collapses to one R2 path regardless of story_id/idx —
    # this prevents positional-key collisions across DB rebuilds / cluster
    # re-synth. The two distinct sources in this test both fetch the same
    # `img` bytes via fake_get, so they share a prefix.
    prefix = hashlib.sha256(img).hexdigest()[:16]
    for key in (
        f"news-images/{prefix}-small.jpg",
        f"news-images/{prefix}-medium.jpg",
    ):
        assert key in uploaded_keys


def test_fetch_story_images_skips_oversized_images_before_variants(monkeypatch) -> None:
    monkeypatch.delenv("HF_DISABLE_STORY_IMAGES", raising=False)
    monkeypatch.setattr(story_images, "r2_configured", lambda cfg: True)

    def fake_get(url: str, **kwargs):
        if url == "https://source.test/a":
            return FakeResponse(
                url=url,
                text='<meta property="og:image" content="/large.png">',
            )
        return FakeResponse(
            url=url,
            headers={"content-length": str(story_images.MAX_IMAGE_BYTES + 1)},
        )

    def fail_variants(*args, **kwargs):
        raise AssertionError("oversized images should not reach PIL")

    monkeypatch.setattr(story_images.curl_requests, "get", fake_get)
    monkeypatch.setattr(story_images, "make_news_image_variants", fail_variants)

    out = story_images.fetch_story_images(
        "story_unit",
        [ClusterSourceDoc("news_a", "A", "https://source.test/a", "P", "")],
        "news_a",
    )

    assert out == []

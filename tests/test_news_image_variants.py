from __future__ import annotations

import io
import random

from PIL import Image, features

from src.news.image_variants import MEDIUM_WIDTH, SMALL_WIDTH, make_news_image_variants


def _jpeg(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(40, 40, 40))
    for x in range(width):
        for y in range(height):
            if (x // 16 + y // 16) % 2 == 0:
                img.putpixel((x, y), (192, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(40, 40, 40))
    rng = random.Random(7)
    for x in range(width):
        for y in range(height):
            img.putpixel((x, y), (rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _by_size_and_mime(variants):
    return {(v.size, v.mime): v for v in variants}


def test_news_image_variants_emit_avif_webp_jpeg_for_both_sizes():
    variants = make_news_image_variants(_jpeg(1000, 600))
    by_key = _by_size_and_mime(variants)
    # JPEG + WebP are always emitted. AVIF only when Pillow was built with
    # libavif — accept both shapes.
    expected_required = {
        ("small", "image/jpeg"),
        ("small", "image/webp"),
        ("medium", "image/jpeg"),
        ("medium", "image/webp"),
    }
    assert expected_required.issubset(by_key.keys())
    if features.check("avif"):
        assert ("small", "image/avif") in by_key
        assert ("medium", "image/avif") in by_key


def test_news_image_variants_preserve_aspect_ratio_per_size():
    variants = make_news_image_variants(_jpeg(1000, 600))
    by_key = _by_size_and_mime(variants)
    small = by_key[("small", "image/jpeg")]
    medium = by_key[("medium", "image/jpeg")]
    assert small.width == SMALL_WIDTH
    assert small.height == round(600 * SMALL_WIDTH / 1000)
    assert medium.width == MEDIUM_WIDTH
    assert medium.height == round(600 * MEDIUM_WIDTH / 1000)


def test_news_image_variants_jpeg_fallback_is_valid_jpeg_and_smaller():
    source = _png(1000, 600)
    variants = make_news_image_variants(source)
    by_key = _by_size_and_mime(variants)
    for size in ("small", "medium"):
        jpeg = by_key[(size, "image/jpeg")]
        assert jpeg.ext == "jpg"
        assert jpeg.body[:3] == b"\xff\xd8\xff"
        assert len(jpeg.body) < len(source)


def test_news_image_variants_webp_is_smaller_than_jpeg_at_matching_size():
    """WebP q=75 should beat JPEG q=78 on typical photographic content — this
    is what justifies the extra encode/upload cost."""
    variants = make_news_image_variants(_jpeg(1000, 600))
    by_key = _by_size_and_mime(variants)
    for size in ("small", "medium"):
        assert (
            len(by_key[(size, "image/webp")].body)
            < len(by_key[(size, "image/jpeg")].body)
        )


def test_news_image_variants_do_not_upscale():
    """A 120×80 source should round-trip to 120×80 for every format & size."""
    variants = make_news_image_variants(_jpeg(120, 80))
    for variant in variants:
        assert variant.width == 120
        assert variant.height == 80

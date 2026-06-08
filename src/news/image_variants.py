"""Compressed small/medium variants for downloaded news images.

Each source image fans out to 6 derived variants — {small, medium} × {avif,
webp, jpg}. The card surface and `<picture>` element pick the best format
per client (AVIF in Chrome/Safari/Firefox, WebP universally, JPEG as the
last-resort fallback inside `<img src>`).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

SMALL_WIDTH = 250
MEDIUM_WIDTH = 600

JPEG_QUALITY = 78
WEBP_QUALITY = 75
# AVIF q ≈ -10 vs JPEG q for similar perceptual quality. q=50 in Pillow's
# AVIF encoder (libavif) generally lands 35–50% smaller than JPEG q=78 with
# no visible loss on photographic news imagery.
AVIF_QUALITY = 50


@dataclass(frozen=True)
class ImageVariant:
    size: str  # "small" | "medium"
    width: int
    height: int
    body: bytes
    mime: str
    ext: str


def _resize_to_width(img: Image.Image, target_width: int) -> Image.Image:
    if img.width <= target_width:
        return img.copy()
    ratio = target_width / float(img.width)
    target_height = max(1, round(img.height * ratio))
    return img.resize((target_width, target_height), Image.Resampling.LANCZOS)


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or ("transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        return bg.convert("RGB")
    return img.convert("RGB")


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(
        buf,
        format="JPEG",
        quality=JPEG_QUALITY,
        optimize=True,
        progressive=True,
    )
    return buf.getvalue()


def _encode_webp(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6)
    return buf.getvalue()


def _encode_avif(img: Image.Image) -> bytes | None:
    """Encode AVIF, returning None if Pillow's AVIF support isn't compiled in.

    Pillow ≥ 11 ships AVIF support via libavif, but the wheel for the host
    platform might be built without it. Caller treats `None` as "skip AVIF"
    rather than failing the whole upload.
    """
    try:
        buf = io.BytesIO()
        # speed=6 is the libavif default trade-off between encode time and
        # compression ratio. quality=50 ≈ JPEG q78 visually.
        img.save(buf, format="AVIF", quality=AVIF_QUALITY, speed=6)
        return buf.getvalue()
    except (OSError, KeyError, ValueError):
        # OSError: encoder missing. KeyError: format not registered.
        # ValueError: invalid param on an old libavif build.
        return None


def make_news_image_variants(image_bytes: bytes) -> list[ImageVariant]:
    """Return compressed [small × avif/webp/jpg, medium × avif/webp/jpg] variants.

    AVIF is best-effort — if Pillow on the host wasn't built with libavif the
    AVIF variants are silently dropped and only WebP + JPEG ship. JPEG is
    always emitted so `<img src>` fallbacks keep working.
    """
    src = Image.open(io.BytesIO(image_bytes))
    src.load()
    src_rgb = _flatten_to_rgb(src)

    small = _resize_to_width(src_rgb, SMALL_WIDTH)
    medium = _resize_to_width(src_rgb, MEDIUM_WIDTH)

    out: list[ImageVariant] = []
    for size_name, img in (("small", small), ("medium", medium)):
        avif = _encode_avif(img)
        if avif is not None:
            out.append(
                ImageVariant(size_name, img.width, img.height, avif, "image/avif", "avif")
            )
        out.append(
            ImageVariant(size_name, img.width, img.height, _encode_webp(img), "image/webp", "webp")
        )
        out.append(
            ImageVariant(size_name, img.width, img.height, _encode_jpeg(img), "image/jpeg", "jpg")
        )
    return out


__all__ = [
    "AVIF_QUALITY",
    "ImageVariant",
    "JPEG_QUALITY",
    "MEDIUM_WIDTH",
    "SMALL_WIDTH",
    "WEBP_QUALITY",
    "make_news_image_variants",
]

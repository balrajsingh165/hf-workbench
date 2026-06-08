"""Story-level image ingestion.

Images belong to synthesized stories, not raw news rows. The first returned
image is anchored to the cluster centroid when that source exposes an image.
"""

from __future__ import annotations

import hashlib
import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from urllib.parse import urljoin

from curl_cffi import requests as curl_requests

from src.agent.config import get_agent_config
from src.agent.r2_storage import r2_configured, upload_r2_object
from src.news.image_variants import make_news_image_variants
from src.news.types import ClusterSourceDoc


FETCH_TIMEOUT_S = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
IMAGE_ROOT = "news-images"
logger = logging.getLogger("hf.scheduler")

_OG_IMAGE_RE = re.compile(
    r"""<meta\b[^>]*(?:property|name)\s*=\s*["'](?:og:image|og:image:url|twitter:image)["'][^>]*\bcontent\s*=\s*["']([^"']+)["'][^>]*>""",
    re.IGNORECASE,
)
_OG_IMAGE_RE_ALT = re.compile(
    r"""<meta\b[^>]*\bcontent\s*=\s*["']([^"']+)["'][^>]*(?:property|name)\s*=\s*["'](?:og:image|og:image:url|twitter:image)["'][^>]*>""",
    re.IGNORECASE,
)


def _disabled() -> bool:
    return os.environ.get("HF_DISABLE_STORY_IMAGES") == "1"


def _ordered_members(
    members: list[ClusterSourceDoc],
    centroid_news_id: str,
) -> list[ClusterSourceDoc]:
    return sorted(members, key=lambda m: 0 if m.news_id == centroid_news_id else 1)


def _extract_image_url(member: ClusterSourceDoc, idx: int) -> tuple[int, str, str] | None:
    if not member.url:
        return None
    try:
        resp = curl_requests.get(
            member.url,
            impersonate="chrome",
            timeout=FETCH_TIMEOUT_S,
            allow_redirects=True,
        )
    except Exception as exc:
        logger.warning(
            "story_images: og fetch failed news_id=%s url=%s err=%s",
            member.news_id,
            member.url,
            exc,
        )
        return None
    if resp.status_code != 200 or not resp.text:
        logger.warning(
            "story_images: og fetch empty news_id=%s status=%s",
            member.news_id,
            resp.status_code,
        )
        return None
    match = _OG_IMAGE_RE.search(resp.text) or _OG_IMAGE_RE_ALT.search(resp.text)
    if not match:
        logger.info("story_images: no og image news_id=%s", member.news_id)
        return None
    image_url = urljoin(str(resp.url or member.url), unescape(match.group(1)).strip())
    if not image_url.startswith(("http://", "https://")):
        return None
    return idx, member.url, image_url


def _download_image_bytes(story_id: str, image_url: str) -> bytes | None:
    try:
        resp = curl_requests.get(
            image_url,
            impersonate="chrome",
            timeout=FETCH_TIMEOUT_S,
            allow_redirects=True,
            stream=True,
        )
    except Exception as exc:
        logger.warning(
            "story_images: image fetch failed story_id=%s url=%s err=%s",
            story_id,
            image_url,
            exc,
        )
        return None

    try:
        if resp.status_code != 200:
            logger.warning(
                "story_images: image fetch empty story_id=%s status=%s url=%s",
                story_id,
                resp.status_code,
                image_url,
            )
            return None
        content_length = getattr(resp, "headers", {}).get("content-length")
        if content_length and int(content_length) > MAX_IMAGE_BYTES:
            logger.warning(
                "story_images: image too large story_id=%s bytes=%s max_bytes=%s url=%s",
                story_id,
                content_length,
                MAX_IMAGE_BYTES,
                image_url,
            )
            return None

        chunks: list[bytes] = []
        total = 0
        iter_content = getattr(resp, "iter_content", None)
        if callable(iter_content):
            iterator = iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES)
        else:
            iterator = [getattr(resp, "content", b"")]
        for chunk in iterator:
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                logger.warning(
                    "story_images: image exceeded max size story_id=%s bytes>%s url=%s",
                    story_id,
                    MAX_IMAGE_BYTES,
                    image_url,
                )
                return None
            chunks.append(chunk)
        body = b"".join(chunks)
        if not body:
            logger.warning(
                "story_images: image fetch empty story_id=%s status=%s url=%s",
                story_id,
                resp.status_code,
                image_url,
            )
            return None
        return body
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()


def _upload_image(
    *,
    story_id: str,
    idx: int,
    source_url: str,
    image_url: str,
) -> tuple[int, dict] | None:
    cfg = get_agent_config()
    try:
        image_bytes = _download_image_bytes(story_id, image_url)
        if image_bytes is None:
            return None
        # Content-addressed key prefix: derived from the source image bytes so
        # the 6 variants for one image always share the same prefix, and the
        # same source content uploads idempotently. Positional keys (story_id
        # + idx) collide when story_ids are recycled (DB rebuilds, cluster
        # re-synth), leaving R2 in a mixed state where AVIF/WebP/JPEG variants
        # of the "same" image surface different content.
        content_prefix = hashlib.sha256(image_bytes).hexdigest()[:16]
        variants = make_news_image_variants(image_bytes)
        variant_refs: list[dict] = []
        for variant in variants:
            key = f"{IMAGE_ROOT}/{content_prefix}-{variant.size}.{variant.ext}"
            uploaded = upload_r2_object(
                cfg,
                key=key,
                body=variant.body,
                content_type=variant.mime,
            )
            variant_refs.append({
                "size": variant.size,
                "url": uploaded.url,
                "key": uploaded.key,
                "width": variant.width,
                "height": variant.height,
                "mime": variant.mime,
                "sizeBytes": uploaded.size_bytes,
            })
        logger.info(
            "story_images: uploaded story_id=%s idx=%s source=%s",
            story_id,
            idx,
            source_url,
        )
        return idx, {"sourceUrl": source_url, "variants": variant_refs}
    except Exception as exc:
        logger.warning(
            "story_images: image upload failed story_id=%s idx=%s url=%s err=%s",
            story_id,
            idx,
            image_url,
            exc,
        )
        return None


def fetch_story_images(
    story_id: str,
    members: list[ClusterSourceDoc],
    centroid_news_id: str,
) -> list[dict]:
    """Fetch, variant, upload, and return story image refs.

    Returns [] on global disable / missing R2 config / total failure. Partial
    success is expected at this network boundary.
    """
    cfg = get_agent_config()
    if _disabled():
        logger.info("story_images: disabled story_id=%s", story_id)
        return []
    if not r2_configured(cfg):
        logger.info("story_images: r2 not configured story_id=%s", story_id)
        return []

    ordered = _ordered_members(members, centroid_news_id)
    if not ordered:
        return []

    candidates: list[tuple[int, str, str]] = []
    with ThreadPoolExecutor(max_workers=min(3, len(ordered))) as pool:
        futures = {
            pool.submit(_extract_image_url, member, idx): member
            for idx, member in enumerate(ordered)
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                candidates.append(result)

    seen_image_urls: set[str] = set()
    unique_candidates: list[tuple[int, str, str]] = []
    for idx, source_url, image_url in sorted(candidates, key=lambda item: item[0]):
        dedupe_key = image_url.split("#", 1)[0]
        if dedupe_key in seen_image_urls:
            continue
        seen_image_urls.add(dedupe_key)
        unique_candidates.append((len(unique_candidates), source_url, image_url))

    if not unique_candidates:
        return []

    uploaded: list[tuple[int, dict]] = []
    with ThreadPoolExecutor(max_workers=min(3, len(unique_candidates))) as pool:
        futures = [
            pool.submit(
                _upload_image,
                story_id=story_id,
                idx=idx,
                source_url=source_url,
                image_url=image_url,
            )
            for idx, source_url, image_url in unique_candidates
        ]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                uploaded.append(result)

    return [item for _, item in sorted(uploaded, key=lambda pair: pair[0])]


__all__ = ["fetch_story_images"]

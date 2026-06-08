"""Cloudflare R2 uploader for public artifacts.

R2 is S3-compatible — we point boto3's S3 client at the R2 endpoint and use
the same `put_object` call. The bucket is publicly readable via the R2 dev
URL, so we return `{R2_PUBLIC_BASE_URL}/{key}` as the persisted URL.

Sync boto3 calls are wrapped in `asyncio.to_thread` at the call site so they
don't block event loops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3
from botocore.client import Config

from src.agent.config import AgentConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class R2Upload:
    url: str
    key: str
    size_bytes: int


def r2_configured(cfg: AgentConfig) -> bool:
    return all(
        [cfg.r2_endpoint, cfg.r2_bucket, cfg.r2_access_key, cfg.r2_secret_key, cfg.r2_public_base_url]
    )


def _make_client(cfg: AgentConfig):
    # `region_name="auto"` is the R2 convention; the endpoint URL is what
    # actually routes the request. SigV4 is required.
    return boto3.client(
        "s3",
        endpoint_url=cfg.r2_endpoint,
        aws_access_key_id=cfg.r2_access_key,
        aws_secret_access_key=cfg.r2_secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def upload_r2_object(
    cfg: AgentConfig,
    *,
    key: str,
    body: bytes,
    content_type: str,
) -> R2Upload:
    """Upload bytes to R2 and return the public URL.

    Caller is responsible for picking a unique key. Sets
    `Cache-Control: public, max-age=..., immutable` so Cloudflare's edge can
    cache aggressively; artifact keys are content/request-scoped and not
    overwritten.
    """
    if not r2_configured(cfg):
        raise RuntimeError("R2 is not configured (R2_ENDPOINT/R2_BUCKET/keys/PUBLIC_BASE_URL)")

    client = _make_client(cfg)
    client.put_object(
        Bucket=cfg.r2_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
    )
    url = f"{cfg.r2_public_base_url}/{key}"
    logger.info("r2 upload ok: bucket=%s key=%s size=%d url=%s", cfg.r2_bucket, key, len(body), url)
    return R2Upload(url=url, key=key, size_bytes=len(body))


def upload_chart(
    cfg: AgentConfig,
    *,
    key: str,
    body: bytes,
    content_type: str = "image/png",
) -> R2Upload:
    """Upload chart bytes to R2 and return the public URL."""
    return upload_r2_object(cfg, key=key, body=body, content_type=content_type)


__all__ = ["R2Upload", "r2_configured", "upload_chart", "upload_r2_object"]

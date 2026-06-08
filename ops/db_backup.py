"""Standalone SQLite backup to local disk and Cloudflare R2.

No imports from src/ or agents/. Used by ops.db_backup_scheduler (pm2).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from botocore.client import Config
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "db" / "hf.db"
DEFAULT_LOCAL_DIR = ROOT / "backups" / "db"
DEFAULT_R2_PREFIX = "backups/db"
DEFAULT_RETENTION_DAYS = 7
SUCCESS_MARKER = ROOT / "logs" / ".db_backup_last_success"

logger = logging.getLogger("hf.db_backup")


@dataclass(frozen=True)
class BackupConfig:
    db_path: Path
    local_dir: Path
    r2_prefix: str
    retention_days: int
    r2_endpoint: str
    r2_bucket: str
    r2_access_key: str
    r2_secret_key: str


@dataclass(frozen=True)
class BackupResult:
    backup_name: str
    local_path: Path
    r2_key: str
    size_bytes: int
    uploaded: bool
    local_deleted: int
    r2_deleted: int


def load_env() -> None:
    load_dotenv(ROOT / ".env")


def config_from_env() -> BackupConfig:
    load_env()
    missing = [
        name
        for name in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY", "R2_SECRET_KEY")
        if not (os.getenv(name) or "").strip()
    ]
    if missing:
        raise RuntimeError(f"R2 is not configured (missing: {', '.join(missing)})")

    return BackupConfig(
        db_path=Path(os.getenv("HF_DB_PATH", str(DEFAULT_DB_PATH))).resolve(),
        local_dir=Path(os.getenv("DB_BACKUP_LOCAL_DIR", str(DEFAULT_LOCAL_DIR))).resolve(),
        r2_prefix=(os.getenv("DB_BACKUP_R2_PREFIX", DEFAULT_R2_PREFIX) or DEFAULT_R2_PREFIX).strip("/"),
        retention_days=int(os.getenv("DB_BACKUP_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))),
        r2_endpoint=os.environ["R2_ENDPOINT"].strip(),
        r2_bucket=os.environ["R2_BUCKET"].strip(),
        r2_access_key=os.environ["R2_ACCESS_KEY"].strip(),
        r2_secret_key=os.environ["R2_SECRET_KEY"].strip(),
    )


def backup_name_for(now: datetime | None = None) -> str:
    ts = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H%M%SZ")
    return f"hf-{ts}.db"


def sqlite_backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dest_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()


def _r2_client(cfg: BackupConfig):
    return boto3.client(
        "s3",
        endpoint_url=cfg.r2_endpoint,
        aws_access_key_id=cfg.r2_access_key,
        aws_secret_access_key=cfg.r2_secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def upload_backup(cfg: BackupConfig, local_path: Path, backup_name: str) -> str:
    key = f"{cfg.r2_prefix}/{backup_name}"
    client = _r2_client(cfg)
    body = local_path.read_bytes()
    client.put_object(
        Bucket=cfg.r2_bucket,
        Key=key,
        Body=body,
        ContentType="application/x-sqlite3",
    )
    logger.info(
        "uploaded backup bucket=%s key=%s size=%d",
        cfg.r2_bucket,
        key,
        len(body),
    )
    return key


def _cutoff(retention_days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=retention_days)


def prune_local_backups(cfg: BackupConfig, *, now: datetime | None = None) -> int:
    cutoff = _cutoff(cfg.retention_days, now)
    deleted = 0
    if not cfg.local_dir.is_dir():
        return deleted

    for path in sorted(cfg.local_dir.glob("hf-*.db")):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
            path.unlink(missing_ok=True)
            deleted += 1
            logger.info("deleted local backup path=%s mtime=%s", path, mtime.isoformat())
    return deleted


def prune_r2_backups(cfg: BackupConfig, *, now: datetime | None = None) -> int:
    cutoff = _cutoff(cfg.retention_days, now)
    client = _r2_client(cfg)
    paginator = client.get_paginator("list_objects_v2")
    deleted = 0

    for page in paginator.paginate(Bucket=cfg.r2_bucket, Prefix=f"{cfg.r2_prefix}/"):
        contents = page.get("Contents") or []
        for obj in contents:
            key = obj["Key"]
            if not key.endswith(".db"):
                continue
            last_modified = obj["LastModified"]
            if last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=UTC)
            if last_modified < cutoff:
                client.delete_object(Bucket=cfg.r2_bucket, Key=key)
                deleted += 1
                logger.info(
                    "deleted r2 backup key=%s last_modified=%s",
                    key,
                    last_modified.isoformat(),
                )
    return deleted


def write_success_marker(result: BackupResult) -> None:
    SUCCESS_MARKER.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": datetime.now(UTC).isoformat(),
        "backup_name": result.backup_name,
        "local_path": str(result.local_path),
        "r2_key": result.r2_key,
        "size_bytes": result.size_bytes,
        "local_deleted": result.local_deleted,
        "r2_deleted": result.r2_deleted,
    }
    SUCCESS_MARKER.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def run_backup(cfg: BackupConfig, *, now: datetime | None = None) -> BackupResult:
    name = backup_name_for(now)
    local_path = cfg.local_dir / name

    logger.info("starting backup db=%s local=%s", cfg.db_path, local_path)
    sqlite_backup(cfg.db_path, local_path)
    size_bytes = local_path.stat().st_size
    logger.info("sqlite backup complete size=%d", size_bytes)

    r2_key = upload_backup(cfg, local_path, name)
    local_deleted = prune_local_backups(cfg, now=now)
    r2_deleted = prune_r2_backups(cfg, now=now)

    result = BackupResult(
        backup_name=name,
        local_path=local_path,
        r2_key=r2_key,
        size_bytes=size_bytes,
        uploaded=True,
        local_deleted=local_deleted,
        r2_deleted=r2_deleted,
    )
    write_success_marker(result)
    logger.info(
        "backup complete name=%s r2_key=%s pruned local=%d r2=%d",
        name,
        r2_key,
        local_deleted,
        r2_deleted,
    )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backup hf.db locally and to Cloudflare R2.")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help=f"SQLite source path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=None,
        help=f"Local backup directory (default: {DEFAULT_LOCAL_DIR})",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help=f"Delete backups older than N days (default: {DEFAULT_RETENTION_DAYS})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args(argv)
    cfg = config_from_env()
    if args.db_path is not None:
        cfg = BackupConfig(**{**cfg.__dict__, "db_path": args.db_path.resolve()})
    if args.local_dir is not None:
        cfg = BackupConfig(**{**cfg.__dict__, "local_dir": args.local_dir.resolve()})
    if args.retention_days is not None:
        cfg = BackupConfig(**{**cfg.__dict__, "retention_days": args.retention_days})

    try:
        run_backup(cfg)
    except Exception:
        logger.exception("backup failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

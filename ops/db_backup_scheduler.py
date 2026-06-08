"""Long-lived daily DB backup scheduler (pm2 `hf-db-backup`).

Runs ops.db_backup once per day and keeps local/R2 retention at one week.
Standalone — no imports from src/ or agents/.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ops.db_backup import BackupConfig, config_from_env, run_backup

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "hf-db-backup.log"

logger = logging.getLogger("hf.db_backup.scheduler")


def backup_enabled() -> bool:
    if os.getenv("DB_BACKUP_DISABLED", "").lower() in {"1", "true", "yes"}:
        return False
    if os.getenv("HF_AGENT_PROTOCOL_SMOKE", "").lower() in {"1", "true", "yes"}:
        return False
    return True


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream, file_handler], force=True)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


def schedule_hour() -> int:
    return int(os.getenv("DB_BACKUP_HOUR_UTC", "4"))


def schedule_minute() -> int:
    return int(os.getenv("DB_BACKUP_MINUTE_UTC", "0"))


def create_scheduler(cfg: BackupConfig, *, run_initial: bool) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    hour = schedule_hour()
    minute = schedule_minute()
    scheduler.add_job(
        run_backup,
        kwargs={"cfg": cfg},
        trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        id="hf_db_backup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "registered hf_db_backup cron hour=%02d:%02d UTC retention_days=%s",
        hour,
        minute,
        cfg.retention_days,
    )
    if run_initial:
        scheduler.add_job(
            run_backup,
            kwargs={"cfg": cfg},
            id="hf_db_backup_boot",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("scheduled immediate boot backup")
    return scheduler


async def scheduler_main(*, once: bool, run_initial: bool) -> None:
    cfg = config_from_env()
    scheduler = create_scheduler(cfg, run_initial=run_initial and not once)
    scheduler.start()

    if once:
        await asyncio.to_thread(run_backup, cfg)
        scheduler.shutdown(wait=False)
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_stop)

    await stop_event.wait()
    logger.info("shutdown requested")
    scheduler.shutdown(wait=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily hf.db backup to R2.")
    parser.add_argument("--once", action="store_true", help="Run one backup cycle and exit.")
    parser.add_argument(
        "--no-initial",
        action="store_true",
        help="Wait for the next scheduled run instead of backing up at startup.",
    )
    return parser.parse_args()


def main() -> None:
    setup_logging()
    if not backup_enabled():
        logger.info("db backup scheduler exiting (DB_BACKUP_DISABLED or HF_AGENT_PROTOCOL_SMOKE)")
        return

    args = parse_args()
    logger.info(
        "starting db backup scheduler once=%s run_initial=%s",
        args.once,
        not args.no_initial,
    )
    asyncio.run(scheduler_main(once=args.once, run_initial=not args.no_initial))


if __name__ == "__main__":
    main()

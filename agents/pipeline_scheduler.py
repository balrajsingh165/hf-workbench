"""Long-lived pipeline scheduler (pm2 `hf-pipeline`).

Wires APScheduler to `PipelineService` (src/pipeline/). Step subprocesses,
metrics parsing, and cooperative shutdown live there — this module is config,
logging, boot-once-per-session, and the asyncio entrypoint.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scripts.apply_news_rearchitecture_schema import apply as apply_news_schema
from src.pipeline.runner import SchedulerConfig
from src.pipeline.service import PipelineService

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "hf.db"
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "hf-scheduler.log"
BOOT_MARKER_PATH = LOG_DIR / ".pipeline_boot_marker"
SOCIAL_INTERVAL_HOURS = 24.0

logger = logging.getLogger("hf.scheduler")


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode()
    except OSError:
        return ""


def supervisor_session_id() -> int:
    """Stable ID for the current server process tree (survives uvicorn --reload)."""
    parent = os.getppid()
    parent_cmd = _proc_cmdline(parent)
    if "uvicorn" in parent_cmd and "--reload" in parent_cmd:
        return parent
    return os.getpid()


def should_run_boot_pipeline(*, marker_path: Path = BOOT_MARKER_PATH) -> bool:
    """Return True once per supervisor session; skip on uvicorn hot reload."""
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    session_id = supervisor_session_id()
    if marker_path.exists():
        try:
            recorded = int(marker_path.read_text().strip())
        except (ValueError, OSError):
            recorded = None
        if recorded == session_id:
            logger.info(
                "skipping boot pipeline session_id=%s (already ran this session)",
                session_id,
            )
            return False
    marker_path.write_text(str(session_id))
    logger.info("boot pipeline claimed session_id=%s", session_id)
    return True


def social_boot_due(
    *,
    interval_hours: float,
    db_path: Path = DB_PATH,
    now: datetime | None = None,
) -> bool:
    """Skip the boot social sweep when the lane ran within half its interval.

    Boot otherwise re-fetches every Tier-1 ticker on each pm2 restart (~$0.80
    in Grok spend) minutes after the last sweep. Last-run signal is `llm_calls`
    (stamped once per ticker per live run) rather than `story.created_at`,
    which refreshes leave untouched. Fails open: no signal or a broken DB
    means run the sweep. Forcing one regardless: `uv run python -m
    agents.social_topics`.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            row = conn.execute(
                "SELECT MAX(created_at) FROM llm_calls WHERE caller = 'social_topics'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return True
    if not row or not row[0]:
        return True
    try:
        last_run = datetime.strptime(str(row[0]), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return now - last_run >= timedelta(hours=interval_hours / 2)


def pipeline_enabled() -> bool:
    if os.getenv("HF_PIPELINE_DISABLED", "").lower() in {"1", "true", "yes"}:
        return False
    if os.getenv("HF_AGENT_PROTOCOL_SMOKE", "").lower() in {"1", "true", "yes"}:
        return False
    return True


def config_from_env() -> SchedulerConfig:
    return SchedulerConfig(
        interval_hours=float(os.getenv("HF_PIPELINE_INTERVAL_HOURS", "3")),
        top_stories=int(os.getenv("HF_PIPELINE_TOP_STORIES", "40")),
        route_cluster_limit=int(os.getenv("HF_PIPELINE_ROUTE_EVAL_LIMIT", "1200")),
        synth_workers=int(os.getenv("HF_PIPELINE_SYNTH_WORKERS", "6")),
        match_window_days=int(os.getenv("HF_PIPELINE_MATCH_WINDOW_DAYS", "30")),
        match_max_candidates=int(os.getenv("HF_PIPELINE_MATCH_MAX_CANDIDATES", "8")),
        match_min_score=float(os.getenv("HF_PIPELINE_MATCH_MIN_SCORE", "0.50")),
        match_max_workers=int(os.getenv("HF_PIPELINE_MATCH_MAX_WORKERS", "4")),
        no_images=os.getenv("HF_DISABLE_STORY_IMAGES", "").lower() in {"1", "true", "yes"},
        run_initial=os.getenv("HF_PIPELINE_NO_INITIAL", "").lower() not in {"1", "true", "yes"},
        firehose_interval_minutes=float(os.getenv("HF_FIREHOSE_INTERVAL_MINUTES", "10")),
        firehose_enabled=os.getenv("HF_FIREHOSE_DISABLED", "").lower() not in {"1", "true", "yes"},
        repromotion_interval_hours=float(os.getenv("HF_REPROMOTION_INTERVAL_HOURS", "6")),
        repromotion_max_stale_days=int(os.getenv("HF_REPROMOTION_MAX_STALE_DAYS", "30")),
        repromotion_enabled=os.getenv("HF_REPROMOTION_DISABLED", "").lower() not in {"1", "true", "yes"},
        trending_tier1_interval_days=float(os.getenv("HF_TRENDING_TIER1_INTERVAL_DAYS", "1")),
        trending_tier2_interval_days=float(os.getenv("HF_TRENDING_TIER2_INTERVAL_DAYS", "3")),
        social_interval_hours=SOCIAL_INTERVAL_HOURS,
    )


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
        maxBytes=20 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream, file_handler], force=True)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


def create_scheduler(service: PipelineService, config: SchedulerConfig) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        service.run_pipeline,
        trigger=IntervalTrigger(hours=config.interval_hours),
        id="hf_pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("registered hf_pipeline interval_hours=%s", config.interval_hours)
    if config.firehose_enabled and not config.once:
        scheduler.add_job(
            service.run_firehose,
            trigger=IntervalTrigger(minutes=config.firehose_interval_minutes),
            id="hf_firehose",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "registered hf_firehose interval_minutes=%s",
            config.firehose_interval_minutes,
        )
    if config.repromotion_enabled and not config.once:
        scheduler.add_job(
            service.run_repromotion,
            trigger=IntervalTrigger(hours=config.repromotion_interval_hours),
            id="hf_repromotion",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "registered hf_repromotion interval_hours=%s",
            config.repromotion_interval_hours,
        )
    if not config.once:
        # Two tiers, one job each: Tier 1 (hot, rank <= TIER1_MAX) daily,
        # Tier 2 (tail) every few days. Bind `tier` per job so the closure
        # doesn't capture the loop variable.
        for tier, days in (
            (1, config.trending_tier1_interval_days),
            (2, config.trending_tier2_interval_days),
        ):
            scheduler.add_job(
                service.run_trending,
                trigger=IntervalTrigger(days=days),
                args=[tier],
                id=f"hf_trending_t{tier}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("registered hf_trending_t%s interval_days=%s", tier, days)
        if config.social_enabled:
            scheduler.add_job(
                service.run_social_topics,
                trigger=IntervalTrigger(hours=config.social_interval_hours),
                id="hf_social_topics",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(
                "registered hf_social_topics interval_hours=%s",
                config.social_interval_hours,
            )
        else:
            logger.info(
                "social lane disabled (--no-social); run "
                "`uv run python -m agents.social_topics` manually"
            )
    return scheduler


async def run_boot(service: PipelineService, config: SchedulerConfig) -> None:
    # Retrieve trending-ticker news before routing so the boot pipeline can
    # promote it in the same cycle. Both tiers run once on boot/--once. The lane
    # swallows its own failures (returns an ok=false metric), so a broken scrape
    # just means no new rows here — firehose + pipeline still run below.
    await asyncio.to_thread(service.run_trending, 1)
    await asyncio.to_thread(service.run_trending, 2)
    if not config.social_enabled:
        logger.info("skipping boot social run (--no-social)")
    elif social_boot_due(interval_hours=config.social_interval_hours):
        await asyncio.to_thread(service.run_social_topics)
    else:
        logger.info(
            "skipping boot social run (lane ran within the last %sh)",
            config.social_interval_hours / 2,
        )
    if config.firehose_enabled:
        await asyncio.to_thread(service.run_firehose)
    await asyncio.to_thread(service.run_pipeline)
    if config.repromotion_enabled:
        await asyncio.to_thread(service.run_repromotion)


async def scheduler_main(config: SchedulerConfig) -> None:
    service = PipelineService(config, db_path=DB_PATH, root=ROOT)
    service.shutdown.clear()

    scheduler = create_scheduler(service, config)
    scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_stop() -> None:
        service.shutdown.request()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_stop)

    if config.once or (config.run_initial and should_run_boot_pipeline()):
        await run_boot(service, config)

    if config.once:
        scheduler.shutdown(wait=False)
        return

    await stop_event.wait()
    logger.info("shutdown requested")
    scheduler.shutdown(wait=False)


def parse_args() -> SchedulerConfig:
    parser = argparse.ArgumentParser(description="Run HF pipeline every N hours.")
    parser.add_argument("--interval-hours", type=float, default=3.0)
    parser.add_argument("--top-stories", type=int, default=40)
    parser.add_argument(
        "--route-eval-limit",
        type=int,
        default=1200,
        help="Clusters to route per cycle (promote-first pool size).",
    )
    parser.add_argument(
        "--synth-workers",
        type=int,
        default=6,
        help="Parallel story synthesis workers in route_news_clusters.",
    )
    parser.add_argument("--match-window-days", type=int, default=30)
    parser.add_argument("--match-max-candidates", type=int, default=8)
    parser.add_argument("--match-min-score", type=float, default=0.50)
    parser.add_argument("--match-max-workers", type=int, default=4)
    parser.add_argument("--no-images", action="store_true", help="Skip story image downloads during ingest.")
    parser.add_argument("--no-initial", action="store_true", help="Wait 12h before first run.")
    parser.add_argument("--once", action="store_true", help="Run one pipeline cycle and exit.")
    parser.add_argument(
        "--firehose-interval-minutes",
        type=float,
        default=10.0,
        help="Press-wire firehose poll cadence (default: 10).",
    )
    parser.add_argument(
        "--no-firehose",
        action="store_true",
        help="Disable the firehose job (story routing only).",
    )
    parser.add_argument(
        "--repromotion-interval-hours",
        type=float,
        default=6.0,
        help="Candidate re-promotion sweep cadence (default: 6).",
    )
    parser.add_argument(
        "--repromotion-max-stale-days",
        type=int,
        default=30,
        help="Reject candidates older than this with zero story links (default: 30).",
    )
    parser.add_argument(
        "--no-repromotion",
        action="store_true",
        help="Disable the candidate re-promotion sweep.",
    )
    parser.add_argument(
        "--trending-tier1-interval-days",
        type=float,
        default=1.0,
        help="Trending Tier-1 (hot) retrieval cadence in days (default: 1).",
    )
    parser.add_argument(
        "--trending-tier2-interval-days",
        type=float,
        default=3.0,
        help="Trending Tier-2 (tail) retrieval cadence in days (default: 3).",
    )
    parser.add_argument(
        "--social-interval-hours",
        type=float,
        default=SOCIAL_INTERVAL_HOURS,
        help="Social-topic refresh cadence in hours (default: 24).",
    )
    parser.add_argument(
        "--no-social",
        action="store_true",
        help="Disable scheduled social-topic sweeps (manual runs only).",
    )
    args = parser.parse_args()
    return SchedulerConfig(
        interval_hours=args.interval_hours,
        top_stories=args.top_stories,
        route_cluster_limit=args.route_eval_limit,
        synth_workers=args.synth_workers,
        match_window_days=args.match_window_days,
        match_max_candidates=args.match_max_candidates,
        match_min_score=args.match_min_score,
        match_max_workers=args.match_max_workers,
        no_images=args.no_images,
        run_initial=not args.no_initial,
        once=args.once,
        firehose_interval_minutes=args.firehose_interval_minutes,
        firehose_enabled=not args.no_firehose,
        repromotion_interval_hours=args.repromotion_interval_hours,
        repromotion_max_stale_days=args.repromotion_max_stale_days,
        repromotion_enabled=not args.no_repromotion,
        trending_tier1_interval_days=args.trending_tier1_interval_days,
        trending_tier2_interval_days=args.trending_tier2_interval_days,
        social_interval_hours=args.social_interval_hours,
        social_enabled=not args.no_social,
    )


def main() -> None:
    setup_logging()
    if not pipeline_enabled():
        logger.info(
            "pipeline scheduler exiting (HF_PIPELINE_DISABLED or HF_AGENT_PROTOCOL_SMOKE)"
        )
        return
    # Story persist INSERTs the llm_calls token/cost columns added in the news
    # rearchitecture. init_db() only creates them on a full (destructive)
    # rebuild, so apply the idempotent ALTERs here — otherwise a deploy against
    # an existing hf.db dies on the first persist with "no such column".
    apply_news_schema(DB_PATH)
    config = parse_args() if len(sys.argv) > 1 else config_from_env()
    logger.info("starting scheduler config=%s", config)
    asyncio.run(scheduler_main(config))


if __name__ == "__main__":
    main()

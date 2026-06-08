"""Facade used by the long-lived scheduler process."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from src.pipeline.runner import PipelineRunner, SchedulerConfig
from src.pipeline.shutdown import ShutdownController
from src.pipeline_metrics import append_metric, top_counts

logger = logging.getLogger("hf.scheduler")


class PipelineService:
    def __init__(
        self,
        config: SchedulerConfig,
        *,
        db_path: Path,
        root: Path,
        shutdown: ShutdownController | None = None,
    ) -> None:
        self.config = config
        self._db_path = db_path
        self._root = root
        self.shutdown = shutdown or ShutdownController()

    def run_pipeline(self) -> dict[str, Any]:
        return PipelineRunner(
            self.config,
            db_path=self._db_path,
            root=self._root,
            shutdown=self.shutdown,
        ).run()

    def run_repromotion(self) -> dict[str, Any]:
        """Re-run promotion gates over stuck candidate theses (independent job)."""
        from agents.repromote_candidates import repromote_candidates

        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        max_stale_days = self.config.repromotion_max_stale_days
        logger.info("repromotion run_id=%s start max_stale_days=%d", run_id, max_stale_days)
        try:
            sweep = repromote_candidates(
                self._root, self._db_path, max_stale_days=max_stale_days
            )
            ok = True
            error: str | None = None
        except Exception as exc:
            sweep = None
            ok = False
            error = repr(exc)
            logger.exception("repromotion run_id=%s failed", run_id)
        duration = round(time.perf_counter() - started, 3)
        metric: dict[str, Any] = {
            "event": "repromotion_run",
            "run_id": run_id,
            "ok": ok,
            "duration_s": duration,
            "max_stale_days": max_stale_days,
        }
        if sweep is not None:
            metric.update({
                "candidates_seen": sweep.candidates_seen,
                "promoted": sweep.promoted,
                "rejected": sweep.rejected,
                "still_candidate": sweep.still_candidate,
                "transitions": sweep.transitions,
            })
        if error is not None:
            metric["error"] = error
        append_metric(metric)
        if sweep is not None:
            logger.info(
                "repromotion run_id=%s finish ok=%s duration_s=%.3f "
                "seen=%d promoted=%d rejected=%d still=%d",
                run_id,
                ok,
                duration,
                sweep.candidates_seen,
                sweep.promoted,
                sweep.rejected,
                sweep.still_candidate,
            )
        return metric

    def run_trending(self, tier: int) -> dict[str, Any]:
        """Trending-ticker retrieval lane for one tier (1=daily, 2=every 3 days).

        Mirrors `run_firehose`: run_id, try/except, `trending_run` metric. The
        lane never raises (it returns ok/error/phase), but we still guard so an
        unexpected error can't take the scheduler thread down. A fetch/parse
        failure (ok=false) is the no-fallback-source critical signal the health
        check promotes to the /metrics page."""
        from agents.trending import run_trending

        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        logger.info("trending run_id=%s tier=%s start", run_id, tier)
        try:
            result = run_trending(
                tier,
                db_path=self._db_path,
                should_stop=lambda: self.shutdown.requested,
            )
            error: str | None = result.get("error")
        except Exception as exc:
            result = {"ok": False, "error": repr(exc), "phase": "fetch", "tier": tier}
            error = result["error"]
            logger.exception("trending run_id=%s tier=%s failed", run_id, tier)
        duration = round(time.perf_counter() - started, 3)
        metric: dict[str, Any] = {
            "event": "trending_run",
            "run_id": run_id,
            "source": "scheduler",
            "duration_s": duration,
            **result,
        }
        append_metric(metric)
        logger.info(
            "trending run_id=%s tier=%s finish ok=%s phase=%s due=%s inserted=%s scrape_errors=%s",
            run_id,
            tier,
            result.get("ok"),
            result.get("phase"),
            result.get("symbols_due"),
            result.get("inserted"),
            result.get("scrape_errors"),
        )
        return metric

    def run_social_topics(self) -> dict[str, Any]:
        """Social-topic ingestion lane. Best-effort and isolated."""
        from agents.social_topics import run_social_topics

        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        logger.info("social_topics run_id=%s start", run_id)
        try:
            result = run_social_topics(
                db_path=self._db_path,
                should_stop=lambda: self.shutdown.requested,
            )
        except Exception as exc:
            result = {"ok": False, "error": repr(exc), "phase": "unexpected"}
            logger.exception("social_topics run_id=%s failed", run_id)
        duration = round(time.perf_counter() - started, 3)
        result["duration_s"] = result.get("duration_s", duration)
        metric: dict[str, Any] = {
            "event": "social_run",
            "run_id": run_id,
            "source": "scheduler",
            **result,
        }
        append_metric(metric)
        logger.info(
            "social_topics run_id=%s finish ok=%s phase=%s selected=%s called=%s admitted=%s refreshed=%s",
            run_id,
            result.get("ok"),
            result.get("phase"),
            result.get("tickers_selected"),
            result.get("tickers_called"),
            result.get("topics_admitted"),
            result.get("topics_refreshed"),
        )
        return metric

    def run_firehose(self) -> dict[str, Any]:
        from agents.firehose import ALL_FEEDS, RUN_FIREHOSE_MAX_WALL_S, run_firehose

        run_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        logger.info("firehose run_id=%s start", run_id)
        try:
            stats = run_firehose(
                list(ALL_FEEDS),
                should_stop=lambda: self.shutdown.requested,
                max_wall_s=RUN_FIREHOSE_MAX_WALL_S,
            )
            ok = True
            error: str | None = None
        except Exception as exc:
            stats = None
            ok = False
            error = repr(exc)
            logger.exception("firehose run_id=%s failed", run_id)
        duration = round(time.perf_counter() - started, 3)
        metric: dict[str, Any] = {
            "event": "firehose_run",
            "run_id": run_id,
            "ok": ok,
            "duration_s": duration,
            "aborted": self.shutdown.requested,
            "max_wall_s": RUN_FIREHOSE_MAX_WALL_S,
        }
        if stats is not None:
            metric.update({
                "feeds_polled": stats.feeds_polled,
                "raw_items": stats.raw_items,
                "gate_dropped": stats.gate_dropped,
                "duplicates": stats.duplicates,
                "inserted": stats.inserted,
                "inserted_spam": stats.inserted_spam,
                "unknown_tickers": stats.unknown_tickers,
                "low_materiality": stats.low_materiality,
                "inserts_by_publisher_top": top_counts(stats.inserts_by_publisher),
                "wall_clock_exceeded": stats.wall_clock_exceeded,
            })
        if error is not None:
            metric["error"] = error
        append_metric(metric)
        if stats is not None:
            logger.info(
                "firehose run_id=%s finish ok=%s duration_s=%.3f "
                "feeds=%d raw=%d ins=%d dup=%d dropped=%d unknown=%d "
                "wall_clock_exceeded=%s",
                run_id,
                ok,
                duration,
                stats.feeds_polled,
                stats.raw_items,
                stats.inserted,
                stats.duplicates,
                stats.gate_dropped,
                stats.unknown_tickers,
                stats.wall_clock_exceeded,
            )
            if stats.wall_clock_exceeded:
                logger.warning(
                    "firehose run_id=%s hit max_wall_s=%.0f after %d feeds",
                    run_id,
                    RUN_FIREHOSE_MAX_WALL_S,
                    stats.feeds_polled,
                )
        return metric

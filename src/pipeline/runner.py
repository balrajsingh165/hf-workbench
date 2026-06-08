"""Full pipeline cycle: route → judge → match → score → brief."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.pipeline import metrics as step_metrics
from src.pipeline.db import active_thesis_ids, count_rows, count_story_rows
from src.pipeline.shutdown import ShutdownController
from src.pipeline.step import StepExecutor, StepResult
from src.pipeline_metrics import append_metric

logger = logging.getLogger("hf.scheduler")


@dataclass(slots=True)
class SchedulerConfig:
    interval_hours: float = 12.0
    top_stories: int = 40
    route_cluster_limit: int = 1200
    synth_workers: int = 6
    match_window_days: int = 30
    match_max_candidates: int = 8
    match_min_score: float = 0.50
    match_max_workers: int = 4
    no_images: bool = False
    run_initial: bool = True
    once: bool = False
    firehose_interval_minutes: float = 10.0
    firehose_enabled: bool = True
    repromotion_interval_hours: float = 6.0
    repromotion_max_stale_days: int = 30
    repromotion_enabled: bool = True
    # Trending-ticker retrieval lane (agents/trending.py). Always on: it only
    # adds news rows, and every failure path degrades to "fewer stories" (the
    # lane never raises, the read surfaces collapse to no-ops). Two tiers on one
    # cadence each: Tier 1 (hot) daily, Tier 2 (tail) every 3 days.
    trending_tier1_interval_days: float = 1.0
    trending_tier2_interval_days: float = 3.0
    social_interval_hours: float = 24.0
    # Grok x_search lane (agents/social_topics.py). --no-social turns off the
    # scheduled sweeps (boot + interval) for manual-only operation via
    # `uv run python -m agents.social_topics`.
    social_enabled: bool = True


@dataclass
class _RunContext:
    run_id: str
    started: float
    before_news: int
    before_links: int
    steps: list[dict[str, Any]]
    db_path: Path

    def record(self, result: StepResult, extra: dict[str, Any]) -> None:
        append_metric({
            "event": "step_metrics",
            "run_id": self.run_id,
            "step": result.name,
            **extra,
        })
        self.steps.append({"name": result.name, "ok": result.ok, **extra})

    def summary(self, *, ok: bool, aborted: bool = False) -> dict[str, Any]:
        duration = round(time.perf_counter() - self.started, 3)
        return {
            "run_id": self.run_id,
            "ok": ok,
            "aborted": aborted,
            "duration_s": duration,
            "news_before": self.before_news,
            "news_after": count_rows(self.db_path, "news"),
            "links_before": self.before_links,
            "links_after": count_rows(self.db_path, "thesis_story_links"),
            "steps": self.steps,
        }


class PipelineRunner:
    def __init__(
        self,
        config: SchedulerConfig,
        *,
        db_path: Path,
        root: Path,
        shutdown: ShutdownController,
    ) -> None:
        self._config = config
        self._db_path = db_path
        self._shutdown = shutdown
        self._steps = StepExecutor(root=root, shutdown=shutdown)

    def run(self) -> dict[str, Any]:
        if self._shutdown.requested:
            logger.info("pipeline skipped (shutdown)")
            return {"ok": False, "aborted": True, "steps": []}

        if self._config.no_images:
            os.environ["HF_DISABLE_STORY_IMAGES"] = "1"

        ctx = _RunContext(
            run_id=uuid.uuid4().hex[:12],
            started=time.perf_counter(),
            before_news=count_rows(self._db_path, "news"),
            before_links=count_rows(self._db_path, "thesis_story_links"),
            steps=[],
            db_path=self._db_path,
        )
        logger.info("pipeline run_id=%s start", ctx.run_id)
        append_metric({
            "event": "run_start",
            "run_id": ctx.run_id,
            "config": asdict(self._config),
        })

        ingest_ok = self._ingest(ctx)
        if self._shutdown.requested:
            return self._emit_finish(ctx, ok=False)

        judge_ok = self._judge(ctx)
        if self._shutdown.requested:
            return self._emit_finish(ctx, ok=ingest_ok and judge_ok)

        match_ok = self._match(ctx)
        if self._shutdown.requested:
            return self._emit_finish(ctx, ok=ingest_ok and judge_ok and match_ok)

        score_ok = self._score(ctx)
        if self._shutdown.requested:
            return self._emit_finish(
                ctx, ok=ingest_ok and judge_ok and match_ok and score_ok,
            )

        brief_ok = self._brief(ctx)
        return self._emit_finish(
            ctx,
            ok=ingest_ok and judge_ok and match_ok and score_ok and brief_ok,
        )

    def _emit_finish(self, ctx: _RunContext, *, ok: bool) -> dict[str, Any]:
        aborted = self._shutdown.requested
        summary = ctx.summary(ok=ok and not aborted, aborted=aborted)
        label = "aborted" if aborted else "finish"
        logger.info(
            "pipeline run_id=%s %s ok=%s duration_s=%.3f",
            ctx.run_id,
            label,
            summary["ok"],
            summary["duration_s"],
        )
        append_metric({"event": "run_finish", **summary})
        return summary

    def _ingest(self, ctx: _RunContext) -> bool:
        cfg = self._config
        before_stories = count_story_rows(self._db_path)
        result = self._steps.run(
            "route_news_clusters",
            self._steps.module_cmd(
                "agents.route_news_clusters",
                "--write",
                "--run-id",
                ctx.run_id,
                "--synth-budget",
                str(cfg.top_stories),
                "--route-eval-limit",
                str(cfg.route_cluster_limit),
                "--synth-workers",
                str(cfg.synth_workers),
            ),
            run_id=ctx.run_id,
        )
        extra = step_metrics.parse_ingest_metrics(
            result,
            self._db_path,
            before_news=ctx.before_news,
            before_links=ctx.before_links,
            before_stories=before_stories,
        )
        ctx.record(result, extra)
        return result.ok

    def _judge(self, ctx: _RunContext) -> bool:
        cfg = self._config
        result = self._steps.run(
            "judge_stories",
            self._steps.module_cmd(
                "agents.judge_stories",
                "--limit",
                str(max(cfg.top_stories * 2, 30)),
            ),
            run_id=ctx.run_id,
        )
        extra = step_metrics.parse_judge_metrics(result)
        ctx.record(result, extra)
        return result.ok

    def _match(self, ctx: _RunContext) -> bool:
        cfg = self._config
        match_before = count_rows(self._db_path, "thesis_story_links")
        results: list[StepResult] = []
        for thesis_id in active_thesis_ids(self._db_path):
            if self._shutdown.requested:
                break
            result = self._steps.run(
                f"match_{thesis_id}",
                self._steps.module_cmd(
                    "agents.match_story_for_thesis",
                    "--thesis",
                    thesis_id,
                    "--window",
                    str(cfg.match_window_days),
                    "--min-score",
                    str(cfg.match_min_score),
                    "--max-candidates",
                    str(cfg.match_max_candidates),
                    "--max-workers",
                    str(cfg.match_max_workers),
                ),
                run_id=ctx.run_id,
            )
            result.metrics["thesis_id"] = thesis_id
            results.append(result)

        extra = step_metrics.parse_match_metrics(
            results, self._db_path, before_links=match_before,
        )
        ok = all(r.ok for r in results)
        extra["duration_s"] = round(sum(r.duration_s for r in results), 3)
        step_name = "match_story_for_thesis"
        append_metric({
            "event": "step_metrics",
            "run_id": ctx.run_id,
            "step": step_name,
            **extra,
        })
        ctx.steps.append({"name": step_name, "ok": ok, **extra})
        return ok

    def _score(self, ctx: _RunContext) -> bool:
        result = self._steps.run(
            "score_theses",
            self._steps.module_cmd("agents.score_theses"),
            run_id=ctx.run_id,
        )
        extra = step_metrics.parse_score_metrics(result)
        ctx.record(result, extra)
        return result.ok

    def _brief(self, ctx: _RunContext) -> bool:
        result = self._steps.run(
            "daily_brief",
            self._steps.module_cmd("agents.daily_brief", "--force"),
            run_id=ctx.run_id,
        )
        extra = step_metrics.parse_brief_metrics(result)
        ctx.record(result, extra)
        return result.ok

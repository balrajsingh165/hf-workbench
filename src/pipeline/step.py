"""Run one pipeline stage as a subprocess with metrics logging."""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.pipeline.shutdown import ShutdownController
from src.pipeline_metrics import append_metric

logger = logging.getLogger("hf.scheduler")


@dataclass(slots=True)
class StepResult:
    name: str
    command: list[str]
    returncode: int
    duration_s: float
    stdout: str
    stderr: str
    metrics: dict[str, Any] = field(default_factory=dict)
    aborted: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.aborted


class StepExecutor:
    def __init__(self, *, root: Path, shutdown: ShutdownController) -> None:
        self._root = root
        self._shutdown = shutdown

    def module_cmd(self, module: str, *args: str) -> list[str]:
        return [sys.executable, "-m", module, *args]

    def run(self, name: str, command: list[str], *, run_id: str) -> StepResult:
        if self._shutdown.requested:
            return self._aborted(name, command, run_id=run_id)

        logger.info("step=%s start command=%s", name, " ".join(command))
        started = time.perf_counter()
        proc = subprocess.Popen(
            command,
            cwd=self._root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._shutdown.register_child(proc)
        try:
            stdout, stderr = proc.communicate()
        finally:
            self._shutdown.release_child(proc)

        duration = time.perf_counter() - started
        returncode = proc.returncode if proc.returncode is not None else -signal.SIGTERM
        aborted = self._shutdown.requested and returncode != 0

        if stdout.strip():
            logger.info("step=%s stdout:\n%s", name, stdout.strip())
        if stderr.strip():
            logger.info("step=%s stderr:\n%s", name, stderr.strip())

        result = StepResult(
            name=name,
            command=command,
            returncode=returncode,
            duration_s=round(duration, 3),
            stdout=stdout,
            stderr=stderr,
            aborted=aborted,
        )
        logger.info(
            "step=%s finish status=%s returncode=%s duration_s=%.3f",
            name,
            "ok" if result.ok else "failed",
            returncode,
            duration,
        )
        append_metric({
            "event": "step",
            "run_id": run_id,
            "step": name,
            "returncode": returncode,
            "duration_s": result.duration_s,
            "ok": result.ok,
            "aborted": aborted,
        })
        return result

    def _aborted(self, name: str, command: list[str], *, run_id: str) -> StepResult:
        logger.info("step=%s aborted (shutdown)", name)
        result = StepResult(
            name=name,
            command=command,
            returncode=-signal.SIGTERM,
            duration_s=0.0,
            stdout="",
            stderr="aborted: shutdown requested",
            aborted=True,
        )
        append_metric({
            "event": "step",
            "run_id": run_id,
            "step": name,
            "returncode": result.returncode,
            "duration_s": result.duration_s,
            "ok": False,
            "aborted": True,
        })
        return result

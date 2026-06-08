"""Cooperative shutdown for pm2 `hf-pipeline` (SIGTERM → stop flag + child kill)."""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field

logger = logging.getLogger("hf.scheduler")


@dataclass
class ShutdownController:
    """Process-wide stop signal; terminates the active pipeline step subprocess."""

    _stop: threading.Event = field(default_factory=threading.Event)
    _proc_lock: threading.Lock = field(default_factory=threading.Lock)
    _child: subprocess.Popen[str] | None = field(default=None, init=False)

    @property
    def requested(self) -> bool:
        return self._stop.is_set()

    def clear(self) -> None:
        self._stop.clear()

    def request(self) -> None:
        self._stop.set()
        self._terminate_child()

    def register_child(self, proc: subprocess.Popen[str]) -> None:
        with self._proc_lock:
            self._child = proc

    def release_child(self, proc: subprocess.Popen[str]) -> None:
        with self._proc_lock:
            if self._child is proc:
                self._child = None

    def _terminate_child(self) -> None:
        with self._proc_lock:
            proc = self._child
        if proc is None or proc.poll() is not None:
            return
        logger.info("terminating in-flight step pid=%s", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("killing step subprocess pid=%s", proc.pid)
            proc.kill()
            proc.wait(timeout=5)

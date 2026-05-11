"""Subprocess wrapper for the macOS Tabracadabra event tap.

The event tap is in the user's keyboard latency path. Keeping it in a small
helper process prevents unrelated server-side agent work from delaying the tap
thread through Python GIL contention.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping

logger = logging.getLogger(__name__)

_READY_MARKER = "[tabracadabra] Event tap active"
_FAILED_MARKER = "[tabracadabra] Failed to create event tap"


class TabracadabraProcessService:
    """Owns a child process running ``apps.tabracadabra.main``."""

    def __init__(
        self,
        *,
        base_url: str,
        logs_dir: str,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url
        self._logs_dir = logs_dir
        self._env_overrides = dict(env or {})
        self._proc: subprocess.Popen[str] | None = None
        self._ready_event = threading.Event()
        self._failed_event = threading.Event()
        self._stop_requested = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the helper unless it is already alive."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return

            self._ready_event.clear()
            self._failed_event.clear()
            self._stop_requested = False

            env = {
                **os.environ,
                **self._env_overrides,
                "TADA_BASE_URL": self._base_url,
                "TADA_LOG_DIR": self._logs_dir,
                "TADA_PARENT_PID": str(os.getpid()),
                "TADA_PARENT_WATCHDOG": "1",
                "PYTHONUNBUFFERED": "1",
            }
            cmd = [sys.executable, "-u", "-m", "apps.tabracadabra.main"]
            self._proc = subprocess.Popen(
                cmd,
                env=env,
                text=True,
                bufsize=1,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self._start_pipe_reader("stdout", self._proc.stdout, logging.INFO)
            self._start_pipe_reader("stderr", self._proc.stderr, logging.WARNING)
            threading.Thread(
                target=self._watch_exit,
                args=(self._proc,),
                daemon=True,
                name="tabracadabra-process-watch",
            ).start()

        logger.info("Tabracadabra helper process started pid=%s", self._proc.pid if self._proc else None)

    def is_ready(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None and self._ready_event.is_set()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        """Wait until the child reports that the event tap is installed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.is_ready():
                return True
            proc = self._proc
            if self._failed_event.is_set() or proc is None or proc.poll() is not None:
                return False
            if deadline is None:
                wait_s = 0.1
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait_s = min(0.1, remaining)
            self._ready_event.wait(wait_s)

    def stop(self, timeout: float = 2.0) -> None:
        """Terminate the helper process."""
        with self._lock:
            proc = self._proc
            self._stop_requested = True
            self._ready_event.clear()
            self._failed_event.clear()
            if proc is None:
                return
            self._proc = None

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("Tabracadabra helper did not stop in %.1fs; killing", timeout)
                proc.kill()
                proc.wait(timeout=timeout)

        logger.info("Tabracadabra helper process stopped")

    def _start_pipe_reader(self, name: str, stream, level: int) -> None:
        if stream is None:
            return
        threading.Thread(
            target=self._read_pipe,
            args=(name, stream, level),
            daemon=True,
            name=f"tabracadabra-process-{name}",
        ).start()

    def _read_pipe(self, name: str, stream, level: int) -> None:
        try:
            for line in iter(stream.readline, ""):
                line = line.rstrip()
                if not line:
                    continue
                if _READY_MARKER in line:
                    self._ready_event.set()
                elif _FAILED_MARKER in line:
                    self._failed_event.set()
                logger.log(level, "[tabracadabra:%s] %s", name, line)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _watch_exit(self, proc: subprocess.Popen[str]) -> None:
        returncode = proc.wait()
        self._ready_event.clear()
        if not self._stop_requested:
            logger.warning("Tabracadabra helper exited unexpectedly with code %s", returncode)

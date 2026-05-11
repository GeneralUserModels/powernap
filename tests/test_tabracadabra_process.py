from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from apps.tabracadabra.process_service import TabracadabraProcessService


class _FakePipe:
    def __init__(self, lines: list[str]):
        self._lines = lines
        self.closed = False

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        return ""

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    pid = 12345

    def __init__(self):
        self.stdout = _FakePipe(["[tabracadabra] Event tap active. Press Option+Tab to generate.\n"])
        self.stderr = _FakePipe([])
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._done = threading.Event()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if timeout is None:
            self._done.wait()
        elif not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(["fake"], timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 143
        self._done.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()


class TabracadabraProcessServiceTests(unittest.TestCase):
    def test_process_service_starts_helper_with_isolated_event_tap_env(self):
        fake_proc = _FakeProc()
        created = {}

        def fake_popen(cmd, **kwargs):
            created["cmd"] = cmd
            created["kwargs"] = kwargs
            return fake_proc

        with patch("apps.tabracadabra.process_service.subprocess.Popen", fake_popen):
            service = TabracadabraProcessService(
                base_url="http://127.0.0.1:8765",
                logs_dir="/tmp/tada-logs",
            )
            service.start()

            self.assertTrue(service.wait_until_ready(1.0))
            self.assertTrue(service.is_ready())

            self.assertEqual(created["cmd"], [sys.executable, "-u", "-m", "apps.tabracadabra.main"])
            env = created["kwargs"]["env"]
            self.assertEqual(env["TADA_BASE_URL"], "http://127.0.0.1:8765")
            self.assertEqual(env["TADA_LOG_DIR"], "/tmp/tada-logs")
            self.assertEqual(env["TADA_PARENT_WATCHDOG"], "1")
            self.assertEqual(env["PYTHONUNBUFFERED"], "1")
            self.assertTrue(env["TADA_PARENT_PID"].isdigit())

            service.stop()

        self.assertTrue(fake_proc.terminated)
        self.assertFalse(service.is_ready())


if __name__ == "__main__":
    unittest.main()

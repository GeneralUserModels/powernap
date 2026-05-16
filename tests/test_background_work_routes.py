from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from apps.memory.service import successful_run_marker as memory_success_marker
from apps.moments.runtime.discovery import successful_run_marker as tada_success_marker
from server.config import ServerConfig
from server.routes import background_work
from server.state import ServerState


class BackgroundWorkRouteTests(unittest.TestCase):
    def _state(self, root: Path) -> ServerState:
        logs = root / "logs"
        tada = root / "logs-tada"
        logs.mkdir()
        tada.mkdir()
        return ServerState(config=ServerConfig(log_dir=str(logs), tada_dir=str(tada)))

    def test_seeded_memory_checkpoint_does_not_allow_first_manual_start(self):
        with tempfile.TemporaryDirectory() as d:
            state = self._state(Path(d))
            memory_dir = Path(state.config.log_dir) / "memory"
            memory_dir.mkdir()
            (memory_dir / ".last_run").write_text((datetime.now() - timedelta(days=1)).isoformat())

            status = background_work._memory_status(state)

            self.assertFalse(status["manual_start_allowed"])
            self.assertEqual(status["manual_start_blocked_reason"], "first_run")

    def test_memory_success_marker_allows_manual_start(self):
        with tempfile.TemporaryDirectory() as d:
            state = self._state(Path(d))
            marker = memory_success_marker(state.config.log_dir)
            marker.parent.mkdir(parents=True)
            marker.write_text("")

            status = background_work._memory_status(state)

            self.assertTrue(status["manual_start_allowed"])
            self.assertIsNone(status["manual_start_blocked_reason"])

    def test_tada_start_runs_discovery_then_due_moment_scan(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                state = self._state(Path(d))
                tada_success_marker(state.config.tada_dir).write_text("")
                request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(server=state)))
                calls: list[str] = []

                async def fake_discovery(state):
                    calls.append("discovery")
                    return True

                async def fake_scan(state):
                    calls.append("scan")
                    return 2

                with (
                    patch.object(background_work, "run_moments_discovery_once", new=fake_discovery),
                    patch.object(background_work, "scan_due_moments_once", new=fake_scan),
                ):
                    response = await background_work.start_tada(request)
                    await asyncio.gather(*list(state.background_work_tasks))

                return response.status_code, calls

        status_code, calls = asyncio.run(run())

        self.assertEqual(status_code, 202)
        self.assertEqual(calls, ["discovery", "scan"])


if __name__ == "__main__":
    unittest.main()

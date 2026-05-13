from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from server.process_jobs import ProcessJobError, ProcessJobRunner, relay_worker_event


FAKE_WORKER = r'''
import json
import os
import sys
import time

PREFIX = "__tada_worker_event__ "

def emit(event):
    print(PREFIX + json.dumps(event), flush=True)

job = sys.argv[1]
payload = json.loads(sys.stdin.read() or "{}")

if job == "success":
    print("ordinary worker log", flush=True)
    emit({"type": "activity", "agent": "memory", "message": "Working"})
    emit({"type": "round", "agent": "memory", "message": "Working", "num_turns": 2, "max_turns": 5})
    emit({
        "type": "result",
        "result": {
            "ok": True,
            "payload_secret": payload.get("api_key"),
            "argv": sys.argv,
            "parent_pid": os.environ.get("TADA_PARENT_PID"),
            "watchdog": os.environ.get("TADA_PARENT_WATCHDOG"),
            "unbuffered": os.environ.get("PYTHONUNBUFFERED"),
        },
    })
elif job == "malformed":
    print(PREFIX + "{not-json", flush=True)
elif job == "nonzero":
    print("boom", file=sys.stderr, flush=True)
    sys.exit(7)
elif job == "sleep":
    time.sleep(30)
'''


class ProcessJobRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _runner(self, tmp: str) -> ProcessJobRunner:
        path = tmp + os.pathsep + os.environ.get("PYTHONPATH", "")
        self._env = {"PYTHONPATH": path}
        return ProcessJobRunner(module="fake_worker", terminate_timeout_s=0.2)

    async def test_success_relays_progress_and_hides_secrets_from_argv(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "fake_worker.py").write_text(FAKE_WORKER)
            runner = self._runner(d)
            events = []

            result = await runner.run(
                "success",
                {"api_key": "secret-token"},
                env=self._env,
                on_event=lambda event: events.append(event),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload_secret"], "secret-token")
        self.assertNotIn("secret-token", " ".join(result["argv"]))
        self.assertEqual(result["watchdog"], "1")
        self.assertEqual(result["unbuffered"], "1")
        self.assertTrue(str(result["parent_pid"]).isdigit())
        self.assertEqual([event["type"] for event in events], ["activity", "round"])

    async def test_malformed_event_raises(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "fake_worker.py").write_text(FAKE_WORKER)
            runner = self._runner(d)
            with self.assertRaises(ProcessJobError):
                await runner.run("malformed", {}, env=self._env)

    async def test_nonzero_exit_raises(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "fake_worker.py").write_text(FAKE_WORKER)
            runner = self._runner(d)
            with self.assertRaises(ProcessJobError):
                await runner.run("nonzero", {}, env=self._env)

    async def test_cancel_terminates_worker(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "fake_worker.py").write_text(FAKE_WORKER)
            runner = self._runner(d)
            task = asyncio.create_task(runner.run("sleep", {}, env=self._env))
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_relay_worker_event_broadcasts_existing_activity_shape(self):
        calls = []

        class State:
            async def broadcast_activity(self, *args, **kwargs):
                calls.append((args, kwargs))

        await relay_worker_event(
            State(),
            {
                "type": "round",
                "agent": "memory",
                "message": "Working",
                "slug": "abc",
                "cadence": "once",
                "num_turns": 1,
                "max_turns": 3,
            },
        )

        self.assertEqual(calls, [(("memory", "Working"), {"slug": "abc", "cadence": "once", "num_turns": 1, "max_turns": 3})])


if __name__ == "__main__":
    unittest.main()

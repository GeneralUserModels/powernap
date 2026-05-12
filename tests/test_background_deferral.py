from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from server.config import ServerConfig
from server.routes import onboarding
from server.services import BACKGROUND_WORK_FRESH_INSTALL_DELAY
from server.state import ServerState


class BackgroundDeferralTests(unittest.TestCase):
    def test_first_onboarding_finalize_sets_background_work_deferral(self):
        async def run():
            async def fake_start_services(state):
                return None

            state = ServerState(config=ServerConfig())
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(server=state)))
            before = datetime.now()

            with (
                patch.object(ServerConfig, "save", autospec=True),
                patch.object(onboarding, "start_services", new=fake_start_services),
            ):
                result = await onboarding.onboarding_finalize(request)
                await asyncio.sleep(0)

            after = datetime.now()
            return state, result, before, after

        state, result, before, after = asyncio.run(run())

        self.assertEqual(result, {"ok": True})
        self.assertTrue(state.config.onboarding_complete)
        deferred_until = datetime.fromisoformat(state.config.background_work_deferred_until)
        self.assertGreaterEqual(deferred_until, before + BACKGROUND_WORK_FRESH_INSTALL_DELAY)
        self.assertLessEqual(deferred_until, after + BACKGROUND_WORK_FRESH_INSTALL_DELAY)

    def test_returning_onboarding_finalize_does_not_reset_existing_deferral(self):
        async def run():
            async def fake_start_services(state):
                return None

            config = ServerConfig(
                onboarding_complete=True,
                background_work_deferred_until="2030-01-01T00:00:00",
            )
            state = ServerState(config=config)
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(server=state)))

            with (
                patch.object(ServerConfig, "save", autospec=True),
                patch.object(onboarding, "start_services", new=fake_start_services),
            ):
                await onboarding.onboarding_finalize(request)
                await asyncio.sleep(0)

            return state.config.background_work_deferred_until

        self.assertEqual(asyncio.run(run()), "2030-01-01T00:00:00")

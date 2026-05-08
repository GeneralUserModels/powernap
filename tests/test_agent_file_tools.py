from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sandbox_runtime import SandboxManager, SandboxRuntimeConfig

from agent.tools.edit import EditTool
from agent.tools.write import WriteTool


class AgentFileToolSandboxTests(unittest.TestCase):
    def tearDown(self):
        self._reset_sandbox()

    def _reset_sandbox(self):
        result = SandboxManager.reset()
        if inspect.isawaitable(result):
            asyncio.run(result)

    def _init_sandbox(self, allowed: Path):
        self._reset_sandbox()
        asyncio.run(SandboxManager.initialize(SandboxRuntimeConfig(
            network={},
            filesystem={"allow_write": [str(allowed)], "deny_read": []},
        )))

    def test_write_file_rejects_paths_outside_sandbox_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            allowed = root / "allowed"
            denied = root / "denied"
            allowed.mkdir()
            denied.mkdir()
            self._init_sandbox(allowed)

            WriteTool().run(str(allowed / "ok.txt"), "ok")
            self.assertEqual((allowed / "ok.txt").read_text(), "ok")

            with self.assertRaises(PermissionError):
                WriteTool().run(str(denied / "no.txt"), "no")
            self.assertFalse((denied / "no.txt").exists())

    def test_write_file_uses_explicit_allowlist_over_global_sandbox(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            allowed = root / "allowed"
            denied = root / "denied"
            allowed.mkdir()
            denied.mkdir()
            self._init_sandbox(root)

            WriteTool([allowed]).run(str(allowed / "ok.txt"), "ok")
            self.assertEqual((allowed / "ok.txt").read_text(), "ok")

            with self.assertRaises(PermissionError):
                WriteTool([allowed]).run(str(denied / "no.txt"), "no")
            self.assertFalse((denied / "no.txt").exists())

    def test_edit_file_rejects_paths_outside_sandbox_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            allowed = root / "allowed"
            denied = root / "denied"
            allowed.mkdir()
            denied.mkdir()
            (allowed / "ok.txt").write_text("before")
            (denied / "no.txt").write_text("before")
            self._init_sandbox(allowed)

            EditTool().run(str(allowed / "ok.txt"), "before", "after")
            self.assertEqual((allowed / "ok.txt").read_text(), "after")

            with self.assertRaises(PermissionError):
                EditTool().run(str(denied / "no.txt"), "before", "after")
            self.assertEqual((denied / "no.txt").read_text(), "before")

    def test_edit_file_uses_explicit_allowlist_over_global_sandbox(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            allowed = root / "allowed"
            denied = root / "denied"
            allowed.mkdir()
            denied.mkdir()
            (allowed / "ok.txt").write_text("before")
            (denied / "no.txt").write_text("before")
            self._init_sandbox(root)

            EditTool([allowed]).run(str(allowed / "ok.txt"), "before", "after")
            self.assertEqual((allowed / "ok.txt").read_text(), "after")

            with self.assertRaises(PermissionError):
                EditTool([allowed]).run(str(denied / "no.txt"), "before", "after")
            self.assertEqual((denied / "no.txt").read_text(), "before")


if __name__ == "__main__":
    unittest.main()

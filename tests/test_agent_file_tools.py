from __future__ import annotations

import asyncio
import inspect
import json
import shlex
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
from agent.tools.read import OUTPUT_LIMIT as READ_OUTPUT_LIMIT, ReadTool
from agent.tools.sanitize import sanitize_tool_output
from agent.tools.terminal import OUTPUT_LIMIT as TERMINAL_OUTPUT_LIMIT, TerminalTool
from agent.tools.write import WriteTool


class UnsandboxedTerminalTool(TerminalTool):
    TIMEOUT_SECONDS = 5

    def _wrap_sandbox(self, command: str):
        return command


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

    def test_sanitize_tool_output_removes_raw_events_from_jsonl(self):
        row = {
            "timestamp": 1,
            "text": "useful caption",
            "source": {
                "id": "row-id",
                "summary": "summary",
                "raw_events": [{"event_type": "mouse_scroll", "dy": -2}],
            },
        }
        output = sanitize_tool_output(f"/tmp/screen/filtered.jsonl:{json.dumps(row)}\n")

        self.assertIn("/tmp/screen/filtered.jsonl:", output)
        self.assertIn("useful caption", output)
        self.assertIn("row-id", output)
        self.assertNotIn("mouse_scroll", output)
        self.assertNotIn("raw_events", output)

    def test_read_file_sanitizes_raw_events(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "filtered.jsonl"
            path.write_text(json.dumps({
                "timestamp": 1,
                "text": "caption",
                "source": {"raw_events": [{"event_type": "mouse_scroll"}]},
            }))

            output = ReadTool().run(str(path))

            self.assertIn("caption", output)
            self.assertNotIn("mouse_scroll", output)
            self.assertNotIn("raw_events", output)

    def test_read_file_truncates_long_single_line_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "large.txt"
            path.write_text(("x" * (READ_OUTPUT_LIMIT + 1000)) + "SENTINEL_END")

            output = ReadTool().run(str(path))

            self.assertLessEqual(len(output), READ_OUTPUT_LIMIT)
            self.assertIn("Warning: file output truncated", output)
            self.assertIn(f"after {READ_OUTPUT_LIMIT} bytes", output)
            self.assertNotIn("SENTINEL_END", output)

    def test_read_file_limit_returns_bounded_lines_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lines.txt"
            path.write_text("one\ntwo\nthree\n")

            output = ReadTool().run(str(path), limit=2)

            self.assertIn("one", output)
            self.assertIn("two", output)
            self.assertNotIn("three", output)
            self.assertIn("Warning: file output truncated after 2 lines", output)

    def test_bash_truncates_large_output_with_warning(self):
        command = (
            f"{shlex.quote(sys.executable)} -c "
            f"\"import sys; sys.stdout.write('x' * {TERMINAL_OUTPUT_LIMIT + 10000})\""
        )

        output = UnsandboxedTerminalTool().run(command)

        self.assertLessEqual(len(output), TERMINAL_OUTPUT_LIMIT)
        self.assertIn("Warning: command output truncated", output)


if __name__ == "__main__":
    unittest.main()

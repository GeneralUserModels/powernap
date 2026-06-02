from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent.cli_backends import install as cli_install


class AgentBackendInstallTests(unittest.TestCase):
    def test_detect_npm_reports_version_and_install_url(self):
        with patch.object(cli_install, "_which", return_value="/usr/local/bin/npm"), \
             patch.object(cli_install, "_probe_version", return_value="10.9.0"):
            status = cli_install.detect_npm()

        self.assertTrue(status.available)
        self.assertEqual(status.version, "10.9.0")
        self.assertEqual(status.bin, "/usr/local/bin/npm")
        self.assertIn("nodejs.org", status.install_url)

    def test_install_cli_uses_latest_package_and_user_prefix_fallback(self):
        calls: list[list[str]] = []

        def fake_which(name: str, extra_path: str | None = None):
            if name == "npm":
                return "/usr/local/bin/npm"
            if name == "codex" and extra_path:
                return str(Path(extra_path) / "codex")
            return None

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as d, \
             patch.object(cli_install, "_which", side_effect=fake_which), \
             patch.object(cli_install, "_npm_prefix", return_value=Path("/usr/local")), \
             patch.object(cli_install, "_is_writable", return_value=False), \
             patch.object(cli_install.subprocess, "run", side_effect=fake_run):
            result = cli_install.install_cli("codex", log_dir=Path(d))

        self.assertTrue(result.ok)
        self.assertEqual(calls[0][0], "/usr/local/bin/npm")
        self.assertIn("--prefix", calls[0])
        self.assertIn("@openai/codex@latest", calls[0])
        self.assertEqual(result.extra_path, str(Path.home() / ".local" / "bin"))

    def test_install_cli_reports_missing_npm(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.object(cli_install, "_which", return_value=None):
            result = cli_install.install_cli("claude_code", log_dir=Path(d))
            log_text = result.log_path.read_text()

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "npm_not_found")
        self.assertIn("npm not found", log_text)


if __name__ == "__main__":
    unittest.main()

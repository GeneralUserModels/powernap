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

from agent.cli_backends import load_cli_config
from agent.cli_backends.claude import build_claude_command
from agent.cli_backends.codex import build_codex_command
from apps.chat import service


def _config(log_dir: str, backend: str):
    return SimpleNamespace(
        log_dir=log_dir,
        agent_backend=backend,
        agent_model="gemini/gemini-3-flash-preview",
        agent_api_key="",
        codex_bin="codex",
        claude_bin="claude",
        cli_bin_extra_path="",
        codex_model="ignored-codex-model",
        codex_reasoning_effort="minimal",
        claude_model="ignored-claude-model",
        claude_effort="max",
        resolve_api_key=lambda _key: None,
    )


class ChatCliBackendTests(unittest.TestCase):
    def test_effort_options_follow_selected_backend(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                service.effort_options(_config(d, "codex")),
                ["minimal", "low", "medium", "high", "xhigh"],
            )
            self.assertEqual(
                service.effort_options(_config(d, "claude_code")),
                ["low", "medium", "high", "xhigh", "max"],
            )
            self.assertEqual(
                service.effort_options(_config(d, "gemini")),
                ["low", "medium", "high"],
            )

    def test_create_session_defaults_invalid_codex_effort_to_xhigh(self):
        with tempfile.TemporaryDirectory() as d:
            state = SimpleNamespace(config=_config(d, "codex"))
            meta = service.create_session(state, model="", effort="max")

        self.assertEqual(meta["effort"], "xhigh")
        self.assertEqual(meta["model"], "gpt-5.5")

    def test_cli_config_ignores_config_file_model_and_effort_values(self):
        with tempfile.TemporaryDirectory() as d:
            codex = load_cli_config(_config(d, "codex"))
            claude = load_cli_config(_config(d, "claude_code"))

        assert codex is not None
        assert claude is not None
        self.assertEqual(codex.codex_model, "gpt-5.5")
        self.assertEqual(codex.codex_reasoning_effort, "xhigh")
        self.assertEqual(claude.claude_model, "claude-sonnet-4-6")
        self.assertEqual(claude.claude_effort, "medium")

    def test_codex_command_can_enable_native_web_search(self):
        cmd = build_codex_command(
            codex_bin="codex",
            model="gpt-5.5",
            reasoning_effort="xhigh",
            cwd=Path("/tmp/work"),
            enable_web_search=True,
        )

        self.assertEqual(cmd[:3], ["codex", "--search", "exec"])

    def test_claude_command_can_enable_browser_access(self):
        cmd = build_claude_command(
            claude_bin="claude",
            model="claude-sonnet-4-6",
            effort="medium",
            add_dirs=[],
            max_turns=10,
            enable_browser=True,
        )

        self.assertIn("--chrome", cmd)

    def test_cli_chat_agent_uses_chat_codex_effort_and_appends_answer(self):
        with tempfile.TemporaryDirectory() as d:
            state = SimpleNamespace(config=_config(d, "codex"))
            cli_config = load_cli_config(state.config)
            assert cli_config is not None
            agent = service._CliChatAgent(
                state=state,
                meta={"id": "chat_test", "effort": "xhigh"},
                cli_config=cli_config,
                system_prompt="System prompt",
                on_round=None,
                should_stop=None,
            )
            captured = {}

            def fake_run_stage_via_cli(**kwargs):
                captured.update(kwargs)
                answer_path = kwargs["expected_outputs"][0]
                answer_path.parent.mkdir(parents=True, exist_ok=True)
                answer_path.write_text("Done.")

            messages = [{"role": "user", "content": "hello"}]
            with patch.object(service, "run_stage_via_cli", side_effect=fake_run_stage_via_cli):
                result = agent.run(messages)

        self.assertEqual(result, "Done.")
        self.assertEqual(messages[-1], {"role": "assistant", "content": "Done."})
        self.assertEqual(captured["config"].codex_reasoning_effort, "xhigh")
        self.assertIn("Write the final answer only", captured["prompt"])

    def test_cli_chat_agent_uses_chat_claude_effort(self):
        with tempfile.TemporaryDirectory() as d:
            state = SimpleNamespace(config=_config(d, "claude_code"))
            cli_config = load_cli_config(state.config)
            assert cli_config is not None
            agent = service._CliChatAgent(
                state=state,
                meta={"id": "chat_test", "effort": "max"},
                cli_config=cli_config,
                system_prompt="System prompt",
                on_round=None,
                should_stop=None,
            )

            def fake_run_stage_via_cli(**kwargs):
                answer_path = kwargs["expected_outputs"][0]
                answer_path.parent.mkdir(parents=True, exist_ok=True)
                answer_path.write_text("Done.")
                self.assertEqual(kwargs["config"].claude_effort, "max")

            with patch.object(service, "run_stage_via_cli", side_effect=fake_run_stage_via_cli):
                result = agent.run([{"role": "user", "content": "hello"}])

        self.assertEqual(result, "Done.")


if __name__ == "__main__":
    unittest.main()

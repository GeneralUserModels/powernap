from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent.cli_backends import CliAgentError, CliBackendConfig
from apps.moments.runtime import editor


def _cli_config() -> CliBackendConfig:
    return CliBackendConfig(
        backend="codex",
        codex_bin="codex",
        claude_bin="claude",
        codex_model="test",
        codex_reasoning_effort="low",
        claude_model="test",
        claude_effort="low",
    )


def _write_app(result_dir: Path) -> None:
    out = result_dir / "output"
    out.mkdir(parents=True)
    (result_dir / "meta.json").write_text(json.dumps({"title": "Demo", "description": "Test"}))
    (out / "index.html").write_text(
        "<!doctype html><script src=\"components.js\"></script><script src=\"app.js\"></script>"
    )
    (out / "styles.css").write_text(".title { color: red; }\n")
    (out / "app.js").write_text("const { PageHeader } = PN;\nconsole.log('old');\n")
    (out / "base.css").write_text("body { margin: 0; }\n")
    (out / "components.js").write_text("window.PN = { PageHeader: function(){} };\n")


class MomentsEditorRuntimeTests(unittest.TestCase):
    def test_prepare_editor_bridge_copies_canonical_components(self):
        with tempfile.TemporaryDirectory() as d:
            result_dir = Path(d) / "results" / "demo"
            _write_app(result_dir)

            prepared = editor.prepare_editor_bridge(result_dir)

            self.assertTrue(prepared["prepared"])
            self.assertEqual(
                (result_dir / "output" / "components.js").read_bytes(),
                (SRC / "apps/moments/templates/shared/components.js").read_bytes(),
            )

    def test_run_editor_turn_applies_allowed_file_change(self):
        with tempfile.TemporaryDirectory() as d:
            result_dir = Path(d) / "results" / "demo"
            _write_app(result_dir)

            def fake_cli(**kwargs):
                (result_dir / "output" / "app.js").write_text(
                    "const { PageHeader } = PN;\nconsole.log('new');\n"
                )
                kwargs["expected_outputs"][0].parent.mkdir(parents=True, exist_ok=True)
                kwargs["expected_outputs"][0].write_text(json.dumps({
                    "summary": "Updated the app title.",
                    "changed_files": ["output/app.js"],
                    "draft_patch": {"body": "new draft"},
                }))

            with patch("apps.moments.runtime.editor.run_stage_via_cli", side_effect=fake_cli):
                result = editor.run_editor_turn(
                    result_dir=result_dir,
                    slug="demo",
                    user_message="Change the title",
                    draft_snapshot={"body": "old draft"},
                    conversation=[{"role": "user", "content": "Change the title"}],
                    cli_config=_cli_config(),
                )

            self.assertEqual(result.summary, "Updated the app title.")
            self.assertEqual(result.changed_files, ["output/app.js"])
            self.assertEqual(result.draft_patch, {"body": "new draft"})
            self.assertIn("new", (result_dir / "output" / "app.js").read_text())

    def test_run_editor_turn_restores_backup_on_cli_failure(self):
        with tempfile.TemporaryDirectory() as d:
            result_dir = Path(d) / "results" / "demo"
            _write_app(result_dir)
            original = (result_dir / "output" / "app.js").read_text()

            def fake_cli(**kwargs):
                (result_dir / "output" / "app.js").write_text("broken")
                raise CliAgentError("failed")

            with patch("apps.moments.runtime.editor.run_stage_via_cli", side_effect=fake_cli):
                with self.assertRaises(CliAgentError):
                    editor.run_editor_turn(
                        result_dir=result_dir,
                        slug="demo",
                        user_message="Break it",
                        draft_snapshot={},
                        conversation=[{"role": "user", "content": "Break it"}],
                        cli_config=_cli_config(),
                    )

            self.assertEqual((result_dir / "output" / "app.js").read_text(), original)

    def test_run_editor_turn_rejects_protected_file_change(self):
        with tempfile.TemporaryDirectory() as d:
            result_dir = Path(d) / "results" / "demo"
            _write_app(result_dir)
            original = (result_dir / "output" / "components.js").read_text()

            def fake_cli(**kwargs):
                (result_dir / "output" / "components.js").write_text("window.PN = {};\n")
                kwargs["expected_outputs"][0].parent.mkdir(parents=True, exist_ok=True)
                kwargs["expected_outputs"][0].write_text(json.dumps({
                    "summary": "Changed shared components.",
                    "changed_files": ["output/components.js"],
                    "draft_patch": {},
                }))

            with patch("apps.moments.runtime.editor.run_stage_via_cli", side_effect=fake_cli):
                with self.assertRaises(editor.MomentEditorError):
                    editor.run_editor_turn(
                        result_dir=result_dir,
                        slug="demo",
                        user_message="Change shared components",
                        draft_snapshot={},
                        conversation=[{"role": "user", "content": "Change shared components"}],
                        cli_config=_cli_config(),
                    )

            self.assertEqual((result_dir / "output" / "components.js").read_text(), original)

    def test_editor_session_saves_and_loads_json_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            result_dir = Path(d) / "results" / "demo"
            result_dir.mkdir(parents=True)
            session = editor.EditorSession(path=result_dir / "edit_20260526_120000.md")
            session.add_user_message("Change the headline")
            session.add_assistant_message("Updated the headline.")

            session.save()
            loaded = editor.load_latest_editor_session(result_dir)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.visible_messages(), session.visible_messages())
            self.assertTrue((result_dir / "edit_20260526_120000.json").is_file())

    def test_load_latest_editor_session_supports_legacy_markdown_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            result_dir = Path(d) / "results" / "demo"
            result_dir.mkdir(parents=True)
            (result_dir / "edit_20260526_120000.md").write_text(
                "# Tada App Editor Conversation\n\n"
                "**User:** Make the title shorter\n\n"
                "**Codex/Claude:** Shortened the title.\n"
            )

            loaded = editor.load_latest_editor_session(result_dir)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.visible_messages(), [
                {"role": "user", "content": "Make the title shorter"},
                {"role": "assistant", "content": "Shortened the title."},
            ])


if __name__ == "__main__":
    unittest.main()

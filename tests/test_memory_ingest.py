from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from apps.memory import ingest
from apps.moments.runtime.scheduler import scheduled_service_due


def _write_checkpoint(path: Path, value: str = "2025-01-01T00:00:00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")


class MemoryIngestTests(unittest.TestCase):
    def test_memory_service_waits_on_first_launch_but_catches_up_after_schedule(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            last_run = root / ".memory_last_run"

            self.assertFalse(scheduled_service_due("daily at 3am", last_run))
            self.assertTrue(last_run.exists())
            last_run.write_text(datetime(2000, 1, 1).isoformat())
            self.assertTrue(scheduled_service_due("daily at 3am", last_run))

    def test_collect_inputs_classifies_first_incremental_and_no_new_data(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir()
            ingest._bootstrap_memory(logs / "memory")

            first = ingest._collect_ingest_inputs(logs, None)
            self.assertEqual(first.mode, "first_run")

            _write_checkpoint(logs / "memory" / ".last_ingest")
            chat = logs / "chats" / "chat_1" / "conversation.md"
            chat.parent.mkdir(parents=True)
            chat.write_text("**User:** hello\n")
            screen = logs / "screen"
            screen.mkdir()
            (screen / "filtered.jsonl").write_text(
                json.dumps({"timestamp": datetime(2025, 1, 2).timestamp(), "source_name": "screen", "text": "coding"}) + "\n"
            )

            incremental = ingest._collect_ingest_inputs(logs, datetime(2025, 1, 1))
            self.assertEqual(incremental.mode, "incremental")
            self.assertIn("chats/chat_1/conversation.md", incremental.new_inputs_list)
            self.assertIn("screen/filtered.jsonl", incremental.new_inputs_list)

            no_new = ingest._collect_ingest_inputs(logs, datetime(2099, 1, 1))
            self.assertEqual(no_new.mode, "no_new_data")
            self.assertEqual(no_new.new_inputs_list, "- (none detected)")

    def test_bootstrap_creates_special_files_without_overwriting(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "logs" / "memory"
            memory.mkdir(parents=True)
            (memory / "index.md").write_text("existing index")
            (memory / "log.md").write_text("existing log")

            ingest._bootstrap_memory(memory)

            self.assertEqual((memory / "index.md").read_text(), "existing index")
            self.assertEqual((memory / "log.md").read_text(), "existing log")
            self.assertIn("Memory Wiki Schema", (memory / "schema.md").read_text())

    def test_validation_detects_frontmatter_index_and_log_issues(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "logs" / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "project.md").write_text("# Project\n")
            (memory / ".hidden.md").write_text("# Hidden\n")

            issues = ingest._validate_wiki(memory, "2026-05-03")
            codes = {issue["code"] for issue in issues}

            self.assertIn("missing_frontmatter", codes)
            self.assertIn("index_missing_page", codes)
            self.assertIn("missing_log_entry", codes)
            self.assertFalse(any(issue["path"] == ".hidden.md" for issue in issues))

    def test_run_executes_split_passes_and_writes_checkpoint_after_finalize(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir()
            calls: list[tuple[str, str]] = []

            def fake_pass(pass_name, instruction, logs_dir, model, api_key, on_round, subagent_model, subagent_api_key):
                calls.append((pass_name, instruction))
                memory = Path(logs_dir) / "memory"
                if pass_name == "inventory":
                    return """```json
{"mode":"first_run","sources_to_read":[],"existing_pages_to_read":[],"likely_pages_to_create":["Project"],"likely_pages_to_update":[],"backfill_sources_to_sample":[],"rationale":"test"}
```"""
                if pass_name == "opportunities":
                    self.assertIn('"likely_pages_to_create"', instruction)
                    return "```json\n" + json.dumps({
                        "page_candidates": [{
                            "title": "Project",
                            "kind": "project",
                            "evidence": ["screen/filtered.jsonl: Project evidence"],
                            "novelty": "No existing page covers it.",
                            "confidence": 0.7,
                            "recommended_action": "create_full",
                        }],
                        "rejected_candidates": [],
                        "notes": "found project",
                    }) + "\n```"
                if pass_name == "update":
                    return "```json\n" + json.dumps({
                        "create_pages": [],
                        "update_pages": [],
                        "deferred_page_candidates": [{
                            "title": "Deferred Project",
                            "kind": "project",
                            "evidence": ["screen/filtered.jsonl: Deferred project evidence"],
                            "novelty": "Discovered while reading existing pages.",
                            "confidence": 0.55,
                            "recommended_action": "create_stub",
                        }],
                        "notes": "no existing pages",
                    }) + "\n```"
                if pass_name == "create":
                    self.assertIn('"page_candidates"', instruction)
                    self.assertIn("## Deferred Page Candidates From Update", instruction)
                    self.assertIn('"Deferred Project"', instruction)
                    return "```json\n" + json.dumps({
                        "create_pages": [{
                            "path": "project.md",
                            "markdown": "---\ntitle: Project\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nProject page. [c:0.7]\n",
                        }],
                        "update_pages": [],
                        "notes": "created project.md",
                    }) + "\n```"
                if pass_name == "finalize":
                    today = datetime.now().strftime("%Y-%m-%d")
                    return "```json\n" + json.dumps({
                        "create_pages": [],
                        "update_pages": [
                            {"path": "index.md", "markdown": "# Memory Index\n\n- Project — project.md\n"},
                            {"path": "log.md", "markdown": f"# Memory Log\n\n## {today}\n- Created Project.\n"},
                        ],
                        "notes": "finalized",
                    }) + "\n```"
                raise AssertionError(pass_name)

            with patch.object(ingest, "_run_agent_pass", side_effect=fake_pass):
                result = ingest.run(str(logs), model="fake-model")

            self.assertEqual([name for name, _ in calls], ["inventory", "opportunities", "update", "create", "finalize"])
            self.assertTrue((logs / "memory" / ".last_ingest").exists())
            self.assertIn("## Inventory", result)
            self.assertIn("## Page Opportunities", result)
            self.assertIn("## Create", result)
            self.assertIn("created project.md", result)

    def test_run_does_not_checkpoint_on_bad_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir()

            with patch.object(ingest, "_run_agent_pass", return_value="no json"):
                with self.assertRaises(ValueError):
                    ingest.run(str(logs), model="fake-model")

            self.assertFalse((logs / "memory" / ".last_ingest").exists())

    def test_run_does_not_checkpoint_when_final_validation_fails(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir()

            def fake_pass(pass_name, instruction, logs_dir, model, api_key, on_round, subagent_model, subagent_api_key):
                memory = Path(logs_dir) / "memory"
                if pass_name == "inventory":
                    return """```json
{"mode":"first_run","sources_to_read":[],"existing_pages_to_read":[],"likely_pages_to_create":["Project"],"likely_pages_to_update":[],"backfill_sources_to_sample":[],"rationale":"test"}
```"""
                if pass_name == "opportunities":
                    return "```json\n" + json.dumps({
                        "page_candidates": [{
                            "title": "Project",
                            "kind": "project",
                            "evidence": ["screen/filtered.jsonl: Project evidence"],
                            "novelty": "No existing page covers it.",
                            "confidence": 0.7,
                            "recommended_action": "create_full",
                        }],
                        "rejected_candidates": [],
                        "notes": "found project",
                    }) + "\n```"
                if pass_name == "update":
                    return "```json\n" + json.dumps({
                        "create_pages": [],
                        "update_pages": [],
                        "notes": "no existing pages",
                    }) + "\n```"
                if pass_name == "create":
                    return "```json\n" + json.dumps({
                        "create_pages": [{
                            "path": "project.md",
                            "markdown": "---\ntitle: Project\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nProject page.\n",
                        }],
                        "update_pages": [],
                        "notes": "updated",
                    }) + "\n```"
                return "```json\n" + json.dumps({"create_pages": [], "update_pages": [], "notes": "did not repair index or log"}) + "\n```"

            with patch.object(ingest, "_run_agent_pass", side_effect=fake_pass):
                with self.assertRaises(RuntimeError):
                    ingest.run(str(logs), model="fake-model")

            self.assertFalse((logs / "memory" / ".last_ingest").exists())

    def test_validation_checks_all_memory_pages_for_index_entries(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "logs" / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "project.md").write_text(
                "---\ntitle: Project\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nProject page.\n"
            )
            (memory / "person.md").write_text(
                "---\ntitle: Person\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nPerson page.\n"
            )
            (memory / "index.md").write_text("# Memory Index\n\n- Project — project.md\n")
            (memory / "log.md").write_text("## 2026-05-03\n- Updated Project.\n")
            today = "2026-05-03"

            issues = ingest._validate_wiki(memory, today)

            self.assertIn(
                {"code": "index_missing_page", "path": "person.md", "message": "Content page is not represented in index.md by path or title"},
                issues,
            )

    def test_validation_detects_unresolved_wiki_links(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "logs" / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "project.md").write_text(
                "---\ntitle: Project\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nUses [[GitHub]] and [[Existing Tool]].\n"
            )
            (memory / "existing.md").write_text(
                "---\ntitle: Existing Tool\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nExisting page.\n"
            )
            (memory / "index.md").write_text("# Memory Index\n\n- Project — project.md\n- Existing Tool — existing.md\n")
            (memory / "log.md").write_text("## 2026-05-03\n- Updated Project.\n")

            issues = ingest._validate_wiki(memory, "2026-05-03")

            self.assertIn(
                {
                    "code": "unresolved_wiki_link",
                    "path": "project.md",
                    "target": "GitHub",
                    "message": "Wiki link [[GitHub]] does not resolve to an existing page or index entry",
                },
                issues,
            )
            self.assertFalse(any(issue.get("target") == "Existing Tool" for issue in issues))

    def test_update_prompt_includes_update_rules_without_stage_label(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            memory = logs / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "project.md").write_text(
                "---\ntitle: Project\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nProject summary.\n"
            )
            inputs = ingest.IngestInputs(
                mode="incremental",
                last_ingest=datetime(2025, 1, 1),
                new_inputs_list="- chats/chat_1/conversation.md",
                active_conversations=[],
                chats=[],
                audio=[],
                tada_feedback=[],
                modified_streams=[],
            )
            prompt = ingest._update_prompt(
                "2026-05-03 12:00",
                str(logs),
                memory,
                inputs,
                {
                    "mode": "incremental",
                    "sources_to_read": [],
                    "existing_pages_to_read": [],
                    "likely_pages_to_create": [],
                    "likely_pages_to_update": [],
                    "backfill_sources_to_sample": [],
                    "rationale": "test",
                },
            )

            self.assertIn("Output existing content-page patch operations only.", prompt)
            self.assertIn("Do not create new pages in this pass.", prompt)
            self.assertIn("Do not return full-page markdown for existing pages.", prompt)
            self.assertIn("Do not update `index.md`, `log.md`, or `schema.md`.", prompt)
            self.assertIn("Do not call `write_file` or `edit_file`", prompt)
            self.assertIn('"create_pages"', prompt)
            self.assertIn("## Existing Content Page Metadata", prompt)
            self.assertIn("`project.md` — title: Project", prompt)
            self.assertIn("Preserve source dates exactly.", prompt)
            self.assertIn("Do not spend tool calls only checking whether a planned wiki page exists", prompt)
            self.assertIn("Use shell analysis, search, or bounded discovery", prompt)
            self.assertIn("Planning is useful when it improves coverage.", prompt)
            self.assertIn("Do not use PlanUpdate just to mark routine items complete", prompt)
            self.assertIn("Use `deferred_page_candidates` to hand off missing page opportunities", prompt)
            self.assertIn('"deferred_page_candidates"', prompt)
            self.assertIn('"patches"', prompt)
            self.assertIn("append_to_section", prompt)
            self.assertIn("Avoid leaving dangling `[[wiki-links]]`", prompt)
            self.assertNotIn("You are doing the UPDATE pass", prompt)

    def test_update_patch_ops_preserve_existing_page_content(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "logs" / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "work-patterns.md").write_text(
                "---\n"
                "title: Work Patterns\n"
                "confidence: 0.8\n"
                "last_updated: 2026-05-01\n"
                "---\n\n"
                "# Work Patterns\n\n"
                "## Existing Detail\n\n"
                "A long-standing detail that must survive updates.\n"
            )
            result = "```json\n" + json.dumps({
                "create_pages": [],
                "update_pages": [{
                    "path": "work-patterns.md",
                    "patches": [
                        {"type": "update_frontmatter", "fields": {"last_updated": "2026-05-06", "confidence": 0.82}},
                        {"type": "append_to_section", "heading": "## Existing Detail", "markdown": "- New evidence. [c:0.8]"},
                    ],
                }],
                "notes": "patched work patterns",
            }) + "\n```"

            ops, notes = ingest._parse_update_patch_ops(result, memory)
            changed = ingest._apply_update_patch_ops(memory, ops)
            text = (memory / "work-patterns.md").read_text()

            self.assertEqual(changed, ["work-patterns.md"])
            self.assertEqual(notes, "patched work patterns")
            self.assertIn("last_updated: 2026-05-06", text)
            self.assertIn("confidence: 0.82", text)
            self.assertIn("A long-standing detail that must survive updates.", text)
            self.assertIn("- New evidence. [c:0.8]", text)

    def test_update_patch_ops_reject_full_page_markdown(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "logs" / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "project.md").write_text(
                "---\ntitle: Project\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\n# Project\n\nOld detail.\n"
            )
            result = "```json\n" + json.dumps({
                "create_pages": [],
                "update_pages": [{
                    "path": "project.md",
                    "markdown": "---\ntitle: Project\nconfidence: 0.9\nlast_updated: 2026-05-06\n---\n\nReplacement.\n",
                }],
                "notes": "bad",
            }) + "\n```"

            with self.assertRaises(ValueError):
                ingest._parse_update_patch_ops(result, memory)

    def test_update_patch_ops_parse_deferred_page_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "logs" / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "work-patterns.md").write_text(
                "---\ntitle: Work Patterns\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\n# Work Patterns\n\nOld detail.\n"
            )
            result = "```json\n" + json.dumps({
                "create_pages": [],
                "update_pages": [{
                    "path": "work-patterns.md",
                    "patches": [
                        {"type": "append_section", "heading": "## New Evidence", "markdown": "A split candidate emerged. [c:0.7]"},
                    ],
                }],
                "deferred_page_candidates": [{
                    "title": "PowerNap Fuse Layer",
                    "kind": "project",
                    "evidence": ["work-patterns.md: recurring implementation detail needs its own page"],
                    "novelty": "Existing Work Patterns page is too broad for this implementation detail.",
                    "confidence": 0.65,
                    "recommended_action": "create_stub",
                }],
                "notes": "handoff",
            }) + "\n```"

            ops, notes = ingest._parse_update_patch_ops(result, memory)

            self.assertEqual(notes, "handoff")
            self.assertEqual(ops["deferred_page_candidates"][0]["title"], "PowerNap Fuse Layer")

    def test_opportunity_and_create_prompts_bias_toward_new_pages(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            memory = logs / "memory"
            ingest._bootstrap_memory(memory)
            inputs = ingest.IngestInputs(
                mode="incremental",
                last_ingest=datetime(2025, 1, 1),
                new_inputs_list="- chats/chat_1/conversation.md",
                active_conversations=[],
                chats=[],
                audio=[],
                tada_feedback=[],
                modified_streams=[],
            )
            inventory = {
                "mode": "incremental",
                "sources_to_read": ["chats/chat_1/conversation.md"],
                "existing_pages_to_read": [],
                "likely_pages_to_create": ["New Project"],
                "likely_pages_to_update": [],
                "backfill_sources_to_sample": [],
                "rationale": "test",
            }
            opportunities = {
                "page_candidates": [{
                    "title": "New Project",
                    "kind": "project",
                    "evidence": ["chats/chat_1/conversation.md: grounded evidence"],
                    "novelty": "No existing page covers it.",
                    "confidence": 0.45,
                    "recommended_action": "create_stub",
                }],
                "rejected_candidates": [],
                "notes": "found new project",
            }

            opportunity_prompt = ingest._opportunity_prompt("2026-05-03 12:00", str(logs), memory, inputs, inventory)
            create_prompt = ingest._create_prompt(
                "2026-05-03 12:00",
                str(logs),
                memory,
                inputs,
                inventory,
                opportunities,
                [{
                    "title": "Deferred Project",
                    "kind": "project",
                    "evidence": ["project.md: split candidate"],
                    "novelty": "Needs a focused page.",
                    "confidence": 0.6,
                    "recommended_action": "create_stub",
                }],
            )

            self.assertIn("Output page opportunity candidates only.", opportunity_prompt)
            self.assertIn("Mark early but grounded signals as `create_stub`", opportunity_prompt)
            self.assertIn('"page_candidates"', opportunity_prompt)
            self.assertIn("If new inputs exist and no candidates are returned", opportunity_prompt)
            self.assertIn("Output new content-page operations only.", create_prompt)
            self.assertIn("This pass exists to grow the wiki.", create_prompt)
            self.assertIn("Prefer 3-8 focused new pages", create_prompt)
            self.assertIn('"New Project"', create_prompt)
            self.assertIn("## Deferred Page Candidates From Update", create_prompt)
            self.assertIn('"Deferred Project"', create_prompt)

    def test_parse_opportunities_validates_candidate_shape(self):
        payload = {
            "page_candidates": [{
                "title": "Project",
                "kind": "project",
                "evidence": ["screen/filtered.jsonl: evidence"],
                "novelty": "new",
                "confidence": 0.5,
                "recommended_action": "create_stub",
            }],
            "rejected_candidates": [{"title": "Old", "reason": "duplicate"}],
            "notes": "ok",
        }

        parsed = ingest._parse_opportunities("```json\n" + json.dumps(payload) + "\n```")

        self.assertEqual(parsed["page_candidates"][0]["title"], "Project")

    def test_inventory_prompt_allows_first_run_discovery_without_broad_source_rules(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            memory = logs / "memory"
            ingest._bootstrap_memory(memory)
            screen = logs / "screen"
            screen.mkdir(parents=True)
            (screen / "filtered.jsonl").write_text(
                json.dumps({"timestamp": datetime(2025, 1, 2).timestamp(), "source_name": "screen", "text": "Clicked Memex"}) + "\n"
            )
            inputs = ingest.IngestInputs(
                mode="first_run",
                last_ingest=None,
                new_inputs_list="- screen/filtered.jsonl",
                active_conversations=[],
                chats=[],
                audio=[],
                tada_feedback=[],
                modified_streams=["screen/filtered.jsonl"],
            )
            prompt = ingest._inventory_prompt("2026-05-03 12:00", str(logs), memory, inputs)

            self.assertIn("For first runs, discovery is expected", prompt)
            self.assertIn("## Changed Input Preview", prompt)
            self.assertIn("Clicked Memex", prompt)
            self.assertIn("inspect the source layout and sample available source files", prompt)
            self.assertIn("Keep discovery purposeful and bounded", prompt)
            self.assertNotIn("Use subagents for independent source groups", prompt)

    def test_finalize_prompt_includes_changed_page_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            memory = logs / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "project.md").write_text(
                "---\ntitle: Project\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nProject page summary.\n"
            )
            inputs = ingest.IngestInputs(
                mode="first_run",
                last_ingest=None,
                new_inputs_list="- screen/filtered.jsonl",
                active_conversations=[],
                chats=[],
                audio=[],
                tada_feedback=[],
                modified_streams=[],
            )
            inventory = {
                "mode": "first_run",
                "sources_to_read": [],
                "existing_pages_to_read": [],
                "likely_pages_to_create": [],
                "likely_pages_to_update": [],
                "backfill_sources_to_sample": [],
                "rationale": "test",
            }

            prompt = ingest._finalize_prompt(
                "2026-05-03 12:00",
                str(logs),
                memory,
                inputs,
                inventory,
                ["project.md"],
                [],
            )

            self.assertIn("## Changed Page Metadata", prompt)
            self.assertIn("## All Content Page Metadata", prompt)
            self.assertIn("`project.md` — title: Project", prompt)
            self.assertIn("Project page summary.", prompt)
            self.assertIn("Do not crawl directories", prompt)
            self.assertIn("Do not list or read content pages just to build `index.md`", prompt)
            self.assertIn("Do not use shell redirection, append operators, heredocs", prompt)
            self.assertIn("Do not call `write_file` or `edit_file`", prompt)
            self.assertIn('"update_pages"', prompt)
            self.assertIn("Planning is optional. Keep it compact", prompt)
            self.assertIn("read them together once", prompt)


if __name__ == "__main__":
    unittest.main()

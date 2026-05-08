from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from unittest.mock import patch

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from apps.memory import ingest
from apps.memory.schemas.structured import ExistingPageUpdatePayload, FinalizePageOpsPayload, NewPageCreatePayload
from apps.moments.core.incremental import DEFAULT_MISSING_CHECKPOINT_AGE, read_checkpoint
from apps.moments.runtime.scheduler import scheduled_service_due


def _write_checkpoint(path: Path, value: str = "2025-01-01T00:00:00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")


class _FakeMemoryAgent:
    def __init__(self, result: str):
        self.result = result
        self.max_rounds = None
        self.on_round = None
        self.calls: list[dict] = []

    def run(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return self.result


class MemoryIngestTests(unittest.TestCase):
    def test_checkpoint_reader_seeds_missing_checkpoint_to_24_hours_ago(self):
        with tempfile.TemporaryDirectory() as d:
            checkpoint = Path(d) / ".last_ingest"
            before = datetime.now() - DEFAULT_MISSING_CHECKPOINT_AGE

            value = read_checkpoint(checkpoint, default_age=DEFAULT_MISSING_CHECKPOINT_AGE)

            after = datetime.now() - DEFAULT_MISSING_CHECKPOINT_AGE
            self.assertIsNotNone(value)
            assert value is not None
            self.assertTrue(before <= value <= after)
            self.assertEqual(read_checkpoint(checkpoint), value.replace(microsecond=0))

    def test_checkpoint_reader_accepts_fractional_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            checkpoint = Path(d) / ".last_ingest"
            checkpoint.write_text("2026-05-06T22:43:21.335231\n")

            self.assertEqual(read_checkpoint(checkpoint), datetime(2026, 5, 6, 22, 43, 21, 335231))

    def test_memory_service_uses_ingest_checkpoint_for_24_hour_catchup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            last_run = root / ".last_ingest"

            self.assertTrue(scheduled_service_due("daily at 3am", last_run))
            self.assertTrue(last_run.exists())
            seeded = datetime.fromisoformat(last_run.read_text().strip())
            self.assertTrue(datetime.now() - timedelta(hours=25) <= seeded <= datetime.now() - timedelta(hours=23))
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

    def test_page_agent_env_knobs_parse_with_bounds(self):
        with patch.dict(
            "os.environ",
            {"MEMORY_PAGE_AGENT_CONCURRENCY": "7", "MEMORY_PAGE_AGENT_MAX_ROUNDS": "4"},
        ):
            self.assertEqual(ingest._page_agent_concurrency(), 7)
            self.assertEqual(ingest._page_agent_max_rounds(), 5)

        with patch.dict(
            "os.environ",
            {"MEMORY_PAGE_AGENT_CONCURRENCY": "bad", "MEMORY_PAGE_AGENT_MAX_ROUNDS": "bad"},
        ):
            self.assertEqual(ingest._page_agent_concurrency(), ingest.DEFAULT_PAGE_AGENT_CONCURRENCY)
            self.assertEqual(ingest._page_agent_max_rounds(), ingest.DEFAULT_PAGE_AGENT_MAX_ROUNDS)

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

            def fake_pass(pass_name, instruction, logs_dir, model, api_key, on_round, subagent_model, subagent_api_key, **kwargs):
                calls.append((pass_name, instruction))
                if pass_name == "create_page":
                    self.assertIs(kwargs.get("final_response_model"), NewPageCreatePayload)
                    self.assertEqual(kwargs.get("final_metadata_app"), "memory_create_page")
                if pass_name == "finalize":
                    self.assertIs(kwargs.get("final_response_model"), FinalizePageOpsPayload)
                    self.assertEqual(kwargs.get("final_metadata_app"), "memory_finalize_pages")
                if pass_name == "inventory":
                    return """```json
{"mode":"no_new_data","sources_to_read":[],"existing_pages_to_read":[],"likely_pages_to_create":["Project"],"likely_pages_to_update":[],"backfill_sources_to_sample":[],"rationale":"test"}
```"""
                if pass_name == "create_page":
                    self.assertIn("## Candidate Page", instruction)
                    self.assertIn("`Project`", instruction)
                    return "```json\n" + json.dumps({
                        "create_pages": [{
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

            self.assertEqual([name for name, _ in calls], ["inventory", "create_page", "finalize"])
            self.assertTrue((logs / "memory" / ".last_ingest").exists())
            self.assertIn("## Inventory", result)
            self.assertIn("## Content", result)
            self.assertIn("created project.md", result)

    def test_run_emits_monotonic_aggregate_progress(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir()
            emitted: list[tuple[int, int]] = []
            emitted_lock = Lock()

            def on_round(num_turns: int, max_turns: int) -> None:
                with emitted_lock:
                    emitted.append((num_turns, max_turns))

            def fake_pass(pass_name, instruction, logs_dir, model, api_key, on_round, subagent_model, subagent_api_key, **kwargs):
                if pass_name == "inventory":
                    on_round(1, 50)
                    on_round(25, 50)
                    return """```json
{"mode":"no_new_data","sources_to_read":[],"existing_pages_to_read":[],"likely_pages_to_create":["Alpha","Beta"],"likely_pages_to_update":[],"backfill_sources_to_sample":[],"rationale":"test"}
```"""
                if pass_name == "create_page":
                    title = "Alpha" if "`Alpha`" in instruction else "Beta"
                    if title == "Alpha":
                        on_round(20, 20)
                    else:
                        on_round(1, 20)
                        on_round(20, 20)
                    return "```json\n" + json.dumps({
                        "create_pages": [{
                            "markdown": f"---\ntitle: {title}\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\n{title} page. [c:0.7]\n",
                        }],
                        "update_pages": [],
                        "notes": f"created {title}",
                    }) + "\n```"
                if pass_name == "finalize":
                    today = datetime.now().strftime("%Y-%m-%d")
                    on_round(1, 50)
                    on_round(50, 50)
                    return "```json\n" + json.dumps({
                        "create_pages": [],
                        "update_pages": [
                            {"path": "index.md", "markdown": "# Memory Index\n\n- Alpha — alpha.md\n- Beta — beta.md\n"},
                            {"path": "log.md", "markdown": f"# Memory Log\n\n## {today}\n- Created Alpha and Beta.\n"},
                        ],
                        "notes": "finalized",
                    }) + "\n```"
                raise AssertionError(pass_name)

            with patch.object(ingest, "_run_agent_pass", side_effect=fake_pass):
                ingest.run(str(logs), model="fake-model", on_round=on_round)

            pcts = [num for num, max_turns in emitted if max_turns == 100]
            self.assertEqual(pcts, sorted(pcts))
            self.assertIn(20, pcts)
            self.assertIn(80, pcts)
            self.assertEqual(pcts[-1], 100)

    def test_run_uses_per_page_update_payload_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            memory = logs / "memory"
            ingest._bootstrap_memory(memory)
            (memory / "project.md").write_text(
                "---\n"
                "title: Project\n"
                "confidence: 0.7\n"
                "last_updated: 2026-05-03\n"
                "---\n\n"
                "# Project\n\n"
                "## Evidence\n\n"
                "Existing detail.\n"
            )

            today = datetime.now().strftime("%Y-%m-%d")
            agents = [
                _FakeMemoryAgent("""```json
{"mode":"no_new_data","sources_to_read":[],"existing_pages_to_read":["project.md"],"likely_pages_to_create":[],"likely_pages_to_update":["project.md"],"backfill_sources_to_sample":[],"rationale":"test"}
```"""),
                _FakeMemoryAgent(json.dumps({
                    "create_pages": [],
                    "update_pages": [{
                        "markdown": (
                            "---\n"
                            "title: Project\n"
                            "confidence: 0.8\n"
                            "last_updated: 2026-05-03\n"
                            "---\n\n"
                            "# Project\n\n"
                            "## Evidence\n\n"
                            "Existing detail.\n"
                            "- Structured update evidence. [c:0.8]\n"
                        ),
                    }],
                    "notes": "updated project.md",
                })),
                _FakeMemoryAgent("```json\n" + json.dumps({
                    "create_pages": [],
                    "update_pages": [
                        {"path": "index.md", "markdown": "# Memory Index\n\n- Project — project.md\n"},
                        {"path": "log.md", "markdown": f"# Memory Log\n\n## {today}\n- Updated Project.\n"},
                    ],
                    "notes": "finalized",
                }) + "\n```"),
            ]

            with patch.object(ingest, "build_agent", side_effect=[(agent, None) for agent in agents]):
                result = ingest.run(str(logs), model="fake-model")

            update_kwargs = agents[1].calls[0]["kwargs"]
            self.assertIs(update_kwargs["final_response_model"], ExistingPageUpdatePayload)
            self.assertEqual(update_kwargs["final_metadata_app"], "memory_update_page")
            self.assertIs(agents[2].calls[0]["kwargs"]["final_response_model"], FinalizePageOpsPayload)
            self.assertEqual(agents[2].calls[0]["kwargs"]["final_metadata_app"], "memory_finalize_pages")
            self.assertIn("Applied content page ops: project.md", result)
            self.assertIn("Structured update evidence", (memory / "project.md").read_text())
            self.assertTrue((memory / ".last_ingest").exists())

    def test_run_keeps_seed_checkpoint_on_bad_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir()

            with patch.object(ingest, "_run_agent_pass", return_value="no json"):
                with self.assertRaises(ValueError):
                    ingest.run(str(logs), model="fake-model")

            seeded = read_checkpoint(logs / "memory" / ".last_ingest")
            self.assertIsNotNone(seeded)
            assert seeded is not None
            self.assertTrue(datetime.now() - timedelta(hours=25) <= seeded <= datetime.now() - timedelta(hours=23))

    def test_run_keeps_seed_checkpoint_when_final_validation_fails(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            logs.mkdir()

            def fake_pass(pass_name, instruction, logs_dir, model, api_key, on_round, subagent_model, subagent_api_key, **kwargs):
                if pass_name == "inventory":
                    return """```json
{"mode":"no_new_data","sources_to_read":[],"existing_pages_to_read":[],"likely_pages_to_create":["Project"],"likely_pages_to_update":[],"backfill_sources_to_sample":[],"rationale":"test"}
```"""
                if pass_name == "create_page":
                    return "```json\n" + json.dumps({
                        "create_pages": [{
                            "markdown": "---\ntitle: Project\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nProject page.\n",
                        }],
                        "update_pages": [],
                        "notes": "updated",
                    }) + "\n```"
                return "```json\n" + json.dumps({"create_pages": [], "update_pages": [], "notes": "did not repair index or log"}) + "\n```"

            with patch.object(ingest, "_run_agent_pass", side_effect=fake_pass):
                with self.assertRaises(RuntimeError):
                    ingest.run(str(logs), model="fake-model")

            seeded = read_checkpoint(logs / "memory" / ".last_ingest")
            self.assertIsNotNone(seeded)
            assert seeded is not None
            self.assertTrue(datetime.now() - timedelta(hours=25) <= seeded <= datetime.now() - timedelta(hours=23))

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

    def test_update_page_prompt_scopes_agent_to_one_page(self):
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
            prompt = ingest._update_page_prompt(
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
                "project.md",
            )

            self.assertIn("Output one existing content-page replacement only.", prompt)
            self.assertIn("This agent owns exactly one target page.", prompt)
            self.assertIn("Path: `project.md`", prompt)
            self.assertIn("Project summary.", prompt)
            self.assertIn("Return the full replacement markdown for the target page", prompt)
            self.assertIn("Do not update `index.md`, `log.md`, or `schema.md`.", prompt)
            self.assertIn("Do not call `write_file` or `edit_file`", prompt)
            self.assertIn("`create_pages`", prompt)
            self.assertIn("one-item `update_pages`", prompt)
            self.assertIn("## Existing Content Page Metadata", prompt)
            self.assertIn("`project.md` — title: Project", prompt)
            self.assertIn("Preserve source dates exactly.", prompt)
            self.assertIn("Use shell analysis, search, or bounded discovery", prompt)
            self.assertIn("Planning is optional.", prompt)
            self.assertIn("do not use PlanUpdate just to mark routine items complete", prompt)
            self.assertIn("likely next need", prompt)
            self.assertIn("Avoid leaving dangling `[[wiki-links]]`", prompt)
            self.assertNotIn("You are doing the UPDATE pass", prompt)
            self.assertNotIn("patches", prompt)
            self.assertNotIn("append_to_section", prompt)

    def test_existing_page_write_ops_replace_one_page(self):
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
                    "markdown": (
                        "---\n"
                        "title: Work Patterns\n"
                        "confidence: 0.82\n"
                        "last_updated: 2026-05-06\n"
                        "---\n\n"
                        "# Work Patterns\n\n"
                        "## Existing Detail\n\n"
                        "A long-standing detail that must survive updates.\n"
                        "- New evidence. [c:0.8]\n"
                    ),
                }],
                "notes": "updated work patterns",
            }) + "\n```"

            ops, notes = ingest._parse_page_ops(
                result,
                memory,
                allow_special=False,
                payload_model=ExistingPageUpdatePayload,
                require_update_exists=True,
                default_update_path="work-patterns.md",
            )
            changed = ingest._apply_page_ops(memory, ops)
            text = (memory / "work-patterns.md").read_text()

            self.assertEqual(changed, ["work-patterns.md"])
            self.assertEqual(notes, "updated work patterns")
            self.assertIn("last_updated: 2026-05-06", text)
            self.assertIn("confidence: 0.82", text)
            self.assertIn("A long-standing detail that must survive updates.", text)
            self.assertIn("- New evidence. [c:0.8]", text)

    def test_existing_page_update_payload_rejects_wrong_shape(self):
        with self.assertRaises(ValidationError):
            ExistingPageUpdatePayload.model_validate({
                "create_pages": [{"markdown": "---\ntitle: New\n---\n"}],
                "update_pages": [],
                "notes": "bad",
            })
        with self.assertRaises(ValidationError):
            ExistingPageUpdatePayload.model_validate({
                "create_pages": [],
                "update_pages": [
                    {"markdown": "---\ntitle: One\n---\n"},
                    {"markdown": "---\ntitle: Two\n---\n"},
                ],
                "notes": "bad",
            })

    def test_parse_page_ops_rejects_update_target_that_does_not_exist(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "logs" / "memory"
            ingest._bootstrap_memory(memory)
            result = "```json\n" + json.dumps({
                "create_pages": [],
                "update_pages": [{
                    "markdown": "---\ntitle: Missing\nconfidence: 0.7\nlast_updated: 2026-05-03\n---\n\nMissing.\n",
                }],
                "notes": "bad",
            }) + "\n```"

            with self.assertRaises(ValueError):
                ingest._parse_page_ops(
                    result,
                    memory,
                    allow_special=False,
                    payload_model=ExistingPageUpdatePayload,
                    require_update_exists=True,
                )

    def test_create_candidate_prompt_biases_toward_new_pages(self):
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
            create_prompt = ingest._create_candidate_prompt(
                "2026-05-03 12:00",
                str(logs),
                memory,
                inputs,
                inventory,
                "New Project",
            )

            self.assertIn("Output at most one new content-page create operation.", create_prompt)
            self.assertIn("This agent owns exactly one candidate page.", create_prompt)
            self.assertIn("## Candidate Page", create_prompt)
            self.assertIn("`New Project`", create_prompt)
            self.assertIn("likely future relevance", create_prompt)
            self.assertIn("empty `create_pages` list", create_prompt)

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
            self.assertIn("`update_pages`", prompt)
            self.assertIn("Planning is optional. Keep it compact", prompt)
            self.assertIn("read them together once", prompt)


if __name__ == "__main__":
    unittest.main()

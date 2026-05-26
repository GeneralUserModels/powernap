from __future__ import annotations

import json
import sys
import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from apps.moments.steps import discover, promote
from apps.common import structured_completion as structured_completion_module
from apps.common.structured_ops import StructuredOpsError, extract_json_object
from apps.moments.core.candidates import (
    CandidateError,
    parse_discovery_result,
    parse_promotion_result,
    render_accepted_markdown,
    validate_candidate,
    write_candidates_jsonl,
)
from apps.moments.core.paths import summarize_tada_tasks
from apps.moments.api import routes as moments_routes
from apps.moments.runtime import execute
from apps.moments.runtime.scheduler import _execute_one_moment, load_run_history, scheduled_service_due, should_run
from apps.moments.schemas.structured import DiscoveryPayload


def _candidate(**overrides):
    base = {
        "id": "paper-digest",
        "slug": "paper-digest",
        "topic": "research",
        "title": "Paper Digest",
        "description": "Track relevant papers.",
        "cadence": "scheduled",
        "schedule": "daily at 8am",
        "trigger": "",
        "confidence": 0.8,
        "usefulness": 8,
        "specific_instructions": "Find new papers and summarize why they matter.",
        "desired_artifact": "A ranked feed of papers.",
        "likely_next_need": "The user will need to keep up with new research without manually scanning sources.",
        "why_now": "The user is actively researching.",
        "user_value": "Saves triage time.",
    }
    base.update(overrides)
    return base


class _FakeStructuredCompletion:
    def __init__(self, *results: str | Exception):
        self.results = list(results)
        self.instructions: list[str] = []

    def __call__(self, *, instruction, response_model, **kwargs):
        self.instructions.append(instruction)
        if not self.results:
            raise AssertionError("No fake structured completion result queued")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        try:
            payload = extract_json_object(result)
        except StructuredOpsError:
            try:
                payload = json.loads(result)
            except json.JSONDecodeError as exc:
                raise StructuredOpsError(f"invalid JSON: {exc}") from exc
        try:
            parsed = response_model.model_validate(payload)
        except ValidationError as exc:
            raise StructuredOpsError(f"structured output validation failed: {exc}") from exc
        return result, parsed


class _FakeToolAgent:
    def __init__(self, *results: str):
        self.results = list(results) or ["```json\n" + json.dumps({"tasks": [_candidate()]}) + "\n```"]
        self.max_rounds = None
        self.on_round = None
        self.messages: list[list[dict]] = []

    def run(self, messages, **kwargs):
        self.messages.append(messages)
        if not self.results:
            raise AssertionError("No fake tool agent result queued")
        return self.results.pop(0)


class _FakeExecuteAgent:
    def __init__(self, output_dir: Path, test_case: unittest.TestCase):
        self.output_dir = output_dir
        self.test_case = test_case
        self.max_rounds = None
        self.warning_round = None
        self.on_round = None
        self.messages: list[list[dict]] = []

    def run(self, messages, **kwargs):
        self.messages.append(messages)
        prompt = messages[0]["content"]
        self.test_case.assertIn("/output", prompt)
        self.test_case.assertIn("Start with an `index.md` page", prompt)
        self.test_case.assertIn("Use markdown links liberally", prompt)
        self.test_case.assertIn("Do not build a website or app", prompt)
        output_pages_dir = self.output_dir / "output"
        output_pages_dir.mkdir(parents=True)
        (output_pages_dir / "evidence.md").write_text("# Evidence\n\nA cited [source](https://example.com).\n")
        (output_pages_dir / "synthesis.md").write_text("# Synthesis\n\nUseful researched findings.\n")
        return "research complete"


class _FakeMissingResearchAgent:
    def __init__(self):
        self.max_rounds = None
        self.warning_round = None
        self.on_round = None
        self.messages: list[list[dict]] = []

    def run(self, messages, **kwargs):
        self.messages.append(messages)
        return "no file written"


def _filtered_row(timestamp: datetime, source_name: str, text: str, **extra):
    row = {
        "timestamp": timestamp.timestamp(),
        "source_name": source_name,
        "text": text,
        "source": {},
    }
    row.update(extra)
    return json.dumps(row)


def _discovery_state(root: Path) -> Path:
    return root / "logs-tada" / "_discovery"


def _tada_run_checkpoint(root: Path) -> Path:
    return root / "logs-tada" / ".last_run"


def _candidate_files(root: Path) -> list[Path]:
    return sorted((_discovery_state(root) / "candidates").glob("*.jsonl"))


class MomentsPipelineTests(unittest.TestCase):
    def test_execute_one_moment_runs_in_background_worker_and_records_success(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                logs = root / "logs"
                logs.mkdir()
                tada = root / "logs-tada"
                task_dir = tada / "research"
                task_dir.mkdir(parents=True)
                task_path = task_dir / "brief.md"
                task_path.write_text(
                    "---\n"
                    "title: Brief\n"
                    "description: Worker-backed run.\n"
                    "cadence: once\n"
                    "---\n\nBody\n"
                )
                results_dir = tada / "results"
                fm = {"title": "Brief", "description": "Worker-backed run.", "cadence": "once"}

                class FakeRunner:
                    def __init__(self):
                        self.calls = []

                    async def run(self, job_name, payload, on_event=None):
                        self.calls.append((job_name, payload))
                        output_dir = Path(payload["output_dir"])
                        output_pages = output_dir / "output"
                        output_pages.mkdir(parents=True)
                        (output_pages / "a.md").write_text("# A\n")
                        (output_pages / "b.md").write_text("# B\n")
                        (output_dir / "meta.json").write_text(json.dumps({
                            "title": "Brief",
                            "description": "Worker-backed run.",
                        }))
                        if on_event:
                            await on_event({
                                "type": "round",
                                "agent": "moment_run:brief",
                                "message": "Running: Brief",
                                "slug": "brief",
                                "cadence": "once",
                                "num_turns": 1,
                                "max_turns": 2,
                            })
                        return {"success": True}

                class FakeState:
                    def __init__(self):
                        self.moments_executor_sem = asyncio.BoundedSemaphore(1)
                        self.moments_runs_lock = asyncio.Lock()
                        self.background_job_runner = FakeRunner()
                        self.activities = []
                        self.broadcasts = []

                    async def broadcast_activity(self, *args, **kwargs):
                        self.activities.append((args, kwargs))

                    async def broadcast(self, event, data):
                        self.broadcasts.append((event, data))

                state = FakeState()
                await _execute_one_moment(
                    state,
                    task_path,
                    "brief",
                    fm,
                    {},
                    "once",
                    "",
                    str(logs),
                    results_dir,
                    tada,
                    "fake-model",
                    "secret-key",
                    None,
                )

                return state, load_run_history(results_dir)

        state, history = asyncio.run(run())
        self.assertEqual(state.background_job_runner.calls[0][0], "moments.execute")
        payload = state.background_job_runner.calls[0][1]
        self.assertEqual(payload["api_key"], "secret-key")
        self.assertEqual(payload["activity"]["agent"], "moment_run:brief")
        self.assertIn("brief", history)
        self.assertEqual([event for event, _ in state.broadcasts], ["moment_completed"])
        self.assertTrue(any(
            args and args[0] == "moment_run:brief" and (len(args) == 1 or args[1] is None)
            for args, _ in state.activities
        ))

    def test_scheduled_services_seed_missing_pipeline_checkpoint_to_24_hour_catchup(self):
        with tempfile.TemporaryDirectory() as d:
            last_run = Path(d) / ".last_run"
            self.assertTrue(scheduled_service_due("daily at 2am", last_run))
            self.assertTrue(last_run.exists())
            seeded = datetime.fromisoformat(last_run.read_text().strip())
            self.assertTrue(datetime.now() - timedelta(hours=25) <= seeded <= datetime.now() - timedelta(hours=23))
            last_run.write_text(datetime(2000, 1, 1).isoformat())
            self.assertTrue(scheduled_service_due("daily at 2am", last_run))

    def test_candidate_validation_for_cadences(self):
        self.assertEqual(validate_candidate(_candidate(cadence="once", schedule="daily at 8am")).schedule, "")
        self.assertEqual(validate_candidate(_candidate(cadence="trigger", schedule="", trigger="a deadline appears")).trigger, "a deadline appears")
        with self.assertRaises(CandidateError):
            validate_candidate(_candidate(cadence="scheduled", schedule=""))
        with self.assertRaises(CandidateError):
            validate_candidate(_candidate(cadence="trigger", trigger=""))

    def test_structured_candidate_payload_requires_cadence_fields(self):
        with self.assertRaises(ValidationError):
            DiscoveryPayload.model_validate({"tasks": [_candidate(cadence="scheduled", schedule="")]})
        with self.assertRaises(ValidationError):
            raw = _candidate(cadence="scheduled")
            raw.pop("schedule")
            DiscoveryPayload.model_validate({"tasks": [raw]})

    def test_markdown_render_uses_cadence_frontmatter(self):
        candidate = validate_candidate(_candidate(cadence="scheduled", schedule="Monday at 9am"))
        markdown = render_accepted_markdown(candidate)
        self.assertIn("cadence: scheduled", markdown)
        self.assertIn("schedule: Monday at 9am", markdown)
        self.assertNotIn("frequency:", markdown)

    def test_parse_promotion_selects_candidates(self):
        candidates = parse_discovery_result("```json\n" + json.dumps({"candidates": [_candidate()]}) + "\n```")
        promoted, rejected = parse_promotion_result(
            '```json\n{"ranked":[{"id":"paper-digest","score":9,"reason":"useful"}],"rejected":[]}\n```',
            candidates,
        )
        self.assertEqual([c.slug for c in promoted], ["paper-digest"])
        self.assertEqual(rejected, [])

    def test_scheduler_respects_cadence(self):
        self.assertTrue(should_run("once", "once", "", {}))
        self.assertFalse(should_run("once", "once", "", {"once": datetime.now().timestamp()}))
        self.assertFalse(should_run("trigger", "trigger", "", {}))
        self.assertTrue(should_run("daily", "scheduled", "daily at 12:01am", {}))

    def test_run_history_counts_failed_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            results = Path(d)
            (results / "_runs.jsonl").write_text(
                json.dumps({
                    "slug": "broken-once",
                    "started_at": 100.0,
                    "completed_at": 120.0,
                    "status": "failed",
                }) + "\n"
            )

            self.assertEqual(load_run_history(results), {"broken-once": 120.0})

    def test_output_pages_are_ordered_and_titled(self):
        with tempfile.TemporaryDirectory() as d:
            result_dir = Path(d)
            output_pages = result_dir / "output"
            output_pages.mkdir()
            (output_pages / "z-notes.md").write_text("# Zebra Notes\n\nBody\n")
            (output_pages / "overview.md").write_text("---\ntitle: Brief Overview\n---\n\nBody\n")
            (output_pages / "index.md").write_text("# Start Here\n\nBody\n")

            pages = moments_routes._list_output_pages(result_dir)

            self.assertEqual([p.name for p in pages], ["index.md", "overview.md", "z-notes.md"])
            self.assertEqual(moments_routes._page_meta(pages[1], output_pages)["title"], "Brief Overview")

    def test_resolve_output_page_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            tada = Path(d) / "logs-tada"
            output_pages = tada / "results" / "brief" / "output"
            output_pages.mkdir(parents=True)
            safe = output_pages / "safe.md"
            safe.write_text("# Safe\n")
            (tada / "results" / "brief" / "outside.md").write_text("# Outside\n")

            self.assertEqual(moments_routes._resolve_output_page(tada, "brief", "safe.md"), safe.resolve())
            self.assertIsNone(moments_routes._resolve_output_page(tada, "brief", "../outside.md"))
            self.assertIsNone(moments_routes._resolve_output_page(tada, "brief", "/etc/passwd"))

    def test_list_results_includes_markdown_backed_results(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tada = root / "logs-tada"
            task_dir = tada / "research"
            task_dir.mkdir(parents=True)
            (task_dir / "strategy-brief.md").write_text(
                "---\n"
                "title: Strategy Brief\n"
                "description: Prepare a concise interview briefing.\n"
                "cadence: once\n"
                "confidence: 0.80\n"
                "usefulness: 8\n"
                "---\n\nBody\n"
            )
            result_dir = tada / "results" / "strategy-brief"
            output_pages = result_dir / "output"
            output_pages.mkdir(parents=True)
            (output_pages / "evidence.md").write_text("# Evidence\n")
            (output_pages / "synthesis.md").write_text("# Synthesis\n")
            (result_dir / "meta.json").write_text(json.dumps({
                "title": "Strategy Brief",
                "description": "Markdown result.",
                "completed_at": "2026-05-06T00:00:00Z",
                "cadence": "once",
                "schedule": "",
            }))
            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        server=SimpleNamespace(
                            config=SimpleNamespace(tada_dir=str(tada)),
                        ),
                    ),
                ),
            )

            results = asyncio.run(moments_routes.list_results(request))

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["slug"], "strategy-brief")
            self.assertEqual(results[0]["page_count"], 2)

    def test_execute_generates_markdown_only_result(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            logs.mkdir()
            tada = root / "logs-tada"
            task_dir = tada / "research"
            task_dir.mkdir(parents=True)
            task_path = task_dir / "strategy-brief.md"
            task_path.write_text(
                "---\n"
                "title: Strategy Brief\n"
                "description: Prepare a concise interview briefing.\n"
                "cadence: once\n"
                "confidence: 0.80\n"
                "usefulness: 8\n"
                "---\n\n"
                "## Specific Instructions for Agent\n\n"
                "Create a brief report from the evidence.\n\n"
                "## Desired Artifact\n\n"
                "Structured report.\n\n"
                "## Evidence\n\n"
                "- memory/index.md mentions an interview thread\n"
            )
            output_dir = tada / "results" / "strategy-brief"
            output_dir.mkdir(parents=True)
            research_agent = _FakeExecuteAgent(output_dir, self)

            with patch.object(execute, "build_agent", return_value=(research_agent, None)):
                success = execute.run(str(task_path), str(output_dir), str(logs), model="fake")

            self.assertTrue(success)
            self.assertGreaterEqual(len(list((output_dir / "output").glob("*.md"))), 2)
            index = output_dir / "output" / "index.md"
            self.assertTrue(index.exists())
            self.assertIn("[Evidence](evidence.md)", index.read_text())
            self.assertTrue((output_dir / "meta.json").exists())
            self.assertFalse((output_dir / "index.html").exists())
            self.assertFalse((output_dir / "styles.css").exists())
            self.assertFalse((output_dir / "app.js").exists())
            self.assertFalse((output_dir / "templates").exists())
            self.assertEqual(len(research_agent.messages), 1)

    def test_execute_fails_when_research_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            logs.mkdir()
            tada = root / "logs-tada"
            task_dir = tada / "research"
            task_dir.mkdir(parents=True)
            task_path = task_dir / "strategy-brief.md"
            task_path.write_text(
                "---\n"
                "title: Strategy Brief\n"
                "description: Prepare a concise interview briefing.\n"
                "cadence: once\n"
                "confidence: 0.80\n"
                "usefulness: 8\n"
                "---\n\n"
                "## Specific Instructions for Agent\n\n"
                "Create a brief report from the evidence.\n"
            )
            output_dir = tada / "results" / "strategy-brief"
            research_agent = _FakeMissingResearchAgent()

            with patch.object(execute, "build_agent", return_value=(research_agent, None)):
                success = execute.run(str(task_path), str(output_dir), str(logs), model="fake")

            self.assertFalse(success)
            self.assertFalse(output_dir.exists())
            self.assertEqual(len(research_agent.messages), 2)
            self.assertIn("output folder is not ready", research_agent.messages[1][0]["content"])

    def test_discover_and_promote_with_fake_agents(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            logs.mkdir()
            screen = logs / "screen"
            screen.mkdir()
            recent = datetime.now() - timedelta(hours=23)
            (screen / "filtered.jsonl").write_text(
                _filtered_row(recent, "screen", "reading papers") + "\n"
            )
            draft_state_json = "```json\n" + json.dumps({"tasks": [_candidate()]}) + "\n```"
            promotion_json = '```json\n{"ranked":[{"id":"paper-digest","score":9,"reason":"useful"}],"rejected":[]}\n```'
            promote_structured = _FakeStructuredCompletion(promotion_json)

            fake_agent = _FakeToolAgent(draft_state_json)
            with patch.object(discover, "build_agent", return_value=(fake_agent, None)):
                discover.run(str(logs), model="fake")
            discover_prompt = fake_agent.messages[0][0]["content"]
            self.assertIn("screen/filtered.jsonl", discover_prompt)
            self.assertIn("Activity window starts after:", discover_prompt)
            self.assertIn("Activity Chunk", discover_prompt)
            self.assertIn("current implementation activity is not enough evidence", discover_prompt)
            self.assertIn("independent of the current edits", discover_prompt)
            candidate_files = _candidate_files(root)
            self.assertEqual(len(candidate_files), 1)
            self.assertTrue(_tada_run_checkpoint(root).exists())

            with patch.object(promote, "structured_completion", side_effect=promote_structured):
                promote.run(str(logs), model="fake")
            accepted = root / "logs-tada" / "research" / "paper-digest.md"
            self.assertTrue(accepted.exists())
            self.assertIn("cadence: scheduled", accepted.read_text())
            promote_prompt = promote_structured.instructions[0]
            self.assertIn("paper-digest", promote_prompt)
            self.assertIn("likely_next_need", promote_prompt)
            self.assertIn("Drop completed work", promote_prompt)
            self.assertIn("explicit confirmation", promote_prompt)
            self.assertIn("Do not call tools", promote_prompt)

    def test_invalid_discovery_keeps_seed_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            screen = logs / "screen"
            screen.mkdir(parents=True)
            recent = datetime.now() - timedelta(hours=23)
            (screen / "filtered.jsonl").write_text(
                _filtered_row(recent, "screen", "reading papers") + "\n"
            )
            with patch.object(discover, "build_agent", return_value=(_FakeToolAgent("not json", "not json"), None)):
                with self.assertRaises(CandidateError):
                    discover.run(str(logs), model="fake")
            seeded = datetime.fromisoformat(_tada_run_checkpoint(Path(d)).read_text().strip())
            self.assertTrue(datetime.now() - timedelta(hours=25) <= seeded <= datetime.now() - timedelta(hours=23))

    def test_first_discovery_defaults_to_24_hour_activity_window(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            screen = logs / "screen"
            screen.mkdir(parents=True)
            ancient = datetime.now() - timedelta(hours=25)
            recent = datetime.now() - timedelta(hours=23)
            (screen / "filtered.jsonl").write_text(
                _filtered_row(ancient, "screen", "ancient topic") + "\n"
                + _filtered_row(recent, "screen", "recent topic") + "\n"
            )
            fake_agent = _FakeToolAgent("```json\n" + json.dumps({"tasks": [_candidate()]}) + "\n```")

            with patch.object(discover, "build_agent", return_value=(fake_agent, None)):
                result = discover.run(str(logs), model="fake")

            discover_prompt = fake_agent.messages[0][0]["content"]
            self.assertIn("Activity window starts after:", discover_prompt)
            self.assertIn("Activity window starts after:", result)
            self.assertIn("recent topic", discover_prompt)
            self.assertNotIn("ancient topic", discover_prompt)

    def test_discovery_retries_malformed_json_once(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            screen = logs / "screen"
            screen.mkdir(parents=True)
            recent = datetime.now() - timedelta(hours=23)
            (screen / "filtered.jsonl").write_text(
                _filtered_row(recent, "screen", "reading papers") + "\n"
            )
            with patch.object(discover, "build_agent", return_value=(_FakeToolAgent(
                "not json",
                "```json\n" + json.dumps({"tasks": [_candidate()]}) + "\n```",
            ), None)):
                discover.run(str(logs), model="fake")

            self.assertTrue(_tada_run_checkpoint(Path(d)).exists())
            candidate_files = _candidate_files(Path(d))
            self.assertEqual(len(candidate_files), 1)

    def test_discovery_accepts_empty_tasks(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d) / "logs"
            screen = logs / "screen"
            screen.mkdir(parents=True)
            recent = datetime.now() - timedelta(hours=23)
            (screen / "filtered.jsonl").write_text(
                _filtered_row(recent, "screen", "reading papers") + "\n"
            )
            discovery_json = (
                "```json\n"
                + json.dumps({"tasks": []})
                + "\n```"
            )

            with patch.object(discover, "build_agent", return_value=(_FakeToolAgent(discovery_json), None)):
                result = discover.run(str(logs), model="fake")

            self.assertIn("Wrote 0 candidates", result)
            self.assertTrue(_tada_run_checkpoint(Path(d)).exists())

    def test_structured_completion_accepts_provider_rejected_but_valid_discovery_json(self):
        raw = json.dumps({"tasks": [_candidate()]})

        class _FakeSchemaError(Exception):
            raw_response = raw

        with patch.object(structured_completion_module.litellm, "JSONSchemaValidationError", _FakeSchemaError), \
             patch.object(structured_completion_module, "_litellm_structured_completion", side_effect=_FakeSchemaError()):
            text, payload = structured_completion_module.structured_completion(
                model="fake",
                instruction="instruction",
                response_model=DiscoveryPayload,
            )

            self.assertEqual(text, raw)
            self.assertEqual(payload.tasks[0].slug, "paper-digest")

    def test_promotion_retries_malformed_json_once(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            logs.mkdir()
            write_candidates_jsonl(root / "logs-tada", [validate_candidate(_candidate())])
            structured = _FakeStructuredCompletion(
                StructuredOpsError("structured output validation failed"),
                '```json\n{"ranked":[{"id":"paper-digest","score":9,"reason":"useful"}],"rejected":[]}\n```',
            )

            with patch.object(promote, "structured_completion", side_effect=structured):
                promote.run(str(logs), model="fake")

            self.assertTrue(_tada_run_checkpoint(root).exists())
            accepted = root / "logs-tada" / "research" / "paper-digest.md"
            self.assertTrue(accepted.exists())

    def test_promotion_routes_same_slug_to_existing_topic(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            logs.mkdir()
            tada = root / "logs-tada"
            accepted_dir = tada / "research"
            accepted_dir.mkdir(parents=True)
            (accepted_dir / "paper-digest.md").write_text(
                "---\n"
                "title: Paper Digest\n"
                "description: Track relevant papers.\n"
                "cadence: scheduled\n"
                "schedule: daily at 8am\n"
                "confidence: 0.80\n"
                "usefulness: 8\n"
                "---\n\nExisting body\n"
            )
            (tada / "results" / "paper-digest").mkdir(parents=True)
            write_candidates_jsonl(tada, [
                validate_candidate(
                    _candidate(
                        id="paper-digest-update",
                        slug="paper-digest",
                        topic="wrong-topic",
                        title="Updated Paper Digest",
                        specific_instructions="Use the new research signal to refresh the digest.",
                    )
                )
            ])
            structured = _FakeStructuredCompletion('```json\n{"ranked":[{"id":"paper-digest-update","score":9,"reason":"useful update"}],"rejected":[]}\n```')

            with patch.object(promote, "structured_completion", side_effect=structured):
                result = promote.run(str(logs), model="fake")

            accepted = accepted_dir / "paper-digest.md"
            accepted_text = accepted.read_text()
            self.assertIn("Updated Paper Digest", accepted_text)
            self.assertIn("Use the new research signal", accepted_text)
            self.assertFalse((tada / "wrong-topic" / "paper-digest.md").exists())
            self.assertIn('"topic": "research"', structured.instructions[0])
            self.assertNotIn('"topic": "wrong-topic"', structured.instructions[0])
            self.assertIn("Routed 1 same-slug candidates", result)

    def test_promotion_ranks_all_candidates_then_promotes_top_k(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs = root / "logs"
            logs.mkdir()
            first = validate_candidate(_candidate(id="first", slug="first", title="First"))
            second = validate_candidate(_candidate(id="second", slug="second", title="Second"))
            third = validate_candidate(_candidate(id="third", slug="third", title="Third"))
            write_candidates_jsonl(root / "logs-tada", [first, second, third])
            structured = _FakeStructuredCompletion(
                "```json\n"
                + json.dumps({
                    "ranked": [
                        {"id": "third", "score": 10, "reason": "best"},
                        {"id": "first", "score": 8, "reason": "next"},
                        {"id": "second", "score": 6, "reason": "viable"},
                    ],
                    "rejected": [],
                })
                + "\n```"
            )

            with patch.object(promote, "structured_completion", side_effect=structured):
                result = promote.run(str(logs), model="fake", n=2)

            self.assertIn('"slug": "second"', structured.instructions[0])
            self.assertTrue((root / "logs-tada" / "research" / "third.md").exists())
            self.assertTrue((root / "logs-tada" / "research" / "first.md").exists())
            self.assertFalse((root / "logs-tada" / "research" / "second.md").exists())
            self.assertIn("Ranked 3 of 3 candidates. Promoted top 2", result)

    def test_accepted_summary_marks_completed_once_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tada = root / "logs-tada"
            task_dir = tada / "research"
            task_dir.mkdir(parents=True)
            (task_dir / "brief.md").write_text(
                "---\n"
                "title: Brief\n"
                "description: Completed one-shot brief.\n"
                "cadence: once\n"
                "confidence: 0.80\n"
                "usefulness: 8\n"
                "---\n\nBody\n"
            )
            output_dir = tada / "results" / "brief" / "output"
            output_dir.mkdir(parents=True)
            (output_dir / "index.md").write_text("# Brief\n")

            summary = summarize_tada_tasks(tada)

            self.assertIn("research/brief", summary)
            self.assertIn("one-shot output already generated", summary)


if __name__ == "__main__":
    unittest.main()

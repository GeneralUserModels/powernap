"""Analyze user activity logs in one discovery pass and write candidates."""

from __future__ import annotations

import json
import logging
import shutil
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_none

load_dotenv()

from apps.common.activity_streams import (
    ActivityRow,
    merge_filtered_streams,
    parse_timestamp,
    render_activity_row,
)
from apps.common.structured_ops import StructuredOpsError, extract_json_object
from agent.builder import build_agent, _ensure_sandbox
from agent.cli_backends import (
    CliBackendConfig,
    cli_footer,
    run_stage_via_cli,
    strip_tool_plumbing,
)
from apps.moments.core.candidates import (
    CandidateError,
    MomentCandidate,
    discovery_state_dir,
    validate_candidate,
    write_candidates_jsonl,
)
from apps.moments.core.incremental import DEFAULT_MISSING_CHECKPOINT_AGE, read_checkpoint, write_checkpoint
from apps.moments.core.paths import summarize_tada_tasks
from apps.moments.schemas.structured import DiscoveryPayload

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
DISCOVER_TEMPLATE = (_PROMPTS / "discover.txt").read_text()

FILTERED_STREAM_SOURCES = [
    "screen/filtered.jsonl",
    "email/filtered.jsonl",
    "calendar/filtered.jsonl",
    "notifications/filtered.jsonl",
    "filesys/filtered.jsonl",
]
ACTIVITY_PREVIEW_MAX_ROWS = 240
ACTIVITY_PREVIEW_MAX_CHARS = 120_000
INITIAL_DISCOVERY_LOOKBACK = timedelta(days=2)
# Hard ceiling on the activity window — never look back more than this even
# if the last_run checkpoint is missing or stale.
MAX_ACTIVITY_WINDOW = timedelta(days=2)
STRUCTURED_OUTPUT_ATTEMPTS = 2
AGENT_IDEATION_MAX_ROUNDS = 200
logger = logging.getLogger(__name__)
_BUILD_AGENT_LOCK = Lock()

FilteredRow = ActivityRow


def _merged_filtered_rows(logs_path: Path, since: datetime | None):
    return merge_filtered_streams(logs_path, since, FILTERED_STREAM_SOURCES)


def _render_filtered_row(row: FilteredRow) -> str:
    return render_activity_row(row)


def _latest_filtered_timestamp(logs_path: Path) -> datetime | None:
    """Best-effort newest timestamp across the streaming filtered logs.

    Used only to anchor the "prioritize since" hint on a first run when
    there is no checkpoint. Cheap heuristic: read the tail of each
    filtered.jsonl and take the max parsed timestamp.
    """
    candidates: list[datetime] = []
    for rel in ("screen", "email", "calendar", "notifications", "filesys"):
        path = logs_path / rel / "filtered.jsonl"
        if not path.is_file():
            continue
        try:
            with path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                read = min(size, 64_000)
                f.seek(size - read)
                tail = f.read(read).decode(errors="replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = parse_timestamp(entry.get("timestamp")) if isinstance(entry, dict) else None
            if parsed is not None:
                candidates.append(parsed[0])
                break
    return max(candidates) if candidates else None


def _initial_discovery_since(logs_path: Path) -> datetime:
    latest = _latest_filtered_timestamp(logs_path)
    return (latest or datetime.now()) - INITIAL_DISCOVERY_LOOKBACK


def _activity_preview(logs_path: Path, since: datetime) -> tuple[str, str]:
    """Return metadata and a bounded recent row preview for the single pass.

    The preview is a hint, not the full source of truth. The discovery agent is
    expected to inspect source files directly when evaluating candidates.
    """
    preview_rows: deque[str] = deque()
    preview_chars = 0
    total_rows = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    source_counts: Counter[str] = Counter()

    for row in _merged_filtered_rows(logs_path, since):
        total_rows += 1
        first_ts = row.timestamp if first_ts is None else min(first_ts, row.timestamp)
        last_ts = row.timestamp if last_ts is None else max(last_ts, row.timestamp)
        source_counts[row.source_name] += 1
        rendered = _render_filtered_row(row)
        preview_rows.append(rendered)
        preview_chars += len(rendered) + 2
        while (
            len(preview_rows) > ACTIVITY_PREVIEW_MAX_ROWS
            or preview_chars > ACTIVITY_PREVIEW_MAX_CHARS
        ):
            removed = preview_rows.popleft()
            preview_chars -= len(removed) + 2

    if total_rows == 0:
        return (
            "Rows after activity window: 0\nPreview rows shown: 0",
            "(no recent filtered rows; use tools to inspect logs, memory, chats, accepted moments, and feedback for missed or recurring opportunities)",
        )

    source_summary = ", ".join(f"{name}={count}" for name, count in sorted(source_counts.items()))
    metadata = "\n".join([
        f"Rows after activity window: {total_rows}",
        f"Preview rows shown: {len(preview_rows)} most recent",
        f"Time range: {first_ts.strftime('%Y-%m-%d %H:%M:%S')} to {last_ts.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Sources: {source_summary}",
    ])
    return metadata, "\n\n".join(preview_rows)


def _feedback_state_summary(tada_dir: Path) -> str:
    state_path = tada_dir / "results" / "_moment_state.json"
    feedback = sorted((tada_dir / "results").glob("*/feedback_*.md")) if (tada_dir / "results").exists() else []
    parts: list[str] = []
    if state_path.exists():
        parts.append("State file exists at `results/_moment_state.json`; inspect it for dismissals, pins, thumbs, and pending updates.")
    if feedback:
        parts.append("Feedback files:\n" + "\n".join(f"- {p.relative_to(tada_dir)}" for p in feedback[-20:]))
    return "\n\n".join(parts) or "- (none)"


def _parse_discovery_payload(payload: dict[str, Any]) -> list[MomentCandidate]:
    raw_candidates = payload.get("tasks", [])
    if raw_candidates is None:
        raw_candidates = []
    if not isinstance(raw_candidates, list):
        raise CandidateError("discovery JSON tasks must be a list")
    tasks = [validate_candidate(raw) for raw in raw_candidates]
    seen: set[str] = set()
    for candidate in tasks:
        if candidate.id in seen or candidate.slug in seen:
            raise CandidateError(f"duplicate discovery task id or slug: {candidate.id}")
        seen.add(candidate.id)
        seen.add(candidate.slug)
    return tasks


def _parse_structured_discovery(payload: DiscoveryPayload) -> list[MomentCandidate]:
    return _parse_discovery_payload(payload.model_dump(exclude_none=True))


@retry(
    stop=stop_after_attempt(STRUCTURED_OUTPUT_ATTEMPTS),
    wait=wait_none(),
    retry=retry_if_exception_type(CandidateError),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _run_tool_agent_for_tasks(
    *,
    instruction: str,
    tada_dir: Path,
    model: str,
    api_key: str | None,
    subagent_model: str | None,
    subagent_api_key: str | None,
) -> list[MomentCandidate]:
    with _BUILD_AGENT_LOCK:
        agent, _ = build_agent(
            model,
            str(tada_dir),
            api_key=api_key,
            subagent_model=subagent_model,
            subagent_api_key=subagent_api_key,
        )
    agent.max_rounds = AGENT_IDEATION_MAX_ROUNDS
    result = agent.run(
        [{"role": "user", "content": instruction}],
        final_response_model=DiscoveryPayload,
        final_instruction=(
            "Convert your discovery work into the required structured discovery payload. "
            "Return only tasks that are explicitly supported by the activity context or tool results. "
            "If there are no grounded tasks, return an empty tasks list."
        ),
        final_metadata_app="moments_discovery",
    ).strip()
    if not result or result == "(max rounds reached)":
        raise CandidateError("discovery agent did not produce usable tasks")
    try:
        try:
            payload = extract_json_object(result)
            parsed = DiscoveryPayload.model_validate(payload)
        except StructuredOpsError:
            parsed = DiscoveryPayload.model_validate_json(result)
    except (StructuredOpsError, ValidationError) as exc:
        raise CandidateError(f"discovery agent returned invalid task JSON: {exc}") from exc
    return _parse_structured_discovery(parsed)


def _build_instruction(
    *,
    now: str,
    mode: str,
    last_run: datetime | None,
    activity_since: datetime,
    logs_dir: str,
    tada_dir: Path,
    accepted_moments: str,
    feedback_state_summary: str,
    activity_preview_metadata: str,
    activity_preview: str,
) -> str:
    return DISCOVER_TEMPLATE.format(
        now=now,
        mode=mode,
        last_run_date=last_run.strftime("%Y-%m-%d %H:%M") if last_run else "never",
        activity_since_date=activity_since.strftime("%Y-%m-%d %H:%M"),
        logs_dir=logs_dir,
        tada_dir=str(tada_dir),
        accepted_moments=accepted_moments,
        feedback_state_summary=feedback_state_summary,
        activity_preview_metadata=activity_preview_metadata,
        activity_preview=activity_preview,
    )


def _run_discovery_via_cli(
    *,
    instruction: str,
    tada_dir: Path,
    logs_dir: str,
    cli_config: CliBackendConfig,
) -> list[MomentCandidate]:
    """CLI variant — codex/claude writes the DiscoveryPayload JSON to a known
    path; we read and parse it.
    """
    out_dir = discovery_state_dir(tada_dir) / "cli"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "discovery.json"
    if out_path.exists():
        out_path.unlink()
    logger.info("discover[cli] output=%s", out_path)

    prompt = strip_tool_plumbing(instruction) + cli_footer(
        stage="discover",
        cwd=tada_dir,
        output_paths=[out_path],
        output_model=DiscoveryPayload,
    )
    run_stage_via_cli(
        stage="discover",
        config=cli_config,
        prompt=prompt,
        cwd=tada_dir,
        log_dir=out_dir,
        label="discover",
        # Claude needs explicit access to the logs dir for source inspection;
        # codex's workspace-write sandbox already allows reads outside cwd.
        add_dirs=[Path(logs_dir).resolve()],
        expected_outputs=[out_path],
    )
    try:
        parsed = DiscoveryPayload.model_validate_json(out_path.read_text())
    except (ValidationError, json.JSONDecodeError) as exc:
        raise CandidateError(f"CLI discovery wrote invalid JSON to {out_path}: {exc}") from exc
    tasks = _parse_structured_discovery(parsed)
    logger.info("discover[cli] produced %d task(s)", len(tasks))
    return tasks


def run(
    logs_dir: str,
    model: str,
    api_key: str | None = None,
    subagent_model: str | None = None,
    subagent_api_key: str | None = None,
    write_run_checkpoint: bool = True,
    cli_config: CliBackendConfig | None = None,
) -> str:
    logs_path = Path(logs_dir).resolve()
    logs_dir = str(logs_path)
    tada_dir = logs_path.parent / "logs-tada"
    state_dir = discovery_state_dir(tada_dir)
    checkpoint_path = tada_dir / ".last_run"
    tada_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    # The in-process agent's sandbox is only needed for the Gemini path; the
    # CLI backends scope writes via their own --sandbox / --permission-mode.
    if cli_config is None:
        _ensure_sandbox([str(tada_dir.resolve())])

    # Fresh slate every run — leftover CLI JSON from a previous run would
    # otherwise mask a mid-run failure.
    cli_dir = state_dir / "cli"
    if cli_dir.exists():
        shutil.rmtree(cli_dir)

    last_run = read_checkpoint(checkpoint_path, default_age=DEFAULT_MISSING_CHECKPOINT_AGE)
    mode = "first_run" if last_run is None else "incremental"
    activity_since = last_run if last_run is not None else _initial_discovery_since(logs_path)
    # Hard ceiling for the inline preview. The discovery agent may still inspect
    # older source files with tools when a candidate needs history.
    earliest_allowed = datetime.now() - MAX_ACTIVITY_WINDOW
    if activity_since < earliest_allowed:
        logger.info("discover[run] activity_since=%s clamped to %s (MAX_ACTIVITY_WINDOW=%s)",
                    activity_since, earliest_allowed, MAX_ACTIVITY_WINDOW)
        activity_since = earliest_allowed
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    backend = "cli" if cli_config is not None else "gemini"
    logger.info("discover[run] mode=%s backend=%s logs_dir=%s tada_dir=%s activity_since=%s",
                mode, backend, logs_dir, tada_dir, activity_since)

    accepted_moments = summarize_tada_tasks(tada_dir)
    feedback_summary = _feedback_state_summary(tada_dir)
    activity_preview_metadata, activity_preview = _activity_preview(logs_path, activity_since)
    if activity_preview_metadata.startswith("Rows after activity window: 0"):
        mode = "no_new_data"
    instruction = _build_instruction(
        now=now,
        mode=mode,
        last_run=last_run,
        activity_since=activity_since,
        logs_dir=logs_dir,
        tada_dir=tada_dir,
        accepted_moments=accepted_moments,
        feedback_state_summary=feedback_summary,
        activity_preview_metadata=activity_preview_metadata,
        activity_preview=activity_preview,
    )

    if cli_config is None:
        candidates = _run_tool_agent_for_tasks(
            instruction=instruction,
            tada_dir=tada_dir,
            model=model,
            api_key=api_key,
            subagent_model=subagent_model,
            subagent_api_key=subagent_api_key,
        )
    else:
        candidates = _run_discovery_via_cli(
            instruction=instruction,
            tada_dir=tada_dir,
            logs_dir=logs_dir,
            cli_config=cli_config,
        )

    candidate_path = write_candidates_jsonl(tada_dir, candidates)
    if write_run_checkpoint:
        write_checkpoint(checkpoint_path)
    logger.info("discover[run] wrote %d candidates to %s", len(candidates), candidate_path)
    return "\n".join([
        f"Mode: {mode}",
        f"Activity window starts after: {activity_since.strftime('%Y-%m-%d %H:%M')}",
        "Processed discovery in one pass.",
        f"Wrote {len(candidates)} candidates to {candidate_path}",
    ])

"""Analyze user activity logs and write candidate moments as JSONL.

The activity stream is sliced into ~64k-token chunks. Each chunk is sent to
its own discovery agent (in parallel via a small ThreadPool) — that's what
keeps any one prompt under the model's effective context cap. Within a chunk
the agent is still pushed to explore beyond it (read further back in the
same source files, check memory + accepted moments, look at sibling
streams) so a candidate can be grounded in context the chunk itself does
not contain.

Each chunk run produces a `DiscoveryPayload` (`tasks: [...]`). The worker
flattens tasks across all chunks, dedupes by id/slug, and writes
`candidates.jsonl`. Reconcile and the multi-file output shape are gone.
"""

from __future__ import annotations

import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from dotenv import load_dotenv
from pydantic import ValidationError
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_none

load_dotenv()

from apps.common.activity_streams import (
    ActivityChunk,
    ActivityRow,
    RenderedActivityRow,
    chunk_activity_rows,
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
ESTIMATED_CHARS_PER_TOKEN = 4
CHUNK_TARGET_TOKENS = 64_000
CHUNK_OVERLAP_TOKENS = 8_000
CHUNK_TARGET_CHARS = CHUNK_TARGET_TOKENS * ESTIMATED_CHARS_PER_TOKEN
CHUNK_OVERLAP_CHARS = CHUNK_OVERLAP_TOKENS * ESTIMATED_CHARS_PER_TOKEN
DISCOVERY_CHUNK_CONCURRENCY = 4
INITIAL_DISCOVERY_LOOKBACK = timedelta(days=2)
# Hard ceiling on the activity window — never look back more than this even
# if the last_run checkpoint is missing or stale.
MAX_ACTIVITY_WINDOW = timedelta(days=2)
STRUCTURED_OUTPUT_ATTEMPTS = 2
AGENT_IDEATION_MAX_ROUNDS = 60
logger = logging.getLogger(__name__)
_BUILD_AGENT_LOCK = Lock()

FilteredRow = ActivityRow
RenderedRow = RenderedActivityRow


@dataclass(frozen=True)
class ChunkDiscoveryResult:
    chunk_index: int
    tasks: list[MomentCandidate]


def _merged_filtered_rows(logs_path: Path, since: datetime | None):
    return merge_filtered_streams(logs_path, since, FILTERED_STREAM_SOURCES)


def _chunk_filtered_rows(
    rows: Iterator[FilteredRow],
    target_chars: int | None = None,
    overlap_chars: int | None = None,
) -> Iterator[ActivityChunk]:
    target_chars = CHUNK_TARGET_CHARS if target_chars is None else target_chars
    overlap_chars = CHUNK_OVERLAP_CHARS if overlap_chars is None else overlap_chars
    yield from chunk_activity_rows(rows, target_chars=target_chars, overlap_chars=overlap_chars)


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
    logs_dir: str,
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
    chunk: ActivityChunk,
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
        chunk_metadata=chunk.metadata,
        activity_chunk=chunk.rendered_text,
    )


def _run_discovery_chunk_via_cli(
    *,
    instruction: str,
    chunk_index: int,
    tada_dir: Path,
    logs_dir: str,
    cli_config: CliBackendConfig,
) -> list[MomentCandidate]:
    """CLI variant — codex/claude writes the DiscoveryPayload JSON to a known
    path; we read and parse it.
    """
    out_dir = discovery_state_dir(tada_dir) / "cli"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"discovery_chunk_{chunk_index}.json"
    if out_path.exists():
        out_path.unlink()
    logger.info("discover[cli] chunk=%d output=%s", chunk_index, out_path)

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
        label=f"discover_chunk_{chunk_index}",
        # Claude needs explicit access to the logs dir for `read further back`;
        # codex's workspace-write sandbox already allows reads outside cwd.
        add_dirs=[Path(logs_dir).resolve()],
        expected_outputs=[out_path],
    )
    try:
        parsed = DiscoveryPayload.model_validate_json(out_path.read_text())
    except (ValidationError, json.JSONDecodeError) as exc:
        raise CandidateError(f"CLI discovery wrote invalid JSON to {out_path}: {exc}") from exc
    tasks = _parse_structured_discovery(parsed)
    logger.info("discover[cli] chunk=%d produced %d task(s)", chunk_index, len(tasks))
    return tasks


def _process_discovery_chunk(
    *,
    chunk: ActivityChunk,
    now: str,
    mode: str,
    last_run: datetime | None,
    activity_since: datetime,
    logs_dir: str,
    tada_dir: Path,
    accepted_moments: str,
    feedback_state_summary: str,
    model: str,
    api_key: str | None,
    subagent_model: str | None,
    subagent_api_key: str | None,
    cli_config: CliBackendConfig | None = None,
) -> ChunkDiscoveryResult:
    instruction = _build_instruction(
        now=now,
        mode=mode,
        last_run=last_run,
        activity_since=activity_since,
        logs_dir=logs_dir,
        tada_dir=tada_dir,
        accepted_moments=accepted_moments,
        feedback_state_summary=feedback_state_summary,
        chunk=chunk,
    )
    if cli_config is None:
        tasks = _run_tool_agent_for_tasks(
            instruction=instruction,
            logs_dir=logs_dir,
            tada_dir=tada_dir,
            model=model,
            api_key=api_key,
            subagent_model=subagent_model,
            subagent_api_key=subagent_api_key,
        )
    else:
        tasks = _run_discovery_chunk_via_cli(
            instruction=instruction,
            chunk_index=chunk.index,
            tada_dir=tada_dir,
            logs_dir=logs_dir,
            cli_config=cli_config,
        )
    return ChunkDiscoveryResult(chunk_index=chunk.index, tasks=tasks)


def _process_discovery_chunks(
    *,
    chunks: list[ActivityChunk],
    now: str,
    mode: str,
    last_run: datetime | None,
    activity_since: datetime,
    logs_dir: str,
    tada_dir: Path,
    accepted_moments: str,
    feedback_state_summary: str,
    model: str,
    api_key: str | None,
    subagent_model: str | None,
    subagent_api_key: str | None,
    cli_config: CliBackendConfig | None = None,
) -> list[ChunkDiscoveryResult]:
    if not chunks:
        return []
    max_workers = max(1, min(DISCOVERY_CHUNK_CONCURRENCY, len(chunks)))
    logger.info("discover[run] processing %d chunk(s) with concurrency=%d", len(chunks), max_workers)
    if max_workers == 1:
        return [
            _process_discovery_chunk(
                chunk=chunk,
                now=now,
                mode=mode,
                last_run=last_run,
                activity_since=activity_since,
                logs_dir=logs_dir,
                tada_dir=tada_dir,
                accepted_moments=accepted_moments,
                feedback_state_summary=feedback_state_summary,
                model=model,
                api_key=api_key,
                subagent_model=subagent_model,
                subagent_api_key=subagent_api_key,
                cli_config=cli_config,
            )
            for chunk in chunks
        ]

    results: list[ChunkDiscoveryResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _process_discovery_chunk,
                chunk=chunk,
                now=now,
                mode=mode,
                last_run=last_run,
                activity_since=activity_since,
                logs_dir=logs_dir,
                tada_dir=tada_dir,
                accepted_moments=accepted_moments,
                feedback_state_summary=feedback_state_summary,
                model=model,
                api_key=api_key,
                subagent_model=subagent_model,
                subagent_api_key=subagent_api_key,
                cli_config=cli_config,
            )
            for chunk in chunks
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda result: result.chunk_index)


def _dedupe_across_chunks(results: list[ChunkDiscoveryResult]) -> list[MomentCandidate]:
    """Flatten tasks from every chunk, drop later duplicates by id or slug.

    The chunks overlap by design (CHUNK_OVERLAP_CHARS) so the same activity
    can produce the same candidate in two neighbouring chunks. We keep the
    first occurrence (earlier chunk index, sorted) and drop later twins.
    """
    out: list[MomentCandidate] = []
    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    dropped = 0
    for result in results:
        for candidate in result.tasks:
            if candidate.id in seen_ids or candidate.slug in seen_slugs:
                dropped += 1
                continue
            seen_ids.add(candidate.id)
            seen_slugs.add(candidate.slug)
            out.append(candidate)
    if dropped:
        logger.info("discover[run] deduped %d cross-chunk twin(s)", dropped)
    return out


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

    # Fresh slate every run — leftover chunk JSONs from a previous run would
    # otherwise mask a mid-run failure (we'd happily read the stale output).
    cli_dir = state_dir / "cli"
    if cli_dir.exists():
        shutil.rmtree(cli_dir)

    last_run = read_checkpoint(checkpoint_path, default_age=DEFAULT_MISSING_CHECKPOINT_AGE)
    mode = "first_run" if last_run is None else "incremental"
    activity_since = last_run if last_run is not None else _initial_discovery_since(logs_path)
    # Hard ceiling: never look back more than MAX_ACTIVITY_WINDOW. Protects
    # against a stale or wiped checkpoint dumping weeks of activity into the
    # chunking layer.
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

    rows = _merged_filtered_rows(logs_path, activity_since)
    chunks = list(_chunk_filtered_rows(rows))
    chunks_processed = len(chunks)
    logger.info("discover[run] sliced activity into %d chunk(s)", chunks_processed)

    if chunks_processed == 0:
        mode = "no_new_data"
        candidates: list[MomentCandidate] = []
    else:
        chunk_results = _process_discovery_chunks(
            chunks=chunks,
            now=now,
            mode=mode,
            last_run=last_run,
            activity_since=activity_since,
            logs_dir=logs_dir,
            tada_dir=tada_dir,
            accepted_moments=accepted_moments,
            feedback_state_summary=feedback_summary,
            model=model,
            api_key=api_key,
            subagent_model=subagent_model,
            subagent_api_key=subagent_api_key,
            cli_config=cli_config,
        )
        candidates = _dedupe_across_chunks(chunk_results)

    candidate_path = write_candidates_jsonl(tada_dir, candidates)
    if write_run_checkpoint:
        write_checkpoint(checkpoint_path)
    logger.info("discover[run] wrote %d candidates to %s", len(candidates), candidate_path)
    return "\n".join([
        f"Mode: {mode}",
        f"Activity window starts after: {activity_since.strftime('%Y-%m-%d %H:%M')}",
        f"Processed {chunks_processed} discovery chunks.",
        f"Wrote {len(candidates)} candidates to {candidate_path}",
    ])

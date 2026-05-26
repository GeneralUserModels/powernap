"""Promote discovered candidate moments into accepted markdown moments."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_none

load_dotenv()

from apps.common.structured_completion import structured_completion
from apps.common.structured_ops import StructuredOpsError
from agent.cli_backends import (
    CliBackendConfig,
    cli_footer,
    run_stage_via_cli,
    strip_tool_plumbing,
)
from apps.moments.core.incremental import write_checkpoint
from apps.moments.core.candidates import (
    CandidateError,
    MomentCandidate,
    discovery_state_dir,
    latest_candidate_file,
    parse_promotion_result,
    read_candidate_jsonl,
    write_accepted_moment,
)
from apps.moments.core.paths import find_task_md, get_topic, summarize_tada_tasks
from apps.moments.schemas.structured import PromotionPayload

_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
PROMOTE_TEMPLATE = (_PROMPTS / "promote.txt").read_text()
STRUCTURED_OUTPUT_ATTEMPTS = 2
logger = logging.getLogger(__name__)


def _feedback_state_summary(tada_dir: Path) -> str:
    state_path = tada_dir / "results" / "_moment_state.json"
    feedback = sorted((tada_dir / "results").glob("*/feedback_*.md")) if (tada_dir / "results").exists() else []
    parts: list[str] = []
    if state_path.exists():
        parts.append("State file exists at `results/_moment_state.json`; inspect it for negative signals and dismissed moments.")
    if feedback:
        parts.append("Recent feedback files:\n" + "\n".join(f"- {p.relative_to(tada_dir)}" for p in feedback[-20:]))
    return "\n\n".join(parts) or "- (none)"


def _route_existing_slug_updates(tada_dir: Path, candidates: list[MomentCandidate]) -> tuple[list[MomentCandidate], int]:
    routed: list[MomentCandidate] = []
    routed_count = 0
    for candidate in candidates:
        accepted_path = find_task_md(tada_dir, candidate.slug)
        if accepted_path is None:
            routed.append(candidate)
            continue
        accepted_topic = get_topic(accepted_path, tada_dir)
        if candidate.topic == accepted_topic:
            routed.append(candidate)
            continue
        routed.append(replace(candidate, topic=accepted_topic))
        routed_count += 1
    return routed, routed_count


@retry(
    stop=stop_after_attempt(STRUCTURED_OUTPUT_ATTEMPTS),
    wait=wait_none(),
    retry=retry_if_exception_type(CandidateError),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _run_promotion_agent_for_valid_json(
    *,
    instruction: str,
    candidates: list[MomentCandidate],
    model: str,
    api_key: str | None,
):
    try:
        result, payload = structured_completion(
            model=model,
            instruction=instruction,
            response_model=PromotionPayload,
            api_key=api_key,
            metadata_app="moments_promote",
        )
    except StructuredOpsError as exc:
        raise CandidateError(str(exc)) from exc
    return result, parse_promotion_result(
        "```json\n" + json.dumps(payload.model_dump(exclude_none=True)) + "\n```",
        candidates,
    )


@retry(
    stop=stop_after_attempt(STRUCTURED_OUTPUT_ATTEMPTS),
    wait=wait_none(),
    retry=retry_if_exception_type(CandidateError),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _run_promotion_via_cli(
    *,
    instruction: str,
    candidates: list[MomentCandidate],
    tada_dir: Path,
    cli_config: CliBackendConfig,
    candidate_path: Path,
):
    """CLI variant — the agent writes a PromotionPayload JSON file we parse."""
    out_dir = discovery_state_dir(tada_dir) / "cli"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"promote_{candidate_path.stem}.json"
    if out_path.exists():
        out_path.unlink()

    prompt = strip_tool_plumbing(instruction) + cli_footer(
        stage="promote",
        cwd=tada_dir,
        output_paths=[out_path],
        output_model=PromotionPayload,
    )
    run_stage_via_cli(
        stage="promote",
        config=cli_config,
        prompt=prompt,
        cwd=tada_dir,
        log_dir=out_dir,
        label=f"promote_{candidate_path.stem}",
        expected_outputs=[out_path],
    )
    raw = out_path.read_text()
    try:
        payload = PromotionPayload.model_validate_json(raw)
    except Exception as exc:
        raise CandidateError(f"CLI promote wrote invalid JSON to {out_path}: {exc}") from exc

    result_text = f"(CLI promote wrote {out_path})"
    return result_text, parse_promotion_result(
        "```json\n" + json.dumps(payload.model_dump(exclude_none=True)) + "\n```",
        candidates,
    )


def run(
    logs_dir: str,
    model: str,
    n: int = 8,
    api_key: str | None = None,
    subagent_model: str | None = None,
    subagent_api_key: str | None = None,
    write_run_checkpoint: bool = True,
    cli_config: CliBackendConfig | None = None,
) -> str:
    logs_path = Path(logs_dir).resolve()
    tada_path = logs_path.parent / "logs-tada"
    state_dir = discovery_state_dir(tada_path)
    tada_path.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = latest_candidate_file(tada_path)
    if candidate_path is None:
        return "no candidate files to promote"
    candidates = read_candidate_jsonl(candidate_path)
    candidates, routed_updates = _route_existing_slug_updates(tada_path, candidates)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # For same-slug candidates, inline the existing accepted moment's markdown
    # body so the promoter can judge whether the update is materially better.
    enriched_candidates = []
    for c in candidates:
        entry = c.to_json()
        accepted_path = find_task_md(tada_path, c.slug)
        if accepted_path is not None:
            try:
                entry["previous_version"] = accepted_path.read_text()
            except OSError:
                pass
        enriched_candidates.append(entry)
    candidate_json = json.dumps(enriched_candidates, indent=2)
    instruction = PROMOTE_TEMPLATE.format(
        now=now,
        tada_dir=str(tada_path),
        accepted_moments=summarize_tada_tasks(tada_path),
        feedback_state_summary=_feedback_state_summary(tada_path),
        candidate_json=candidate_json,
    )

    if cli_config is None:
        result, (ranked, _rejected) = _run_promotion_agent_for_valid_json(
            instruction=instruction,
            candidates=candidates,
            model=model,
            api_key=api_key,
        )
    else:
        result, (ranked, _rejected) = _run_promotion_via_cli(
            instruction=instruction,
            candidates=candidates,
            tada_dir=tada_path,
            cli_config=cli_config,
            candidate_path=candidate_path,
        )
    promoted = ranked[:n] if n > 0 else ranked
    for candidate in promoted:
        write_accepted_moment(tada_path, candidate)
    if write_run_checkpoint:
        write_checkpoint(tada_path / ".last_run")

    summary = f"{result}\n\nRanked {len(ranked)} of {len(candidates)} candidates. Promoted top {len(promoted)} from {candidate_path}"
    if routed_updates:
        summary += f"\nRouted {routed_updates} same-slug candidates to existing accepted moment paths."
    return summary

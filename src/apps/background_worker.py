"""One-shot subprocess worker for heavy Tada/Memex jobs."""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.cost_tracker import init_cost_tracking
from server.process_jobs import WORKER_EVENT_PREFIX

logger = logging.getLogger(__name__)


def _emit(event: dict[str, Any]) -> None:
    print(WORKER_EVENT_PREFIX + json.dumps(event), flush=True)


def _activity(
    agent: str,
    message: str | None,
    *,
    slug: str | None = None,
    cadence: str | None = None,
    num_turns: int | None = None,
    max_turns: int | None = None,
) -> None:
    event: dict[str, Any] = {
        "type": "activity" if num_turns is None and max_turns is None else "round",
        "agent": agent,
        "message": message,
    }
    if slug is not None:
        event["slug"] = slug
    if cadence is not None:
        event["cadence"] = cadence
    if num_turns is not None:
        event["num_turns"] = num_turns
    if max_turns is not None:
        event["max_turns"] = max_turns
    _emit(event)


def _make_round_callback(
    agent: str,
    message: str,
    *,
    slug: str | None = None,
    cadence: str | None = None,
) -> Callable[[int, int], None]:
    def on_round(num_turns: int, max_turns: int) -> None:
        _activity(
            agent,
            message,
            slug=slug,
            cadence=cadence,
            num_turns=num_turns,
            max_turns=max_turns,
        )

    return on_round


def _apply_low_priority() -> None:
    if not hasattr(os, "nice"):
        return
    raw = os.getenv("TADA_BACKGROUND_WORKER_NICE", "10")
    try:
        increment = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid TADA_BACKGROUND_WORKER_NICE=%r", raw)
        return
    if increment <= 0:
        return
    try:
        os.nice(increment)
    except OSError:
        logger.warning("Could not lower background worker priority", exc_info=True)


def _moments_execute(payload: dict[str, Any]) -> dict[str, Any]:
    from apps.moments.runtime.execute import run as execute_moment

    activity = payload.get("activity") or {}
    agent = str(activity.get("agent") or "moments")
    message = str(activity.get("message") or "Running Tada")
    slug = activity.get("slug") if isinstance(activity.get("slug"), str) else None
    cadence = activity.get("cadence") if isinstance(activity.get("cadence"), str) else None

    _activity(agent, message, slug=slug, cadence=cadence)
    on_round = _make_round_callback(agent, message, slug=slug, cadence=cadence)
    try:
        success = execute_moment(
            payload["task_path"],
            payload["output_dir"],
            payload["logs_dir"],
            payload["model"],
            cadence_override=payload.get("cadence_override"),
            schedule_override=payload.get("schedule_override"),
            api_key=payload.get("api_key"),
            last_run_at=payload.get("last_run_at"),
            on_round=on_round,
            subagent_model=payload.get("subagent_model"),
            subagent_api_key=payload.get("subagent_api_key"),
        )
        return {"success": bool(success)}
    finally:
        _activity(agent, None, slug=slug, cadence=cadence)


def _moments_discovery(payload: dict[str, Any]) -> dict[str, Any]:
    from apps.moments.core.incremental import write_checkpoint
    from apps.moments.runtime.discovery import MomentsDiscovery, TaskFilter, TriggersCheck

    logs_dir = payload["logs_dir"]
    model = payload["model"]
    api_key = payload.get("api_key")
    subagent_model = payload.get("subagent_model")
    subagent_api_key = payload.get("subagent_api_key")
    summaries: dict[str, str] = {}

    agent = "moments_discovery"
    try:
        _activity(agent, "Discovering Tadas...")
        summaries["discovery"] = MomentsDiscovery(
            logs_dir, model, api_key, subagent_model, subagent_api_key,
        ).run(write_run_checkpoint=False)

        _activity(agent, "Promoting Tadas...")
        summaries["promotion"] = TaskFilter(
            logs_dir, model, api_key, subagent_model, subagent_api_key,
        ).run(write_run_checkpoint=False)

        _activity(agent, "Checking Triggers...")
        summaries["triggers"] = TriggersCheck(
            logs_dir, model, api_key, subagent_model, subagent_api_key,
        ).run()

        run_checkpoint = payload.get("run_checkpoint")
        if run_checkpoint:
            write_checkpoint(Path(run_checkpoint))
        return {"success": True, "summaries": summaries}
    finally:
        _activity(agent, None)


def _memory_pipeline(payload: dict[str, Any]) -> dict[str, Any]:
    from apps.memory.service import MemoryIngest, MemoryLint

    logs_dir = payload["logs_dir"]
    model = payload["model"]
    api_key = payload.get("api_key")
    subagent_model = payload.get("subagent_model")
    subagent_api_key = payload.get("subagent_api_key")
    summaries: dict[str, str] = {}

    agent = "memory"
    try:
        ingest_msg = "Ingesting memories..."
        _activity(agent, ingest_msg)
        summaries["ingest"] = MemoryIngest(
            logs_dir, model, api_key, subagent_model, subagent_api_key,
        ).run(on_round=_make_round_callback(agent, ingest_msg))

        lint_msg = "Auditing memories..."
        _activity(agent, lint_msg)
        summaries["lint"] = MemoryLint(
            logs_dir, model, api_key, subagent_model, subagent_api_key,
        ).run(on_round=_make_round_callback(agent, lint_msg))

        return {"success": True, "summaries": summaries}
    finally:
        _activity(agent, None)


_JOBS = {
    "moments.execute": _moments_execute,
    "moments.discovery": _moments_discovery,
    "memory.pipeline": _memory_pipeline,
}


def _log_costs(tracker, job_name: str) -> None:
    snapshot, elapsed = tracker.snapshot()
    total_cost = sum(s["cost"] for s in snapshot.values())
    total_tokens = sum(s["input_tokens"] + s["output_tokens"] for s in snapshot.values())
    logger.info(
        "[cost] %s finished - $%.4f total, %d tokens, %.0fs",
        job_name, total_cost, total_tokens, elapsed,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    if len(sys.argv) != 2 or sys.argv[1] not in _JOBS:
        print("usage: python -m apps.background_worker <job>", file=sys.stderr)
        return 2

    try:
        from connectors._parent_watchdog import start_parent_watchdog

        start_parent_watchdog()
    except Exception:
        logger.warning("Parent watchdog unavailable", exc_info=True)

    _apply_low_priority()
    job_name = sys.argv[1]
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print("invalid JSON payload", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("payload must be a JSON object", file=sys.stderr)
        return 2

    tracker = init_cost_tracking()
    try:
        result = _JOBS[job_name](payload)
        _emit({"type": "result", "result": result})
        _log_costs(tracker, job_name)
        return 0
    except Exception as exc:
        _emit({"type": "error", "message": str(exc)})
        traceback.print_exc()
        _log_costs(tracker, job_name)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

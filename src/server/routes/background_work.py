"""Status and manual starts for scheduled background feature work."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from apps.memory.service import run_memory_pipeline_once, successful_run_marker as memory_success_marker
from apps.moments.runtime.discovery import (
    run_moments_discovery_once,
    successful_run_marker as tada_success_marker,
)
from apps.moments.runtime.scheduler import next_scheduled_service_run, scan_due_moments_once
from server.feature_flags import is_enabled

router = APIRouter(prefix="/api/background-work", tags=["background-work"])
logger = logging.getLogger(__name__)

_MEMORY_SPECIAL_FILES = {"index.md", "log.md", "schema.md"}


def _iso_from_mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _read_datetime(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        value = datetime.fromisoformat(path.read_text().strip())
    except (OSError, ValueError):
        return None
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def _iso_from_checkpoint(path: Path) -> str | None:
    value = _read_datetime(path)
    return value.isoformat() if value else None


def _memory_has_completed_run(memory_dir: Path) -> bool:
    marker = memory_success_marker(memory_dir.parent)
    if marker.exists():
        return True
    if not memory_dir.exists():
        return False
    for md_file in memory_dir.rglob("*.md"):
        rel = md_file.relative_to(memory_dir)
        if str(rel) in _MEMORY_SPECIAL_FILES:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] == "_archive":
            continue
        return True
    return False


def _tada_has_completed_run(tada_dir: Path) -> bool:
    marker = tada_success_marker(tada_dir)
    if marker.exists():
        return True
    if any((tada_dir / "results").glob("*/meta.json")):
        return True
    if any((tada_dir / "_discovery" / "candidates").glob("*.jsonl")):
        return True
    return False


def _status_entry(
    *,
    enabled: bool,
    running: bool,
    schedule: str,
    checkpoint: Path,
    marker: Path,
    has_completed_run: bool,
) -> dict:
    next_run = next_scheduled_service_run(schedule, checkpoint)
    last_completed = _iso_from_mtime(marker)
    if last_completed is None and has_completed_run:
        last_completed = _iso_from_checkpoint(checkpoint)
    blocked_reason = None
    if not has_completed_run:
        blocked_reason = "first_run"
    elif running:
        blocked_reason = "running"
    elif not enabled:
        blocked_reason = "disabled"
    return {
        "enabled": enabled,
        "running": running,
        "schedule": schedule,
        "next_run_at": next_run.isoformat() if next_run else None,
        "last_completed_at": last_completed,
        "manual_start_allowed": enabled and has_completed_run and not running,
        "manual_start_blocked_reason": blocked_reason,
    }


def _memory_status(state) -> dict:
    logs_dir = Path(state.config.log_dir).resolve()
    memory_dir = logs_dir / "memory"
    enabled = is_enabled(state.config, "memory") and state.config.memory_enabled
    running = "memory" in state.active_agents or "memory" in state.background_work_in_flight
    return _status_entry(
        enabled=enabled,
        running=running,
        schedule=getattr(state.config, "memory_schedule", "daily at 3am"),
        checkpoint=memory_dir / ".last_run",
        marker=memory_success_marker(logs_dir),
        has_completed_run=_memory_has_completed_run(memory_dir),
    )


def _tada_status(state) -> dict:
    tada_dir = Path(state.config.tada_dir).resolve()
    enabled = is_enabled(state.config, "moments") and state.config.moments_enabled
    running = (
        "moments_discovery" in state.active_agents
        or any(key.startswith("moment_run:") for key in state.active_agents)
        or "tada" in state.background_work_in_flight
    )
    return _status_entry(
        enabled=enabled,
        running=running,
        schedule=getattr(state.config, "moments_discovery_schedule", "daily at 2am"),
        checkpoint=tada_dir / ".last_run",
        marker=tada_success_marker(tada_dir),
        has_completed_run=_tada_has_completed_run(tada_dir),
    )


def _track_task(state, feature: str, task: asyncio.Task) -> None:
    state.background_work_in_flight.add(feature)
    state.background_work_tasks.add(task)

    def _cleanup(t: asyncio.Task) -> None:
        state.background_work_in_flight.discard(feature)
        state.background_work_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.exception("Manual background work task failed", exc_info=exc)

    task.add_done_callback(_cleanup)


@router.get("/status")
async def get_status(request: Request):
    state = request.app.state.server
    return {
        "memory": _memory_status(state),
        "tada": _tada_status(state),
    }


@router.post("/memory/start")
async def start_memory(request: Request):
    state = request.app.state.server
    status = _memory_status(state)
    if status["running"]:
        return JSONResponse({"error": "Memory is already running"}, status_code=409)
    if not status["enabled"]:
        return JSONResponse({"error": "Memory is disabled"}, status_code=403)
    if not status["manual_start_allowed"]:
        return JSONResponse({"error": "Memory has not completed its first scheduled run yet"}, status_code=403)

    task = asyncio.create_task(run_memory_pipeline_once(state))
    _track_task(state, "memory", task)
    return JSONResponse({"status": "started"}, status_code=202)


@router.post("/tada/start")
async def start_tada(request: Request):
    state = request.app.state.server
    status = _tada_status(state)
    if status["running"]:
        return JSONResponse({"error": "Tada is already running"}, status_code=409)
    if not status["enabled"]:
        return JSONResponse({"error": "Tada is disabled"}, status_code=403)
    if not status["manual_start_allowed"]:
        return JSONResponse({"error": "Tada has not completed its first scheduled run yet"}, status_code=403)

    async def _run_tada_now() -> None:
        success = await run_moments_discovery_once(state)
        if success:
            await scan_due_moments_once(state)

    task = asyncio.create_task(_run_tada_now())
    _track_task(state, "tada", task)
    return JSONResponse({"status": "started"}, status_code=202)

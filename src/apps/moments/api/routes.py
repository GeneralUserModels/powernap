"""REST endpoints for moments (Tada tab)."""

import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

import asyncio
import time as _time

from apps.moments.runtime.execute import _parse_frontmatter as parse_frontmatter
from apps.moments.core.paths import find_task_md, get_topic, list_task_files
from apps.moments.runtime.scheduler import save_run, load_run_history
from server.process_jobs import relay_worker_event
from apps.moments.core.state import (
    load_state,
    save_state,
    DEFAULT_SLUG_STATE,
)
from chat import ChatAgent, ChatSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/moments", tags=["moments"])
OUTPUT_SUBDIR = "output"


class MomentStateUpdate(BaseModel):
    dismissed: Optional[bool] = None
    pinned: Optional[bool] = None
    thumbs: Optional[str] = None


class ScheduleUpdate(BaseModel):
    cadence: str
    schedule: str


class ViewEnd(BaseModel):
    duration_ms: int


def _get_tada_dir(request: Request) -> Path:
    return Path(request.app.state.server.config.tada_dir).resolve()


def _output_pages_dir(result_dir: Path) -> Path:
    return result_dir / OUTPUT_SUBDIR


_SERVED_SUFFIXES = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json",
    ".png", ".jpg", ".jpeg", ".svg", ".webp",
}


def _list_output_pages(result_dir: Path) -> list[Path]:
    output_pages_dir = _output_pages_dir(result_dir)
    if not output_pages_dir.is_dir():
        return []
    base = output_pages_dir.resolve()
    pages: list[Path] = []
    for path in output_pages_dir.rglob("*.html"):
        if not path.is_file():
            continue
        rel = path.relative_to(output_pages_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            path.resolve().relative_to(base)
        except ValueError:
            continue
        pages.append(path)

    def sort_key(path: Path) -> tuple[int, str]:
        rel = path.relative_to(output_pages_dir).as_posix()
        priority = 0 if path.name.lower() == "index.html" else 1
        return (priority, rel.lower())

    return sorted(pages, key=sort_key)


def _page_meta(path: Path, output_pages_dir: Path) -> dict:
    stat = path.stat()
    return {
        "path": path.relative_to(output_pages_dir).as_posix(),
        "title": path.stem.replace("-", " ").title(),
        "bytes": stat.st_size,
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _resolve_output_page(tada_dir: Path, slug: str, page_path: str) -> Path | None:
    output_pages_dir = _output_pages_dir(tada_dir / "results" / slug)
    if not output_pages_dir.is_dir():
        return None
    base = output_pages_dir.resolve()
    target = (output_pages_dir / page_path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file() or target.suffix.lower() not in _SERVED_SUFFIXES:
        return None
    return target


def _list_results_data(tada_dir: Path, include_dismissed: bool) -> list[dict]:
    results_dir = tada_dir / "results"
    if not results_dir.exists():
        return []

    all_state = load_state(tada_dir)
    # Build slug -> topic/frontmatter maps once so each result row can carry
    # metadata without re-globbing per slug.
    task_files = list_task_files(tada_dir)
    slug_topics: dict[str, str] = {
        md.stem: get_topic(md, tada_dir) for md in task_files
    }
    slug_frontmatter: dict[str, dict] = {
        md.stem: parse_frontmatter(md.read_text()) for md in task_files
    }
    results = []
    for meta_path in results_dir.glob("*/meta.json"):
        result_dir = meta_path.parent
        page_paths = _list_output_pages(result_dir)
        if not page_paths:
            continue
        meta = json.loads(meta_path.read_text())
        slug = meta_path.parent.name
        slug_state = {**DEFAULT_SLUG_STATE, **all_state.get(slug, {})}

        if slug_state["dismissed"] and not include_dismissed:
            continue

        # Take the freshest mtime across generated output files, skipping
        # user-interaction artifacts like feedback_*.md that would inflate
        # "last updated".
        output_files = [
            f for f in result_dir.iterdir()
            if f.is_file() and not f.name.startswith("feedback_")
        ] + page_paths
        mtime = max(f.stat().st_mtime for f in output_files)
        completed_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

        # Feedback status
        feedback_files = list(meta_path.parent.glob("feedback_*.md"))
        has_feedback = len(feedback_files) > 0
        feedback_incorporated = False
        last_incorporated = slug_state.get("last_feedback_incorporated_at")
        if has_feedback and last_incorporated:
            latest_feedback_mtime = max(os.path.getmtime(f) for f in feedback_files)
            latest_feedback_dt = datetime.fromtimestamp(latest_feedback_mtime, tz=timezone.utc)
            incorporated_dt = datetime.fromisoformat(last_incorporated)
            feedback_incorporated = latest_feedback_dt < incorporated_dt

        results.append({
            "slug": slug,
            "title": meta.get("title", slug),
            "description": meta.get("description", ""),
            "completed_at": completed_at,
            "cadence": meta.get("cadence") or slug_frontmatter.get(slug, {}).get("cadence", ""),
            "schedule": meta.get("schedule") or slug_frontmatter.get(slug, {}).get("schedule", ""),
            "topic": slug_topics.get(slug, ""),
            "page_count": len(page_paths),
            "has_feedback": has_feedback,
            "feedback_incorporated": feedback_incorporated,
            **slug_state,
        })

    # Pinned first, then by completed_at descending.
    results.sort(key=lambda r: (not r["pinned"], r["completed_at"]), reverse=False)
    results.sort(key=lambda r: r["completed_at"], reverse=True)
    results.sort(key=lambda r: not r["pinned"])
    return results


def _list_result_pages_data(tada_dir: Path, slug: str) -> list[dict] | None:
    result_dir = tada_dir / "results" / slug
    pages = _list_output_pages(result_dir)
    if not pages:
        return None
    output_pages_dir = _output_pages_dir(result_dir)
    return [_page_meta(path, output_pages_dir) for path in pages]


@router.get("/tasks")
async def list_tasks(request: Request):
    """List all accepted moments from logs-tada/<topic>/*.md."""
    tada_dir = _get_tada_dir(request)
    if not tada_dir.exists():
        return []
    tasks = []
    for md_file in list_task_files(tada_dir):
        fm = parse_frontmatter(md_file.read_text())
        cadence = fm.get("cadence", "")
        if cadence not in ("once", "scheduled", "trigger"):
            continue
        tasks.append({
            "slug": md_file.stem,
            "title": fm.get("title", md_file.stem),
            "description": fm.get("description", ""),
            "cadence": cadence,
            "schedule": fm.get("schedule", ""),
            "trigger": fm.get("trigger", ""),
            "confidence": float(fm.get("confidence", 0)),
            "usefulness": int(fm.get("usefulness", 0)),
            "topic": get_topic(md_file, tada_dir),
        })
    return tasks


@router.get("/results")
async def list_results(request: Request, include_dismissed: bool = False):
    """List completed moment results, sorted by most recent first."""
    tada_dir = _get_tada_dir(request)
    return await asyncio.to_thread(_list_results_data, tada_dir, include_dismissed)


@router.get("/results/{slug}/pages")
async def list_result_pages(slug: str, request: Request):
    """List HTML pages for a completed moment result."""
    data = await asyncio.to_thread(_list_result_pages_data, _get_tada_dir(request), slug)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return data


@router.get("/results/{slug}/pages/{page_path:path}")
async def get_result_page(slug: str, page_path: str, request: Request):
    """Serve a mini-web-app asset (html, css, js, json, image) for a moment."""
    path = _resolve_output_page(_get_tada_dir(request), slug, page_path)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    media_type, _ = mimetypes.guess_type(path.name)
    if media_type is None:
        media_type = "application/octet-stream"
    return Response(path.read_bytes(), media_type=media_type)


@router.put("/{slug}/state")
async def update_moment_state(slug: str, body: MomentStateUpdate, request: Request):
    """Set dismissed and/or pinned for a moment."""
    tada_dir = _get_tada_dir(request)
    all_state = load_state(tada_dir)
    entry = {**DEFAULT_SLUG_STATE, **all_state.get(slug, {})}

    if body.pinned is not None:
        entry["pinned"] = body.pinned
        if body.pinned:
            entry["dismissed"] = False
    if body.dismissed is not None:
        entry["dismissed"] = body.dismissed
        if body.dismissed:
            entry["pinned"] = False
    if body.thumbs is not None:
        if body.thumbs not in ("up", "down", "clear"):
            return JSONResponse({"error": "thumbs must be 'up', 'down', or 'clear'"}, status_code=400)
        entry["thumbs"] = None if body.thumbs == "clear" else body.thumbs

    all_state[slug] = entry
    save_state(tada_dir, all_state)
    return entry


@router.put("/{slug}/schedule")
async def update_moment_schedule(slug: str, body: ScheduleUpdate, request: Request):
    """Update cadence/schedule overrides for a moment."""
    if body.cadence not in ("once", "scheduled", "trigger"):
        return JSONResponse({"error": "cadence must be once, scheduled, or trigger"}, status_code=400)

    tada_dir = _get_tada_dir(request)
    all_state = load_state(tada_dir)
    entry = {**DEFAULT_SLUG_STATE, **all_state.get(slug, {})}
    entry["cadence_override"] = body.cadence
    entry["schedule_override"] = body.schedule
    all_state[slug] = entry
    save_state(tada_dir, all_state)
    return entry


@router.post("/{slug}/view")
async def record_view(slug: str, request: Request):
    """Record a view event: increments view_count, sets last_viewed."""
    tada_dir = _get_tada_dir(request)
    all_state = load_state(tada_dir)
    entry = {**DEFAULT_SLUG_STATE, **all_state.get(slug, {})}
    entry["view_count"] = entry.get("view_count", 0) + 1
    entry["last_viewed"] = datetime.now(tz=timezone.utc).isoformat()
    all_state[slug] = entry
    save_state(tada_dir, all_state)
    return {"view_count": entry["view_count"]}


@router.post("/{slug}/view-end")
async def record_view_end(slug: str, body: ViewEnd, request: Request):
    """Record view duration."""
    tada_dir = _get_tada_dir(request)
    all_state = load_state(tada_dir)
    entry = {**DEFAULT_SLUG_STATE, **all_state.get(slug, {})}
    entry["time_spent_ms"] = entry.get("time_spent_ms", 0) + body.duration_ms
    all_state[slug] = entry
    save_state(tada_dir, all_state)
    return {"time_spent_ms": entry["time_spent_ms"]}


# ── Re-execution ─────────────────────────────────────────────

@router.post("/{slug}/rerun")
async def rerun_moment(slug: str, request: Request):
    """Trigger an immediate re-execution of a moment."""
    state = request.app.state.server
    tada_dir = _get_tada_dir(request)
    task_path = find_task_md(tada_dir, slug)

    if task_path is None:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    # Reject if this exact slug is already running/queued (via scheduler or
    # a prior rerun) — re-firing the same tada concurrently would race on the
    # shared output dir.
    if slug in state.moments_in_flight_slugs:
        return JSONResponse({"error": "This moment is already executing"}, status_code=409)

    # Reject if the executor pool is fully booked. Non-blocking: we don't
    # want the HTTP request to hang waiting for a slot. The user can retry.
    if state.moments_executor_sem.locked():
        return JSONResponse({"error": "All execution slots are busy"}, status_code=409)

    cfg = state.config
    model = cfg.moments_agent_model
    api_key = cfg.resolve_api_key("moments_agent_api_key")
    subagent_model = cfg.subagent_model or None
    subagent_api_key = cfg.resolve_api_key("subagent_api_key") if cfg.subagent_model else None
    logs_dir = str(Path(cfg.log_dir).resolve())
    results_dir = tada_dir / "results"
    output_dir = str(results_dir / slug)

    fm = parse_frontmatter(task_path.read_text())
    all_state = load_state(tada_dir)
    slug_state = all_state.get(slug, {})
    cadence_override = slug_state.get("cadence_override") or None
    sched_override = slug_state.get("schedule_override") or None
    run_history = load_run_history(results_dir)

    state.moments_in_flight_slugs.add(slug)

    async def _run_rerun():
        try:
            async with state.moments_executor_sem:
                await state.broadcast("moment_rerun_started", {"slug": slug})
                started_at = _time.time()
                logger.info(f"Re-executing moment: {slug}")

                # Signal handlers require main thread — pre-init before to_thread.
                from agent.builder import _ensure_sandbox_async
                await _ensure_sandbox_async([str(tada_dir.resolve())])

                moment_title = fm.get("title", slug)
                run_msg = f"Running: {moment_title}"
                effective_cadence = cadence_override or fm.get("cadence", "")
                activity_key = f"moment_run:{slug}"
                await state.broadcast_activity(
                    activity_key, run_msg, slug=slug, cadence=effective_cadence,
                )
                try:
                    result = await state.background_job_runner.run(
                        "moments.execute",
                        {
                            "task_path": str(task_path),
                            "output_dir": output_dir,
                            "logs_dir": logs_dir,
                            "model": model,
                            "cadence_override": cadence_override,
                            "schedule_override": sched_override,
                            "api_key": api_key,
                            "last_run_at": run_history.get(slug),
                            "subagent_model": subagent_model,
                            "subagent_api_key": subagent_api_key,
                            "activity": {
                                "agent": activity_key,
                                "message": run_msg,
                                "slug": slug,
                                "cadence": effective_cadence,
                            },
                        },
                        on_event=lambda event: relay_worker_event(state, event),
                    )
                    success = bool(result.get("success"))
                except Exception:
                    logger.exception("Moment rerun worker failed: %s", slug)
                    success = False
                finally:
                    await state.broadcast_activity(activity_key)
                completed_at = _time.time()
                async with state.moments_runs_lock:
                    save_run(results_dir, slug, started_at, completed_at, "success" if success else "failed")

                if success:
                    effective_schedule = sched_override or fm.get("schedule", "")
                    meta_path = Path(output_dir) / "meta.json"
                    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
                    result_dir = Path(output_dir)
                    output_files = [
                        f for f in result_dir.iterdir()
                        if f.is_file() and not f.name.startswith("feedback_")
                    ] if result_dir.exists() else []
                    true_updated = (
                        datetime.fromtimestamp(max(f.stat().st_mtime for f in output_files), tz=timezone.utc).isoformat()
                        if output_files else datetime.now().isoformat()
                    )
                    await state.broadcast("moment_completed", {
                        "slug": slug,
                        "title": meta.get("title", fm.get("title", slug)),
                        "description": meta.get("description", fm.get("description", "")),
                        "completed_at": true_updated,
                        "cadence": effective_cadence,
                        "schedule": effective_schedule,
                    })
                    logger.info(f"Moment re-executed: {slug}")
                else:
                    await state.broadcast("moment_rerun_failed", {"slug": slug})
                    logger.warning(f"Moment rerun failed: {slug}")
        finally:
            state.moments_in_flight_slugs.discard(slug)

    asyncio.create_task(_run_rerun())
    return JSONResponse({"status": "started"}, status_code=202)


# ── Feedback ──────────────────────────────────────────────────

FEEDBACK_SYSTEM_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "feedback.txt").read_text()


_FEEDBACK_FILE_LANGS = {
    ".html": "html", ".htm": "html",
    ".css": "css",
    ".js": "javascript", ".mjs": "javascript",
    ".json": "json",
}


def _read_moment_files(result_dir: Path) -> str:
    """Read all moment output files for the feedback system prompt."""
    parts = []
    meta_path = result_dir / "meta.json"
    if meta_path.exists():
        content = meta_path.read_text(errors="replace")
        parts.append(f"### meta.json\n```json\n{content}\n```")
    output_pages_dir = _output_pages_dir(result_dir)
    if output_pages_dir.is_dir():
        for path in sorted(output_pages_dir.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            lang = _FEEDBACK_FILE_LANGS.get(suffix)
            if lang is None:
                continue
            content = path.read_text(errors="replace")
            if len(content) > 10000:
                content = content[:10000] + "\n... (truncated)"
            rel = path.relative_to(output_pages_dir).as_posix()
            parts.append(f"### {output_pages_dir.name}/{rel}\n```{lang}\n{content}\n```")
    return "\n\n".join(parts)


def _resolve_feedback_api_key(config) -> str | None:
    return config.moments_agent_api_key or config.resolve_api_key("agent_api_key")


@dataclass
class _FeedbackEntry:
    """In-memory feedback session bound to a stable on-disk transcript path."""
    session: object  # ChatSession
    path: Path


def _persist_feedback(entry: _FeedbackEntry) -> None:
    entry.session.save(entry.path, assistant_label="Tada")
    logger.info(f"Feedback saved to {entry.path}")


async def _stream_feedback_response(entry: _FeedbackEntry):
    """Stream LLM tokens as SSE; persist transcript when the turn completes."""
    async for token in entry.session.respond_stream():
        yield f"data: {json.dumps({'token': token})}\n\n"
    # Save after every turn so transcripts survive the user closing the panel.
    _persist_feedback(entry)
    yield f"data: {json.dumps({'done': True})}\n\n"


class FeedbackMessageBody(BaseModel):
    content: str


@router.post("/{slug}/feedback/start")
async def start_feedback(slug: str, body: FeedbackMessageBody, request: Request):
    """Start a feedback conversation for a moment. First message comes from the user."""
    state = request.app.state.server
    tada_dir = _get_tada_dir(request)
    result_dir = tada_dir / "results" / slug

    if not _list_output_pages(result_dir):
        return JSONResponse({"error": "Moment not found"}, status_code=404)

    # If a session for this slug is already in memory (e.g. the user closed the
    # panel without calling /end), flush it to its transcript before replacing.
    existing = state.feedback_sessions.pop(slug, None)
    if existing is not None:
        _persist_feedback(existing)

    # Build system prompt with moment context
    meta_path = result_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    file_contents = _read_moment_files(result_dir)
    system_prompt = FEEDBACK_SYSTEM_PROMPT.format(
        title=meta.get("title", slug),
        description=meta.get("description", ""),
        file_contents=file_contents,
    )

    agent = ChatAgent(
        model=state.config.moments_agent_model,
        system_prompt=system_prompt,
        api_key=_resolve_feedback_api_key(state.config),
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    entry = _FeedbackEntry(
        session=ChatSession(agent=agent, done_marker=None),
        path=result_dir / f"feedback_{timestamp}.md",
    )
    state.feedback_sessions[slug] = entry

    # User sends the first message
    entry.session.add_user_message(body.content)

    return StreamingResponse(
        _stream_feedback_response(entry),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{slug}/feedback/message")
async def send_feedback_message(slug: str, body: FeedbackMessageBody, request: Request):
    """Send a message in the active feedback conversation."""
    state = request.app.state.server

    entry = state.feedback_sessions.get(slug)
    if entry is None:
        return JSONResponse({"error": "No active feedback conversation for this moment"}, status_code=409)

    if not body.content.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    entry.session.add_user_message(body.content)

    return StreamingResponse(
        _stream_feedback_response(entry),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{slug}/feedback/end")
async def end_feedback(slug: str, request: Request):
    """End the feedback conversation and save the transcript."""
    state = request.app.state.server

    entry = state.feedback_sessions.pop(slug, None)
    if entry is None:
        return JSONResponse({"error": "No active feedback conversation for this moment"}, status_code=409)

    _persist_feedback(entry)

    return {"status": "ended", "filename": entry.path.name}


@router.get("/{slug}/feedback/conversation")
async def get_feedback_conversation(slug: str, request: Request):
    """Get the current feedback conversation state."""
    state = request.app.state.server

    entry = state.feedback_sessions.get(slug)
    if entry is not None:
        return {"active": True, "messages": entry.session.visible_messages()}

    return {"active": False, "messages": []}

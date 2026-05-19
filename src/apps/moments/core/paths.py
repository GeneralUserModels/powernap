"""Filesystem helpers for the topic-grouped tada layout."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

# Top-level dir names inside `logs-tada/` that are NOT topic folders.
_RESERVED_DIRS = {"results", "_backups", "_pre_refine"}


def _is_topic_dir(p: Path) -> bool:
    return p.is_dir() and p.name not in _RESERVED_DIRS and not p.name.startswith("_")


def list_task_files(tada_dir: Path) -> list[Path]:
    """Return every task .md under tada_dir, across topic subdirs and any
    top-level task files. Sorted by path for stable iteration."""
    files: list[Path] = list(tada_dir.glob("*.md"))
    for sub in tada_dir.iterdir():
        if _is_topic_dir(sub):
            files.extend(sub.glob("*.md"))
    files.sort()
    return files


def find_task_md(tada_dir: Path, slug: str) -> Path | None:
    """Locate a task .md by slug, checking topic dirs first, then top level."""
    for sub in tada_dir.iterdir():
        if not _is_topic_dir(sub):
            continue
        candidate = sub / f"{slug}.md"
        if candidate.exists():
            return candidate
    top_level = tada_dir / f"{slug}.md"
    return top_level if top_level.exists() else None


def get_topic(md_file: Path, tada_dir: Path) -> str:
    """Topic name for a task .md, derived from its parent dir. Empty string
    for top-level task files sitting directly in tada_dir."""
    if md_file.parent == tada_dir:
        return ""
    return md_file.parent.name


def list_topics(tada_dir: Path) -> list[str]:
    """Return sorted list of topic folder names under tada_dir."""
    return sorted(p.name for p in tada_dir.iterdir() if _is_topic_dir(p))


def list_active_task_files(tada_dir: Path) -> list[Path]:
    """Task .md files that have been executed (have a results dir) and are not dismissed.

    "Active" = the slug appears under `logs-tada/results/<slug>/` AND the slug's
    state in `_moment_state.json` does not have `dismissed: true`. Used by
    discovery/promotion/triggers — they should only consider tasks the user has
    actually engaged with.
    """
    from apps.moments.core.state import load_state

    if not tada_dir.exists():
        return []
    results_dir = tada_dir / "results"
    if not results_dir.exists():
        return []

    moment_state = load_state(tada_dir)
    active: list[Path] = []
    for md in list_task_files(tada_dir):
        slug = md.stem
        if not (results_dir / slug).is_dir():
            continue
        if moment_state.get(slug, {}).get("dismissed"):
            continue
        active.append(md)
    return active


def snapshot_tada_mtimes(tada_dir: Path) -> dict[str, float]:
    """Map slug → mtime for every active (executed + non-dismissed) tada task."""
    return {md.stem: md.stat().st_mtime for md in list_active_task_files(tada_dir)}


def _result_completed_at(tada_dir: Path, slug: str) -> str | None:
    result_dir = tada_dir / "results" / slug
    if not result_dir.is_dir():
        return None
    files = [
        path
        for path in result_dir.rglob("*")
        if path.is_file() and not path.name.startswith("feedback_")
    ]
    if not files:
        return None
    mtime = max(path.stat().st_mtime for path in files)
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="minutes")


def summarize_tada_tasks(tada_dir: Path) -> str:
    """Render active (executed and not dismissed) tada tasks as a markdown listing.

    Each entry shows topic/slug, title, description, cadence, schedule, and trigger
    so the discovery agent can decide whether a new candidate would duplicate
    completed work or an existing task. Excludes tasks that have never been
    executed and tasks the user dismissed — proposing variants of those would
    not be useful.
    """
    from apps.moments.runtime.execute import _parse_frontmatter
    from apps.moments.core.state import load_state

    files = list_active_task_files(tada_dir)
    if not files:
        return "(no existing tasks yet)"

    moment_state = load_state(tada_dir)
    lines: list[str] = []
    for md in files:
        fm = _parse_frontmatter(md.read_text())
        topic = get_topic(md, tada_dir) or "(top-level)"
        slug = md.stem
        title = fm.get("title", slug)
        description = fm.get("description", "")
        cadence = fm.get("cadence", "?")
        schedule = fm.get("schedule", "—")
        trigger = fm.get("trigger") or "—"
        completed_at = _result_completed_at(tada_dir, slug)
        state = moment_state.get(slug, {})
        status_parts: list[str] = []
        if cadence == "once" and completed_at:
            status_parts.append(f"one-shot output already generated at {completed_at}")
        elif completed_at:
            status_parts.append(f"last output generated at {completed_at}")
        if state.get("last_viewed"):
            status_parts.append(f"last viewed at {state['last_viewed']}")
        if state.get("thumbs"):
            status_parts.append(f"user feedback: thumbs {state['thumbs']}")
        status = " | ".join(status_parts) or "status unknown"
        lines.append(
            f"- **{topic}/{slug}** — {title}\n"
            f"  {description}\n"
            f"  cadence: {cadence} | schedule: {schedule} | trigger: {trigger}\n"
            f"  status: {status}"
        )
    return "\n".join(lines)

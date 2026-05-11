"""Ingest new activity logs into the personal knowledge wiki."""

from __future__ import annotations

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

from agent.builder import build_agent
from apps.common.activity_streams import DEFAULT_FILTERED_STREAM_SOURCES
from apps.common.structured_ops import StructuredOpsError, extract_json_object, require_list, require_string, safe_rel_path
from apps.memory.schemas.structured import ExistingPageUpdatePayload, FinalizePageOpsPayload, InventoryPayload, NewPageCreatePayload
from apps.moments.core.incremental import DEFAULT_MISSING_CHECKPOINT_AGE, read_checkpoint, write_checkpoint


_PROMPTS = Path(__file__).parent / "prompts"
INVENTORY_TEMPLATE = (_PROMPTS / "inventory.txt").read_text()
UPDATE_TEMPLATE = (_PROMPTS / "update.txt").read_text()
CREATE_TEMPLATE = (_PROMPTS / "create.txt").read_text()
FINALIZE_TEMPLATE = (_PROMPTS / "finalize.txt").read_text()
SCHEMA_TEMPLATE = (_PROMPTS / "schema.md").read_text()

FILTERED_STREAM_SOURCES = DEFAULT_FILTERED_STREAM_SOURCES

SPECIAL_MEMORY_FILES = {"index.md", "log.md", "schema.md"}
ARCHIVE_MEMORY_DIR = "_archive"
_WIKI_LINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
PREVIEW_MAX_FILES = 20
PREVIEW_MAX_LINES = 8
PREVIEW_MAX_CHARS = 900
DEFAULT_PAGE_AGENT_CONCURRENCY = 3
DEFAULT_PAGE_AGENT_MAX_ROUNDS = 20


@dataclass
class IngestInputs:
    mode: str
    last_run: datetime | None
    new_inputs_list: str
    active_conversations: list[Path]
    chats: list[Path]
    audio: list[Path]
    tada_feedback: list[Path]
    modified_streams: list[str]


@dataclass(frozen=True)
class PageAgentTask:
    pass_name: str
    label: str
    instruction: str
    payload_model: Any
    final_instruction: str
    final_metadata_app: str
    allow_special: bool = False
    require_create_missing: bool = False
    require_update_exists: bool = False
    expected_update_path: str | None = None
    default_create_path: str | None = None


class MemoryIngestProgress:
    """Map nested memory-agent rounds into one monotonic 0-100 progress signal."""

    def __init__(self, on_round: Callable[[int, int], None] | None):
        self._on_round = on_round
        self._lock = Lock()
        self._last_pct = -1

    def emit(self, pct: float) -> None:
        if self._on_round is None:
            return
        next_pct = min(100, max(0, round(pct)))
        with self._lock:
            if next_pct <= self._last_pct:
                return
            self._last_pct = next_pct
            self._on_round(next_pct, 100)

    def phase_callback(self, start_pct: float, span_pct: float) -> Callable[[int, int], None]:
        def _callback(num_turns: int, max_turns: int) -> None:
            denom = max(1, max_turns)
            fraction = min(1.0, max(0.0, num_turns / denom))
            self.emit(start_pct + span_pct * fraction)

        return _callback

    def page_callbacks(self, count: int, start_pct: float, span_pct: float) -> list[Callable[[int, int], None]]:
        if count <= 0:
            return []
        task_progress = [0.0] * count

        def _make_callback(idx: int) -> Callable[[int, int], None]:
            def _callback(num_turns: int, max_turns: int) -> None:
                denom = max(1, max_turns)
                fraction = min(1.0, max(0.0, num_turns / denom))
                with self._lock:
                    task_progress[idx] = max(task_progress[idx], fraction)
                    aggregate = sum(task_progress) / count
                self.emit(start_pct + span_pct * aggregate)

            return _callback

        return [_make_callback(idx) for idx in range(count)]


def _modified_sources(logs_dir: str, since: datetime | None) -> list[str]:
    """Return non-session source files modified after *since*."""
    if since is None:
        return [s for s in FILTERED_STREAM_SOURCES if (Path(logs_dir) / s).exists()]
    result = []
    for src in FILTERED_STREAM_SOURCES:
        p = Path(logs_dir) / src
        if p.exists() and datetime.fromtimestamp(p.stat().st_mtime) > since:
            result.append(src)
    return result


def _new_files_in(base: Path, pattern: str, since: datetime | None) -> list[Path]:
    """Return files matching *pattern* under *base* modified after *since*."""
    if not base.exists():
        return []
    files = sorted(base.rglob(pattern))
    if since is None:
        return files
    return [f for f in files if datetime.fromtimestamp(f.stat().st_mtime) > since]


def _is_hidden_or_special(rel: Path) -> bool:
    return (
        str(rel) in SPECIAL_MEMORY_FILES
        or (bool(rel.parts) and rel.parts[0] == ARCHIVE_MEMORY_DIR)
        or any(part.startswith(".") for part in rel.parts)
    )


def _memory_pages(memory_dir: Path) -> list[Path]:
    if not memory_dir.exists():
        return []
    pages: list[Path] = []
    for path in memory_dir.rglob("*.md"):
        rel = path.relative_to(memory_dir)
        if _is_hidden_or_special(rel):
            continue
        pages.append(path)
    return sorted(pages)


def _all_memory_markdown(memory_dir: Path) -> list[Path]:
    if not memory_dir.exists():
        return []
    return sorted(
        p for p in memory_dir.rglob("*.md")
        if not _is_hidden_or_special(p.relative_to(memory_dir))
    )


def _bootstrap_memory(memory_dir: Path) -> None:
    """Create deterministic first-run wiki files without overwriting user content."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    index = memory_dir / "index.md"
    log = memory_dir / "log.md"
    schema = memory_dir / "schema.md"
    if not index.exists():
        index.write_text("# Memory Index\n\n")
    if not log.exists():
        log.write_text("# Memory Log\n\n")
    if not schema.exists():
        schema.write_text(SCHEMA_TEMPLATE)


def _section(label: str, items: list, formatter) -> str | None:
    if not items:
        return None
    body = "\n".join(f"- {formatter(item)}" for item in items)
    return f"**{label}:**\n{body}"


def _collect_ingest_inputs(logs_path: Path, last_run: datetime | None) -> IngestInputs:
    logs_dir = str(logs_path)
    tada_results = logs_path.parent / "logs-tada" / "results"

    new_active_convos = _new_files_in(logs_path / "active-conversations", "conversation_*.md", last_run)
    new_chats = _new_files_in(logs_path / "chats", "conversation.md", last_run)
    new_audio = _new_files_in(logs_path / "audio", "*.md", last_run)
    new_tada_feedback = _new_files_in(tada_results, "feedback_*.md", last_run)
    modified_streams = _modified_sources(logs_dir, last_run)

    rel = lambda f: os.path.relpath(f, logs_path)
    sections = [
        _section("Active conversations (user-answered Q&A)", new_active_convos, rel),
        _section("Chats with assistant", new_chats, rel),
        _section("Audio transcripts", new_audio, rel),
        _section("Tada moment feedback", new_tada_feedback, rel),
        _section("Modified filtered streams", modified_streams, str),
    ]
    new_inputs_list = "\n\n".join(s for s in sections if s)
    if last_run is None:
        mode = "first_run"
    elif new_inputs_list:
        mode = "incremental"
    else:
        mode = "no_new_data"

    return IngestInputs(
        mode=mode,
        last_run=last_run,
        new_inputs_list=new_inputs_list or "- (none detected)",
        active_conversations=new_active_convos,
        chats=new_chats,
        audio=new_audio,
        tada_feedback=new_tada_feedback,
        modified_streams=modified_streams,
    )


def _existing_pages_list(memory_dir: Path) -> str:
    pages = _memory_pages(memory_dir)
    if not pages:
        return "- (no existing content pages)"
    return "\n".join(f"- {p.relative_to(memory_dir)}" for p in pages)


def _page_excerpt(path: Path, max_chars: int = 280) -> str:
    if not path.exists():
        return ""
    text = path.read_text()
    if text.startswith("---\n"):
        marker = "\n---\n"
        end = text.find(marker, 4)
        if end != -1:
            text = text[end + len(marker):]
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _page_metadata_list(memory_dir: Path, rel_paths: list[str] | None = None) -> str:
    pages: list[Path] = []
    if rel_paths is None:
        pages = _memory_pages(memory_dir)
    else:
        for rel in rel_paths:
            page = memory_dir / rel
            if not page.exists() or page.suffix != ".md":
                continue
            try:
                page_rel = page.relative_to(memory_dir)
            except ValueError:
                continue
            if _is_hidden_or_special(page_rel):
                continue
            pages.append(page)
    if not pages:
        return "- (no content pages)"
    lines = []
    for page in sorted(set(pages)):
        rel = page.relative_to(memory_dir)
        title = _page_title(page)
        category = _page_category(page, memory_dir)
        excerpt = _page_excerpt(page)
        category_suffix = f" — category: {category}" if category else ""
        suffix = f" — {excerpt}" if excerpt else ""
        lines.append(f"- `{rel}` — title: {title}{category_suffix}{suffix}")
    return "\n".join(lines)


def _page_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    if not text.startswith("---\n"):
        return {}
    marker = "\n---\n"
    end = text.find(marker, 4)
    if end == -1:
        return {}
    frontmatter: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            frontmatter[key.strip()] = value.strip().strip("\"'")
    return frontmatter


def _page_title(path: Path) -> str:
    title = _page_frontmatter(path).get("title")
    if title:
        return title
    return path.stem.replace("-", " ").title()


def _page_category(path: Path, memory_dir: Path) -> str | None:
    category = _page_frontmatter(path).get("category")
    if category:
        return category
    rel = path.relative_to(memory_dir)
    if str(rel.parent) != ".":
        return str(rel.parent)
    return None


def _clean_page_ref(ref: Any) -> str:
    text = str(ref).strip().strip("`")
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
    return text


def _resolve_existing_page_refs(memory_dir: Path, refs: list[Any]) -> list[str]:
    lookup: dict[str, str] = {}
    for page in _memory_pages(memory_dir):
        rel = str(page.relative_to(memory_dir))
        stem = rel[:-3] if rel.endswith(".md") else rel
        title = _page_title(page)
        for key in {rel, stem, title}:
            lookup[key.lower()] = rel

    resolved: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        cleaned = _clean_page_ref(ref)
        candidates = [cleaned]
        if cleaned and not cleaned.endswith(".md"):
            candidates.append(f"{cleaned}.md")
        match = next((lookup[candidate.lower()] for candidate in candidates if candidate.lower() in lookup), None)
        if match and match not in seen:
            seen.add(match)
            resolved.append(match)
    return resolved


def _create_candidate_titles(inventory: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    seen: set[str] = set()
    for value in inventory.get("likely_pages_to_create", []):
        title = _clean_page_ref(value)
        if title and title.lower() not in seen:
            seen.add(title.lower())
            titles.append(title)
    return titles


def _candidate_page_path(title: str) -> str:
    cleaned = _clean_page_ref(title)
    if cleaned.endswith(".md") and "/" not in cleaned and not cleaned.startswith("."):
        return cleaned
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")
    if not slug:
        slug = "untitled"
    return f"{slug}.md"


def _preview_line(line: str) -> str:
    text = re.sub(r"\s+", " ", line).strip()
    if len(text) > PREVIEW_MAX_CHARS:
        return text[:PREVIEW_MAX_CHARS].rstrip() + "..."
    return text


def _file_preview(path: Path, root: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    samples: list[str] = []
    line_count = 0
    with path.open(errors="replace") as f:
        for line in f:
            line_count += 1
            if len(samples) >= PREVIEW_MAX_LINES:
                continue
            sample = _preview_line(line)
            if sample:
                samples.append(sample)
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(os.path.relpath(path, root))
    if not samples:
        return f"- `{rel}` ({line_count} lines): (no non-empty preview lines)"
    preview = "\n".join(f"  {i + 1}. {sample}" for i, sample in enumerate(samples))
    return f"- `{rel}` ({line_count} lines):\n{preview}"


def _changed_input_preview(logs_path: Path, inputs: IngestInputs) -> str:
    paths: list[Path] = []
    paths.extend(inputs.active_conversations)
    paths.extend(inputs.chats)
    paths.extend(inputs.audio)
    paths.extend(inputs.tada_feedback)
    paths.extend(logs_path / stream for stream in inputs.modified_streams)

    seen: set[Path] = set()
    previews: list[str] = []
    for path in sorted(paths, key=lambda p: os.path.relpath(p, logs_path)):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        preview = _file_preview(path, logs_path)
        if preview:
            previews.append(preview)
        if len(previews) >= PREVIEW_MAX_FILES:
            break
    if not previews:
        return "- (no changed input preview available)"
    suffix = ""
    if len(seen) < len(paths):
        suffix = f"\n- ({len(paths) - len(seen)} additional changed files omitted from preview)"
    return "\n\n".join(previews) + suffix


def _has_frontmatter(path: Path) -> bool:
    text = path.read_text()
    if not text.startswith("---\n"):
        return False
    return "\n---\n" in text[4:]


def _wiki_link_targets(text: str) -> list[str]:
    targets: list[str] = []
    for match in _WIKI_LINK_RE.finditer(text):
        target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


def _page_identifiers(memory_dir: Path) -> set[str]:
    identifiers: set[str] = set()
    for page in _memory_pages(memory_dir):
        rel = str(page.relative_to(memory_dir))
        stem = rel[:-3] if rel.endswith(".md") else rel
        title = _page_title(page)
        identifiers.update({rel.lower(), stem.lower(), title.lower()})
    return identifiers


def _wiki_link_resolves(target: str, page_identifiers: set[str], index_text: str) -> bool:
    target = target.strip()
    if not target:
        return True
    candidates = {target.lower()}
    if not target.endswith(".md"):
        candidates.add(f"{target}.md".lower())
    if any(candidate in page_identifiers for candidate in candidates):
        return True
    index_lower = index_text.lower()
    return any(candidate in index_lower for candidate in candidates)


def _validate_wiki(memory_dir: Path, today: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    index_path = memory_dir / "index.md"
    log_path = memory_dir / "log.md"
    if not index_path.exists():
        issues.append({"code": "missing_special_file", "path": "index.md", "message": "index.md is missing"})
    if not log_path.exists():
        issues.append({"code": "missing_special_file", "path": "log.md", "message": "log.md is missing"})

    for page in _memory_pages(memory_dir):
        if not _has_frontmatter(page):
            issues.append({
                "code": "missing_frontmatter",
                "path": str(page.relative_to(memory_dir)),
                "message": "Content page is missing YAML frontmatter",
            })

    index_text = index_path.read_text() if index_path.exists() else ""
    index_lower = index_text.lower()
    for page in _memory_pages(memory_dir):
        rel_text = str(page.relative_to(memory_dir))
        stem_text = rel_text[:-3] if rel_text.endswith(".md") else rel_text
        title = _page_title(page)
        represented = (
            rel_text.lower() in index_lower
            or stem_text.lower() in index_lower
            or title.lower() in index_lower
        )
        if not represented:
            issues.append({
                "code": "index_missing_page",
                "path": rel_text,
                "message": "Content page is not represented in index.md by path or title",
            })

    page_identifiers = _page_identifiers(memory_dir)
    seen_unresolved: set[tuple[str, str]] = set()
    for page in _all_memory_markdown(memory_dir):
        rel_text = str(page.relative_to(memory_dir))
        if rel_text == "schema.md":
            continue
        for target in _wiki_link_targets(page.read_text()):
            if _wiki_link_resolves(target, page_identifiers, index_text):
                continue
            key = (rel_text, target)
            if key in seen_unresolved:
                continue
            seen_unresolved.add(key)
            issues.append({
                "code": "unresolved_wiki_link",
                "path": rel_text,
                "target": target,
                "message": f"Wiki link [[{target}]] does not resolve to an existing page or index entry",
            })

    log_text = log_path.read_text() if log_path.exists() else ""
    if f"## {today}" not in log_text:
        issues.append({
            "code": "missing_log_entry",
            "path": "log.md",
            "message": f"log.md needs a dated entry headed '## {today}'",
        })

    return issues


def _format_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)


def _parse_inventory(result: str, expected_mode: str) -> dict[str, Any]:
    payload = InventoryPayload.model_validate_json(result).model_dump()
    if payload.get("mode") != expected_mode:
        raise ValueError(f"Inventory mode {payload.get('mode')!r} did not match expected mode {expected_mode!r}")
    return payload


def _validate_markdown_for_write(path: Path, memory_dir: Path, markdown: str, allow_special: bool) -> None:
    memory_dir = memory_dir.resolve()
    path = path.resolve()
    try:
        rel = path.relative_to(memory_dir)
    except ValueError as exc:
        raise ValueError(f"Memory op path escapes memory dir: {path}") from exc
    if any(part.startswith(".") for part in rel.parts):
        raise ValueError(f"Memory op path cannot be hidden: {rel}")
    is_special = str(rel) in SPECIAL_MEMORY_FILES
    if is_special and not allow_special:
        raise ValueError(f"Memory op cannot modify special file in this pass: {rel}")
    if not is_special and not _has_frontmatter_text(markdown):
        raise ValueError(f"Memory content page must include YAML frontmatter: {rel}")


def _validate_page_for_delete(path: Path, memory_dir: Path) -> None:
    memory_dir = memory_dir.resolve()
    path = path.resolve()
    try:
        rel = path.relative_to(memory_dir)
    except ValueError as exc:
        raise ValueError(f"Memory delete path escapes memory dir: {path}") from exc
    if _is_hidden_or_special(rel):
        raise ValueError(f"Memory delete path must be a normal content page: {rel}")
    if not path.exists():
        raise ValueError(f"delete_pages target does not exist: {rel}")
    if not path.is_file():
        raise ValueError(f"delete_pages target is not a file: {rel}")


def _has_frontmatter_text(text: str) -> bool:
    return text.startswith("---\n") and "\n---\n" in text[4:]


def _parse_page_ops(
    result: str,
    memory_dir: Path,
    allow_special: bool,
    payload_model=None,
    require_create_missing: bool = False,
    require_update_exists: bool = False,
    default_create_path: str | None = None,
    default_update_path: str | None = None,
) -> tuple[dict[str, list[dict[str, str]]], str]:
    memory_dir = memory_dir.resolve()
    try:
        if payload_model is None:
            payload = extract_json_object(result)
        else:
            try:
                payload = payload_model.model_validate_json(result).model_dump()
            except Exception:
                payload = payload_model.model_validate(extract_json_object(result)).model_dump()
    except (StructuredOpsError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    ops: dict[str, list[dict[str, str]]] = {"create_pages": [], "update_pages": [], "delete_pages": []}
    for op_name in ("create_pages", "update_pages"):
        for item in require_list(payload, op_name):
            if not isinstance(item, dict):
                raise ValueError(f"{op_name} entries must be objects")
            if "path" in item:
                rel = require_string(item, "path")
            elif op_name == "create_pages" and default_create_path is not None:
                rel = default_create_path
            elif op_name == "update_pages" and default_update_path is not None:
                rel = default_update_path
            else:
                rel = require_string(item, "path")
            markdown = require_string(item, "markdown")
            path = safe_rel_path(memory_dir, rel, suffix=".md")
            if op_name == "create_pages" and require_create_missing and path.exists():
                raise ValueError(f"create_pages cannot overwrite existing file: {rel}")
            if op_name == "update_pages" and require_update_exists and not path.exists():
                raise ValueError(f"update_pages target does not exist: {rel}")
            _validate_markdown_for_write(path, memory_dir, markdown, allow_special=allow_special)
            ops[op_name].append({"path": str(path.relative_to(memory_dir)), "markdown": markdown})
    for item in require_list(payload, "delete_pages"):
        if not isinstance(item, dict):
            raise ValueError("delete_pages entries must be objects")
        rel = require_string(item, "path")
        path = safe_rel_path(memory_dir, rel, suffix=".md")
        _validate_page_for_delete(path, memory_dir)
        ops["delete_pages"].append({"path": str(path.relative_to(memory_dir))})
    written_paths = {
        item["path"]
        for op_name in ("create_pages", "update_pages")
        for item in ops.get(op_name, [])
    }
    deleted_paths = {item["path"] for item in ops.get("delete_pages", [])}
    conflicts = sorted(written_paths & deleted_paths)
    if conflicts:
        raise ValueError(f"Memory op cannot write and delete the same page: {', '.join(conflicts)}")
    notes = payload.get("notes", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise ValueError("Page operation notes must be a string")
    return ops, notes.strip()


def _apply_page_ops(memory_dir: Path, ops: dict[str, list[dict[str, str]]]) -> list[str]:
    memory_dir = memory_dir.resolve()
    changed: list[str] = []
    for item in ops.get("create_pages", []):
        path = safe_rel_path(memory_dir, item["path"], suffix=".md")
        if path.exists():
            raise ValueError(f"create_pages cannot overwrite existing file: {item['path']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["markdown"])
        changed.append(str(path.relative_to(memory_dir)))
    for item in ops.get("update_pages", []):
        path = safe_rel_path(memory_dir, item["path"], suffix=".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["markdown"])
        changed.append(str(path.relative_to(memory_dir)))
    for item in ops.get("delete_pages", []):
        path = safe_rel_path(memory_dir, item["path"], suffix=".md")
        _validate_page_for_delete(path, memory_dir)
        rel = str(path.relative_to(memory_dir))
        path.unlink()
        changed.append(rel)
        parent = path.parent
        while parent != memory_dir and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return sorted(set(changed))


def _base_prompt_context(now: str, logs_dir: str, memory_dir: Path) -> dict[str, str]:
    return {
        "now": now,
        "logs_dir": logs_dir,
        "memory_dir": str(memory_dir),
    }


def _inventory_prompt(now: str, logs_dir: str, memory_dir: Path, inputs: IngestInputs) -> str:
    last_run_text = (
        inputs.last_run.strftime("%Y-%m-%d %H:%M")
        if inputs.last_run is not None else "never"
    )
    return INVENTORY_TEMPLATE.format(
        now=now,
        logs_dir=logs_dir,
        memory_dir=str(memory_dir),
        mode=inputs.mode,
        last_run_date=last_run_text,
        new_inputs_list=inputs.new_inputs_list,
        existing_pages_list=_existing_pages_list(memory_dir),
        existing_page_metadata=_page_metadata_list(memory_dir),
        changed_input_preview=_changed_input_preview(Path(logs_dir), inputs),
    )


def _update_page_prompt(
    now: str,
    logs_dir: str,
    memory_dir: Path,
    inputs: IngestInputs,
    inventory: dict[str, Any],
    page_rel: str,
) -> str:
    page_path = safe_rel_path(memory_dir, page_rel, suffix=".md")
    return UPDATE_TEMPLATE.format(
        **_base_prompt_context(now, logs_dir, memory_dir),
        mode=inputs.mode,
        new_inputs_list=inputs.new_inputs_list,
        existing_page_metadata=_page_metadata_list(memory_dir),
        inventory_json=_format_json(inventory),
        target_page_path=page_rel,
        target_page_markdown=page_path.read_text(),
    )


def _create_candidate_prompt(
    now: str,
    logs_dir: str,
    memory_dir: Path,
    inputs: IngestInputs,
    inventory: dict[str, Any],
    candidate_title: str,
) -> str:
    return CREATE_TEMPLATE.format(
        **_base_prompt_context(now, logs_dir, memory_dir),
        mode=inputs.mode,
        new_inputs_list=inputs.new_inputs_list,
        existing_page_metadata=_page_metadata_list(memory_dir),
        inventory_json=_format_json(inventory),
        candidate_title=candidate_title,
    )


def _finalize_prompt(
    now: str,
    logs_dir: str,
    memory_dir: Path,
    inputs: IngestInputs,
    inventory: dict[str, Any],
    changed_pages: list[str],
    validation_issues: list[dict[str, str]],
) -> str:
    return FINALIZE_TEMPLATE.format(
        **_base_prompt_context(now, logs_dir, memory_dir),
        mode=inputs.mode,
        today=datetime.now().strftime("%Y-%m-%d"),
        new_inputs_list=inputs.new_inputs_list,
        inventory_json=_format_json(inventory),
        changed_pages_list="\n".join(f"- {p}" for p in changed_pages) or "- (none detected)",
        changed_page_metadata=_page_metadata_list(memory_dir, changed_pages),
        all_page_metadata=_page_metadata_list(memory_dir),
        validation_report=_format_json(validation_issues) if validation_issues else "[]",
    )


def _run_agent_pass(
    pass_name: str,
    instruction: str,
    logs_dir: str,
    model: str,
    api_key: str | None,
    on_round,
    subagent_model: str | None,
    subagent_api_key: str | None,
    final_response_model=None,
    final_instruction: str | None = None,
    final_metadata_app: str = "memory_ingest",
) -> str:
    agent, _ = build_agent(
        model, logs_dir, api_key=api_key,
        subagent_model=subagent_model, subagent_api_key=subagent_api_key,
    )
    if pass_name in {"update_page", "create_page"}:
        agent.max_rounds = _page_agent_max_rounds()
    else:
        agent.max_rounds = 50 if pass_name in {"inventory", "finalize"} else 100
    agent.on_round = on_round
    return agent.run(
        [{"role": "user", "content": instruction}],
        final_response_model=final_response_model,
        final_instruction=final_instruction,
        final_metadata_app=final_metadata_app,
    )


def _page_agent_concurrency() -> int:
    raw = os.getenv("MEMORY_PAGE_AGENT_CONCURRENCY")
    if raw is None:
        return DEFAULT_PAGE_AGENT_CONCURRENCY
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_PAGE_AGENT_CONCURRENCY


def _page_agent_max_rounds() -> int:
    raw = os.getenv("MEMORY_PAGE_AGENT_MAX_ROUNDS")
    if raw is None:
        return DEFAULT_PAGE_AGENT_MAX_ROUNDS
    try:
        return max(5, int(raw))
    except ValueError:
        return DEFAULT_PAGE_AGENT_MAX_ROUNDS


def _run_page_agent_tasks(
    tasks: list[PageAgentTask],
    logs_dir: str,
    memory_dir: Path,
    model: str,
    api_key: str | None,
    on_round,
    subagent_model: str | None,
    subagent_api_key: str | None,
    page_on_rounds: list[Callable[[int, int], None]] | None = None,
) -> tuple[list[str], dict[str, list[dict[str, str]]], str]:
    if not tasks:
        return [], {"create_pages": [], "update_pages": []}, ""

    results: list[tuple[int, PageAgentTask, str]] = []
    max_workers = min(len(tasks), _page_agent_concurrency())
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _run_agent_pass,
                pass_name=task.pass_name,
                instruction=task.instruction,
                logs_dir=logs_dir,
                model=model,
                api_key=api_key,
                on_round=page_on_rounds[idx] if page_on_rounds else on_round,
                subagent_model=subagent_model,
                subagent_api_key=subagent_api_key,
                final_response_model=task.payload_model,
                final_instruction=task.final_instruction,
                final_metadata_app=task.final_metadata_app,
            ): (idx, task)
            for idx, task in enumerate(tasks)
        }
        for future in as_completed(futures):
            idx, task = futures[future]
            results.append((idx, task, future.result()))
            if page_on_rounds:
                page_on_rounds[idx](1, 1)

    combined_ops: dict[str, list[dict[str, str]]] = {"create_pages": [], "update_pages": []}
    notes: list[str] = []
    rendered_results: list[str] = []
    for _idx, task, result in sorted(results, key=lambda item: item[0]):
        ops, note = _parse_page_ops(
            result,
            memory_dir,
            allow_special=task.allow_special,
            payload_model=task.payload_model,
            require_create_missing=task.require_create_missing,
            require_update_exists=task.require_update_exists,
            default_create_path=task.default_create_path,
            default_update_path=task.expected_update_path,
        )
        if task.expected_update_path is not None:
            for item in ops.get("update_pages", []):
                if item["path"] != task.expected_update_path:
                    raise ValueError(
                        f"{task.label} returned update for {item['path']} instead of {task.expected_update_path}"
                    )
        combined_ops["create_pages"].extend(ops.get("create_pages", []))
        combined_ops["update_pages"].extend(ops.get("update_pages", []))
        if note:
            notes.append(f"{task.label}: {note}")
        rendered_results.append(f"### {task.label}\n\n{result}")

    return rendered_results, combined_ops, "\n".join(notes)


def run(
    logs_dir: str,
    model: str,
    api_key: str | None = None,
    on_round=None,
    subagent_model: str | None = None,
    subagent_api_key: str | None = None,
) -> str:
    logs_path = Path(logs_dir).resolve()
    logs_dir = str(logs_path)
    memory_dir = logs_path / "memory"
    _bootstrap_memory(memory_dir)

    checkpoint_path = memory_dir / ".last_run"
    last_run = read_checkpoint(checkpoint_path, default_age=DEFAULT_MISSING_CHECKPOINT_AGE)
    inputs = _collect_ingest_inputs(logs_path, last_run)

    progress = MemoryIngestProgress(on_round)
    progress.emit(0)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    inventory_result = _run_agent_pass(
        "inventory",
        _inventory_prompt(now, logs_dir, memory_dir, inputs),
        logs_dir,
        model,
        api_key,
        progress.phase_callback(0, 20),
        subagent_model,
        subagent_api_key,
        final_response_model=InventoryPayload,
        final_instruction=(
            "Summarize the inventory work as a grounded, bounded ingest plan. "
            "Use only paths and page titles grounded in the conversation and tool results."
        ),
        final_metadata_app="memory_inventory",
    )
    progress.emit(20)
    inventory = _parse_inventory(inventory_result, inputs.mode)

    before_mtimes = {str(p.relative_to(memory_dir)): p.stat().st_mtime for p in _all_memory_markdown(memory_dir)}
    update_targets = _resolve_existing_page_refs(
        memory_dir,
        list(inventory.get("likely_pages_to_update", [])) + list(inventory.get("existing_pages_to_read", [])),
    )
    create_candidates = _create_candidate_titles(inventory)
    page_tasks: list[PageAgentTask] = [
        PageAgentTask(
            pass_name="update_page",
            label=f"Update {page_rel}",
            instruction=_update_page_prompt(now, logs_dir, memory_dir, inputs, inventory, page_rel),
            payload_model=ExistingPageUpdatePayload,
            final_instruction=(
                f"Summarize the result for `{page_rel}` only. "
                "Include exactly one full replacement markdown document for the owned target page. "
                "Do not include a path; the caller already owns the target path."
            ),
            final_metadata_app="memory_update_page",
            require_update_exists=True,
            expected_update_path=page_rel,
        )
        for page_rel in update_targets
    ]
    page_tasks.extend(
        PageAgentTask(
            pass_name="create_page",
            label=f"Create {candidate_title}",
            instruction=_create_candidate_prompt(now, logs_dir, memory_dir, inputs, inventory, candidate_title),
            payload_model=NewPageCreatePayload,
            final_instruction=(
                f"Summarize the create decision for `{candidate_title}`. "
                "Include at most one grounded new page markdown document, with no path. "
                "If this candidate is not grounded enough or duplicates an existing page, skip it."
            ),
            final_metadata_app="memory_create_page",
            require_create_missing=True,
            default_create_path=_candidate_page_path(candidate_title),
        )
        for candidate_title in create_candidates
    )
    content_result_parts, content_ops, content_notes = _run_page_agent_tasks(
        page_tasks,
        logs_dir,
        memory_dir,
        model,
        api_key,
        None,
        subagent_model,
        subagent_api_key,
        page_on_rounds=progress.page_callbacks(len(page_tasks), 20, 60),
    )
    progress.emit(80)
    content_result = "\n\n".join(content_result_parts) or "(no content page agents were needed)"
    content_changed = _apply_page_ops(memory_dir, content_ops)

    after_content_mtimes = {str(p.relative_to(memory_dir)): p.stat().st_mtime for p in _all_memory_markdown(memory_dir)}
    changed = sorted(
        set(content_changed)
        | {rel for rel, mtime in after_content_mtimes.items() if before_mtimes.get(rel) != mtime}
    )
    today = datetime.now().strftime("%Y-%m-%d")
    validation_issues = _validate_wiki(memory_dir, today)

    finalize_result = _run_agent_pass(
        "finalize",
        _finalize_prompt(now, logs_dir, memory_dir, inputs, inventory, changed, validation_issues),
        logs_dir,
        model,
        api_key,
        progress.phase_callback(80, 20),
        subagent_model,
        subagent_api_key,
        final_response_model=FinalizePageOpsPayload,
        final_instruction=(
            "Summarize the finalize pass as page operations that repair index.md, log.md, schema.md, "
            "or other validation issues. Include only minimal grounded stubs needed to resolve listed validation issues."
        ),
        final_metadata_app="memory_finalize_pages",
    )
    finalize_ops, finalize_notes = _parse_page_ops(
        finalize_result,
        memory_dir,
        allow_special=True,
        payload_model=FinalizePageOpsPayload,
    )
    _apply_page_ops(memory_dir, finalize_ops)

    final_issues = _validate_wiki(memory_dir, today)
    if final_issues:
        raise RuntimeError(f"Memory ingest validation failed: {_format_json(final_issues)}")

    write_checkpoint(checkpoint_path)
    progress.emit(100)
    content_notes_text = f"\nNotes: {content_notes}" if content_notes else ""
    finalize_notes_text = f"\nNotes: {finalize_notes}" if finalize_notes else ""

    return (
        "## Inventory\n\n"
        f"{inventory_result}\n\n"
        "## Content\n\n"
        f"{content_result}\n\n"
        f"Applied content page ops: {', '.join(content_changed) or '(none)'}"
        f"{content_notes_text}\n\n"
        "## Finalize\n\n"
        f"{finalize_result}\n\n"
        f"Applied finalize page ops."
        f"{finalize_notes_text}"
    )


if __name__ == "__main__":
    import logging

    from server.config import CONFIG_PATH
    from server.cost_tracker import init_cost_tracking

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Ingest activity logs into the personal knowledge wiki")
    parser.add_argument("logs_dir", help="Path to the logs directory")
    parser.add_argument("-m", "--model", default=None)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()

    tracker = init_cost_tracking()

    config = json.loads(CONFIG_PATH.read_text())
    model = args.model or config.get("memory_agent_model") or config["moments_agent_model"]
    api_key = args.api_key or (
        config.get("memory_agent_api_key")
        or config.get("agent_api_key")
        or config.get("default_llm_api_key")
    )

    result = run(args.logs_dir, model=model, api_key=api_key)
    print(result)

    snapshot, elapsed = tracker.snapshot()
    total_cost = sum(s["cost"] for s in snapshot.values())
    total_tokens = sum(s["input_tokens"] + s["output_tokens"] for s in snapshot.values())
    logging.getLogger(__name__).info(
        "[cost] ingest finished — $%.4f total, %d tokens, %.0fs", total_cost, total_tokens, elapsed
    )

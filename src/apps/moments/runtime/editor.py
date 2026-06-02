"""Direct editor runtime for generated Tada mini-apps."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent.cli_backends import CliBackendConfig, run_stage_via_cli

logger = logging.getLogger(__name__)

OUTPUT_SUBDIR = "output"
REQUIRED_APP_FILES = ("index.html", "styles.css", "app.js", "base.css", "components.js")
MUTABLE_APP_FILES = {"index.html", "styles.css", "app.js"}

_MOMENTS_DIR = Path(__file__).resolve().parent.parent
_PROMPT = (_MOMENTS_DIR / "prompts" / "editor.txt").read_text()
_CANONICAL_COMPONENTS = _MOMENTS_DIR / "templates" / "shared" / "components.js"
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_MARKDOWN_MESSAGE_RE = re.compile(r"^\*\*(User|Codex/Claude|Assistant):\*\*\s?(.*)$")


class MomentEditorError(RuntimeError):
    """Raised when an editor turn cannot be applied safely."""


class EditorTurnResult(BaseModel):
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    draft_patch: dict[str, Any] = Field(default_factory=dict)


@dataclass
class EditorSession:
    """In-memory editor conversation with an on-disk transcript path."""

    path: Path
    messages: list[dict[str, str]] = field(default_factory=list)

    @property
    def json_path(self) -> Path:
        return self.path.with_suffix(".json")

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def visible_messages(self) -> list[dict[str, str]]:
        return list(self.messages)

    def to_markdown(self) -> str:
        lines = ["# Tada App Editor Conversation\n"]
        for msg in self.messages:
            label = "Codex/Claude" if msg["role"] == "assistant" else "User"
            lines.append(f"**{label}:** {msg['content']}\n")
        return "\n".join(lines)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.to_markdown())
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(),
            "messages": self.visible_messages(),
        }
        tmp = self.json_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        tmp.replace(self.json_path)

    @classmethod
    def load(cls, path: Path) -> "EditorSession":
        if path.suffix == ".json":
            payload = json.loads(path.read_text())
            return cls(path=path.with_suffix(".md"), messages=_coerce_messages(payload.get("messages")))

        json_path = path.with_suffix(".json")
        if json_path.is_file():
            return cls.load(json_path)

        return cls(path=path, messages=_parse_markdown_messages(path.read_text(errors="replace")))


def load_latest_editor_session(result_dir: Path) -> EditorSession | None:
    """Load the most recent saved editor conversation for a moment, if any."""

    candidates = [
        path for path in result_dir.glob("edit_*.json")
        if path.is_file()
    ] + [
        path for path in result_dir.glob("edit_*.md")
        if path.is_file()
    ]
    if not candidates:
        return None

    def sort_key(path: Path) -> tuple[float, int]:
        return (path.stat().st_mtime, 1 if path.suffix == ".json" else 0)

    for path in sorted(candidates, key=sort_key, reverse=True):
        try:
            return EditorSession.load(path)
        except Exception:
            logger.warning("Could not load Tada editor transcript from %s", path, exc_info=True)
    return None


def _coerce_messages(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    messages: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        messages.append({"role": role, "content": content})
    return messages


def _parse_markdown_messages(text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    current_role: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_role, current_lines
        if current_role is None:
            return
        messages.append({"role": current_role, "content": "\n".join(current_lines).strip()})
        current_role = None
        current_lines = []

    for line in text.splitlines():
        match = _MARKDOWN_MESSAGE_RE.match(line)
        if match:
            flush()
            label, first_line = match.groups()
            current_role = "assistant" if label in {"Codex/Claude", "Assistant"} else "user"
            current_lines = [first_line]
        elif current_role is not None:
            current_lines.append(line)
    flush()
    return messages


def output_dir_for_result(result_dir: Path) -> Path:
    return result_dir / OUTPUT_SUBDIR


def prepare_editor_bridge(result_dir: Path) -> dict[str, Any]:
    """Install the draft bridge into an existing generated app."""

    output_dir = output_dir_for_result(result_dir)
    if not output_dir.is_dir():
        raise MomentEditorError("Moment app output is missing")

    target = output_dir / "components.js"
    if not _CANONICAL_COMPONENTS.is_file():
        raise MomentEditorError("Canonical Tada components.js is missing")

    changed = True
    if target.is_file():
        changed = target.read_bytes() != _CANONICAL_COMPONENTS.read_bytes()
    shutil.copy2(_CANONICAL_COMPONENTS, target)
    _validate_required_files(output_dir)
    return {"prepared": changed, "revision": revision_for_output(output_dir)}


def revision_for_output(output_dir: Path) -> str:
    mtimes: list[float] = []
    for path in output_dir.rglob("*"):
        if path.is_file():
            mtimes.append(path.stat().st_mtime)
    if not mtimes:
        return str(int(time.time() * 1000))
    return str(int(max(mtimes) * 1000))


def run_editor_turn(
    *,
    result_dir: Path,
    slug: str,
    user_message: str,
    draft_snapshot: dict[str, Any] | None,
    conversation: list[dict[str, str]],
    cli_config: CliBackendConfig,
) -> EditorTurnResult:
    """Run one CLI editor turn and validate the resulting app."""

    output_dir = output_dir_for_result(result_dir)
    if not output_dir.is_dir():
        raise MomentEditorError("Moment app output is missing")

    result_dir.mkdir(parents=True, exist_ok=True)
    control_dir = result_dir / ".editor"
    control_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    result_json = control_dir / f"turn_{stamp}.json"
    log_dir = control_dir / "logs"

    backup_dir = result_dir.parent / "_editor_backups" / f"{slug}_{stamp}"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(output_dir, backup_dir)

    before_hashes = _hash_output_files(output_dir)
    prompt = _build_prompt(
        result_dir=result_dir,
        user_message=user_message,
        draft_snapshot=draft_snapshot or {},
        conversation=conversation,
        result_json=result_json,
    )

    try:
        run_stage_via_cli(
            stage="editor",
            config=cli_config,
            prompt=prompt,
            cwd=result_dir,
            log_dir=log_dir,
            label=f"editor_{slug}",
            expected_outputs=[result_json],
            timeout_s=600,
        )
        _validate_editor_output(output_dir, before_hashes)
        parsed = _parse_result(result_json)
    except Exception:
        _restore_backup(backup_dir, output_dir)
        raise
    else:
        actual_changed = _changed_mutable_files(output_dir, before_hashes)
        if actual_changed:
            parsed.changed_files = actual_changed
        shutil.rmtree(backup_dir, ignore_errors=True)
        return parsed


def _build_prompt(
    *,
    result_dir: Path,
    user_message: str,
    draft_snapshot: dict[str, Any],
    conversation: list[dict[str, str]],
    result_json: Path,
) -> str:
    output_dir = output_dir_for_result(result_dir)
    conversation_text = _format_conversation(conversation[:-1])
    values = {
        "user_message": user_message.strip(),
        "draft_snapshot": json.dumps(draft_snapshot, indent=2, ensure_ascii=False),
        "conversation": conversation_text,
        "meta_json": _read_text(result_dir / "meta.json"),
        "index_html": _read_text(output_dir / "index.html"),
        "app_js": _read_text(output_dir / "app.js"),
        "styles_css": _read_text(output_dir / "styles.css"),
        "result_json_path": str(result_json),
    }

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(replace, _PROMPT)


def _format_conversation(messages: list[dict[str, str]]) -> str:
    if not messages:
        return "No prior editor messages."
    lines: list[str] = []
    for msg in messages[-20:]:
        role = msg.get("role", "user")
        label = "Assistant" if role == "assistant" else "User"
        lines.append(f"{label}: {msg.get('content', '')}")
    return "\n\n".join(lines)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    if len(text) > 100_000:
        return text[:100_000] + "\n... (truncated)"
    return text


def _parse_result(path: Path) -> EditorTurnResult:
    if not path.is_file():
        raise MomentEditorError("Editor did not write a result file")
    try:
        raw = json.loads(path.read_text())
        return EditorTurnResult.model_validate(raw)
    except Exception as exc:
        raise MomentEditorError(f"Editor wrote invalid result JSON: {exc}") from exc


def _validate_required_files(output_dir: Path) -> None:
    missing = [
        name for name in REQUIRED_APP_FILES
        if not (output_dir / name).is_file() or (output_dir / name).stat().st_size == 0
    ]
    if missing:
        raise MomentEditorError(f"Moment app is missing required file(s): {', '.join(missing)}")


def _validate_editor_output(output_dir: Path, before_hashes: dict[str, str]) -> None:
    _validate_required_files(output_dir)

    after_hashes = _hash_output_files(output_dir)
    changed = {
        rel for rel, digest in after_hashes.items()
        if before_hashes.get(rel) != digest
    }
    created = set(after_hashes) - set(before_hashes)
    deleted = set(before_hashes) - set(after_hashes)
    disallowed = sorted((changed | created | deleted) - MUTABLE_APP_FILES)
    if disallowed:
        raise MomentEditorError(
            "Editor changed files outside app.js/styles.css/index.html: "
            + ", ".join(disallowed)
        )

    _node_check(output_dir / "app.js")
    _node_check(output_dir / "components.js")


def _changed_mutable_files(output_dir: Path, before_hashes: dict[str, str]) -> list[str]:
    after_hashes = _hash_output_files(output_dir)
    changed = [
        f"output/{rel}"
        for rel in sorted(MUTABLE_APP_FILES)
        if before_hashes.get(rel) != after_hashes.get(rel)
    ]
    return changed


def _hash_output_files(output_dir: Path) -> dict[str, str]:
    import hashlib

    hashes: dict[str, str] = {}
    if not output_dir.is_dir():
        return hashes
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(output_dir).as_posix()
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _node_check(path: Path) -> None:
    node = shutil.which("node")
    if not node:
        logger.info("Skipping node --check for %s because node is not installed", path)
        return
    proc = subprocess.run(
        [node, "--check", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MomentEditorError(f"JavaScript syntax check failed for {path.name}: {detail}")


def _restore_backup(backup_dir: Path, output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(backup_dir, output_dir)

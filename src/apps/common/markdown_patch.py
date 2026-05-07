"""Deterministic markdown patch operations for agent-produced edits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class MarkdownPatchError(ValueError):
    """Raised when a markdown patch operation is invalid or unsafe."""


@dataclass(frozen=True)
class AppliedPatch:
    type: str
    target: str


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _clean_block(text: str) -> str:
    return text.strip("\n")


def _heading_level(heading: str) -> int:
    match = _HEADING_RE.match(heading.strip())
    if not match:
        raise MarkdownPatchError(f"invalid markdown heading: {heading!r}")
    return len(match.group(1))


def _find_heading(lines: list[str], heading: str) -> int:
    wanted = heading.strip()
    matches = [i for i, line in enumerate(lines) if line.strip() == wanted]
    if not matches:
        raise MarkdownPatchError(f"heading not found: {heading}")
    if len(matches) > 1:
        raise MarkdownPatchError(f"heading is ambiguous: {heading}")
    return matches[0]


def _section_bounds(lines: list[str], heading: str) -> tuple[int, int]:
    start = _find_heading(lines, heading)
    level = _heading_level(lines[start])
    end = len(lines)
    for i in range(start + 1, len(lines)):
        match = _HEADING_RE.match(lines[i].strip())
        if match and len(match.group(1)) <= level:
            end = i
            break
    return start, end


def _insert_block(lines: list[str], index: int, markdown: str) -> list[str]:
    block = _clean_block(markdown)
    if not block:
        return lines
    insert = ["", *block.splitlines(), ""]
    return [*lines[:index], *insert, *lines[index:]]


def update_frontmatter(text: str, fields: dict[str, Any]) -> str:
    if not text.startswith("---\n"):
        raise MarkdownPatchError("frontmatter not found")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise MarkdownPatchError("frontmatter closing marker not found")
    raw = text[4:end].splitlines()
    body = text[end + len("\n---\n"):]
    existing: dict[str, str] = {}
    order: list[str] = []
    for line in raw:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        existing[key] = value.strip()
        order.append(key)
    for key, value in fields.items():
        if not isinstance(key, str) or not key.strip():
            raise MarkdownPatchError("frontmatter field keys must be non-empty strings")
        if not isinstance(value, (str, int, float)):
            raise MarkdownPatchError(f"frontmatter field {key!r} has unsupported value type")
        if key not in existing:
            order.append(key)
        existing[key] = str(value)
    frontmatter = "\n".join(f"{key}: {existing[key]}" for key in order)
    return f"---\n{frontmatter}\n---\n{body}"


def apply_markdown_patches(text: str, patches: list[dict[str, Any]], *, allow_shrink: bool = False) -> tuple[str, list[AppliedPatch]]:
    """Apply semantic markdown patches to *text*.

    Supported patch shapes:
    - {"type": "replace_exact", "old_text": "...", "new_text": "..."}
    - {"type": "append_section", "heading": "## Heading", "markdown": "..."}
    - {"type": "insert_after_heading", "heading": "## Heading", "markdown": "..."}
    - {"type": "append_to_section", "heading": "## Heading", "markdown": "..."}
    - {"type": "replace_section", "heading": "## Heading", "markdown": "..."}
    - {"type": "update_frontmatter", "fields": {"last_updated": "YYYY-MM-DD"}}
    """
    if not isinstance(patches, list):
        raise MarkdownPatchError("patches must be a list")
    current = text
    applied: list[AppliedPatch] = []
    for patch in patches:
        if not isinstance(patch, dict):
            raise MarkdownPatchError("patch entries must be objects")
        patch_type = patch.get("type")
        if not isinstance(patch_type, str) or not patch_type:
            raise MarkdownPatchError("patch type is required")

        if patch_type == "replace_exact":
            old = patch.get("old_text")
            new = patch.get("new_text")
            if not isinstance(old, str) or not old:
                raise MarkdownPatchError("replace_exact old_text is required")
            if not isinstance(new, str):
                raise MarkdownPatchError("replace_exact new_text must be a string")
            count = current.count(old)
            if count == 0:
                raise MarkdownPatchError("replace_exact old_text not found")
            if count > 1:
                raise MarkdownPatchError("replace_exact old_text is not unique")
            current = current.replace(old, new, 1)
            applied.append(AppliedPatch(patch_type, old[:60]))
            continue

        if patch_type == "update_frontmatter":
            fields = patch.get("fields")
            if not isinstance(fields, dict) or not fields:
                raise MarkdownPatchError("update_frontmatter fields are required")
            current = update_frontmatter(current, fields)
            applied.append(AppliedPatch(patch_type, ",".join(sorted(fields))))
            continue

        heading = patch.get("heading")
        markdown = patch.get("markdown")
        if not isinstance(heading, str) or not heading.strip():
            raise MarkdownPatchError(f"{patch_type} heading is required")
        if not isinstance(markdown, str):
            raise MarkdownPatchError(f"{patch_type} markdown must be a string")

        lines = current.splitlines()
        if patch_type == "append_section":
            block = _clean_block(markdown)
            if block:
                current = _ensure_trailing_newline(current.rstrip() + "\n\n" + heading.strip() + "\n\n" + block + "\n")
            applied.append(AppliedPatch(patch_type, heading.strip()))
        elif patch_type == "insert_after_heading":
            idx = _find_heading(lines, heading) + 1
            current = _ensure_trailing_newline("\n".join(_insert_block(lines, idx, markdown)))
            applied.append(AppliedPatch(patch_type, heading.strip()))
        elif patch_type == "append_to_section":
            _start, end = _section_bounds(lines, heading)
            current = _ensure_trailing_newline("\n".join(_insert_block(lines, end, markdown)))
            applied.append(AppliedPatch(patch_type, heading.strip()))
        elif patch_type == "replace_section":
            start, end = _section_bounds(lines, heading)
            old_section = "\n".join(lines[start:end])
            block = _clean_block(markdown)
            if not allow_shrink and len(old_section) > 500 and len(block) < len(old_section) * 0.5:
                raise MarkdownPatchError(f"replace_section would shrink {heading!r} too much")
            replacement = [lines[start]]
            if block:
                replacement.extend(["", *block.splitlines(), ""])
            current = _ensure_trailing_newline("\n".join([*lines[: start + 1], *replacement[1:], *lines[end:]]))
            applied.append(AppliedPatch(patch_type, heading.strip()))
        else:
            raise MarkdownPatchError(f"unsupported patch type: {patch_type}")

        if not allow_shrink and len(text) > 1000 and len(current) < len(text) * 0.75:
            raise MarkdownPatchError("patch sequence would shrink document too much")

    return current, applied

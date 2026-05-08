"""Output sanitizers for agent-facing tools."""

from __future__ import annotations

import copy
import json
from typing import Any


def scrub_raw_events(value: Any) -> Any:
    """Return a copy of JSON-like data with raw event payloads removed."""
    if isinstance(value, dict):
        return {
            key: scrub_raw_events(item)
            for key, item in value.items()
            if key != "raw_events"
        }
    if isinstance(value, list):
        return [scrub_raw_events(item) for item in value]
    return copy.deepcopy(value)


def _sanitize_json_line(line: str) -> str | None:
    try:
        return json.dumps(scrub_raw_events(json.loads(line)), ensure_ascii=False)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(line):
        if char not in "{[":
            continue
        suffix = line[index:]
        stripped = suffix.lstrip()
        leading_ws = len(suffix) - len(stripped)
        try:
            parsed, end = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            continue
        if stripped[end:].strip():
            continue
        prefix = line[: index + leading_ws]
        return prefix + json.dumps(scrub_raw_events(parsed), ensure_ascii=False)
    return None


def sanitize_tool_output(text: str) -> str:
    """Remove raw screen interaction event arrays from tool output.

    The common failure mode is `rg` printing an entire JSONL line from
    `screen/filtered.jsonl`; if that row contains `source.raw_events`, the
    agent transcript becomes dominated by mouse/key noise. JSON-aware scrubbing
    preserves the useful fields while dropping the high-volume event payload.
    """
    if "raw_events" not in text:
        return text

    sanitized_lines: list[str] = []
    for line in text.splitlines():
        if "raw_events" not in line:
            sanitized_lines.append(line)
            continue
        sanitized_lines.append(_sanitize_json_line(line) or line.replace("raw_events", "screen_events_omitted"))

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(sanitized_lines) + suffix

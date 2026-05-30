"""Prompt transforms for CLI backends.

Existing in-process prompts embed tool-plumbing language (`write_file`,
`PlanWrite`, `Read`, subagent rules) that the CLIs don't need — they bring
their own built-in tools. We wrap those sections in HTML-comment markers so
that:
  - the Gemini path keeps them transparently (comments are valid prose),
  - the CLI path strips them via strip_tool_plumbing() before sending.

Per-stage CLI-specific guidance (output file path, schema, cwd) is appended
by cli_footer() at runtime.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Type

TOOL_PLUMBING_BEGIN = "<!-- POWERNAP:TOOL_PLUMBING_BEGIN -->"
TOOL_PLUMBING_END = "<!-- POWERNAP:TOOL_PLUMBING_END -->"

# Match a single block (non-greedy) including the markers themselves and any
# blank line that follows the closing marker, so we don't leave double blanks.
_MARKER_BLOCK = re.compile(
    re.escape(TOOL_PLUMBING_BEGIN) + r".*?" + re.escape(TOOL_PLUMBING_END) + r"\n?",
    re.DOTALL,
)


def strip_tool_plumbing(prompt: str) -> str:
    """Remove all marker-delimited tool-plumbing blocks from `prompt`.

    Idempotent: no-op when markers are absent. Multiple blocks per file are
    all removed.
    """
    return _MARKER_BLOCK.sub("", prompt)


def _schema_skeleton(model: Type[Any]) -> str:
    """Compact JSON-schema string for the CLI to follow."""
    schema = model.model_json_schema()
    return json.dumps(schema, indent=2)


def cli_footer(
    *,
    stage: str,
    cwd: Path,
    output_paths: list[Path] | None = None,
    output_dir: Path | None = None,
    output_model: Type[Any] | None = None,
    extra_notes: str = "",
) -> str:
    """Build a stage-aware CLI footer to append after the (stripped) prompt.

    - For JSON-output stages, pass `output_model` (the Pydantic class) and a
      single path in `output_paths`; we instruct the agent to write a JSON
      file matching the schema to that exact path using its built-in Write
      tool. The schema is embedded as guidance — actual termination relies
      on the runner's `expected_outputs` poll.
    - For file-emitting stages (memory pages, execute mini-app), pass
      `output_dir` and we instruct the agent to write substantive files
      there, then stop.
    """
    if stage == "discover":
        lines: list[str] = [
            "",
            "---",
            "## Runtime: CLI agent",
            "",
            f"Your working directory is `{cwd}`. Write outputs inside this directory only.",
            "",
        ]
    else:
        lines = [
            "",
            "---",
            "## Runtime: CLI agent",
            "",
            "You are running under the codex/claude CLI. Use your built-in Read, "
            "Write, Edit, and Bash tools directly — do not pretend you have any "
            "powernap-specific tools (`write_file`, `read_file`, `PlanWrite`, "
            "`subagent`, `task`). Those references in the body above are leftover "
            "from a different runtime and do not apply to you.",
            "",
            f"Your working directory is `{cwd}`. Write outputs inside this "
            "directory only — the sandbox forbids writing outside it.",
            "",
        ]

    if stage == "execute":
        lines.extend([
            "Before your first read/search/write action, make a plan "
            "using the CLI's native planning or todo mechanism. Do not create "
            "a plan file and do not include the plan in the final app.",
            "",
            "Use live web search proactively for current public information "
            "(sources, pricing, docs, papers, news, product details, or public "
            "context the logs mention but do not explain).",
            "",
            "Use the browser tool when you need page text, dynamic content, "
            "screenshots, interaction, or authenticated pages. This runner "
            "adds browser access separately from Powernap's in-process tool "
            "set; do not look for Powernap browser tool names in the CLI.",
            "",
            "Browser access may optionally use the user's browser cookies when "
            "the runtime has permission. Cookie access may not be granted; if "
            "authenticated browsing is unavailable, continue with public pages "
            "and local evidence instead of blocking.",
            "",
        ])

    if output_model is not None and output_paths:
        out = output_paths[0]
        lines.extend([
            f"When you are done, write a single JSON file to this exact path: `{out}`",
            "",
            "The JSON should follow this schema (a guide, not strict — fields "
            "with defaults can be omitted, but the overall shape must match):",
            "",
            "```json",
            _schema_skeleton(output_model),
            "```",
            "",
        ])
        if stage == "discover":
            lines.extend([
                "Do not write any other files. Do not include explanatory prose "
                "in the JSON — just the structured object. Once the file is "
                "written, stop immediately.",
            ])
        else:
            lines.extend([
                "Do not write any other files. Do not include explanatory prose "
                "in the JSON — just the structured object. Once the file is "
                "written, stop immediately — do not run further verification, "
                "exploration, or tool calls.",
            ])
    elif output_dir is not None:
        if stage == "execute":
            lines.extend([
                f"Write your mini-web-app under `{output_dir}`. The required "
                "files are `index.html`, `styles.css`, `app.js`, plus the "
                "shared `base.css` and `components.js` copied in as siblings. "
                "Optional extras like `data.js` are fine if the JS data is "
                "large enough to split out. Once all five core files exist "
                "and are non-empty, stop immediately — do not run further "
                "verification or exploration.",
            ])
        else:
            lines.extend([
                f"Write your output files (markdown pages) under `{output_dir}`. "
                "Each page should be substantive and self-contained. Once you "
                "have written them, stop immediately — do not run further "
                "verification or exploration.",
            ])
    elif output_paths:
        joined = ", ".join(f"`{p}`" for p in output_paths)
        lines.append(
            f"Write your outputs to: {joined}. Once written, stop immediately — "
            "do not run further verification or exploration."
        )

    if extra_notes:
        lines.extend(["", extra_notes])

    return "\n".join(lines) + "\n"

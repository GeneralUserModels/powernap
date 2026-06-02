"""Build the claude CLI command. Mirrors bangers/scripts/discovery/providers.py."""

from __future__ import annotations

from pathlib import Path


def build_claude_command(
    *,
    claude_bin: str,
    model: str,
    effort: str,
    bare: bool = False,
    permission_mode: str = "bypassPermissions",
    add_dirs: list[Path] | None = None,
    max_turns: int | None = None,
    stream_json: bool = True,
    enable_browser: bool = False,
) -> list[str]:
    """Construct the `claude` argv. Prompt is delivered via stdin (`-p`)."""
    cmd = [claude_bin]
    if bare:
        cmd.append("--bare")
    if enable_browser:
        cmd.append("--chrome")
    cmd.extend(["--model", model, "--effort", effort, "--permission-mode", permission_mode])
    if stream_json:
        cmd.extend(["--output-format", "stream-json", "--verbose", "--include-partial-messages"])
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    for d in add_dirs or []:
        cmd.extend(["--add-dir", str(d)])
    cmd.append("-p")
    return cmd

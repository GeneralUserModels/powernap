"""Build the codex CLI command. Mirrors bangers/scripts/discovery/providers.py."""

from __future__ import annotations

from pathlib import Path


def build_codex_command(
    *,
    codex_bin: str,
    model: str,
    reasoning_effort: str,
    cwd: Path,
    sandbox: str = "workspace-write",
    ignore_user_config: bool = False,
) -> list[str]:
    """Construct the `codex exec` argv. Prompt is delivered via stdin (`-`).

    We deliberately do NOT use codex's `--output-schema` + `-o` flags. They
    require strict JSON Schema (every property must be in `required`), which
    fights Pydantic models that have default values. Instead we instruct the
    agent to write the JSON file itself via its built-in Write tool, and rely
    on the runner's `expected_outputs` poll to terminate the subprocess once
    the file lands.
    """
    cmd = [
        codex_bin,
        "exec",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--sandbox",
        sandbox,
        "--cd",
        str(cwd),
    ]
    if ignore_user_config:
        cmd.append("--ignore-user-config")
    cmd.append("-")
    return cmd

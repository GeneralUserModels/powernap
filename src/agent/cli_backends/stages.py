"""Per-stage CLI invocation — the single function each stage calls.

Hands the right argv + cwd + max_turns to the CLI for a given stage. Stages
are responsible for building the (already-stripped + footered) prompt and
specifying expected_outputs; this module is purely about flag selection and
subprocess dispatch.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from .backend import CliBackendConfig
from .claude import build_claude_command
from .codex import build_codex_command
from .runner import run_cli_agent

logger = logging.getLogger(__name__)

# Per-stage max_turns caps. Claude honors --max-turns; Codex relies on its
# own session limits + reasoning effort, but we still record the cap for the
# heartbeat round-event signal so the UI's progress bar has a denominator.
STAGE_MAX_TURNS: dict[str, int] = {
    "discover": 30,
    "promote": 10,
    "memory_inventory": 30,
    "memory_update_page": 20,
    "memory_create_page": 20,
    "memory_finalize": 30,
    "memory_lint": 60,
    "execute": 90,
    "editor": 30,
    "chat": 40,
}


def run_stage_via_cli(
    *,
    stage: str,
    config: CliBackendConfig,
    prompt: str,
    cwd: Path,
    log_dir: Path,
    label: str,
    expected_outputs: list[Path] | None = None,
    outputs_ready_check: Callable[[], bool] | None = None,
    add_dirs: list[Path] | None = None,
    on_round: Callable[[int, int], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    timeout_s: float | None = None,
) -> None:
    """Run the configured CLI for one stage call. Raises on failure.

    Termination is driven by the runner's `expected_outputs` poll: once every
    expected file exists and is non-empty, the runner gives a short grace
    period and then SIGTERMs the subprocess. This works for both codex and
    claude_code without needing backend-specific output flags.
    """
    max_turns = STAGE_MAX_TURNS.get(stage, 30)

    env = {**os.environ, **(config.extra_env or {})}

    if config.backend == "codex":
        log_dir.mkdir(parents=True, exist_ok=True)
        command = build_codex_command(
            codex_bin=config.codex_bin,
            model=config.codex_model,
            reasoning_effort=config.codex_reasoning_effort,
            cwd=cwd,
        )
        is_claude_stream = False
    elif config.backend == "claude_code":
        # Always browser-based OAuth (no --bare); claude reads its OAuth/
        # keychain credentials directly.
        command = build_claude_command(
            claude_bin=config.claude_bin,
            model=config.claude_model,
            effort=config.claude_effort,
            bare=False,
            max_turns=max_turns,
            add_dirs=add_dirs or [],
            stream_json=True,
        )
        is_claude_stream = True
    else:
        raise ValueError(f"Unknown CLI backend: {config.backend}")

    safe_label = label.replace("/", "_").replace(":", "_")
    stdout_log = log_dir / f"{safe_label}.stdout.log"
    stderr_log = log_dir / f"{safe_label}.stderr.log"

    logger.info(
        "stage[%s] backend=%s label=%s prompt_chars=%d cwd=%s argv=%s",
        stage, config.backend, label, len(prompt), cwd, command,
    )

    run_cli_agent(
        command=command,
        cwd=cwd,
        prompt=prompt,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        env=env,
        label=label,
        on_round=on_round,
        on_text=on_text,
        should_stop=should_stop,
        expected_outputs=expected_outputs,
        outputs_ready_check=outputs_ready_check,
        max_turns=max_turns,
        is_claude_stream=is_claude_stream,
        timeout_s=timeout_s,
    )

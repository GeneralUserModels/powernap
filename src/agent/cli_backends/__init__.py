"""Pluggable CLI agent backends (Codex, Claude Code).

When ServerConfig.agent_backend != "gemini", the four stages (discover, promote,
memory ingest/lint, execute) shell out to one of these CLIs in lieu of running
our in-process Agent + tools loop. The CLIs ship their own agentic harness
with Read/Write/Bash built in, so we don't pass tool schemas and we don't use
sandbox-exec — filesystem scoping comes from `--sandbox workspace-write`
(Codex) and `--permission-mode bypassPermissions` + `--add-dir` (Claude).
"""

from .backend import (
    AGENT_BACKENDS,
    CliAgentAuthError,
    CliAgentCancelled,
    CliAgentError,
    CliBackendConfig,
    cli_config_from_payload,
    cli_config_payload,
    is_cli_backend,
    load_cli_config,
)
from .prompts import cli_footer, strip_tool_plumbing
from .stages import run_stage_via_cli

__all__ = [
    "AGENT_BACKENDS",
    "CliAgentAuthError",
    "CliAgentCancelled",
    "CliAgentError",
    "CliBackendConfig",
    "cli_config_from_payload",
    "cli_config_payload",
    "cli_footer",
    "is_cli_backend",
    "load_cli_config",
    "run_stage_via_cli",
    "strip_tool_plumbing",
]

"""Backend selection and shared types for CLI agent backends."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

AGENT_BACKENDS = ("gemini", "codex", "claude_code")
HARDCODED_CODEX_MODEL = "gpt-5.5"
HARDCODED_CODEX_REASONING_EFFORT = "high"
HARDCODED_CLAUDE_MODEL = "claude-sonnet-4-6"
HARDCODED_CLAUDE_EFFORT = "medium"


class CliAgentError(RuntimeError):
    """Raised when the CLI exits non-zero or expected outputs are missing."""


class CliAgentAuthError(CliAgentError):
    """Raised when the CLI reports an auth failure (missing/expired login)."""


class CliAgentCancelled(CliAgentError):
    """Raised when the run was cancelled via should_stop()."""


@dataclass(frozen=True)
class CliBackendConfig:
    backend: Literal["codex", "claude_code"]
    codex_bin: str
    claude_bin: str
    codex_model: str
    codex_reasoning_effort: str
    claude_model: str
    claude_effort: str
    # Subprocess-scoped env additions (PATH augmented with cli_bin_extra_path
    # so binaries installed to a non-default npm prefix remain discoverable).
    extra_env: dict[str, str] = field(default_factory=dict)


def is_cli_backend(name: str | None) -> bool:
    return name in {"codex", "claude_code"}


def cli_config_payload(server_config: Any) -> dict | None:
    """Serialize a CliBackendConfig into a JSON-safe dict for the worker payload.

    Returns None when the configured backend is gemini so callers can decide
    whether to include the key in the payload at all.
    """
    cfg = load_cli_config(server_config)
    if cfg is None:
        return None
    return {
        "backend": cfg.backend,
        "codex_bin": cfg.codex_bin,
        "claude_bin": cfg.claude_bin,
        "codex_model": cfg.codex_model,
        "codex_reasoning_effort": cfg.codex_reasoning_effort,
        "claude_model": cfg.claude_model,
        "claude_effort": cfg.claude_effort,
        "extra_env": dict(cfg.extra_env),
    }


def cli_config_from_payload(payload: dict | None) -> CliBackendConfig | None:
    """Rebuild a CliBackendConfig from the worker payload dict, or None."""
    if not payload:
        return None
    return CliBackendConfig(
        backend=payload["backend"],
        codex_bin=payload.get("codex_bin", "codex"),
        claude_bin=payload.get("claude_bin", "claude"),
        codex_model=HARDCODED_CODEX_MODEL,
        codex_reasoning_effort=HARDCODED_CODEX_REASONING_EFFORT,
        claude_model=HARDCODED_CLAUDE_MODEL,
        claude_effort=HARDCODED_CLAUDE_EFFORT,
        extra_env=dict(payload.get("extra_env") or {}),
    )


def load_cli_config(server_config: Any) -> CliBackendConfig | None:
    """Return CliBackendConfig when cfg.agent_backend is a CLI backend, else None.

    The single signal stages need to decide whether to take the CLI branch.
    """
    backend = getattr(server_config, "agent_backend", "gemini") or "gemini"
    if not is_cli_backend(backend):
        return None

    extra_env: dict[str, str] = {}

    # PATH augmentation so a binary installed to e.g. ~/.local/bin (via the
    # install endpoint's --prefix fallback) stays discoverable on restart.
    extra_path = (getattr(server_config, "cli_bin_extra_path", "") or "").strip()
    if extra_path:
        current_path = os.environ.get("PATH", "")
        extra_env["PATH"] = f"{extra_path}:{current_path}" if current_path else extra_path

    return CliBackendConfig(
        backend=backend,  # type: ignore[arg-type]
        codex_bin=getattr(server_config, "codex_bin", "codex") or "codex",
        claude_bin=getattr(server_config, "claude_bin", "claude") or "claude",
        codex_model=HARDCODED_CODEX_MODEL,
        codex_reasoning_effort=HARDCODED_CODEX_REASONING_EFFORT,
        claude_model=HARDCODED_CLAUDE_MODEL,
        claude_effort=HARDCODED_CLAUDE_EFFORT,
        extra_env=extra_env,
    )

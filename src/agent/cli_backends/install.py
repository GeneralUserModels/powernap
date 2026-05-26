"""Install + auth detection helpers for the CLI backends.

These power the GET /api/agent-backend/status, POST /api/agent-backend/install,
POST /api/agent-backend/login endpoints. We never sudo — when the system npm
prefix isn't user-writable, install via `--prefix ~/.local` and ask the
caller to persist `~/.local/bin` to ServerConfig.cli_bin_extra_path so future
restarts find the binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BACKENDS = ("codex", "claude_code")

NPM_PACKAGE = {
    "codex": "@openai/codex",
    "claude_code": "@anthropic-ai/claude-code",
}

BINARY_NAME = {
    "codex": "codex",
    "claude_code": "claude",
}

# Each CLI ships a probe subcommand we can call to read auth status reliably
# (much more accurate than guessing from leftover ~/.codex / ~/.claude state,
# since both tools keep config files around after logout).
AUTH_PROBE = {
    # `codex login status` → stdout "Not logged in" (exit 1) or "Logged in as ..." (exit 0).
    "codex": ["login", "status"],
    # `claude auth status` → JSON `{"loggedIn": true/false, ...}` on stdout.
    "claude_code": ["auth", "status"],
}

# Login / logout subcommand argv tails.
LOGIN_CMD = {
    "codex": ["login"],
    "claude_code": ["auth", "login"],
}
LOGOUT_CMD = {
    "codex": ["logout"],
    "claude_code": ["auth", "logout"],
}


@dataclass
class BackendStatus:
    backend: str
    available: bool
    version: str | None
    bin: str | None
    auth: Literal["unknown", "oauth", "missing"]
    install_hint: str

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "available": self.available,
            "version": self.version,
            "bin": self.bin,
            "auth": self.auth,
            "install_hint": self.install_hint,
        }


def _path_with_extra(extra: str | None) -> str:
    base = os.environ.get("PATH", "")
    if not extra:
        return base
    return f"{extra}:{base}" if base else extra


def _bin_path(backend: str, extra_path: str | None = None) -> str | None:
    name = BINARY_NAME[backend]
    return shutil.which(name, path=_path_with_extra(extra_path))


def _probe_version(bin_path: str) -> str | None:
    try:
        proc = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    return out.splitlines()[0] if out else None


def _detect_auth(
    backend: str,
    bin_path: str,
    env: dict[str, str],
) -> Literal["unknown", "oauth", "missing"]:
    """Probe the CLI's own status subcommand for browser-based OAuth state."""
    try:
        proc = subprocess.run(
            [bin_path, *AUTH_PROBE[backend]],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "missing"

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")

    if backend == "claude_code":
        # JSON: {"loggedIn": true, "authMethod": "claude.ai" | ...}
        import json as _json
        try:
            payload = _json.loads(proc.stdout or "{}")
        except Exception:
            payload = {}
        return "oauth" if payload.get("loggedIn") else "missing"

    if backend == "codex":
        # "Not logged in" (rc=1) vs "Logged in as ..." (rc=0).
        if proc.returncode == 0 and "logged in" in out.lower() and "not logged in" not in out.lower():
            return "oauth"
        return "missing"

    return "unknown"


def detect_cli(backend: str, *, extra_path: str | None = None) -> BackendStatus:
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")
    bin_path = _bin_path(backend, extra_path)
    version = _probe_version(bin_path) if bin_path else None
    if bin_path:
        env = dict(os.environ)
        if extra_path:
            env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
        auth = _detect_auth(backend, bin_path, env)
    else:
        auth = "missing"
    return BackendStatus(
        backend=backend,
        available=bool(bin_path),
        version=version,
        bin=bin_path,
        auth=auth,
        install_hint=f"npm install -g {NPM_PACKAGE[backend]}",
    )


@dataclass
class InstallResult:
    ok: bool
    bin: str | None
    extra_path: str | None  # caller persists to ServerConfig.cli_bin_extra_path
    log_path: Path
    reason: str | None = None


def _npm_prefix() -> Path | None:
    try:
        proc = subprocess.run(
            ["npm", "config", "get", "prefix"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return Path(out) if out else None


def _is_writable(p: Path) -> bool:
    if not p.exists():
        # Try a parent that exists.
        return _is_writable(p.parent) if p.parent != p else False
    return os.access(p, os.W_OK)


def install_cli(backend: str, *, log_dir: Path) -> InstallResult:
    """Run `npm install -g <pkg>`. Falls back to --prefix ~/.local if the
    system prefix isn't user-writable. Never sudo.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")

    if shutil.which("npm") is None:
        log_path = log_dir / f"install_{backend}_{int(time.time())}.log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text("npm not found on PATH; install Node.js first.\n")
        return InstallResult(
            ok=False,
            bin=None,
            extra_path=None,
            log_path=log_path,
            reason="npm_not_found",
        )

    pkg = NPM_PACKAGE[backend]
    use_local_prefix = False
    prefix = _npm_prefix()
    if prefix is None or not _is_writable(prefix):
        use_local_prefix = True

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"install_{backend}_{int(time.time())}.log"

    cmd = ["npm", "install", "-g", pkg]
    extra_path: str | None = None
    env = dict(os.environ)
    if use_local_prefix:
        local_prefix = Path.home() / ".local"
        cmd = ["npm", "install", "-g", "--prefix", str(local_prefix), pkg]
        extra_path = str(local_prefix / "bin")
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, text=True)

    bin_path = _bin_path(backend, extra_path)
    return InstallResult(
        ok=proc.returncode == 0 and bin_path is not None,
        bin=bin_path,
        extra_path=extra_path,
        log_path=log_path,
        reason=None if proc.returncode == 0 else f"npm exited {proc.returncode}",
    )


@dataclass
class LoginResult:
    ok: bool
    detail: str
    bin: str | None
    auth: str


def login_cli(backend: str, *, extra_path: str | None = None) -> LoginResult:
    """Spawn the CLI's browser-based login subcommand. Interactive — returns
    immediately after spawn; the UI polls /status until auth flips.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")

    bin_path = _bin_path(backend, extra_path)
    if bin_path is None:
        return LoginResult(ok=False, detail=f"{BINARY_NAME[backend]} not installed", bin=None, auth="missing")

    cmd = [bin_path, *LOGIN_CMD[backend]]
    env = dict(os.environ)
    if extra_path:
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return LoginResult(
        ok=True,
        detail="Complete sign-in in the browser window that just opened.",
        bin=bin_path,
        auth="unknown",
    )


def logout_cli(backend: str, *, extra_path: str | None = None) -> LoginResult:
    bin_path = _bin_path(backend, extra_path)
    if bin_path is None:
        return LoginResult(ok=False, detail=f"{BINARY_NAME[backend]} not installed", bin=None, auth="missing")
    env = dict(os.environ)
    if extra_path:
        env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    proc = subprocess.run(
        [bin_path, *LOGOUT_CMD[backend]],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Re-probe so the caller sees the actual post-logout state, not an assumption.
    fresh_auth = _detect_auth(backend, bin_path, env)
    return LoginResult(
        ok=proc.returncode == 0,
        detail=(proc.stdout or proc.stderr or "").strip() or "Signed out.",
        bin=bin_path,
        auth=fresh_auth,
    )

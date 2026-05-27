"""GET /api/agent-backend/status, POST install/login/logout — Codex / Claude CLI."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent.cli_backends.install import (
    BACKENDS,
    detect_cli,
    detect_npm,
    install_cli,
    login_cli,
    logout_cli,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent-backend", tags=["agent-backend"])


class InstallRequest(BaseModel):
    backend: str


class LoginRequest(BaseModel):
    backend: str


class LogoutRequest(BaseModel):
    backend: str


def _state(request: Request):
    return request.app.state.server


def _extra_path(cfg) -> str:
    return getattr(cfg, "cli_bin_extra_path", "") or ""


@router.get("/status")
async def get_status(request: Request):
    cfg = _state(request).config
    extra = _extra_path(cfg)
    return {
        "selected": getattr(cfg, "agent_backend", "gemini"),
        "cli_bin_extra_path": extra,
        "npm": detect_npm().to_dict(),
        "backends": {
            backend: detect_cli(backend, extra_path=extra).to_dict()
            for backend in BACKENDS
        },
    }


@router.post("/install")
async def install(req: InstallRequest, request: Request):
    if req.backend not in BACKENDS:
        raise HTTPException(status_code=400, detail=f"unknown backend {req.backend!r}")
    state = _state(request)
    cfg = state.config

    log_dir = Path(tempfile.gettempdir()) / "powernap-cli-install"
    result = install_cli(req.backend, log_dir=log_dir)

    # If the install fell back to a non-default prefix, persist the bin dir
    # so future restarts find the binary.
    if result.ok and result.extra_path and result.extra_path != _extra_path(cfg):
        cfg.cli_bin_extra_path = result.extra_path
        cfg.save()

    return {
        "ok": result.ok,
        "bin": result.bin,
        "extra_path": result.extra_path,
        "log_path": str(result.log_path),
        "reason": result.reason,
        # Re-probe so the client gets fresh status without a second round-trip.
        "status": detect_cli(req.backend, extra_path=_extra_path(cfg)).to_dict(),
    }


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    if req.backend not in BACKENDS:
        raise HTTPException(status_code=400, detail=f"unknown backend {req.backend!r}")
    cfg = _state(request).config
    result = login_cli(req.backend, extra_path=_extra_path(cfg))
    return {
        "ok": result.ok,
        "detail": result.detail,
        "bin": result.bin,
        "auth": result.auth,
    }


@router.post("/logout")
async def logout(req: LogoutRequest, request: Request):
    if req.backend not in BACKENDS:
        raise HTTPException(status_code=400, detail=f"unknown backend {req.backend!r}")
    cfg = _state(request).config
    result = logout_cli(req.backend, extra_path=_extra_path(cfg))
    return {
        "ok": result.ok,
        "detail": result.detail,
        "bin": result.bin,
        "auth": result.auth,
    }

"""Connector routes — query and toggle connector enabled state.

Registered by server/app.py under /api/connectors.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from server.feature_flags import is_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connectors", tags=["connectors"])
LABEL_HISTORY_DEFAULT_LIMIT = 50
LABEL_HISTORY_MAX_LIMIT = 500
LABEL_HISTORY_TAIL_CHUNK_BYTES = 64 * 1024

# Virtual audio connector names — map to the single "audio" backend connector
_AUDIO_VIRTUAL = {"microphone", "system_audio"}
_AUDIO_FEATURE_FLAGS = {
    "microphone": "connector_microphone",
    "system_audio": "connector_system_audio",
}


class ConnectorUpdate(BaseModel):
    enabled: bool


@router.get("")
async def get_connectors(request: Request):
    state = request.app.state.server
    config = state.config
    google_ok = bool(
        config.google_token_path and Path(config.google_token_path).exists()
    )
    outlook_ok = bool(
        config.outlook_token_path and Path(config.outlook_token_path).exists()
    )

    def _available(ra: str | None) -> bool:
        if ra == "google":
            return google_ok
        if ra == "outlook":
            return outlook_ok
        return True

    result = {}
    for name, conn in state.connectors.items():
        # Hide the real "audio" connector — it's exposed as virtual entries below
        if name == "audio":
            continue
        result[name] = {
            "enabled": not conn.paused,
            "available": _available(state.connector_auth.get(name)),
            "error": conn.error,
            "requires_auth": state.connector_auth.get(name),
        }

    # Add virtual audio connector entries
    audio_conn = state.connectors.get("audio")
    if audio_conn is not None:
        for vname in _AUDIO_VIRTUAL:
            if not is_enabled(config, _AUDIO_FEATURE_FLAGS[vname]):
                continue
            result[vname] = {
                "enabled": vname in config.enabled_connectors,
                "available": True,
                "error": config.connector_errors.get(vname),
                "requires_auth": None,
            }

    return result


@router.put("/{name}")
async def update_connector(name: str, update: ConnectorUpdate, request: Request):
    state = request.app.state.server
    config = state.config

    # Handle virtual audio connectors
    if name in _AUDIO_VIRTUAL:
        logger.info("audio-toggle: %s → enabled=%s", name, update.enabled)
        audio_conn = state.connectors.get("audio")
        if audio_conn is None:
            logger.error("audio-toggle: audio connector not in state.connectors")
            raise HTTPException(status_code=404, detail=f"Audio connector not available")

        if update.enabled:
            if name not in config.enabled_connectors:
                config.enabled_connectors.append(name)
            config.connector_errors.pop(name, None)
        else:
            if name in config.enabled_connectors:
                config.enabled_connectors.remove(name)
        config.save()

        mic_on = "microphone" in config.enabled_connectors
        sys_on = "system_audio" in config.enabled_connectors
        # Was any source already on BEFORE this toggle?
        other = "system_audio" if name == "microphone" else "microphone"
        was_any_on = other in config.enabled_connectors
        logger.info(
            "audio-toggle: post-config mic_on=%s sys_on=%s was_any_on=%s session_alive=%s",
            mic_on, sys_on, was_any_on, audio_conn._session is not None,
        )

        # Update env vars
        audio_conn._server_params.env["TADA_MIC_ENABLED"] = "1" if mic_on else "0"
        audio_conn._server_params.env["TADA_SYS_ENABLED"] = "1" if sys_on else "0"

        if not mic_on and not sys_on:
            # Both off — flush remaining audio, then stop
            logger.info("audio-toggle: both off → flush + stop")
            if audio_conn._session is not None:
                await audio_conn._session.call_tool("flush_audio", {})
            audio_conn.stop()
            audio_conn._server_params.env.pop("TADA_SESSION_FILE", None)
        else:
            session_file_str: str | None = None
            # Create session file if this is the first source being enabled
            if not was_any_on:
                log_dir = config.log_dir or "./logs"
                transcript_dir = Path(log_dir) / "audio"
                transcript_dir.mkdir(parents=True, exist_ok=True)
                dt = datetime.now()
                session_file = transcript_dir / f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}.md"
                session_file.write_text(f"# Audio Transcript — {dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                session_file_str = str(session_file)
                audio_conn._server_params.env["TADA_SESSION_FILE"] = session_file_str
                logger.info("audio-toggle: new session transcript file %s", session_file)

            if audio_conn._session is not None:
                # Server already running — toggle sources in-place, no restart needed
                args: dict = {"mic_enabled": mic_on, "sys_enabled": sys_on}
                if session_file_str is not None:
                    args["session_file"] = session_file_str
                logger.info("audio-toggle: server already running → calling configure_sources(%s)", args)
                await audio_conn._session.call_tool("configure_sources", args)
                logger.info("audio-toggle: configure_sources returned")
            else:
                # Server not running yet — restart so it picks up env vars
                logger.info("audio-toggle: no live session → stop + delayed resume to spawn child")
                audio_conn.stop()

                async def _delayed_resume():
                    await asyncio.sleep(2)
                    logger.info("audio-toggle: delayed resume firing")
                    audio_conn.resume()

                asyncio.create_task(_delayed_resume())

        return {"ok": True, "name": name, "enabled": update.enabled}

    # Standard connector handling
    connector = state.connectors.get(name)
    if connector is None:
        raise HTTPException(status_code=404, detail=f"Unknown connector: {name}")
    if update.enabled:
        connector.resume()  # also clears connector.error
        if name not in state.config.enabled_connectors:
            state.config.enabled_connectors.append(name)
        state.config.connector_errors.pop(name, None)
    else:
        connector.stop()
        if name in state.config.enabled_connectors:
            state.config.enabled_connectors.remove(name)
    state.config.save()
    return {"ok": True, "name": name, "enabled": update.enabled}


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """Return up to max_lines complete lines from the end of a text file."""
    if max_lines <= 0:
        return []

    with path.open("rb") as f:
        f.seek(0, 2)
        position = f.tell()
        chunks: deque[bytes] = deque()
        newline_count = 0

        while position > 0 and newline_count <= max_lines:
            read_size = min(LABEL_HISTORY_TAIL_CHUNK_BYTES, position)
            position -= read_size
            f.seek(position)
            chunk = f.read(read_size)
            chunks.appendleft(chunk)
            newline_count += chunk.count(b"\n")

    data = b"".join(chunks)
    if not data:
        return []

    lines = data.splitlines()
    if position > 0 and lines:
        lines = lines[1:]
    return [line.decode("utf-8", errors="replace") for line in lines[-max_lines:]]


def _load_label_history(log_dir: Path, limit: int) -> list[dict]:
    entries = []
    for jsonl_path in log_dir.glob("*/filtered.jsonl"):
        for line in _tail_lines(jsonl_path, limit):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                text = (
                    entry["text"]
                    if entry.get("prediction_event")
                    else f"[{entry.get('source_name', '')}] {entry['text']}"
                )
                entries.append({
                    "text": text,
                    "timestamp": entry["timestamp"],
                    "dense_caption": entry.get("dense_caption", "") or "",
                })
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.debug("Skipping malformed label-history row in %s", jsonl_path, exc_info=True)
    entries.sort(key=lambda e: e["timestamp"])
    return entries[-limit:]


@router.get("/label-history")
async def get_label_history(
    request: Request,
    limit: int = Query(default=LABEL_HISTORY_DEFAULT_LIMIT, ge=1, le=LABEL_HISTORY_MAX_LIMIT),
):
    log_dir = Path(request.app.state.server.config.log_dir)
    return await asyncio.to_thread(_load_label_history, log_dir, limit)

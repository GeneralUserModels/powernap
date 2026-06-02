"""One-shot background job subprocess runner.

Heavy Tada/Memex agent work runs in short-lived child processes so the
FastAPI event loop stays responsive while jobs are active.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Union

logger = logging.getLogger(__name__)

WORKER_EVENT_PREFIX = "__tada_worker_event__ "

WorkerEvent = Dict[str, Any]
WorkerEventHandler = Callable[[WorkerEvent], Union[None, Awaitable[None]]]


class ProcessJobError(RuntimeError):
    """Raised when a worker exits unsuccessfully or violates the protocol."""


@dataclass
class ProcessJobRunner:
    """Run one background job in a Python subprocess.

    Payloads are sent over stdin so secrets never appear in argv. Structured
    progress is emitted by the child as prefixed JSONL on stdout.
    """

    module: str = "apps.background_worker"
    python_executable: str = field(default_factory=lambda: sys.executable)
    event_prefix: str = WORKER_EVENT_PREFIX
    terminate_timeout_s: float = 5.0
    _last_output_lines: int = 50

    async def run(
        self,
        job_name: str,
        payload: dict[str, Any],
        *,
        on_event: WorkerEventHandler | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run *job_name* and return the final result payload."""

        merged_env = {
            **os.environ,
            **(env or {}),
            "TADA_PARENT_PID": str(os.getpid()),
            "TADA_PARENT_WATCHDOG": "1",
            "PYTHONUNBUFFERED": "1",
        }
        cmd = [self.python_executable, "-u", "-m", self.module, job_name]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        # Bump StreamReader's per-line buffer. Codex/Claude relay raw log
        # rows through their stderr (e.g. a single screen/filtered.jsonl line
        # of mouse events can exceed 100KB). With the asyncio default of
        # 64KB, readline() raises LimitOverrunError and the gather() crashes
        # even though the worker itself finished cleanly. 16MB is plenty for
        # any single line we expect; _safe_readline below catches anything
        # bigger and just fragments it.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream._limit = 16 * 1024 * 1024

        final_result: dict[str, Any] | None = None
        recent_output: list[str] = []

        def remember(line: str) -> None:
            recent_output.append(line)
            if len(recent_output) > self._last_output_lines:
                del recent_output[: len(recent_output) - self._last_output_lines]

        async def dispatch(event: WorkerEvent) -> None:
            if on_event is None:
                return
            result = on_event(event)
            if inspect.isawaitable(result):
                await result

        async def _safe_readline(reader: asyncio.StreamReader) -> bytes:
            """readline() that survives over-limit lines by fragmenting them.

            Returns b"" on real EOF. On LimitOverrunError, drains the bytes
            the reader already has and returns them (without trailing
            newline) — caller treats it as a normal line. The next readline
            picks up the rest. WORKER_EVENT_PREFIX events fragmented this
            way will fail json.loads downstream, but that's better than
            killing the whole read loop.
            """
            try:
                return await reader.readline()
            except asyncio.LimitOverrunError as exc:
                return await reader.readexactly(exc.consumed)
            except ValueError as exc:
                # Older Python wraps LimitOverrunError in ValueError; same recovery.
                logger.warning("[worker:%s] readline ValueError: %s; fragmenting", job_name, exc)
                # Best-effort: drain whatever's in the buffer right now.
                buf = b""
                while reader._buffer:
                    chunk = bytes(reader._buffer)
                    reader._buffer.clear()
                    buf += chunk
                    if len(buf) >= reader._limit:
                        break
                return buf

        async def read_stdout() -> None:
            nonlocal final_result
            assert proc.stdout is not None
            while True:
                raw = await _safe_readline(proc.stdout)
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                if not line.startswith(self.event_prefix):
                    remember(f"stdout: {line}")
                    logger.info("[worker:%s] %s", job_name, line)
                    continue
                event_text = line[len(self.event_prefix):]
                try:
                    event = json.loads(event_text)
                except json.JSONDecodeError as exc:
                    # Most likely a fragmented event from a previous
                    # LimitOverrunError. Log and continue rather than crash
                    # the whole run — the worker may still be making progress.
                    logger.warning(
                        "[worker:%s] dropped malformed event line (%d bytes): %s",
                        job_name, len(event_text), exc,
                    )
                    continue
                if not isinstance(event, dict):
                    logger.warning("[worker:%s] dropped non-object event", job_name)
                    continue
                if event.get("type") == "result":
                    result = event.get("result")
                    if not isinstance(result, dict):
                        raise ProcessJobError(f"Worker {job_name} emitted invalid result event")
                    final_result = result
                else:
                    await dispatch(event)

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                raw = await _safe_readline(proc.stderr)
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                remember(f"stderr: {line}")
                logger.warning("[worker:%s] %s", job_name, line)

        async def write_payload() -> None:
            if proc.stdin is None:
                return
            try:
                proc.stdin.write(json.dumps(payload).encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        stdout_task = asyncio.create_task(read_stdout(), name=f"{job_name}:stdout")
        stderr_task = asyncio.create_task(read_stderr(), name=f"{job_name}:stderr")
        payload_task = asyncio.create_task(write_payload(), name=f"{job_name}:stdin")

        try:
            await payload_task
            returncode = await proc.wait()
            await asyncio.gather(stdout_task, stderr_task)
        except asyncio.CancelledError:
            await self._terminate(proc)
            for task in (payload_task, stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(payload_task, stdout_task, stderr_task, return_exceptions=True)
            raise
        except Exception:
            await self._terminate(proc)
            for task in (payload_task, stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(payload_task, stdout_task, stderr_task, return_exceptions=True)
            raise

        if returncode != 0:
            detail = "\n".join(recent_output[-10:])
            raise ProcessJobError(f"Worker {job_name} exited with code {returncode}\n{detail}")
        if final_result is None:
            detail = "\n".join(recent_output[-10:])
            raise ProcessJobError(f"Worker {job_name} exited without a final result\n{detail}")
        return final_result

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.terminate_timeout_s)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()


async def relay_worker_event(state: Any, event: WorkerEvent) -> None:
    """Relay structured worker events onto the existing SSE activity channel."""

    event_type = event.get("type")
    if event_type not in {"activity", "round"}:
        return
    agent = event.get("agent")
    if not isinstance(agent, str) or not agent:
        return
    message = event.get("message")
    if message is not None and not isinstance(message, str):
        message = str(message)
    await state.broadcast_activity(
        agent,
        message,
        slug=event.get("slug") if isinstance(event.get("slug"), str) else None,
        cadence=event.get("cadence") if isinstance(event.get("cadence"), str) else None,
        num_turns=event.get("num_turns") if isinstance(event.get("num_turns"), int) else None,
        max_turns=event.get("max_turns") if isinstance(event.get("max_turns"), int) else None,
    )

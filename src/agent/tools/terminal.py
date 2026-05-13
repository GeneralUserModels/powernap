import asyncio
import os
import re
import select
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from sandbox_runtime import SandboxManager
from sandbox_runtime.config import FilesystemConfig, NetworkConfig, SandboxRuntimeConfig

from .base_tool import BaseTool
from .sanitize import sanitize_tool_output


OUTPUT_LIMIT = 50000
READ_CHUNK_SIZE = 8192
OUTPUT_TRUNCATION_WARNING = "Warning: command output truncated; narrow the command or use head/tail/rg."


class TerminalTool(BaseTool):
    # Subclasses (e.g. ReadOnlyTerminalTool used by tabracadabra) override this
    # for tighter budgets when latency matters more than completeness.
    TIMEOUT_SECONDS: float = 120

    def __init__(self, allowed_write_dirs: list[str] | None = None):
        self.allowed_write_dirs = allowed_write_dirs
        super().__init__("bash", "Run a shell command (blocking).",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute"
                    }
                },
                "required": ["command"]
            }
        )

    def _sandbox_config(self):
        if self.allowed_write_dirs is None:
            return None
        allowed = [str(Path(p).expanduser().resolve()) for p in self.allowed_write_dirs]
        tmp_dir = str(Path(tempfile.gettempdir()).resolve())
        if tmp_dir not in allowed:
            allowed.append(tmp_dir)
        return SandboxRuntimeConfig(
            network=NetworkConfig(allowed_domains=[]),
            filesystem=FilesystemConfig(
                allow_write=allowed,
                deny_write=[],
                deny_read=SandboxManager.get_fs_read_config().deny_only or [],
            ),
        )

    async def _wrap_sandbox_async(self, command: str):
        custom_config = self._sandbox_config()
        if custom_config is None:
            return await SandboxManager.wrap_with_sandbox(command)
        return await SandboxManager.wrap_with_sandbox(command, custom_config=custom_config)

    def _wrap_sandbox(self, command: str):
        try:
            return asyncio.run(self._wrap_sandbox_async(command))
        except RuntimeError:
            # Event loop already running (e.g., Playwright active)
            import threading
            result = [None]
            def _run():
                result[0] = asyncio.run(self._wrap_sandbox_async(command))
            t = threading.Thread(target=_run)
            t.start()
            t.join()
            return result[0]

    def _blocked_command_reason(self, command: str) -> str | None:
        compact = re.sub(r"\s+", " ", command.strip())
        if re.search(r"(^|[;&|]\s*)find\s+/(?:\s|$)", compact):
            return (
                "Refusing to run root-wide `find /`. Narrow the search to the "
                "project, logs, or output directory, or use `rg --files <dir> | rg <pattern>`."
            )
        if re.search(r"(^|[;&|]\s*)find\s+(?:~|\$HOME)(?:\s|$)", compact):
            return (
                "Refusing to run home-wide `find`. Narrow the search to an explicit "
                "project/logs path, or use `rg --files <dir> | rg <pattern>`."
            )
        return None

    def _kill_process(self, proc):
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)

    def _append_suffix(self, output: str, suffix: str) -> str:
        full_suffix = ("\n" if output else "") + suffix
        return output[:max(0, OUTPUT_LIMIT - len(full_suffix))] + full_suffix

    def _capture_output(self, proc):
        parts = {"stdout": [], "stderr": []}
        streams = {}
        captured = 0
        truncated = False
        timed_out = False
        deadline = time.monotonic() + self.TIMEOUT_SECONDS

        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if stream is None:
                continue
            fd = stream.fileno()
            os.set_blocking(fd, False)
            streams[fd] = name

        while streams:
            if captured >= OUTPUT_LIMIT:
                if proc.poll() is None:
                    truncated = True
                    self._kill_process(proc)
                    break

            now = time.monotonic()
            if proc.poll() is None and now >= deadline:
                timed_out = True
                self._kill_process(proc)
                break

            timeout = 0 if proc.poll() is not None else min(0.05, max(0, deadline - now))
            ready, _, _ = select.select(list(streams), [], [], timeout)
            if not ready:
                if proc.poll() is not None:
                    break
                continue

            for fd in ready:
                name = streams[fd]
                try:
                    data = os.read(fd, READ_CHUNK_SIZE)
                except BlockingIOError:
                    continue
                if not data:
                    del streams[fd]
                    continue

                remaining = OUTPUT_LIMIT - captured
                if len(data) > remaining:
                    parts[name].append(data[:remaining])
                    captured += remaining
                    truncated = True
                    self._kill_process(proc)
                    break

                parts[name].append(data)
                captured += len(data)

        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._kill_process(proc)
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()

        stdout = b"".join(parts["stdout"]).decode(errors="replace")
        stderr = b"".join(parts["stderr"]).decode(errors="replace")
        return stdout, stderr, truncated, timed_out

    def run(self, command: str):
        blocked_reason = self._blocked_command_reason(command)
        if blocked_reason:
            return blocked_reason

        wrapped = self._wrap_sandbox(command)
        proc = None
        try:
            proc = subprocess.Popen(
                wrapped,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr, truncated, timed_out = self._capture_output(proc)
        except subprocess.TimeoutExpired:
            self._kill_process(proc)
            stdout, stderr, truncated, timed_out = "", "", False, True
        output = stdout
        if stderr:
            output += ("\n" if output else "") + stderr
        output = sanitize_tool_output(output)
        if truncated:
            output = self._append_suffix(output, OUTPUT_TRUNCATION_WARNING)
        elif timed_out:
            output = self._append_suffix(
                output,
                f"(timed out after {self.TIMEOUT_SECONDS}s — narrow the scope and retry)",
            )
        elif output:
            output = output[:OUTPUT_LIMIT]
        return output if output else f"(exit code {proc.returncode})"

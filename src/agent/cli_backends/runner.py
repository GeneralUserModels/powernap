"""Subprocess wrapper for CLI agent backends.

Adapted from bangers/scripts/discovery/process.py::run_command with three
deltas: synthesizes round events from stream-json for Claude (heartbeat for
Codex), honors should_stop() via a watcher thread, and forwards streamed
stdout to an optional on_text callback so the existing event/activity
machinery sees per-turn progress.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, TextIO

from .backend import CliAgentAuthError, CliAgentCancelled, CliAgentError

logger = logging.getLogger(__name__)


def _terminate_group(proc: subprocess.Popen, label: str, grace_s: float = 5.0) -> int:
    """Kill the subprocess group, not just the immediate child.

    Codex / claude commonly fork helper processes (MCP server, sub-agents,
    streaming workers) that inherit the parent's stdout/stderr pipe. SIGTERM
    on the immediate child alone leaves those helpers alive — they keep the
    write end of the pipe open, so the reader threads in the parent never
    EOF and `proc.wait()` + `Thread.join()` block forever. start_new_session
    + killpg on the group ensures the whole tree dies together.
    """
    pgid: int | None
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError) as exc:
        logger.warning("cli[%s] could not getpgid(%d): %s", label, proc.pid, exc)
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
            logger.info("cli[%s] SIGTERM sent to process group %d", label, pgid)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning("cli[%s] killpg(SIGTERM, %d) failed: %s; falling back to proc.terminate()",
                           label, pgid, exc)
            proc.terminate()
    else:
        proc.terminate()

    try:
        rc = proc.wait(timeout=grace_s)
        return rc
    except subprocess.TimeoutExpired:
        logger.warning("cli[%s] process group did not exit within %.1fs; sending SIGKILL", label, grace_s)
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        else:
            proc.kill()
        return proc.wait()


def _close_streams(proc: subprocess.Popen, label: str) -> None:
    """Force-close the parent's read ends after the child group is dead.

    Belt-and-suspenders against a reader thread still blocked on
    `pipe.readline()` — even if the kernel hasn't propagated EOF yet,
    closing the file object inside the parent process raises in the
    reader and lets it exit.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(proc, name, None)
        if stream is None:
            continue
        try:
            stream.close()
        except Exception as exc:
            logger.debug("cli[%s] close(%s) raised: %s", label, name, exc)

# stderr substrings that mean "you need to log in". Best-effort — both CLIs
# evolve their wording; treat as a heuristic, not a contract.
_AUTH_NEEDLES = (
    "not logged in",
    "not signed in",
    "please log in",
    "please login",
    "please sign in",
    "authentication required",
    "invalid api key",
    "authentication_error",
    "unauthorized",
    "credit balance is too low",
    "credentials not found",
    "missing api key",
)


def _stream_pipe(
    pipe: TextIO,
    log_file: TextIO,
    console_prefix: str,
    on_line: Callable[[str], None] | None,
    auth_flag: list[bool],
    label: str,
    stream_name: str,
) -> None:
    line_count = 0
    try:
        try:
            for line in iter(pipe.readline, ""):
                line_count += 1
                log_file.write(line)
                log_file.flush()
                sys.stderr.write(f"{console_prefix}{line}")
                sys.stderr.flush()
                lower = line.lower()
                if any(needle in lower for needle in _AUTH_NEEDLES):
                    auth_flag[0] = True
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        pass
        except ValueError:
            # Raised by readline() when the parent force-closes the pipe to
            # unblock us after killpg — that's the intended exit path.
            pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass
        logger.info("cli[%s] %s pipe closed after %d lines", label, stream_name, line_count)


def _make_round_adapter(
    on_round: Callable[[int, int], None] | None,
    max_turns: int,
    is_claude_stream: bool,
) -> Callable[[str], None] | None:
    """Return a per-line callback that emits on_round(turn, max) events.

    For Claude stream-json: parses lines as JSON and increments a turn counter
    on each line that looks like a model message. Fallback for Codex (no
    --json): the watcher thread emits heartbeats instead (see run_cli_agent).
    """
    if on_round is None or not is_claude_stream:
        return None

    counter = {"turn": 0}

    def callback(line: str) -> None:
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            return
        try:
            obj = json.loads(stripped)
        except Exception:
            return
        # Treat any object with a "type" of assistant/message as one turn.
        msg_type = obj.get("type") or obj.get("event") or ""
        if msg_type in {"assistant", "message", "result"} or "message" in obj:
            counter["turn"] += 1
            try:
                on_round(counter["turn"], max_turns)
            except Exception:
                pass

    return callback


def _all_outputs_ready(paths: list[Path] | None) -> bool:
    """True iff every path in `paths` exists and is non-empty.

    Used by the watcher to detect "the agent already wrote what we asked for"
    so we can terminate processes that don't exit on their own (codex without
    `-o`, claude that decides to keep verifying after writing, etc.).
    """
    if not paths:
        return False
    for p in paths:
        try:
            if not p.is_file() or p.stat().st_size == 0:
                return False
        except OSError:
            return False
    return True


def run_cli_agent(
    *,
    command: list[str],
    cwd: Path,
    prompt: str,
    stdout_log: Path,
    stderr_log: Path,
    env: dict[str, str] | None = None,
    label: str,
    on_round: Callable[[int, int], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    expected_outputs: list[Path] | None = None,
    outputs_ready_check: Callable[[], bool] | None = None,
    max_turns: int = 30,
    is_claude_stream: bool = False,
    heartbeat_s: float = 5.0,
    timeout_s: float | None = None,
    output_ready_grace_s: float = 10.0,
) -> int:
    """Spawn the CLI; stream output; raise on non-zero exit or missing outputs.

    Returns the subprocess return code on success (always 0 if no exception
    raised — non-zero exits raise CliAgentError).
    """
    binary = command[0]
    if shutil.which(binary, path=(env or os.environ).get("PATH")) is None:
        raise CliAgentError(
            f"{binary} CLI not installed; install via Settings → Agent Backend"
        )

    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)

    cwd.mkdir(parents=True, exist_ok=True)

    auth_flag = [False]
    round_cb = _make_round_adapter(on_round, max_turns, is_claude_stream)

    def stdout_handler(line: str) -> None:
        if round_cb is not None:
            round_cb(line)
        if on_text is not None:
            try:
                on_text(line)
            except Exception:
                pass

    with stdout_log.open("w", encoding="utf-8") as stdout_file, stderr_log.open(
        "w", encoding="utf-8"
    ) as stderr_file:
        # Tee key runner events into the per-run stderr_log too. The Python
        # logger already routes to the parent process via worker stdout, but
        # writing copies here means the runtime story sits right next to the
        # codex/claude output that produced it.
        def _runner_event(msg: str, *args) -> None:
            text = msg % args if args else msg
            logger.info("cli[%s] %s", label, text)
            try:
                stderr_file.write(f"[runner {label}] {text}\n")
                stderr_file.flush()
            except Exception:
                pass

        _runner_event(
            "spawning %s in %s (stdout=%s, stderr=%s, expected_outputs=%s)",
            binary, cwd, stdout_log, stderr_log,
            [str(p) for p in (expected_outputs or [])],
        )
        # start_new_session=True puts codex in its own process group so SIGTERM
        # via killpg also reaps the MCP server / sub-agents / streaming workers
        # that would otherwise inherit the read end of our pipes and block the
        # readline() in _stream_pipe forever.
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        _runner_event("subprocess started, pid=%d, pgid=%d", proc.pid, os.getpgid(proc.pid))

        stdout_thread = threading.Thread(
            target=_stream_pipe,
            args=(proc.stdout, stdout_file, f"[{label} out] ", stdout_handler, auth_flag, label, "stdout"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_stream_pipe,
            args=(proc.stderr, stderr_file, f"[{label} err] ", None, auth_flag, label, "stderr"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            proc.stdin.write(prompt)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

        # Watcher: handle should_stop, timeout, and "expected outputs already
        # landed but the agent keeps running." Also emits a heartbeat round
        # event for non-streaming backends so the UI doesn't go silent.
        started = time.time()
        last_beat = started
        last_status_log = started
        cancelled = False
        timed_out = False
        outputs_ready_at: float | None = None
        outputs_force_killed = False

        while True:
            try:
                rc = proc.wait(timeout=0.5)
                _runner_event("subprocess exited naturally with rc=%d after %.1fs", rc, time.time() - started)
                break
            except subprocess.TimeoutExpired:
                pass

            now = time.time()
            if should_stop is not None and should_stop():
                cancelled = True
                _runner_event("should_stop() returned True after %.1fs; terminating group", now - started)
                rc = _terminate_group(proc, label)
                break

            if timeout_s is not None and (now - started) > timeout_s:
                timed_out = True
                _runner_event("timeout after %.1fs (limit %.1fs); terminating group", now - started, timeout_s)
                rc = _terminate_group(proc, label)
                break

            # Belt-and-suspenders for stages that don't use codex --output-schema
            # + -o (e.g. execute writes files directly; some CLIs decide to do
            # post-write verification). Once every expected output exists and
            # is non-empty, give the process a grace period to exit on its own,
            # then SIGTERM the whole group. Treat the early termination as a
            # clean exit since the work product is on disk.
            # If a content-aware predicate is provided, it owns the "ready"
            # decision (e.g. execute stage holds off while app.js is still a
            # verbatim template copy). Otherwise fall back to existence-only.
            ready_now = (
                outputs_ready_check() if outputs_ready_check is not None
                else (bool(expected_outputs) and _all_outputs_ready(expected_outputs))
            )
            if ready_now:
                if outputs_ready_at is None:
                    outputs_ready_at = now
                    _runner_event("expected outputs landed after %.1fs; %.1fs grace before SIGTERM",
                                  now - started, output_ready_grace_s)
                elif (now - outputs_ready_at) >= output_ready_grace_s:
                    outputs_force_killed = True
                    rc = _terminate_group(proc, label)
                    _runner_event("subprocess group reaped after force-kill, rc=%d", rc)
                    break
            elif outputs_ready_at is not None:
                # Predicate flipped back to not-ready (e.g. agent reverted a
                # file). Reset the grace timer so we don't kill prematurely.
                outputs_ready_at = None

            # Periodic status log so a hang inside the watch loop is visible.
            if (now - last_status_log) >= 30.0:
                last_status_log = now
                ready_state = "ready" if ready_now else "waiting"
                _runner_event("watch tick: %.1fs elapsed, outputs=%s", now - started, ready_state)

            # Heartbeat round event when stream parsing isn't available.
            if (
                on_round is not None
                and round_cb is None
                and (now - last_beat) >= heartbeat_s
            ):
                last_beat = now
                try:
                    on_round(1, max_turns)
                except Exception:
                    pass

        # Force-close the parent's read ends so any reader thread still blocked
        # on readline (because a helper kept the write end alive past killpg)
        # gets an immediate ValueError and exits.
        _close_streams(proc, label)

        _runner_event("joining stdout pipe thread")
        stdout_thread.join(timeout=5.0)
        if stdout_thread.is_alive():
            _runner_event("WARNING: stdout pipe thread still alive after 5s; leaking it (daemon)")
        _runner_event("joining stderr pipe thread")
        stderr_thread.join(timeout=5.0)
        if stderr_thread.is_alive():
            _runner_event("WARNING: stderr pipe thread still alive after 5s; leaking it (daemon)")
        _runner_event("pipe threads joined")

    if cancelled:
        raise CliAgentCancelled(f"{label}: cancelled by caller")

    if timed_out:
        raise CliAgentError(
            f"{label}: timed out after {timeout_s:.0f}s (see {stderr_log})"
        )

    # If we force-killed after outputs landed, the process exits non-zero by
    # signal; treat as success since the deliverable is on disk.
    if rc != 0 and not outputs_force_killed:
        # Distinguish auth failures so the UI can route the user to the
        # Sign-in button instead of a generic error toast.
        if auth_flag[0]:
            raise CliAgentAuthError(
                f"{label}: {binary} reports it is not authenticated "
                f"(rc={rc}; see {stderr_log})"
            )
        raise CliAgentError(
            f"{label}: {binary} exited with code {rc} (see {stderr_log})"
        )

    if expected_outputs:
        missing = [p for p in expected_outputs if not p.exists()]
        if missing:
            paths = ", ".join(str(p) for p in missing)
            raise CliAgentError(
                f"{label}: {binary} exited 0 but expected output(s) missing: {paths} "
                f"(see {stdout_log})"
            )

    final_rc = 0 if outputs_force_killed else rc
    logger.info("cli[%s] run_cli_agent returning rc=%d (outputs_force_killed=%s)",
                label, final_rc, outputs_force_killed)
    return final_rc

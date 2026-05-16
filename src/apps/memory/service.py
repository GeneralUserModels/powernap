"""Memory wiki service: periodically ingests logs and lints the wiki."""

from __future__ import annotations

import argparse
import os
import asyncio
import logging
from pathlib import Path

from server.feature_flags import is_enabled
from agent.builder import _ensure_sandbox_async
from apps.moments.runtime.scheduler import scheduled_service_due
from server.cost_tracker import init_cost_tracking
from server.config import DEFAULT_AGENT_MODEL
from server.process_jobs import relay_worker_event

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 60  # seconds between schedule checks
SUCCESSFUL_RUN_MARKER = ".last_successful_run"

class MemoryIngest:
    """Ingests new activity logs into the personal knowledge wiki."""

    def __init__(
        self,
        logs_dir: str,
        model: str,
        api_key: str | None = None,
        subagent_model: str | None = None,
        subagent_api_key: str | None = None,
    ):
        self.logs_dir = str(Path(logs_dir).resolve())
        self.model = model
        self.api_key = api_key
        self.subagent_model = subagent_model
        self.subagent_api_key = subagent_api_key

    def run(self, on_round=None) -> str:
        """Read new logs and update wiki pages. Blocking."""
        from apps.memory.ingest import run as ingest_run
        return ingest_run(
            self.logs_dir, model=self.model, api_key=self.api_key, on_round=on_round,
            subagent_model=self.subagent_model, subagent_api_key=self.subagent_api_key,
        )


class MemoryLint:
    """Audits and maintains wiki health."""

    def __init__(
        self,
        logs_dir: str,
        model: str,
        api_key: str | None = None,
        subagent_model: str | None = None,
        subagent_api_key: str | None = None,
    ):
        self.logs_dir = str(Path(logs_dir).resolve())
        self.model = model
        self.api_key = api_key
        self.subagent_model = subagent_model
        self.subagent_api_key = subagent_api_key

    def run(self, on_round=None) -> str:
        """Lint the wiki. Blocking."""
        from apps.memory.lint import run as lint_run
        return lint_run(
            self.logs_dir, model=self.model, api_key=self.api_key, on_round=on_round,
            subagent_model=self.subagent_model, subagent_api_key=self.subagent_api_key,
        )


def successful_run_marker(logs_dir: str | Path) -> Path:
    return Path(logs_dir).resolve() / "memory" / SUCCESSFUL_RUN_MARKER


async def run_memory_pipeline_once(state) -> bool:
    """Run the memory ingest/lint worker once."""
    logs_dir = str(Path(state.config.log_dir).resolve())
    cfg = state.config
    model = cfg.memory_agent_model
    api_key = cfg.memory_agent_api_key or cfg.resolve_api_key("agent_api_key")
    subagent_model = cfg.subagent_model or None
    subagent_api_key = cfg.resolve_api_key("subagent_api_key") if cfg.subagent_model else None

    try:
        logger.info("Memory: running worker pipeline")
        result = await state.background_job_runner.run(
            "memory.pipeline",
            {
                "logs_dir": logs_dir,
                "model": model,
                "api_key": api_key,
                "subagent_model": subagent_model,
                "subagent_api_key": subagent_api_key,
            },
            on_event=lambda event: relay_worker_event(state, event),
        )
        if result.get("success"):
            logger.info("Memory: worker pipeline complete")
            successful_run_marker(logs_dir).write_text("")
            await state.broadcast("memory_updated", {})
            return True
        logger.warning("Memory: worker pipeline returned unsuccessful result: %s", result)
        return False
    finally:
        await state.broadcast_activity("memory")


async def run_memory_service(state) -> None:
    """Background task: poll every SCAN_INTERVAL and run ingest whenever the
    most recent scheduled occurrence hasn't completed yet. Lint runs as part
    of the same memory feature run.
    """

    logger.info("Memory wiki service started")

    logs_dir = str(Path(state.config.log_dir).resolve())
    run_checkpoint_file = Path(logs_dir) / "memory" / ".last_run"

    # Keep the server-side sandbox ready for lightweight service setup; heavy
    # agent work initializes its own sandbox inside the background worker.
    await _ensure_sandbox_async([logs_dir])

    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL)

            if not (is_enabled(state.config, "memory") and state.config.memory_enabled):
                continue

            schedule = getattr(state.config, "memory_schedule", "daily at 3am")
            if not scheduled_service_due(schedule, run_checkpoint_file):
                continue

            await run_memory_pipeline_once(state)

        except asyncio.CancelledError:
            logger.info("Memory wiki service stopped")
            return
        except Exception:
            logger.exception("Memory wiki service error")
            await asyncio.sleep(300)


def main():
    """Run ingest (and optionally lint) once from the command line."""

    parser = argparse.ArgumentParser(description="Run memory wiki pipeline")
    parser.add_argument("--logs-dir", default=os.getenv("TADA_LOG_DIR", "./logs"),
                        help="Path to logs directory (default: $TADA_LOG_DIR or ./logs)")
    parser.add_argument("--model", default=os.getenv("TADA_AGENT_MODEL", DEFAULT_AGENT_MODEL),
                        help="Model to use")
    parser.add_argument("--api-key", default=os.getenv("ANTHROPIC_API_KEY", ""),
                        help="API key (default: $ANTHROPIC_API_KEY)")
    parser.add_argument("--lint", action="store_true", help="Also run lint after ingest")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    tracker = init_cost_tracking()

    logs_dir = str(Path(args.logs_dir).resolve())
    api_key = args.api_key or None

    logger.info("Memory: running ingest in %s", logs_dir)
    MemoryIngest(logs_dir, args.model, api_key).run()
    logger.info("Memory: ingest complete")

    if args.lint:
        logger.info("Memory: running lint")
        MemoryLint(logs_dir, args.model, api_key).run()
        logger.info("Memory: lint complete")

    snapshot, elapsed = tracker.snapshot()
    total_cost = sum(s["cost"] for s in snapshot.values())
    total_tokens = sum(s["input_tokens"] + s["output_tokens"] for s in snapshot.values())
    logger.info("[cost] memory pipeline — $%.4f total, %d tokens, %.0fs", total_cost, total_tokens, elapsed)


if __name__ == "__main__":
    main()

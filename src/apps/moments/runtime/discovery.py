"""Discovery service: periodically finds new moments from activity logs."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from server.feature_flags import is_enabled
from server.process_jobs import relay_worker_event

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 60  # seconds between schedule checks


class _DiscoveryBase:
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


class MomentsDiscovery(_DiscoveryBase):
    """Discovers candidate moments from activity logs."""

    def run(self, *, write_run_checkpoint: bool = True) -> str:
        """Analyze logs and write task files. Blocking."""
        from apps.moments.steps.discover import run as moments_run
        return moments_run(
            self.logs_dir, model=self.model, api_key=self.api_key,
            subagent_model=self.subagent_model, subagent_api_key=self.subagent_api_key,
            write_run_checkpoint=write_run_checkpoint,
        )


class TaskFilter(_DiscoveryBase):
    """Promotes discovered candidates into logs-tada/."""

    def run(self, *, write_run_checkpoint: bool = True) -> str:
        """Promote candidate moments through tada. Blocking."""
        from apps.moments.steps.promote import run as filter_run
        return filter_run(
            self.logs_dir, model=self.model, api_key=self.api_key,
            subagent_model=self.subagent_model, subagent_api_key=self.subagent_api_key,
            write_run_checkpoint=write_run_checkpoint,
        )


class TriggersCheck(_DiscoveryBase):
    """Evaluates trigger conditions on existing tada tasks and re-fires matches."""

    def run(self) -> str:
        """Check triggers and mark fired tasks for re-execution. Blocking."""
        from apps.moments.steps.triggers import run as triggers_run
        return triggers_run(
            self.logs_dir, model=self.model, api_key=self.api_key,
            subagent_model=self.subagent_model, subagent_api_key=self.subagent_api_key,
        )


async def run_moments_discovery(state) -> None:
    """Background task: poll every SCAN_INTERVAL and run the discovery pipeline
    whenever the most recent scheduled occurrence hasn't completed yet.

    Polling (instead of one long sleep to the next target) catches up after
    laptop sleep/wake and avoids drift if the schedule is edited at runtime.
    """
    from apps.moments.runtime.scheduler import scheduled_service_due

    logger.info("Moments discovery service started")

    # Keep the server-side sandbox ready for lightweight service setup; heavy
    # agent work initializes its own sandbox inside the background worker.
    from agent.builder import _ensure_sandbox_async
    logs_dir = str(Path(state.config.log_dir).resolve())
    tada_path = Path(state.config.tada_dir).resolve()
    tada_dir = str(tada_path)
    await _ensure_sandbox_async([tada_dir])

    run_checkpoint = tada_path / ".last_run"

    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL)

            if not (is_enabled(state.config, "moments") and state.config.moments_enabled):
                continue

            schedule = getattr(state.config, "moments_discovery_schedule", "daily at 2am")
            if not scheduled_service_due(schedule, run_checkpoint):
                continue

            cfg = state.config
            model = cfg.moments_agent_model
            api_key = cfg.resolve_api_key("moments_agent_api_key")
            subagent_model = cfg.subagent_model or None
            subagent_api_key = cfg.resolve_api_key("subagent_api_key") if cfg.subagent_model else None

            try:
                logger.info("Discovery: running worker pipeline")
                result = await state.background_job_runner.run(
                    "moments.discovery",
                    {
                        "logs_dir": logs_dir,
                        "model": model,
                        "api_key": api_key,
                        "subagent_model": subagent_model,
                        "subagent_api_key": subagent_api_key,
                        "run_checkpoint": str(run_checkpoint),
                    },
                    on_event=lambda event: relay_worker_event(state, event),
                )
                if result.get("success"):
                    summaries = result.get("summaries") or {}
                    logger.info("Discovery pipeline complete:\n%s", summaries)
                else:
                    logger.warning("Discovery worker returned unsuccessful result: %s", result)
            finally:
                await state.broadcast_activity("moments_discovery")

        except asyncio.CancelledError:
            logger.info("Moments discovery service stopped")
            return
        except Exception:
            logger.exception("Moments discovery error")
            await asyncio.sleep(300)

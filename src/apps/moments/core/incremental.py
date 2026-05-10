"""Helpers for incremental discovery checkpoint I/O."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

CHECKPOINT_TIME_FMT = "%Y-%m-%dT%H:%M:%S"
DEFAULT_MISSING_CHECKPOINT_AGE = timedelta(days=1)


def read_checkpoint(checkpoint_path: Path, *, default_age: timedelta | None = None) -> datetime | None:
    """Read an incremental timestamp, optionally seeding missing checkpoints."""
    if not checkpoint_path.exists():
        if default_age is None:
            return None
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        default_time = datetime.now() - default_age
        checkpoint_path.write_text(default_time.strftime(CHECKPOINT_TIME_FMT) + "\n")
        return default_time
    text = checkpoint_path.read_text().strip()
    if not text:
        if default_age is None:
            return None
        default_time = datetime.now() - default_age
        checkpoint_path.write_text(default_time.strftime(CHECKPOINT_TIME_FMT) + "\n")
        return default_time
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def write_checkpoint(checkpoint_path: Path) -> None:
    """Write the current time to the checkpoint file."""
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(datetime.now().strftime(CHECKPOINT_TIME_FMT) + "\n")

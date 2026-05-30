"""Small helpers for bounded connector backlogs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import os
import queue
from logging import Logger
from typing import Any


TIMESTAMP_FIELDS = ("timestamp", "date", "delivered_date", "start")


@dataclass(frozen=True)
class TrimResult:
    items: list[Any]
    dropped: int
    used_timestamps: bool


def bounded_int_from_env(*names: str, default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except ValueError:
            continue
    return max(0, int(default))


class DropCounter:
    def __init__(self, log_every: int = 25) -> None:
        self.count = 0
        self.log_every = max(1, log_every)

    def add(self, dropped: int, logger: Logger, label: str, max_items: int) -> None:
        if dropped <= 0:
            return
        before = self.count
        self.count += dropped
        if before == 0 or (before // self.log_every) != (self.count // self.log_every):
            logger.warning(
                "%s full (max=%s); dropped %d oldest item(s) total, kept newest",
                label,
                max_items,
                self.count,
            )


def trim_list_latest(items: list[Any], max_items: int, logger: Logger, label: str, counter: DropCounter) -> int:
    if max_items <= 0 or len(items) <= max_items:
        return 0
    dropped = len(items) - max_items
    del items[:dropped]
    counter.add(dropped, logger, label, max_items)
    return dropped


def _coerce_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("timestamp", "dateTime", "date", "time", "value"):
            parsed = _coerce_timestamp(value.get(key))
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        try:
            return parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return None


def item_timestamp(item: Any) -> float | None:
    if not isinstance(item, dict):
        return None
    for field in TIMESTAMP_FIELDS:
        parsed = _coerce_timestamp(item.get(field))
        if parsed is not None:
            return parsed
    return None


def trim_items_latest_by_timestamp(items: list[Any], max_items: int) -> TrimResult:
    if max_items <= 0 or len(items) <= max_items:
        return TrimResult(items=list(items), dropped=0, used_timestamps=False)

    dropped = len(items) - max_items
    indexed = [(idx, item, item_timestamp(item)) for idx, item in enumerate(items)]
    if not any(ts is not None for _idx, _item, ts in indexed):
        return TrimResult(items=list(items[-max_items:]), dropped=dropped, used_timestamps=False)

    ranked = sorted(
        indexed,
        key=lambda row: (
            row[2] is not None,
            row[2] if row[2] is not None else float("-inf"),
            row[0],
        ),
    )
    keep_indices = {idx for idx, _item, _ts in ranked[-max_items:]}
    kept = [item for idx, item, _ts in indexed if idx in keep_indices]
    return TrimResult(items=kept, dropped=dropped, used_timestamps=True)


def put_latest_queue(q: queue.Queue, item: Any, logger: Logger, label: str, counter: DropCounter) -> None:
    if q.maxsize <= 0:
        q.put_nowait(item)
        return
    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass

    try:
        q.get_nowait()
        counter.add(1, logger, label, q.maxsize)
    except queue.Empty:
        pass

    try:
        q.put_nowait(item)
    except queue.Full:
        counter.add(1, logger, label, q.maxsize)


def put_latest_async_queue(q: asyncio.Queue, item: Any, logger: Logger, label: str, counter: DropCounter) -> None:
    if q.maxsize <= 0:
        q.put_nowait(item)
        return
    try:
        q.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass

    try:
        q.get_nowait()
        counter.add(1, logger, label, q.maxsize)
    except asyncio.QueueEmpty:
        pass

    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        counter.add(1, logger, label, q.maxsize)

from __future__ import annotations

import asyncio
import queue
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from connectors._bounded import (
    DropCounter,
    put_latest_async_queue,
    put_latest_queue,
    trim_items_latest_by_timestamp,
)


class _Logger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, tuple]] = []

    def warning(self, msg: str, *args) -> None:
        self.messages.append((msg, args))


class BoundedConnectorBacklogTests(unittest.TestCase):
    def test_trim_items_latest_uses_ascending_timestamps(self):
        items = [{"id": "old", "timestamp": 1}, {"id": "mid", "timestamp": 2}, {"id": "new", "timestamp": 3}]

        result = trim_items_latest_by_timestamp(items, 2)

        self.assertTrue(result.used_timestamps)
        self.assertEqual(result.dropped, 1)
        self.assertEqual([item["id"] for item in result.items], ["mid", "new"])

    def test_trim_items_latest_uses_descending_timestamps(self):
        items = [{"id": "new", "timestamp": 3}, {"id": "mid", "timestamp": 2}, {"id": "old", "timestamp": 1}]

        result = trim_items_latest_by_timestamp(items, 2)

        self.assertTrue(result.used_timestamps)
        self.assertEqual([item["id"] for item in result.items], ["new", "mid"])

    def test_trim_items_latest_falls_back_to_tail_without_timestamps(self):
        items = [{"id": "old"}, {"id": "mid"}, {"id": "new"}]

        result = trim_items_latest_by_timestamp(items, 2)

        self.assertFalse(result.used_timestamps)
        self.assertEqual([item["id"] for item in result.items], ["mid", "new"])

    def test_trim_items_latest_handles_mixed_timestamp_formats(self):
        items = [
            {"id": "old", "timestamp": "1704067200"},
            {"id": "rfc", "date": "Tue, 02 Jan 2024 00:00:00 GMT"},
            {"id": "iso", "start": {"dateTime": "2024-01-03T00:00:00+00:00"}},
            {"id": "new", "delivered_date": 1704326400},
        ]

        result = trim_items_latest_by_timestamp(items, 2)

        self.assertTrue(result.used_timestamps)
        self.assertEqual([item["id"] for item in result.items], ["iso", "new"])

    def test_queue_overflow_drops_oldest_and_keeps_newest(self):
        logger = _Logger()
        q: queue.Queue[str] = queue.Queue(maxsize=2)
        counter = DropCounter()

        put_latest_queue(q, "old", logger, "test queue", counter)
        put_latest_queue(q, "mid", logger, "test queue", counter)
        put_latest_queue(q, "new", logger, "test queue", counter)

        self.assertEqual([q.get_nowait(), q.get_nowait()], ["mid", "new"])
        self.assertIn("kept newest", logger.messages[0][0])

    def test_async_queue_overflow_drops_oldest_and_keeps_newest(self):
        logger = _Logger()
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        counter = DropCounter()

        put_latest_async_queue(q, "old", logger, "test async queue", counter)
        put_latest_async_queue(q, "mid", logger, "test async queue", counter)
        put_latest_async_queue(q, "new", logger, "test async queue", counter)

        self.assertEqual([q.get_nowait(), q.get_nowait()], ["mid", "new"])
        self.assertIn("kept newest", logger.messages[0][0])

    def test_filesystem_event_backlog_keeps_latest_events(self):
        from connectors.filesystem import server as fs_server

        fs_server._events.clear()
        fs_server._notify_event = None
        fs_server._loop = None
        handler = fs_server._Handler()
        events = [
            SimpleNamespace(is_directory=False, event_type="modified", src_path=f"/tmp/file_{idx}")
            for idx in range(3)
        ]

        with patch.object(fs_server, "EVENT_BACKLOG_MAX", 2):
            for event in events:
                handler.on_any_event(event)

        self.assertEqual([event["path"] for event in fs_server._events], ["/tmp/file_1", "/tmp/file_2"])
        fs_server._events.clear()

    def test_audio_leftover_stream_backlog_keeps_latest_streams(self):
        from connectors.audio import server as audio_server

        audio_server._leftover_streams.clear()
        with patch.object(audio_server, "LEFTOVER_STREAMS_MAX", 2):
            audio_server._append_leftover_stream("old")
            audio_server._append_leftover_stream("mid")
            audio_server._append_leftover_stream("new")

        self.assertEqual(audio_server._leftover_streams, ["mid", "new"])
        audio_server._leftover_streams.clear()

    def test_screen_recorder_backlog_keeps_latest_aggregations(self):
        from connectors.screen.napsack.recorder import OnlineRecorder

        recorder = object.__new__(OnlineRecorder)
        recorder.aggregation_queue = queue.Queue(maxsize=2)
        recorder._aggregation_drop_counter = DropCounter()

        recorder._put_aggregation("old")
        recorder._put_aggregation("mid")
        recorder._put_aggregation("new")

        self.assertEqual(
            [recorder.aggregation_queue.get_nowait(), recorder.aggregation_queue.get_nowait()],
            ["mid", "new"],
        )


if __name__ == "__main__":
    unittest.main()

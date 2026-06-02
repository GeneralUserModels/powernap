"""Microphone audio recorder using sounddevice."""

import logging
import threading

import numpy as np
import sounddevice as sd

from connectors._bounded import DropCounter, bounded_int_from_env

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
MAX_BUFFER_SECONDS = bounded_int_from_env(
    "TADA_AUDIO_RECORDER_BUFFER_SECONDS_MAX",
    "TADA_CONNECTOR_AUDIO_BUFFER_SECONDS_MAX",
    default=300,
)


class MicRecorder:
    """Records microphone audio into a buffer that can be drained by the mixer."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._buffer: list[np.ndarray] = []
        self._buffer_samples = 0
        self._max_buffer_samples = self.sample_rate * MAX_BUFFER_SECONDS if MAX_BUFFER_SECONDS > 0 else 0
        self._drop_counter = DropCounter(log_every=10)
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        logger.info("Microphone recorder started (rate=%d)", self.sample_rate)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("Microphone recorder stopped")

    def read_and_clear(self) -> np.ndarray | None:
        """Return accumulated samples and reset the buffer. Returns None if empty."""
        with self._lock:
            if not self._buffer:
                return None
            data = np.concatenate(self._buffer)
            self._buffer.clear()
            self._buffer_samples = 0
        return data

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            logger.warning("mic: %s", status)
        chunk = indata[:, 0].copy()
        with self._lock:
            self._buffer.append(chunk)
            self._buffer_samples += len(chunk)
            dropped = 0
            while (
                self._max_buffer_samples > 0
                and self._buffer_samples > self._max_buffer_samples
                and self._buffer
            ):
                old = self._buffer.pop(0)
                self._buffer_samples -= len(old)
                dropped += 1
            if dropped:
                self._drop_counter.add(
                    dropped,
                    logger,
                    "mic: audio sample backlog",
                    self._max_buffer_samples,
                )

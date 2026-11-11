"""Decide when the speech heard so far should be pushed into the conversation.

Every push wakes the model, so the policy trades latency against noise:

``live``   Push at a natural pause, a couple of seconds after the talking
           stops. If pushes start coming thick and fast (a meeting, a phone
           call) space them out, and cut a monologue into chunks rather than
           holding it forever.
``notes``  Push one digest per interval, for when the user wants a record of
           what was said rather than a conversation.

Standard library only, and no clock of its own unless you ask: tests drive it
with a fake one.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Utterance:
    text: str
    started_at: float  # wall-clock seconds
    ended_at: float
    language: str = ""  # ISO code, empty when unknown
    added_at: float = 0.0  # batcher clock when it arrived


class Batcher:
    def __init__(self, cfg: dict, clock=time.monotonic) -> None:
        self.mode = str(cfg["mode"])
        self.pause_s = float(cfg["pause_s"])
        self.min_interval_s = float(cfg["min_interval_s"])
        self.max_wait_s = float(cfg["max_wait_s"])
        self.busy_events = int(cfg["busy_events"])
        self.busy_interval_s = float(cfg["busy_interval_s"])
        self.notes_interval_s = float(cfg["notes_interval_s"])
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: list[Utterance] = []
        self._flushes: deque[float] = deque()
        self._last_flush = float("-inf")

    def add(self, utterance: Utterance) -> None:
        with self._lock:
            utterance.added_at = self._clock()
            self._pending.append(utterance)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)

    def spacing(self) -> float:
        """Least time between two live pushes right now."""
        with self._lock:
            return self._spacing(self._clock())

    def _spacing(self, now: float) -> float:
        while self._flushes and now - self._flushes[0] > 60.0:
            self._flushes.popleft()
        if len(self._flushes) >= self.busy_events:
            return self.busy_interval_s
        return self.min_interval_s

    def due(self, speech_active: bool, stt_busy: bool, quiet_for: float) -> bool:
        """Should the pending utterances be pushed now?

        ``speech_active``: someone is mid-utterance. ``stt_busy``: audio is
        still being transcribed, so more text is on its way. ``quiet_for``:
        seconds since the microphone last heard a voice.
        """
        now = self._clock()
        with self._lock:
            if not self._pending:
                return False
            oldest = now - self._pending[0].added_at
            more_coming = speech_active or stt_busy

            if self.mode == "notes":
                if oldest < self.notes_interval_s:
                    return False
                # Prefer to cut the digest at a pause, but do not hold it long.
                return not more_coming or oldest >= self.notes_interval_s * 1.5

            if now - self._last_flush < self._spacing(now):
                return False
            if oldest >= self.max_wait_s:
                return True
            if more_coming:
                return False
            return quiet_for >= self.pause_s

    def take(self) -> list[Utterance]:
        """Hand over everything pending and start the next batch."""
        now = self._clock()
        with self._lock:
            items, self._pending = self._pending, []
            if items:
                self._last_flush = now
                self._flushes.append(now)
            return items

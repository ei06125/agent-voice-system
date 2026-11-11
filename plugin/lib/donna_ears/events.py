"""Format utterances as monitor events and write them at a rate the harness accepts.

A Claude Code monitor turns the process's stdout into notifications, with rules
this module is built around (read from Claude Code 2.1.322):

* Lines written within 200 ms of each other become ONE notification.
* A line is cut at 500 characters and a notification at 3000.
* Notifications are metered by a token bucket: 10 to start, one back every
  2 s. Anything over that is dropped, not queued.

So one batch of speech is written in a single burst, long text is wrapped and
split below the limits, and a local copy of the bucket makes us wait rather
than lose words.

Standard library only.
"""

from __future__ import annotations

import threading
import time

from .batcher import Utterance

MAX_LINE = 460
MAX_EVENT = 2600
ERROR_PREFIX = "[ears error] "
NOTICE_PREFIX = "[ears] "


def stamp(wall: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(wall))


def _wrap(text: str, width: int) -> list[str]:
    """Split at spaces into pieces of at most ``width`` characters."""
    pieces: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > width:  # one absurdly long token: hard split
            if current:
                pieces.append(current)
                current = ""
            pieces.append(word[:width])
            word = word[width:]
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            pieces.append(current)
            current = word
    if current:
        pieces.append(current)
    return pieces


def lines_for(utterance: Utterance) -> list[str]:
    language = utterance.language
    tag = f" {language}" if language and language != "en" else ""
    head = f"[heard {stamp(utterance.started_at)}{tag}] "
    more = "[heard cont.] "
    width = MAX_LINE - max(len(head), len(more))
    pieces = _wrap(utterance.text, width)
    return [(head if i == 0 else more) + piece for i, piece in enumerate(pieces)]


def render(utterances: list[Utterance]) -> list[str]:
    """One or more events (newline-joined blocks) covering every utterance."""
    events: list[str] = []
    current: list[str] = []
    size = 0
    for utterance in utterances:
        for line in lines_for(utterance):
            if current and size + len(line) + 1 > MAX_EVENT:
                events.append("\n".join(current))
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
    if current:
        events.append("\n".join(current))
    return events


class EventWriter:
    """Serialises events onto a stream without outrunning the harness's meter."""

    CAPACITY = 6  # the harness allows 10; stay well inside
    REFILL_S = 2.2  # the harness returns a token every 2.0 s
    GAP_S = 0.4  # more than the 200 ms window that merges lines

    def __init__(self, stream, clock=time.monotonic, sleep=time.sleep) -> None:
        self._stream = stream
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()  # the STT thread reports errors too
        self._tokens = float(self.CAPACITY)
        self._refilled_at = clock()
        self._last_write = float("-inf")
        self.broken = False  # the reader went away: nobody is listening
        self.count = 0

    def _refill(self) -> None:
        now = self._clock()
        gained = (now - self._refilled_at) / self.REFILL_S
        if gained > 0:
            self._tokens = min(float(self.CAPACITY), self._tokens + gained)
            self._refilled_at = now

    def emit(self, block: str) -> bool:
        """Write one event. Blocks while the meter is empty; False if the pipe is gone."""
        text = "\n".join(line for line in block.splitlines() if line.strip())
        if not text:
            return True
        with self._lock:
            if self.broken:
                return False
            self._refill()
            if self._tokens < 1.0:
                self._sleep((1.0 - self._tokens) * self.REFILL_S)
                self._refill()
            gap = self.GAP_S - (self._clock() - self._last_write)
            if gap > 0:
                self._sleep(gap)
            self._tokens -= 1.0
            try:
                self._stream.write(text + "\n")
                self._stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.broken = True
                return False
            self._last_write = self._clock()
            self.count += 1
            return True

    def error(self, message: str) -> bool:
        return self.emit(ERROR_PREFIX + message)

    def notice(self, message: str) -> bool:
        return self.emit(NOTICE_PREFIX + message)

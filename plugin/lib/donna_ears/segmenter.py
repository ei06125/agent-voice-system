"""Cut a stream of audio frames into utterances, given a speech probability per frame.

The probability comes from Silero VAD, but this module does not know that: it is
a plain state machine over ``(frame, probability)`` pairs so it can be tested
without a model or a microphone.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # the chunk size Silero VAD requires at 16 kHz
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE  # 32 ms


@dataclass
class Segment:
    frames: list  # consecutive audio frames, oldest first
    started_at: float  # wall-clock time of the first frame
    ended_at: float  # wall-clock time just after the last frame
    voiced_seconds: float  # how much of it the detector called speech

    @property
    def duration(self) -> float:
        return len(self.frames) * FRAME_SECONDS


def _frames(milliseconds: float) -> int:
    return max(1, round(milliseconds / 1000 / FRAME_SECONDS))


class Segmenter:
    """Hysteresis on the speech probability, with pre-roll and a length cap.

    Speech starts when the probability reaches ``threshold`` and ends after
    ``min_silence_ms`` of frames below ``threshold - 0.15``. Frames in between
    the two levels neither extend nor end an utterance, which keeps a trailing
    breath or a soft consonant from splitting a sentence.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_silence_ms: float = 700,
        pre_roll_ms: float = 300,
        min_speech_ms: float = 250,
        max_utterance_s: float = 28.0,
        tail_ms: float = 200,
    ) -> None:
        self.threshold = threshold
        self.low = max(0.05, threshold - 0.15)
        self.min_silence = _frames(min_silence_ms)
        self.min_speech = _frames(min_speech_ms)
        self.max_frames = _frames(max_utterance_s * 1000)
        # Silence kept after the last voiced frame, so words are not clipped.
        self.tail = min(_frames(tail_ms), self.min_silence)
        self._pre: deque = deque(maxlen=_frames(pre_roll_ms))
        self._buf: list = []
        self._voiced = 0
        self._silence = 0
        self._started_at = 0.0
        self.in_speech = False

    def reset(self) -> None:
        self._pre.clear()
        self._buf = []
        self._voiced = 0
        self._silence = 0
        self.in_speech = False

    def push(self, frame, probability: float, now: float) -> Segment | None:
        """Feed one frame; return a finished utterance when this frame ends one."""
        if not self.in_speech:
            if probability >= self.threshold:
                self.in_speech = True
                self._buf = list(self._pre) + [frame]
                self._started_at = now - len(self._buf) * FRAME_SECONDS
                self._pre.clear()
                self._voiced = 1
                self._silence = 0
            else:
                self._pre.append(frame)
            return None

        self._buf.append(frame)
        if probability >= self.threshold:
            self._voiced += 1
            self._silence = 0
        elif probability < self.low:
            self._silence += 1

        if self._silence >= self.min_silence:
            return self._finish(now, trim=self._silence - self.tail)
        if len(self._buf) >= self.max_frames:
            # Someone has been talking without a pause. Hand over what we have
            # and keep listening: the next frame starts a new utterance.
            return self._finish(now, trim=0)
        return None

    def flush(self, now: float) -> Segment | None:
        """End the utterance in progress, if any (source closed or paused)."""
        if not self.in_speech:
            return None
        return self._finish(now, trim=max(0, self._silence - self.tail))

    def _finish(self, now: float, trim: int) -> Segment | None:
        frames = self._buf[: len(self._buf) - trim] if trim > 0 else self._buf
        voiced = self._voiced
        ended_at = now - max(0, trim) * FRAME_SECONDS
        started_at = self._started_at
        self.reset()
        if voiced < self.min_speech or not frames:
            return None
        return Segment(
            frames=frames,
            started_at=started_at,
            ended_at=ended_at,
            voiced_seconds=voiced * FRAME_SECONDS,
        )

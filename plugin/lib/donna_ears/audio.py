"""Where the audio comes from: the microphone, or a file standing in for one.

Both sources hand out 512-sample float32 frames at 16 kHz, the unit Silero VAD
works in. The file source exists so the whole pipeline can be exercised, and
tested end to end, without opening a microphone.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import time
import wave

from .segmenter import FRAME_SAMPLES, FRAME_SECONDS, SAMPLE_RATE


class SourceError(RuntimeError):
    pass


class EndOfSource(Exception):
    """A file source has played everything it had."""


def list_inputs() -> list[dict]:
    """Input-capable devices as PortAudio currently sees them."""
    import sounddevice as sd

    try:
        default_index = sd.default.device[0]
    except Exception:  # noqa: BLE001 - backend specific
        default_index = None
    found = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            found.append(
                {
                    "index": index,
                    "name": device["name"],
                    "channels": device["max_input_channels"],
                    "default": index == default_index,
                }
            )
    return found


def probe_default_input() -> str | None:
    """Name of the system default input, asked from a fresh process.

    PortAudio snapshots the device list when it initialises, so a process with
    a stream open cannot see that the default changed (AirPods connecting, a
    headset being unplugged). A new process can.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sounddevice as sd; print(sd.query_devices(kind='input')['name'])",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return name or None


class MicSource:
    is_microphone = True

    def __init__(self, hint: str = "") -> None:
        self.hint = hint
        self.follows_default = not hint.strip()
        self.name = ""
        self.dropped = 0
        self._stream = None
        self._queue: queue.Queue = queue.Queue(maxsize=int(10 / FRAME_SECONDS))

    def open(self) -> None:
        import sounddevice as sd

        # Re-read the device list: the previous one may predate a device change.
        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001 - private API, best effort
            pass
        index = None
        name = ""
        devices = sd.query_devices()
        for fragment in (part.strip().lower() for part in self.hint.split(",")):
            if not fragment:
                continue
            for candidate, device in enumerate(devices):
                if device["max_input_channels"] > 0 and fragment in device["name"].lower():
                    index, name = candidate, device["name"]
                    break
            if index is not None:
                break
        if index is None:
            try:
                name = sd.query_devices(kind="input")["name"]
            except Exception as exc:  # noqa: BLE001
                raise SourceError(f"no audio input device available ({exc})") from exc
        self._queue = queue.Queue(maxsize=int(10 / FRAME_SECONDS))
        try:
            self._stream = sd.InputStream(
                device=index,
                channels=1,
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SAMPLES,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudioError and friends
            self._stream = None
            raise SourceError(f"cannot open input device {name!r}: {exc}") from exc
        self.name = name

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            self.dropped += 1

    def read(self, timeout: float = 0.25):
        """Next frame, or None when none arrived within ``timeout``."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass


class FileSource:
    """Plays an audio file as if it were being spoken into the microphone."""

    is_microphone = False
    follows_default = False

    def __init__(
        self,
        path: str,
        realtime: bool = True,
        tail_silence_s: float = 4.0,
        hold_open: bool = False,
    ) -> None:
        self.path = path
        self.realtime = realtime
        self.tail_frames = int(tail_silence_s / FRAME_SECONDS)
        self.hold_open = hold_open
        self.name = f"file:{path.rsplit('/', 1)[-1]}"
        self.dropped = 0
        self._samples = None
        self._position = 0
        self._served = 0
        self._started = 0.0

    def _decode(self):
        import numpy as np

        try:
            with wave.open(self.path, "rb") as source:
                if (
                    source.getframerate() == SAMPLE_RATE
                    and source.getnchannels() == 1
                    and source.getsampwidth() == 2
                ):
                    raw = source.readframes(source.getnframes())
                    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        except (wave.Error, EOFError, OSError):
            pass
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise SourceError(f"{self.path} is not 16 kHz mono 16-bit WAV and ffmpeg is missing")
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                self.path,
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-",
            ],  # fmt: skip
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise SourceError(f"ffmpeg could not decode {self.path}: {result.stderr[-200:]!r}")
        return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0

    def open(self) -> None:
        if self._samples is None:
            self._samples = self._decode()
        self._started = time.monotonic() - self._served * FRAME_SECONDS

    def read(self, timeout: float = 0.25):  # noqa: ARG002
        import numpy as np

        if self.realtime:
            due = self._started + self._served * FRAME_SECONDS
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        samples = self._samples
        if self._position < len(samples):
            frame = samples[self._position : self._position + FRAME_SAMPLES]
            self._position += FRAME_SAMPLES
            if len(frame) < FRAME_SAMPLES:
                frame = np.concatenate(
                    [frame, np.zeros(FRAME_SAMPLES - len(frame), dtype=np.float32)]
                )
        else:
            past_end = (self._position - len(samples)) // FRAME_SAMPLES
            if past_end >= self.tail_frames and not self.hold_open:
                raise EndOfSource
            self._position += FRAME_SAMPLES
            frame = np.zeros(FRAME_SAMPLES, dtype=np.float32)
            if not self.realtime and self.hold_open:
                time.sleep(FRAME_SECONDS)
        self._served += 1
        return frame

    def close(self) -> None:
        pass

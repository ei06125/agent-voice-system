"""Always-on wake-word-anywhere listener with buffering + mute/deaf control.

Started/stopped via the stt-local MCP server's start_listener/stop_listener
tools - runs continuously, independent of any Claude Code turn or Stop hook
lifecycle. Silero VAD chunks the mic stream, whisper.cpp transcribes each
utterance, and each transcript is checked (in order) for the "unmute",
"mute", and "deaf" control keywords, then for the wake phrase matched
ANYWHERE in the utterance (not anchored to the start, unlike the old
per-turn voice-listen.sh gate) - so it can be said mid-thought without
needing a clean line boundary. Once heard, every utterance from that point
is appended to a buffer file that the Stop hook drains on each Claude turn
end; a drain (file truncated by something other than this process) ends
that capture and requires a fresh wake phrase to resume.

"mute"/"unmute" only gate whether Claude speaks replies aloud (see
.voice-mute, checked by Claude itself before calling kokoro-local) - this
daemon keeps listening and buffering either way. "deaf" stops this process
entirely; since a stopped daemon can't hear anything, the only way back is
typing in the Claude Code session to trigger start_listener again.
"""

import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf
from silero_vad_notorch import VADIterator, load_silero_vad

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # required chunk size for silero-vad-notorch at 16kHz

DEVICE_PRIORITY = [
    p.strip()
    for p in os.environ.get(
        "STT_DEVICE_PRIORITY", "External,Speaker,Logitech,BRIO,AirPods"
    ).split(",")
    if p.strip()
]
THRESHOLD = 0.5
MIN_SILENCE_MS = int(os.environ.get("STT_MIN_SILENCE_MS", 500))
SPEECH_PAD_MS = 200

WHISPER_BIN = "/opt/homebrew/bin/whisper-cli"
WHISPER_MODEL = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "models", "ggml-base.en.bin")
)

SCRIPTS_DIR = os.path.expanduser("~/.claude/scripts")
BUFFER_FILE = os.path.join(SCRIPTS_DIR, ".voice-buffer.txt")
MUTE_FLAG = os.path.join(SCRIPTS_DIR, ".voice-mute")
DISABLE_FLAG = os.path.join(SCRIPTS_DIR, ".voice-hook-disabled")
PID_FILE = os.path.join(SCRIPTS_DIR, ".voice-daemon.pid")

# Testing wake phrase while validating the local STT engine - matches the
# one used by the old voice-listen.sh gate. Production phrase (swap in
# once confident the wake word itself is the only remaining unknown):
# WAKE_RE = re.compile(r"(oh my god|omg|o m g)[,.!]?\s*donna[,.!]?\s*", re.IGNORECASE)
WAKE_RE = re.compile(
    r"testing[,.!]?\s*(1|one)[,.]?\s+(2|two)[,.]?\s+(3|three)[,.:]?\s*",
    re.IGNORECASE,
)
UNMUTE_RE = re.compile(r"\bunmute\b", re.IGNORECASE)
MUTE_RE = re.compile(r"\bmute\b", re.IGNORECASE)
DEAF_RE = re.compile(r"\bdeaf\b", re.IGNORECASE)


def find_device() -> int:
    devices = sd.query_devices()

    # If AirPods are the active default *output* (macOS auto-switches to
    # them the moment they connect), follow them for input too - talking
    # into a desk mic while audio plays in your ears makes no sense, and
    # the AirPods mic is right there already. Falls through to the
    # priority list below when AirPods aren't the active output.
    try:
        default_output = sd.query_devices(kind="output")
        if "airpods" in default_output["name"].lower():
            for idx, dev in enumerate(devices):
                if "airpods" in dev["name"].lower() and dev["max_input_channels"] > 0:
                    return idx
    except Exception:
        pass

    for pattern in DEVICE_PRIORITY:
        for idx, dev in enumerate(devices):
            if pattern.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
                return idx
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            return idx
    raise RuntimeError("No audio input device available")


def transcribe(path: str) -> str:
    result = subprocess.run(
        [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", path, "-nt", "-np", "-sns"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def afplay_async(path: str) -> None:
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class BufferWriter:
    """Appends to BUFFER_FILE and detects external drains (size shrank)."""

    def __init__(self, path: str):
        self.path = path
        self._known_size = self._current_size()

    def _current_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def was_drained(self) -> bool:
        return self._current_size() < self._known_size

    def append(self, text: str) -> None:
        with open(self.path, "a") as f:
            f.write(text + "\n")
        self._known_size = self._current_size()


def handle_utterance(text: str, state: dict, buf: BufferWriter) -> None:
    """Pure-ish dispatch on one transcribed utterance. Mutates state/buf."""
    if UNMUTE_RE.search(text):
        try:
            os.remove(MUTE_FLAG)
        except FileNotFoundError:
            pass
        print("[listen-daemon] unmute", file=sys.stderr)
        return
    if MUTE_RE.search(text):
        open(MUTE_FLAG, "a").close()
        print("[listen-daemon] mute", file=sys.stderr)
        return
    if DEAF_RE.search(text):
        print("[listen-daemon] deaf - stopping self", file=sys.stderr)
        state["stop"] = True
        return

    if os.path.exists(DISABLE_FLAG):
        return

    if state["buffering"] and buf.was_drained():
        state["buffering"] = False

    if not state["buffering"]:
        m = WAKE_RE.search(text)
        if not m:
            return
        state["buffering"] = True
        afplay_async("/System/Library/Sounds/Tink.aiff")
        remainder = text[m.end():].strip()
        if remainder:
            buf.append(remainder)
    else:
        buf.append(text)


def main() -> None:
    device_idx = find_device()
    print(
        f"[listen-daemon] listening on device {device_idx}: "
        f"{sd.query_devices(device_idx)['name']}",
        file=sys.stderr,
    )

    model = load_silero_vad()
    vad = VADIterator(
        model,
        threshold=THRESHOLD,
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=MIN_SILENCE_MS,
        speech_pad_ms=SPEECH_PAD_MS,
    )

    clip_queue: "queue.Queue[np.ndarray]" = queue.Queue()
    state = {"buffering": False, "stop": False}
    buf = BufferWriter(BUFFER_FILE)

    def worker() -> None:
        while not state["stop"]:
            try:
                audio = clip_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                clip_path = f.name
            sf.write(clip_path, audio, SAMPLE_RATE)
            text = transcribe(clip_path)
            os.unlink(clip_path)
            if text:
                print(text, file=sys.stderr, flush=True)
                handle_utterance(text, state, buf)

    threading.Thread(target=worker, daemon=True).start()

    preroll_frames = max(1, math.ceil((SPEECH_PAD_MS / 1000) * SAMPLE_RATE / FRAME_SAMPLES))
    preroll: "deque[np.ndarray]" = deque(maxlen=preroll_frames)
    speech_buffer: list[np.ndarray] = []
    in_speech = False

    def callback(indata, frames, time_info, status):
        nonlocal in_speech
        if status:
            print(f"[listen-daemon] stream status: {status}", file=sys.stderr)
        chunk = indata[:, 0].copy()
        event = vad(chunk)
        if event is not None and "start" in event:
            in_speech = True
            speech_buffer.clear()
            speech_buffer.extend(preroll)
        if in_speech:
            speech_buffer.append(chunk)
        else:
            preroll.append(chunk)
        if event is not None and "end" in event:
            in_speech = False
            if speech_buffer:
                clip_queue.put(np.concatenate(speech_buffer))
            speech_buffer.clear()
            preroll.clear()

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        with sd.InputStream(
            device=device_idx,
            channels=1,
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            dtype="float32",
            callback=callback,
        ):
            while not state["stop"]:
                time.sleep(0.1)
    finally:
        try:
            os.remove(PID_FILE)
        except FileNotFoundError:
            pass
        print("[listen-daemon] stopped", file=sys.stderr)


if __name__ == "__main__":
    main()

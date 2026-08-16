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
MIN_SILENCE_MS = int(os.environ.get("STT_MIN_SILENCE_MS", "500"))
SPEECH_PAD_MS = 200

WHISPER_BIN = "/opt/homebrew/bin/whisper-cli"
WHISPER_MODEL = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "models", "ggml-base.en.bin")
)

# Bounds so an undrained buffer (Claude busy, no session ending a turn for
# a while) or a backed-up transcription worker can't grow memory/disk
# without limit.
MAX_BUFFER_LINES = int(os.environ.get("STT_MAX_BUFFER_LINES", "200"))
MAX_QUEUED_CLIPS = int(os.environ.get("STT_MAX_QUEUED_CLIPS", "10"))

# Raw audio frames waiting for the VAD worker (should rarely queue up at
# all - each frame is 32ms and VAD inference is much faster than that -
# but bounded rather than unbounded in case the worker ever falls behind).
MAX_QUEUED_FRAMES = int(os.environ.get("STT_MAX_QUEUED_FRAMES", "50"))

# Hard cap on one continuous in-speech run: without this, VAD misclassifying
# sustained non-speech (music, noise, feedback) as speech means it never
# fires an "end" event and speech_buffer grows without limit. Force-flush
# past this many seconds rather than waiting indefinitely for silence.
MAX_UTTERANCE_SECONDS = float(os.environ.get("STT_MAX_UTTERANCE_SECONDS", "30"))

# A capture nothing drains (Claude busy for a long stretch, or just never
# checks back in) shouldn't sit around forever - expire it after this many
# seconds since the wake phrase that started it, checked by a watchdog
# thread every WATCHDOG_INTERVAL_SECONDS. 300s (5min) was too aggressive -
# a real monologue got silently destroyed by this expiry while Claude was
# legitimately still mid-turn (building the very poller meant to catch
# it). Line-count/queue caps already bound worst-case memory, so this only
# needs to catch genuinely abandoned captures, not routine long turns.
MAX_BUFFER_AGE_SECONDS = int(os.environ.get("STT_MAX_BUFFER_AGE_SECONDS", str(21 * 60)))
WATCHDOG_INTERVAL_SECONDS = 10

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
# Control keywords share the "OMG" prefix from the production wake phrase
# (same (oh my god|omg|o m g) alternation) rather than bare words. Bare
# "unmute" measured unreliable live - whisper transcribed it as "and mute",
# "Amute", and plain "mute" across repeated attempts, never cleanly as one
# word, and each miss re-triggered mute instead of clearing it.
OMG_PREFIX = r"(oh my god|omg|o m g)[,.!]?\s*"
UNMUTE_RE = re.compile(OMG_PREFIX + r"speak\b", re.IGNORECASE)
MUTE_RE = re.compile(OMG_PREFIX + r"mute\b", re.IGNORECASE)
DEAF_RE = re.compile(OMG_PREFIX + r"go deaf\b", re.IGNORECASE)


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
    except Exception:  # noqa: BLE001, S110 - backend-specific, fall through
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
        check=False,
    )
    return result.stdout.strip()


def afplay_async(path: str) -> None:
    subprocess.Popen(
        ["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


class BufferWriter:
    """Ring-buffered writer to BUFFER_FILE: keeps only the most recent
    MAX_BUFFER_LINES utterances, so a buffer nothing drains for a long
    time can't grow memory/disk without limit - oldest lines are dropped
    once full. Detects external drains (file shrank since our last write)
    to know when a capture session has been consumed."""

    def __init__(self, path: str):
        self.path = path
        self.lines: deque[str] = deque(maxlen=MAX_BUFFER_LINES)
        self._known_size = self._current_size()

    def _current_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def was_drained(self) -> bool:
        return self._current_size() < self._known_size

    def reset(self) -> None:
        self.lines.clear()

    def clear(self) -> None:
        """Actively empty the buffer (self-initiated, e.g. staleness
        expiry) - unlike reset(), also removes the file on disk rather
        than just forgetting in-memory state about an external drain."""
        self.lines.clear()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        self._known_size = 0

    def append(self, text: str) -> None:
        was_full = len(self.lines) == self.lines.maxlen
        self.lines.append(text)
        if was_full:
            print(
                f"[listen-daemon] buffer full ({self.lines.maxlen} lines), "
                "dropped oldest",
                file=sys.stderr,
            )
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(self.lines) + "\n")
        os.replace(tmp, self.path)
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
        state["buffer_started_at"] = None
        buf.reset()

    if not state["buffering"]:
        m = WAKE_RE.search(text)
        if not m:
            return
        state["buffering"] = True
        state["buffer_started_at"] = time.time()
        afplay_async("/System/Library/Sounds/Tink.aiff")
        remainder = text[m.end() :].strip()
        if remainder:
            buf.append(remainder)
    else:
        buf.append(text)


def run_vad_worker(
    frame_queue: "queue.Queue[np.ndarray]",
    clip_queue: "queue.Queue[np.ndarray]",
    vad: VADIterator,
    state: dict,
) -> None:
    """Owns VAD inference and utterance segmentation. Runs on a normal
    thread, fed by the audio callback's frame_queue, so ONNX inference
    never runs inside the real-time PortAudio callback (which must avoid
    allocation, blocking, and unpredictable-duration work)."""
    preroll_frames = max(
        1, math.ceil((SPEECH_PAD_MS / 1000) * SAMPLE_RATE / FRAME_SAMPLES)
    )
    preroll: deque[np.ndarray] = deque(maxlen=preroll_frames)
    speech_buffer: list[np.ndarray] = []
    in_speech = False
    max_utterance_frames = max(
        1, math.ceil(MAX_UTTERANCE_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
    )

    def flush(reason: str) -> None:
        nonlocal in_speech
        in_speech = False
        if speech_buffer:
            try:
                clip_queue.put_nowait(np.concatenate(speech_buffer))
            except queue.Full:
                print(
                    "[listen-daemon] transcription backlog full, dropped a clip",
                    file=sys.stderr,
                )
        speech_buffer.clear()
        preroll.clear()
        if reason == "max_duration":
            vad.reset_states()

    while not state["stop"]:
        try:
            chunk = frame_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        event = vad(chunk)
        if event is not None and "start" in event:
            in_speech = True
            speech_buffer.clear()
            speech_buffer.extend(preroll)
        if in_speech:
            speech_buffer.append(chunk)
            if len(speech_buffer) >= max_utterance_frames:
                print(
                    f"[listen-daemon] utterance exceeded "
                    f"{MAX_UTTERANCE_SECONDS:.0f}s, force-flushing",
                    file=sys.stderr,
                )
                flush("max_duration")
                continue
        else:
            preroll.append(chunk)
        if event is not None and "end" in event:
            flush("vad_end")


def main() -> None:
    # A prior run's buffer file survives a stop/start cycle on disk (only
    # in-memory state resets) - stale undrained content from before a
    # restart would otherwise resurface on the next hook drain. Start
    # every run from a clean buffer.
    try:
        os.remove(BUFFER_FILE)
    except FileNotFoundError:
        pass

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

    clip_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=MAX_QUEUED_CLIPS)
    state = {"buffering": False, "stop": False, "buffer_started_at": None}
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

    def watchdog() -> None:
        while not state["stop"]:
            time.sleep(WATCHDOG_INTERVAL_SECONDS)
            started_at = state["buffer_started_at"]
            if state["buffering"] and started_at is not None:
                age = time.time() - started_at
                if age > MAX_BUFFER_AGE_SECONDS:
                    print(
                        f"[listen-daemon] buffer stale ({age:.0f}s), "
                        "clearing and waiting for a fresh wake phrase",
                        file=sys.stderr,
                    )
                    state["buffering"] = False
                    state["buffer_started_at"] = None
                    buf.clear()

    # PortAudio's callback contract requires avoiding allocation, blocking,
    # and unpredictable-duration work (ONNX inference, buffer concatenation,
    # I/O) - a short inference often works, but risks dropouts under
    # scheduler pressure. The callback below only copies frames into a
    # bounded queue; VAD inference and segmentation run in run_vad_worker,
    # on a normal worker thread instead.
    frame_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=MAX_QUEUED_FRAMES)

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(
        target=run_vad_worker, args=(frame_queue, clip_queue, vad, state), daemon=True
    ).start()

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[listen-daemon] stream status: {status}", file=sys.stderr)
        try:
            frame_queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            print("[listen-daemon] VAD backlog full, dropped a frame", file=sys.stderr)

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

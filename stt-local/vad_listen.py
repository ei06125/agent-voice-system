"""Silero VAD chunker + whisper.cpp transcriber.

Replaces `dg listen --mic --non-interactive` as the producer inside
voice-listen.sh. Reads the BRIO mic continuously, uses Silero VAD to
detect utterance boundaries (real endpointing, not an energy gate),
flushes each utterance to a temp WAV, transcribes it with whisper-cli,
and prints one plain-text line per utterance to stdout - same output
shape the wake-word gate in voice-listen.sh already expects.
"""

import math
import os
import queue
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

# Tried in order; first matching *connected* device wins. AirPods aren't
# always connected, so they're last choice, not the default.
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

MAX_QUEUED_CLIPS = int(os.environ.get("STT_MAX_QUEUED_CLIPS", "10"))
MAX_QUEUED_FRAMES = int(os.environ.get("STT_MAX_QUEUED_FRAMES", "50"))
# Hard cap on one continuous in-speech run: without this, VAD misclassifying
# sustained non-speech (music, noise, feedback) as speech means it never
# fires an "end" event and speech_buffer grows without limit.
MAX_UTTERANCE_SECONDS = float(os.environ.get("STT_MAX_UTTERANCE_SECONDS", "30"))

WHISPER_BIN = "/opt/homebrew/bin/whisper-cli"
WHISPER_MODEL = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "models", "ggml-base.en.bin")
)


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
    # Nothing on the priority list is connected - fall back to whatever
    # input device macOS actually has available, rather than hard-failing.
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


def worker(clip_queue: "queue.Queue[np.ndarray]") -> None:
    while True:
        audio = clip_queue.get()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            clip_path = f.name
        sf.write(clip_path, audio, SAMPLE_RATE)
        text = transcribe(clip_path)
        os.unlink(clip_path)
        if text:
            print(text, flush=True)


def run_vad_worker(
    frame_queue: "queue.Queue[np.ndarray]",
    clip_queue: "queue.Queue[np.ndarray]",
    vad: VADIterator,
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
                    "[stt-local] transcription backlog full, dropped a clip",
                    file=sys.stderr,
                )
        speech_buffer.clear()
        preroll.clear()
        if reason == "max_duration":
            vad.reset_states()

    while True:
        chunk = frame_queue.get()
        event = vad(chunk)
        if event is not None and "start" in event:
            in_speech = True
            speech_buffer.clear()
            speech_buffer.extend(preroll)
        if in_speech:
            speech_buffer.append(chunk)
            if len(speech_buffer) >= max_utterance_frames:
                print(
                    f"[stt-local] utterance exceeded "
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
    device_idx = find_device()
    print(
        f"[stt-local] listening on device {device_idx}: "
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
    threading.Thread(target=worker, args=(clip_queue,), daemon=True).start()

    # PortAudio's callback contract requires avoiding allocation, blocking,
    # and unpredictable-duration work (ONNX inference, buffer concatenation,
    # I/O) - a short inference often works, but risks dropouts under
    # scheduler pressure. The callback below only copies frames into a
    # bounded queue; VAD inference and segmentation run in run_vad_worker,
    # on a normal worker thread instead.
    frame_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=MAX_QUEUED_FRAMES)
    threading.Thread(
        target=run_vad_worker, args=(frame_queue, clip_queue, vad), daemon=True
    ).start()

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[stt-local] stream status: {status}", file=sys.stderr)
        try:
            frame_queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            print("[stt-local] VAD backlog full, dropped a frame", file=sys.stderr)

    with sd.InputStream(
        device=device_idx,
        channels=1,
        samplerate=SAMPLE_RATE,
        blocksize=FRAME_SAMPLES,
        dtype="float32",
        callback=callback,
    ):
        while True:
            time.sleep(0.1)


if __name__ == "__main__":
    main()

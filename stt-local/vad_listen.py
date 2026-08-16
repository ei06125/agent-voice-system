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

    clip_queue: queue.Queue[np.ndarray] = queue.Queue()
    threading.Thread(target=worker, args=(clip_queue,), daemon=True).start()

    # VADIterator's 'start' event reports a start sample that already backs
    # up by speech_pad_samples - it assumes that pre-roll audio is still
    # available. Keep a rolling buffer of recent frames so that pre-roll is
    # actually there to prepend, instead of losing the first word to the
    # few frames Silero needs to cross the speech threshold.
    preroll_frames = max(
        1, math.ceil((SPEECH_PAD_MS / 1000) * SAMPLE_RATE / FRAME_SAMPLES)
    )
    preroll: deque[np.ndarray] = deque(maxlen=preroll_frames)

    speech_buffer: list[np.ndarray] = []
    in_speech = False

    def callback(indata, frames, time_info, status):
        nonlocal in_speech
        if status:
            print(f"[stt-local] stream status: {status}", file=sys.stderr)
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

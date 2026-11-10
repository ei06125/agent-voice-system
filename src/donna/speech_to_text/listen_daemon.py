"""Always-on listener with mute/deaf control and two independent modes.

Started/stopped via the stt-local MCP server's start_listener/stop_listener
tools - runs continuously, independent of any Claude Code turn or Stop hook
lifecycle. Silero VAD chunks the mic stream, whisper.cpp transcribes each
utterance, and each transcript is checked (in order) for the "unmute",
"mute", and "deaf" control keywords.

DONNA_VOICE_QUEUE_MODE=shadow (and "off") keep the original design: a wake
phrase matched ANYWHERE in an utterance arms a capture, every utterance
from that point is appended to a buffer file the Stop hook drains, and a
drain ends the capture (fresh wake phrase required to resume).

DONNA_VOICE_QUEUE_MODE=deliver is a different, newer design (no wake
phrase, no capture state): every finalized utterance is logged
unconditionally, with a timestamp, to one append-only file
(UTTERANCE_LOG_PATH) - nothing is ever lost to a pause or a missing wake
word. Separately, hearing the bare name "Donna" anywhere touches a
lightweight trigger file (NAME_MENTIONED_PATH) - not a content gate,
everything is logged regardless, just what taps whichever Claude session
is watching so it isn't paged on every ambient sound. That session reads
everything logged since its own last-read position and judges relevance
itself. See handle_utterance's deliver branch for the full rationale.

"mute"/"unmute" only gate whether Claude speaks replies aloud (see
.voice-mute, checked by Claude itself before calling kokoro-local) - this
daemon keeps listening and logging either way. "deaf" stops this process
entirely; since a stopped daemon can't hear anything, the only way back is
typing in the Claude Code session to trigger start_listener again.
"""

import glob
import json
import math
import os
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from silero_vad_notorch import VADIterator, load_silero_vad

from donna.orchestration.voice_queue import DEFAULT_STATE_DIR, VoiceQueue
from donna.text_to_speech.playback_control import cancel_playback, register_playback

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # required chunk size for silero-vad-notorch at 16kHz

DEVICE_PRIORITY = [
    p.strip()
    for p in os.environ.get(
        "STT_DEVICE_PRIORITY", "External,Speaker,Logitech,BRIO,AirPods"
    ).split(",")
    if p.strip()
]
# Default True preserves the original "AirPods become default output ->
# follow them for input too" behavior. Set to false when the goal is the
# opposite: route TTS to AirPods specifically to physically separate it
# from a fixed desk/webcam mic, so the mic never picks up the echo at
# all - the daemon must NOT then also switch its own input to the
# AirPods' mic, which would put input and output back on the same
# physical device and reintroduce exactly the echo problem this was
# meant to avoid.
FOLLOW_AIRPODS_INPUT = os.environ.get("STT_FOLLOW_AIRPODS_INPUT", "1") not in {
    "0",
    "false",
    "False",
}
THRESHOLD = 0.5
MIN_SILENCE_MS = int(os.environ.get("STT_MIN_SILENCE_MS", "500"))
SPEECH_PAD_MS = 200

WHISPER_BIN = (
    os.environ.get("STT_WHISPER_BIN")
    or shutil.which("whisper-cli")
    or "whisper-cli"
)
WHISPER_MODEL = os.environ.get(
    "STT_WHISPER_MODEL",
    os.path.normpath(
        Path(__file__).resolve().parents[3] / "models" / "ggml-base.en.bin"
    ),
)
WHISPER_NO_GPU = os.environ.get("STT_WHISPER_NO_GPU", "1") not in {
    "0",
    "false",
    "False",
}

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

VOICE_QUEUE_MODE = os.environ.get("DONNA_VOICE_QUEUE_MODE", "shadow").lower()
if VOICE_QUEUE_MODE not in {"off", "shadow", "deliver"}:
    raise ValueError("DONNA_VOICE_QUEUE_MODE must be 'off', 'shadow', or 'deliver'")
# No DONNA_CLAUDE_SESSION_ID requirement here anymore: deliver mode no
# longer resumes any Claude session (see the continuous-log design below,
# by _append_utterance_log/_write_name_mentioned_trigger) - it just writes
# a local file for whichever session happens to be watching.

# Set by kokoro-local's speak() around each afplay call (see its
# mcp_server.py) - the only signal this daemon has that DONNA's own TTS is
# about to come out of the speaker. VAD can't tell the difference between
# that and real speech (per DeepResearchEchoCancellation.md); discarding
# capture while it's set, plus a short hangover after it clears, is Mode A
# from that doc - half-duplex turn-taking, not real echo cancellation, but
# directly fixes the self-feedback loop hit live this session.
#
# kokoro-local is user-scoped, so multiple server processes (one per open
# Claude Code session) can be alive and speaking concurrently. Each writes
# its own PID-suffixed flag file (.voice-playback-flag-<pid>) rather than
# a single shared path, so one process's cleanup can never delete another
# still-playing process's flag - gate on whether *any* match the glob.
# (A shared single path let exactly that happen live: one process's
# `finally` cleanup cleared the flag mid-playback of a second process,
# and the daemon transcribed that second process's own TTS output as a
# real voice command.)
PLAYBACK_FLAG_GLOB_NAME = ".voice-playback-flag-*"
PLAYBACK_HANGOVER_MS = int(os.environ.get("STT_PLAYBACK_HANGOVER_MS", "250"))


def is_playback_active(scripts_dir: str = SCRIPTS_DIR) -> bool:
    """True while any kokoro-local process (any session) is mid-speech."""
    return bool(glob.glob(os.path.join(scripts_dir, PLAYBACK_FLAG_GLOB_NAME)))


def _should_gate_for_playback(voice_queue_mode: str) -> bool:
    """shadow/off discard all mic frames during playback (self-feedback
    prevention - a real, previously hit-live bug for those modes' single-
    command semantics). deliver mode intentionally does not: real barge-in
    ("kill my current afplay the moment you say my name again") needs the
    mic listening *during* playback, and true self-feedback rejection
    (telling my own echoed voice apart from yours) would need real
    acoustic echo cancellation to do safely. The substitute deliver mode
    already has: everything gets logged either way, and only a "Donna"
    mention triggers anything - my own TTS essentially never says my own
    name mid-reply, so echo picked up by the mic just sits harmlessly in
    the log instead of paging anyone."""
    return voice_queue_mode != "deliver" and is_playback_active()


WAKE_RE = re.compile(
    r"(oh my god|omg|o m g)[,.!]?\s*(donna|agnis)[,.!]?\s*",
    re.IGNORECASE,
)
# Control keywords require an explicit address prefix rather than bare
# words, same reasoning as ever: bare "unmute" measured unreliable live -
# whisper transcribed it as "and mute", "Amute", and plain "mute" across
# repeated attempts, never cleanly as one word, and each miss re-triggered
# mute instead of clearing it. Accepts either the original "OMG Donna"
# phrase or bare "Donna" (deliver mode's newer bare-name convention - hit
# live: "Okay, Donna, mute" didn't match the OMG-only version of this
# regex, so mute silently failed while the daemon kept talking).
OMG_PREFIX = r"(oh my god|omg|o m g)[,.!]?\s*"
ADDRESS_PREFIX = r"((oh my god|omg|o m g)([,.!]?\s*(donna|agnis))?|donna|agnis)[,.!]?\s*"
UNMUTE_RE = re.compile(ADDRESS_PREFIX + r"speak\b", re.IGNORECASE)
MUTE_RE = re.compile(ADDRESS_PREFIX + r"mute\b", re.IGNORECASE)
DEAF_RE = re.compile(ADDRESS_PREFIX + r"go deaf\b", re.IGNORECASE)


def find_device() -> int:
    devices = sd.query_devices()

    # If AirPods are the active default *output* (macOS auto-switches to
    # them the moment they connect), follow them for input too - talking
    # into a desk mic while audio plays in your ears makes no sense, and
    # the AirPods mic is right there already. Falls through to the
    # priority list below when AirPods aren't the active output, or when
    # FOLLOW_AIRPODS_INPUT is explicitly disabled (STT_FOLLOW_AIRPODS_INPUT=0)
    # for the opposite goal: TTS to AirPods, mic staying on a fixed desk
    # device, so playback and capture are on physically separate hardware.
    if FOLLOW_AIRPODS_INPUT:
        try:
            default_output = sd.query_devices(kind="output")
            if "airpods" in default_output["name"].lower():
                for idx, dev in enumerate(devices):
                    if (
                        "airpods" in dev["name"].lower()
                        and dev["max_input_channels"] > 0
                    ):
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
    command = [WHISPER_BIN]
    if WHISPER_NO_GPU:
        command.append("-ng")
    command.extend(
        ["-m", WHISPER_MODEL, "-f", path, "-nt", "-np", "-sns"]
    )
    result = subprocess.run(
        command,
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


def _close_capture(state: dict, buf: BufferWriter) -> None:
    """End the current wake-authorized capture (shadow/off modes only); a
    fresh wake phrase is required before the next utterance is treated as
    a command. deliver mode has no capture state at all - see below."""
    state["buffering"] = False
    state["buffer_started_at"] = None
    state["capture_id"] = None
    state["utterance_seq"] = 0
    buf.reset()


def append_command(
    text: str,
    state: dict,
    buf: BufferWriter,
    voice_queue: VoiceQueue | None,
    vad_ended_at: float | None,
) -> None:
    """shadow/off only now - deliver mode never calls this (see
    _append_utterance_log below)."""
    if voice_queue is not None:
        state["utterance_seq"] += 1
        try:
            result = voice_queue.enqueue_and_notify(
                capture_id=state["capture_id"],
                utterance_seq=state["utterance_seq"],
                text=text,
                vad_ended_at=vad_ended_at,
                is_shadow=True,
            )
        except (OSError, sqlite3.Error) as error:
            print(
                f"[listen-daemon] queue write failed: {error}",
                file=sys.stderr,
            )
        else:
            print(
                f"[listen-daemon] shadow-queued voice command {result.message_id}",
                file=sys.stderr,
            )
    buf.append(text)


# --- deliver mode: continuous log + name-mention trigger ------------------
#
# Replaces an earlier wake-phrase-gated "one command per wake" design.
# That required "Oh my god, Donna" before every single utterance and cut
# you off mid-thought the moment VAD saw a pause (a new wake phrase was
# required to continue) - reported live as "I really don't want this
# wake-up word... I want you to be able to speak freely."
#
# New design, per that conversation: log every finalized utterance
# unconditionally (mute/deaf/DISABLE_FLAG excepted) to one append-only,
# timestamped file - never gated, so nothing is ever lost to a pause or a
# missing wake phrase, and lead-up context is always available. Separately,
# touch a lightweight trigger file only when the bare name "Donna" is
# heard (no "OMG" prefix needed) - not a content gate (everything is
# logged either way), just what taps Claude so ambient noise/video/other
# people don't page it every time Whisper transcribes something. Claude
# reads everything since its own last-read position on each trigger and
# judges relevance itself (its name was misheard, someone else was being
# addressed, etc.) rather than the daemon guessing.
UTTERANCE_LOG_PATH = DEFAULT_STATE_DIR / "utterance-log.jsonl"
NAME_MENTIONED_PATH = DEFAULT_STATE_DIR / "donna-mentioned.json"
NAME_RE = re.compile(r"\bdonna\b", re.IGNORECASE)

# Pre-rendered (not live-synthesized - that has its own latency) filler
# clips, played instantly, in this process, the moment "Donna" is heard -
# not after the round trip to Claude, which measured ~35s end to end live
# tonight (VAD finalize -> file write -> fswatch -> notification -> Claude
# reads, composes, calls speak()). A filler fired only after that round
# trip completes is fired 35s too late to mask anything. Regenerate with
# kokoro-local's speak(play=False, output_dir=str(FILLERS_DIR)) - plain
# afplay here, no TTS dependency in this process.
FILLERS_DIR = DEFAULT_STATE_DIR / "fillers"

# The filler+notify trigger is debounced by this many ms of silence after
# the last utterance, not fired on the instant "Donna" is heard - firing
# immediately risked firing mid-thought, since VAD splits one continuous
# sentence into multiple utterances on any brief pause (hit live: "Alright
# Donna, so for the" / "Myth." were one interrupted thought, not two).
# Any further speech during the window - not just another "Donna" mention
# - cancels and reschedules, so it only fires once you've actually gone
# quiet. Barge-in (killing current playback) is NOT debounced - see
# handle_utterance's deliver branch - there's no reason to delay cutting
# off a reply just because you might keep talking after saying my name.
TRIGGER_DEBOUNCE_MS = int(os.environ.get("STT_DONNA_DEBOUNCE_MS", "900"))
TRIGGER_MODE = os.environ.get("STT_TRIGGER_MODE", "name").lower()
if TRIGGER_MODE not in {"name", "always"}:
    raise ValueError("STT_TRIGGER_MODE must be 'name' or 'always'")


def _append_utterance_log(
    text: str, vad_ended_at: float | None, playback_overlap: bool = False
) -> None:
    DEFAULT_STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "ts": vad_ended_at if vad_ended_at is not None else time.time(),
        "text": text,
        "playback_overlap": playback_overlap,
    }
    fd = os.open(UTTERANCE_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(record) + "\n").encode())
    finally:
        os.close(fd)


def _play_filler() -> None:
    """Instant, local, no round trip to Claude - see FILLERS_DIR comment
    for why this exists. Registered with playback_control so a later
    barge-in (another "Donna" mention, or "mute") can still kill it, same
    as any other playback; harmless if none exist yet (empty list)."""
    clips = sorted(FILLERS_DIR.glob("filler-*.wav"))
    if not clips:
        return
    proc = subprocess.Popen(
        ["afplay", str(random.choice(clips))],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        register_playback(proc.pid)
    except ProcessLookupError:
        pass


def _fire_trigger(state: dict, text: str) -> None:
    """The actual filler+notify action - only reached after
    TRIGGER_DEBOUNCE_MS of silence, via _schedule_trigger below. Runs on
    a threading.Timer's own thread, not the VAD worker thread."""
    # Must clear this: state["pending_trigger_timer"] is what
    # handle_utterance checks to decide "is a trigger already pending"
    # (see its `name_mentioned or state.get(...) is not None` check).
    # Leaving the completed timer object in place would make every
    # utterance from here on look like it's still within a pending
    # window, since a finished Timer is still not None - re-triggering
    # forever on content with no "Donna" mention at all (hit live: "This
    # is it, Donna." correctly fired, then "This is the final test." -
    # no name in it - incorrectly fired again because of exactly this).
    state["pending_trigger_timer"] = None
    _play_filler()
    data = {"trigger_text": text, "ts": time.time()}
    NAME_MENTIONED_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = NAME_MENTIONED_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    os.chmod(temporary, 0o600)
    os.replace(temporary, NAME_MENTIONED_PATH)


def _schedule_trigger(state: dict, text: str) -> None:
    """(Re)start the debounce window. Called for the utterance that first
    mentions "Donna", and for every utterance after that while a trigger
    is still pending - see TRIGGER_DEBOUNCE_MS."""
    existing = state.get("pending_trigger_timer")
    if existing is not None:
        existing.cancel()
    timer = threading.Timer(
        TRIGGER_DEBOUNCE_MS / 1000, _fire_trigger, args=(state, text)
    )
    timer.daemon = True
    state["pending_trigger_timer"] = timer
    timer.start()


def handle_utterance(
    text: str,
    state: dict,
    buf: BufferWriter,
    voice_queue: VoiceQueue | None = None,
    vad_ended_at: float | None = None,
    playback_overlap: bool = False,
) -> None:
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
        # "mute" previously only prevented *future* speak() calls - a
        # reply already mid-playback kept talking right over you, since
        # nothing told afplay to stop. Same cancel_playback() barge-in
        # already wired for hearing "Donna" again mid-reply.
        cancel_playback()
        print("[listen-daemon] mute", file=sys.stderr)
        return
    if DEAF_RE.search(text):
        print("[listen-daemon] deaf - stopping self", file=sys.stderr)
        state["stop"] = True
        return

    if os.path.exists(DISABLE_FLAG):
        return

    if VOICE_QUEUE_MODE == "deliver":
        _append_utterance_log(text, vad_ended_at, playback_overlap)
        name_mentioned = NAME_RE.search(text)
        if name_mentioned:
            # Barge-in stays instant, not debounced: hearing "Donna"
            # again while a reply is playing means "stop talking, listen
            # to this instead" - no reason to wait and see if more
            # speech follows before cutting off the old reply. No-op
            # (empty list) if nothing is currently registered as
            # playing. Uses the same register_playback()/cancel_playback()
            # ownership contract the typed UserPromptSubmit hook uses -
            # see playback_control.py.
            cancel_playback()
        should_trigger = bool(name_mentioned) or (
            TRIGGER_MODE == "always" and not playback_overlap
        )
        should_extend = (
            state.get("pending_trigger_timer") is not None and not playback_overlap
        )
        if should_trigger or should_extend:
            _schedule_trigger(state, text)
        return

    # shadow/off: unchanged wake-phrase-gated buffering state machine.
    if state["buffering"] and buf.was_drained():
        _close_capture(state, buf)

    if not state["buffering"]:
        m = WAKE_RE.search(text)
        if not m:
            return
        state["buffering"] = True
        state["buffer_started_at"] = time.time()
        state["capture_id"] = str(uuid.uuid4())
        state["utterance_seq"] = 0
        afplay_async("/System/Library/Sounds/Tink.aiff")
        remainder = text[m.end() :].strip()
        if remainder:
            append_command(remainder, state, buf, voice_queue, vad_ended_at)
    else:
        append_command(text, state, buf, voice_queue, vad_ended_at)


def run_vad_worker(
    frame_queue: "queue.Queue[np.ndarray]",
    clip_queue: "queue.Queue[tuple[np.ndarray, float, bool]]",
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
    playback_overlap = False
    max_utterance_frames = max(
        1, math.ceil(MAX_UTTERANCE_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)
    )

    def flush(reason: str) -> None:
        nonlocal in_speech, playback_overlap
        in_speech = False
        if speech_buffer:
            try:
                clip_queue.put_nowait(
                    (np.concatenate(speech_buffer), time.time(), playback_overlap)
                )
            except queue.Full:
                print(
                    "[listen-daemon] transcription backlog full, dropped a clip",
                    file=sys.stderr,
                )
        speech_buffer.clear()
        preroll.clear()
        playback_overlap = False
        if reason == "max_duration":
            vad.reset_states()

    in_playback_gate = False
    hangover_until: float | None = None

    while not state["stop"]:
        try:
            chunk = frame_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if _should_gate_for_playback(VOICE_QUEUE_MODE):
            if not in_playback_gate:
                # Playback just started - discard whatever was mid-flight
                # rather than risk transcribing audio mixed with it.
                in_speech = False
                speech_buffer.clear()
                preroll.clear()
            in_playback_gate = True
            hangover_until = None
            continue

        if in_playback_gate:
            if hangover_until is None:
                hangover_until = time.time() + PLAYBACK_HANGOVER_MS / 1000
            if time.time() < hangover_until:
                continue
            # Hangover elapsed - resume with a clean VAD state so residual
            # playback tail/reverb doesn't get mistaken for speech onset.
            vad.reset_states()
            in_playback_gate = False
            hangover_until = None

        event = vad(chunk)
        if event is not None and "start" in event:
            in_speech = True
            playback_overlap = is_playback_active()
            speech_buffer.clear()
            speech_buffer.extend(preroll)
        if in_speech:
            playback_overlap = playback_overlap or is_playback_active()
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

    clip_queue: queue.Queue[tuple[np.ndarray, float, bool]] = queue.Queue(
        maxsize=MAX_QUEUED_CLIPS
    )
    state = {
        "buffering": False,
        "stop": False,
        "buffer_started_at": None,
        "capture_id": None,
        "utterance_seq": 0,
        "pending_trigger_timer": None,
    }
    buf = BufferWriter(BUFFER_FILE)
    voice_queue = None
    if VOICE_QUEUE_MODE == "shadow":
        try:
            voice_queue = VoiceQueue()
        except (OSError, sqlite3.Error) as error:
            print(
                f"[listen-daemon] queue unavailable: {error}",
                file=sys.stderr,
            )

    def worker() -> None:
        while not state["stop"]:
            try:
                audio, vad_ended_at, playback_overlap = clip_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                clip_path = f.name
            sf.write(clip_path, audio, SAMPLE_RATE)
            text = transcribe(clip_path)
            os.unlink(clip_path)
            if text:
                print(text, file=sys.stderr, flush=True)
                handle_utterance(
                    text,
                    state,
                    buf,
                    voice_queue,
                    vad_ended_at,
                    playback_overlap,
                )

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
                    state["capture_id"] = None
                    state["utterance_seq"] = 0
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

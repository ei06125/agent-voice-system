"""Paths, settings and the small state files the listener shares with the CLI.

Standard library only: the status/pause/resume commands and the session hooks
import this module under a bare ``python3`` and have to start instantly.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

# Every setting, with the value used when neither ~/.donna/config.json nor an
# EARS_<NAME> environment variable overrides it. The default's type decides how
# an override is parsed.
DEFAULTS: dict[str, object] = {
    # Speech model: a name (resolved to <models dir>/ggml-<name>.bin and
    # downloaded on first use) or an absolute path to a ggml file.
    "model": "large-v3-turbo",
    # Whisper language code, or "auto" to detect it per utterance.
    "language": "auto",
    # Optional vocabulary hint passed to Whisper as its initial prompt.
    "prompt": "",
    # Input device: empty follows the macOS default input. Otherwise a
    # comma-separated list of name fragments, first match wins.
    "device": "",
    "whisper_threads": 4,
    # --- voice activity detection ---
    "vad_threshold": 0.5,
    "min_silence_ms": 700,  # silence that ends an utterance
    "pre_roll_ms": 300,  # audio kept from before speech was detected
    "min_speech_ms": 250,  # shorter blips are discarded
    "max_utterance_s": 28.0,  # Whisper's window is 30 s
    # --- delivery: "live" pushes at natural pauses, "notes" pushes digests ---
    "mode": "live",
    "pause_s": 2.0,  # live: quiet time that ends a batch
    "min_interval_s": 3.0,  # live: least time between two events
    "max_wait_s": 30.0,  # live: deliver a long monologue in chunks
    "busy_events": 5,  # live: this many events in a minute means "busy"...
    "busy_interval_s": 15.0,  # ...and events are then spaced this far apart
    "notes_interval_s": 60.0,  # notes: one digest per interval
    # --- speech output (`ears say`) ---
    "speak": False,  # whether a session starts with spoken replies switched on
    "voice": "af_heart",  # Kokoro voice; its first letter is its language
    "voice_speed": 1.0,
    "voice_engine": "kokoro",  # or "say": the macOS built-in voice, nothing to load
    "say_voice": "",  # macOS voice for the "say" engine; empty is the system voice
    # Ignore the microphone while speaking, so the listener does not transcribe
    # the reply: "auto" does it unless the output looks like headphones.
    "voice_gate": "auto",  # auto | on | off
    "voice_barge_in": True,  # stop speaking when the user starts to talk
    "voice_idle_s": 1800.0,  # the voice server exits after this long unused
}

# Only these may be changed at runtime through control.json.
MODES = ("live", "notes")


def home() -> Path:
    return Path(os.environ.get("DONNA_HOME") or "~/.donna").expanduser()


def ears_dir() -> Path:
    return home() / "ears"


def run_dir() -> Path:
    return ears_dir() / "run"


def sessions_dir() -> Path:
    return ears_dir() / "sessions"


def models_dir() -> Path:
    return home() / "models"


def log_path() -> Path:
    return ears_dir() / "ears.log"


def config_path() -> Path:
    return home() / "config.json"


def control_path() -> Path:
    return run_dir() / "control.json"


def lock_path() -> Path:
    return run_dir() / "mic.lock"


def voice_state_path() -> Path:
    return run_dir() / "voice.json"


def voice_lock_path() -> Path:
    return run_dir() / "voice.lock"


def speaking_path() -> Path:
    return run_dir() / "speaking.json"


def voice_log_path() -> Path:
    return ears_dir() / "voice.log"


def ensure_dirs() -> None:
    """Create the state tree, readable by the user only: it holds transcripts."""
    for path in (home(), ears_dir(), run_dir(), sessions_dir(), models_dir()):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _coerce(value: object, like: object) -> object:
    if isinstance(like, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(like, int):
        return int(float(value))  # type: ignore[arg-type]
    if isinstance(like, float):
        return float(value)  # type: ignore[arg-type]
    return str(value)


def load() -> dict[str, object]:
    """Defaults, overlaid by config.json's "ears" object, overlaid by EARS_* env."""
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(config_path().read_text())
        overrides = raw.get("ears", {}) if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        overrides = {}
    for key, value in overrides.items():
        if key in DEFAULTS:
            try:
                cfg[key] = _coerce(value, DEFAULTS[key])
            except (TypeError, ValueError):
                pass
    for key in DEFAULTS:
        env = os.environ.get("EARS_" + key.upper())
        if env is not None and env != "":
            try:
                cfg[key] = _coerce(env, DEFAULTS[key])
            except (TypeError, ValueError):
                pass
    if cfg["mode"] not in MODES:
        cfg["mode"] = DEFAULTS["mode"]
    return cfg


def write_json_atomic(path: Path, data: dict) -> None:
    """Replace ``path`` in one step so a reader never sees half a file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_control() -> dict:
    """Runtime switches: ``{"paused": bool, "mode": "live"|"notes", "speak": bool|None}``.

    ``speak`` is None until something has set it, so a caller can fall back to
    the configured default.
    """
    data = read_json(control_path())
    mode = data.get("mode")
    speak = data.get("speak")
    return {
        "paused": bool(data.get("paused", False)),
        "mode": mode if mode in MODES else None,
        "speak": speak if isinstance(speak, bool) else None,
    }


def write_control(**changes: object) -> dict:
    data = read_json(control_path())
    data.update(changes)
    data["updated_at"] = time.time()
    write_json_atomic(control_path(), data)
    return data


def state_path(pid: int) -> Path:
    return run_dir() / f"listener-{pid}.json"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_name(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def listeners() -> list[dict]:
    """State of every listener process that is still running.

    A listener that was killed outright leaves its state file behind, and may
    leave its speech server running. Both are cleaned up here.
    """
    found = []
    try:
        entries = sorted(run_dir().glob("listener-*.json"))
    except OSError:
        return found
    for path in entries:
        data = read_json(path)
        if pid_alive(int(data.get("pid") or 0)):
            found.append(data)
            continue
        orphan = int(data.get("whisper_pid") or 0)
        # Check the name: the pid may have been reused by something unrelated.
        if orphan and pid_alive(orphan) and "whisper-server" in _process_name(orphan):
            try:
                os.kill(orphan, signal.SIGTERM)
            except OSError:
                pass
        try:
            path.unlink()
        except OSError:
            pass
    return found

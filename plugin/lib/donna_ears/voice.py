"""Speech output, the part every command can import.

``ears say`` hands text to the voice server (``voice_server.py``), which keeps
a Kokoro model warm while a session wants spoken replies. This module is the
client side of that, the small state files the server shares with the listener,
and the fallback to the macOS built-in voice.

Standard library only, like ``config.py`` and for the same reason.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from . import config

MAX_TEXT = 1500  # characters: about a minute and a half of speech
SPEAKING_MAX_S = 300.0  # a "speaking" flag older than this is a leftover
START_TIMEOUT_S = 45.0  # how long `ears say` waits for a server that is loading
VOICE_NAME = re.compile(r"^[a-z]{2}_[a-z0-9]{2,24}$")

_FENCE = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_LINE_MARK = re.compile(r"^\s*(?:[-*+•]|\d+[.)]|#{1,6}|>)\s+")
_SENTENCE_END = ".!?…"


class VoiceError(RuntimeError):
    pass


# -------------------------------------------------------------------- text


def clean(text: str) -> str:
    """Text as it should be spoken: markup out, whitespace collapsed, capped.

    The model is asked to send plain sentences. This is for when it does not.
    """
    text = _FENCE.sub(" ", text)  # code blocks are for the screen
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    lines = []
    for line in text.splitlines():
        line = _LINE_MARK.sub("", line)
        line = re.sub(r"[*`~]+", "", line)
        line = re.sub(r"[_|]+", " ", line)
        line = " ".join(line.split())
        if line:
            lines.append(line)
    text = "\n".join(lines)
    if len(text) > MAX_TEXT:
        head = text[:MAX_TEXT]
        cut = max(head.rfind(mark) for mark in _SENTENCE_END + "\n")
        text = head[: cut + 1] if cut > MAX_TEXT // 2 else head
    return text.strip()


def chunks(text: str, size: int = 280) -> list[str]:
    """Sentences grouped for synthesis.

    The first sentence goes alone so that speech starts as soon as possible;
    the rest are grouped so the voice keeps its flow across short sentences.
    """
    sentences = []
    for part in re.split(r"(?<=[.!?…])\s+|\n+", text):
        part = part.strip()
        if part:
            sentences.append(part if part[-1] in _SENTENCE_END + ":;," else part + ".")
    if not sentences:
        return []
    grouped = [sentences[0]]
    current = ""
    for sentence in sentences[1:]:
        if current and len(current) + 1 + len(sentence) > size:
            grouped.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        grouped.append(current)
    return grouped


# ------------------------------------------------------- the speaking flag
#
# While a reply is being played this file exists. The listener reads it to
# know that what the microphone picks up may be the reply itself ("gate"), and
# whom to signal when the user starts talking over it.


def write_speaking(gate: bool, server: bool = False) -> None:
    config.write_json_atomic(
        config.speaking_path(),
        {"pid": os.getpid(), "since": time.time(), "gate": bool(gate), "server": bool(server)},
    )


def clear_speaking(force: bool = False) -> None:
    path = config.speaking_path()
    if not force and int(config.read_json(path).get("pid") or 0) != os.getpid():
        return
    try:
        path.unlink()
    except OSError:
        pass


def speaking() -> dict | None:
    """The flag, if a live process is speaking right now."""
    path = config.speaking_path()
    data = config.read_json(path)
    if not data:
        return None
    fresh = time.time() - float(data.get("since") or 0) < SPEAKING_MAX_S
    if fresh and config.pid_alive(int(data.get("pid") or 0)):
        return data
    try:
        path.unlink()  # whoever wrote it died mid-sentence
    except OSError:
        pass
    return None


class SpeakingWatch:
    """The speaking flag, cheap enough to ask about for every audio frame."""

    def __init__(self) -> None:
        self._signature: tuple | None = None
        self._data: dict | None = None
        self._checked = 0.0

    def get(self) -> dict | None:
        try:
            stat = os.stat(config.speaking_path())
            signature: tuple | None = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            signature = None
        now = time.monotonic()
        # Re-read when the file changes, and once a second while it exists so
        # that a speaker that died is noticed.
        if signature != self._signature or (self._data and now - self._checked > 1.0):
            self._signature = signature
            self._checked = now
            self._data = speaking() if signature else None
        return self._data


def interrupt() -> bool:
    """Stop the reply that is playing. True if there was one to stop."""
    state = speaking()
    if not state or not state.get("server"):
        return False
    try:
        os.kill(int(state["pid"]), signal.SIGUSR1)
    except (OSError, KeyError, ValueError):
        return False
    return True


# ------------------------------------------------------------ the server


def owner_pid() -> int:
    """The Claude Code process this command runs under, or 0 outside one."""
    value = os.environ.get("CLAUDE_PID", "")
    return int(value) if value.isdigit() else 0


def server_state() -> dict | None:
    """State of the voice server, if one is alive."""
    data = config.read_json(config.voice_state_path())
    if not data:
        return None
    if config.pid_alive(int(data.get("pid") or 0)):
        return data
    try:
        config.voice_state_path().unlink()
    except OSError:
        pass
    return None


def request(payload: dict, timeout: float = 5.0) -> dict:
    state = server_state()
    if not state or state.get("status") != "ready" or not state.get("port"):
        raise VoiceError("the voice server is not running")
    message = dict(payload, token=state.get("token", ""))
    buffer = b""
    try:
        address = ("127.0.0.1", int(state["port"]))
        with socket.create_connection(address, timeout=min(timeout, 5.0)) as conn:
            conn.settimeout(timeout)
            conn.sendall((json.dumps(message) + "\n").encode())
            while not buffer.endswith(b"\n"):
                part = conn.recv(65536)
                if not part:
                    break
                buffer += part
        reply = json.loads(buffer.decode() or "{}")
    except (OSError, ValueError) as exc:
        raise VoiceError(f"the voice server did not answer: {exc}") from exc
    if not isinstance(reply, dict) or not reply.get("ok"):
        error = reply.get("error") if isinstance(reply, dict) else None
        raise VoiceError(str(error or "the voice server refused the request"))
    return reply


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    home = os.path.expanduser("~")
    for candidate in (
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
        f"{home}/.local/bin/uv",
        f"{home}/.cargo/bin/uv",
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def server_script() -> Path:
    return Path(__file__).resolve().parent.parent / "speak.py"


def start_server() -> subprocess.Popen:
    """Start the voice server in the background, detached from this command."""
    uv = find_uv()
    if not uv:
        raise VoiceError("uv is not installed (brew install uv)")
    config.ensure_dirs()
    raw = config.ears_dir() / "voice-server.log"  # the libraries' own chatter
    try:
        if raw.stat().st_size > 1_000_000:
            raw.unlink()
    except OSError:
        pass
    with open(raw, "ab") as log:
        return subprocess.Popen(
            [uv, "run", "--quiet", "--script", str(server_script())],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=str(config.ears_dir()),
            start_new_session=True,
        )


def warm() -> str:
    """Make sure a server is loading or ready, without waiting for it."""
    state = server_state()
    if state:
        return str(state.get("status") or "loading")
    start_server()
    return "loading"


def ensure_server(timeout: float = START_TIMEOUT_S) -> dict:
    """State of a ready server, starting one and waiting for it if need be."""
    state = server_state()
    if state and state.get("status") == "ready":
        return state
    proc = None if state else start_server()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = server_state()
        if state and state.get("status") == "ready":
            return state
        if proc is not None and proc.poll() is not None and not state:
            raise VoiceError(f"the voice server could not start (see {config.voice_log_path()})")
        time.sleep(0.2)
    raise VoiceError("the voice is still loading")


def hush() -> int:
    """Stop the reply being spoken and drop the ones queued behind it."""
    try:
        return int(request({"op": "stop"}, timeout=2.0).get("stopped", 0))
    except VoiceError:
        return 0


def release(owner: int) -> None:
    """Tell the server that a session is leaving; it exits when none is left."""
    if owner and server_state():
        try:
            request({"op": "bye", "owner": owner}, timeout=0.5)
        except VoiceError:
            pass


def stop_server() -> bool:
    state = server_state()
    if not state:
        return False
    try:
        os.kill(int(state["pid"]), signal.SIGTERM)
    except (OSError, KeyError, ValueError):
        return False
    return True


# ----------------------------------------------------------------- speaking


def system_say(text: str, cfg: dict, note: str = "") -> dict:
    """Speak with the macOS built-in voice. Blocks until it has finished."""
    binary = shutil.which("say") or ("/usr/bin/say" if os.access("/usr/bin/say", os.X_OK) else "")
    if not binary:
        raise VoiceError(note or "no speech engine is available on this machine")
    command = [binary]
    if cfg["say_voice"]:
        command += ["-v", str(cfg["say_voice"])]
    speed = float(cfg["voice_speed"])
    if abs(speed - 1.0) > 0.01:
        command += ["-r", str(int(180 * speed))]
    # No way to tell headphones from speakers here, so assume speakers.
    gate = str(cfg["voice_gate"]).lower() not in ("off", "never", "false", "0")
    started = time.monotonic()
    write_speaking(gate)
    try:
        # The text goes in on stdin: an argument would show in `ps`.
        done = subprocess.run(command, input=text.encode(), timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise VoiceError(f"say failed: {exc}") from exc
    finally:
        clear_speaking()
    if done.returncode != 0:
        raise VoiceError(f"say exited with status {done.returncode}")
    return {
        "ok": True,
        "engine": "say",
        "seconds": round(time.monotonic() - started, 1),
        "note": note,
    }


def say(
    text: str,
    cfg: dict,
    voice_name: str = "",
    speed: float = 0.0,
    wait: bool = False,
    start_timeout: float = START_TIMEOUT_S,
) -> dict:
    """Speak ``text``. Kokoro when it can be had in time, the macOS voice otherwise."""
    text = clean(text)
    if not text:
        raise VoiceError("there is nothing to say")
    note = ""
    if str(cfg["voice_engine"]).lower() != "say":
        voice_name = voice_name or str(cfg["voice"])
        if not VOICE_NAME.match(voice_name):
            raise VoiceError(f"'{voice_name}' is not a voice name (they look like af_heart)")
        try:
            ensure_server(start_timeout)
            return request(
                {
                    "op": "say",
                    "text": text,
                    "voice": voice_name,
                    "speed": speed or float(cfg["voice_speed"]),
                    "wait": wait,
                    "owner": owner_pid(),
                },
                timeout=900.0 if wait else 10.0,
            )
        except VoiceError as exc:
            note = str(exc)
    return system_say(text, cfg, note)


def describe(result: dict) -> str:
    """One line about what `say` did, for whoever ran it."""
    engine = result.get("engine", "?")
    if engine == "say":
        line = f"ears: spoke with the macOS voice ({result.get('seconds', 0)}s)"
        if result.get("note"):
            line += f" because {result['note']}"
        return line
    name = f"{engine} {result.get('voice', '')}".strip()
    if "seconds" not in result:
        ahead = int(result.get("ahead") or 0)
        return f"ears: speaking ({name})" + (f", after {ahead} queued" if ahead else "")
    if result.get("error"):
        return f"ears: could not speak: {result['error']}"
    if result.get("interrupted"):
        return f"ears: interrupted after {result['seconds']}s ({name})"
    return (
        f"ears: spoke {result['seconds']}s ({name}, first sound after {result.get('latency', 0)}s)"
    )

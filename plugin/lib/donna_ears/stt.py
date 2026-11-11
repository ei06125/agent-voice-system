"""Speech to text through a local whisper.cpp server kept warm for the session.

``whisper-cli`` would have to load the model and initialise Metal for every
utterance, which costs seconds each time. ``whisper-server`` pays that once;
after it, a five second utterance comes back in about a second on Apple
silicon. The server listens on loopback only and audio never leaves the machine.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

from . import config
from .segmenter import SAMPLE_RATE

MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"

# whisper.cpp reports the detected language by name.
LANGUAGE_CODES = {
    "english": "en", "portuguese": "pt", "spanish": "es", "french": "fr",
    "german": "de", "italian": "it", "dutch": "nl", "catalan": "ca",
    "galician": "gl", "polish": "pl", "russian": "ru", "ukrainian": "uk",
    "turkish": "tr", "swedish": "sv", "norwegian": "no", "danish": "da",
    "finnish": "fi", "greek": "el", "czech": "cs", "romanian": "ro",
    "hungarian": "hu", "japanese": "ja", "chinese": "zh", "korean": "ko",
    "arabic": "ar", "hindi": "hi", "hebrew": "he",
}  # fmt: skip


class SttError(RuntimeError):
    pass


def find_server_binary() -> str | None:
    found = shutil.which("whisper-server")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/whisper-server", "/usr/local/bin/whisper-server"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def model_path(cfg: dict) -> Path:
    model = str(cfg["model"])
    if "/" in model or model.endswith(".bin"):
        return Path(model).expanduser()
    return config.models_dir() / f"ggml-{model}.bin"


def model_is_named(cfg: dict) -> bool:
    """True when the model is a name we know how to download, not a path."""
    model = str(cfg["model"])
    return not ("/" in model or model.endswith(".bin"))


def download_model(cfg: dict) -> Path:
    """Fetch a named model from the whisper.cpp repository. Resumes a partial file."""
    dest = model_path(cfg)
    curl = shutil.which("curl")
    if not curl:
        raise SttError("curl is not available to download the speech model")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    url = MODEL_URL.format(name=cfg["model"])
    result = subprocess.run(
        [curl, "-sSL", "--fail", "--retry", "3", "-C", "-", "-o", str(part), url],
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if result.returncode != 0:
        raise SttError(f"downloading {url} failed: {result.stderr.strip()[:200]}")
    os.replace(part, dest)
    return dest


def wav_bytes(samples) -> bytes:
    """16 kHz mono float samples in [-1, 1] as a 16-bit WAV file in memory."""
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _multipart(fields: dict[str, str], filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
        parts.append(f"{head}{value}\r\n".encode())
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode()
        + payload
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class WhisperServer:
    def __init__(self, cfg: dict, log) -> None:
        self.cfg = cfg
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self._log_handle = None
        # Loopback only: never route these requests through a configured proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 120.0) -> None:
        binary = find_server_binary()
        if not binary:
            raise SttError("whisper-server is not installed (brew install whisper-cpp)")
        model = model_path(self.cfg)
        if not model.exists():
            raise SttError(f"speech model not found: {model}")
        self.port = _free_port()
        command = [
            binary,
            "-m", str(model),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-t", str(int(self.cfg["whisper_threads"])),
            "-l", str(self.cfg["language"]),
            "-sns",  # suppress non-speech tokens such as [MUSIC]
        ]  # fmt: skip
        if self.cfg["prompt"]:
            command += ["--prompt", str(self.cfg["prompt"])]
        self._log_handle = open(config.ears_dir() / "whisper-server.log", "ab")
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(config.ears_dir()),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SttError(
                    f"whisper-server exited with code {self.proc.returncode} "
                    f"(see {config.ears_dir() / 'whisper-server.log'})"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    self.log.info("whisper-server ready pid=%s port=%s", self.proc.pid, self.port)
                    return
            except OSError:
                time.sleep(0.25)
        self.stop()
        raise SttError("whisper-server did not become ready in time")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None

    def kill(self) -> None:
        """Last resort, for a shutdown that has run out of time."""
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()

    def transcribe(self, samples, timeout: float = 120.0) -> tuple[str, str]:
        """Return ``(text, language_code)`` for one utterance of 16 kHz mono audio."""
        if not self.alive():
            raise SttError("whisper-server is not running")
        fields = {
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "response_format": "verbose_json",
            "language": str(self.cfg["language"]),
        }
        body, content_type = _multipart(fields, "utterance.wav", wav_bytes(samples))
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/inference",
            data=body,
            headers={"Content-Type": content_type},
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                data = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SttError(f"transcription request failed: {exc}") from exc
        if not isinstance(data, dict) or "error" in data:
            raise SttError(f"whisper-server answered with an error: {str(data)[:200]}")
        segments = data.get("segments") or []
        if segments:
            # Segments can break in the middle of a word; each one carries its
            # own leading space where a word starts, so join them with nothing.
            text = "".join(str(segment.get("text", "")) for segment in segments)
        else:
            text = str(data.get("text", ""))
        if str(self.cfg["language"]) != "auto":
            language = str(self.cfg["language"])
        else:
            name = str(data.get("detected_language") or data.get("language") or "").lower()
            language = LANGUAGE_CODES.get(name, name[:12])
        return text.strip(), language

    def warm_up(self) -> None:
        """Run half a second of silence through the model so the first real
        utterance does not pay for lazy initialisation."""
        import numpy as np

        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32), timeout=60.0)

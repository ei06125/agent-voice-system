"""The voice server: a Kokoro model kept warm, speaking what ``ears say`` sends.

Kokoro takes several seconds to load and a fraction of a second per sentence
after that, so one process holds the model while a session wants spoken
replies. ``ears say`` starts it on demand and talks to it over a loopback
socket; the token in the state file keeps other local processes out.

It lives by the listener's rules:

* It must not outlive the sessions using it. Every request names the Claude
  Code process it came from; the server exits when none of them is alive, and
  when nothing has been said for ``voice_idle_s``.
* What is spoken is never written to a log.
* Signal handlers only record. ``listen.py`` explains what happens otherwise.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import secrets
import signal
import socket
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import config, voice
from .lock import FileLock

REPO = "hexgrad/Kokoro-82M"
SAMPLE_RATE = 24000  # what Kokoro produces
BLOCK = 1200  # 50 ms: how soon an interruption takes effect
LEAD_IN_S = 0.15  # Bluetooth outputs swallow the first moment of a stream
TAIL_S = 0.2
STOP_DEADLINE_S = 5.0

# Output devices that do not leak into the microphone. Anything else is treated
# as a loudspeaker, and the listener ignores the microphone while it plays.
HEADPHONES = ("airpods", "headphone", "earphone", "earbud", "buds", "headset", "beats")


class Interrupted(Exception):
    pass


@dataclass
class Job:
    text: str
    voice: str
    speed: float
    done: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)


def make_logger() -> logging.Logger:
    log = logging.getLogger("ears.voice")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        handler = logging.handlers.RotatingFileHandler(
            config.voice_log_path(), maxBytes=1_000_000, backupCount=1
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
        log.addHandler(handler)
    return log


def _model_is_cached() -> bool:
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"), "hub"
    )
    snapshots = Path(hub) / ("models--" + REPO.replace("/", "--")) / "snapshots"
    try:
        return any(snapshots.glob("*/kokoro-v1_0.pth"))
    except OSError:
        return False


class KokoroEngine:
    name = "kokoro"

    def __init__(self, log: logging.Logger) -> None:
        self.log = log
        self.device = "cpu"
        self._model = None
        self._pipelines: dict = {}

    def load(self, voice_name: str) -> None:
        if _model_is_cached():
            # Do not ask the network whether a model we already have has
            # changed: that is a round trip at every start, and a hang offline.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        import torch
        from kokoro import KModel

        try:
            from loguru import logger

            logger.remove()  # Kokoro logs through loguru, and may log the text
        except Exception:  # noqa: BLE001
            pass
        # Kokoro only looks for CUDA; on Apple silicon it has to be told about Metal.
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self._model = KModel(repo_id=REPO).to(self.device).eval()
        # Metal compiles its kernels on first use. Pay for that now, not
        # during the first reply.
        for _ in self.synthesize("Ready when you are.", voice_name, 1.0):
            pass

    def _pipeline(self, voice_name: str):
        code = voice_name[0]
        if code not in self._pipelines:
            from kokoro import KPipeline

            self._pipelines[code] = KPipeline(lang_code=code, repo_id=REPO, model=self._model)
        pipeline = self._pipelines[code]
        try:
            pipeline.load_voice(voice_name)
        except Exception:  # noqa: BLE001
            # A voice that is not on disk yet, with the hub switched offline.
            import huggingface_hub.constants as hub

            if not hub.HF_HUB_OFFLINE:
                raise
            hub.HF_HUB_OFFLINE = False
            try:
                pipeline.load_voice(voice_name)
            finally:
                hub.HF_HUB_OFFLINE = True
        return pipeline

    def synthesize(self, text: str, voice_name: str, speed: float):
        """Yield the speech as float32 arrays at 24 kHz, a few sentences each."""
        import numpy as np

        pipeline = self._pipeline(voice_name)
        prepared = "\n".join(voice.chunks(text))
        for result in pipeline(prepared, voice=voice_name, speed=speed, split_pattern=r"\n+"):
            audio = result.audio
            if audio is None:
                continue
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            yield np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)


class Player:
    """The default output device, opened afresh for every reply so that a
    change of output (headphones in, headphones out) is followed."""

    def open(self):
        import sounddevice as sd

        sd._terminate()  # PortAudio lists devices once, when it starts
        sd._initialize()
        name = str(sd.query_devices(kind="output")["name"])
        stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        stream.start()
        return stream, name


class VoiceServer:
    def __init__(self, cfg: dict, log: logging.Logger, engine=None, player=None) -> None:
        self.cfg = cfg
        self.log = log
        self.engine = engine or KokoroEngine(log)
        self.player = player or Player()
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.owners: set[int] = set()
        first = voice.owner_pid()
        if first and config.pid_alive(first):
            self.owners.add(first)
        self.token = secrets.token_hex(16)
        self.port = 0
        self.device_name = ""
        self.started_at = time.time()
        self.last_used = time.monotonic()
        self.spoken = 0
        self.stop = threading.Event()
        self._done = threading.Event()
        self._signalled: int | None = None
        self._interrupt = False
        self._busy = False

    # --------------------------------------------------------------- lifetime

    def _stopping(self) -> bool:
        return self._signalled is not None or self.stop.is_set()

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        self._signalled = signum  # nothing else: handlers nest (see listen.py)

    def _on_interrupt(self, signum, frame) -> None:  # noqa: ARG002
        self._interrupt = True  # the listener heard the user talk over us

    def _relay_signals(self, fd: int) -> None:
        while True:
            try:
                data = os.read(fd, 32)
            except OSError:
                return
            if not data:
                return
            if any(byte != signal.SIGUSR1 for byte in data):
                self.stop.set()

    def _watchdog(self) -> None:
        idle_limit = float(self.cfg["voice_idle_s"])
        while not self.stop.wait(1.0):
            idle = time.monotonic() - self.last_used
            if self._signalled is not None:
                self.stop.set()
            elif self.owners and not any(config.pid_alive(pid) for pid in list(self.owners)):
                self.log.info("no session left: stopping")
                self.stop.set()
            elif idle_limit > 0 and idle > idle_limit and not self._busy and self.jobs.empty():
                self.log.info("unused for %ss: stopping", int(idle))
                self.stop.set()
        if not self._done.wait(STOP_DEADLINE_S):
            self.log.error("shutdown exceeded %ss: exiting forcibly", STOP_DEADLINE_S)
            os._exit(0)

    def _write_state(self, status: str) -> None:
        config.write_json_atomic(
            config.voice_state_path(),
            {
                "pid": os.getpid(),
                "status": status,
                "port": self.port,
                "token": self.token,
                "engine": self.engine.name,
                "voice": str(self.cfg["voice"]),
                "started_at": self.started_at,
                "updated_at": time.time(),
            },
        )

    def run(self) -> int:
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self._on_signal)
        signal.signal(signal.SIGUSR1, self._on_interrupt)
        threading.Thread(
            target=self._relay_signals, args=(read_fd,), name="signals", daemon=True
        ).start()
        return self.serve()

    def serve(self) -> int:
        lock = FileLock(config.voice_lock_path())
        if not lock.try_acquire():
            self.log.info("another voice server is running: leaving it to it")
            self._done.set()
            return 0
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        listener = None
        try:
            self._write_state("loading")
            started = time.monotonic()
            self.engine.load(str(self.cfg["voice"]))
            self.log.info(
                "voice server ready in %.1fs: %s on %s, owners %s",
                time.monotonic() - started,
                self.engine.name,
                getattr(self.engine, "device", "?"),
                sorted(self.owners),
            )
            if self._stopping():
                return 0
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            listener.settimeout(0.5)
            self.port = listener.getsockname()[1]
            threading.Thread(target=self._work, name="speaker", daemon=True).start()
            self.last_used = time.monotonic()
            self._write_state("ready")
            while not self._stopping():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            return 0
        except Exception:  # noqa: BLE001
            self.log.exception("voice server crashed")
            return 1
        finally:
            self.stop.set()
            self._interrupt = True
            deadline = time.monotonic() + 1.0
            while self._busy and time.monotonic() < deadline:
                time.sleep(0.02)  # let the reply in progress let go of the device
            if listener is not None:
                listener.close()
            voice.clear_speaking()
            if int(config.read_json(config.voice_state_path()).get("pid") or 0) == os.getpid():
                try:
                    config.voice_state_path().unlink()
                except OSError:
                    pass
            lock.release()
            self.log.info("voice server stop: %s replies spoken", self.spoken)
            self._done.set()

    # --------------------------------------------------------------- requests

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(5.0)
                buffer = b""
                while not buffer.endswith(b"\n") and len(buffer) < 65536:
                    part = conn.recv(65536)
                    if not part:
                        break
                    buffer += part
                message = json.loads(buffer.decode())
                if not isinstance(message, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError):
                reply = {"ok": False, "error": "malformed request"}
            else:
                if secrets.compare_digest(str(message.get("token", "")), self.token):
                    reply = self._dispatch(message)
                else:
                    reply = {"ok": False, "error": "not authorised"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass

    def _dispatch(self, message: dict) -> dict:
        op = message.get("op")
        owner = message.get("owner")
        if isinstance(owner, int) and owner > 1 and op != "bye" and config.pid_alive(owner):
            self.owners.add(owner)
        self.last_used = time.monotonic()
        if op == "say":
            return self._say(message)
        if op == "stop":
            was_busy = self._busy
            self._interrupt = True
            return {"ok": True, "stopped": self._drain() + (1 if was_busy else 0)}
        if op == "status":
            return {
                "ok": True,
                "engine": self.engine.name,
                "voice": str(self.cfg["voice"]),
                "speaking": self._busy,
                "queued": self.jobs.qsize(),
                "spoken": self.spoken,
                "device": self.device_name,
                "owners": sorted(self.owners),
            }
        if op == "bye":
            self.owners.discard(owner if isinstance(owner, int) else 0)
            if not any(config.pid_alive(pid) for pid in list(self.owners)):
                self.log.info("last session said goodbye: stopping")
                self.stop.set()
            return {"ok": True}
        if op == "shutdown":
            self.stop.set()
            return {"ok": True}
        return {"ok": False, "error": f"unknown request '{op}'"}

    def _say(self, message: dict) -> dict:
        text = voice.clean(str(message.get("text") or ""))
        name = str(message.get("voice") or self.cfg["voice"])
        if not text:
            return {"ok": False, "error": "there is nothing to say"}
        if not voice.VOICE_NAME.match(name):
            return {"ok": False, "error": f"'{name}' is not a voice name"}
        try:
            speed = float(message.get("speed") or self.cfg["voice_speed"])
        except (TypeError, ValueError):
            speed = 1.0
        job = Job(text, name, min(2.0, max(0.5, speed)))
        ahead = self.jobs.qsize() + (1 if self._busy else 0)
        self.jobs.put(job)
        described = {"engine": self.engine.name, "voice": name}
        if not message.get("wait"):
            return {"ok": True, "ahead": ahead, **described}
        while not job.done.wait(0.5):
            if self._stopping():
                return {"ok": False, "error": "the voice server is stopping"}
        if job.result.get("error"):
            return {"ok": False, "error": job.result["error"]}
        return {"ok": True, **described, **job.result}

    def _drain(self) -> int:
        dropped = 0
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                return dropped
            job.result = {"interrupted": True, "seconds": 0.0, "latency": 0.0}
            job.done.set()
            dropped += 1

    # --------------------------------------------------------------- speaking

    def _gate(self, device: str) -> bool:
        """Whether the listener should ignore the microphone during this reply."""
        mode = str(self.cfg["voice_gate"]).lower()
        if mode in ("on", "always", "true", "1"):
            return True
        if mode in ("off", "never", "false", "0"):
            return False
        return not any(word in device.lower() for word in HEADPHONES)

    def _work(self) -> None:
        while not self._stopping():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self._busy = True
            try:
                self._speak(job)
            finally:
                self._busy = False
                self.last_used = time.monotonic()

    def _play(self, stream, samples) -> float:
        for start in range(0, len(samples), BLOCK):
            if self._interrupt or self._stopping():
                raise Interrupted
            stream.write(samples[start : start + BLOCK])
        return len(samples) / SAMPLE_RATE

    def _speak(self, job: Job) -> None:
        import numpy as np

        self._interrupt = False
        started = time.monotonic()
        stream = None
        seconds = 0.0
        latency = 0.0
        synthesis = 0.0
        interrupted = False
        error = ""
        # An output device, a Bluetooth one above all, takes about as long to
        # open as the first sentence takes to synthesise. Do both at once.
        output: dict = {}

        def open_output() -> None:
            try:
                output["stream"], output["device"] = self.player.open()
            except Exception as exc:  # noqa: BLE001 - reported by the speaker thread
                output["error"] = exc

        opener = threading.Thread(target=open_output, name="output", daemon=True)
        opener.start()
        try:
            for samples in self.engine.synthesize(job.text, job.voice, job.speed):
                if self._interrupt or self._stopping():
                    raise Interrupted
                if stream is None:
                    synthesis = time.monotonic() - started
                    opener.join()
                    if "error" in output:
                        raise output["error"]
                    stream, self.device_name = output["stream"], output["device"]
                    voice.write_speaking(self._gate(self.device_name), server=True)
                    latency = time.monotonic() - started
                    self._play(stream, np.zeros(int(LEAD_IN_S * SAMPLE_RATE), dtype=np.float32))
                seconds += self._play(stream, samples)
            if stream is not None:
                self._play(stream, np.zeros(int(TAIL_S * SAMPLE_RATE), dtype=np.float32))
        except Interrupted:
            interrupted = True
            self._drain()  # whoever interrupts does not want the rest either
        except Exception as exc:  # noqa: BLE001
            # The message may quote the text, so only the type is logged.
            error = type(exc).__name__
            frames = "".join(traceback.format_tb(exc.__traceback__)[-4:])
            self.log.error("speaking failed: %s\n%s", error, frames)
        finally:
            opener.join(timeout=5.0)
            stream = stream or output.get("stream")  # opened, but nothing to play
            if stream is not None:
                try:
                    if interrupted or error:
                        stream.abort()  # drop what is buffered: silence now
                    else:
                        stream.stop()  # play what is buffered, then stop
                    stream.close()
                except Exception:  # noqa: BLE001
                    self.log.warning("closing the output failed")
            voice.clear_speaking()
            self.spoken += 1
            job.result = {
                "seconds": round(seconds, 1),
                "latency": round(latency, 2),
                "interrupted": interrupted,
            }
            if error:
                job.result["error"] = f"speech synthesis failed ({error})"
            job.done.set()
            self.log.info(
                "reply of %s chars: %.1fs of speech, first sound after %.2fs "
                "(first sentence synthesised in %.2fs)%s",
                len(job.text),
                seconds,
                latency,
                synthesis,
                " (interrupted)" if interrupted else f" ({error})" if error else "",
            )


def main() -> int:
    config.ensure_dirs()
    code = VoiceServer(config.load(), make_logger()).run()
    logging.shutdown()
    os._exit(code)  # no finalisers: an audio library's exit hook must not hold us up

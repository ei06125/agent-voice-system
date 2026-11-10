"""Always-on local voice coordinator."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from donna.orchestration.voice_queue import DEFAULT_STATE_DIR

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL = "qwen3.8:27b-mlx"
LOCAL_PROVIDER = "ollama"
LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LOG_PATH = DEFAULT_STATE_DIR / "utterance-log.jsonl"
DEFAULT_TRIGGER_PATH = DEFAULT_STATE_DIR / "donna-mentioned.json"
DEFAULT_PID_PATH = Path.home() / ".claude" / "scripts" / ".voice-daemon.pid"


class Speaker(Protocol):
    def speak(self, text: str) -> None: ...


class Agent(Protocol):
    def reply(self, text: str) -> str: ...


@dataclass(frozen=True)
class TranscriptBatch:
    text: str
    start_offset: int
    end_offset: int
    started_at: float
    ended_at: float


class TranscriptCursor:
    def __init__(self, log_path: Path, state_path: Path) -> None:
        self.log_path = log_path
        self.state_path = state_path
        self.offset = 0
        self.last_read_ts = 0.0
        if state_path.exists():
            state = json.loads(state_path.read_text())
            self.offset = int(state["offset"])
            self.last_read_ts = float(state["last_read_ts"])

    def initialize_at_end(self) -> None:
        if self.state_path.exists():
            return
        self.offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        self.commit(self.offset, time.time())

    def read_until(self, cutoff: float) -> TranscriptBatch | None:
        if not self.log_path.exists():
            return None
        entries: list[tuple[float, str]] = []
        end_offset = self.offset
        with self.log_path.open("rb") as stream:
            stream.seek(self.offset)
            while True:
                line_start = stream.tell()
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                record = json.loads(line)
                timestamp = float(record["ts"])
                if timestamp > cutoff:
                    stream.seek(line_start)
                    break
                end_offset = stream.tell()
                text = str(record["text"]).strip()
                playback_overlap = bool(record.get("playback_overlap"))
                wake_name = "donna" in text.casefold()
                if text and (not playback_overlap or wake_name):
                    entries.append((timestamp, text))
        if not entries:
            if end_offset != self.offset:
                self.commit(end_offset, cutoff)
            return None
        lines = [f"[{timestamp:.6f}] {text}" for timestamp, text in entries]
        prompt = (
            "The microphone was continuously active. Treat the following as one "
            f"ordered audio interval ({self.last_read_ts:.6f}, {cutoff:.6f}].\n"
            + "\n".join(lines)
        )
        return TranscriptBatch(
            text=prompt,
            start_offset=self.offset,
            end_offset=end_offset,
            started_at=self.last_read_ts,
            ended_at=cutoff,
        )

    def commit(self, offset: int, timestamp: float) -> None:
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"offset": offset, "last_read_ts": timestamp})
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.state_path)
        self.offset = offset
        self.last_read_ts = timestamp


class TriggerWatcher:
    def __init__(self, path: Path, poll_interval: float = 0.1) -> None:
        self.path = path
        self.poll_interval = poll_interval
        self.last_token = self._token()

    def _token(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def wait(self) -> float:
        while True:
            token = self._token()
            if token is not None and token != self.last_token:
                self.last_token = token
                return float(json.loads(self.path.read_text())["ts"])
            time.sleep(self.poll_interval)


class KokoroLocalSpeaker:
    def __init__(self, voice: str = "af_heart") -> None:
        self.voice = voice
        self.python = Path(
            os.environ.get(
                "KOKORO_LOCAL_PYTHON",
                Path.home() / ".mcp" / "kokoro-local" / ".venv" / "bin" / "python",
            )
        )
        self.output_dir = DEFAULT_STATE_DIR / "voice-loop-audio"
        self.process: subprocess.Popen[str] | None = None
        self._start_worker()

    def _start_worker(self) -> None:
        environment = os.environ.copy()
        environment["HF_HUB_OFFLINE"] = "1"
        environment["TRANSFORMERS_OFFLINE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(PROJECT_ROOT / "src"), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        self.process = subprocess.Popen(
            [str(self.python), "-m", "donna.text_to_speech.speak_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=environment,
        )

    def _read_response(self) -> dict:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Kokoro worker is not running")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(
                f"Kokoro worker exited with status {self.process.poll()}"
            )
        return json.loads(line)

    def speak(self, text: str) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Kokoro worker is not running")
        if not getattr(self, "_ready", False):
            response = self._read_response()
            if not response.get("ready"):
                raise RuntimeError("Kokoro worker failed to initialize")
            self._ready = True
        self.process.stdin.write(
            json.dumps(
                {
                    "text": text,
                    "voice": self.voice,
                    "output_dir": str(self.output_dir),
                }
            )
            + "\n"
        )
        self.process.stdin.flush()
        self._read_response()

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        self.process.wait(timeout=5)


class HermesLocalAgent:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        cwd: Path = PROJECT_ROOT,
        executable: str | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.model = model
        self.cwd = cwd
        self.executable = executable or shutil.which("hermes") or "hermes"
        self.state_dir = state_dir or DEFAULT_STATE_DIR / "hermes-local"
        self.session_id: str | None = None

    def command(self, text: str, usage_path: Path) -> list[str]:
        prompt = (
            "This is a local voice conversation. Respond concisely in plain text "
            "without Markdown. Do not address yourself by name.\n\n" + text
        )
        command = [
            self.executable,
            "-z",
            prompt,
            "--provider",
            LOCAL_PROVIDER,
            "-m",
            self.model,
            "--safe-mode",
            "--reasoning",
            "none",
            "--toolsets",
            "context_engine",
            "--usage-file",
            str(usage_path),
        ]
        if self.session_id:
            command.extend(["--resume", self.session_id, "--no-restore-cwd"])
        return command

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(self.state_dir)
        environment["OLLAMA_API_KEY"] = "ollama"
        environment["OLLAMA_BASE_URL"] = LOCAL_BASE_URL
        environment["AWS_PROFILE"] = "__donna_local_only__"
        environment["AWS_DEFAULT_PROFILE"] = "__donna_local_only__"
        for key in (
            "ANTHROPIC_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "ELEVENLABS_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GROQ_API_KEY",
            "MISTRAL_API_KEY",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "XAI_API_KEY",
        ):
            environment.pop(key, None)
        return environment

    def _validate_usage(self, usage: dict) -> None:
        if not usage.get("completed") or usage.get("failed"):
            raise RuntimeError("Hermes did not complete the local turn")
        if usage.get("model") != self.model:
            raise RuntimeError(f"Hermes used unexpected model {usage.get('model')!r}")
        if usage.get("estimated_cost_usd") not in (0, 0.0):
            raise RuntimeError("Hermes reported non-zero inference cost")

    def reply(self, text: str) -> str:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".json", dir=self.state_dir, delete=False
        ) as handle:
            usage_path = Path(handle.name)
        try:
            result = subprocess.run(
                self.command(text, usage_path),
                cwd=self.cwd,
                env=self._environment(),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"Hermes local turn failed: {error}")
            usage = json.loads(usage_path.read_text())
            self._validate_usage(usage)
            self.session_id = usage.get("session_id") or self.session_id
            reply = result.stdout.strip()
            if not reply:
                raise RuntimeError("Hermes returned an empty reply")
            return reply
        finally:
            usage_path.unlink(missing_ok=True)


class ListenerProcess:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        if DEFAULT_PID_PATH.exists():
            try:
                pid = int(DEFAULT_PID_PATH.read_text())
                os.kill(pid, 0)
            except (OSError, ValueError):
                pass
            else:
                raise RuntimeError(
                    f"listener already running as pid {pid}; use --no-start-listener"
                )
        environment = os.environ.copy()
        environment["DONNA_VOICE_QUEUE_MODE"] = "deliver"
        environment["STT_TRIGGER_MODE"] = "always"
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "donna.speech_to_text.listen_daemon",
            ],
            env=environment,
        )

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        self.process.wait(timeout=5)
        try:
            if int(DEFAULT_PID_PATH.read_text()) == self.process.pid:
                DEFAULT_PID_PATH.unlink()
        except (FileNotFoundError, ValueError):
            pass


class VoiceLoop:
    def __init__(
        self,
        cursor: TranscriptCursor,
        watcher: TriggerWatcher,
        speaker: Speaker,
        agent: Agent,
    ) -> None:
        self.cursor = cursor
        self.watcher = watcher
        self.speaker = speaker
        self.agent = agent

    def run_once(self) -> bool:
        cutoff = self.watcher.wait()
        batch = self.cursor.read_until(cutoff)
        if batch is None:
            return False
        print(batch.text, flush=True)
        reply = self.agent.reply(batch.text)
        self.cursor.commit(batch.end_offset, batch.ended_at)
        print(f"Hermes: {reply}", flush=True)
        self.speaker.speak(reply)
        return True

    def run(self) -> None:
        while True:
            self.run_once()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--conversation", default="hermes-local")
    parser.add_argument("--no-start-listener", action="store_true")
    parser.add_argument("--replay-existing", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    cursor = TranscriptCursor(
        DEFAULT_LOG_PATH,
        DEFAULT_STATE_DIR / f"{args.conversation}-last-read.json",
    )
    if not args.replay_existing:
        cursor.initialize_at_end()
    watcher = TriggerWatcher(DEFAULT_TRIGGER_PATH)
    listener = ListenerProcess()
    speaker = KokoroLocalSpeaker(args.voice)
    if not args.no_start_listener:
        listener.start()
    try:
        print("[voice-loop] ready; speak and pause to submit an interval", flush=True)
        VoiceLoop(
            cursor,
            watcher,
            speaker,
            HermesLocalAgent(args.model),
        ).run()
    finally:
        listener.stop()
        speaker.close()


if __name__ == "__main__":
    main()

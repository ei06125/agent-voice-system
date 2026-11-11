"""End to end: real speech audio through the real listener process.

The audio comes from the macOS speech synthesiser and is played from a file, so
nothing here opens a microphone. Skipped where the pieces are missing (the
speech model is 1.6 GB and is not downloaded by the test suite).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = str(REPO / "plugin" / "bin" / "ears")
MODEL = Path(
    os.environ.get("DONNA_TEST_MODEL") or "~/.donna/models/ggml-large-v3-turbo.bin"
).expanduser()

missing = [tool for tool in ("say", "ffmpeg", "uv", "whisper-server") if not shutil.which(tool)]
pytestmark = pytest.mark.skipif(
    bool(missing) or not MODEL.exists(),
    reason=f"needs {', '.join(missing) or MODEL} for end-to-end speech tests",
)


@pytest.fixture(scope="session")
def audio(tmp_path_factory):
    """Synthesised speech as 16 kHz mono WAV files, plus stretches of silence."""
    directory = tmp_path_factory.mktemp("audio")

    def speak(name: str, text: str) -> Path:
        aiff = directory / f"{name}.aiff"
        wav = directory / f"{name}.wav"
        subprocess.run(["say", "-o", str(aiff), text], check=True, timeout=60)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(aiff),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(wav),
            ],  # fmt: skip
            check=True,
            timeout=60,
        )
        return wav

    def silence(name: str, seconds: float) -> Path:
        wav = directory / f"{name}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=16000:cl=mono",
                "-t",
                str(seconds),
                "-c:a",
                "pcm_s16le",
                str(wav),
            ],  # fmt: skip
            check=True,
            timeout=60,
        )
        return wav

    def join(name: str, *parts: Path) -> Path:
        listing = directory / f"{name}.txt"
        listing.write_text("".join(f"file '{part}'\n" for part in parts))
        wav = directory / f"{name}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                str(wav),
            ],  # fmt: skip
            check=True,
            timeout=60,
        )
        return wav

    one = speak("one", "Remind me to review the pull request this afternoon.")
    two = speak("two", "The deploy is scheduled for Friday.")
    three = speak("three", "Hey Claude, what is the current git branch?")
    return {
        "one": one,
        "conversation": join(
            "conversation", one, silence("gap1", 1.0), two, silence("gap5", 5.0), three
        ),
        "silence": silence("quiet", 6.0),
    }


def environment(home: Path, owner: int | None = None, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EARS_")}
    env.update(DONNA_HOME=str(home), EARS_MODEL=str(MODEL), CLAUDE_CODE_SESSION_ID="e2e-session")
    env["CLAUDE_PID"] = str(owner or os.getpid())
    env.pop("CLAUDE_CODE_SESSION_ATTENDED", None)
    env.update(extra)
    return env


def listen(home: Path, wav: Path, *flags: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [LAUNCHER, "listen", "--input-file", str(wav), "--exit-at-eof", *flags],
        capture_output=True,
        text=True,
        timeout=180,
        env=environment(home, **extra),
    )


def start(home: Path, wav: Path, owner: int | None = None, **extra: str) -> subprocess.Popen:
    """A listener that keeps "hearing" silence after the file ends, like a live mic."""
    return subprocess.Popen(
        [LAUNCHER, "listen", "--input-file", str(wav)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=environment(home, owner, **extra),
        start_new_session=True,  # its own process group, as Claude Code gives a monitor
    )


def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def state(home: Path) -> dict:
    for path in (home / "ears" / "run").glob("listener-*.json"):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return {}


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def whisper_servers(home: Path) -> list[int]:
    """Speech servers started by listeners using this DONNA_HOME."""
    out = subprocess.run(["pgrep", "-f", "whisper-server"], capture_output=True, text=True).stdout
    mine = []
    for pid in out.split():
        cwd = subprocess.run(
            ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True
        ).stdout
        if str(home) in cwd:
            mine.append(int(pid))
    return mine


# ------------------------------------------------------------------ speech


def test_speech_comes_out_as_one_event_line(donna_home, audio):
    result = listen(donna_home, audio["one"], "--fast")
    assert result.returncode == 0, result.stderr
    (line,) = result.stdout.strip().splitlines()
    assert line.startswith("[heard ")
    assert "pull request" in line.lower()
    # ...and it is on record for `ears tail`
    (transcript,) = (donna_home / "ears" / "sessions").glob("*.jsonl")
    record = json.loads(transcript.read_text().splitlines()[0])
    assert "pull request" in record["text"].lower() and record["lang"] == "en"
    assert transcript.stat().st_mode & 0o077 == 0


def test_a_pause_ends_a_batch_and_a_short_gap_does_not(donna_home, audio):
    """Real time: two sentences one second apart arrive together; the sentence
    after a five second silence arrives on its own."""
    process = subprocess.Popen(
        [LAUNCHER, "listen", "--input-file", str(audio["conversation"]), "--exit-at-eof"],
        stdout=subprocess.PIPE,
        text=True,
        env=environment(donna_home),
    )
    arrivals = []
    for line in process.stdout:
        arrivals.append((time.monotonic(), line.strip()))
    assert process.wait(timeout=60) == 0
    assert len(arrivals) == 3
    (t1, first), (t2, second), (t3, third) = arrivals
    assert "pull request" in first.lower() and "friday" in second.lower()
    assert "branch" in third.lower()
    assert t2 - t1 < 0.2, "the first two sentences belong to one event"
    assert t3 - t2 > 3.0, "the third sentence belongs to a later event"


def test_silence_produces_no_events(donna_home, audio):
    result = listen(donna_home, audio["silence"], "--fast")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_stdout_carries_events_and_nothing_else(donna_home, audio):
    """Every stdout line wakes the model, so diagnostics must never leak there."""
    result = listen(donna_home, audio["one"], "--fast")
    for line in result.stdout.strip().splitlines():
        assert line.startswith(("[heard ", "[ears"))
    log = (donna_home / "ears" / "ears.log").read_text()
    assert "listener start" in log and "pushed 1 utterance" in log
    assert "pull request" not in log.lower(), "what was said does not belong in the log"


# --------------------------------------------------------------- lifecycle


def test_sigterm_closes_everything_promptly(donna_home, audio):
    """What Claude Code does to a monitor when the session ends."""
    process = start(donna_home, audio["silence"])
    try:
        wait_for(lambda: state(donna_home).get("status") == "listening", 60, "listening")
        assert whisper_servers(donna_home)
        began = time.monotonic()
        os.killpg(process.pid, signal.SIGTERM)
        assert process.wait(timeout=15) == 0
        assert time.monotonic() - began < 8
        wait_for(lambda: not whisper_servers(donna_home), 10, "the speech server to exit")
        assert not state(donna_home), "the state file is removed on the way out"
    finally:
        stop(process)


def test_the_listener_stops_when_its_session_dies_without_saying_goodbye(donna_home, audio):
    """Claude Code killed outright sends no signal: the monitor is orphaned and
    would keep the microphone open. The watchdog is what prevents that."""
    session = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    process = start(donna_home, audio["silence"], owner=session.pid)
    try:
        wait_for(lambda: state(donna_home).get("status") == "listening", 60, "listening")
        session.kill()
        session.wait()
        assert process.wait(timeout=15) == 0
        wait_for(lambda: not whisper_servers(donna_home), 10, "the speech server to exit")
    finally:
        session.kill()
        stop(process)


def test_only_one_session_hears_and_the_next_takes_over(donna_home, audio):
    first = start(donna_home, audio["silence"])
    second = None
    try:
        wait_for(lambda: state(donna_home).get("status") == "listening", 60, "first listener")
        second = start(donna_home, audio["silence"])

        def statuses():
            found = {}
            for path in (donna_home / "ears" / "run").glob("listener-*.json"):
                try:
                    data = json.loads(path.read_text())
                    found[data["pid"]] = data["status"]
                except (OSError, ValueError, KeyError):
                    pass
            return found

        wait_for(lambda: sorted(statuses().values()) == ["listening", "standby"], 30, "standby")
        assert len(whisper_servers(donna_home)) == 1, "a standby listener loads no model"
        stop(first)
        wait_for(lambda: list(statuses().values()) == ["listening"], 60, "takeover")
    finally:
        stop(first)
        if second:
            stop(second)


def test_pause_closes_the_source_and_resume_reopens_it(donna_home, audio):
    process = start(donna_home, audio["silence"])
    cli = [LAUNCHER]
    env = environment(donna_home)
    try:
        wait_for(lambda: state(donna_home).get("status") == "listening", 60, "listening")
        paused = subprocess.run(
            cli + ["pause"], capture_output=True, text=True, env=env, timeout=30
        )
        assert paused.returncode == 0 and "the microphone is closed." in paused.stdout
        assert state(donna_home)["status"] == "paused"
        status = subprocess.run(
            cli + ["status"], capture_output=True, text=True, env=env, timeout=30
        )
        assert "ears: paused" in status.stdout
        resumed = subprocess.run(
            cli + ["resume"], capture_output=True, text=True, env=env, timeout=30
        )
        assert "listening again" in resumed.stdout
        assert state(donna_home)["status"] == "listening"
    finally:
        stop(process)


def test_a_crashed_listener_is_started_again(donna_home, audio):
    """Claude Code never restarts a monitor, so the launcher has to."""
    process = start(donna_home, audio["silence"])
    try:
        first = wait_for(
            lambda: state(donna_home).get("status") == "listening" and state(donna_home)["pid"],
            60,
            "listening",
        )
        os.kill(first, signal.SIGKILL)
        second = wait_for(
            lambda: (
                state(donna_home).get("status") == "listening"
                and state(donna_home)["pid"] != first
                and state(donna_home)["pid"]
            ),
            60,
            "the replacement listener",
        )
        assert second != first
        assert process.poll() is None, "the launcher itself stayed up"
        wait_for(
            lambda: len(whisper_servers(donna_home)) == 1, 15, "the orphaned server to be reaped"
        )
    finally:
        stop(process)


def test_a_missing_model_is_reported_once_and_not_retried(donna_home, audio):
    result = listen(donna_home, audio["one"], "--fast", EARS_MODEL=str(donna_home / "nope.bin"))
    assert result.returncode == 75
    (line,) = result.stdout.strip().splitlines()
    assert line.startswith("[ears error] speech model not found")


def test_an_unattended_session_never_opens_a_source(donna_home, audio):
    process = start(donna_home, audio["one"], CLAUDE_CODE_SESSION_ATTENDED="0")
    try:
        wait_for(lambda: state(donna_home).get("status") == "standby", 60, "standby")
        time.sleep(2.0)
        assert state(donna_home)["detail"] == "unattended session"
        assert not whisper_servers(donna_home)
    finally:
        stop(process)


# ------------------------------------------------------------ speaking back


def speaking_flag(home: Path, pid: int, gate: bool) -> None:
    """What the voice server writes while a reply is being played."""
    path = home / "ears" / "run" / "speaking.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "since": time.time(), "gate": gate, "server": True}))


def test_a_reply_through_loudspeakers_is_not_heard_as_speech(donna_home, audio):
    """Voice detection cannot tell a spoken reply from the user. While one
    plays through loudspeakers the microphone is ignored, or the listener
    would hand the model its own words as something the user said."""
    speaking_flag(donna_home, os.getpid(), gate=True)
    result = listen(donna_home, audio["one"], "--fast")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert "gate on" in (donna_home / "ears" / "ears.log").read_text()


def test_talking_over_a_reply_in_headphones_interrupts_it(donna_home, audio, tmp_path):
    """Headphones do not leak into the microphone, so it stays open: the user
    is still heard, and the voice server is told to stop talking."""
    marker = tmp_path / "interrupted"
    stand_in = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib, signal, sys, time\n"
            "signal.signal(signal.SIGUSR1, lambda *_: pathlib.Path(sys.argv[1]).write_text('x'))\n"
            "print('ready', flush=True)\n"
            "time.sleep(120)\n",
            str(marker),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert stand_in.stdout.readline().strip() == "ready"
        speaking_flag(donna_home, stand_in.pid, gate=False)
        result = listen(donna_home, audio["one"], "--fast")
        assert result.returncode == 0, result.stderr
        (line,) = result.stdout.strip().splitlines()
        assert "pull request" in line.lower()
        wait_for(marker.exists, 5, "the voice server to be interrupted")
        assert "barge-in" in (donna_home / "ears" / "ears.log").read_text()
    finally:
        stand_in.kill()
        stand_in.wait()

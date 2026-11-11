"""The voice server with a stand-in for Kokoro and for the loudspeaker.

What matters here is the behaviour around the model: replies in order, silence
on request, the flag the listener watches, and a server that goes away when
the sessions using it do.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from donna_ears import config, voice, voice_server

np = pytest.importorskip("numpy")

RATE = voice_server.SAMPLE_RATE


class FakeEngine:
    name = "fake"
    device = "test"

    def __init__(self, seconds: float = 0.2, fail: bool = False) -> None:
        self.seconds = seconds
        self.fail = fail
        self.loaded = ""
        self.spoken: list[tuple[str, str, float]] = []

    def load(self, voice_name: str) -> None:
        self.loaded = voice_name

    def synthesize(self, text: str, voice_name: str, speed: float):
        self.spoken.append((text, voice_name, speed))
        if self.fail:
            raise RuntimeError(f"cannot pronounce: {text}")
        for _ in range(2):
            yield np.zeros(int(self.seconds * RATE / 2), dtype=np.float32)


class FakeStream:
    def __init__(self, realtime: bool) -> None:
        self.realtime = realtime
        self.samples = 0
        self.ended = ""

    def write(self, block) -> None:
        self.samples += len(block)
        if self.realtime:
            time.sleep(len(block) / RATE)

    def stop(self) -> None:
        self.ended = "played out"

    def abort(self) -> None:
        self.ended = "cut off"

    def close(self) -> None:
        pass


class FakePlayer:
    def __init__(self, device: str = "Desk Speakers", realtime: bool = False) -> None:
        self.device = device
        self.realtime = realtime
        self.streams: list[FakeStream] = []

    def open(self):
        stream = FakeStream(self.realtime)
        self.streams.append(stream)
        return stream, self.device


def cfg(**overrides):
    merged = dict(config.DEFAULTS)
    merged.update(overrides)
    return merged


def wait_until(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("timed out")


@pytest.fixture
def serve(monkeypatch):
    """Start voice servers in threads; they are stopped when the test ends."""
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    # A server that fails to stop must fail the test, not end the test run.
    monkeypatch.setattr(voice_server.os, "_exit", lambda code: None)
    monkeypatch.setattr(voice_server, "STOP_DEADLINE_S", 0.2)
    running: list[tuple[voice_server.VoiceServer, threading.Thread]] = []

    def start(engine=None, player=None, **settings):
        log = logging.getLogger(f"voice-test-{len(running)}-{time.monotonic_ns()}")
        log.setLevel(logging.INFO)
        log.propagate = False
        log.addHandler(logging.FileHandler(config.voice_log_path()))
        server = voice_server.VoiceServer(
            cfg(**settings), log, engine or FakeEngine(), player or FakePlayer()
        )
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        running.append((server, thread))
        wait_until(lambda: (voice.server_state() or {}).get("status") == "ready")
        return server, thread

    yield start
    for server, thread in running:
        server.stop.set()
        thread.join(5.0)
        assert not thread.is_alive(), "the voice server did not stop"


def say(text: str, **options) -> dict:
    return voice.say(text, cfg(), **options)


# ------------------------------------------------------------------ speaking


def test_a_reply_is_synthesised_and_played_to_the_end(serve):
    engine, player = FakeEngine(seconds=0.4), FakePlayer()
    serve(engine, player)
    result = say("**Tests** pass.", wait=True, voice_name="bm_george", speed=1.2)
    assert result["engine"] == "fake" and result["voice"] == "bm_george"
    assert result["seconds"] == 0.4 and result["interrupted"] is False
    assert engine.loaded == "af_heart"  # the configured voice is what gets warmed up
    assert engine.spoken == [("Tests pass.", "bm_george", 1.2)]
    (stream,) = player.streams
    silence = voice_server.LEAD_IN_S + voice_server.TAIL_S  # around the words
    assert stream.samples == int(0.4 * RATE) + int(silence * RATE)
    assert stream.ended == "played out"
    assert "spoke 0.4s (fake bm_george" in voice.describe(result)


def test_replies_are_spoken_one_after_another(serve):
    engine = FakeEngine()
    serve(engine)
    assert "speaking" in voice.describe(say("One."))
    say("Two.")
    say("Three.", wait=True)
    assert [text for text, _, _ in engine.spoken] == ["One.", "Two.", "Three."]


def test_the_flag_is_up_while_speaking_and_down_after(serve):
    serve(FakeEngine(seconds=0.6), FakePlayer(realtime=True))
    assert voice.speaking() is None
    say("A sentence that takes a moment.")
    flag = wait_until(voice.speaking)
    assert flag["server"] is True and flag["pid"] == os.getpid()
    wait_until(lambda: voice.speaking() is None)


def test_loudspeakers_close_the_microphone_and_headphones_do_not(serve):
    server, _ = serve(FakeEngine(seconds=0.5), FakePlayer("Mac Studio Speakers", realtime=True))
    say("Through the speakers.")
    assert wait_until(voice.speaking)["gate"] is True
    assert server._gate("LG ULTRAWIDE") is True  # unknown: assume it is audible
    assert server._gate("Pedro’s AirPods Pro") is False
    assert server._gate("External Headphones") is False
    server.cfg["voice_gate"] = "off"
    assert server._gate("Mac Studio Speakers") is False
    server.cfg["voice_gate"] = "on"
    assert server._gate("Pedro’s AirPods Pro") is True


def test_stop_silences_the_reply_and_drops_the_ones_behind_it(serve):
    engine, player = FakeEngine(seconds=3.0), FakePlayer(realtime=True)
    serve(engine, player)
    say("A long explanation.")
    say("And another thing.")
    wait_until(voice.speaking)
    assert voice.hush() == 2
    wait_until(lambda: voice.speaking() is None)
    assert player.streams[0].ended == "cut off"
    assert [text for text, _, _ in engine.spoken] == ["A long explanation."]
    # ...and the server is still there for the next reply
    assert say("Short.", wait=True)["interrupted"] is False


def test_the_user_talking_over_a_reply_ends_it(serve):
    engine, player = FakeEngine(seconds=3.0), FakePlayer("Pedro’s AirPods Pro", realtime=True)
    server, _ = serve(engine, player)
    results = []
    waiting = threading.Thread(target=lambda: results.append(say("A long answer.", wait=True)))
    waiting.start()
    say("With more to come.")
    assert wait_until(voice.speaking)["gate"] is False
    server._on_interrupt(signal.SIGUSR1, None)  # what the listener's signal does
    waiting.join(5.0)
    (result,) = results
    assert result["interrupted"] is True and result["seconds"] < 3.0
    assert "interrupted after" in voice.describe(result)
    time.sleep(0.2)
    assert len(engine.spoken) == 1, "the queued reply was spoken after the interruption"


def test_a_failed_synthesis_is_reported_without_the_words(serve):
    serve(FakeEngine(fail=True))
    with pytest.raises(voice.VoiceError, match=r"synthesis failed \(RuntimeError\)"):
        voice.request({"op": "say", "text": "the launch code is 1234", "wait": True})
    assert "launch code" not in config.voice_log_path().read_text()


def test_what_is_said_is_not_logged(serve):
    serve()
    say("the launch code is 1234", wait=True)
    log = config.voice_log_path().read_text()
    assert "launch code" not in log and "reply of 23 chars" in log


# ------------------------------------------------------------------ requests


def raw_request(payload: dict) -> dict:
    state = voice.server_state()
    with socket.create_connection(("127.0.0.1", state["port"]), timeout=5) as conn:
        conn.sendall((json.dumps(payload) + "\n").encode())
        return json.loads(conn.makefile().readline())


def test_only_holders_of_the_token_are_served(serve):
    engine = FakeEngine()
    serve(engine)
    assert raw_request({"op": "say", "text": "Hello."}) == {"ok": False, "error": "not authorised"}
    assert raw_request({"op": "say", "text": "Hello.", "token": "0" * 32})["ok"] is False
    assert engine.spoken == []
    assert config.voice_state_path().stat().st_mode & 0o077 == 0  # where the token is kept


def test_malformed_and_unknown_requests_are_refused(serve):
    serve()
    assert raw_request(["not", "an", "object"])["error"] == "malformed request"
    with pytest.raises(voice.VoiceError, match="unknown request"):
        voice.request({"op": "sing"})
    with pytest.raises(voice.VoiceError, match="not a voice name"):
        voice.request({"op": "say", "text": "Hello.", "voice": "../x"})


def test_status_describes_the_server(serve):
    serve()
    say("Hello.", wait=True)
    status = voice.request({"op": "status"})
    assert status["spoken"] == 1 and status["speaking"] is False
    assert status["device"] == "Desk Speakers"


# ------------------------------------------------------------------ lifetime


def test_it_leaves_when_its_last_session_says_goodbye(serve, monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    server, thread = serve()
    say("Hello.", wait=True)
    assert server.owners == {os.getpid()}
    voice.release(os.getpid())
    thread.join(5.0)
    assert not thread.is_alive()
    assert voice.server_state() is None and not config.voice_state_path().exists()


def test_it_leaves_when_its_sessions_die_without_a_word(serve, monkeypatch):
    session = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setenv("CLAUDE_PID", str(session.pid))
        server, thread = serve()
        say("Hello.", wait=True)
        assert server.owners == {session.pid}
    finally:
        session.kill()
        session.wait()
    thread.join(5.0)
    assert not thread.is_alive()


def test_it_stays_while_any_session_is_left(serve, monkeypatch):
    gone = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setenv("CLAUDE_PID", str(gone.pid))
    server, thread = serve()
    say("From the first session.", wait=True)
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    say("From the second.", wait=True)
    gone.kill()
    gone.wait()
    time.sleep(1.5)  # longer than one turn of the watchdog
    assert thread.is_alive()
    assert say("Still here.", wait=True)["interrupted"] is False


def test_it_leaves_after_going_unused(serve):
    _, thread = serve(voice_idle_s=0.3)
    thread.join(5.0)
    assert not thread.is_alive()
    assert voice.server_state() is None


def test_a_second_server_leaves_the_first_alone(serve):
    first, _ = serve()
    second = voice_server.VoiceServer(cfg(), logging.getLogger("second"), FakeEngine())
    assert second.serve() == 0  # returns at once
    assert voice.server_state()["token"] == first.token
    assert say("Hello.", wait=True)["engine"] == "fake"


def test_stopping_mid_reply_cuts_it_off_and_clears_the_flag(serve):
    player = FakePlayer(realtime=True)
    server, thread = serve(FakeEngine(seconds=5.0), player)
    say("A reply that will not be finished.")
    wait_until(voice.speaking)
    server._on_signal(signal.SIGTERM, None)
    thread.join(5.0)
    assert not thread.is_alive()
    assert player.streams[0].ended == "cut off"
    assert voice.speaking() is None and voice.server_state() is None


def test_the_signal_handlers_only_record(serve):
    """As in the listener: a handler that took a lock could deadlock on itself."""
    server, _ = serve()
    with server.stop._cond:  # the lock Event.set() needs
        server._on_signal(signal.SIGTERM, None)
        server._on_interrupt(signal.SIGUSR1, None)
    assert server._stopping() and server._interrupt

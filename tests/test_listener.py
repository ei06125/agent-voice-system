"""The listener's lifetime rules, tested without audio.

The property that matters most is that the microphone never outlives the
session, so most of this file is about stopping.
"""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import threading
import types

import pytest

from donna_ears import config, events, listen
from donna_ears.batcher import Utterance


@pytest.fixture
def listener(monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "unit-session")
    args = types.SimpleNamespace(input_file=None, fast=False, exit_at_eof=False)
    stream = io.StringIO()
    log = logging.getLogger("ears-test")
    log.addHandler(logging.NullHandler())
    made = listen.Listener(config.load(), args, events.EventWriter(stream), log)
    made.stream = stream
    return made


def finishes(target, timeout=2.0) -> bool:
    """Run ``target`` on a thread; False if it is still stuck after ``timeout``."""
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive()


# ------------------------------------------------------------------ signals


def test_the_signal_handler_cannot_deadlock_on_itself(listener):
    """Regression. Three SIGTERMs reach the listener together when a session
    ends, and Python nests the handler calls on the main thread. A handler that
    calls Event.set() takes the event's lock, and the nested call then waits
    for a lock its own outer call holds: the listener hung with the microphone
    open. Holding that lock here reproduces what the nested call finds."""

    def nested_handler_call():
        with listener.stop._cond:  # the lock Event.set() needs
            listener._on_signal(signal.SIGTERM, None)

    assert finishes(nested_handler_call), "the signal handler blocked on the stop event's lock"
    assert listener._stopping()
    assert not listener.stop.is_set(), "setting the event is the relay thread's job"


def test_the_wakeup_pipe_turns_a_signal_into_a_stop(listener):
    read_fd, write_fd = os.pipe()
    relay = threading.Thread(target=listener._relay_signals, args=(read_fd,), daemon=True)
    relay.start()
    os.write(write_fd, bytes([signal.SIGTERM]))
    assert listener.stop.wait(2.0)
    os.close(write_fd)
    relay.join(2.0)
    assert not relay.is_alive()


def test_a_stop_that_stalls_ends_the_process_anyway(listener, monkeypatch):
    """If teardown hangs (an audio driver that will not close), the watchdog
    ends the process rather than leave the microphone open."""
    exited = threading.Event()
    killed = threading.Event()
    monkeypatch.setattr(listen, "STOP_DEADLINE_S", 0.2)
    monkeypatch.setattr(listen.os, "_exit", lambda code: exited.set())
    listener.server = types.SimpleNamespace(kill=killed.set, pid=0)
    listener.stop.set()  # asked to stop...
    assert finishes(listener._watchdog, timeout=3.0)  # ...and _done never comes
    assert exited.is_set() and killed.is_set()


def test_a_clean_stop_is_left_alone(listener, monkeypatch):
    exited = threading.Event()
    monkeypatch.setattr(listen, "STOP_DEADLINE_S", 0.5)
    monkeypatch.setattr(listen.os, "_exit", lambda code: exited.set())
    listener.stop.set()
    listener._done.set()
    assert finishes(listener._watchdog, timeout=3.0)
    assert not exited.is_set()


def test_the_watchdog_stops_when_the_session_process_is_gone(listener, monkeypatch):
    monkeypatch.setattr(listen.os, "_exit", lambda code: None)
    monkeypatch.setattr(listen, "STOP_DEADLINE_S", 0.1)
    listener.owner_pid = 2**22 + 12345  # beyond any real pid
    assert finishes(listener._watchdog, timeout=5.0)
    assert listener.stop.is_set()


def test_the_watchdog_stops_when_nobody_is_reading(listener, monkeypatch):
    monkeypatch.setattr(listen.os, "_exit", lambda code: None)
    monkeypatch.setattr(listen, "STOP_DEADLINE_S", 0.1)
    listener.out.broken = True
    assert finishes(listener._watchdog, timeout=5.0)
    assert listener.stop.is_set()


# ------------------------------------------------------------------- errors


def test_a_fatal_problem_is_reported_to_the_model_and_not_retried(listener):
    assert listener._fatal("speech model not found: /nowhere.bin") == listen.EXIT_FATAL
    assert listener.stream.getvalue() == "[ears error] speech model not found: /nowhere.bin\n"
    assert config.listeners()[0]["status"] == "error"


def test_failures_during_shutdown_are_not_reported(listener):
    """The speech server dies with the session; that must not look like an error."""
    listener._on_signal(signal.SIGTERM, None)
    assert listener._fatal("whisper-server exited with code -15") == 0
    listener.error_once("stt", "speech recognition is failing")
    assert listener.stream.getvalue() == ""


def test_the_same_complaint_is_not_repeated(listener):
    for _ in range(5):
        listener.error_once("open", "cannot open the microphone")
    listener.error_once("silent", "the microphone is delivering pure digital silence")
    lines = listener.stream.getvalue().splitlines()
    assert lines == [
        "[ears error] cannot open the microphone",
        "[ears error] the microphone is delivering pure digital silence",
    ]


# ------------------------------------------------------- shared microphone


def test_the_microphone_lock_admits_one_holder():
    first, second = listen.MicLock(), listen.MicLock()
    assert first.try_acquire()
    assert not second.try_acquire()
    first.release()
    assert second.try_acquire()
    second.release()
    first.release()  # releasing twice is harmless


# --------------------------------------------------------------- transcript


def test_the_transcript_is_private_and_append_only():
    log = listen.TranscriptLog("abc-123")
    log.append(Utterance(text="olá mundo", started_at=10.0, ended_at=11.5, language="pt"))
    log.append(Utterance(text="second", started_at=20.0, ended_at=21.0, language="en"))
    assert log.path.stat().st_mode & 0o077 == 0
    first, second = (json.loads(line) for line in log.path.read_text().splitlines())
    assert first == {"t0": 10.0, "t1": 11.5, "lang": "pt", "text": "olá mundo"}
    assert second["text"] == "second"


def test_a_session_id_cannot_escape_the_sessions_directory():
    log = listen.TranscriptLog("../../etc/passwd")
    assert log.path.parent == config.sessions_dir()
    assert "/" not in log.path.name and ".." not in log.path.name


def test_the_state_file_reports_progress(listener):
    listener.utterances, listener.pushed = 4, 2
    listener.write_state("listening", device="Desk Mic")
    (state,) = config.listeners()
    assert (state["status"], state["device"], state["utterances"]) == ("listening", "Desk Mic", 4)
    assert state["claude_pid"] == os.getpid() and state["session_id"] == "unit-session"
    listener.write_state("error", detail="cannot open the microphone")
    assert config.listeners()[0]["detail"] == "cannot open the microphone"
    listener.write_state("listening")
    assert "detail" not in config.listeners()[0]

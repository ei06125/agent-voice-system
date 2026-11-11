"""Speech output, the client side: what gets spoken, the speaking flag the
listener watches, and what happens when there is no voice server to talk to."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from donna_ears import cli, config, voice


def cfg(**overrides):
    merged = dict(config.DEFAULTS)
    merged.update(overrides)
    return merged


@pytest.fixture(autouse=True)
def outside_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_PID", raising=False)


# --------------------------------------------------------------------- text


def test_markup_is_not_spoken():
    text = voice.clean(
        "## Result\n"
        "- **Tests** pass: `pytest -q`\n"
        "- see [the log](https://example.com/x) or https://example.com/y\n"
        "```\nrm -rf build\n```\n"
    )
    assert text == "Result\nTests pass: pytest -q\nsee the log or a link"


def test_identifiers_are_read_as_words():
    assert voice.clean("set voice_gate to off | then retry") == "set voice gate to off then retry"


def test_a_monologue_is_cut_at_the_end_of_a_sentence():
    text = voice.clean("This is a sentence. " * 200)
    assert len(text) <= voice.MAX_TEXT
    assert text.endswith("sentence.")


def test_the_first_sentence_goes_alone_so_speech_starts_early():
    grouped = voice.chunks("First one. Second one! Third?\nFourth line")
    assert grouped == ["First one.", "Second one! Third? Fourth line."]
    assert voice.chunks("   ") == []


def test_long_replies_are_grouped_into_a_few_sentences_each():
    grouped = voice.chunks("Start. " + "Another sentence of average length follows here. " * 20)
    assert grouped[0] == "Start."
    assert all(len(group) <= 280 for group in grouped)
    assert len(grouped) > 3


# ---------------------------------------------------------------- the flag


def test_the_speaking_flag_lasts_as_long_as_the_speech():
    voice.write_speaking(gate=True, server=True)
    state = voice.speaking()
    assert state["gate"] is True and state["server"] is True and state["pid"] == os.getpid()
    voice.clear_speaking()
    assert voice.speaking() is None


def test_a_flag_left_by_a_dead_speaker_is_ignored_and_removed():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    flag = {"pid": dead.pid, "since": time.time(), "gate": True}
    config.write_json_atomic(config.speaking_path(), flag)
    assert voice.speaking() is None
    assert not config.speaking_path().exists()


def test_a_flag_that_has_been_up_for_minutes_is_a_leftover():
    old = {"pid": os.getpid(), "since": time.time() - voice.SPEAKING_MAX_S - 1, "gate": True}
    config.write_json_atomic(config.speaking_path(), old)
    assert voice.speaking() is None


def test_only_its_writer_clears_the_flag():
    other = {"pid": os.getppid(), "since": time.time(), "gate": False}
    config.write_json_atomic(config.speaking_path(), other)
    voice.clear_speaking()
    assert config.speaking_path().exists()
    voice.clear_speaking(force=True)
    assert not config.speaking_path().exists()


def test_the_watch_follows_the_flag():
    watch = voice.SpeakingWatch()
    assert watch.get() is None
    voice.write_speaking(gate=False, server=True)
    assert watch.get()["gate"] is False
    voice.clear_speaking()
    assert watch.get() is None


def test_talking_over_a_reply_signals_the_voice_server():
    received = []
    previous = signal.signal(signal.SIGUSR1, lambda signum, frame: received.append(signum))
    try:
        voice.write_speaking(gate=False, server=True)
        assert voice.interrupt() is True
        deadline = time.monotonic() + 2.0
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
        assert received == [signal.SIGUSR1]
    finally:
        signal.signal(signal.SIGUSR1, previous)
        voice.clear_speaking()


def test_the_macos_fallback_is_never_signalled():
    """Its flag belongs to a short-lived command, which the signal would kill
    while leaving `say` talking."""
    voice.write_speaking(gate=True, server=False)
    assert voice.interrupt() is False
    voice.clear_speaking()
    assert voice.interrupt() is False  # and with nobody speaking, nothing happens


# ------------------------------------------------------------- no server


def test_requests_fail_cleanly_without_a_server():
    with pytest.raises(voice.VoiceError, match="not running"):
        voice.request({"op": "status"})
    assert voice.hush() == 0
    voice.release(os.getpid())
    assert voice.stop_server() is False


def test_a_dead_servers_state_is_cleaned_up():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    config.write_json_atomic(config.voice_state_path(), {"pid": dead.pid, "status": "ready"})
    assert voice.server_state() is None
    assert not config.voice_state_path().exists()


@pytest.fixture
def fake_say(tmp_path, monkeypatch):
    """A stand-in for /usr/bin/say that records what it was given."""
    record = tmp_path / "said.txt"
    script = tmp_path / "bin" / "say"
    script.parent.mkdir()
    script.write_text(f'#!/bin/sh\nprintf "%s" "$*" > "{record}.args"\ncat > "{record}"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    return record


def test_the_say_engine_speaks_through_macos(fake_say):
    result = voice.say("**Hello** there.", cfg(voice_engine="say", say_voice="Samantha"))
    assert result["engine"] == "say"
    assert fake_say.read_text() == "Hello there."
    # The words go in on stdin: as an argument they would show in `ps`.
    assert Path(f"{fake_say}.args").read_text() == "-v Samantha"
    assert voice.speaking() is None


def test_kokoro_falls_back_to_macos_when_its_server_cannot_start(fake_say, monkeypatch):
    monkeypatch.setattr(voice, "find_uv", lambda: None)
    result = voice.say("Hello.", cfg())
    assert result["engine"] == "say" and "uv is not installed" in result["note"]
    assert fake_say.read_text() == "Hello."
    assert "because uv is not installed" in voice.describe(result)


def test_a_voice_still_loading_does_not_keep_the_user_waiting(fake_say):
    loading = {"pid": os.getpid(), "status": "loading"}
    config.write_json_atomic(config.voice_state_path(), loading)
    result = voice.say("Hello.", cfg(), start_timeout=0.3)
    assert result["engine"] == "say" and "still loading" in result["note"]


def test_nothing_to_say_is_an_error():
    with pytest.raises(voice.VoiceError, match="nothing to say"):
        voice.say("```\ncode only\n```", cfg())


def test_a_voice_name_cannot_be_a_path():
    with pytest.raises(voice.VoiceError, match="not a voice name"):
        voice.say("Hello.", cfg(), voice_name="../../etc/passwd")


# ------------------------------------------------------------------ the cli


def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_say_speaks_its_arguments(fake_say, capsys, monkeypatch):
    monkeypatch.setenv("EARS_VOICE_ENGINE", "say")
    code, out, _ = run(capsys, "say", "All", "tests", "pass.")
    assert code == 0 and "spoke with the macOS voice" in out
    assert fake_say.read_text() == "All tests pass."


def test_say_with_nothing_to_say_fails(capsys, monkeypatch):
    monkeypatch.setenv("EARS_VOICE_ENGINE", "say")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    code, _, err = run(capsys, "say")
    assert code == 1 and "nothing to say" in err


def test_the_voice_switch_is_remembered_and_shown(capsys, monkeypatch):
    monkeypatch.setenv("EARS_VOICE_ENGINE", "say")
    _, out, _ = run(capsys, "voice")
    assert "spoken replies off" in out
    _, out, _ = run(capsys, "voice", "on")
    assert "spoken replies on" in out
    assert config.read_control()["speak"] is True
    _, out, _ = run(capsys, "status")
    assert "spoken replies on; voice: macOS built-in" in out
    run(capsys, "voice", "off")
    assert config.read_control()["speak"] is False


def test_the_configured_default_applies_until_the_session_chooses(capsys, monkeypatch):
    monkeypatch.setenv("EARS_VOICE_ENGINE", "say")
    monkeypatch.setenv("EARS_SPEAK", "true")
    _, out, _ = run(capsys, "voice")
    assert "spoken replies on" in out
    run(capsys, "voice", "off")
    _, out, _ = run(capsys, "voice")
    assert "spoken replies off" in out

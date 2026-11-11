import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from donna_ears import cli, config

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "plugin" / "bin" / "ears"


def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def fake_listener(pid, **fields):
    state = {
        "pid": pid,
        "claude_pid": 0,
        "session_id": "11111111-aaaa",
        "status": "listening",
        "device": "Desk Mic",
        "mode": "live",
        "model": "large-v3-turbo",
        "language": "auto",
        "started_at": time.time() - 600,
        "last_voice_at": time.time() - 12,
        "utterances": 7,
        "pushed": 3,
        "pending": 0,
    }
    state.update(fields)
    config.write_json_atomic(config.state_path(pid), state)
    return state


@pytest.fixture
def no_claude(monkeypatch):
    for name in ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


# ----------------------------------------------------------------- status


def test_status_without_a_listener_says_so(capsys, no_claude):
    code, out = run(capsys, "status")
    assert code == 0
    assert "no listener is running" in out


def test_status_describes_this_sessions_listener(capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", "4242")
    fake_listener(os.getpid(), claude_pid=4242)
    _, out = run(capsys, "status")
    assert 'ears: listening (live) on "Desk Mic"' in out
    assert "7 utterances heard, 3 events pushed" in out
    assert "other session" not in out


def test_status_shows_a_listener_owned_by_another_session(capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    fake_listener(os.getpid(), claude_pid=999999)
    _, out = run(capsys, "status")
    assert "no listener is running for this session" in out
    assert "other session -> ears: listening" in out


def test_a_dead_listeners_state_file_is_cleaned_up(capsys, no_claude):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    fake_listener(dead.pid)
    _, out = run(capsys, "status")
    assert "no listener is running" in out
    assert not config.state_path(dead.pid).exists()


def test_status_json_is_machine_readable(capsys, no_claude):
    fake_listener(os.getpid())
    _, out = run(capsys, "status", "--json")
    data = json.loads(out)
    assert data["listeners"][0]["device"] == "Desk Mic"
    assert data["control"] == {"paused": False, "mode": None, "speak": None}


# ---------------------------------------------------------- pause and mode


def test_pause_and_resume_need_a_listener(capsys, no_claude):
    assert run(capsys, "pause")[0] == 1
    assert run(capsys, "resume")[0] == 1
    assert config.read_control()["paused"] is False


def test_pause_sets_the_switch_the_listener_watches(capsys, no_claude):
    fake_listener(
        os.getpid(), status="paused"
    )  # as the listener reports once it has closed the mic
    code, out = run(capsys, "pause")
    assert code == 0
    assert config.read_control()["paused"] is True
    assert "the microphone is closed." in out
    assert "cannot be heard" in out


def test_resume_clears_the_switch(capsys, no_claude):
    fake_listener(os.getpid())
    config.write_control(paused=True)
    code, out = run(capsys, "resume")
    assert code == 0
    assert config.read_control()["paused"] is False
    assert "listening again" in out


def test_mode_switches_and_reports(capsys, no_claude):
    fake_listener(os.getpid())
    assert "mode is live" in run(capsys, "mode")[1]
    _, out = run(capsys, "mode", "notes")
    assert "notes mode" in out and "60s" in out
    assert config.read_control()["mode"] == "notes"
    run(capsys, "mode", "live")
    assert config.read_control()["mode"] == "live"


def test_an_unknown_mode_is_refused(capsys):
    with pytest.raises(SystemExit):
        cli.main(["mode", "telepathy"])


# ------------------------------------------------------- transcript replay


def write_transcript(session, records):
    path = config.sessions_dir() / f"{session}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_tail_prints_the_latest_utterances(capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    now = time.time()
    write_transcript(
        "sess-1",
        [
            {"t0": now - 30, "t1": now - 28, "lang": "en", "text": "first thing"},
            {"t0": now - 20, "t1": now - 18, "lang": "pt", "text": "segunda coisa"},
            {"t0": now - 10, "t1": now - 8, "lang": "en", "text": "third thing"},
        ],
    )
    _, out = run(capsys, "tail", "-n", "2")
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("pt] segunda coisa")
    assert lines[1].endswith("] third thing")


def test_since_filters_by_time(capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-2")
    now = time.time()
    write_transcript(
        "sess-2",
        [
            {"t0": now - 3600, "t1": now - 3590, "lang": "en", "text": "an hour ago"},
            {"t0": now - 120, "t1": now - 110, "lang": "en", "text": "two minutes ago"},
        ],
    )
    _, out = run(capsys, "since", "5m")
    assert "two minutes ago" in out and "an hour ago" not in out
    _, out = run(capsys, "since", "2h")
    assert "an hour ago" in out
    _, out = run(capsys, "since", "30s")
    assert "nothing heard since" in out


def test_replay_falls_back_to_the_most_recent_session(capsys, no_claude):
    now = time.time()
    old = write_transcript("older", [{"t0": now - 9000, "t1": now - 8990, "text": "stale"}])
    os.utime(old, (now - 9000, now - 9000))
    write_transcript("newer", [{"t0": now - 5, "t1": now - 4, "text": "fresh"}])
    _, out = run(capsys, "tail")
    assert "fresh" in out and "stale" not in out


def test_tail_with_nothing_heard(capsys, no_claude):
    assert "nothing has been heard yet" in run(capsys, "tail")[1]


@pytest.mark.parametrize(
    "spec, seconds_back", [("15m", 900), ("2h", 7200), ("90s", 90), ("10", 600)]
)
def test_spans_are_parsed(spec, seconds_back):
    assert abs(cli._parse_since(spec) - (time.time() - seconds_back)) < 2


def test_a_clock_time_means_today():
    parsed = time.localtime(cli._parse_since("14:30"))
    assert (parsed.tm_hour, parsed.tm_min) == (14, 30)
    assert parsed.tm_yday == time.localtime().tm_yday


def test_nonsense_spans_are_rejected(capsys):
    assert cli._parse_since("soon") is None
    assert cli.main(["since", "soon"]) == 2


# ------------------------------------------------------- SessionStart hook


def context(capsys, monkeypatch, hook, entrypoint="cli", attended="1"):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", attended)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(hook)))
    code, out = run(capsys, "context")
    assert code == 0
    return json.loads(out) if out.strip() else None


def test_the_hook_explains_the_transcript_lines_to_the_model(capsys, monkeypatch, no_claude):
    out = context(capsys, monkeypatch, {"source": "startup"})
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    protocol = specific["additionalContext"]
    for needle in ("[heard 22:41:07]", "not authorisation", "ears pause", "ears mode notes"):
        assert needle in protocol
    assert "microphone is live" in out["systemMessage"]


def test_the_user_is_not_told_twice_after_a_compaction(capsys, monkeypatch, no_claude):
    out = context(capsys, monkeypatch, {"source": "compact"})
    assert "additionalContext" in out["hookSpecificOutput"]
    assert "systemMessage" not in out


def test_a_headless_run_is_told_nothing_because_nothing_is_listening(
    capsys, monkeypatch, no_claude
):
    # What `claude -p` puts in a hook's environment (Claude Code 2.1.322).
    hook = {"source": "startup"}
    assert context(capsys, monkeypatch, hook, entrypoint="sdk-cli", attended="0") is None
    assert context(capsys, monkeypatch, hook, entrypoint="sdk-cli") is None
    assert context(capsys, monkeypatch, hook, attended="0") is None


def test_the_hook_admits_when_another_session_has_the_microphone(capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    fake_listener(os.getpid(), claude_pid=999999)
    out = context(capsys, monkeypatch, {"source": "startup"})
    assert "standby" in out["systemMessage"]
    assert (
        "another Claude Code session holds the microphone"
        in (out["hookSpecificOutput"]["additionalContext"])
    )


def test_the_hook_survives_garbage_on_stdin(capsys, monkeypatch, no_claude):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    code, out = run(capsys, "context")
    assert code == 0 and "additionalContext" in out


# ---------------------------------------------------------------- settings


def test_settings_come_from_defaults_then_file_then_environment(monkeypatch):
    assert config.load()["pause_s"] == 2.0
    config.config_path().write_text(json.dumps({"ears": {"pause_s": 3.5, "device": "Desk"}}))
    cfg = config.load()
    assert cfg["pause_s"] == 3.5 and cfg["device"] == "Desk"
    monkeypatch.setenv("EARS_PAUSE_S", "1.25")
    monkeypatch.setenv("EARS_WHISPER_THREADS", "8")
    cfg = config.load()
    assert cfg["pause_s"] == 1.25 and cfg["whisper_threads"] == 8 and cfg["device"] == "Desk"


def test_bad_settings_are_ignored_not_fatal(monkeypatch):
    config.config_path().write_text(json.dumps({"ears": {"pause_s": "soon", "mode": "psychic"}}))
    monkeypatch.setenv("EARS_MAX_WAIT_S", "forever")
    cfg = config.load()
    assert cfg["pause_s"] == 2.0 and cfg["mode"] == "live" and cfg["max_wait_s"] == 30.0


def test_a_corrupt_config_file_falls_back_to_defaults():
    config.config_path().write_text("{ this is not json")
    assert config.load() == config.DEFAULTS


def test_the_state_tree_is_private(donna_home):
    for path in (donna_home, config.ears_dir(), config.sessions_dir()):
        assert path.stat().st_mode & 0o077 == 0


# ---------------------------------------------------------------- launcher


def test_the_launcher_runs_light_commands_without_the_audio_stack(donna_home):
    result = subprocess.run(
        [str(LAUNCHER), "status"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "DONNA_HOME": str(donna_home)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ears:")


@pytest.mark.skipif(not os.access("/usr/bin/python3", os.X_OK), reason="no system python")
def test_hook_commands_work_on_the_system_python(donna_home):
    """The hooks may run under whatever python3 is first on PATH; on macOS that
    can be Apple's 3.9."""
    result = subprocess.run(
        ["/usr/bin/python3", str(REPO / "plugin" / "lib" / "ears.py"), "context"],
        input='{"source": "startup"}',
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "DONNA_HOME": str(donna_home),
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "additionalContext" in result.stdout

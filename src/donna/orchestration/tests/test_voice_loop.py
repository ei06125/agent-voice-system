import json
import sys
from pathlib import Path

import pytest

from donna.orchestration.voice_loop import (
    DEFAULT_MODEL,
    LOCAL_BASE_URL,
    HermesLocalAgent,
    TranscriptCursor,
    VoiceLoop,
)


class FakeWatcher:
    def __init__(self, cutoff: float) -> None:
        self.cutoff = cutoff

    def wait(self) -> float:
        return self.cutoff


class FakeSpeaker:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


class FakeAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def reply(self, text: str) -> str:
        self.prompts.append(text)
        return "local reply"


def _append(
    path: Path, timestamp: float, text: str, playback_overlap: bool = False
) -> None:
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "ts": timestamp,
                    "text": text,
                    "playback_overlap": playback_overlap,
                }
            )
            + "\n"
        )


def test_cursor_returns_complete_interval_and_stops_at_cutoff(tmp_path: Path):
    log = tmp_path / "utterances.jsonl"
    state = tmp_path / "cursor.json"
    _append(log, 10.0, "ambient context")
    _append(log, 11.0, "Donna answer this")
    _append(log, 12.0, "future speech")
    cursor = TranscriptCursor(log, state)

    batch = cursor.read_until(11.5)

    assert batch is not None
    assert "[10.000000] ambient context" in batch.text
    assert "[11.000000] Donna answer this" in batch.text
    assert "future speech" not in batch.text
    cursor.commit(batch.end_offset, batch.ended_at)
    next_batch = cursor.read_until(12.5)
    assert next_batch is not None
    assert "future speech" in next_batch.text
    assert "ambient context" not in next_batch.text


def test_cursor_does_not_advance_until_agent_succeeds(tmp_path: Path):
    class FailingAgent:
        def reply(self, _text: str) -> str:
            raise RuntimeError("failed")

    log = tmp_path / "utterances.jsonl"
    state = tmp_path / "cursor.json"
    _append(log, 10.0, "Donna test")
    cursor = TranscriptCursor(log, state)
    loop = VoiceLoop(cursor, FakeWatcher(11.0), FakeSpeaker(), FailingAgent())

    with pytest.raises(RuntimeError, match="failed"):
        loop.run_once()

    assert cursor.offset == 0
    assert not state.exists()


def test_voice_loop_commits_then_speaks(tmp_path: Path):
    log = tmp_path / "utterances.jsonl"
    state = tmp_path / "cursor.json"
    _append(log, 10.0, "Donna hello")
    cursor = TranscriptCursor(log, state)
    speaker = FakeSpeaker()
    agent = FakeAgent()

    assert VoiceLoop(cursor, FakeWatcher(11.0), speaker, agent).run_once()

    assert len(agent.prompts) == 1
    assert speaker.spoken == ["local reply"]
    assert json.loads(state.read_text())["last_read_ts"] == 11.0


def test_initialize_at_end_avoids_replaying_stale_audio(tmp_path: Path):
    log = tmp_path / "utterances.jsonl"
    state = tmp_path / "cursor.json"
    _append(log, 10.0, "old audio")
    cursor = TranscriptCursor(log, state)

    cursor.initialize_at_end()
    _append(log, 20.0, "new audio")

    batch = cursor.read_until(21.0)
    assert batch is not None
    assert "new audio" in batch.text
    assert "old audio" not in batch.text


def test_cursor_filters_playback_echo_but_keeps_barge_in(tmp_path: Path):
    log = tmp_path / "utterances.jsonl"
    state = tmp_path / "cursor.json"
    _append(log, 10.0, "assistant reply echoed by speakers", True)
    _append(log, 11.0, "Donna stop and answer this", True)
    cursor = TranscriptCursor(log, state)

    batch = cursor.read_until(12.0)

    assert batch is not None
    assert "assistant reply echoed by speakers" not in batch.text
    assert "Donna stop and answer this" in batch.text


def test_hermes_command_is_pinned_to_local_model(tmp_path: Path):
    agent = HermesLocalAgent(cwd=tmp_path, executable="hermes", state_dir=tmp_path)
    command = agent.command("hello", tmp_path / "usage.json")

    assert command[command.index("--provider") + 1] == "ollama"
    assert command[command.index("-m") + 1] == DEFAULT_MODEL
    assert "--safe-mode" in command
    assert command[command.index("--reasoning") + 1] == "none"
    assert command[command.index("--toolsets") + 1] == "context_engine"


def test_hermes_rejects_nonzero_cost(tmp_path: Path):
    agent = HermesLocalAgent(cwd=tmp_path, executable="hermes", state_dir=tmp_path)
    usage = {
        "completed": True,
        "failed": False,
        "model": DEFAULT_MODEL,
        "estimated_cost_usd": 0.01,
    }
    with pytest.raises(RuntimeError, match="non-zero inference cost"):
        agent._validate_usage(json.loads(json.dumps(usage)))


def test_hermes_environment_forces_loopback_ollama(tmp_path: Path):
    agent = HermesLocalAgent(cwd=tmp_path, executable="hermes", state_dir=tmp_path)

    environment = agent._environment()

    assert environment["OLLAMA_BASE_URL"] == LOCAL_BASE_URL
    assert environment["OLLAMA_API_KEY"] == "ollama"
    assert environment["AWS_PROFILE"] == "__donna_local_only__"
    assert "OPENROUTER_API_KEY" not in environment

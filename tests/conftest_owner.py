"""Shared world and steps for the owner-listening features (shadow, owner-only).

The listener's per-segment handler is driven directly: segments are built by
hand, the transcriber and the speaker model are stand-ins. No microphone, no
Whisper, no subprocess.

Speech is faked by value: the owner's frames are +0.1, anyone else's -0.1.
The stand-in model maps positive audio to the owner's voiceprint direction and
negative audio to an orthogonal one, so scores are 1.0 and 0.0.
"""

from __future__ import annotations

import io
import json
import logging
import types

import numpy as np
import pytest
from pytest_bdd import given, parsers, then, when

from donna_ears import cli, config, events, listen, ownercheck, voiceprint
from donna_ears.segmenter import FRAME_SAMPLES, Segment

OWNER_VECTOR = [1.0, 0.0]


class StubEmbedder:
    preprocessing = "stub"
    dim = 2

    def __init__(self, path, threads=1) -> None:  # noqa: ARG002
        pass

    def embed(self, samples):
        return np.array([1.0, 0.0] if float(np.mean(samples)) > 0 else [0.0, 1.0], np.float32)


class World:
    def __init__(self, listener, log_stream) -> None:
        self.listener = listener
        self.log_stream = log_stream
        self.clock = 1000.0
        self.transcribed: list[float] = []
        self.lines: list[str] = []

    def log_text(self) -> str:
        return self.log_stream.getvalue()

    def delivered(self) -> list[str]:
        """Every line delivered so far (taking from the batcher empties it)."""
        for block in events.render(self.listener.batcher.take()):
            self.lines.extend(block.split("\n"))
        return self.lines

    def records(self) -> list[dict]:
        path = self.listener.transcript.path
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def world(monkeypatch):
    model = voiceprint.ModelId("stub-model", "a" * 64, "stub")
    monkeypatch.setattr(ownercheck, "make_embedder", StubEmbedder)
    monkeypatch.setattr(ownercheck, "verified_model", lambda profile: ("stub.onnx", model))
    log_stream = io.StringIO()
    log = logging.getLogger("ears-test")
    log.handlers[:] = [logging.StreamHandler(log_stream)]
    log.setLevel(logging.INFO)
    log.propagate = False
    args = types.SimpleNamespace(input_file=None, fast=False, exit_at_eof=False)
    listener = listen.Listener(config.load(), args, events.EventWriter(io.StringIO()), log)
    world = World(listener, log_stream)

    def transcribe(samples):
        world.transcribed.append(float(samples[-1]))
        return world.pending_text, "en"

    monkeypatch.setattr(listener, "_transcribe", transcribe)
    listener._device = "AirPods"
    listener._epoch = 1
    return world


def speak(world, text, seconds, owner_speaking, gap=0.5):
    level = 0.1 if owner_speaking else -0.1
    frames = [np.full(FRAME_SAMPLES, level, np.float32)] * max(
        1, int(seconds * 16000 / FRAME_SAMPLES)
    )
    start = world.clock
    world.clock += seconds
    segment = Segment(frames=frames, started_at=start, ended_at=world.clock, voiced_seconds=seconds)
    world.clock += gap
    world.pending_text = text
    world.listener._stamp(segment)
    world.listener._handle(segment)


# ------------------------------------------------------------------ given


@given(parsers.parse('the owner has a calibrated voiceprint for "{device}"'))
def calibrated(device):
    profile = voiceprint.load() or voiceprint.Profile.new(
        voiceprint.ModelId("stub-model", "a" * 64, "stub")
    )
    profile.enroll(device, [OWNER_VECTOR], seconds=40.0)
    profile.calibrate(device, accept=0.6, reject=0.3)
    voiceprint.save(profile)


@given(parsers.parse('the owner has an uncalibrated voiceprint for "{device}"'))
def uncalibrated(device):
    profile = voiceprint.Profile.new(voiceprint.ModelId("stub-model", "a" * 64, "stub"))
    profile.enroll(device, [OWNER_VECTOR], seconds=40.0)
    voiceprint.save(profile)


@given("there is no voiceprint")
def no_voiceprint():
    voiceprint.delete()


@given("the voiceprint file is damaged")
def damaged():
    voiceprint.path().write_text("{damaged")


@given(parsers.parse('the listener hears through "{device}"'))
def hears_through(world, device):
    world.listener._device = device


@given(parsers.parse('speaker mode is "{mode}"'))
def speaker_mode(world, mode):
    world.listener._speaker_mode = mode


# ------------------------------------------------------------------- when


@when(parsers.parse('the owner says "{text}" for {seconds:g} seconds'))
def owner_says(world, text, seconds):
    speak(world, text, seconds, owner_speaking=True)


@when(parsers.parse('someone else says "{text}" for {seconds:g} seconds'))
def other_says(world, text, seconds):
    speak(world, text, seconds, owner_speaking=False)


@when(
    parsers.parse(
        'the owner says "{text}" for {seconds:g} seconds after a pause of {gap:g} seconds'
    )
)
def owner_says_after(world, text, seconds, gap):
    world.clock += gap
    speak(world, text, seconds, owner_speaking=True)


@when(
    parsers.parse(
        'someone else says "{text}" for {seconds:g} seconds after a pause of {gap:g} seconds'
    )
)
def other_says_after(world, text, seconds, gap):
    world.clock += gap
    speak(world, text, seconds, owner_speaking=False)


@when(parsers.parse('the user runs "ears speaker {mode}"'))
def run_speaker(mode):
    assert cli.main(["speaker", mode]) == 0


@when("the listener reads its controls")
def reads_controls(world):
    world.listener._apply_control(config.read_control())


@when(parsers.parse('the listener switches to "{device}"'))
def switches(world, device):
    world.listener._device = device
    world.listener._epoch += 1


@when("a spoken reply plays")
def reply_plays(world):
    world.listener._epoch += 1


# ------------------------------------------------------------------- then


@then(parsers.parse('"{text}" is delivered as usual'))
def delivered_plain(world, text):
    lines = world.delivered()
    assert any(
        line.startswith("[heard ") and "?]" not in line and line.endswith(text) for line in lines
    ), lines


@then(parsers.parse('the transcript marks "{text}" as "{kind}"'))
def transcript_marks(world, text, kind):
    (record,) = [r for r in world.records() if r["text"] == text]
    assert record["speaker"]["kind"] == kind


@then(parsers.parse('the transcript has no verdict for "{text}"'))
def transcript_no_verdict(world, text):
    (record,) = [r for r in world.records() if r["text"] == text]
    assert "speaker" not in record


@then(parsers.parse('the log mentions the verdict "{kind}"'))
def log_verdict(world, kind):
    assert f"speaker: {kind}" in world.log_text()


@then(parsers.parse('the log does not contain "{word}"'))
def log_lacks(world, word):
    assert word not in world.log_text()


@then("the log does not contain a score")
def log_no_score(world):
    assert "score" not in world.log_text() and "1.0" not in world.log_text()


@then("the log says once that there is no voiceprint")
def log_once_no_voiceprint(world):
    assert world.log_text().count("no voiceprint") == 1


@then("nothing was transcribed for someone else")
def nobody_else_transcribed(world):
    assert all(level > 0 for level in world.transcribed), world.transcribed


@then("nothing is delivered")
def nothing_delivered(world):
    assert world.delivered() == []


@then("the transcript is empty")
def transcript_empty(world):
    assert world.records() == []


@then(parsers.parse('"{text}" is delivered marked unverified'))
def delivered_unverified(world, text):
    lines = world.delivered()
    assert any(
        line.startswith("[heard ") and " ?] " in line and line.endswith(text) for line in lines
    ), lines


@then(parsers.parse('"{text}" is not delivered'))
def not_delivered(world, text):
    assert not any(line.endswith(f"] {text}") for line in world.delivered())


@then("the user is told once that owner-only listening is inactive")
def told_once(world):
    out = world.listener.out._stream.getvalue()
    assert out.count("owner-only listening is inactive") == 1, out

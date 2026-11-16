"""Steps for features/enrollment.feature: `ears enroll` with a stand-in model."""

from __future__ import annotations

import stat
import wave

import numpy as np
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from donna_ears import cli, enroll, speaker_models, voiceprint
from synthetic import signal

scenarios("features/enrollment.feature")


class StubEmbedder:
    """Maps audio to a vector by a fixed random projection of its spectrum, so
    the same synthetic voice gives nearly the same embedding."""

    preprocessing = "stub-features"
    dim = 8

    def __init__(self, path) -> None:
        self.path = path
        self._projection = np.random.RandomState(0).standard_normal((self.dim, 129))

    def embed(self, samples):
        spectrum = np.abs(np.fft.rfft(samples[: 256 * (len(samples) // 256)].reshape(-1, 256)))
        vector = self._projection @ np.log1p(spectrum.mean(axis=0))
        return (vector / np.linalg.norm(vector)).astype(np.float32)


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(speaker_models, "ensure", lambda name: tmp_path / f"{name}.onnx")
    monkeypatch.setattr(enroll, "make_embedder", StubEmbedder)
    return {"dir": tmp_path, "capsys": capsys, "out": "", "code": None, "clips": []}


def write_wav(path, samples):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return str(path)


def speech_clips(world, count, start=0):
    return [
        write_wav(world["dir"] / f"speech-{start + i}.wav", signal("vowel", 3.0 + i))
        for i in range(count)
    ]


def silent_clips(world, count):
    return [
        write_wav(world["dir"] / f"silent-{i}.wav", np.zeros(48000, dtype=np.float32))
        for i in range(count)
    ]


def run(world, *argv):
    world["code"] = cli.main(list(argv))
    world["out"] = world["capsys"].readouterr().out


@given("no voiceprint exists")
def no_voiceprint():
    voiceprint.delete()


@given(parsers.parse('the owner has enrolled "{device}"'))
def enrolled(world, device):
    run(world, "enroll", "--device", device, *speech_clips(world, 3, start=100))
    assert world["code"] == 0


@given(parsers.parse('the owner has enrolled "{device}" with another model'))
def enrolled_with_another_model(world, device):
    profile = voiceprint.Profile.new(voiceprint.ModelId("old-model", "f" * 64, "old-features"))
    profile.enroll(device, [[1.0, 0.0]], seconds=30.0, now=1.0)
    voiceprint.save(profile)
    world["before"] = voiceprint.path().read_text()


@when(parsers.parse('the owner enrolls "{device}" from {count:d} clips of their speech'))
def enroll_speech(world, device, count):
    world["clips"] = speech_clips(world, count)
    run(world, "enroll", "--device", device, *world["clips"])


@when(
    parsers.re(
        r'the owner enrolls "(?P<device>[^"]+)" from (?P<count>\d+) clips of their speech '
        r"and (?P<silent>\d+) silent clips?"
    )
)
def enroll_mixed(world, device, count, silent):
    world["silent"] = silent_clips(world, int(silent))
    run(world, "enroll", "--device", device, *speech_clips(world, int(count)), *world["silent"])


@when("the owner deletes their voiceprint")
def delete(world):
    run(world, "enroll", "--delete")


@then(parsers.parse('a voiceprint exists for "{device}" made from {count:d} clips'))
def voiceprint_exists(device, count):
    entry = voiceprint.load().device(device)
    assert entry is not None and entry.clips == count


@then("only the owner can read the voiceprint file")
def private_file():
    assert stat.S_IMODE(voiceprint.path().stat().st_mode) == 0o600


@then("the command does not print the voiceprint itself")
def no_vector_printed(world):
    centroid = voiceprint.load().device("Logitech BRIO").centroid
    assert f"{centroid[0]:.4f}" not in world["out"]
    assert "[" not in world["out"]


@then("the command names the silent clip as skipped")
def names_silent(world):
    assert "silent-0.wav" in world["out"] and "skipped" in world["out"]


@then("the command fails")
def fails(world):
    assert world["code"] != 0


@then("the command fails and says to re-enroll")
def fails_re_enroll(world):
    assert world["code"] != 0 and "re-enroll" in world["out"]


@then("no voiceprint exists")
def no_voiceprint_now():
    assert voiceprint.load() is None


@then(parsers.parse('the voiceprint for "{device}" is unchanged'))
def unchanged(world, device):  # noqa: ARG001
    assert voiceprint.path().read_text() == world["before"]

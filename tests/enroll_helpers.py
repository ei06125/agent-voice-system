"""Shared by the enrollment and calibration features: `ears` run with a
stand-in speaker model on synthetic clips."""

from __future__ import annotations

import wave

import numpy as np

from donna_ears import cli, enroll, speaker_models
from synthetic import signal


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


def make_world(tmp_path, monkeypatch, capsys) -> dict:
    """A temporary directory for clips, a stand-in model, captured output."""
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

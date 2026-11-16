"""The speaker embedder: fbank + an ONNX model, on onnxruntime.

Needs the real models (opt-in assets): `python3 tests/speaker_assets.py`.
"""

from __future__ import annotations

import itertools
import wave
from pathlib import Path

import numpy as np
import pytest

from donna_ears import speaker, speaker_models
from synthetic import KINDS, signal

SHERPA = Path(__file__).parent / "data" / "sherpa_embeddings.npz"


@pytest.fixture(scope="module")
def embedders(speaker_assets):
    return {
        name: speaker.Embedder(speaker_assets.model_file(name)) for name in speaker_models.MODELS
    }


def read_clip(path):
    with wave.open(str(path)) as clip:
        assert clip.getframerate() == 16000 and clip.getnchannels() == 1
        raw = clip.readframes(clip.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768


@pytest.mark.parametrize("name", list(speaker_models.MODELS))
@pytest.mark.parametrize("kind", KINDS)
def test_in_sherpa_mode_it_reproduces_sherpa_onnx_exactly(speaker_assets, name, kind):
    embedder = speaker.Embedder(speaker_assets.model_file(name), normalise="sherpa")
    reference = np.load(SHERPA)[f"{name}/{kind}"]
    reference = reference / np.linalg.norm(reference)
    assert float(embedder.embed(signal(kind)) @ reference) > 0.9999


def test_it_reads_scaling_and_normalisation_from_the_model(embedders):
    wespeaker = embedders["wespeaker_en_voxceleb_resnet34"]
    campplus = embedders["3dspeaker_speech_campplus_sv_en_voxceleb_16k"]
    assert (wespeaker.scale, wespeaker.dim) == (32768.0, 256)
    assert (campplus.scale, campplus.dim) == (1.0, 512)
    assert campplus.mean_normalise is True


def test_wespeaker_gets_the_mean_normalisation_it_was_trained_with(embedders, speaker_assets):
    """sherpa-onnx skips it; upstream WeSpeaker applies it, and on real clips
    it is the difference between separating speakers and not."""
    default = embedders["wespeaker_en_voxceleb_resnet34"]
    sherpa = speaker.Embedder(
        speaker_assets.model_file("wespeaker_en_voxceleb_resnet34"), normalise="sherpa"
    )
    assert default.mean_normalise is True and sherpa.mean_normalise is False
    assert default.preprocessing != sherpa.preprocessing


def test_embeddings_are_unit_length_and_deterministic(embedders):
    embedder = embedders[speaker_models.DEFAULT]
    first, second = embedder.embed(signal("vowel")), embedder.embed(signal("vowel"))
    assert first.shape == (embedder.dim,) and first.dtype == np.float32
    assert abs(float(np.linalg.norm(first)) - 1) < 1e-5
    np.testing.assert_array_equal(first, second)


def test_audio_too_short_for_a_single_frame_is_refused(embedders):
    with pytest.raises(speaker.SpeakerError, match="too short"):
        embedders[speaker_models.DEFAULT].embed(np.zeros(40, dtype=np.float32))


def test_on_real_speech_the_default_model_scores_same_speakers_above_different_ones(
    embedders, speaker_assets
):
    embedder = embedders[speaker_models.DEFAULT]
    names = sorted(speaker_assets.CLIPS)
    vectors = {name: embedder.embed(read_clip(speaker_assets.clip_file(name))) for name in names}
    same, different = [], []
    for a, b in itertools.combinations(names, 2):
        score = float(vectors[a] @ vectors[b])
        (same if a.split("-")[0] == b.split("-")[0] else different).append(score)
    assert min(same) > max(different)


def test_centroid_is_the_unit_length_mean():
    a, b = np.array([1, 0], np.float32), np.array([0, 1], np.float32)
    np.testing.assert_allclose(speaker.centroid([a, b]), [2**-0.5, 2**-0.5], atol=1e-6)
    assert speaker.score(a, b) == 0.0


def test_opposite_embeddings_cannot_make_a_centroid():
    with pytest.raises(speaker.SpeakerError):
        speaker.centroid([np.array([1, 0], np.float32), np.array([-1, 0], np.float32)])

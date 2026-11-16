"""Kaldi-compatible log-mel filterbank in pure numpy.

The golden was computed by the implementation whose speaker embeddings matched
sherpa-onnx's (kaldi-native-fbank) to cosine 1.000000 on 14 real clips.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from donna_ears import fbank
from synthetic import KINDS, SAMPLE_RATE, signal

GOLDEN = Path(__file__).parent / "data" / "fbank_golden.npz"


@pytest.mark.parametrize("kind", KINDS)
def test_matches_the_golden_computed_by_the_validated_implementation(kind):
    golden = np.load(GOLDEN)[kind]
    features = fbank.fbank(signal(kind) * 32768)
    np.testing.assert_allclose(features[: len(golden)], golden, rtol=0, atol=1e-4)


@pytest.mark.parametrize(
    ("samples", "frames"),
    [(0, 0), (79, 0), (80, 1), (160, 1), (16000, 100), (16079, 100), (16080, 101)],
)
def test_one_frame_every_10_ms_rounded_like_kaldi_without_snipped_edges(samples, frames):
    features = fbank.fbank(np.zeros(samples, dtype=np.float32))
    assert features.shape == (frames, 80)
    assert features.dtype == np.float32


def test_a_pure_tone_lands_in_the_mel_bin_whose_centre_is_nearest():
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    for hz in (300, 1000, 3000):
        features = fbank.fbank(np.sin(2 * np.pi * hz * t) * 10000)
        centres = fbank.mel_centres_hz()
        assert abs(centres[int(features[50].argmax())] - hz) < 0.1 * hz


def test_doubling_the_amplitude_adds_log_four_everywhere():
    x = signal("vowel") * 32768
    np.testing.assert_allclose(fbank.fbank(2 * x), fbank.fbank(x) + np.log(4), atol=1e-3)


def test_silence_is_floored_not_minus_infinity():
    features = fbank.fbank(np.zeros(1600, dtype=np.float32))
    assert np.isfinite(features).all()

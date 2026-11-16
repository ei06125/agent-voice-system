"""Kaldi-compatible log-mel filterbank features, in pure numpy.

Speaker-embedding models (WeSpeaker, 3D-Speaker) are trained on Kaldi fbank
features, and their ONNX exports take those features rather than audio. This
reproduces kaldi-native-fbank as sherpa-onnx configures it: 25 ms Povey
windows every 10 ms, DC removal, pre-emphasis 0.97, a 512-point power
spectrum, 80 triangular mel bins from 20 Hz to Nyquist - 400 Hz, natural log,
no dither, and `snip_edges=False` (frames centred on every 10 ms, the signal
mirrored at both ends). Validated against sherpa-onnx's speaker embeddings
(cosine 1.000000 on 14 real clips; see tests/test_fbank.py).

numpy is imported inside the functions: modules in this package must import
under a bare python3 (tests/test_plugin_layout.py).
"""

from __future__ import annotations

from functools import lru_cache

SAMPLE_RATE = 16000
FRAME_SHIFT = 160  # 10 ms
FRAME_LENGTH = 400  # 25 ms
FFT_SIZE = 512
NUM_BINS = 80
LOW_HZ = 20.0
HIGH_HZ = SAMPLE_RATE / 2 - 400.0
PREEMPHASIS = 0.97

# Bump when anything above changes: stored voiceprints record it, and a
# profile made with different features must not be compared with new ones.
VERSION = "kaldi-fbank80-povey-v1"


def _mel(hz):
    import numpy as np

    return 1127.0 * np.log1p(np.asarray(hz, dtype=np.float64) / 700.0)


def _mel_to_hz(mel):
    import numpy as np

    return 700.0 * np.expm1(np.asarray(mel, dtype=np.float64) / 1127.0)


@lru_cache(maxsize=1)
def _mel_edges():
    import numpy as np

    return np.linspace(_mel(LOW_HZ), _mel(HIGH_HZ), NUM_BINS + 2)


def mel_centres_hz():
    return _mel_to_hz(_mel_edges()[1:-1])


@lru_cache(maxsize=1)
def _mel_matrix():
    """(FFT_SIZE // 2 + 1, NUM_BINS): power spectrum -> mel energies."""
    import numpy as np

    edges = _mel_edges()
    bins = FFT_SIZE // 2
    fft_mel = _mel(np.arange(bins) * SAMPLE_RATE / FFT_SIZE)
    left, centre, right = edges[:-2, None], edges[1:-1, None], edges[2:, None]
    rising = (fft_mel - left) / (centre - left)
    falling = (right - fft_mel) / (right - centre)
    weights = np.maximum(0.0, np.minimum(rising, falling))  # (NUM_BINS, bins)
    matrix = np.zeros((bins + 1, NUM_BINS))
    matrix[:bins] = weights.T  # Kaldi leaves the Nyquist bin out
    return matrix


@lru_cache(maxsize=1)
def _window():
    import numpy as np

    a = 2 * np.pi * np.arange(FRAME_LENGTH) / (FRAME_LENGTH - 1)
    return (0.5 - 0.5 * np.cos(a)) ** 0.85


def fbank(samples):
    """(frames, 80) float32 log-mel energies for 16 kHz samples.

    Scale matters: WeSpeaker models expect 16-bit-range samples (x 32768),
    3D-Speaker models expect [-1, 1]. The caller applies the model's scaling.
    """
    import numpy as np

    x = np.asarray(samples, dtype=np.float64).ravel()
    n = len(x)
    frames = (n + FRAME_SHIFT // 2) // FRAME_SHIFT
    if frames == 0:
        return np.zeros((0, NUM_BINS), dtype=np.float32)
    starts = np.arange(frames) * FRAME_SHIFT + FRAME_SHIFT // 2 - FRAME_LENGTH // 2
    index = starts[:, None] + np.arange(FRAME_LENGTH)[None, :]
    # Mirror at both ends, repeatedly if the signal is shorter than a window.
    index = np.mod(index, 2 * n)
    index = np.where(index >= n, 2 * n - 1 - index, index)
    windows = x[index]
    windows -= windows.mean(axis=1, keepdims=True)
    windows[:, 1:] -= PREEMPHASIS * windows[:, :-1].copy()
    windows[:, 0] *= 1 - PREEMPHASIS
    windows *= _window()
    power = np.abs(np.fft.rfft(windows, FFT_SIZE)) ** 2
    energies = power @ _mel_matrix()
    floor = np.finfo(np.float32).eps
    return np.log(np.maximum(energies, floor)).astype(np.float32)

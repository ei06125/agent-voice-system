"""Deterministic synthetic test signals: no recorded voice in the repository.

`RandomState` is numpy's legacy generator, whose stream is frozen across
numpy versions, so goldens computed from these signals stay valid.
"""

from __future__ import annotations

SAMPLE_RATE = 16000


def signal(kind: str, seconds: float = 3.0):
    """float32 samples in [-1, 1] at 16 kHz."""
    import numpy as np

    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    rng = np.random.RandomState({"chirp": 1, "vowel": 2, "noise": 3}[kind])
    if kind == "chirp":
        # 100 Hz -> 4 kHz sweep plus a little noise.
        phase = 2 * np.pi * (100 * t + (3900 / (2 * seconds)) * t**2)
        x = 0.3 * np.sin(phase) + 0.01 * rng.standard_normal(n)
    elif kind == "vowel":
        # A buzzy 140 Hz source with formant-like peaks, amplitude-modulated
        # at a syllable rate: closer to speech than a chirp, still synthetic.
        x = np.zeros(n)
        for harmonic in range(1, 30):
            f = 140 * harmonic
            gain = sum(np.exp(-(((f - formant) / 120) ** 2)) for formant in (700, 1200, 2600))
            x += (0.05 + gain) / harmonic * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28))
        x *= 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t) ** 2
        x = 0.25 * x / np.abs(x).max() + 0.003 * rng.standard_normal(n)
    elif kind == "noise":
        x = 0.1 * rng.standard_normal(n)
    else:
        raise ValueError(kind)
    return x.astype(np.float32)


KINDS = ("chirp", "vowel", "noise")

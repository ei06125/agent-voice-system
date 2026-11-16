"""The listener's speaker check with the real default model (opt-in assets)."""

from __future__ import annotations

import logging

import numpy as np

from donna_ears import audio, config, ownercheck, speaker_models, voiceprint
from donna_ears.segmenter import FRAME_SAMPLES, Segment


def segment_of(samples, device="test-mic", start=1000.0):
    frames = [
        samples[i : i + FRAME_SAMPLES]
        for i in range(0, len(samples) - FRAME_SAMPLES + 1, FRAME_SAMPLES)
    ]
    seconds = len(frames) * FRAME_SAMPLES / 16000
    segment = Segment(
        frames=frames, started_at=start, ended_at=start + seconds, voiced_seconds=seconds
    )
    segment.device, segment.epoch, segment.speaker_mode = device, 1, "shadow"
    return segment


def test_shadow_check_recognises_an_enrolled_public_speaker(speaker_assets, monkeypatch):
    monkeypatch.setattr(speaker_models, "model_path", speaker_assets.model_file)
    model = speaker_models.DEFAULT
    embedder = ownercheck.make_embedder(speaker_assets.model_file(model))
    vectors = [
        embedder.embed(audio.decode_file(str(speaker_assets.clip_file(f"leijun-sr-{i}"))))
        for i in (1, 2)
    ]
    profile = voiceprint.Profile.new(
        voiceprint.ModelId(model, speaker_models.MODELS[model].sha256, embedder.preprocessing)
    )
    profile.enroll("test-mic", vectors, seconds=9.0)
    voiceprint.save(profile)

    check = ownercheck.OwnerCheck(config.load(), logging.getLogger("t"), lambda *a: None)
    scores = {}
    for name in speaker_assets.CLIPS:
        if name.startswith("leijun-sr"):
            continue
        samples = audio.decode_file(str(speaker_assets.clip_file(name)))
        result = check.assess(segment_of(samples), samples)
        assert result.active and result.route == "deliver"  # shadow never filters
        scores[name] = result.decision.score
    same = [score for name, score in scores.items() if name.startswith("leijun")]
    others = [score for name, score in scores.items() if not name.startswith("leijun")]
    assert min(same) > max(others)
    assert check.counts["unknown"] == len(scores)  # no thresholds yet: never "owner"
    assert np.isfinite(list(scores.values())).all()

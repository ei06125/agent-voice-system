"""The listener's speaker check, with a stand-in model: failure handling,
reloading, and what shadow mode records."""

from __future__ import annotations

import logging
import os

import numpy as np
import pytest

from donna_ears import config, owner, ownercheck, voiceprint
from donna_ears.segmenter import FRAME_SAMPLES, Segment

MODEL = voiceprint.ModelId("stub-model", "a" * 64, "stub")


class Stub:
    """+ audio is the owner ([1, 0]); - audio is someone else ([0, 1])."""

    preprocessing = "stub"
    fail = False

    def __init__(self, path, threads=1) -> None:  # noqa: ARG002
        pass

    def embed(self, samples):
        if Stub.fail:
            raise RuntimeError("model blew up")
        return np.array([1.0, 0.0] if float(np.mean(samples)) > 0 else [0.0, 1.0], np.float32)


@pytest.fixture
def check(monkeypatch):
    Stub.fail = False
    monkeypatch.setattr(ownercheck, "make_embedder", Stub)
    monkeypatch.setattr(ownercheck, "verified_model", lambda profile: ("stub.onnx", MODEL))
    told: list[str] = []
    made = ownercheck.OwnerCheck(config.load(), logging.getLogger("t"), lambda k, m: told.append(m))
    made.told = told
    return made


def enroll(calibrated=True, device="AirPods"):
    profile = voiceprint.load() or voiceprint.Profile.new(MODEL)
    profile.enroll(device, [[1.0, 0.0]], seconds=40.0)
    if calibrated:
        profile.calibrate(device, accept=0.6, reject=0.3)
    voiceprint.save(profile)


def segment(levels, mode="owner", device="AirPods", start=1000.0, seconds_each=3.0):
    """A segment made of consecutive stretches at the given levels."""
    frames = []
    for level in levels:
        frames += [np.full(FRAME_SAMPLES, level, np.float32)] * int(
            seconds_each * 16000 / FRAME_SAMPLES
        )
    seconds = len(frames) * FRAME_SAMPLES / 16000
    made = Segment(
        frames=frames, started_at=start, ended_at=start + seconds, voiced_seconds=seconds
    )
    made.device, made.epoch, made.speaker_mode = device, 1, mode
    return made, np.concatenate(frames)


def test_counts_start_with_every_key_so_the_state_writer_never_sees_them_grow(check):
    assert set(check.counts) >= {"owner", "other", "unknown", "inactive"}


def test_a_voiceprint_enrolled_mid_session_is_picked_up(check):
    first = check.assess(*segment([0.1]))
    assert first.active is False and check.told  # no voiceprint yet: inactive, said so
    enroll()
    os.utime(voiceprint.path(), (2e9, 2e9))
    second = check.assess(*segment([0.1], start=2000.0))
    assert second.active and second.decision.kind == owner.OWNER


def test_new_thresholds_written_mid_session_are_used(check):
    enroll(calibrated=False)
    assert check.assess(*segment([0.1])).active is False
    enroll(calibrated=True)
    os.utime(voiceprint.path(), (2e9, 2e9))
    result = check.assess(*segment([0.1], start=2000.0))
    assert result.active and result.route == owner.DELIVER


def test_a_model_that_keeps_failing_makes_the_check_inactive_and_says_so_once(check):
    enroll()
    Stub.fail = True
    results = [check.assess(*segment([0.1], start=1000.0 + 10 * i)) for i in range(6)]
    assert all(r.route == owner.DELIVER for r in results[ownercheck.MAX_FAILURES - 1 :])
    assert results[-1].active is False
    assert sum("inactive" in message for message in check.told) == 1


def test_shadow_records_what_owner_mode_would_have_done(check):
    enroll()
    other = check.assess(*segment([-0.1], mode="shadow"))
    assert other.route == owner.DELIVER and other.record()["would"] == owner.DROP
    mine = check.assess(*segment([0.1], mode="shadow", start=2000.0))
    assert mine.record()["would"] == owner.DELIVER


def test_a_long_segment_that_ends_with_someone_else_is_not_the_owners(check):
    enroll()
    result = check.assess(*segment([0.1, 0.1, -0.1, -0.1, -0.1]))  # 6 s owner, 9 s other
    assert result.decision.kind != owner.OWNER and result.route == owner.DROP


def test_a_long_segment_of_the_owner_alone_is_the_owners(check):
    enroll()
    result = check.assess(*segment([0.1] * 5))
    assert result.decision.kind == owner.OWNER

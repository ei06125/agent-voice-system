"""Who said it, and what happens to it: the pure decision rules."""

from __future__ import annotations

import pytest

from donna_ears import owner

CAL = {"accept": 0.6, "reject": 0.3}


def classify(score, voiced=3.0, accept=0.6, reject=0.3, min_s=1.0):
    return owner.classify(score, voiced, accept=accept, reject=reject, min_voiced_s=min_s)


@pytest.mark.parametrize(
    ("score", "kind", "reason"),
    [
        (0.9, owner.OWNER, "match"),
        (0.6, owner.OWNER, "match"),
        (0.45, owner.UNKNOWN, "between"),
        (0.3, owner.UNKNOWN, "between"),
        (0.1, owner.OTHER, "mismatch"),
    ],
)
def test_bands(score, kind, reason):
    decision = classify(score)
    assert (decision.kind, decision.reason, decision.score) == (kind, reason, score)


def test_short_speech_is_never_owner_on_its_own():
    decision = classify(0.95, voiced=0.6)
    assert (decision.kind, decision.reason) == (owner.UNKNOWN, "short")


def test_short_speech_that_clearly_is_not_the_owner_is_still_other():
    assert classify(0.05, voiced=0.6).kind == owner.OTHER


def test_without_a_score_or_thresholds_it_is_unknown_never_owner():
    assert classify(None).reason == "no-score"
    assert classify(0.99, accept=None, reject=None).reason == "uncalibrated"


def test_routing():
    assert owner.route(classify(0.9), follows_owner=False) == owner.DELIVER
    assert owner.route(classify(0.1), follows_owner=True) == owner.DROP
    assert owner.route(classify(0.45), follows_owner=True) == owner.DROP
    assert owner.route(classify(0.9, voiced=0.5), follows_owner=True) == owner.UNVERIFIED
    assert owner.route(classify(0.9, voiced=0.5), follows_owner=False) == owner.DROP
    assert owner.route(classify(None), follows_owner=True) == owner.DROP


class TestFollow:
    """A short reply right after the owner may be delivered, marked unverified."""

    def test_follows_recent_owner_speech_on_the_same_device_and_epoch(self):
        follow = owner.Follow(window_s=5.0)
        follow.note(classify(0.9), device="AirPods", epoch=1, ended_at=100.0)
        assert follow.allows(device="AirPods", epoch=1, started_at=104.0)

    def test_overlapping_boundaries_still_follow(self):
        follow = owner.Follow(window_s=5.0)
        follow.note(classify(0.9), device="AirPods", epoch=1, ended_at=100.0)
        assert follow.allows(device="AirPods", epoch=1, started_at=99.8)

    def test_nothing_to_follow_at_first(self):
        assert not owner.Follow(window_s=5.0).allows(device="AirPods", epoch=1, started_at=1.0)

    @pytest.mark.parametrize(
        ("device", "epoch", "started_at"),
        [("Logitech BRIO", 1, 101.0), ("AirPods", 2, 101.0), ("AirPods", 1, 105.5)],
        ids=["device-changed", "capture-reset-or-reply", "gap-too-long"],
    )
    def test_continuity_resets(self, device, epoch, started_at):
        follow = owner.Follow(window_s=5.0)
        follow.note(classify(0.9), device="AirPods", epoch=1, ended_at=100.0)
        assert not follow.allows(device=device, epoch=epoch, started_at=started_at)

    def test_only_owner_decisions_extend_it(self):
        follow = owner.Follow(window_s=5.0)
        follow.note(classify(0.9), device="AirPods", epoch=1, ended_at=100.0)
        follow.note(classify(0.9, voiced=0.5), device="AirPods", epoch=1, ended_at=104.0)
        assert not follow.allows(device="AirPods", epoch=1, started_at=106.0)

    def test_someone_else_speaking_ends_it(self):
        follow = owner.Follow(window_s=5.0)
        follow.note(classify(0.9), device="AirPods", epoch=1, ended_at=100.0)
        follow.note(classify(0.1), device="AirPods", epoch=1, ended_at=102.0)
        assert not follow.allows(device="AirPods", epoch=1, started_at=103.0)


def test_a_short_other_verdict_is_too_noisy_to_end_the_owners_turn():
    follow = owner.Follow(window_s=5.0)
    follow.note(classify(0.9), device="AirPods", epoch=1, ended_at=100.0)
    follow.note(classify(0.1, voiced=0.5), device="AirPods", epoch=1, ended_at=101.0, short=True)
    assert follow.allows(device="AirPods", epoch=1, started_at=102.0)

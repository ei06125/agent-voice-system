"""Choosing per-device thresholds from scored trials: pure arithmetic."""

from __future__ import annotations

import pytest

from donna_ears import calibrate


def test_equal_error_rate_of_separated_scores_is_zero():
    assert calibrate.eer([0.8, 0.9, 0.7], [0.1, 0.2, 0.3]) == 0.0


def test_equal_error_rate_of_identical_scores_is_a_half():
    assert calibrate.eer([0.5, 0.5], [0.5, 0.5]) == pytest.approx(0.5)


def test_equal_error_rate_with_overlap():
    owner = [0.9, 0.8, 0.7, 0.4]
    others = [0.1, 0.2, 0.3, 0.75]
    assert calibrate.eer(owner, others) == pytest.approx(0.25)


def test_thresholds_accept_above_every_impostor_and_reject_below_almost_every_owner_trial():
    owner = [0.62 + 0.01 * i for i in range(30)]
    others = [0.05 + 0.01 * i for i in range(30)]  # up to 0.34
    proposal = calibrate.propose(owner, others, max_owner_rejected=0.05)
    assert proposal.accept > max(others)
    assert proposal.reject <= sorted(owner)[1]
    assert proposal.reject < proposal.accept
    assert proposal.overlap is False


def test_when_owner_and_others_overlap_it_says_so_and_still_leaves_an_unknown_band():
    owner = [0.3, 0.5, 0.7, 0.9] * 6
    others = [0.2, 0.4, 0.6, 0.8] * 6
    proposal = calibrate.propose(owner, others)
    assert proposal.overlap is True
    assert proposal.reject < proposal.accept


def test_too_few_trials_is_refused():
    with pytest.raises(ValueError, match="at least"):
        calibrate.propose([0.9] * 3, [0.1] * 30)
    with pytest.raises(ValueError, match="at least"):
        calibrate.propose([0.9] * 30, [0.1] * 3)


def test_rule_of_three_bound_on_false_accepts():
    assert calibrate.false_accept_bound(0, 300) == pytest.approx(0.01)
    assert calibrate.false_accept_bound(2, 100) == pytest.approx(0.02)


def test_windows_cut_a_clip_into_non_overlapping_pieces():
    samples = list(range(16000 * 5))
    pieces = calibrate.windows(samples, seconds=2.0)
    assert [len(p) for p in pieces] == [32000, 32000]
    assert calibrate.windows(samples, seconds=0) == [samples]

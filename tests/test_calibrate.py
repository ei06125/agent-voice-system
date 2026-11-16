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


def test_a_proposal_that_would_shut_the_owner_out_is_flagged():
    owner = [0.5 + 0.01 * i for i in range(30)]  # 0.50 .. 0.79
    others = [0.1] * 29 + [0.7]  # one contaminated clip: the owner audible in it
    proposal = calibrate.propose(owner, others)
    assert proposal.overlap is True
    (problem,) = calibrate.problems(proposal, owner_long=owner)
    assert "of your speech" in problem


def test_a_clean_proposal_has_no_problems():
    owner = [0.62 + 0.01 * i for i in range(30)]
    others = [0.05 + 0.01 * i for i in range(30)]
    proposal = calibrate.propose(owner, others)
    assert calibrate.problems(proposal, owner_long=owner) == []


def test_the_false_accept_bound_counts_distinct_recordings_not_windows():
    owner = [0.8] * 40
    others = [0.1] * 300  # 300 windows, but cut from 10 recordings
    proposal = calibrate.propose(owner, others, other_recordings=10)
    assert proposal.false_accept_bound == pytest.approx(0.3)


def test_the_share_of_other_voices_that_could_slip_through_as_unverified_is_reported():
    owner = [0.62 + 0.01 * i for i in range(30)]
    others = [0.05 + 0.01 * i for i in range(30)]  # reject lands at 0.31
    proposal = calibrate.propose(owner, others, others_short=[0.05] * 8 + [0.65] * 2)
    assert proposal.short_leak == pytest.approx(0.2)

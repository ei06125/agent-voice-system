"""Per-device thresholds for owner-only listening, from the owner's recordings.

The owner records held-out clips of their own speech and someone else's (a
podcast, a video, a person in the room, played or spoken near the same
microphone). Each clip, and windows cut from it at the lengths the listener
actually sees, is scored against the device's voiceprint. Then:

* accept: above every score from someone else (so no false accepts in the
  data; the rule of three bounds what that is worth);
* reject: below all but `max_owner_rejected` of the owner's scores, so the
  owner is rarely discarded outright; between the two is "unknown".

Pure arithmetic, standard library only; `ears calibrate` does the audio.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_TRIALS = 20
MARGIN = 0.02  # above the highest impostor score
MIN_BAND = 0.05  # accept - reject is never smaller than this


@dataclass(frozen=True)
class Proposal:
    accept: float
    reject: float
    eer: float
    owner_trials: int
    other_trials: int
    owner_rejected: float  # share of owner trials below `reject`
    owner_unknown: float  # share of owner trials between the thresholds
    false_accept_bound: float  # 95% upper bound on accepting someone else
    overlap: bool  # some other-speaker trial scored at or above some owner trial


def eer(owner: list[float], others: list[float]) -> float:
    """Equal error rate: where false rejects and false accepts are equal."""
    best = (float("inf"), 1.0)  # (|frr - far|, (frr + far) / 2) at the crossing
    for threshold in sorted(set(owner) | set(others)) + [float("inf")]:
        frr = sum(score < threshold for score in owner) / len(owner)
        far = sum(score >= threshold for score in others) / len(others)
        best = min(best, (abs(frr - far), (frr + far) / 2))
    return best[1]


def false_accept_bound(accepted: int, trials: int) -> float:
    """With none accepted, the 95% upper bound on the false-accept rate (the
    rule of three, 3/n); otherwise the observed rate."""
    if accepted == 0:
        return 3.0 / trials
    return accepted / trials


def propose(owner: list[float], others: list[float], max_owner_rejected: float = 0.02) -> Proposal:
    if len(owner) < MIN_TRIALS or len(others) < MIN_TRIALS:
        raise ValueError(
            f"need at least {MIN_TRIALS} owner and {MIN_TRIALS} other-speaker trials "
            f"(have {len(owner)} and {len(others)})"
        )
    accept = max(others) + MARGIN
    ranked = sorted(owner)
    reject = ranked[int(max_owner_rejected * len(ranked))]
    reject = min(reject, accept - MIN_BAND)
    return Proposal(
        accept=round(accept, 4),
        reject=round(reject, 4),
        eer=eer(owner, others),
        owner_trials=len(owner),
        other_trials=len(others),
        owner_rejected=sum(score < reject for score in owner) / len(owner),
        owner_unknown=sum(reject <= score < accept for score in owner) / len(owner),
        false_accept_bound=false_accept_bound(0, len(others)),
        overlap=max(others) >= min(owner),
    )


def windows(samples, seconds: float, rate: int = 16000) -> list:
    """Non-overlapping `seconds`-long pieces of `samples` (the whole clip for 0)."""
    if seconds <= 0:
        return [samples]
    size = int(seconds * rate)
    return [samples[start : start + size] for start in range(0, len(samples) - size + 1, size)]

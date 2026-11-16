"""Owner-only listening: is this segment the owner, and what happens to it.

Pure rules, no audio, no model: the listener feeds them a similarity score
and timing, and acts on the answer.

* `classify` turns a score into owner / other / unknown. Anything uncertain
  is unknown, never owner: no score, no thresholds for this device, a score
  between the two thresholds, or too little voiced speech to judge.
* `route` decides what happens: the owner's speech is delivered; someone
  else's is dropped; unknown is dropped too, except a short reply that
  directly follows the owner on the same device (`Follow`), which is
  delivered marked as unverified. "yes" and "stop" are too short to verify,
  and this is how they still get through without letting a television's
  short phrases in.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

OWNER = "owner"
OTHER = "other"
UNKNOWN = "unknown"

DELIVER = "deliver"
UNVERIFIED = "unverified"
DROP = "drop"


@dataclass(frozen=True)
class Decision:
    kind: str
    reason: str
    score: float | None


def classify(
    score: float | None,
    voiced_s: float,
    *,
    accept: float | None,
    reject: float | None,
    min_voiced_s: float,
) -> Decision:
    if score is None:
        return Decision(UNKNOWN, "no-score", None)
    if accept is None or reject is None:
        return Decision(UNKNOWN, "uncalibrated", score)
    if score < reject:
        return Decision(OTHER, "mismatch", score)
    if voiced_s < min_voiced_s:
        return Decision(UNKNOWN, "short", score)
    if score >= accept:
        return Decision(OWNER, "match", score)
    return Decision(UNKNOWN, "between", score)


def route(decision: Decision, follows_owner: bool) -> str:
    if decision.kind == OWNER:
        return DELIVER
    if decision.kind == UNKNOWN and decision.reason == "short" and follows_owner:
        return UNVERIFIED
    return DROP


class Follow:
    """Remembers the owner's last segment, to let a short reply follow it.

    Continuity holds only on the same device and the same capture epoch (the
    listener bumps the epoch whenever capture restarts, a reply plays, or the
    gate closes), and for `window_s` from the end of the owner's segment.
    Only an owner decision extends it; anyone else speaking ends it.
    """

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._last: tuple[str, int, float] | None = None

    def note(self, decision: Decision, *, device: str, epoch: int, ended_at: float) -> None:
        if decision.kind == OWNER:
            self._last = (device, epoch, ended_at)
        elif decision.kind == OTHER:
            self._last = None

    def allows(self, *, device: str, epoch: int, started_at: float) -> bool:
        if self._last is None:
            return False
        last_device, last_epoch, ended_at = self._last
        return (
            device == last_device
            and epoch == last_epoch
            # Segments come in order; pre-roll can make one start a hair
            # before the last one's recorded end, so there is no lower bound.
            and started_at - ended_at <= self.window_s
        )

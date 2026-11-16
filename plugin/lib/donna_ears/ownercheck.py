"""The speaker check inside the listener's transcription worker.

For each segment: whose voice is it, and may it be delivered? It loads the
voiceprint and the speaker model lazily, in the worker thread (never in the
capture thread, and it never downloads anything: `ears enroll` did that).

When it cannot check (no voiceprint, a damaged one, the model missing, a
device with no voiceprint or no thresholds) it is *visibly inactive*: the
segment is delivered as it would be without the check, and the problem is
reported once. It never classifies anyone as the owner without a score, and
it never goes silently deaf.

Modes, from `speaker` in config.json or `ears speaker` at runtime:
  off     nothing is checked
  shadow  every segment is checked and the verdict recorded; delivery unchanged
  owner   only the owner is delivered (owner.route)

Only the verdict and its reason are logged: never words, never scores. The
score goes to the private transcript record, where calibration can use it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from . import owner, speaker, speaker_models, voiceprint

MODES = ("off", "shadow", "owner")


def make_embedder(path, threads=1):
    return speaker.Embedder(path, threads=threads)


def verified_model(profile: voiceprint.Profile):
    """(path, ModelId) for the model the voiceprint was made with, or ProfileError."""
    path = speaker_models.model_path(profile.model.name)
    if not path.exists():
        raise voiceprint.ProfileError(
            f"the speaker model {profile.model.name} is missing: run `ears enroll` again"
        )
    sha256 = speaker_models.sha256_of(path)
    if sha256 != profile.model.sha256:
        raise voiceprint.ProfileError(
            f"the speaker model file {path.name} does not match the voiceprint: re-enroll"
        )
    return path, voiceprint.ModelId(profile.model.name, sha256, profile.model.preprocessing)


MAX_FAILURES = 3  # consecutive model failures before the check goes inactive
COUNT_KEYS = ("owner", "other", "unknown", "inactive")


@dataclass(frozen=True)
class Assessment:
    decision: owner.Decision
    route: str  # owner.DELIVER / UNVERIFIED / DROP
    active: bool  # False: the check could not run (or is uncalibrated); route is DELIVER
    would: str | None = None  # what owner mode would do (shadow review); None if not judged

    def record(self) -> dict:
        score = self.decision.score
        return {
            "kind": self.decision.kind,
            "why": self.decision.reason,
            "score": None if score is None else round(score, 3),
            "route": self.route,
            "would": self.would,
        }


class OwnerCheck:
    def __init__(self, cfg: dict, log, report) -> None:
        """`report(key, message)` tells the user, at most once per key."""
        self.log = log
        self.report = report
        self.min_voiced_s = float(cfg["speaker_min_s"])
        self.max_embed_s = float(cfg["speaker_max_s"])
        self.threads = int(cfg["speaker_threads"])
        self.alias = str(cfg["speaker_device"]).strip()
        self.follow = owner.Follow(float(cfg["speaker_follow_s"]))
        # Every key up front: the capture thread copies this for the state
        # file while the worker updates it, and a dict must not grow meanwhile.
        self.counts: Counter = Counter({key: 0 for key in COUNT_KEYS})
        self._stamp: tuple | None = None  # the voiceprint file's (mtime, size) when loaded
        self._unavailable: str | None = None
        self._profile: voiceprint.Profile | None = None
        self._embedder = None
        self._embedder_model: voiceprint.ModelId | None = None
        self._failures = 0
        self._warned: set[str] = set()

    def _file_stamp(self) -> tuple | None:
        try:
            info = voiceprint.path().stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _refresh(self) -> None:
        """(Re)load the voiceprint when it changed: `ears enroll` and `ears
        calibrate --write` take effect in a running session."""
        stamp = self._file_stamp()
        if self._profile is not None or self._unavailable is not None:
            if stamp == self._stamp:
                return
        self._stamp = stamp
        self._unavailable = None
        self._warned.clear()
        self._failures = 0
        try:
            profile = voiceprint.load()
            if profile is None or not profile.devices:
                raise voiceprint.ProfileError("there is no voiceprint (ears enroll)")
            if self._embedder is None or self._embedder_model != profile.model:
                path, model = verified_model(profile)
                self._embedder = make_embedder(path, threads=self.threads)
                self._embedder_model = profile.model
            else:
                model = self._embedder_model
            profile.check(
                voiceprint.ModelId(model.name, model.sha256, self._embedder.preprocessing)
            )
        except voiceprint.ProfileError as exc:
            self._unavailable, self._profile = str(exc), None
            return
        except Exception as exc:  # noqa: BLE001 - a broken model must not stop listening
            self._unavailable = f"the speaker model failed to load ({type(exc).__name__})"
            self._profile = None
            return
        self._profile = profile
        self.log.info("speaker check ready: %s device(s)", len(profile.devices))

    def _tell(self, key: str, message: str, mode: str) -> None:
        """Owner mode reports to the user; shadow mode only logs, once."""
        if key in self._warned:
            return
        self._warned.add(key)
        if mode == "owner":
            self.report(key, f"owner-only listening is inactive: {message}")
        else:
            self.log.warning("speaker check inactive: %s", message)

    def _inactive(self, why: str, score: float | None = None) -> Assessment:
        self.counts["inactive"] += 1
        return Assessment(owner.Decision(owner.UNKNOWN, why, score), owner.DELIVER, False)

    def _score(self, samples, centroid) -> float | None:
        """Similarity of the segment's start and, for a long one, its end.

        A segment longer than speaker_max_s is judged on both ends and gets
        the lower score: if someone else took over, it is not the owner's.
        """
        limit = int(self.max_embed_s * speaker.fbank.SAMPLE_RATE)
        pieces = [samples[:limit]]
        if len(samples) > limit:
            pieces.append(samples[-limit:])
        scores = []
        for piece in pieces:
            try:
                scores.append(speaker.score(self._embedder.embed(piece), centroid))
            except speaker.SpeakerError:
                return None  # too short to embed: no score, so not the owner
        self._failures = 0
        return min(scores)

    def assess(self, segment, samples) -> Assessment | None:
        mode = getattr(segment, "speaker_mode", "off")
        if mode not in ("shadow", "owner"):
            return None
        self._refresh()
        if self._unavailable:
            self._tell("speaker-unavailable", self._unavailable, mode)
            return self._inactive("unavailable")
        if self._failures >= MAX_FAILURES:
            return self._inactive("failing")
        device = self.alias or segment.device
        entry = self._profile.device(device)
        if entry is None:
            self._tell(
                f"speaker-device:{device}",
                f"there is no voiceprint for {device!r} (ears enroll --device ...)",
                mode,
            )
            return self._inactive("no-profile")

        try:
            score = self._score(samples, entry.centroid)
        except Exception as exc:  # noqa: BLE001 - one bad segment must not stop listening
            self._failures += 1
            self.log.warning("speaker check failed on a segment (%s)", type(exc).__name__)
            if self._failures >= MAX_FAILURES:
                self._tell(
                    "speaker-failing",
                    f"the speaker model keeps failing ({type(exc).__name__})",
                    mode,
                )
                return self._inactive("failing")
            score = None

        decision = owner.classify(
            score,
            segment.voiced_seconds,
            accept=entry.accept,
            reject=entry.reject,
            min_voiced_s=self.min_voiced_s,
        )
        follows = self.follow.allows(
            device=device, epoch=segment.epoch, started_at=segment.started_at
        )
        self.follow.note(
            decision,
            device=device,
            epoch=segment.epoch,
            ended_at=segment.ended_at,
            short=segment.voiced_seconds < self.min_voiced_s,
        )
        self.counts[decision.kind] += 1
        self.log.info("speaker: %s (%s) on %s, %s", decision.kind, decision.reason, device, mode)
        would = owner.route(decision, follows)

        if mode == "owner" and entry.calibrated:
            return Assessment(decision, would, True, would)
        if mode == "owner":
            self._tell(
                f"speaker-calibration:{device}",
                f"{device!r} has no thresholds yet (calibrate it first)",
                mode,
            )
            return Assessment(decision, owner.DELIVER, False, would)
        return Assessment(decision, owner.DELIVER, True, would)

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


@dataclass(frozen=True)
class Assessment:
    decision: owner.Decision
    route: str  # owner.DELIVER / UNVERIFIED / DROP
    active: bool  # False: the check could not run; route is DELIVER

    def record(self) -> dict:
        score = self.decision.score
        return {
            "kind": self.decision.kind,
            "why": self.decision.reason,
            "score": None if score is None else round(score, 3),
            "route": self.route,
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
        self.counts: Counter = Counter()
        self._loaded = False
        self._unavailable: str | None = None
        self._profile: voiceprint.Profile | None = None
        self._embedder = None
        self._warned: set[str] = set()

    def _load(self) -> None:
        self._loaded = True
        try:
            profile = voiceprint.load()
            if profile is None or not profile.devices:
                raise voiceprint.ProfileError("there is no voiceprint (ears enroll)")
            path, model = verified_model(profile)
            embedder = make_embedder(path, threads=self.threads)
            profile.check(voiceprint.ModelId(model.name, model.sha256, embedder.preprocessing))
        except voiceprint.ProfileError as exc:
            self._unavailable = str(exc)
            return
        except Exception as exc:  # noqa: BLE001 - a broken model must not stop listening
            self._unavailable = f"the speaker model failed to load ({type(exc).__name__})"
            return
        self._profile, self._embedder = profile, embedder
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

    def assess(self, segment, samples) -> Assessment | None:
        mode = getattr(segment, "speaker_mode", "off")
        if mode not in ("shadow", "owner"):
            return None
        if not self._loaded:
            self._load()
        if self._unavailable:
            self._tell("speaker-unavailable", self._unavailable, mode)
            return self._inactive("unavailable")
        device = self.alias or segment.device
        entry = self._profile.device(device)
        if entry is None:
            self._tell(
                f"speaker-device:{device}",
                f"there is no voiceprint for {device!r} (ears enroll --device ...)",
                mode,
            )
            return self._inactive("no-profile")

        score = None
        try:
            limit = int(self.max_embed_s * speaker.fbank.SAMPLE_RATE)
            score = speaker.score(self._embedder.embed(samples[:limit]), entry.centroid)
        except speaker.SpeakerError:
            pass  # too short to embed: no score, so not the owner
        except Exception as exc:  # noqa: BLE001 - one bad segment must not stop listening
            self.log.warning("speaker check failed on a segment (%s)", type(exc).__name__)

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
        self.follow.note(decision, device=device, epoch=segment.epoch, ended_at=segment.ended_at)
        self.counts[decision.kind] += 1
        self.log.info("speaker: %s (%s) on %s, %s", decision.kind, decision.reason, device, mode)

        if mode == "owner" and entry.calibrated:
            return Assessment(decision, owner.route(decision, follows), True)
        if mode == "owner":
            self._tell(
                f"speaker-calibration:{device}",
                f"{device!r} has no thresholds yet (calibrate it first)",
                mode,
            )
            return Assessment(decision, owner.DELIVER, False)
        return Assessment(decision, owner.DELIVER, True)

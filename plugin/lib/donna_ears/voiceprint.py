"""The owner's voiceprint: one embedding centroid per input device.

A voiceprint is biometric data. It lives in one private file
(`~/.donna/voiceprint.json`, mode 0600), is never logged, never printed and
never sent to the model, and `ears enroll --delete` removes it.

The file records the model's checksum and the feature preprocessing it was
made with: embeddings from a different model or different features are not
comparable, so such a profile is refused rather than silently mis-scored.

Thresholds are per device and optional. They come from an evaluation on the
owner's own recordings; until a device has them, nothing it hears can be
classified as the owner (see owner.py).

Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

FORMAT = 1


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelId:
    name: str
    sha256: str
    preprocessing: str


@dataclass
class DeviceProfile:
    centroid: list[float]
    clips: int
    seconds: float
    enrolled_at: float
    accept: float | None = None
    reject: float | None = None

    @property
    def calibrated(self) -> bool:
        return self.accept is not None and self.reject is not None


@dataclass
class Profile:
    model: ModelId
    devices: dict[str, DeviceProfile] = field(default_factory=dict)

    @classmethod
    def new(cls, model: ModelId) -> Profile:
        return cls(model=model)

    def check(self, model: ModelId) -> None:
        if model != self.model:
            raise ProfileError(
                f"the voiceprint was made with {self.model.name} "
                f"({self.model.sha256[:12]}, {self.model.preprocessing}), not "
                f"{model.name} ({model.sha256[:12]}, {model.preprocessing}): re-enroll"
            )

    def device(self, name: str) -> DeviceProfile | None:
        return self.devices.get(name)

    def enroll(
        self, device: str, embeddings: list, seconds: float, now: float | None = None
    ) -> DeviceProfile:
        """Replace `device`'s voiceprint with the centroid of `embeddings`."""
        vectors = [[float(v) for v in vector] for vector in embeddings]
        if not vectors:
            raise ValueError("enrolling needs at least one embedding")
        mean = [sum(column) / len(vectors) for column in zip(*vectors, strict=True)]
        norm = math.sqrt(sum(v * v for v in mean))
        if norm == 0:
            raise ValueError("the embeddings cancel out")
        entry = DeviceProfile(
            centroid=[v / norm for v in mean],
            clips=len(vectors),
            seconds=round(float(seconds), 2),
            enrolled_at=time.time() if now is None else now,
        )
        self.devices[device] = entry
        return entry

    def calibrate(self, device: str, accept: float, reject: float) -> None:
        if not reject < accept:
            raise ValueError("the reject threshold must be below the accept threshold")
        entry = self.devices[device]
        entry.accept, entry.reject = float(accept), float(reject)


def path() -> Path:
    return config.home() / "voiceprint.json"


def save(profile: Profile) -> None:
    data = {
        "format": FORMAT,
        "model": asdict(profile.model),
        "devices": {name: asdict(entry) for name, entry in profile.devices.items()},
    }
    # mkstemp creates the file 0600 before anything is written to it.
    config.write_json_atomic(path(), data)


def load() -> Profile | None:
    """The stored profile, None if there is none; ProfileError if unusable."""
    try:
        text = path().read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProfileError(f"cannot read the voiceprint: {exc.strerror}") from None
    try:
        data = json.loads(text)
        if data.get("format") != FORMAT:
            raise ProfileError(f"unsupported voiceprint format {data.get('format')!r}")
        model = ModelId(**data["model"])
        devices = {name: DeviceProfile(**entry) for name, entry in data["devices"].items()}
    except ProfileError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ProfileError("the voiceprint file is damaged: re-enroll") from None
    return Profile(model=model, devices=devices)


def delete() -> bool:
    try:
        os.unlink(path())
    except FileNotFoundError:
        return False
    return True

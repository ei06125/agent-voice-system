"""Speaker-embedding models: which ones we know, where they come from, and a
download that only keeps a file whose sha256 matches the pin.

Standard library only, so `ears enroll` and `ears status` can use it without
the audio environment.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"


@dataclass(frozen=True)
class Model:
    url: str
    sha256: str


def _pinned(name: str, sha256: str) -> tuple[str, Model]:
    return name, Model(url=f"{RELEASE}/{name}.onnx", sha256=sha256)


# The candidates for the bake-off (see .claude/research/voice-biometrics.md §4).
MODELS: dict[str, Model] = dict(
    [
        _pinned(
            "wespeaker_en_voxceleb_resnet34",
            "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94",
        ),
        _pinned(
            "wespeaker_en_voxceleb_resnet34_LM",
            "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
        ),
        _pinned(
            "3dspeaker_speech_campplus_sv_en_voxceleb_16k",
            "357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b",
        ),
    ]
)

# Until the bake-off on the owner's recordings picks one.
DEFAULT = "wespeaker_en_voxceleb_resnet34"


class ModelError(RuntimeError):
    pass


def model_path(name: str) -> Path:
    return config.models_dir() / f"{name}.onnx"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, sha256: str, target: Path) -> Path:
    """Make `target` a verified copy of `url`, downloading only if needed."""
    if target.exists() and sha256_of(target) == sha256:
        return target
    curl = shutil.which("curl")
    if not curl:
        raise ModelError("curl is not available to download the speaker model")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    result = subprocess.run(
        [curl, "-sSL", "--fail", "--retry", "3", "-o", str(part), url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        part.unlink(missing_ok=True)
        raise ModelError(f"downloading {url} failed: {result.stderr.strip()[:200]}")
    if sha256_of(part) != sha256:
        part.unlink(missing_ok=True)
        raise ModelError(f"{url}: checksum does not match the pinned sha256")
    os.replace(part, target)
    return target


def ensure(name: str) -> Path:
    """The verified local file for a known model, downloaded on first use."""
    try:
        model = MODELS[name]
    except KeyError:
        raise ModelError(f"unknown speaker model {name!r}") from None
    return fetch(model.url, model.sha256, model_path(name))

"""Opt-in assets for the speaker tests: real models and public speech clips.

They are not in the repository (models are large; the clips are other people's
voices). Fetch them once, with every file checked against a pinned sha256:

    python3 tests/speaker_assets.py            # into ~/.cache/donna/test-assets
    DONNA_TEST_ASSETS=/some/dir python3 tests/speaker_assets.py

Tests that need them skip when they are missing. The clips are sherpa-onnx's
speaker-identification examples: three speakers, Mandarin, 16 kHz mono. They
prove the plumbing and the model maths; they say nothing about where the
owner's thresholds belong.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLIPS = {
    "fangjun-sr-1": "33c24061180224d2350143ee19e3af031446995c676bd25996325d34bb20a4d5",
    "fangjun-sr-2": "c9209ff0cc83cb3c30b3efb376cec6df93f33ceb0f3c1a8eb28fced7a8d08409",
    "fangjun-sr-3": "5a5f2fc98cb95cde4e457c31349dcbd5cf7ec2567528aac11487aa3884795bd8",
    "fangjun-test-sr-1": "9175e523081bf6a630ce72a55b05f92148eaafaf58cbbbe743686cd81c50848e",
    "fangjun-test-sr-2": "c55912144a28817a67d5eac806e5952ffcd29b82db3925f46d33df4c5f038907",
    "leijun-sr-1": "160a3d9bf5dd5038da8191b4430e1f3f751461613ae9489401b6b35a61b488ad",
    "leijun-sr-2": "37a759c036f2520d143708dfe46d45901b38a0a6e621b52d6f1aef2c000d7fe0",
    "leijun-test-sr-1": "36cda04ee4d10e38095de73b77c99e8e7c54347232967a9cde4cf54f7d496bab",
    "leijun-test-sr-2": "84b85e7413b5348303a95bd78813e4d669b4dc70fa5a99990b9860ca6fda91e8",
    "leijun-test-sr-3": "ef026f55e457780fc066b0f5d22d944e102690b2d77c7c0536e8d84f55a034a8",
    "liudehua-sr-1": "f39ebb7357d537009a9b7e59a6c9c0199f2204fcffb3c2d9d829a6961813eca1",
    "liudehua-sr-2": "0f0afddb99d381a23c899f464864f692dc21190cebbd4edf4ff695e10a107c9c",
    "liudehua-test-sr-1": "a855706ad8e8253e341e3f65892c66d7dfb4bdc83675bcf38b863f2f8d167553",
    "liudehua-test-sr-2": "2c9ba09e7fe9deca8bea0b67776c39665cad3a1036e7a8d3407d1e45e2ede23a",
}


def asset_dir() -> Path:
    default = Path("~/.cache/donna/test-assets").expanduser()
    return Path(os.environ.get("DONNA_TEST_ASSETS") or default)


def model_file(name: str) -> Path:
    return asset_dir() / "models" / f"{name}.onnx"


def clip_file(name: str) -> Path:
    return asset_dir() / "clips" / f"{name}.wav"


def missing() -> list[str]:
    from donna_ears import speaker_models

    paths = [model_file(name) for name in speaker_models.MODELS]
    paths += [clip_file(name) for name in CLIPS]
    return [str(path) for path in paths if not path.exists()]


def fetch_all() -> None:
    from donna_ears import speaker_models

    for name, model in speaker_models.MODELS.items():
        speaker_models.fetch(model.url, model.sha256, model_file(name))
        print(f"ok  {name}")
    for name, digest in CLIPS.items():
        speaker_models.fetch(f"{speaker_models.RELEASE}/{name}.wav", digest, clip_file(name))
        print(f"ok  {name}.wav")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "lib"))
    fetch_all()

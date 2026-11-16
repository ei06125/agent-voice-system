"""Speaker models are downloaded by name and checked against a pinned sha256."""

from __future__ import annotations

import hashlib
import re

import pytest

from donna_ears import speaker_models


def test_every_known_model_is_pinned_to_a_release_url_and_a_checksum():
    assert speaker_models.MODELS
    for name, model in speaker_models.MODELS.items():
        assert model.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
        assert model.url.endswith(f"/{name}.onnx")
        assert re.fullmatch(r"[0-9a-f]{64}", model.sha256)


def test_the_default_model_is_a_known_one():
    assert speaker_models.DEFAULT in speaker_models.MODELS


def _served(tmp_path, payload: bytes):
    source = tmp_path / "served" / "m.onnx"
    source.parent.mkdir()
    source.write_bytes(payload)
    return source.as_uri(), hashlib.sha256(payload).hexdigest()


def test_fetch_downloads_and_verifies(tmp_path):
    url, digest = _served(tmp_path, b"model bytes")
    target = speaker_models.fetch(url, digest, tmp_path / "models" / "m.onnx")
    assert target.read_bytes() == b"model bytes"


def test_fetch_refuses_a_file_with_the_wrong_checksum_and_leaves_nothing(tmp_path):
    url, _ = _served(tmp_path, b"tampered")
    target = tmp_path / "models" / "m.onnx"
    with pytest.raises(speaker_models.ModelError, match="checksum"):
        speaker_models.fetch(url, "0" * 64, target)
    assert not target.exists()
    assert not list(target.parent.glob("*"))


def test_fetch_keeps_a_verified_file_without_downloading_again(tmp_path):
    target = tmp_path / "m.onnx"
    target.write_bytes(b"already here")
    digest = hashlib.sha256(b"already here").hexdigest()
    assert speaker_models.fetch("file:///nonexistent", digest, target) == target


def test_fetch_replaces_a_file_that_no_longer_verifies(tmp_path):
    url, digest = _served(tmp_path, b"good")
    target = tmp_path / "m.onnx"
    target.write_bytes(b"corrupt")
    speaker_models.fetch(url, digest, target)
    assert target.read_bytes() == b"good"


def test_model_path_lives_in_the_models_directory(donna_home):
    path = speaker_models.model_path(speaker_models.DEFAULT)
    assert path == donna_home / "models" / f"{speaker_models.DEFAULT}.onnx"

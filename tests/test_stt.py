"""Speech-to-text plumbing that can be checked without a model or a server."""

from __future__ import annotations

import io
import logging
import shutil
import wave

import pytest

from donna_ears import config, stt


def cfg(**overrides):
    merged = dict(config.DEFAULTS)
    merged.update(overrides)
    return merged


def test_a_model_name_resolves_into_the_models_directory():
    path = stt.model_path(cfg(model="large-v3-turbo"))
    assert path == config.models_dir() / "ggml-large-v3-turbo.bin"
    assert stt.model_is_named(cfg(model="large-v3-turbo"))


def test_a_model_path_is_used_as_given(tmp_path):
    explicit = tmp_path / "my-model.bin"
    assert stt.model_path(cfg(model=str(explicit))) == explicit
    assert not stt.model_is_named(cfg(model=str(explicit)))
    assert not stt.model_is_named(cfg(model="custom.bin"))


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_a_named_model_is_downloaded_into_place(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "ggml-tiny-test.bin").write_bytes(b"ggml" * 1000)
    monkeypatch.setattr(stt, "MODEL_URL", f"file://{origin}/ggml-{{name}}.bin")
    dest = stt.download_model(cfg(model="tiny-test"))
    assert dest == config.models_dir() / "ggml-tiny-test.bin"
    assert dest.read_bytes() == b"ggml" * 1000
    assert not dest.with_name(dest.name + ".part").exists()


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_a_failed_download_leaves_no_model_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(stt, "MODEL_URL", f"file://{tmp_path}/missing-{{name}}.bin")
    with pytest.raises(stt.SttError, match="downloading"):
        stt.download_model(cfg(model="nope"))
    assert not stt.model_path(cfg(model="nope")).exists()


def test_starting_without_a_model_says_which_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(stt, "find_server_binary", lambda: "/usr/bin/true")
    server = stt.WhisperServer(cfg(model=str(tmp_path / "absent.bin")), logging.getLogger("t"))
    with pytest.raises(stt.SttError, match="speech model not found: .*absent.bin"):
        server.start()
    assert not server.alive()


def test_starting_without_the_server_binary_says_how_to_get_it(monkeypatch):
    monkeypatch.setattr(stt, "find_server_binary", lambda: None)
    server = stt.WhisperServer(cfg(), logging.getLogger("t"))
    with pytest.raises(stt.SttError, match="brew install whisper-cpp"):
        server.start()


def test_transcribing_with_no_server_fails_cleanly():
    server = stt.WhisperServer(cfg(), logging.getLogger("t"))
    with pytest.raises(stt.SttError, match="not running"):
        server.transcribe([0.0] * 16000)
    server.stop()  # stopping what never started is harmless
    server.kill()


def test_the_multipart_body_carries_fields_and_the_audio():
    body, content_type = stt._multipart(
        {"language": "auto", "temperature": "0.0"}, "u.wav", b"RIFF"
    )
    boundary = content_type.split("boundary=")[1]
    text = body.decode("latin-1")
    assert content_type.startswith("multipart/form-data")
    assert text.count(f"--{boundary}") == 4  # two fields, the file, the closing marker
    assert 'name="language"\r\n\r\nauto\r\n' in text
    assert 'name="file"; filename="u.wav"' in text and "RIFF" in text
    assert text.endswith(f"--{boundary}--\r\n")


def test_audio_is_sent_as_16_khz_mono_16_bit_wav():
    np = pytest.importorskip("numpy")
    samples = np.array([0.0, 0.5, -0.5, 2.0, -2.0], dtype=np.float32)  # the last two clip
    with wave.open(io.BytesIO(stt.wav_bytes(samples)), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    assert pcm.tolist() == [0, 16383, -16383, 32767, -32767]

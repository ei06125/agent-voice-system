import subprocess
import sys
from pathlib import Path

import pytest

from donna.speech_to_text import listen_daemon, vad_listen


@pytest.mark.parametrize("module", [listen_daemon, vad_listen])
def test_whisper_cpp_uses_cpu_backend_by_default(monkeypatch, module):
    captured = []

    def fake_run(command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="local transcript")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "WHISPER_NO_GPU", True)

    assert module.transcribe("clip.wav") == "local transcript"
    assert captured[0][0][0] == module.WHISPER_BIN
    assert captured[0][0][1] == "-ng"
    assert captured[0][0][2:] == [
        "-m",
        module.WHISPER_MODEL,
        "-f",
        "clip.wav",
        "-nt",
        "-np",
        "-sns",
    ]

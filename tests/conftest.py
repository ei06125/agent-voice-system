"""Shared fixtures. Every test gets a private DONNA_HOME, so nothing here can
touch the real transcripts, the real control file or a running listener."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def donna_home(tmp_path, monkeypatch):
    home = tmp_path / "donna"
    monkeypatch.setenv("DONNA_HOME", str(home))
    for name in list(__import__("os").environ):
        if name.startswith("EARS_"):
            monkeypatch.delenv(name)
    from donna_ears import config

    config.ensure_dirs()
    return home


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()

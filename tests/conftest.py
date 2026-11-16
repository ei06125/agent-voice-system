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


@pytest.fixture(scope="session")
def speaker_assets():
    """Real speaker models and public clips; skips unless they were fetched."""
    import speaker_assets as assets

    absent = assets.missing()
    if absent:
        pytest.skip(
            f"speaker test assets not fetched ({len(absent)} missing): "
            "run python3 tests/speaker_assets.py"
        )
    return assets


@pytest.fixture
def cli_world(tmp_path, monkeypatch, capsys):
    """`ears enroll` / `ears calibrate` with a stand-in model (enroll_helpers)."""
    import enroll_helpers

    return enroll_helpers.make_world(tmp_path, monkeypatch, capsys)

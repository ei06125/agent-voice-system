"""features/shadow.feature: the speaker check runs, delivery is unchanged."""

from __future__ import annotations

from pytest_bdd import scenarios

from conftest_owner import *  # noqa: F403 - the shared steps and the `world` fixture

scenarios("features/shadow.feature")

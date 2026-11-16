"""features/owner_only.feature: only the owner's voice reaches the model."""

from __future__ import annotations

from pytest_bdd import scenarios

from conftest_owner import *  # noqa: F403 - the shared steps and the `world` fixture

scenarios("features/owner_only.feature")

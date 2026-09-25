"""Shared fixtures of the SYNTH-FLEET tests: one generated world (seed 7, source files written)
per test session, and its assembled lanes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.core.records import Lane
from tokenbill.synth.fleet import FleetWorld, generate


@pytest.fixture(scope="session")
def fleet_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("fleet")


@pytest.fixture(scope="session")
def world(fleet_dir: Path) -> FleetWorld:
    return generate(seed=7, out_dir=fleet_dir)


@pytest.fixture(scope="session")
def lanes(world: FleetWorld) -> list[Lane]:
    return world.lanes()

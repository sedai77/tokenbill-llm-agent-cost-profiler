"""Fixtures of the CP-SYNTH-W tests: the fixture world and its files written once per session."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.synth import copilot_writers as w

from .helpers import ALL_CONVENTIONS
from .world import World, build_world


@pytest.fixture(scope="session")
def world() -> World:
    return build_world()


@pytest.fixture(scope="session")
def ident(world: World) -> w.Identity:
    return w.build_identity(world)


@pytest.fixture(scope="session")
def full(world: World, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Path]]:
    """Every source, all three conventions, one overlapping VS Code conversation."""
    root = tmp_path_factory.mktemp("full")
    return root, w.write_world(world, root, conventions=ALL_CONVENTIONS, overlap_sessions=1)

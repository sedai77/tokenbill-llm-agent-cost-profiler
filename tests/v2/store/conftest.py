"""Fixtures of the STORE tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from .helpers import Stores


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[Stores]:
    """Opens fresh ledgers under ``tmp_path`` and closes them at teardown."""
    s = Stores(tmp_path)
    yield s
    s.close()

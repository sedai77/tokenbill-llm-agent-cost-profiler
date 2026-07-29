"""Keep the checked-in examples from rotting: they must at least compile."""

from __future__ import annotations

import py_compile
from pathlib import Path

import pytest

EXAMPLES = sorted((Path(__file__).parent.parent / "examples").glob("*.py"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_compiles(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)


def test_examples_exist() -> None:
    assert EXAMPLES, "examples/ directory should ship at least one example"

"""Fixtures of the F-EXT tests: extensions are registered by ``monkeypatch`` (addendum §3.8), so
no test depends on which real extension modules happen to be merged."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from tokenbill.core import registry
from tokenbill.core.registry import ExtensionSpec

from .fake_ext import hooks


@pytest.fixture(autouse=True)
def _clear_calls() -> Iterator[None]:
    hooks.CALLS.clear()
    yield
    hooks.CALLS.clear()


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """``install(*specs)`` replaces ``core.registry.EXTENSIONS`` for the test."""

    def _install(*specs: ExtensionSpec) -> None:
        monkeypatch.setattr(registry, "EXTENSIONS", {spec.name: spec for spec in specs})

    return _install

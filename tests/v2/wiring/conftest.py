"""Fixtures of the WIRING tests: fake adapters registered in ``core.registry`` for one test."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tokenbill.core import registry

from .support import (
    AltAdapter,
    DeferredAdapter,
    ExportAdapter,
    FakeUsageAdapter,
    NotAnAdapterResult,
    PathRecordStore,
    PathStore,
)


@pytest.fixture
def fake_adapters(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeUsageAdapter]]:
    """``wiring-fake`` first in sniff order, ``wiring-fake-alt`` last, plus ``wiring-fake-b``,
    ``wiring-broken``, ``wiring-missing`` (unimportable) and a stand-in ``copilot-export``."""
    table = {"wiring-fake": FakeUsageAdapter, **registry.BUILTIN_ADAPTERS,
             "copilot-export": ExportAdapter, "wiring-fake-b": DeferredAdapter,
             "wiring-broken": NotAnAdapterResult,
             "wiring-missing": "v2.wiring.no_such_module:Missing",
             "wiring-fake-alt": AltAdapter}
    monkeypatch.setattr(registry, "BUILTIN_ADAPTERS", table)
    FakeUsageAdapter.reads.clear()
    PathStore.opened.clear()
    PathRecordStore.opened.clear()
    PathRecordStore.last = None
    yield FakeUsageAdapter
    FakeUsageAdapter.reads.clear()

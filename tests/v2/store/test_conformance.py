"""``SqliteStore`` passes the foundation conformance suites (SPEC §3.18, K-5, amendment A-2)."""

from __future__ import annotations

from typing import Any

from tokenbill.core.protocols import LedgerStats, LedgerStore
from tokenbill.core.testing import (
    SOURCES_MASK_BITS,
    assert_store_conforms,
    assert_store_copilot_conforms,
)
from tokenbill.store.db import SqliteStore
from tokenbill.store.merge import SOURCES_MASK_BITS as STORE_BITS

from .helpers import Stores


def _factory(stores: Stores) -> Any:
    def make(**kw: Any) -> SqliteStore:
        return stores.open(**kw)
    return make


def test_assert_store_conforms(stores: Stores) -> None:
    got = assert_store_conforms(_factory(stores), permutations=120)
    assert got == {"requests": 13, "orders": 120, "lanes": 6}


def test_assert_store_copilot_conforms(stores: Stores) -> None:
    got = assert_store_copilot_conforms(_factory(stores))
    assert got["cost_line_users"] == 3


def test_protocol_surfaces(stores: Stores) -> None:
    store = stores.open()
    assert isinstance(store, LedgerStore)
    assert isinstance(store, LedgerStats)


def test_sources_mask_bits_match_the_reference() -> None:
    assert dict(STORE_BITS) == dict(SOURCES_MASK_BITS)
    assert STORE_BITS["copilot-export"] == 4096

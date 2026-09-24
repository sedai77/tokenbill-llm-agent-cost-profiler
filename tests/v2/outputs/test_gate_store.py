"""Gate (merge gate 1, PLAN §1.5): FOCUS totals equal ``SqliteStore.cost_rows`` sums on a small
ledger; showback built from a real ``SqliteStore.aggregate`` + ``core.kanon.publish``."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.outputs.focus import write_focus

from .ledger import GROUP_BY, ORG_KEY, SINCE_MS, UNTIL_MS, ingest_result
from .test_store_backed import focus_totals_match, showback_from_aggregate

db = pytest.importorskip("tokenbill.store.db")

pytestmark = pytest.mark.gate


def sqlite_store(path: Path):
    s = db.SqliteStore(path / "ledger.db", org_key=ORG_KEY)
    s.ingest(ingest_result(), pricer=FakePricer())
    return s


def test_focus_totals_equal_sqlite_cost_rows(tmp_path: Path) -> None:
    focus_totals_match(sqlite_store(tmp_path))


def test_focus_export_is_identical_on_sqlite_and_memory(tmp_path: Path) -> None:
    mem = MemoryStore(org_key=ORG_KEY, pricer=FakePricer())
    mem.ingest(ingest_result())
    outs = []
    for s in (sqlite_store(tmp_path), mem):
        buf = io.StringIO()
        write_focus(s.cost_rows(since_ms=SINCE_MS, until_ms=UNTIL_MS, group_by=GROUP_BY), buf,
                    reconciled_channels=frozenset({"anthropic_api", "bedrock"}),
                    allow_unreconciled=False, rate_card_sha="ab" * 32)
        outs.append(buf.getvalue())
    assert outs[0] == outs[1]


def test_showback_from_sqlite_aggregate_and_publish(tmp_path: Path) -> None:
    showback_from_aggregate(sqlite_store(tmp_path), tmp_path / "showback")

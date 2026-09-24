"""Gate F' seams of the host with the sibling wave-1.5b core pieces (F-KIT-C): the real
``core.kanon.scope_counter`` behind ``count_users_fn``, the real ``MemoryStore`` as a
``LedgerStats`` feeding ``rounding_remainders``, and the real ``MemoryRecordStore`` behind
``persist`` / ``capabilities_present``. Skipped until those pieces are merged."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from tokenbill.core import extensions as ext
from tokenbill.core import testing as core_testing
from tokenbill.core.builders import FlatRates, make_license
from tokenbill.core.labels import Basis, estimated
from tokenbill.core.protocols import LedgerStats
from tokenbill.core.testing import MemoryStore
from tokenbill.core.types import DataQualityNote, Finding, Scope

from .fake_ext import hooks
from .support import COPILOT, WINDOW, fake_spec, ingest_result, source

Install = Callable[..., None]


@pytest.mark.gate
def test_count_users_fn_with_the_real_scope_counter() -> None:
    kanon = pytest.importorskip("tokenbill.core.kanon")
    catalog = pytest.importorskip("tokenbill.core.catalog")
    if not hasattr(kanon, "scope_counter") or not hasattr(catalog, "COUNT_SOURCE"):
        pytest.skip("F-KIT-C (scope_counter, COUNT_SOURCE) not merged")
    notes: list[DataQualityNote] = []
    count = ext.count_users_fn(MemoryStore(), [], since_ms=0, until_ms=10**13, notes=notes)
    assert notes == []
    finding = Finding(
        finding_id="fd_x", detector_id="cache.miss-by-cause", kind="ttl-expiry",
        detector_version="1", category="breaker", lever_class="cache_transform", audience="org",
        title="t", summary="s", scope=Scope(dims=(("team", "t"),)), n_events=1, n_lanes=1,
        n_users=1, first_seen_ms=0, cost_observed=estimated(1, Basis.LIST, note="x"),
        recoverable=None, references=("r",))
    assert count(finding, finding.scope) == 0  # an empty ledger has nobody in any scope


@pytest.mark.gate
def test_run_reconcilers_on_the_real_memory_store(install: Install) -> None:
    store = MemoryStore()
    if not isinstance(store, LedgerStats):
        pytest.skip("MemoryStore.source_stats (F-KIT-C) not merged")
    store.ingest(ingest_result(src=source("src_report", "github-ai-usage", None),
                               stats={"rounding_remainder_e18": 12, "rows": 1}))
    store.ingest(ingest_result(src=source("src_cc", "claude-code", None), stats={"lines": 3}))
    install(fake_spec())
    ext.run_reconcilers(store, [], FlatRates(), since_ms=0, until_ms=1, tolerance_pct="0.5",
                        unexplained_pct="1.0", closed_only=False, today="2026-09-24")
    (_args, kw), = hooks.calls("reconciler")
    assert kw["rounding_remainders"] == {"github-ai-usage": Decimal("1.2E-17")}


@pytest.mark.gate
def test_activity_report_only_handoff_with_the_real_record_store(install: Install) -> None:
    """The brief's acceptance case on F-KIT-C's ``MemoryRecordStore``: a store holding only
    record-store licenses (activity report, plan and team assignment unknown) → ``ext:copilot``."""
    record_store_cls = getattr(core_testing, "MemoryRecordStore", None)
    if record_store_cls is None:
        pytest.skip("MemoryRecordStore (F-KIT-C) not merged")
    install(COPILOT)
    records = record_store_cls(None, org_key_id="k_org")
    lic = make_license(snapshot_date="2026-09-15", plan="unknown", assigned_via_team=None,
                       source_kind="github.copilot_activity_report")
    counts = ext.persist([records], ingest_result(licenses=(lic,)), notes=[])
    assert counts["licenses"] == 1 and counts["activity"] == 0 and counts["config"] == 0
    notes: list[DataQualityNote] = []
    assert ext.capabilities_present(MemoryStore(), [records], since_ms=WINDOW["since_ms"],
                                    until_ms=WINDOW["until_ms"], notes=notes) == frozenset(
        {"ext:copilot"})
    assert notes == []

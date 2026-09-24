"""``count_users_fn`` delegates to ``core.kanon.scope_counter`` (F-KIT-C) with ``source_of`` from
``core.catalog.COUNT_SOURCE``; both are stubbed here, so the test is independent of F-KIT-C."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tokenbill.core import catalog, kanon
from tokenbill.core import extensions as ext
from tokenbill.core.labels import Basis, estimated
from tokenbill.core.types import DataQualityNote, Finding, Scope

from .fake_ext import hooks
from .support import PlainLedger


def _finding(detector_id: str, kind: str) -> Finding:
    return Finding(
        finding_id=f"fd_{detector_id}_{kind}", detector_id=detector_id, kind=kind,
        detector_version="1", category="aggregate", lever_class="none", audience="org",
        title="t", summary="s", scope=Scope(dims=(("product", "copilot"), ("team", "t"))),
        n_events=1, n_lanes=0, n_users=3, first_seen_ms=0,
        cost_observed=estimated(1, Basis.LIST, note="x"), recoverable=None, references=("r",))


class _StubCounter:
    """Stands in for ``core.kanon.scope_counter``: records its arguments and returns a counter
    that answers 7 for every (finding, scope) and records the count source it was asked for."""

    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()
        self.kw: dict[str, Any] = {}
        self.sources: list[str] = []

    def __call__(self, ledger: Any, record_stores: Any, **kw: Any) -> Callable[[Finding, Scope],
                                                                                int]:
        self.args, self.kw = (ledger, record_stores), kw

        def count(finding: Finding, scope: Scope) -> int:
            self.sources.append(kw["source_of"](finding))
            return 7

        return count


def test_delegates_to_the_scope_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubCounter()
    monkeypatch.setattr(kanon, "scope_counter", stub, raising=False)
    monkeypatch.setattr(catalog, "COUNT_SOURCE", {("copilot.seats-budgets", "idle-seat"):
                                                  "licenses",
                                                  ("copilot.org-scan", "pool-regime"): "entity"},
                        raising=False)
    ledger, stores = PlainLedger(), [hooks.FakeRecordStore()]
    notes: list[DataQualityNote] = []
    count = ext.count_users_fn(ledger, stores, since_ms=10, until_ms=20, notes=notes)
    assert notes == []
    assert stub.args[0] is ledger and stub.args[1] is stores
    assert stub.kw["since_ms"] == 10 and stub.kw["until_ms"] == 20
    assert set(stub.kw) == {"since_ms", "until_ms", "source_of"}
    scope = Scope(dims=(("product", "copilot"),))
    assert count(_finding("copilot.seats-budgets", "idle-seat"), scope) == 7
    assert count(_finding("copilot.org-scan", "pool-regime"), scope) == 7
    assert count(_finding("cache.miss-by-cause", "ttl-expiry"), scope) == 7
    assert stub.sources == ["licenses", "entity", ext.DEFAULT_COUNT_SOURCE]
    assert ext.DEFAULT_COUNT_SOURCE == "requests"


def test_missing_count_source_table_defaults_to_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubCounter()
    monkeypatch.setattr(kanon, "scope_counter", stub, raising=False)
    monkeypatch.delattr(catalog, "COUNT_SOURCE", raising=False)
    notes: list[DataQualityNote] = []
    count = ext.count_users_fn(PlainLedger(), [], since_ms=0, until_ms=1, notes=notes)
    count(_finding("copilot.seats-budgets", "idle-seat"), Scope(dims=()))
    assert stub.sources == ["requests"]
    assert [n.detail for n in notes] == ["catalog:COUNT_SOURCE"]
    assert notes[0].code == "dq.extension_unavailable"


def test_missing_scope_counter_is_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(kanon, "scope_counter", raising=False)
    monkeypatch.setattr(catalog, "COUNT_SOURCE", {}, raising=False)
    notes: list[DataQualityNote] = []
    count = ext.count_users_fn(PlainLedger(), [], since_ms=0, until_ms=1, notes=notes)
    scope = Scope(dims=(("team", "t"),))
    assert count(_finding("x", "y"), scope) == 0   # two-argument form
    assert count(scope) == 0                        # one-argument (SPEC) form
    assert [n.detail for n in notes] == ["kanon:scope_counter"]
    # without a notes list the fallback still works (and is logged)
    assert ext.count_users_fn(PlainLedger(), [], since_ms=0, until_ms=1)(scope) == 0

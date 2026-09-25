"""``capabilities_present``: ``ext:<name>`` from ledger cost lines, aggregates and requests on an
extension's channels, or from its record stores' licenses / activity / config in the window."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tokenbill.core import extensions as ext
from tokenbill.core.builders import (
    make_activity,
    make_aggregate,
    make_config,
    make_cost_line,
    make_license,
)
from tokenbill.core.types import DataQualityNote

from .fake_ext import hooks
from .support import (
    COPILOT,
    SEP_10_MS,
    WINDOW,
    bare_spec,
    fake_request,
    fake_spec,
    ingest_result,
    memory_store,
)

Install = Callable[..., None]


def _caps(store: Any, stores: list[Any], **window: int) -> frozenset[str]:
    w = {**WINDOW, **window}
    notes: list[DataQualityNote] = []
    out = ext.capabilities_present(store, stores, since_ms=w["since_ms"],
                                   until_ms=w["until_ms"], notes=notes)
    assert notes == []
    return out


def test_nothing_present(install: Install) -> None:
    install(fake_spec(), COPILOT)
    assert _caps(memory_store(), [hooks.FakeRecordStore(), hooks.FakeRecordStore(
        name="copilot")]) == frozenset()


def test_no_extensions(install: Install) -> None:
    install()
    store = memory_store(ingest_result(cost_lines=(make_cost_line(1, channel="fake_a"),)))
    assert _caps(store, []) == frozenset()


def test_cost_line_on_an_extension_channel(install: Install) -> None:
    install(fake_spec(), COPILOT)
    store = memory_store(ingest_result(cost_lines=(
        make_cost_line(5, channel="fake_b", date_utc="2026-09-10"),
        make_cost_line(5, channel="anthropic_api", date_utc="2026-09-10"))))
    assert _caps(store, []) == frozenset({"ext:fake"})
    # outside the window: nothing
    assert _caps(store, [], since_ms=WINDOW["until_ms"], until_ms=WINDOW["until_ms"] + 1) == (
        frozenset())


def test_aggregate_with_an_extension_channel_dim(install: Install) -> None:
    install(fake_spec(), COPILOT)
    agg = make_aggregate({"uncached_input": 5}, source_kind="github.ai_usage_report",
                         bucket_start_ms=SEP_10_MS, bucket_end_ms=SEP_10_MS + 86_400_000,
                         dims={"channel": "github_copilot", "model": "claude-opus-5-5"})
    other = make_aggregate({"uncached_input": 5}, bucket_start_ms=SEP_10_MS,
                           bucket_end_ms=SEP_10_MS + 86_400_000, dims={"model": "x"})
    store = memory_store(ingest_result(aggregates=(agg, other)))
    assert _caps(store, []) == frozenset({"ext:copilot"})


def test_request_on_an_extension_channel(install: Install) -> None:
    install(fake_spec(), COPILOT)
    store = memory_store(ingest_result(requests=(fake_request("fake_a"),)))
    assert _caps(store, []) == frozenset({"ext:fake"})
    store = memory_store(ingest_result(requests=(fake_request("anthropic_api"),)))
    assert _caps(store, []) == frozenset()


def test_activity_report_only_handoff_sees_copilot(install: Install) -> None:
    """Brief acceptance: a store with only record-store licenses (an activity-report-only
    handoff: plan unknown, assignment unknown) → ``ext:copilot``."""
    install(COPILOT)
    records = hooks.FakeRecordStore(name="copilot")
    records.put(ingest_result(licenses=(make_license(
        snapshot_date="2026-09-15", plan="unknown", assigned_via_team=None,
        source_kind="github.copilot_activity_report"),)), principal_key_id="k_export")
    assert _caps(memory_store(), [records]) == frozenset({"ext:copilot"})


@pytest.mark.parametrize("kind", ["licenses", "activity", "config"])
def test_each_record_kind_counts(install: Install, kind: str) -> None:
    install(fake_spec(), COPILOT)
    records = hooks.FakeRecordStore(name="copilot")
    record = {"licenses": {"licenses": (make_license(snapshot_date="2026-09-10"),)},
              "activity": {"activity": (make_activity(date_utc="2026-09-10"),)},
              "config": {"config": (make_config("seat_counts", {"team": "t", "n": 3,
                                                               "n_people": 3},
                                                 snapshot_ms=SEP_10_MS),)}}[kind]
    records.put(ingest_result(**record), principal_key_id=None)
    assert _caps(memory_store(), [records]) == frozenset({"ext:copilot"})
    assert _caps(memory_store(), [records], since_ms=WINDOW["until_ms"],
                 until_ms=WINDOW["until_ms"] + 1) == frozenset()


def test_record_store_attribution_by_name(install: Install) -> None:
    install(fake_spec(), COPILOT, bare_spec("nostore"))
    lic = ingest_result(licenses=(make_license(snapshot_date="2026-09-10"),))
    named = hooks.FakeRecordStore(name="fake")
    named.put(lic, principal_key_id=None)
    assert _caps(memory_store(), [named]) == frozenset({"ext:fake"})
    # a store named after no extension belongs to every extension with a record_store hook
    anonymous = hooks.FakeRecordStore(name="memory")
    anonymous.put(lic, principal_key_id=None)
    assert _caps(memory_store(), [anonymous]) == frozenset({"ext:fake", "ext:copilot"})
    # a store named after an extension belongs to that extension only
    other = hooks.FakeRecordStore(name="nostore")
    other.put(lic, principal_key_id=None)
    assert _caps(memory_store(), [other]) == frozenset({"ext:nostore"})


class _CountingStore:
    """Wraps a ledger, recording every protocol read ``capabilities_present`` makes."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.reads: list[str] = []
        self.aggregate_calls: list[dict[str, Any]] = []

    def cost_lines(self, source_kind: str | None = None, **window: int) -> list[Any]:
        self.reads.append("cost_lines")
        return self.inner.cost_lines(source_kind, **window)

    def aggregates(self, source_kind: str | None = None, **window: int) -> list[Any]:
        self.reads.append("aggregates")
        return self.inner.aggregates(source_kind, **window)

    def aggregate(self, **kw: Any) -> Any:
        self.reads.append("aggregate")
        self.aggregate_calls.append(kw)
        return self.inner.aggregate(**kw)

    def iter_requests(self, **kw: Any) -> Iterator[Any]:  # pragma: no cover - must not be used
        self.reads.append("iter_requests")
        return self.inner.iter_requests(**kw)


def test_each_ledger_source_is_read_at_most_once(install: Install) -> None:
    install(fake_spec(), COPILOT)
    inner = memory_store(ingest_result(requests=(
        fake_request("github_copilot", lane="ln_1"), fake_request("github_copilot", lane="ln_2"))))
    store = _CountingStore(inner)
    assert _caps(store, []) == frozenset({"ext:copilot"})
    # two extensions, three ledger sources: each read once, requests through one GROUP BY channel
    # (SPEC §7.2 whitelists `channel` for SqliteStore.aggregate; never an iter_requests filter)
    assert store.reads == ["cost_lines", "aggregates", "aggregate"]
    assert store.aggregate_calls == [{**WINDOW, "group_by": ("channel",)}]


def test_request_channels_are_the_serving_inference_channels(install: Install) -> None:
    install(fake_spec(), COPILOT)
    store = memory_store(ingest_result(requests=(
        fake_request("fake_b", lane="ln_1"), fake_request("anthropic_api", lane="ln_2"))))
    assert _caps(store, []) == frozenset({"ext:fake"})
    # outside the window: nothing
    assert _caps(store, [], since_ms=WINDOW["until_ms"], until_ms=WINDOW["until_ms"] + 1) == (
        frozenset())


def test_short_circuit_when_cost_lines_decide(install: Install) -> None:
    install(fake_spec())
    store = _CountingStore(memory_store(ingest_result(
        cost_lines=(make_cost_line(1, channel="fake_a"),))))
    assert _caps(store, []) == frozenset({"ext:fake"})
    assert store.reads == ["cost_lines"]

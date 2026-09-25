"""Streaming lane access for sharded analysis (SPEC §7.2, §3.6, D30, R-E7): ``iter_lanes`` with
``where`` and ``lane_keys``, ``lane_index``, ``lane_first_reads``, read-only shard workers."""

from __future__ import annotations

import threading

import pytest

from tokenbill.core.builders import make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.records import LaneKind
from tokenbill.core.shards import plan_shards, shard_where
from tokenbill.core.testing import FakePricer
from tokenbill.store import db as db_mod
from tokenbill.store.db import SqliteStore

from .helpers import (
    FOREVER,
    T0,
    Stores,
    canon,
    five_sources,
    keyed,
    memory,
    result,
    shell,
    src,
)


def _both(stores: Stores):
    store = keyed(stores)
    mem = memory()
    for r in five_sources():
        store.ingest(r)
        mem.ingest(r)
    return store, mem


def _lanes(store, **kw) -> list[str]:
    return [canon(lane) for lane in store.iter_lanes(**kw)]


@pytest.mark.parametrize("where", [None, {"team": "payments", "lane_kind": "main"},
                                   {"team": "platform"}, {"team": ""},
                                   {"lane_kind": "api_run"}, {"billing_class": "allowance"},
                                   {"workspace_id": ""}, {"agent_product": "agent_sdk"},
                                   {"workload_class": "ci"}, {"model": "claude-opus-5-5"},
                                   {"team": "payments", "channel": "anthropic_api"}])
def test_iter_lanes_filters_equal_the_memory_reference(stores: Stores, where) -> None:
    store, mem = _both(stores)
    assert _lanes(store, where=where, **FOREVER) == _lanes(mem, where=where, **FOREVER)


def test_iter_lanes_named_filter_and_sampling(stores: Stores) -> None:
    store, mem = _both(stores)
    main = list(store.iter_lanes(where={"team": "payments", "lane_kind": "main"}, **FOREVER))
    assert [lane.lane_key for lane in main] == ["L-allow", "L-main"]
    sample = ["L-sdk", "L-otel", "L-missing"]
    assert _lanes(store, lane_keys=sample, **FOREVER) == _lanes(mem, lane_keys=sample, **FOREVER)
    assert [lane.lane_key for lane in store.iter_lanes(lane_keys=sample, **FOREVER)] == [
        "L-otel", "L-sdk"]
    both = {"where": {"team": "payments"}, "lane_keys": ["L-main", "L-sdk"]}
    assert _lanes(store, **both, **FOREVER) == _lanes(mem, **both, **FOREVER)
    assert list(store.iter_lanes(lane_keys=[], **FOREVER)) == []


@pytest.mark.parametrize("window", [(T0, T0 + 450_000), (T0 + 60_000, T0 + 60_001),
                                    (0, T0), (T0 + 480_000, 2**53)])
def test_windows_equal_the_memory_reference(stores: Stores, window) -> None:
    store, mem = _both(stores)
    lo, hi = window
    kw = {"since_ms": lo, "until_ms": hi}
    assert _lanes(store, **kw) == _lanes(mem, **kw)
    assert list(store.lane_index(**kw)) == list(mem.lane_index(**kw))
    assert list(store.lane_first_reads(**kw)) == list(mem.lane_first_reads(**kw))
    assert [canon(r) for r in store.iter_requests(**kw)] == [canon(r) for r in
                                                             mem.iter_requests(**kw)]
    assert [canon(r) for r in store.iter_usage_records(**kw)] == [
        canon(r) for r in mem.iter_usage_records(**kw)]


def test_lane_index_and_first_reads(stores: Stores) -> None:
    store, mem = _both(stores)
    index = list(store.lane_index(**FOREVER))
    assert index == list(mem.lane_index(**FOREVER))
    kinds = {row.lane_key: row.lane_kind for row in index}
    assert kinds["L-main"] == "main" and kinds["L-sdk"] == "api_run"
    assert kinds["L-otel"] == "unknown"
    first = list(store.lane_first_reads(**FOREVER))
    assert first == list(mem.lane_first_reads(**FOREVER))
    assert ("ws:w1", "claude-opus-5-5", 0) in first


def test_shards_from_the_lane_index_cover_every_lane_once(stores: Stores) -> None:
    store, _ = _both(stores)
    shards = plan_shards(list(store.lane_index(**FOREVER)))
    seen: list[str] = []
    for shard in shards:
        seen += [lane.lane_key for lane in store.iter_lanes(where=shard_where(shard), **FOREVER)]
    assert sorted(seen) == sorted(row.lane_key for row in store.lane_index(**FOREVER))


def test_a_shell_arriving_later_gives_the_lane_its_kind(stores: Stores) -> None:
    req = make_request("L-late", 0, T0, {"uncached_input": 10, "output": 1}, request_id="rq_l",
                       session_key="S-late", attribution={"team": "t"})
    store = keyed(stores)
    store.ingest(result(src("s_req", "trace@2"), [req]))
    (row,) = store.lane_index(**FOREVER)
    assert row.lane_kind == "unknown"
    store.ingest(result(src("s_shell", "trace@2"),
                        sessions=[shell("L-late", "S-late", LaneKind.SUBAGENT, "ws:9")]))
    (row,) = store.lane_index(**FOREVER)
    assert row.lane_kind == "subagent"
    (lane,) = store.iter_lanes(**FOREVER)
    assert (lane.kind, lane.cache_scope_key) == (LaneKind.SUBAGENT, "ws:9")
    assert store.connection.execute("SELECT DISTINCT lane_kind FROM inferences").fetchall() == [
        ("subagent",)]
    # a worse shell never replaces a better one
    store.ingest(result(src("s_shell2", "trace@2"),
                        sessions=[shell("L-late", "S-late", LaneKind.UNKNOWN, "unknown")]))
    (lane,) = store.iter_lanes(**FOREVER)
    assert lane.kind is LaneKind.SUBAGENT


def test_lanes_stream_across_chunks(stores: Stores, monkeypatch: pytest.MonkeyPatch) -> None:
    reqs = [make_request(f"L{n % 7}", n, T0 + n, {"uncached_input": n + 1, "output": 1},
                         request_id=f"rq{n:04d}", attribution={"team": f"t{n % 3}"})
            for n in range(300)]
    store = keyed(stores)
    mem = memory()
    for s in (store, mem):
        s.ingest(result(src("s_many", "trace@2"), reqs))
    monkeypatch.setattr(db_mod, "_IN", 7)
    assert _lanes(store, **FOREVER) == _lanes(mem, **FOREVER)
    assert _lanes(store, where={"team": "t1"}, **FOREVER) == _lanes(mem, where={"team": "t1"},
                                                                     **FOREVER)
    assert [canon(r) for r in store.iter_requests(**FOREVER)] == [
        canon(r) for r in mem.iter_requests(**FOREVER)]


def test_two_read_only_connections_scan_concurrently(stores: Stores) -> None:
    store, _ = _both(stores)
    expected = _lanes(store, **FOREVER)
    workers = [SqliteStore(store.path, read_only=True) for _ in range(2)]
    got: dict[int, list[str]] = {}
    errors: list[BaseException] = []

    def scan(i: int) -> None:
        try:
            got[i] = [canon(lane) for lane in workers[i].iter_lanes(**FOREVER)]
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    iters = [workers[i].iter_lanes(**FOREVER) for i in range(2)]
    interleaved = [[], []]
    for a, b in zip(*iters, strict=True):         # interleaved lane by lane
        interleaved[0].append(canon(a))
        interleaved[1].append(canon(b))
    assert interleaved == [expected, expected]
    threads = [threading.Thread(target=scan, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and got == {0: expected, 1: expected}
    for w in workers:
        with pytest.raises(UsageError):
            w.ingest(five_sources()[0])
        with pytest.raises(UsageError):
            w.reprice(FakePricer())
        with pytest.raises(UsageError):
            w.purge(before_ms=1, actor="x")
        with pytest.raises(UsageError):
            w.set_cursor("s", "u", byte_offset=1, head_sha="x", size=1, mtime_ns=1)
        assert w.meta() == store.meta()
        w.close()


def test_read_only_needs_an_existing_ledger(stores: Stores, tmp_path) -> None:
    with pytest.raises(UsageError):
        SqliteStore(tmp_path / "missing.db", read_only=True)
    with pytest.raises(UsageError):
        SqliteStore(tmp_path / "missing.db", create=False)
    stray = tmp_path / "stray.db"
    stray.write_bytes(b"")
    with pytest.raises(UsageError):
        SqliteStore(stray, read_only=True)


def test_bad_windows_are_refused(stores: Stores) -> None:
    store, _ = _both(stores)
    with pytest.raises(UsageError):
        list(store.iter_requests(since_ms="0"))  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        store.aggregates(since_ms=0, until_ms=1, day=3)
    assert store.cost_lines(since_ms=5, until_ms=5) == []
    assert store.outcomes(since_ms=5, until_ms=5) == []
    with pytest.raises(UsageError):
        list(store.iter_lanes(where={"team": 5}, **FOREVER))  # type: ignore[dict-item]

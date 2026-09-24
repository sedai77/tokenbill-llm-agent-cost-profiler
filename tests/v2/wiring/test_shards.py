"""``map_shards`` (SPEC §15, §3.21, D30): sequential and process-pool runs return identical results
in shard order; workers open the store read-only by path."""

from __future__ import annotations

import functools
import os
import pickle
from pathlib import Path

import pytest

from tokenbill.core.builders import make_request
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.records import Attribution
from tokenbill.core.shards import plan_shards
from tokenbill.core.types import IngestResult, ShardKey, SourceInfo
from tokenbill.pipeline import common
from tokenbill.pipeline.common import map_shards, shard_store

from .support import (
    T0,
    PathStore,
    shard_fail,
    shard_label,
    shard_lanes,
    shard_pid,
)

SHARDS = [ShardKey(team=None, lane_kind=None), ShardKey(team="data", lane_kind=None),
          ShardKey(team="payments", lane_kind="main"), ShardKey(team="payments",
                                                               lane_kind="subagent"),
          ShardKey(team="search", lane_kind=None)]


def _ledger(path: Path) -> Path:
    """A PathStore file with lanes in four teams (one unattributed)."""
    requests = []
    for i, team in enumerate(["payments", "payments", "search", "data", None, "search"]):
        attr = Attribution(team=team, principal=f"r_user{i}")
        requests += [make_request(f"L{i}", s, T0 + 3_600_000 + s * 1000 + i,
                                  {"uncached_input": 100 + s, "output": 10},
                                  attribution=attr, session_key=f"s{i}") for s in range(i + 1)]
    source = SourceInfo(source_id="s_ledger", adapter="test", name_hmac="h", sha256="0",
                        bytes=0, name_key_id=None, principal_key_id=None)
    result = IngestResult(source=source, requests=requests, sessions=[], events=[],
                          aggregates=[], cost_lines=[], outcomes=[], quarantined=[], notes=[],
                          stats={}, capabilities=frozenset())
    store = PathStore(path)
    store.ingest(result)
    return path


@pytest.fixture
def path_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "STORE_CLASS", "v2.wiring.support:PathStore")


def test_jobs_1_and_2_identical_in_shard_order() -> None:
    one = map_shards(shard_label, SHARDS, jobs=1, db_path=None)
    two = map_shards(shard_label, SHARDS, jobs=2, db_path=None)
    assert one == two == ["None/None", "data/None", "payments/main", "payments/subagent",
                          "search/None"]


def test_pool_really_runs_in_other_processes() -> None:
    pids = map_shards(shard_pid, SHARDS, jobs=2, db_path=None)
    assert os.getpid() not in pids and 1 <= len(set(pids)) <= 2
    assert set(map_shards(shard_pid, SHARDS, jobs=1, db_path=None)) == {os.getpid()}
    # more workers than shards: capped (a single shard runs here)
    assert map_shards(shard_pid, SHARDS[:1], jobs=8, db_path=None) == [os.getpid()]


def test_workers_open_the_store_read_only_by_path(path_store: None, tmp_path: Path) -> None:
    db = _ledger(tmp_path / "ledger.pkl")
    shards = plan_shards(PathStore(db).lane_index(since_ms=0, until_ms=2**53))
    assert [s.team for s in shards] == [None, "data", "payments", "search"]
    one = map_shards(shard_lanes, shards, jobs=1, db_path=db)
    two = map_shards(shard_lanes, shards, jobs=2, db_path=db)
    assert one == two
    assert [row[2] for row in one] == [(("L4", 5),), (("L3", 4),), (("L0", 1), ("L1", 2)),
                                       (("L2", 3), ("L5", 6))]
    assert {row[3:] for row in one} == {("PathStore", True)}
    # a functools.partial of a top-level function is picklable too
    labelled = map_shards(functools.partial(shard_lanes), shards, jobs=2, db_path=db)
    assert labelled == one


def test_sequential_store_is_scoped_and_closed(path_store: None, tmp_path: Path) -> None:
    db = _ledger(tmp_path / "ledger.pkl")
    seen: list[PathStore] = []

    def grab(key: ShardKey) -> int:
        store = shard_store()
        assert isinstance(store, PathStore)
        seen.append(store)
        return len(list(store.iter_lanes()))

    assert map_shards(grab, SHARDS[:2], jobs=1, db_path=db) == [6, 6]
    assert seen[0] is seen[1] and seen[0].closed and seen[0].read_only
    with pytest.raises(UsageError, match="no shard store"):
        shard_store()
    # without db_path the function runs without a store
    with pytest.raises(UsageError, match="no shard store"):
        map_shards(grab, SHARDS[:1], jobs=1, db_path=None)


def test_nested_calls_restore_the_outer_store(path_store: None, tmp_path: Path) -> None:
    db = _ledger(tmp_path / "ledger.pkl")

    def outer(key: ShardKey) -> tuple[bool, bool]:
        before = shard_store()
        inner = map_shards(shard_label, [key], jobs=1, db_path=None)
        return (shard_store() is before, inner == [shard_label(key)])

    assert map_shards(outer, SHARDS[:2], jobs=1, db_path=db) == [(True, True), (True, True)]


def test_worker_initializer(path_store: None, monkeypatch: pytest.MonkeyPatch,
                            tmp_path: Path) -> None:
    """What each pool worker runs first (measured here: coverage does not follow spawned
    workers)."""
    monkeypatch.setattr(common, "_SHARD_STORE", None)
    db = _ledger(tmp_path / "ledger.pkl")
    common._worker_init(str(db), "v2.wiring.support:PathStore")
    store = shard_store()
    assert isinstance(store, PathStore) and store.read_only
    common._worker_init(None, "v2.wiring.support:PathStore")
    with pytest.raises(UsageError, match="no shard store"):
        shard_store()


def test_empty_and_invalid_arguments(tmp_path: Path) -> None:
    assert map_shards(shard_label, [], jobs=3, db_path=None) == []
    for jobs in (0, -1, True, "2", 1.0):
        with pytest.raises(UsageError, match="jobs"):
            map_shards(shard_label, SHARDS, jobs=jobs, db_path=None)  # type: ignore[arg-type]
    with pytest.raises(ContractViolation, match="ShardKey"):
        map_shards(shard_label, [("payments", None)], jobs=1, db_path=None)  # type: ignore[list-item]
    with pytest.raises(UsageError, match="not found"):
        map_shards(shard_label, SHARDS, jobs=1, db_path=tmp_path / "missing.db")


def test_unpicklable_function_with_a_pool() -> None:
    with pytest.raises(ContractViolation, match="picklable"):
        map_shards(lambda key: key.team, SHARDS, jobs=2, db_path=None)
    # sequential runs accept any callable
    assert map_shards(lambda key: key.team, SHARDS[:2], jobs=1, db_path=None) == [None, "data"]


def test_errors_propagate() -> None:
    shards = [ShardKey(team="ok", lane_kind=None), ShardKey(team="boom", lane_kind=None)]
    with pytest.raises(UsageError, match="boom shard"):
        map_shards(shard_fail, shards, jobs=1, db_path=None)
    with pytest.raises(UsageError, match="boom shard"):
        map_shards(shard_fail, shards, jobs=2, db_path=None)


def test_shard_keys_and_results_pickle() -> None:
    assert pickle.loads(pickle.dumps(SHARDS)) == SHARDS

"""Gate (merge gate 1, PLAN §1.5): ``open_store`` + ``ingest_paths`` + ``map_shards(jobs=2)`` over a
real STORE ``SqliteStore`` equal ``jobs=1``; ``bill_summary`` and key-id adoption on SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.config import Config
from tokenbill.core.builders import make_principal
from tokenbill.core.ids import key_id
from tokenbill.core.shards import plan_shards
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import IngestOptions
from tokenbill.pipeline.common import (
    Env,
    bill_summary,
    build_env,
    ingest_paths,
    map_shards,
    open_store,
)

from .support import EXPORT_KEY, ORG_KEY, T0, req, shard_lanes, write_fake, write_key

db = pytest.importorskip("tokenbill.store.db")

pytestmark = [pytest.mark.gate, pytest.mark.usefixtures("fake_adapters")]

WINDOW = {"since_ms": T0, "until_ms": T0 + 86_400_000}


def _env(tmp_path: Path) -> Env:
    key = write_key(tmp_path / "keys" / "org.key", ORG_KEY)
    return build_env(Config(), key_file=key, pricer_factory=lambda **kw: FakePricer(),
                     now_ms=T0 + 86_400_000)


def _close(store: object) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _fleet(tmp_path: Path) -> list[dict]:
    records = []
    for i, team in enumerate(["payments", "payments", "search", "data", None, "search", "data"]):
        records += [req(f"L{i}", s, team=team, principal=f"r_user{i}", r=100 * s)
                    for s in range(i + 1)]
    return records


def test_map_shards_jobs_2_equals_jobs_1_on_sqlite(tmp_path: Path) -> None:
    env = _env(tmp_path)
    path = tmp_path / "ledger.db"
    store = open_store(path, env)
    assert isinstance(store, db.SqliteStore)
    sources, _ = ingest_paths(store, [write_fake(tmp_path / "fleet.jsonl", _fleet(tmp_path))],
                              env, IngestOptions())
    assert len(sources) == 1
    shards = plan_shards(store.lane_index(**WINDOW))
    _close(store)
    one = map_shards(shard_lanes, shards, jobs=1, db_path=path)
    two = map_shards(shard_lanes, shards, jobs=2, db_path=path)
    assert one == two
    assert sorted(lane for row in one for lane, _ in row[2]) == [f"L{i}" for i in range(7)]
    assert sum(n for row in one for _, n in row[2]) == 28


def test_bill_summary_on_sqlite(tmp_path: Path) -> None:
    env = _env(tmp_path)
    store = open_store(tmp_path / "ledger.db", env)
    records = [req(f"P{i}", 0, principal=f"r_p{i}") for i in range(5)] + [
        req("S0", 0, principal="r_s0", billing_path="subscription")]
    ingest_paths(store, [write_fake(tmp_path / "f.jsonl", records, source_adapter="claude-code",
                                    naive={"claude-opus-5-5": {"uncached_input": 12000,
                                                               "output": 1200}})],
                 env, IngestOptions())
    summary = bill_summary(store, env, group_by=["team", "team,model"], **WINDOW)
    assert summary.total.exact.nano == 5 * (1000 * 4_000 + 100 * 20_000)
    assert summary.total.allowance is not None and summary.total.allowance.nano == 6_000_000
    assert summary.total.pool is None
    assert [k for k, _ in summary.breakdowns] == ["team", "team,model"]
    assert summary.naive_ratio == "2"                 # 12000/1200 naive vs 6000/600 de-duplicated


def test_adopt_key_ids_reaches_sqlite(tmp_path: Path) -> None:
    env = _env(tmp_path)
    store = open_store(tmp_path / "ledger.db", env, adopt_key_ids=True)
    bundle = write_fake(tmp_path / "export.tbx", [], principal_key_id=key_id(EXPORT_KEY),
                        licenses=[make_principal(1)])
    ingest_paths(store, [bundle], env, IngestOptions(), adapter="copilot-export")
    assert store.meta()["adopted_key_id"] == key_id(EXPORT_KEY)

"""Rollups (SPEC §7.4): ``daily_rollup`` and ``cluster_day``, ``active_users``, the allowance and
pool columns apart from billed money, ``attr_extra_json`` cluster kinds, stale-date refresh."""

from __future__ import annotations

import pytest

from tokenbill.core.builders import make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.records import InferenceKind
from tokenbill.store import rollups
from tokenbill.store.db import SqliteStore

from .helpers import (
    FOREVER,
    Stores,
    five_sources,
    keyed,
    memory,
    result,
    src,
    ts_of,
)

DAYS = ("2026-09-21", "2026-09-22", "2026-09-23")


def _requests() -> list:
    out = []
    for i, day in enumerate(DAYS):
        for dev in range(4):
            extra = {"mdm_group": f"g{dev % 2}", "gateway": "gw-1", "task_id": f"task-{dev}"}
            out.append(make_request(
                f"L-{dev}-{day}", 0, ts_of(day, 10) + dev, {"uncached_input": 1000,
                                                             "cache_read": 5000, "output": 200},
                "claude-sonnet-5", request_id=f"rq-{dev}-{day}",
                attribution={"principal": f"r_dev{dev}", "team": "alpha" if dev < 3 else "beta",
                             "workspace_id": "ws-1", "arm": "control" if i else None,
                             "wave": "0" if i else None,
                             "extra": tuple(sorted(extra.items())), "billing_path": "api_key"},
                billing_path="api_key"))
        # a seat user (allowance) and a request with only a non-billable inference
        out.append(make_request(f"L-sub-{day}", 0, ts_of(day, 11), {"uncached_input": 400,
                                                                     "output": 40},
                                "claude-opus-5-5", request_id=f"rq-sub-{day}",
                                attribution={"principal": "r_seat", "team": "alpha",
                                             "billing_path": "subscription"},
                                billing_path="subscription"))
        out.append(make_request(f"L-nb-{day}", 0, ts_of(day, 12), {"uncached_input": 10},
                                "claude-opus-5-5", request_id=f"rq-nb-{day}", billable=False,
                                kind=InferenceKind.MESSAGE,
                                attribution={"principal": "r_ghost", "team": "alpha"}))
    return out


def _load(store) -> None:
    store.ingest(result(src("s_roll", "trace@2", name_key=None), _requests()))


@pytest.mark.parametrize("kind", ["team", "workspace", "mdm_group", "gateway"])
def test_cluster_days_equal_the_memory_reference(stores: Stores, kind: str) -> None:
    store = keyed(stores)
    mem = memory()
    _load(store)
    _load(mem)
    got = store.cluster_days(cluster_kind=kind, since=DAYS[0], until="2026-09-24")
    assert got == mem.cluster_days(cluster_kind=kind, since=DAYS[0], until="2026-09-24")
    assert got


def test_active_users_count_requests_with_a_billable_inference(stores: Stores) -> None:
    store = keyed(stores)
    _load(store)
    days = store.cluster_days(cluster_kind="team", since=DAYS[0], until="2026-09-24")
    alpha = [d for d in days if d.cluster_id == "alpha" and d.date_utc == DAYS[1]]
    # devs 0-2 in the control arm + the seat user (no arm): the non-billable ghost is not active
    assert sorted((d.arm or "", d.active_users, d.requests) for d in alpha) == [
        ("", 1, 1), ("control", 3, 3)]
    assert sum(d.allowance_nano for d in alpha) > 0
    assert all(d.exact_nano == 0 for d in alpha if d.arm is None)


def test_extra_keys_round_trip_and_name_clusters(stores: Stores) -> None:
    store = keyed(stores)
    _load(store)
    req = next(r for r in store.iter_requests(**FOREVER) if r.request_id == "rq-1-2026-09-22")
    assert dict(req.attribution.extra) == {"gateway": "gw-1", "mdm_group": "g1",
                                           "task_id": "task-1"}
    groups = store.cluster_days(cluster_kind="mdm_group", since=DAYS[0], until="2026-09-24")
    assert {d.cluster_id for d in groups} == {"g0", "g1"}
    assert {d.cluster_id for d in store.cluster_days(cluster_kind="gateway", since=DAYS[0],
                                                     until="2026-09-24")} == {"gw-1"}


def test_daily_rollup_matches_the_ledger(stores: Stores) -> None:
    store = keyed(stores)
    _load(store)
    assert rollups.refresh_rollups(store.connection) == 3
    assert rollups.dirty_dates(store.connection) == set()
    conn = store.connection
    rows = conn.execute("SELECT date_utc, team, model, billing_path, active_users, requests, "
                        "exact_nano, allowance_nano, pool_nano, unpriced_inferences "
                        "FROM daily_rollup WHERE date_utc=? ORDER BY team, model",
                        (DAYS[2],)).fetchall()
    assert [(r[1], r[2], r[3], r[4], r[5]) for r in rows] == [
        ("alpha", "claude-opus-5-5", "subscription", 1, 1),
        ("alpha", "claude-sonnet-5", "api_key", 3, 3),
        ("beta", "claude-sonnet-5", "api_key", 1, 1)]
    agg = store.aggregate(group_by=["team"], where={"date": DAYS[2]}, since_ms=0,
                          until_ms=2**53)
    by_team = {dict(r.dims)["team"]: r.priced for r in agg.rows}
    assert rows[1][6] + rows[2][6] == by_team["alpha"].exact.nano + by_team["beta"].exact.nano
    assert rows[0][7] == by_team["alpha"].allowance.nano and rows[0][6] == 0
    assert all(r[8] == 0 and r[9] == 0 for r in rows)


def test_stale_dates_are_tracked_and_refreshed(stores: Stores) -> None:
    store = keyed(stores)
    for r in five_sources():
        store.ingest(r)
    conn = store.connection
    assert rollups.dirty_dates(conn) == {"2026-09-23"}
    assert conn.execute("SELECT COUNT(*) FROM cluster_day").fetchone()[0] == 0
    store.cluster_days(cluster_kind="team", since="2026-09-01", until="2026-10-01")
    assert rollups.dirty_dates(conn) == set()
    assert conn.execute("SELECT COUNT(*) FROM cluster_day").fetchone()[0] > 0
    assert rollups.refresh_rollups(conn) == 0
    assert rollups.refresh_rollups(conn, ["2026-09-23"]) == 1
    with pytest.raises(UsageError):
        rollups.refresh_rollups(conn, ["23/09/2026"])
    with pytest.raises(UsageError):
        store.cluster_days(cluster_kind="nope", since="2026-09-01", until="2026-10-01")
    with pytest.raises(UsageError):
        store.cluster_days(cluster_kind="team", since="yesterday", until="2026-10-01")


def test_a_read_only_store_computes_stale_dates_on_the_fly(stores: Stores) -> None:
    store = keyed(stores)
    _load(store)
    ro = SqliteStore(store.path, read_only=True)
    try:
        assert rollups.dirty_dates(ro.connection)
        got = ro.cluster_days(cluster_kind="team", since=DAYS[0], until="2026-09-24")
        assert got == store.cluster_days(cluster_kind="team", since=DAYS[0], until="2026-09-24")
    finally:
        ro.close()


def test_date_helpers() -> None:
    assert rollups.date_of(0) == "1970-01-01"
    assert rollups.date_of(rollups.MAX_MS) == "9999-12-31"
    with pytest.raises(UsageError):
        rollups.date_of(rollups.MAX_MS + 1)
    with pytest.raises(UsageError):
        rollups.date_of(-1)
    assert rollups.date_start_ms("1970-01-02") == rollups.DAY_MS
    with pytest.raises(UsageError):
        rollups.date_start_ms(None)  # type: ignore[arg-type]

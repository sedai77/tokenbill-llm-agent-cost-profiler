"""Retention, purge and key rotation (SPEC §7.5, §8.2): identity retention after the rollups
refresh, purge with ``VACUUM`` and an identity-free audit row, ``key_events`` on rotation."""

from __future__ import annotations

import json
import re

import pytest

from tokenbill.core.builders import make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.records import Fidelity, LaneEvent
from tokenbill.store import retention, rollups
from tokenbill.store.db import SqliteStore

from .helpers import (
    DAY,
    DAY_MS,
    FOREVER,
    NAME_KEY,
    ORG_KEY,
    T0,
    Stores,
    api_dump,
    five_sources,
    h,
    keyed,
    memory,
    p,
    ref,
    result,
    src,
    ts_of,
    with_hint,
)

OLD, MID, NEW = "2026-05-01", "2026-08-01", "2026-09-20"
PSEUDONYM = re.compile(r"[prc]_[0-9a-f]{20}")
NOW_MS = ts_of("2026-09-23", 0)


def _day_requests(day: str, devs: range, *, team: str = "alpha") -> list:
    return [make_request(f"L-{dev}-{day}", 0, ts_of(day) + dev, {"uncached_input": 100,
                                                                 "output": 10},
                         "claude-sonnet-5", request_id=f"rq-{dev}-{day}",
                         attribution={"principal": f"r_dev{dev}", "team": team,
                                      "extra": (("mdm_group", "g1"),)})
            for dev in devs]


def _history(store) -> None:
    reqs = _day_requests(OLD, range(3)) + _day_requests(MID, range(4)) + _day_requests(
        NEW, range(2))
    events = [LaneEvent(lane_key=f"L-0-{d}", ts_ms=ts_of(d) + 5, kind="human_prompt")
              for d in (OLD, MID, NEW)]
    store.ingest(result(src("s_hist", "trace@2", name_key=None), reqs, events=events))


def test_identity_retention_after_rollups_refresh(stores: Stores) -> None:
    store = keyed(stores)
    _history(store)
    counts = store.apply_retention(NOW_MS, identity_days=30, request_days=395, event_days=90,
                                   rollup_days=395)
    assert counts["rollup_dates_refreshed"] == 2 and counts["identities_nulled"] == 7
    conn = store.connection
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE principal IS NOT NULL AND "
                        "date_utc < ?", (NEW,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM inferences WHERE principal IS NOT NULL AND "
                        "date_utc < ?", (NEW,)).fetchone()[0] == 0
    assert store.count_users(where={}, **FOREVER) == 2          # only the recent day's people
    # cluster_day survives with its active developer-days
    mid = store.cluster_days(cluster_kind="team", since=MID, until="2026-08-02")
    assert [(d.active_users, d.requests) for d in mid] == [(4, 4)]
    groups = store.cluster_days(cluster_kind="mdm_group", since=OLD, until="2026-05-02")
    assert [d.active_users for d in groups] == [3]
    # a later refresh of a retained date keeps the users it had
    rollups.refresh_rollups(conn, [MID])
    assert [d.active_users for d in store.cluster_days(cluster_kind="team", since=MID,
                                                       until="2026-08-02")] == [4]
    # events older than event_days are gone; the audit row carries no identity
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    action, detail = conn.execute("SELECT action, detail_json FROM audit").fetchone()
    assert action == "retention" and not PSEUDONYM.search(detail)
    assert json.loads(detail)["identity_before"] == "2026-08-24"


def test_request_and_rollup_retention(stores: Stores) -> None:
    store = keyed(stores)
    _history(store)
    rollups.refresh_rollups(store.connection)
    counts = store.apply_retention(NOW_MS, identity_days=90, request_days=100, event_days=90,
                                   rollup_days=60)
    assert counts["requests_deleted"] == 3
    conn = store.connection
    assert conn.execute("SELECT MIN(date_utc) FROM requests").fetchone()[0] == MID
    assert conn.execute("SELECT COUNT(*) FROM inferences WHERE date_utc < ?",
                        (MID,)).fetchone()[0] == 0
    assert conn.execute("SELECT MIN(date_utc) FROM cluster_day").fetchone()[0] == MID
    assert conn.execute("SELECT COUNT(*) FROM lanes WHERE lane_key LIKE ?",
                        (f"%{OLD}",)).fetchone()[0] == 0
    with pytest.raises(UsageError):
        retention.apply_retention(conn, NOW_MS, identity_days=-1)


def _mixed_group() -> list:
    """bob's transcript contribution (priority 40) and alice's OTel contribution (priority 20)
    joined through a provider request id: one request attributed to bob."""
    t = make_request("L-m", 0, T0, {"cache_write_5m": 1000, "output": 50}, "claude-opus-5-5",
                     session_key="S-m", request_id=stable_id("rq", "anthropic", "msg_x"),
                     message_id="msg_x", attribution={"principal": "r_bob", "team": "t"},
                     source=ref("claude-code", "s_t", Fidelity.FULL, 40, 0))
    o = make_request("L-o", 0, T0, {"cache_write_unknown": 1000, "output": 50},
                     "claude-opus-5-5", session_key="S-o", request_id="rq_otel_x",
                     attribution={"principal": "r_alice", "cost_center": "cc-9"},
                     source=ref("otlp", "s_o", Fidelity.NO_TTL_SPLIT, 20, 0))
    return [result(src("s_t", "claude-code"), [with_hint(t, "req_x")]),
            result(src("s_o", "otlp"), [with_hint(o, "req_x")])]


@pytest.mark.parametrize("who", ["alice", "bob", "carol"])
def test_purge_principal_equals_the_memory_reference(stores: Stores, who: str) -> None:
    sources = five_sources() + _mixed_group()
    store = keyed(stores)
    mem = memory()
    for r in sources:
        store.ingest(r)
        mem.ingest(r)
    assert store.purge(principal=p(who), actor="dpo") == mem.purge(principal=p(who), actor="dpo")
    assert api_dump(store) == api_dump(mem)
    assert all(r.attribution.principal != p(who) for r in store.iter_requests(**FOREVER))
    for kind in ("team", "mdm_group"):
        assert store.cluster_days(cluster_kind=kind, since=DAY, until="2026-09-24") == \
            mem.cluster_days(cluster_kind=kind, since=DAY, until="2026-09-24")
    action, detail = store.connection.execute(
        "SELECT action, detail_json FROM audit ORDER BY rowid DESC").fetchone()
    assert action == "purge" and not PSEUDONYM.search(detail) and who not in detail
    assert json.loads(detail)["by"] == "principal"


def test_purging_a_contributor_remerges_the_request(stores: Stores) -> None:
    store = keyed(stores)
    for r in _mixed_group():
        store.ingest(r)
    (merged,) = store.iter_requests(**FOREVER)
    assert merged.attribution.cost_center == "cc-9"
    assert store.purge(principal=p("alice"), actor="dpo") == 0
    (left,) = store.iter_requests(**FOREVER)
    assert left.attribution.principal == p("bob") and left.attribution.cost_center is None
    assert store.connection.execute("SELECT COUNT(*) FROM merge_members").fetchone()[0] == 0


def test_purge_before_equals_the_memory_reference(stores: Stores) -> None:
    sources = five_sources() + _mixed_group()
    store = keyed(stores)
    mem = memory()
    for r in sources:
        store.ingest(r)
        mem.ingest(r)
    cut = T0 + 450_000
    assert store.purge(before_ms=cut, actor="ops") == mem.purge(before_ms=cut, actor="ops")
    assert api_dump(store) == api_dump(mem)
    detail = json.loads(store.connection.execute(
        "SELECT detail_json FROM audit ORDER BY rowid DESC").fetchone()[0])
    assert detail == {"before_ms": cut, "by": "before_ms", "rows": detail["rows"]}
    store.purge(before_ms=T0 + DAY_MS, actor="ops")
    assert list(store.iter_requests(**FOREVER)) == []
    assert store.aggregates(**FOREVER) == [] and store.cost_lines(**FOREVER) == []


def test_purge_vacuums_and_validates(stores: Stores) -> None:
    store = keyed(stores)
    for r in five_sources():
        store.ingest(r)
    conn = store.connection
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    pages_before = conn.execute("PRAGMA page_count").fetchone()[0]
    store.purge(before_ms=T0 + DAY_MS, actor="ops")
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0     # VACUUMed
    assert conn.execute("PRAGMA page_count").fetchone()[0] <= pages_before
    with pytest.raises(UsageError):
        store.purge(actor="ops")
    with pytest.raises(UsageError):
        store.purge(before_ms=-5, actor="ops")
    with pytest.raises(UsageError):
        store.purge(principal=p("x"), actor="")
    with pytest.raises(UsageError):
        retention.purge(store.connection, principal=7, actor="x")  # type: ignore[arg-type]


def test_purge_without_a_regroup_deletes_partial_requests_whole(stores: Stores) -> None:
    store = keyed(stores)
    for r in _mixed_group():
        store.ingest(r)
    assert retention.purge(store.connection, principal=p("alice"), actor="dpo", now_ms=1) == 1
    assert list(store.iter_requests(**FOREVER)) == []


def test_rotate_the_org_key(stores: Stores) -> None:
    store = keyed(stores)
    for r in five_sources():
        store.ingest(r)
    users = store.count_users(where={}, **FOREVER)
    new_key = bytes(range(160, 192))
    n = store.rotate_key(ORG_KEY, new_key, "org")
    assert n > 0
    conn = store.connection
    assert conn.execute("SELECT key_kind, old_key_id, new_key_id FROM key_events").fetchall() == [
        ("org", key_id(ORG_KEY), key_id(new_key))]
    assert store.meta()["org_key_id"] == key_id(new_key)
    principals = {r.attribution.principal for r in store.iter_requests(**FOREVER)}
    assert p("alice") not in principals
    assert pseudonym(new_key, "p", p("alice")) in principals
    assert store.count_users(where={}, **FOREVER) == users          # equal values stay equal
    docs = "".join(d for (d,) in conn.execute("SELECT doc FROM merge_members"))
    assert p("alice") not in docs
    # new collector data is pseudonymized with the new key
    store.ingest(result(src("s_new", "trace@2", name_key=None), [make_request(
        "L-n", 0, T0, {"uncached_input": 1}, request_id="rq_new",
        attribution={"principal": "r_zed"})]))
    assert pseudonym(new_key, "p", "zed") in {r.attribution.principal
                                              for r in store.iter_requests(**FOREVER)}
    assert conn.execute("SELECT action FROM audit ORDER BY rowid DESC").fetchone()[0] == "rotate"
    with pytest.raises(UsageError):
        store.rotate_key(ORG_KEY, new_key, "org")                  # not the current key
    with pytest.raises(UsageError):
        store.rotate_key(new_key, new_key, "org")
    with pytest.raises(UsageError):
        store.rotate_key(new_key, ORG_KEY, "team")


def test_rotate_the_name_key(stores: Stores) -> None:
    store = keyed(stores)
    for r in five_sources()[:3]:
        store.ingest(r)
    new_key = bytes(range(33, 65))
    store.rotate_key(NAME_KEY, new_key, "name")
    rekey = pseudonym  # h_ values become pseudonym(new, "h", old)
    sdk = [r for r in store.iter_requests(**FOREVER) if r.lane_key == "L-sdk"]
    assert sdk[0].attribution.repo == rekey(new_key, "h", h("repo-x"))
    main = [r for r in store.iter_requests(**FOREVER) if r.lane_key == "L-main"]
    assert main[0].attribution.cwd_key == rekey(new_key, "h", h("/repo/a"))
    dims = dict(store.aggregates(**FOREVER)[0].dims)
    assert dims["api_key_id"] == rekey(new_key, "h", h("key-1"))
    assert store.meta()["name_key_id"] == key_id(new_key)


def test_rotation_refuses_a_ledger_with_an_adopted_key(stores: Stores) -> None:
    store = keyed(stores)
    store.connection.execute("UPDATE meta SET value='k_adopted' WHERE key='adopted_key_id'")
    with pytest.raises(UsageError):
        store.rotate_key(ORG_KEY, bytes(range(160, 192)), "org")


def test_retention_through_the_module_api(stores: Stores) -> None:
    store = keyed(stores)
    _history(store)
    path = store.path
    store.close()
    ro = SqliteStore(path, read_only=True)
    with pytest.raises(UsageError):
        ro.apply_retention(NOW_MS)
    ro.close()
    rw = SqliteStore(path, org_key=ORG_KEY)
    counts = retention.apply_retention(rw.connection, NOW_MS, **retention.DEFAULTS)
    assert counts["requests_deleted"] == 0
    rw.close()

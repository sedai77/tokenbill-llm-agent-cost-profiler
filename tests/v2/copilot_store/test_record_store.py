"""CopilotRecordStore (CP-STORE brief "Provides" and acceptance tests; addendum §7.2; rulings
R-E21, R-E45): conformance, DDL, round trips, key ids, counting people, retention, purge, WAL."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from tokenbill.core import extensions
from tokenbill.core import testing as kit
from tokenbill.core.builders import make_activity, make_config, make_license
from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.protocols import ExtRecordStore
from tokenbill.copilot.record_store import (
    COPILOT_TABLES,
    DQ_PRINCIPAL_KEY_MISMATCH,
    RECORD_WHERE_KEYS,
    STORE_NAME,
    CopilotRecordStore,
)

from .support import (
    KEY_A,
    KEY_B,
    ORG_KEY,
    W,
    ledger_meta,
    ms,
    p,
    result,
)

DAY = "2026-09-20"


def open_store(tmp_path: Path, *, org_key: bytes | None = ORG_KEY, adopted: bytes | None = None,
               name: str = "ledger.db", audit: bool = False, **kw) -> CopilotRecordStore:
    path = tmp_path / name
    ledger_meta(path, org_key_id=key_id(org_key) if org_key else "",
                adopted_key_id=key_id(adopted) if adopted else "", audit=audit)
    return CopilotRecordStore(path, **kw)


# ---------------------------------------------------------------------------------------------
# conformance (R-E45: a two-argument factory that opens the ledger first)
# ---------------------------------------------------------------------------------------------


def conformance_factory(path: Path, org_key: bytes) -> CopilotRecordStore:
    ledger_meta(path, org_key_id=key_id(org_key))    # what SqliteStore(path, org_key=…) writes
    return CopilotRecordStore(path)


def test_assert_record_store_conforms() -> None:
    summary = kit.assert_record_store_conforms(conformance_factory)
    assert summary == {"batches": 5, "orders": 12}


def test_one_argument_factory_is_refused_as_contract_change_kit_c_1_says() -> None:
    # the brief's literal lambda: a fresh file has no ledger meta, so every p_ row is refused
    with pytest.raises(AssertionError, match="refused p_ values"):
        kit.assert_record_store_conforms(lambda path: CopilotRecordStore(path), permutations=1)


def test_protocol_surface_and_registry_path(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    assert isinstance(store, ExtRecordStore) and store.name == STORE_NAME == "copilot"
    notes: list = []
    stores = extensions.open_record_stores(tmp_path / "ledger.db", create=False, notes=notes)
    assert notes == [] and [type(s) for s in stores] == [CopilotRecordStore]
    counts = extensions.persist(stores, result([make_license(p(ORG_KEY, "u1"))]))
    assert counts["licenses"] == 1 and len(store.licenses(**W)) == 1


# ---------------------------------------------------------------------------------------------
# DDL and round trips
# ---------------------------------------------------------------------------------------------


def columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[str, int, object]]:
    return {r[1]: (r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")}


def test_ddl_matches_addendum_7_2(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(COPILOT_TABLES) <= tables and "copilot_decisions" not in tables   # C-17
    lic = columns(conn, "copilot_license_snapshots")
    assert list(lic) == ["rec_key", "snapshot_date", "product", "plan", "principal", "team",
                         "cost_center", "org", "seat_created", "pending_cancellation",
                         "last_activity_bucket", "last_activity_surface",
                         "last_authenticated_bucket", "assigned_via_team", "fetched_ms",
                         "source_kind", "principal_key_id"]
    assert lic["assigned_via_team"] == ("INTEGER", 0, None)          # nullable (revision 3)
    assert lic["org"] == ("TEXT", 1, "''")
    act = columns(conn, "copilot_activity_days")
    assert list(act) == ["rec_key", "date_utc", "product", "principal", "team", "cost_center",
                         "reported_cost_nano", "counts_json", "flags", "fetched_ms",
                         "source_kind", "principal_key_id"]
    cfg = columns(conn, "copilot_config_snapshots")
    assert list(cfg) == ["rec_key", "snapshot_ms", "source_kind", "kind", "entity_id",
                         "attrs_json", "fetched_ms"]
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"cls_date_team", "cad_date_team"} <= indexes
    assert conn.execute("SELECT value FROM copilot_meta WHERE key='schema_version'"
                        ).fetchone() == ("1",)
    assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    conn.close()
    store.close()


def test_activity_report_license_round_trips_null_assignment(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    ar = make_license(p(ORG_KEY, "u1"), snapshot_date=DAY, plan="unknown", org=None,
                      assigned_via_team=None, last_activity_surface=None,
                      source_kind="github.copilot_activity_report", fetched_ms=5)
    team = make_license(p(ORG_KEY, "u2"), snapshot_date=DAY, assigned_via_team=True,
                        seat_created="2026-01-01", pending_cancellation="2026-10-01",
                        team="t", cost_center="cc", last_authenticated_bucket="unknown")
    direct = make_license(p(ORG_KEY, "u3"), snapshot_date=DAY, assigned_via_team=False)
    store.put(result([ar, team, direct]), principal_key_id=key_id(ORG_KEY))
    raw = dict(sqlite3.connect(str(tmp_path / "ledger.db")).execute(
        "SELECT principal, assigned_via_team FROM copilot_license_snapshots").fetchall())
    assert raw == {ar.principal: None, team.principal: 1, direct.principal: 0}
    assert sorted(store.licenses(**W), key=lambda x: x.principal) == sorted(
        [ar, team, direct], key=lambda x: x.principal)
    (back,) = [x for x in store.licenses(**W) if x.principal == ar.principal]
    assert back.assigned_via_team is None and back.org is None and back == ar


def test_activity_and_config_round_trip(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    day = make_activity(p(ORG_KEY, "u1"), date_utc=DAY, team="t", cost_center="cc",
                        reported_cost_nano=125_000_000,
                        counts={"interactions": 4, "ide:intellij": 2, "cli_requests": 0},
                        flags=["used_cli", "used_chat"], fetched_ms=3)
    bare = make_activity(p(ORG_KEY, "u2"), date_utc=DAY, counts={}, reported_cost_nano=None)
    cfgs = [make_config("run_flags", {"promo_eligible": False, "billing_mode.enterprise":
                                      "volume", "pool_seats.enterprise.business": 10,
                                      "compliance": None}, snapshot_ms=ms(DAY, 3)),
            make_config("seat_counts", {"team": "t", "plan": "business", "bucket": "*",
                                        "n_people": 7}, entity_id="org:o", snapshot_ms=ms(DAY)),
            make_config("budget", {"scope": "user", "amount_nano": 0, "team": "t"},
                        entity_id="budget:b1", snapshot_ms=ms(DAY, 1), fetched_ms=9)]
    counts = store.put(result(activity=[day, bare], config=cfgs),
                       principal_key_id=key_id(ORG_KEY))
    assert counts == {"licenses": 0, "activity": 2, "config": 3, "skipped": 0,
                      DQ_PRINCIPAL_KEY_MISMATCH: 0}
    assert sorted(store.activity(**W), key=lambda a: a.principal) == sorted(
        [day, bare], key=lambda a: a.principal)
    assert store.config(**W) == sorted(cfgs, key=lambda c: c.snapshot_ms)
    flags_cfg = store.config(**W)[-1]
    assert dict(flags_cfg.attrs)["promo_eligible"] is False
    assert dict(flags_cfg.attrs)["pool_seats.enterprise.business"] == 10


def test_latest_fetch_wins_and_ties_follow_the_fake(tmp_path: Path) -> None:
    old = make_license(p(ORG_KEY, "u1"), snapshot_date=DAY, last_activity_bucket="0-7",
                       fetched_ms=5)
    new = dataclasses.replace(old, last_activity_bucket="8-30", fetched_ms=9)
    tie_a = dataclasses.replace(old, team="a", fetched_ms=9)
    tie_b = dataclasses.replace(old, team="b", fetched_ms=9)
    for order in ([old, new, tie_a, tie_b], [tie_b, tie_a, new, old], [new, tie_b, old, tie_a]):
        store = open_store(tmp_path, name=f"s{id(order)}.db")
        fake = kit.MemoryRecordStore(org_key_id=key_id(ORG_KEY))
        for rec in order:
            store.put(result([rec]), principal_key_id=key_id(ORG_KEY))
            fake.put(result([rec]), principal_key_id=key_id(ORG_KEY))
        assert store.licenses(**W) == fake.licenses(**W)
        (kept,) = store.licenses(**W)
        assert kept.fetched_ms == 9
    # several versions inside one batch
    store = open_store(tmp_path, name="batch.db")
    store.put(result([old, tie_a, new, tie_b]), principal_key_id=key_id(ORG_KEY))
    fake = kit.MemoryRecordStore(org_key_id=key_id(ORG_KEY))
    fake.put(result([old, tie_a, new, tie_b]), principal_key_id=key_id(ORG_KEY))
    assert store.licenses(**W) == fake.licenses(**W)


# ---------------------------------------------------------------------------------------------
# key ids (R-E21)
# ---------------------------------------------------------------------------------------------


def lic(key: bytes, who: str, **kw):
    return make_license(p(key, who), snapshot_date=DAY, **kw)


def test_batch_under_an_unaccepted_key_id_is_skipped(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    cfg = make_config("run_flags", {"promo_eligible": True}, snapshot_ms=ms(DAY))
    counts = store.put(result([lic(KEY_B, "x")], [make_activity(p(KEY_B, "x"), date_utc=DAY)],
                              [cfg], key=KEY_B), principal_key_id=key_id(KEY_B))
    assert counts == {"licenses": 0, "activity": 0, "config": 1, "skipped": 2,
                      DQ_PRINCIPAL_KEY_MISMATCH: 2}
    assert store.licenses(**W) == [] and store.activity(**W) == [] and store.config(**W) == [cfg]
    none = store.put(result([lic(ORG_KEY, "u")]), principal_key_id=None)
    assert none[DQ_PRINCIPAL_KEY_MISMATCH] == 1 and store.licenses(**W) == []
    empty = store.put(result([lic(ORG_KEY, "u")]), principal_key_id="")
    assert empty[DQ_PRINCIPAL_KEY_MISMATCH] == 1
    assert store.put(result(), principal_key_id=None) == {
        "licenses": 0, "activity": 0, "config": 0, "skipped": 0, DQ_PRINCIPAL_KEY_MISMATCH: 0}


def test_no_ledger_meta_or_keyless_unadopted_store_refuses_people(tmp_path: Path) -> None:
    bare = CopilotRecordStore(tmp_path / "bare.db")
    assert bare.accepted_key_ids() == frozenset()
    assert bare.put(result([lic(ORG_KEY, "u")]), principal_key_id=key_id(ORG_KEY))[
        DQ_PRINCIPAL_KEY_MISMATCH] == 1
    keyless = open_store(tmp_path, org_key=None, name="keyless.db")
    assert keyless.accepted_key_ids() == frozenset()
    assert keyless.put(result([lic(KEY_A, "u")], key=KEY_A),
                       principal_key_id=key_id(KEY_A))["skipped"] == 1
    assert keyless.licenses(**W) == []


def test_adopted_key_id_is_accepted_after_adoption(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    ledger_meta(path)                                   # keyless, not adopted yet
    store = CopilotRecordStore(path)
    assert store.put(result([lic(KEY_A, "u1")], key=KEY_A),
                     principal_key_id=key_id(KEY_A))["skipped"] == 1
    # the ledger adopts A (keyless: org_key_id and adopted_key_id both A, R-E21)
    ledger_meta(path, org_key_id=key_id(KEY_A), adopted_key_id=key_id(KEY_A))
    assert store.accepted_key_ids() == {key_id(KEY_A)}
    ok = store.put(result([lic(KEY_A, "u1")], key=KEY_A), principal_key_id=key_id(KEY_A))
    assert (ok["licenses"], ok["skipped"]) == (1, 0)
    other = store.put(result([lic(KEY_B, "u1")], key=KEY_B), principal_key_id=key_id(KEY_B))
    assert other[DQ_PRINCIPAL_KEY_MISMATCH] == 1 and len(store.licenses(**W)) == 1


def test_org_keyed_store_with_adoption_keeps_both_key_spaces_apart(tmp_path: Path) -> None:
    store = open_store(tmp_path, adopted=KEY_A)
    assert store.accepted_key_ids() == {key_id(ORG_KEY), key_id(KEY_A)}
    k_rows = [lic(ORG_KEY, u, team="t") for u in ("u1", "u2", "u3")]
    a_rows = [lic(KEY_A, u, team="t") for u in ("u1", "u2")]
    store.put(result(k_rows), principal_key_id=key_id(ORG_KEY))
    store.put(result(a_rows, [make_activity(p(KEY_A, "u1"), date_utc=DAY)], key=KEY_A),
              principal_key_id=key_id(KEY_A))
    assert store.put(result([lic(KEY_B, "u9")], key=KEY_B),
                     principal_key_id=key_id(KEY_B))["skipped"] == 1
    assert {store.principal_key_id_of(x) for x in k_rows} == {key_id(ORG_KEY)}
    assert {store.principal_key_id_of(x) for x in a_rows} == {key_id(KEY_A)}
    (day,) = store.activity(**W)
    assert store.principal_key_id_of(day) == key_id(KEY_A)
    assert store.principal_key_id_of(lic(ORG_KEY, "nobody")) is None
    assert dict((x.principal, k) for x, k in store.licenses_with_key_ids(**W)) == {
        **{x.principal: key_id(ORG_KEY) for x in k_rows},
        **{x.principal: key_id(KEY_A) for x in a_rows}}
    assert [k for _, k in store.activity_with_key_ids(**W)] == [key_id(KEY_A)]
    # never joined: the same people under two key ids are not added up
    assert store.count_users(where={"team": "t"}, source="licenses", **W) == 3
    with pytest.raises(UsageError):
        store.principal_key_id_of(make_config())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# counting people
# ---------------------------------------------------------------------------------------------


def count(store: CopilotRecordStore, source: str = "licenses", **where: str) -> int:
    return store.count_users(where=where, source=source, since_ms=ms("2026-09-01"),
                             until_ms=ms("2026-10-01"))


def test_count_users_licenses(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    k = ORG_KEY
    store.put(result([
        lic(k, "u1", org="org-a", team="t", plan="business"),
        lic(k, "u1", org="org-b", team="t", plan="business"),     # seated via two orgs
        lic(k, "u2", org="org-a", team="t", last_activity_bucket="none_90d"),
        lic(k, "u3", org="org-a", team=None, plan="enterprise", last_activity_surface="jetbrains"),
        lic(k, "u4", org=None, plan="unknown", assigned_via_team=None,
            source_kind="github.copilot_activity_report", last_activity_surface=None),
        make_license(p(k, "u5"), snapshot_date="2026-08-30"),       # outside the window
    ]), principal_key_id=key_id(k))
    assert count(store) == 4
    assert count(store, team="t") == 2 and count(store, team="") == 2
    assert count(store, org="org-a") == 3 and count(store, org="org-b") == 1
    assert count(store, org="") == 1
    assert count(store, plan="business") == 2 and count(store, plan="unknown") == 1
    assert count(store, bucket="none_90d") == 1 and count(store, bucket="0-7") == 3
    assert count(store, editor_family="jetbrains") == 1 and count(store, surface="") == 1
    assert count(store, product="github_copilot") == 4 and count(store, product="x") == 0
    assert count(store, team="t", plan="enterprise") == 0
    assert count(store, date_from=DAY, date_to=DAY) == 4
    assert count(store, date_from="2026-09-21") == 0 and count(store, date_to="2026-09-19") == 0
    assert store.count_users(where={}, source="licenses", **W) == 5


def test_count_users_activity(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    k = ORG_KEY
    store.put(result(activity=[
        make_activity(p(k, "u1"), date_utc=DAY, team="a"),
        make_activity(p(k, "u1"), date_utc="2026-09-21", team="a"),
        make_activity(p(k, "u2"), date_utc=DAY, team="b", cost_center="cc"),
    ]), principal_key_id=key_id(k))
    assert count(store, "activity") == 2 and count(store, "activity", team="a") == 1
    assert count(store, "activity", cost_center="cc") == 1
    assert count(store, "activity", cost_center="") == 1
    assert count(store, "activity", org="org-a") == 0          # activity days carry no org
    assert count(store, "activity", org="", plan="") == 2      # "" filters are no-ops there
    assert count(store, "activity", date_from="2026-09-21") == 1


def test_count_rows_of_aggregate_only_bundles(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    t0 = ms(DAY)
    rows = [make_config("seat_counts", {"team": "t", "bucket": "*", "n_people": 7},
                        entity_id="org:o1", snapshot_ms=t0),
            make_config("seat_counts", {"team": "t", "plan": "business", "bucket": "0-7",
                                        "n": 7}, entity_id="org:o1", snapshot_ms=t0),
            make_config("seat_counts", {"team": "t", "bucket": "*", "n_people": 5},
                        entity_id="org:o2", snapshot_ms=t0),
            make_config("seat_counts", {"team": "u", "bucket": "*", "n_people": 6},
                        entity_id="org:o1", snapshot_ms=t0),
            make_config("seat_counts", {"team": "bad", "bucket": "*", "n_people": "x"},
                        entity_id="org:o1", snapshot_ms=t0),
            make_config("activity_counts", {"team": "t", "month": "2026-09", "n_people": 9},
                        entity_id="org:o1", snapshot_ms=t0),
            make_config("activity_counts", {"team": "t", "month": "2026-09", "n_people": "12"},
                        entity_id="org:o2", snapshot_ms=t0)]
    store.put(result(config=rows, key=None), principal_key_id=None)
    assert count(store, team="t") == 7                  # max over orgs, never a sum across them
    assert count(store, team="t", org="o2") == 5
    assert count(store) == 13                           # o1: 7 + 6 (+ garbage 0)
    assert count(store, team="bad") == 0
    assert count(store, team="t", bucket="0-7") == 0     # detail rows carry no n_people
    assert count(store, team="t", cost_center="x") == 0
    assert count(store, team="t", product="github_copilot") == 7
    assert count(store, team="u", surface="") == 6 and count(store, date_from="2026-09-21") == 0
    assert count(store, "activity", team="t") == 12
    assert count(store, "activity", org="o1") == 9
    # person rows in the window disable the fallback
    store.put(result([lic(ORG_KEY, "u1", team="t")]), principal_key_id=key_id(ORG_KEY))
    assert count(store, team="t") == 1


def test_count_users_argument_checks(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    for bad in ({"principal": p(ORG_KEY, "u")}, {"session_key": "s"}, {"session": "s"}):
        with pytest.raises(PrivacyError):
            count(store, **bad)
    with pytest.raises(UsageError):
        count(store, workspace_id="w")
    with pytest.raises(UsageError):
        store.count_users(where={"team": 1}, source="licenses", **W)  # type: ignore[dict-item]
    with pytest.raises(UsageError):
        store.count_users(where=["team"], source="licenses", **W)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        store.count_users(where={}, source="cost_lines", **W)
    with pytest.raises(UsageError):
        store.count_users(where={}, source="licenses", since_ms="0", until_ms=1)  # type: ignore
    assert store.count_users(where={}, source="licenses", since_ms=5, until_ms=5) == 0
    assert store.count_users(where={}, source="licenses", since_ms=-10**18,
                             until_ms=10**18) == 0
    assert RECORD_WHERE_KEYS == kit.RECORD_WHERE_KEYS


# ---------------------------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------------------------


def test_windows(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.put(result([lic(ORG_KEY, "u1"), make_license(p(ORG_KEY, "u2"),
                                                       snapshot_date="2026-09-21")],
                     [make_activity(p(ORG_KEY, "u1"), date_utc=DAY)],
                     [make_config(snapshot_ms=ms(DAY, 5))]), principal_key_id=key_id(ORG_KEY))
    # day overlap: a window starting inside the 20th includes it; one ending at its midnight not
    assert len(store.licenses(since_ms=ms(DAY, 23), until_ms=ms("2026-09-21"))) == 1
    assert store.licenses(since_ms=ms("2026-09-01"), until_ms=ms(DAY)) == []
    assert len(store.licenses(since_ms=ms(DAY, 1))) == 2 and len(store.licenses()) == 2
    assert store.licenses(since_ms=10, until_ms=10) == []
    assert len(store.licenses(since_ms=-(10**18), until_ms=10**18)) == 2
    assert store.config(since_ms=ms(DAY, 5), until_ms=ms(DAY, 6)) != []
    assert store.config(since_ms=ms(DAY, 6)) == [] and store.config(until_ms=0) == []
    assert list(store.iter_activity(until_ms=ms("2026-09-21"))) == store.activity(**W)
    for bad in ({"since": 0}, {"since_ms": "0"}, {"until_ms": 1.5}):
        with pytest.raises(UsageError):
            store.licenses(**bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# retention, purge, audit
# ---------------------------------------------------------------------------------------------


def seeded(tmp_path: Path, **kw) -> CopilotRecordStore:
    store = open_store(tmp_path, now_ms=77, **kw)
    k = ORG_KEY
    store.put(result([lic(k, "u1"), lic(k, "u2"), make_license(p(k, "u1"),
                                                               snapshot_date="2026-06-01")],
                     [make_activity(p(k, "u1"), date_utc=DAY),
                      make_activity(p(k, "u3"), date_utc="2026-06-02")],
                     [make_config(snapshot_ms=ms("2026-06-01")),
                      make_config(snapshot_ms=ms(DAY))]), principal_key_id=key_id(k))
    return store


def test_retain_deletes_old_people_rows_only(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    assert store.retain(identity_before_ms=ms("2026-08-01")) == 2
    assert len(store.licenses(**W)) == 2 and len(store.activity(**W)) == 1
    assert len(store.config(**W)) == 2
    assert store.retain(identity_before_ms=ms("2026-08-01")) == 0
    log = store.audit_log()
    assert [(ts, actor, action) for ts, actor, action, _ in log] == [(77, "retention", "retain")]
    assert '"rows":2' in log[0][3] and "p_" not in log[0][3]
    with pytest.raises(UsageError):
        store.retain(identity_before_ms="2026")  # type: ignore[arg-type]


def test_purge_by_principal_and_time(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    who = p(ORG_KEY, "u1")
    assert store.purge(principal=who, before_ms=None, actor="cli") == 3
    assert all(x.principal != who for x in store.licenses(**W) + store.activity(**W))
    assert store.purge(principal=None, before_ms=ms("2026-08-01"), actor="cli") == 2
    assert len(store.config(**W)) == 1 and len(store.activity(**W)) == 0
    assert store.purge(principal=p(ORG_KEY, "u2"), before_ms=ms("2026-08-01"), actor="x") == 1
    actions = [(a, d) for _, _, a, d in store.audit_log()]
    assert [a for a, _ in actions] == ["purge", "purge", "purge"]
    assert all(who not in d and "p_" not in d for _, d in actions)
    assert '"by":"principal"' in actions[0][1] and '"before_ms"' in actions[1][1]


def test_purge_argument_checks(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    with pytest.raises(UsageError):
        store.purge(principal=None, before_ms=None, actor="x")
    for bad in ("octocat", "r_ref", "p_XYZ", p(ORG_KEY, "u1") + "0"):
        with pytest.raises(UsageError):
            store.purge(principal=bad, before_ms=None, actor="x")
    with pytest.raises(UsageError):
        store.purge(principal=None, before_ms=1.0, actor="x")  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        store.purge(principal=None, before_ms=1, actor=None)  # type: ignore[arg-type]
    assert len(store.licenses(**W)) == 3


def test_purged_identity_is_gone_from_the_file(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    who = p(ORG_KEY, "u2")
    store.purge(principal=who, before_ms=None, actor="erasure")
    store.close()
    blob = b"".join(f.read_bytes() for f in tmp_path.iterdir() if f.name.startswith("ledger"))
    assert who.encode() not in blob and p(ORG_KEY, "u1").encode() in blob


def test_audit_rows_go_to_the_store_audit_table_when_present(tmp_path: Path) -> None:
    store = seeded(tmp_path, audit=True)
    store.purge(principal=p(ORG_KEY, "u1"), before_ms=None, actor="cli")
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    rows = conn.execute("SELECT ts_ms, actor, action, detail_json FROM audit").fetchall()
    assert rows == [(77, "cli", "purge", '{"by":"principal","rows":3,"store":"copilot"}')]
    assert conn.execute("SELECT COUNT(*) FROM copilot_audit").fetchone() == (0,)
    conn.execute("INSERT INTO audit VALUES (1, 'ledger', 'purge', '{\"rows\":1}')")
    conn.commit()
    conn.close()
    assert [r[1] for r in store.audit_log()] == ["cli"]       # only this store's rows


def test_wall_clock_audit_timestamp(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.put(result([lic(ORG_KEY, "u1")]), principal_key_id=key_id(ORG_KEY))
    store.purge(principal=p(ORG_KEY, "u1"), before_ms=None, actor="x")
    ((ts, *_),) = store.audit_log()
    assert ts > ms("2026-01-01")


# ---------------------------------------------------------------------------------------------
# files, WAL and coexistence
# ---------------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_created_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "new" / "ledger.db"
    CopilotRecordStore(path).close()
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_open_errors(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        CopilotRecordStore(tmp_path / "missing.db", create=False)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database, just some bytes" * 100)
    with pytest.raises(UsageError):
        CopilotRecordStore(junk)
    with pytest.raises(UsageError):
        CopilotRecordStore(tmp_path / "x.db", now_ms="1")  # type: ignore[arg-type]
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory")
    with pytest.raises(UsageError):
        CopilotRecordStore(blocked / "ledger.db")
    newer = tmp_path / "newer.db"
    CopilotRecordStore(newer).close()
    conn = sqlite3.connect(str(newer))
    conn.execute("UPDATE copilot_meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(UsageError):
        CopilotRecordStore(newer)


def test_two_instances_share_one_file(tmp_path: Path) -> None:
    one = open_store(tmp_path)
    with CopilotRecordStore(tmp_path / "ledger.db", create=False) as two:
        one.put(result([lic(ORG_KEY, "u1")]), principal_key_id=key_id(ORG_KEY))
        two.put(result([lic(ORG_KEY, "u2")]), principal_key_id=key_id(ORG_KEY))
        assert len(one.licenses(**W)) == len(two.licenses(**W)) == 2
        reader = one.iter_licenses(**W)
        first = next(reader)                        # an open read cursor on one connection …
        two.put(result([lic(ORG_KEY, "u3")]), principal_key_id=key_id(ORG_KEY))  # … never blocks
        assert first.principal in {p(ORG_KEY, "u1"), p(ORG_KEY, "u2")}
        reader.close()
        assert len(one.licenses(**W)) == 3


def test_ledger_tables_are_untouched_by_record_writes(tmp_path: Path) -> None:
    store = open_store(tmp_path, audit=True)
    path = tmp_path / "ledger.db"

    def ledger_dump() -> list[str]:
        conn = sqlite3.connect(str(path))
        try:
            return [line for line in conn.iterdump()
                    if "copilot_" not in line and "cls_" not in line and "cad_" not in line
                    and "ccs_" not in line]
        finally:
            conn.close()

    before = ledger_dump()
    for _ in range(2):
        store.put(result([lic(ORG_KEY, "u1")], [make_activity(p(ORG_KEY, "u1"), date_utc=DAY)],
                         [make_config(snapshot_ms=ms(DAY))]), principal_key_id=key_id(ORG_KEY))
        store.put(result([lic(KEY_B, "u1")], key=KEY_B), principal_key_id=key_id(KEY_B))
        store.retain(identity_before_ms=0)
    assert ledger_dump() == before


# ---------------------------------------------------------------------------------------------
# bad input
# ---------------------------------------------------------------------------------------------


def test_bad_batches_raise_and_store_nothing(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    kid = key_id(ORG_KEY)
    with pytest.raises(UsageError):
        store.put("not a result", principal_key_id=kid)  # type: ignore[arg-type]
    wrong = result()
    wrong.licenses = [make_config()]  # type: ignore[list-item]
    with pytest.raises(ContractViolation):
        store.put(wrong, principal_key_id=kid)
    with pytest.raises(UsageError):
        store.put(result(), principal_key_id=5)  # type: ignore[arg-type]
    huge = make_activity(p(ORG_KEY, "u1"), date_utc=DAY, reported_cost_nano=2**70)
    with pytest.raises(UsageError):
        store.put(result([lic(ORG_KEY, "u1")], [huge]), principal_key_id=kid)
    surrogate = lic(ORG_KEY, "u2", team="bad\ud800team")
    with pytest.raises(UsageError):
        store.put(result([lic(ORG_KEY, "u3"), surrogate]), principal_key_id=kid)
    assert store.licenses(**W) == [] and store.activity(**W) == []    # rolled back

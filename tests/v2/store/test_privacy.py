"""Privacy enforced at write time (SPEC §7.2, §7.6, §8.1–§8.3, D39, R-E21): file modes,
pseudonyms, key-id checks, key-id adoption, people never grouped or returned."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY, assert_no_canary, make_ai_usage_row, make_request
from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.records import Fidelity
from tokenbill.store.db import SqliteStore
from tokenbill.store.pseudonym import Pseudonymizer, is_collector_ref, is_name_hash

from .helpers import (
    DAY,
    FOREVER,
    NAME_KEY,
    ORG_KEY,
    OTHER_NAME_KEY,
    T0,
    Stores,
    data_dump,
    five_sources,
    h,
    keyed,
    memory,
    p,
    ref,
    result,
    src,
)

posix = pytest.mark.skipif(os.name == "nt", reason="POSIX modes are no-ops on Windows (D44)")


def _blobs(path: Path) -> list[bytes]:
    return [f.read_bytes() for f in path.parent.iterdir() if f.is_file()]


@posix
def test_file_0600_in_a_0700_directory(stores: Stores) -> None:
    store = keyed(stores)
    for r in five_sources():
        store.ingest(r)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    for extra in store.path.parent.iterdir():         # the WAL and shared-memory files too
        assert stat.S_IMODE(extra.stat().st_mode) & 0o077 == 0, extra.name


def test_collector_principals_are_stored_only_as_p(stores: Stores) -> None:
    canary_ref = f"r_{CANARY}"
    reqs = [make_request("L-c", 0, T0, {"uncached_input": 10, "output": 1}, "claude-opus-5-5",
                         request_id="rq_r", attribution={"principal": canary_ref,
                                                         "team": "t"}),
            make_request("L-c", 1, T0 + 1, {"uncached_input": 10, "output": 1},
                         "claude-opus-5-5", request_id="rq_c",
                         attribution={"principal": "c_" + "ab" * 10, "team": "t"})]
    store = keyed(stores)
    counts = store.ingest(result(src("s_c", "claude-code", principal_key=None), reqs))
    assert counts["principals_pseudonymized"] == 2
    got = {r.request_id: r.attribution.principal for r in store.iter_requests(**FOREVER)}
    assert got == {"rq_r": pseudonym(ORG_KEY, "p", CANARY),
                   "rq_c": pseudonym(ORG_KEY, "p", "c:" + "ab" * 10)}
    conn = store.connection
    for table in ("requests", "inferences", "merge_members"):
        for (value,) in conn.execute(f"SELECT principal FROM {table}"):
            assert value is None or value.startswith("p_")
    store.close()
    assert_no_canary(*_blobs(store.path))
    assert not any(canary_ref.encode() in b for b in _blobs(store.path))


def test_collector_principals_without_an_org_key_raise_before_any_write(stores: Stores) -> None:
    store = stores.open(name_key_id=key_id(NAME_KEY))
    with pytest.raises(PrivacyError):
        store.ingest(five_sources()[0])
    assert data_dump(store) == []


def test_names_under_another_name_key_are_nulled(stores: Stores) -> None:
    other = result(src("s_other", "trace@1", name_key=OTHER_NAME_KEY), [
        make_request("L-o", 0, T0, {"uncached_input": 10, "output": 1}, "claude-opus-5-5",
                     request_id="rq_o", attribution={"repo": h("repo", OTHER_NAME_KEY),
                                                     "cwd_key": h("/x", OTHER_NAME_KEY),
                                                     "extra": (("gateway",
                                                                h("gw", OTHER_NAME_KEY)),),
                                                     "team": "t"})])
    store = keyed(stores)
    counts = store.ingest(other)
    assert counts["dq.name_key_mismatch"] == 3 and counts["names_nulled"] == 3
    (req,) = store.iter_requests(**FOREVER)
    assert (req.attribution.repo, req.attribution.cwd_key, req.attribution.extra) == (
        None, None, ())
    assert store.dq_counts()["dq.name_key_mismatch"] == 3


def test_p_values_under_another_principal_key_are_nulled(stores: Stores) -> None:
    other_key = bytes(range(10, 42))
    r = result(src("s_px", "trace@2", principal_key=other_key), [
        make_request("L-px", 0, T0, {"uncached_input": 10, "output": 1}, "claude-opus-5-5",
                     request_id="rq_px", attribution={"principal": pseudonym(other_key, "p", "x"),
                                                      "team": "t"})])
    store = keyed(stores)
    counts = store.ingest(r)
    assert counts["dq.principal_key_mismatch"] == 1
    assert [q.attribution.principal for q in store.iter_requests(**FOREVER)] == [None]


def test_the_first_source_sets_the_name_key_id(stores: Stores) -> None:
    store = stores.open(org_key=ORG_KEY)          # no name key id up front
    assert store.meta()["name_key_id"] == ""
    store.ingest(five_sources()[2])
    assert store.meta()["name_key_id"] == key_id(NAME_KEY)
    counts = store.ingest(five_sources()[3])      # trace@1 under another name key
    assert counts["names_nulled"] == 2          # one h_ repo per request


@pytest.mark.parametrize("bad", [["principal"], ["team", "principal"], ["session"],
                                 ["session_key"]])
def test_people_are_never_grouped(stores: Stores, bad: list[str]) -> None:
    store = keyed(stores)
    store.ingest(five_sources()[2])
    with pytest.raises(PrivacyError):
        store.aggregate(group_by=bad, **FOREVER)
    with pytest.raises(PrivacyError):
        store.cost_rows(group_by=bad, **FOREVER)


def test_people_are_never_filters_and_unknown_keys_are_refused(stores: Stores) -> None:
    store = keyed(stores)
    for call in (lambda w: list(store.iter_requests(where=w, **FOREVER)),
                 lambda w: list(store.iter_lanes(where=w, **FOREVER)),
                 lambda w: store.count_users(where=w, **FOREVER),
                 lambda w: store.aggregate(group_by=[], where=w, **FOREVER)):
        with pytest.raises(PrivacyError):
            call({"principal": "p_" + "0" * 20})
        with pytest.raises(UsageError):
            call({"no_such_dim": "x"})
    with pytest.raises(UsageError):
        store.aggregate(group_by=["no_such_dim"], **FOREVER)
    with pytest.raises(UsageError):
        store.cost_rows(group_by=["repo"], **FOREVER)
    with pytest.raises(PrivacyError):
        store.count_users(where={"principal": "x"}, source="cost_lines", **FOREVER)
    with pytest.raises(UsageError):
        store.count_users(where={"repo": "x"}, source="cost_lines", **FOREVER)
    with pytest.raises(UsageError):
        store.count_users(where={}, source="licenses", **FOREVER)


@pytest.mark.parametrize("where", [{}, {"team": "payments"}, {"team": "platform"}, {"team": ""},
                                   {"lane_kind": "main"}, {"lane_kind": "unknown"},
                                   {"model": "claude-opus-5-5"}, {"billing_class": "allowance"},
                                   {"billing_class": "billed"}, {"channel": "anthropic_api"},
                                   {"date": DAY}, {"cost_center": "cc-1"}, {"project": ""},
                                   {"team": "payments", "lane_kind": "subagent"},
                                   {"workload_class": "interactive"}, {"provider": "anthropic"},
                                   {"billing_path": "subscription"}, {"repo": ""}])
def test_count_users_is_an_exact_distinct_count(stores: Stores, where: dict) -> None:
    store = keyed(stores)
    mem = memory()
    for r in five_sources():
        store.ingest(r)
        mem.ingest(r)
    got = store.count_users(where=where, **FOREVER)
    assert got == mem.count_users(where=where, **FOREVER)
    brute = {row[0] for row in store.connection.execute("SELECT principal FROM requests")
             if row[0] is not None} if not where else None
    if brute is not None:
        assert got == len(brute) == 4


def test_count_users_over_cost_lines(stores: Stores) -> None:
    who = [pseudonym(ORG_KEY, "p", u) for u in ("u1", "u2", "u3")]
    lines = [make_ai_usage_row(principal=w, team="t1" if i < 2 else "t2", credits="10",
                               date_utc="2026-09-10")[0] for i, w in enumerate(who)]
    store = keyed(stores)
    store.ingest(result(src("s_ai", "github-ai-usage"), cost_lines=lines))
    assert store.count_users(where={}, source="cost_lines", **FOREVER) == 3
    assert store.count_users(where={"team": "t1"}, source="cost_lines", **FOREVER) == 2
    assert store.count_users(where={"cost_center": ""}, source="cost_lines", **FOREVER) == 3
    assert store.count_users(where={}, source="cost_lines", since_ms=0, until_ms=0) == 0


# ---------------------------------------------------------------------------------------------
# key-id adoption (R-E21, amendment A-2)
# ---------------------------------------------------------------------------------------------

EXPORT_KEY = bytes(range(0, 64, 2))
SECOND_KEY = bytes(range(1, 64, 2))


def _bundle(source_id: str, key: bytes, adapter: str = "copilot-export", users=("u1", "u2")):
    reqs, lines = [], []
    for i, user in enumerate(users):
        who = pseudonym(key, "p", user)
        reqs.append(make_request(f"CP-{source_id}-{i}", 0, T0 + i, {"uncached_input": 1000,
                                                                    "output": 100},
                                 "claude-sonnet-5", provider="github", channel="github_copilot",
                                 billing_path="copilot_pool",
                                 request_id=stable_id("rq", source_id, i),
                                 attribution={"principal": who, "team": "t1",
                                              "billing_path": "copilot_pool"}))
        lines.append(make_ai_usage_row(principal=who, credits="5", team="t1",
                                       repo=pseudonym(key, "h", f"repo-{i}"))[0])
    s = src(source_id, adapter, name_key=key, principal_key=key)
    return result(s, reqs, cost_lines=lines)


def test_a_keyless_store_adopts_the_first_export_bundle(stores: Stores) -> None:
    store = stores.open(adopt_key_ids=True)
    store.ingest(_bundle("bundle-1", EXPORT_KEY))
    meta = store.meta()
    assert meta["adopted_key_id"] == meta["org_key_id"] == key_id(EXPORT_KEY)
    assert meta["adopted_name_key_id"] == meta["name_key_id"] == key_id(EXPORT_KEY)
    assert meta["org_key_mode"] == "adopted"
    assert [a[2] for a in store.audit_log()] == ["adopt_key_id"]
    assert key_id(EXPORT_KEY) in store.audit_log()[0][3]
    principals = {r.attribution.principal for r in store.iter_requests(**FOREVER)}
    assert principals == {pseudonym(EXPORT_KEY, "p", u) for u in ("u1", "u2")}
    assert all(c.repo is not None for c in store.cost_lines(**FOREVER))
    # a second bundle under another key id is refused, nothing stored
    before = data_dump(store)
    with pytest.raises(UsageError):
        store.ingest(_bundle("bundle-2", SECOND_KEY))
    assert data_dump(store) == before
    # another adapter's key id is never adopted: its p_ values are nulled
    counts = store.ingest(_bundle("vendor", SECOND_KEY, adapter="github-ai-usage",
                                  users=("u9",)))
    assert counts["dq.principal_key_mismatch"] >= 1
    assert store.meta()["adopted_key_id"] == key_id(EXPORT_KEY)
    # r_ principals need the store's own org key
    with pytest.raises(PrivacyError):
        store.ingest(five_sources()[0])
    # the same bundle key id again (next month) is accepted
    counts = store.ingest(_bundle("bundle-3", EXPORT_KEY, users=("u3",)))
    assert counts["principals_nulled"] == 0


def test_an_org_keyed_store_keeps_both_key_spaces(stores: Stores) -> None:
    store = keyed(stores, adopt_key_ids=True)
    store.ingest(_bundle("bundle-1", EXPORT_KEY))
    store.ingest(five_sources()[2])
    meta = store.meta()
    assert meta["org_key_id"] == key_id(ORG_KEY) and meta["org_key_mode"] == "own"
    assert meta["adopted_key_id"] == key_id(EXPORT_KEY)
    principals = {r.attribution.principal for r in store.iter_requests(**FOREVER)}
    assert pseudonym(EXPORT_KEY, "p", "u1") in principals and p("carol") in principals
    # purge works for adopted values too (the admin supplies the p_)
    assert store.purge(principal=pseudonym(EXPORT_KEY, "p", "u1"), actor="admin") == 1
    assert store.count_users(where={}, source="cost_lines", **FOREVER) == 1


def test_without_adoption_a_bundle_is_nulled(stores: Stores) -> None:
    store = keyed(stores)
    counts = store.ingest(_bundle("bundle-1", EXPORT_KEY))
    assert counts["dq.principal_key_mismatch"] == 4          # 2 requests + 2 cost lines
    assert store.meta()["adopted_key_id"] == ""
    assert all(r.attribution.principal is None for r in store.iter_requests(**FOREVER))


def test_adoption_survives_reopening(stores: Stores) -> None:
    store = stores.open(adopt_key_ids=True)
    store.ingest(_bundle("bundle-1", EXPORT_KEY))
    path = store.path
    store.close()
    again = SqliteStore(path)                       # adoption flag off: the adopted id is kept
    counts = again.ingest(_bundle("bundle-4", EXPORT_KEY, users=("u5",)))
    assert counts["principals_nulled"] == 0
    again.close()


def test_reopening_with_another_key_is_refused(stores: Stores) -> None:
    store = keyed(stores)
    path = store.path
    store.close()
    with pytest.raises(UsageError):
        SqliteStore(path, org_key=bytes(range(200, 232)))
    with pytest.raises(UsageError):
        SqliteStore(path, org_key=ORG_KEY, name_key_id="k_other")
    fresh = stores.open()
    fresh_path = fresh.path
    fresh.close()
    upgraded = SqliteStore(fresh_path, org_key=ORG_KEY, name_key_id=key_id(NAME_KEY))
    assert upgraded.meta()["org_key_id"] == key_id(ORG_KEY)
    assert upgraded.meta()["org_key_mode"] == "own"
    upgraded.close()


# ---------------------------------------------------------------------------------------------
# Pseudonymizer (store/pseudonym.py)
# ---------------------------------------------------------------------------------------------


def test_pseudonymizer_rules() -> None:
    ps = Pseudonymizer(ORG_KEY, accepted_key_ids=["k_adopted"])
    assert ps.principal("r_alice") == pseudonym(ORG_KEY, "p", "alice")
    assert ps.principal("c_" + "0f" * 10) == pseudonym(ORG_KEY, "p", "c:" + "0f" * 10)
    own = pseudonym(ORG_KEY, "p", "x")
    assert ps.principal(own) == own
    assert ps.principal(own, key_id=key_id(ORG_KEY)) == own
    assert ps.principal(own, key_id="k_adopted") == own
    assert ps.principal(own, key_id="k_other") is None
    assert ps.principal(own, key_id=None) is None
    assert ps.principal(None) is None
    assert ps.accepted_key_ids == {key_id(ORG_KEY), "k_adopted"}
    with pytest.raises(UsageError):
        ps.principal("alice@example.com")
    with pytest.raises(UsageError):
        ps.principal(42)  # type: ignore[arg-type]
    keyless = Pseudonymizer(None)
    with pytest.raises(PrivacyError):
        keyless.principal("r_alice")
    assert keyless.principal(own) is None and keyless.key_id is None
    with pytest.raises(UsageError):
        Pseudonymizer(b"")
    assert is_collector_ref("r_x") and is_collector_ref("c_x") and not is_collector_ref("p_x")
    assert is_name_hash("h_x") and not is_name_hash(None)


def test_merge_records_hold_no_collector_refs(stores: Stores) -> None:
    store = keyed(stores)
    for r in five_sources():
        store.ingest(r)
    docs = [d for (d,) in store.connection.execute("SELECT doc FROM merge_members")]
    assert docs and not any('"r_' in d or '"c_' in d for d in docs)
    assert store.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 5


def test_request_sources_round_trip(stores: Stores) -> None:
    store = keyed(stores)
    store.ingest(five_sources()[2])
    sources = {r.request_id: r.source for r in store.iter_requests(**FOREVER)}
    assert all(s == ref("trace@2", "s_trace2", Fidelity.FULL, 50, s.locator.split(":")[1] and
                        int(s.locator.split(":")[1])) for s in sources.values())

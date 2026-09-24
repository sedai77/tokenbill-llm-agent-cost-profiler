"""MemoryStore: the executable specification of the store (SPEC §7; F-KIT acceptance)."""

from __future__ import annotations

import dataclasses
import itertools
import json
from decimal import Decimal

import pytest

from tokenbill.core import testing as kit
from tokenbill.core.builders import CANARY, make_request
from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.labels import Basis
from tokenbill.core.protocols import LedgerStore
from tokenbill.core.records import (
    AppendedItem,
    CacheDiagnostic,
    Fidelity,
    LaneEvent,
    LaneKind,
    RequestParams,
    UsageBuckets,
)
from tokenbill.core.types import ContractOverlay

ORG = kit.STORE_ORG_KEY
NAME_ID = key_id(kit.STORE_NAME_KEY)
DAY = "2026-09-23"
T0 = kit._date_start_ms(DAY) + 10 * 3_600_000
W = {"since_ms": 0, "until_ms": 2**53}


def store(**kw) -> kit.MemoryStore:
    base = {"org_key": ORG, "name_key_id": NAME_ID, "pricer": kit.FakePricer()}
    base.update(kw)
    return kit.MemoryStore(**base)


def src(source_id: str, adapter: str = "claude-code", **kw) -> kit.SourceInfo:
    return dataclasses.replace(kit._src(source_id, adapter), **kw)


def req(lane: str, seq: int, ts: int, usage: dict, *, msg: str | None = None, rq: str | None = None,
        adapter: str = "claude-code", fidelity: Fidelity = Fidelity.FULL, priority: int = 40,
        attribution: dict | None = None, model: str = "claude-opus-5-5", **kw):
    rid = stable_id("rq", "anthropic", msg) if msg else stable_id("rq", adapter, lane, seq)
    r = make_request(lane, seq, ts, usage, model, session_key=f"S-{lane}", request_id=rid,
                     message_id=msg, attribution=attribution or {"principal": "r_alice"},
                     source=kit._ref(adapter, f"s_{adapter}", fidelity, priority, seq), **kw)
    if rq is not None:
        att = dataclasses.replace(r.attempts[0], provider_request_id=rq)
        r = dataclasses.replace(r, attempts=(att,))
    return r


def result(source, requests, **kw):
    return kit._result(source, requests, **kw)


# ---------- conformance ----------


def test_memory_store_conforms() -> None:
    summary = kit.assert_store_conforms(lambda **kw: kit.MemoryStore(**kw), permutations=120)
    assert summary == {"requests": 13, "orders": 120, "lanes": 6}
    assert isinstance(kit.MemoryStore(), LedgerStore)


def test_aggregate_group_by_principal_raises() -> None:
    s = store()
    for bad in (["principal"], ["team", "principal"], ["session"], ["session_key"]):
        with pytest.raises(PrivacyError):
            s.aggregate(group_by=bad, **W)
    with pytest.raises(UsageError):
        s.aggregate(group_by=["bucket"], **W)
    with pytest.raises(PrivacyError):
        s.aggregate(group_by=["team"], where={"principal": "p_" + "1" * 20}, **W)


# ---------- merge rules (SPEC §7.3) ----------


def _transcript_and_otel():
    transcript = req("L1", 0, T0, {"cache_read": 90_000, "cache_write_1h": 4000, "output": 600},
                     msg="msg_1", rq="req_1", attribution={"principal": "r_alice",
                                                           "team": "payments"})
    otel = req("L-otel", 0, T0 + 5, {"cache_read": 90_000, "cache_write_unknown": 4000,
                                     "output": 600}, rq="req_1", adapter="otlp",
               fidelity=Fidelity.NO_TTL_SPLIT, priority=20,
               attribution={"principal": "r_alice", "team": "ignored-lower-priority",
                            "cost_center": "cc-9", "extra": (("mdm_group", "g7"),),
                            "agent_type": "general-purpose"})
    return (result(src("s_cc"), [transcript]), result(src("s_otel", "otlp"), [otel]))


def test_full_transcript_and_no_ttl_split_otel_merge() -> None:
    for order in itertools.permutations(_transcript_and_otel()):
        s = store()
        for r in order:
            s.ingest(r)
        (merged,) = list(s.iter_requests(**W))
        assert merged.request_id == stable_id("rq", "anthropic", "msg_1")
        usage = merged.serving_inference.usage
        assert usage.cache_write_1h == 4000 and usage.cache_write_unknown == 0
        a = merged.attribution
        assert a.team == "payments"  # the transcript (priority 40) wins its non-null field
        assert (a.cost_center, a.agent_type, a.extra) == ("cc-9", "general-purpose",
                                                          (("mdm_group", "g7"),))
        assert a.principal == pseudonym(ORG, "p", "alice")
        state = s._merged().requests[merged.request_id]
        assert state.sources_mask == 1 | 8 and state.fidelity is Fidelity.FULL
        assert s.dq_counts()["dq.cross_source_usage_mismatch"] == 1
        (lane,) = list(s.iter_lanes(**W))
        assert lane.lane_key == "L1"


def test_request_id_collision_never_joins_even_across_sources() -> None:
    a = req("L1", 0, T0, {"output": 10}, msg="msg_a", rq="req_dup")
    b = req("L1", 1, T0 + 10, {"output": 20}, msg="msg_b", rq="req_dup")
    o = req("L-otel", 0, T0 + 5, {"output": 10}, rq="req_dup", adapter="otlp",
            fidelity=Fidelity.NO_TTL_SPLIT, priority=20)
    parts = [result(src("s_a"), [a]), result(src("s_b"), [b]), result(src("s_o", "otlp"), [o])]
    dumps = set()
    for order in itertools.permutations(parts):
        s = store()
        for r in order:
            s.ingest(r)
        assert len(list(s.iter_requests(**W))) == 3
        assert s.dq_counts()["dq.request_id_collision"] == 1
        dumps.add(kit._store_dump(s))
    assert len(dumps) == 1


def test_split_entry_rule_and_priority_rule() -> None:
    small = req("L1", 0, T0, {"output": 10}, msg="msg_s")
    big = req("L1", 0, T0, {"output": 500}, msg="msg_s")
    s = store()
    s.ingest(result(src("s1"), [small, big]))
    (only,) = list(s.iter_requests(**W))
    assert only.serving_inference.usage.output == 500
    rec = req("L2", 0, T0, {"output": 1}, msg="msg_p", adapter="trace@2", priority=50)
    resp = req("L2", 0, T0, {"output": 999}, msg="msg_p", adapter="anthropic-responses",
               priority=35, attribution={"principal": "r_alice", "project": "proj"})
    s2 = store()
    s2.ingest(result(src("s2", "anthropic-responses"), [resp]))
    s2.ingest(result(src("s3", "trace@2"), [rec]))
    (merged,) = list(s2.iter_requests(**W))
    assert merged.serving_inference.usage.output == 1
    assert merged.attribution.project == "proj"
    higher = req("L3", 0, T0, {"output": 1}, msg="msg_f", adapter="otlp",
                 fidelity=Fidelity.NO_TTL_SPLIT, priority=90)
    lower = req("L3", 0, T0, {"output": 2}, msg="msg_f", adapter="claude-code",
                fidelity=Fidelity.FULL, priority=40)
    s3 = store()
    s3.ingest(result(src("s4", "otlp"), [higher]))
    s3.ingest(result(src("s5"), [lower]))
    (fid,) = list(s3.iter_requests(**W))
    assert fid.serving_inference.usage.output == 2  # fidelity beats priority


def test_diagnostics_and_params_fill_if_null() -> None:
    diag = CacheDiagnostic(reason="tools_changed", provider_reason="tools_changed",
                           missed_input_tokens_estimate=10, source="anthropic.cache_diagnostics")
    plain = req("L1", 0, T0, {"output": 5}, msg="msg_d")
    extra = req("L1", 0, T0, {"output": 5}, msg="msg_d", adapter="anthropic-responses",
                priority=35, diagnostics=diag,
                params=RequestParams(model_requested="claude-opus-5-5", effort="high",
                                     betas=("b1",)))
    s = store()
    s.ingest(result(src("s1"), [plain]))
    s.ingest(result(src("s2", "anthropic-responses"), [extra]))
    (m,) = list(s.iter_requests(**W))
    assert m.final_attempt.diagnostics == diag
    assert m.params.effort == "high" and m.params.betas == ("b1",)


# ---------- privacy at write time ----------


def test_collector_principals_need_the_org_key_and_ingest_is_atomic() -> None:
    s = kit.MemoryStore(pricer=kit.FakePricer())
    good = req("L1", 0, T0, {"output": 1}, msg="msg_ok", attribution={"principal": None})
    bad = req("L1", 1, T0 + 1, {"output": 1}, msg="msg_bad")
    with pytest.raises(PrivacyError):
        s.ingest(result(src("s1"), [good, bad]))
    assert list(s.iter_requests(**W)) == []
    s.ingest(result(src("s1"), [good]))
    assert len(list(s.iter_requests(**W))) == 1


def test_c_and_p_principals_and_name_keys() -> None:
    c = req("L1", 0, T0, {"output": 1}, msg="msg_c", attribution={"principal": "c_" + "a" * 20})
    p_ok = req("L2", 0, T0, {"output": 1}, msg="msg_p", attribution={
        "principal": "p_" + "b" * 20, "repo": pseudonym(kit.STORE_NAME_KEY, "h", "repo"),
        "cwd_key": pseudonym(kit.STORE_NAME_KEY, "h", "/x")})
    s = store()
    counts = s.ingest(result(src("s1"), [c, p_ok]))
    assert counts["principals_pseudonymized"] == 1 and counts["dq.name_key_mismatch"] == 0
    got = {r.lane_key: r.attribution for r in s.iter_requests(**W)}
    assert got["L1"].principal == pseudonym(ORG, "p", "c:" + "a" * 20)
    assert got["L2"].principal == "p_" + "b" * 20 and got["L2"].repo is not None
    other = src("s2", principal_key_id="k_other", name_key_id="k_othername")
    foreign = req("L3", 0, T0, {"output": 1}, msg="msg_f", attribution={
        "principal": "p_" + "c" * 20, "repo": pseudonym(b"x" * 32, "h", "repo"),
        "api_key_id": pseudonym(b"x" * 32, "h", "key")},
        appended=[AppendedItem(kind="tool_result", name=pseudonym(b"x" * 32, "h", "mcp"),
                               n_bytes=10)])
    counts = s.ingest(result(other, [foreign]))
    assert counts["principals_nulled"] == 1 and counts["names_nulled"] == 3
    assert counts["dq.name_key_mismatch"] == 4
    f = next(r for r in s.iter_requests(**W) if r.lane_key == "L3")
    assert f.attribution.principal is None and f.attribution.repo is None
    assert f.attribution.api_key_id is None and f.appended[0].name is None
    assert s.dq_counts()["dq.name_key_mismatch"] == 4


def test_first_ingest_sets_the_name_key_id() -> None:
    s = kit.MemoryStore(org_key=ORG, pricer=kit.FakePricer())
    assert s.meta()["name_key_id"] == ""
    s.ingest(result(src("s1"), [req("L1", 0, T0, {"output": 1}, msg="m1")]))
    assert s.meta()["name_key_id"] == NAME_ID
    assert s.meta()["org_key_id"] == key_id(ORG) and s.meta()["schema_version"] == "memory@1"


def test_provider_records_follow_the_key_rules() -> None:
    agg = kit._aggregate()
    line = dataclasses.replace(kit._cost_line(), principal="p_" + "d" * 20,
                               workspace_id=pseudonym(b"z" * 32, "h", "acct"))
    s = store()
    counts = s.ingest(result(src("s1", principal_key_id="k_x", name_key_id="k_y"), [],
                             aggregates=[agg], cost_lines=[line]))
    assert counts["names_nulled"] == 2 and counts["principals_nulled"] == 1
    (a,) = s.aggregates(**W)
    assert "api_key_id" not in dict(a.dims)
    (c,) = s.cost_lines(**W)
    assert c.principal is None and c.workspace_id is None


def test_same_source_is_a_no_op_and_content_never_leaks() -> None:
    s = store()
    r = result(src("s1"), [req("L1", 0, T0, {"output": 1}, msg="m1")])
    assert s.ingest(r)["skipped"] == 0
    again = s.ingest(r)
    assert again["skipped"] == 1 and again["requests"] == 0
    assert CANARY not in repr(s._contribs)


# ---------- reading ----------


def _ledger() -> kit.MemoryStore:
    s = store()
    for r in kit._conformance_sources():
        s.ingest(r)
    return s


def test_windows_and_filters() -> None:
    s = _ledger()
    everything = list(s.iter_requests(**W))
    later = list(s.iter_requests(since_ms=kit._T0 + 450_000, until_ms=2**53))
    assert 0 < len(later) < len(everything)
    assert all(r.ts_start_ms >= kit._T0 + 450_000 for r in later)
    assert [r.lane_key for r in s.iter_requests(where={"team": "data"}, **W)] == ["L-t1"] * 2
    lanes = list(s.iter_lanes(where={"workload_class": "ci"}, **W))
    assert [ln.lane_key for ln in lanes] == ["L-sdk"]
    none_team = list(s.iter_lanes(where={"team": ""}, **W))
    assert none_team == []
    with pytest.raises(UsageError):
        list(s.iter_lanes(where={"colour": "x"}, **W))
    with pytest.raises(PrivacyError):
        list(s.iter_requests(where={"principal": "p_x"}, **W))
    main = next(ln for ln in s.iter_lanes(**W) if ln.lane_key == "L-main")
    assert [e.kind.value for e in main.events] == ["human_prompt"]
    early = list(s.iter_lanes(since_ms=0, until_ms=kit._T0 + 1))
    assert all(ln.lane_key != "L-main" or len(ln.requests) == 1 for ln in early)
    assert all(not ln.events for ln in early if ln.lane_key == "L-main")


def test_usage_records_and_indexes() -> None:
    s = _ledger()
    recs = list(s.iter_usage_records(**W))
    assert len(recs) == 13 and all(r.billable is not False for r in recs)
    assert {r.lane_kind for r in recs} >= {LaneKind.MAIN, LaneKind.API_RUN}
    assert all(r.date_utc == DAY for r in recs)
    assert list(s.iter_usage_records(since_ms=2**52, until_ms=2**53)) == []
    idx = list(s.lane_index(**W))
    assert [r.lane_key for r in idx] == sorted(r.lane_key for r in idx)
    assert sum(r.requests for r in idx) == 13
    first = list(s.lane_first_reads(**W))
    assert ("ws:w1", "claude-opus-5-5", 0) in first


def test_count_users_is_a_distinct_count() -> None:
    s = _ledger()
    assert s.count_users(where={}, **W) == 4  # alice, bob, carol, the trace@1 p_ value
    assert s.count_users(where={"team": "payments"}, **W) == 2
    assert s.count_users(where={"lane_kind": "subagent"}, **W) == 1
    assert s.count_users(where={"cost_center": "cc-1"}, **W) == 1


def test_aggregate_rows_and_pricer_override() -> None:
    s = _ledger()
    raw = s.aggregate(group_by=["team", "model"], **W)
    assert raw.group_by == ("team", "model") and raw.window == (0, 2**53)
    keys = [dict(r.dims)["team"] for r in raw.rows]
    assert keys == sorted(keys, key=lambda t: (t is None, t or ""))
    pay = [r for r in raw.rows if dict(r.dims) == {"team": "payments",
                                                   "model": "claude-opus-5-5"}]
    assert pay and pay[0].n_users == 1 and pay[0].priced.exact.basis is Basis.LIST
    filtered = s.aggregate(group_by=["billing_path"], where={"billing_class": "allowance"}, **W)
    assert [dict(r.dims)["billing_path"] for r in filtered.rows] == ["subscription"]
    contract = kit.FakePricer().with_contract(
        ContractOverlay(name="c", multiplier=Decimal("0.5"), overrides=(),
                            effective_from="2026-01-01", effective_to=None, derived=False,
                            assumed_fields=()))
    half = s.aggregate(group_by=[], pricer=contract, **W).rows[0].priced.exact.nano
    full = s.aggregate(group_by=[], **W).rows[0].priced.exact.nano
    assert half * 2 == full


def test_no_pricer_means_unpriced() -> None:
    s = kit.MemoryStore(org_key=ORG, name_key_id=NAME_ID)
    s.ingest(result(src("s1"), [req("L1", 0, T0, {"output": 100}, msg="m1")]))
    row = s.aggregate(group_by=[], **W).rows[0]
    assert row.priced.unpriced_inferences == 1 and row.priced.coverage == "0"
    assert [r.point_nano for r in s.lane_index(**W)] == [0]
    assert s.cost_rows(group_by=["model"], **W) == []
    assert s.reprice(kit.FakePricer()) == 1
    assert s.cost_rows(group_by=["model"], **W)[0].priced_nano == 2_000_000


def test_cost_rows_dimensions() -> None:
    s = _ledger()
    rows = s.cost_rows(group_by=["team"], **W)
    assert all(r.date_utc == "" and r.model == "" and r.lane_kind == "" for r in rows)
    assert {r.basis for r in rows} == {Basis.LIST, Basis.LIST_EQUIVALENT}
    unknown = [r for r in rows if r.bucket == "cache_write_unknown"]
    assert unknown and all(r.priced_nano == 0 and r.estimated_high_nano > r.estimated_low_nano
                           for r in unknown)
    with pytest.raises(UsageError):
        s.cost_rows(group_by=["repo"], **W)
    by_all = s.cost_rows(group_by=list(kit._COST_DIMS), **W)
    assert all(r.rate_row_id for r in by_all)


def test_cluster_days_kinds() -> None:
    s = _ledger()
    mdm = s.cluster_days(cluster_kind="mdm_group", since=DAY, until="2026-09-24")
    assert {c.cluster_id for c in mdm} == {"g1", "g2"}
    assert s.cluster_days(cluster_kind="workspace", since=DAY, until="2026-09-24") == []
    team = s.cluster_days(cluster_kind="team", since=DAY, until="2026-09-24")
    pay = next(c for c in team if c.cluster_id == "payments")
    assert pay.allowance_nano > 0 and pay.exact_nano > 0
    assert s.cluster_days(cluster_kind="team", since="2026-09-24", until="2026-09-25") == []
    with pytest.raises(UsageError):
        s.cluster_days(cluster_kind="person", since=DAY, until="2026-09-24")


def test_provider_record_windows() -> None:
    s = _ledger()
    later = kit._date_start_ms("2026-09-24")
    assert s.aggregates(since_ms=later, until_ms=2**53) == []
    assert s.cost_lines(since_ms=later, until_ms=2**53) == []
    assert s.outcomes(since_ms=0, until_ms=kit._date_start_ms(DAY)) == []
    assert len(s.outcomes(since_ms=kit._T0, until_ms=kit._T0 + 1)) == 1
    with pytest.raises(UsageError):
        s.aggregates(since=0)


def test_purge_by_date_and_audit() -> None:
    s = _ledger()
    with pytest.raises(UsageError):
        s.purge(actor="me")
    n = s.purge(before_ms=kit._T0 + 60_000, actor="ops")
    assert n > 0
    assert all(r.ts_start_ms >= kit._T0 + 60_000 for r in s.iter_requests(**W))
    assert s.cost_lines(**W) and s.aggregates(**W)
    s.purge(before_ms=kit._date_start_ms("2026-09-25"), actor="ops")
    assert (list(s.iter_requests(**W)), s.cost_lines(**W), s.aggregates(**W), s.outcomes(**W)) \
        == ([], [], [], [])
    log = s.audit_log()
    assert [row[2] for row in log] == ["purge", "purge"]
    assert json.loads(log[0][3]) == {"before_ms": kit._T0 + 60_000, "by": "before_ms",
                                     "rows": n}


def test_purge_by_principal_removes_cost_lines_and_leaves_no_identity() -> None:
    s = store()
    person = "p_" + "e" * 20
    line = dataclasses.replace(kit._cost_line(), principal=person)
    s.ingest(result(src("s1"), [req("L1", 0, T0, {"output": 1}, msg="m1",
                                    attribution={"principal": person})], cost_lines=[line]))
    assert s.purge(principal=person, actor="dpo") == 1
    assert s.cost_lines(**W) == [] and list(s.iter_requests(**W)) == []
    assert person not in repr(s.audit_log())


def test_findings_and_receipts_edge_cases() -> None:
    s = store()
    assert s.findings() == [] and s.findings("nope") == []
    f = kit._conformance_finding()
    s.put_findings("run-a", [f])
    s.put_findings("run-b", [f])
    s.put_findings("run-a", [dataclasses.replace(f, n_users=7)])
    assert [x.n_users for x in s.findings()] == [7]  # run-a was written last
    assert s.findings("run-b") == [f]


def test_events_are_deduplicated_and_shell_requests_count() -> None:
    ev = LaneEvent(lane_key="L1", ts_ms=T0 + 1, kind="clear")
    inner = req("L1", 0, T0, {"output": 3}, msg="m_in")
    shell = kit._shell("L1", "S-L1", LaneKind.SUBAGENT)
    lane = dataclasses.replace(shell.lanes[0], requests=(inner,), events=(ev,))
    session = dataclasses.replace(shell, lanes=(lane,))
    s = store()
    counts = s.ingest(result(src("s1"), [], sessions=[session], events=[ev]))
    assert counts["requests"] == 1 and counts["events"] == 2
    (only,) = list(s.iter_lanes(**W))
    assert only.kind is LaneKind.SUBAGENT and len(only.events) == 1
    assert only.requests[0].serving_inference.usage == UsageBuckets(output=3)

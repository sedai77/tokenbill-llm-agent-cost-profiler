"""Merge rules (SPEC §7.3): idempotence, order independence, cross-source identity, the collision
guard, the split-entry rule and the trace@1 run-id regression ($22, not $40)."""

from __future__ import annotations

import dataclasses
import itertools
from decimal import Decimal

import pytest

from tokenbill.core.builders import FlatRates, make_request
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Basis
from tokenbill.core.records import CacheDiagnostic, Fidelity, LaneKind, RequestParams
from tokenbill.core.testing import FakePricer
from tokenbill.store import db as db_mod
from tokenbill.store import merge as M

from .helpers import (
    FOREVER,
    ORG_KEY,
    T0,
    Stores,
    api_dump,
    data_dump,
    five_sources,
    h,
    keyed,
    memory,
    p,
    ref,
    result,
    src,
    trace1_source,
    with_hint,
)


def _ingest_all(store, sources) -> None:
    for r in sources:
        store.ingest(r)


def test_ingesting_the_same_sources_twice_leaves_an_identical_dump(stores: Stores) -> None:
    store = keyed(stores)
    sources = five_sources()
    _ingest_all(store, sources)
    before = data_dump(store)
    assert before
    for r in sources:
        counts = store.ingest(r)
        assert counts["skipped"] == 1 and counts["requests"] == 0
    assert data_dump(store) == before


def test_every_permutation_of_five_sources_gives_the_same_dump(stores: Stores) -> None:
    sources = five_sources()
    reference = keyed(stores)
    _ingest_all(reference, sources)
    ref_dump = data_dump(reference)
    ref_api = api_dump(reference)
    for order in itertools.permutations(range(len(sources))):
        store = keyed(stores)
        _ingest_all(store, [sources[i] for i in order])
        assert data_dump(store) == ref_dump, order
        store.close()
    assert ref_api["requests"]


def test_order_independence_across_chunk_boundaries(stores: Stores,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    sources = five_sources()
    reference = keyed(stores)
    _ingest_all(reference, sources)
    monkeypatch.setattr(db_mod, "BATCH_ROWS", 2)
    small = keyed(stores)
    _ingest_all(small, reversed(sources))
    assert data_dump(small) == data_dump(reference)


def test_sqlite_equals_the_memory_reference(stores: Stores) -> None:
    sources = five_sources()
    store = keyed(stores)
    mem = memory()
    for r in sources:
        store.ingest(r)
        mem.ingest(r)
    assert api_dump(store) == api_dump(mem)
    assert store.dq_counts() == mem.dq_counts()


def test_cross_source_join_through_the_provider_request_id(stores: Stores) -> None:
    store = keyed(stores)
    _ingest_all(store, five_sources())
    reqs = {r.request_id: r for r in store.iter_requests(**FOREVER)}
    m1 = reqs[stable_id("rq", "anthropic", "msg_m1")]
    si = m1.serving_inference
    assert si is not None
    # FULL transcript usage wins over NO_TTL_SPLIT OTel usage
    assert (si.usage.cache_write_5m, si.usage.cache_write_unknown) == (102_000, 0)
    # OTel-only attribution fills the transcript request, incl. an extra key
    assert m1.attribution.cost_center == "cc-1"
    assert dict(m1.attribution.extra) == {"mdm_group": "g1", "task_id": "t-42"}
    assert m1.attribution.principal == p("alice")
    row = store.connection.execute(
        "SELECT sources_mask, fidelity, adapter, attr_prio_json FROM requests WHERE "
        "request_id=?", (m1.request_id,)).fetchone()
    assert row[0] == M.SOURCES_MASK_BITS["claude-code"] | M.SOURCES_MASK_BITS["otlp"]
    assert bin(row[0]).count("1") == 2
    assert (row[1], row[2]) == (int(Fidelity.FULL), "claude-code")
    assert '"cost_center":20' in row[3] and '"team":40' in row[3]
    members = store.connection.execute(
        "SELECT COUNT(*) FROM merge_members WHERE request_id=?", (m1.request_id,)).fetchone()[0]
    assert members == 2


def test_a_request_id_seen_with_two_message_ids_is_never_a_join_key(stores: Stores) -> None:
    store = keyed(stores)
    _ingest_all(store, five_sources())
    reqs = list(store.iter_requests(**FOREVER))
    otel_only = [r for r in reqs if r.lane_key == "L-otel"]
    assert len(otel_only) == 2          # q9 (no partner) and the one under the colliding req_dup
    row = store.connection.execute(
        "SELECT request_id, collision FROM request_index WHERE provider_request_id='req_dup'"
    ).fetchone()
    assert row == (None, 1)
    assert store.dq_counts()["dq.request_id_collision"] == 1


def _collision_sources() -> list:
    """The colliding pair and a message-less contribution under the same request id, one per
    source (the conformance suite keeps a collision inside one source; STORE also handles it
    across sources and orders)."""
    def req(n: int, source_id: str, adapter: str, msg: str | None, fid: Fidelity) -> object:
        r = make_request(f"L-{source_id}", n, T0 + 1000 * n, {"uncached_input": 100 + n,
                                                               "output": 10},
                         "claude-opus-5-5", session_key=f"S-{source_id}",
                         request_id=stable_id("rq", source_id, n), message_id=msg,
                         attribution={"team": "t"},
                         source=ref(adapter, source_id, fid, 20, n))
        return with_hint(r, "req_X")
    a = result(src("sa", "claude-code"), [req(1, "sa", "claude-code", "msg_1", Fidelity.FULL)])
    b = result(src("sb", "claude-code"), [req(2, "sb", "claude-code", "msg_2", Fidelity.FULL)])
    o = result(src("so", "otlp"), [req(3, "so", "otlp", None, Fidelity.NO_TTL_SPLIT)])
    return [a, b, o]


def test_collisions_across_sources_are_order_independent(stores: Stores) -> None:
    sources = _collision_sources()
    dumps = set()
    for order in itertools.permutations(range(3)):
        store = keyed(stores)
        mem = memory()
        for i in order:
            store.ingest(sources[i])
            mem.ingest(sources[i])
        assert api_dump(store) == api_dump(mem), order
        assert len(list(store.iter_requests(**FOREVER))) == 3
        dumps.add(tuple(data_dump(store)))
        store.close()
    assert len(dumps) == 1


def test_split_entry_rule_and_source_priority(stores: Stores) -> None:
    store = keyed(stores)
    _ingest_all(store, five_sources())
    reqs = {r.request_id: r for r in store.iter_requests(**FOREVER)}
    m2 = reqs[stable_id("rq", "anthropic", "msg_m2")]
    assert m2.serving_inference.usage.output == 500          # the larger output wins
    s0 = reqs[stable_id("rq", "anthropic", "msg_s0")]
    assert s0.serving_inference.usage.output == 800          # trace@2 (50) over responses (35)
    assert s0.attribution.project == "proj-1"                # lower priority fills a null field
    assert s0.source is not None and s0.source.adapter == "trace@2"
    assert len(reqs) == 13


def test_trace1_sessions_reusing_a_run_id_stay_apart_22_not_40(stores: Stores) -> None:
    """Two trace@1 files reusing ``run_id`` → two sessions; the total is the sum ($22)."""
    def file(source_id: str) -> object:
        r = trace1_source(source_id, run_id="run-1")
        usage = {"uncached_input": 1_000_000, "output": 2_000_000}      # $1 + $10 at FlatRates
        reqs = [dataclasses.replace(
            make_request(q.lane_key, q.seq, q.ts_start_ms, usage, "claude-haiku-4-5",
                         session_key=q.session_key, request_id=q.request_id,
                         attribution={"team": "data"}, source=q.source))
                for q in r.requests[:1]]
        return result(r.source, reqs, sessions=r.sessions)

    store = keyed(stores, pricer=FlatRates())
    store.ingest(file("s_file_a"))
    store.ingest(file("s_file_b"))
    lanes = list(store.iter_lanes(**FOREVER))
    assert len({lane.session_key for lane in lanes}) == 2
    assert len(lanes) == 2 and all(len(lane.requests) == 1 for lane in lanes)
    total = store.aggregate(group_by=[], **FOREVER).rows[0].priced.exact
    assert total.nano == 22 * 10**9 and total.basis is Basis.LIST
    assert total.usd == Decimal(22)


def test_usage_mismatch_between_merged_sources_is_counted(stores: Stores) -> None:
    store = keyed(stores)
    counts = [store.ingest(r) for r in five_sources()]
    assert sum(c["dq.cross_source_usage_mismatch"] for c in counts) >= 1
    assert store.dq_counts()["dq.cross_source_usage_mismatch"] == memory_mismatches()


def memory_mismatches() -> int:
    mem = memory()
    for r in five_sources():
        mem.ingest(r)
    return mem.dq_counts()["dq.cross_source_usage_mismatch"]


def test_reopening_the_ledger_continues_the_merge(stores: Stores) -> None:
    sources = five_sources()
    one = keyed(stores)
    _ingest_all(one, sources)
    path = stores.root / f"s{stores.n}" / "tokenbill.db"
    two = keyed(stores)
    _ingest_all(two, sources[:2])
    path2 = stores.root / f"s{stores.n}" / "tokenbill.db"
    two.close()
    reopened = db_mod.SqliteStore(path2, org_key=ORG_KEY, pricer=FakePricer(), now_ms=two._now())
    _ingest_all(reopened, sources[2:])
    assert data_dump(reopened) == data_dump(one)
    reopened.close()
    assert path.exists()


def test_identical_contributions_from_two_sources_count_once(stores: Stores) -> None:
    base = five_sources()[2]
    copy = result(src("s_trace2_copy", "trace@2"), base.requests, sessions=base.sessions)
    store = keyed(stores)
    store.ingest(base)
    before = data_dump(store, ("requests", "attempts", "inferences", "merge_members"))
    store.ingest(copy)
    assert data_dump(store, ("requests", "attempts", "inferences", "merge_members")) == before


# ---------------------------------------------------------------------------------------------
# merge.py units
# ---------------------------------------------------------------------------------------------


def _contrib(priority: int, *, team: str | None = None, extra: tuple = (), msg: str = "m",
             output: int = 1, params: RequestParams | None = None,
             diag: CacheDiagnostic | None = None, adapter: str = "otlp",
             fidelity: Fidelity = Fidelity.FULL) -> M.Contribution:
    req = make_request("L", 0, T0, {"output": output}, request_id=f"rq_{priority}_{output}",
                       message_id=msg, attribution={"team": team, "extra": extra},
                       params=params, diagnostics=diag,
                       source=ref(adapter, "s", fidelity, priority, 0))
    return M.Contribution.of(req, default_adapter=adapter, source_id="s")


def test_attribution_priority_then_smallest_value() -> None:
    merged = M.merge_contributions([_contrib(20, team="zeta", output=1),
                                    _contrib(20, team="alpha", output=2),
                                    _contrib(10, team="aaa", output=3,
                                             extra=(("gateway", "gw"),))])
    assert len(merged) == 1
    mg = merged[0]
    assert mg.request.attribution.team == "alpha"
    assert dict(mg.request.attribution.extra) == {"gateway": "gw"}
    assert mg.attr_prio == {"team": 20, "extra.gateway": 10}
    assert mg.request.serving_inference.usage.output == 2    # tie on fidelity/priority → output


def test_params_and_diagnostics_fill_if_null() -> None:
    diag = CacheDiagnostic(reason="tools_changed", provider_reason="x",
                           missed_input_tokens_estimate=5, source="anthropic.cache_diagnostics")
    hi = _contrib(50, output=9)
    lo = _contrib(10, output=1, params=RequestParams(model_requested="claude-opus-5-5",
                                                     effort="high"), diag=diag)
    mg = M.merge_contributions([lo, hi])[0]
    assert mg.winner is hi
    assert mg.request.params.effort == "high"
    assert mg.request.attempts[-1].diagnostics == diag
    assert mg.sources_mask == M.SOURCES_MASK_BITS["otlp"]


def test_request_id_prefers_contributions_with_a_message_id() -> None:
    a = _contrib(10, msg="m1")
    b = M.Contribution.of(dataclasses.replace(
        make_request("L", 0, T0, {"output": 1}, request_id="aaa_first"), attempts=tuple(
            dataclasses.replace(x, provider_message_id=None) for x in a.request.attempts)),
        default_adapter="otlp", source_id="s")
    b.request = dataclasses.replace(b.request, request_id=a.request.request_id)
    got = M.merge_contributions([a, b])
    assert [g.request.request_id for g in got] == [a.request.request_id]


def test_choose_version_and_shell_rank() -> None:
    from .helpers import aggregate_record, cost_line_record, shell
    old = aggregate_record(fetched_ms=5, finality="final")
    newer = aggregate_record(fetched_ms=9, finality="provisional", output=6)
    assert M.choose_version(old, newer) is old            # final beats a later provisional
    assert M.choose_version(newer, old) is old
    cp_old = cost_line_record(channel="github_copilot", finality="final", fetched_ms=5)
    cp_new = cost_line_record(channel="github_copilot", finality="provisional", fetched_ms=9,
                              amount=1)
    assert M.choose_version(cp_old, cp_new) is cp_new     # Copilot: latest fetch wins
    known = shell("L", "S", LaneKind.MAIN).lanes[0]
    unknown = shell("L", "S", LaneKind.UNKNOWN, "unknown").lanes[0]
    assert M.shell_rank(known) < M.shell_rank(unknown)


def test_hashed_names_under_the_stores_name_key_survive(stores: Stores) -> None:
    store = keyed(stores)
    _ingest_all(store, five_sources())
    sdk = [r for r in store.iter_requests(**FOREVER) if r.lane_key == "L-sdk"]
    assert sdk and all(r.attribution.repo == h("repo-x") for r in sdk)

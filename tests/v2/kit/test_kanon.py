"""k-anonymity (SPEC §8.4, ruling R-E1): publish, rescope_findings, guards (F-KIT acceptance)."""

from __future__ import annotations

import dataclasses
import itertools
import random
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import kanon
from tokenbill.core import testing as kit
from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.labels import Basis, estimated, exact
from tokenbill.core.records import UsageBuckets
from tokenbill.core.registry import _finding_id
from tokenbill.core.types import (
    AggRow,
    EvidenceItem,
    Finding,
    Fix,
    PricedTotal,
    PublishedAggregate,
    RawAggregate,
    Scope,
)

K = 5
OTHER = kanon.other_label(K)


def priced(nano: int, *, est: int | None = None) -> PricedTotal:
    return PricedTotal(exact=exact(nano, Basis.LIST),
                       estimated=None if est is None else estimated(est, Basis.LIST, low=0,
                                                                    high=2 * est, note="r"),
                       allowance=None, priced_inferences=1, unpriced_inferences=0,
                       unpriced_tokens=0, coverage="1")


def row(dims: dict, n_users: int, value: int, *, requests: int | None = None) -> AggRow:
    return AggRow(dims=tuple(dims.items()), n_users=n_users,
                  n_requests=requests if requests is not None else value % 97 + 1,
                  usage=UsageBuckets(uncached_input=value, output=value // 3 + 1),
                  priced=priced(value * 7 + 3))


def raw(group_by: tuple[str, ...], rows: list[AggRow]) -> RawAggregate:
    return RawAggregate(group_by=group_by, rows=tuple(rows), window=(0, 10))


def vector(r: AggRow) -> tuple[int, ...]:
    u = r.usage
    return (r.n_requests, u.uncached_input, u.output, r.priced.exact.nano or 0)


def add(vs) -> tuple[int, ...]:
    vs = list(vs)
    return tuple(sum(col) for col in zip(*vs, strict=True)) if vs else (0, 0, 0, 0)


# ---------- publish: worked examples ----------


def test_small_rows_merge_with_complementary_suppression() -> None:
    table = raw(("team",), [row({"team": "a"}, 10, 1000), row({"team": "b"}, 6, 2000),
                            row({"team": "c"}, 2, 3000)])
    pub = kanon.publish(table, k=K)
    assert isinstance(pub, PublishedAggregate)
    assert [dict(r.dims)["team"] for r in pub.rows] == ["a", OTHER]
    other = pub.rows[1]
    assert other.n_users == 6  # the largest constituent: a lower bound on distinct users
    assert vector(other) == add([vector(table.rows[1]), vector(table.rows[2])])
    assert (pub.suppressed_rows, pub.suppressed_users, pub.k) == (2, 8, K)
    assert add(vector(r) for r in pub.rows) == add(vector(r) for r in table.rows)


def test_nothing_below_k_is_published() -> None:
    table = raw(("team",), [row({"team": "a"}, 3, 10), row({"team": "b"}, 4, 20)])
    pub = kanon.publish(table, k=K)
    assert pub.rows == () and pub.suppressed_rows == 2 and pub.suppressed_users == 7
    single = kanon.publish(raw((), [row({}, 4, 10)]), k=K)
    assert single.rows == ()
    total = kanon.publish(raw((), [row({}, 9, 10)]), k=K)
    assert len(total.rows) == 1 and total.suppressed_rows == 0


def test_two_level_collapse() -> None:
    table = raw(("team", "model"), [
        row({"team": "a", "model": "x"}, 9, 100), row({"team": "a", "model": "y"}, 2, 200),
        row({"team": "b", "model": "x"}, 3, 300), row({"team": "b", "model": "y"}, 1, 400),
        row({"team": "c", "model": "x"}, 7, 500),
    ])
    pub = kanon.publish(table, k=K)
    dims = [dict(r.dims) for r in pub.rows]
    # team a: y merges with the complement a/x; team b has no row >= k and collapses to the top,
    # where it absorbs the smallest remaining row (c/x).
    assert dims == [{"team": "a", "model": OTHER}, {"team": OTHER, "model": OTHER}]
    assert add(vector(r) for r in pub.rows) == add(vector(r) for r in table.rows)
    assert all(r.n_users >= K for r in pub.rows)


def test_custom_parent_of() -> None:
    table = raw(("model", "team"), [row({"model": "x", "team": "a"}, 9, 1),
                                    row({"model": "y", "team": "a"}, 2, 2),
                                    row({"model": "x", "team": "b"}, 8, 3)])
    pub = kanon.publish(table, k=K, parent_of=lambda dims: (dims[1],))  # parent = team
    assert [dict(r.dims) for r in pub.rows] == [{"model": "x", "team": "b"},
                                                {"model": OTHER, "team": "a"}]


def test_merged_values_are_consistent() -> None:
    a = AggRow(dims=(("team", "a"),), n_users=2, n_requests=1,
               usage=UsageBuckets(cache_write_other=5, cache_write_other_ttl_s=1800),
               priced=priced(10, est=4))
    b = AggRow(dims=(("team", "b"),), n_users=9, n_requests=2,
               usage=UsageBuckets(cache_write_other=7, cache_write_other_ttl_s=3600, output=1),
               priced=dataclasses.replace(priced(20), allowance=exact(5, Basis.LIST_EQUIVALENT),
                                          unpriced_tokens=3, unpriced_inferences=1))
    (merged,) = kanon.publish(raw(("team",), [a, b]), k=K).rows
    assert merged.usage.cache_write_other == 12 and merged.usage.cache_write_other_ttl_s == 1800
    assert merged.priced.exact.nano == 30 and merged.priced.estimated.nano == 4
    assert merged.priced.allowance.nano == 5 and merged.priced.unpriced_inferences == 1
    assert merged.priced.coverage == str(Decimal(10) / Decimal(13))[:len(merged.priced.coverage)]


def test_publish_rejects_bad_input() -> None:
    with pytest.raises(UsageError):
        kanon.publish(raw(("team",), []), k=0)
    with pytest.raises(UsageError):
        kanon.publish(raw(("team",), []), k=True)  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        kanon.publish([])  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        PublishedAggregate(group_by=(), rows=(), window=(0, 1), k=5, suppressed_rows=0,
                           suppressed_users=0, token=object())


def test_published_for_tests_only_drops() -> None:
    table = raw(("team",), [row({"team": "a"}, 10, 1), row({"team": "b"}, 2, 2)])
    pub = kit.published_for_tests(table)
    assert [dict(r.dims)["team"] for r in pub.rows] == ["a"]
    assert (pub.suppressed_rows, pub.suppressed_users) == (1, 2)


# ---------- publish: the property test (1,000 seeded random tables) ----------


def _random_table(rnd: random.Random) -> RawAggregate:
    if rnd.random() < 0.5:
        n = rnd.randint(1, 8)
        rows = [row({"team": f"t{i}"}, rnd.randint(1, 12), rnd.randint(1, 10**9),
                    requests=rnd.randint(1, 10**6)) for i in range(n)]
        return raw(("team",), rows)
    teams, models = rnd.randint(1, 3), rnd.randint(1, 3)
    rows = [row({"team": f"t{i}", "model": f"m{j}"}, rnd.randint(1, 12), rnd.randint(1, 10**9),
                requests=rnd.randint(1, 10**6))
            for i in range(teams) for j in range(models) if rnd.random() < 0.85]
    return raw(("team", "model"), rows or [row({"team": "t0", "model": "m0"}, 3, 5)])


def test_publish_property_1000_seeded_tables() -> None:
    for seed in range(1000):
        rnd = random.Random(seed)
        table = _random_table(rnd)
        pub = kanon.publish(table, k=K)
        assert all(r.n_users >= K for r in pub.rows), seed
        published = [vector(r) for r in pub.rows]
        if pub.rows:
            assert add(published) == add(vector(r) for r in table.rows), seed
            assert pub.rows[0].priced.exact.basis is Basis.LIST
        else:
            assert all(r.n_users < K for r in table.rows), seed
        shown = {r.dims for r in pub.rows}
        suppressed = [r for r in table.rows if r.dims not in shown]
        assert pub.suppressed_rows == len(suppressed), seed
        assert pub.suppressed_users == sum(r.n_users for r in suppressed), seed
        # brute force: no suppressed child equals any sum of published rows, so no published total
        # minus published rows recovers it
        subset_sums = {add(c) for n in range(1, len(published) + 1)
                       for c in itertools.combinations(published, n)}
        for child in suppressed:
            assert vector(child) not in subset_sums, seed
        assert len(shown) == len(pub.rows), seed


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(["a", "b", "c", "d"]), st.sampled_from(["x", "y"]),
                          st.integers(1, 12), st.integers(1, 10**6)),
                min_size=1, max_size=8, unique_by=lambda t: (t[0], t[1])),
       st.integers(1, 8))
def test_publish_hypothesis(cells, k) -> None:
    table = raw(("team", "model"), [row({"team": t, "model": m}, n, v) for t, m, n, v in cells])
    pub = kanon.publish(table, k=k)
    assert all(r.n_users >= k for r in pub.rows)
    if pub.rows:
        assert add(vector(r) for r in pub.rows) == add(vector(r) for r in table.rows)
    assert pub.rows == kanon.publish(table, k=k).rows  # deterministic


# ---------- rescope_findings ----------


def finding(scope: dict, n_users: int, *, kind: str = "ttl-expiry", detector: str = "d.x",
            recoverable: int | None = 100, category: str = "lever", audience: str = "org",
            **kw) -> Finding:
    sc = Scope(dims=tuple(sorted(scope.items())))
    return Finding(
        finding_id=_finding_id(detector, kind, sc), detector_id=detector, kind=kind,
        detector_version="1", category=category, lever_class="cache_transform",
        audience=audience, title="t", summary="s", scope=sc, n_events=2, n_lanes=1,
        n_users=n_users, first_seen_ms=kw.pop("first_seen_ms", 50),
        cost_observed=exact(1000, Basis.LIST),
        recoverable=None if recoverable is None else estimated(recoverable, Basis.LIST, note="x"),
        references=kw.pop("references", ("ref-a",)), **kw)


def test_count_users_refuses_a_four_person_parent_whose_children_sum_to_five() -> None:
    a = finding({"team": "t", "lane_kind": "main", "model": "a"}, 2)
    b = finding({"team": "t", "lane_kind": "main", "model": "b"}, 3)
    calls = []

    def count_users(scope: Scope) -> int:
        calls.append(scope.dims)
        return 4  # one person appears in both children

    assert kanon.rescope_findings([a, b], k=K, count_users=count_users) == []
    assert calls[0] == (("lane_kind", "main"), ("team", "t"))
    assert kanon.rescope_findings([a, b], k=K) == []  # max child count (3) is a lower bound
    org = kanon.rescope_findings([a, b], k=K,
                                 count_users=lambda s: 9 if s.dims == () else 4)
    assert len(org) == 1 and org[0].scope.dims == () and org[0].n_users == 9


def test_rescope_merges_and_publishes_at_the_first_level_that_reaches_k() -> None:
    a = finding({"team": "t", "lane_kind": "main", "model": "a"}, 2, recoverable=100,
                evidence=(EvidenceItem(kind="transition", ref="rq_1", attrs=()),),
                confidence="high", validated_against="v", needs_eval=True, first_seen_ms=10)
    b = finding({"team": "t", "lane_kind": "main", "model": "b"}, 3, recoverable=None,
                evidence=(EvidenceItem(kind="transition", ref="rq_1", attrs=()),
                          EvidenceItem(kind="transition", ref="rq_2", attrs=())),
                references=("ref-b",), confidence="low",
                fix=Fix(text="f", config_patch=None, target=None, doc_url=None))
    big = finding({"team": "u", "lane_kind": "main"}, 8)
    out = kanon.rescope_findings([a, b, big], k=K, count_users=lambda s: 6)
    merged = next(f for f in out if f.scope.dims == (("lane_kind", "main"), ("team", "t")))
    assert merged.n_users == 6 and merged.n_events == 4 and merged.first_seen_ms == 10
    assert merged.cost_observed.nano == 2000 and merged.recoverable.nano == 100
    assert [e.ref for e in merged.evidence] == ["rq_1", "rq_2"]
    assert merged.references == ("ref-a", "ref-b") and merged.confidence == "low"
    assert merged.validated_against is None and merged.needs_eval and merged.fix is not None
    assert merged.finding_id == _finding_id("d.x", "ttl-expiry", merged.scope)
    assert merged.summary.startswith("[re-scoped for k-anonymity (k=5); 2 finding(s) merged]")
    assert big in out
    assert [f.recoverable.nano if f.recoverable else 0 for f in out] == sorted(
        (f.recoverable.nano if f.recoverable else 0 for f in out), reverse=True)


def test_rescope_joins_an_existing_parent_finding() -> None:
    parent = finding({"team": "t", "lane_kind": "main"}, 7, recoverable=10)
    child = finding({"team": "t", "lane_kind": "main", "model": "a"}, 1, recoverable=5)
    out = kanon.rescope_findings([parent, child], k=K, count_users=lambda s: 7)
    assert len(out) == 1 and out[0].recoverable.nano == 15 and out[0].n_users == 7


def test_rescope_escalates_team_to_cost_center_to_org() -> None:
    f = finding({"team": "t", "cost_center": "cc", "lane_kind": "main"}, 1)
    counts = {(("cost_center", "cc"), ("lane_kind", "main"), ("team", "t")): 2,
              (("cost_center", "cc"), ("team", "t")): 3, (("cost_center", "cc"),): 5}
    out = kanon.rescope_findings([f], k=K, count_users=lambda s: counts.get(s.dims, 0))
    assert [x.scope.dims for x in out] == [(("cost_center", "cc"),)]
    allowance = finding({"team": "t", "billing_class": "allowance"}, 1)
    kept = kanon.rescope_findings([allowance], k=K, count_users=lambda s: 50)
    assert kept[0].scope.dims == (("billing_class", "allowance"), ("team", "t"))


def test_person_dims_are_always_rescoped() -> None:
    f = finding({"team": "t", "principal": "p_" + "1" * 20}, 9)
    s = finding({"team": "t", "session": "s_1"}, 9, kind="runaway-session", detector="tail.runaway")
    out = kanon.rescope_findings([f, s], k=K, count_users=lambda scope: 9)
    assert {x.scope.dims for x in out} == {(("team", "t"),)}
    assert all("principal" not in dict(x.scope.dims) for x in out)


def test_exemptions_r_e1_self_and_aggregate() -> None:
    dq = finding({}, 0, kind="missing-capabilities", category="data-quality", recoverable=None)
    dq2 = finding({"team": "t"}, 1, kind="dq.something", recoverable=None)
    me = finding({"principal": "p_" + "2" * 20}, 1, audience="self")
    ws = finding({"workspace_id": "w", "model": "m"}, 0, category="aggregate",
                 kind="cache-read-share", detector="aggregate.org-scan")
    key = finding({"workspace_id": "w", "api_key_id": "h_" + "3" * 20}, 2, category="aggregate",
                  kind="cache-read-share", detector="aggregate.org-scan")
    key2 = finding({"workspace_id": "w", "api_key_id": "h_" + "4" * 20}, 1, category="aggregate",
                   kind="cache-read-share", detector="aggregate.org-scan", recoverable=1)
    busy = finding({"workspace_id": "w", "api_key_id": "h_" + "5" * 20}, 6, category="aggregate",
                   kind="cache-read-share", detector="aggregate.org-scan")
    out = kanon.rescope_findings([dq, dq2, me, ws, key, key2, busy], k=K,
                                 count_users=lambda s: 0)
    assert dq in out and dq2 in out and me in out and ws in out and busy in out
    rescoped = [f for f in out if f.scope.dims == (("workspace_id", "w"),)]
    assert len(rescoped) == 1 and rescoped[0].recoverable.nano == 101
    with pytest.raises(ContractViolation):
        kanon.rescope_findings(["x"])  # type: ignore[list-item]


def test_rescope_is_deterministic_and_keeps_big_findings() -> None:
    fs = [finding({"team": f"t{i}", "lane_kind": "main"}, i) for i in range(1, 9)]
    a = kanon.rescope_findings(fs, k=K)
    b = kanon.rescope_findings(list(reversed(fs)), k=K)
    assert a == b
    assert sum(1 for f in a if f.scope.dims != ()) == 4  # teams t5..t8 stay


# ---------- guards ----------


def test_require_self_or_aggregate() -> None:
    kanon.require_self_or_aggregate(["team", "model"], None)
    kanon.require_self_or_aggregate(["principal"], "p_" + "a" * 20)
    for bad in (["principal"], ("team", "session"), "team,session_key"):
        with pytest.raises(PrivacyError):
            kanon.require_self_or_aggregate(bad, None)


def test_merge_small_groups() -> None:
    rows = [("a", 7, 10), ("b", 2, 1), ("c", 3, 2), ("d", 9, 4)]
    out, dropped = kanon.merge_small_groups(rows, k=K)
    assert out == [("a", 7, 10), ("d", 9, 4), ("(other)", 5, 3)] and dropped == 0
    out, dropped = kanon.merge_small_groups([("a", 7, 1), ("b", 2, 1)], k=K, other_label="rest")
    assert out == [("a", 7, 1)] and dropped == 1
    usage = kanon.merge_small_groups([("x", 3, UsageBuckets(output=1)),
                                      ("y", 3, UsageBuckets(output=2))], k=K)[0]
    assert usage == [("(other)", 6, UsageBuckets(output=3))]
    tup = kanon.merge_small_groups([("x", 3, (1, 2)), ("y", 3, [3, 4]), ("z", 1, None)], k=K)
    assert tup[0] == [("(other)", 7, (4, 6))]
    maps = kanon.merge_small_groups([("x", 3, {"a": 1}), ("y", 3, {"b": Decimal("0.5")})], k=K)
    assert maps[0] == [("(other)", 6, {"a": 1, "b": Decimal("0.5")})]
    same = kanon.merge_small_groups([("x", 5, 1), ("x", 5, 2), ("(other)", 9, 3)], k=K)
    assert same == ([("x", 10, 3), ("(other)", 9, 3)], 0)
    for bad in ([("x", 3, True), ("y", 3, 1)], [("x", 3, (1,)), ("y", 3, (1, 2))],
                [("x", 3, object()), ("y", 3, object())]):
        with pytest.raises(UsageError):
            kanon.merge_small_groups(bad, k=K)
    with pytest.raises(UsageError):
        kanon.merge_small_groups([("x", -1, 1)], k=K)
    assert kanon.merge_small_groups([], k=K) == ([], 0)

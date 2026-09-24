"""SPEC §3.21 / D30: shard planning, filters, replay and finding merges, stratified sampling."""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import FlatRates
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.findings import build_finding, make_scope
from tokenbill.core.labels import Basis, Calibration, estimated, exact, sub
from tokenbill.core.policy import parse_policy
from tokenbill.core.records import Attribution, Lane, LaneKind
from tokenbill.core.shards import (
    SHARD_MAX_REQUESTS,
    finding_sort_key,
    merge_findings,
    merge_replay,
    plan_shards,
    shard_of_lanes,
    shard_where,
    stratified_sample,
)
from tokenbill.core.types import (
    Finding,
    LaneIndexRow,
    Policy,
    ReplayRequestOutcome,
    ReplayResult,
    ShardKey,
)

from .helpers import lane, req, usage

PRICER = FlatRates()


def row(key: str, team: str | None, kind: str, requests: int, nano: int = 0,
        bclass: str = "billed") -> LaneIndexRow:
    return LaneIndexRow(lane_key=key, team=team, lane_kind=kind, billing_class=bclass,
                        requests=requests, point_nano=nano)


# ---------------------------------------------------------------------------------------------
# plan_shards / shard_where / shard_of_lanes
# ---------------------------------------------------------------------------------------------


def test_team_above_the_cap_is_split_by_lane_kind() -> None:
    index = [row("a", "big", "main", 200_000), row("b", "big", "subagent", 60_000),
             row("c", "big", "subagent", 40_000), row("d", "small", "main", 150_000),
             row("e", "small", "helper", 50_000)]
    assert SHARD_MAX_REQUESTS == 250_000
    assert plan_shards(index) == [ShardKey("big", "main"), ShardKey("big", "subagent"),
                                  ShardKey("small", None)]


def test_cap_boundary_and_order() -> None:
    index = [row("z", "zeta", "main", 250_000), row("n", None, "main", 1),
             row("e", "", "helper", 1), row("a", "alpha", "main", 250_001)]
    shards = plan_shards(list(reversed(index)))
    assert shards == [ShardKey(None, None), ShardKey("alpha", "main"), ShardKey("zeta", None)]
    assert plan_shards(index, max_requests=1) == [
        ShardKey(None, "helper"), ShardKey(None, "main"), ShardKey("alpha", "main"),
        ShardKey("zeta", "main")]
    assert plan_shards([]) == []
    with pytest.raises(UsageError):
        plan_shards(index, max_requests=0)


def test_shard_where() -> None:
    assert shard_where(ShardKey("payments", None)) == {"team": "payments"}
    assert shard_where(ShardKey("payments", "main")) == {"team": "payments", "lane_kind": "main"}
    assert shard_where(ShardKey(None, None)) == {"team": ""}
    assert shard_where(ShardKey(None, LaneKind.SUBAGENT)) == {"team": "", "lane_kind": "subagent"}


def team_lane(key: str, team: str | None, kind: LaneKind = LaneKind.MAIN,
              n: int = 2, spend: int = 10_000) -> Lane:
    attr = Attribution(team=team)
    reqs = [req(key, i, 10 * i, usage(w5=spend + i, o=100), attribution=attr) for i in range(n)]
    return lane(reqs, kind=kind, lane_key=key)


FLEET = [
    team_lane("l1", "payments"), team_lane("l2", "payments", LaneKind.SUBAGENT),
    team_lane("l3", "search"), team_lane("l4", None), team_lane("l5", "", LaneKind.HELPER),
    team_lane("l6", "search", LaneKind.SUBAGENT, n=3),
]


def test_shard_of_lanes_partitions_the_fleet() -> None:
    index = [row(ln.lane_key, ln.team, ln.kind.value, len(ln.requests)) for ln in FLEET]
    for cap in (1, 2, 3, 100):
        shards = plan_shards(index, max_requests=cap)
        parts = [shard_of_lanes(FLEET, s) for s in shards]
        keys = [ln.lane_key for part in parts for ln in part]
        assert sorted(keys) == sorted(ln.lane_key for ln in FLEET)   # disjoint cover
    assert [ln.lane_key for ln in shard_of_lanes(FLEET, ShardKey(None, None))] == ["l4", "l5"]
    assert [ln.lane_key for ln in shard_of_lanes(FLEET, ShardKey("search", "subagent"))] == \
        ["l6"]


# ---------------------------------------------------------------------------------------------
# merge_replay with a trivial in-test replayer
# ---------------------------------------------------------------------------------------------


def trivial_replay(lanes: Sequence[Lane], policy: Policy, *, keep_outcomes: bool = True,
                   calibration: Calibration = Calibration.UNCALIBRATED) -> ReplayResult:
    """Saves 10% of each lane's observed cost, with a ±1-nano-per-request range."""
    base = cost = low = high = requests = 0
    per_lane: list[tuple[str, int]] = []
    outcomes: list[ReplayRequestOutcome] = []
    for ln in lanes:
        lane_base = sum(PRICER.price_inference(inf, ts_ms=r.ts_start_ms).figure.nano or 0
                        for r in ln.requests for inf in r.billable_inferences)
        lane_cost = lane_base - lane_base // 10
        base += lane_base
        cost += lane_cost
        low += lane_cost - len(ln.requests)
        high += lane_cost + len(ln.requests)
        requests += len(ln.requests)
        per_lane.append((ln.lane_key, lane_cost))
        for r in ln.requests:
            outcomes.append(ReplayRequestOutcome(
                request_id=r.request_id, usage=r.serving_inference.usage, extra=(),
                cost_nano=lane_cost // len(ln.requests), low_nano=None, high_nano=None,
                changed=True))
    baseline = exact(base, Basis.LIST, provenance=("flat",))
    policy_cost = estimated(cost, Basis.LIST, low=low, high=high, calibration=calibration,
                            note="trivial replay", provenance=("flat",))
    return ReplayResult(
        policy=policy, mode="documented", baseline=baseline, cost=policy_cost,
        saving=sub(baseline, policy_cost), per_lane=tuple(per_lane),
        outcomes=tuple(outcomes) if keep_outcomes else None,
        assumptions=("trivial: 10% saving",), calibration=calibration,
        added_calls=len(lanes), keepalive_pings=2 * len(lanes), lanes_skipped=(),
        n_lanes=len(lanes), n_requests=requests)


POLICY = parse_policy("ttl=1h")


def test_merge_of_two_halves_equals_the_whole() -> None:
    whole = trivial_replay(FLEET, POLICY)
    merged = merge_replay([trivial_replay(FLEET[:3], POLICY), trivial_replay(FLEET[3:], POLICY)])
    assert merged == whole
    assert merge_replay([whole]) == whole


@settings(max_examples=50, deadline=None)
@given(st.lists(st.integers(1, len(FLEET) - 1), unique=True, max_size=4))
def test_merge_of_any_contiguous_split_equals_the_whole(cuts: list[int]) -> None:
    bounds = [0, *sorted(cuts), len(FLEET)]
    parts = [trivial_replay(FLEET[a:b], POLICY) for a, b in zip(bounds, bounds[1:], strict=False)]
    assert merge_replay(parts) == trivial_replay(FLEET, POLICY)


def test_merge_replay_details() -> None:
    a = trivial_replay(FLEET[:2], POLICY, keep_outcomes=False)
    b = dataclasses.replace(trivial_replay(FLEET[2:4], POLICY, calibration=Calibration.CALIBRATED),
                            assumptions=("trivial: 10% saving", "other"),
                            lanes_skipped=(("l9", "keepalive not allowed for claude_code"),))
    merged = merge_replay([a, b])
    assert merged.outcomes is None
    assert merged.assumptions == ("trivial: 10% saving", "other")
    assert merged.calibration is Calibration.UNCALIBRATED
    assert merged.lanes_skipped == (("l9", "keepalive not allowed for claude_code"),)
    assert merged.n_lanes == 4 and merged.added_calls == 4 and merged.keepalive_pings == 8
    both_calibrated = merge_replay([
        trivial_replay(FLEET[:2], POLICY, calibration=Calibration.CALIBRATED),
        trivial_replay(FLEET[2:], POLICY, calibration=Calibration.CALIBRATED)])
    assert both_calibrated.calibration is Calibration.CALIBRATED
    na = dataclasses.replace(trivial_replay(FLEET[:1], POLICY), calibration=Calibration.NA)
    assert merge_replay([na]).calibration is Calibration.NA


def test_merge_replay_refusals() -> None:
    a, b = trivial_replay(FLEET[:3], POLICY), trivial_replay(FLEET[3:], POLICY)
    with pytest.raises(ContractViolation):
        merge_replay([])
    with pytest.raises(ContractViolation):
        merge_replay([a, a])                                  # overlapping lane sets
    with pytest.raises(ContractViolation):
        merge_replay([a, trivial_replay(FLEET[3:], parse_policy("ttl=5m"))])
    with pytest.raises(ContractViolation):
        merge_replay([a, dataclasses.replace(b, mode="calibrated")])
    allowance = dataclasses.replace(b, baseline=exact(1, Basis.LIST_EQUIVALENT))
    with pytest.raises(ContractViolation):
        merge_replay([a, allowance])                          # mixed billing classes
    skipped = dataclasses.replace(b, per_lane=(), lanes_skipped=(("l1", "x"),))
    with pytest.raises(ContractViolation):
        merge_replay([a, skipped])


# ---------------------------------------------------------------------------------------------
# merge_findings
# ---------------------------------------------------------------------------------------------


def finding(team: str, kind: str, rec: int | None) -> Finding:
    return build_finding(
        detector_id="cache.test", kind=kind, detector_version="1", category="breaker",
        lever_class="cache_transform", audience="org", title=f"{kind} in {team}",
        summary="test finding", scope=make_scope(team=team, lane_kind="main"), n_events=1,
        n_lanes=1, n_users=5, first_seen_ms=0, cost_observed=exact(10, Basis.LIST),
        recoverable=None if rec is None else estimated(rec, Basis.LIST, note="formula"),
        references=("test-ref",))


def test_merge_findings_concatenates_and_sorts() -> None:
    shard_a = [finding("a", "ttl-expiry", 5), finding("a", "model-switch", None)]
    shard_b = [finding("b", "ttl-expiry", 50)]
    merged = merge_findings([shard_a, shard_b])
    assert [f.recoverable.nano if f.recoverable else None for f in merged] == [50, 5, None]
    assert merged == sorted([*shard_a, *shard_b], key=finding_sort_key)
    assert merge_findings([shard_b, shard_a]) == merged      # shard order does not matter
    assert merge_findings([]) == []


def test_merge_findings_raises_on_a_duplicate_id() -> None:
    with pytest.raises(ContractViolation):
        merge_findings([[finding("a", "ttl-expiry", 5)], [finding("a", "ttl-expiry", 7)]])


# ---------------------------------------------------------------------------------------------
# stratified_sample
# ---------------------------------------------------------------------------------------------

BIG_INDEX = [row(f"lane{i:05d}", f"t{i % 7}", ["main", "subagent", "helper"][i % 3],
                 1 + i % 11, nano=(i * 7919) % 100_003,
                 bclass="allowance" if i % 5 == 0 else "billed") for i in range(3000)]


def test_whole_index_when_small() -> None:
    small = BIG_INDEX[:50]
    assert stratified_sample(small, n=50) == frozenset(r.lane_key for r in small)
    assert stratified_sample(small, n=20_000, seed=9) == frozenset(r.lane_key for r in small)
    assert stratified_sample([], n=5) == frozenset()


def test_deterministic_per_seed() -> None:
    a = stratified_sample(BIG_INDEX, n=300, seed=7)
    assert len(a) == 300
    assert stratified_sample(list(reversed(BIG_INDEX)), n=300, seed=7) == a
    assert stratified_sample(BIG_INDEX, n=300, seed=8) != a
    assert a <= {r.lane_key for r in BIG_INDEX}
    assert stratified_sample(BIG_INDEX, n=0) == frozenset()
    with pytest.raises(UsageError):
        stratified_sample(BIG_INDEX, n=-1)


def test_strata_are_represented_proportionally() -> None:
    sample = stratified_sample(BIG_INDEX, n=600, seed=1)
    by_key = {r.lane_key: r for r in BIG_INDEX}
    population = Counter((r.lane_kind, r.billing_class) for r in BIG_INDEX)
    drawn = Counter((by_key[k].lane_kind, by_key[k].billing_class) for k in sample)
    for group, size in population.items():
        assert abs(drawn[group] - size * 600 / 3000) <= 10   # 10 decile strata, ±1 each
    # the expensive decile is not left out
    top = sorted(BIG_INDEX, key=lambda r: r.point_nano)[-300:]
    assert any(r.lane_key in sample for r in top)


@settings(max_examples=40, deadline=None)
@given(st.integers(0, 400), st.integers(0, 10**6))
def test_sample_size_property(n: int, seed: int) -> None:
    index = BIG_INDEX[:300]
    sample = stratified_sample(index, n=n, seed=seed)
    assert len(sample) == min(n, len(index))


def test_enum_valued_index_rows_plan_and_sample_like_str_rows() -> None:
    """A store may fill ``LaneIndexRow.lane_kind`` with LaneKind members: shard keys, strata and
    the seeded sample must not depend on that (the rng is seeded from the plain values)."""
    enum_rows = [dataclasses.replace(r, lane_kind=LaneKind(r.lane_kind)) for r in BIG_INDEX]
    assert stratified_sample(enum_rows, n=300, seed=7) == stratified_sample(BIG_INDEX, n=300,
                                                                            seed=7)
    shards = plan_shards(enum_rows, max_requests=1)
    assert shards == plan_shards(BIG_INDEX, max_requests=1)
    assert all(type(s.lane_kind) is str for s in shards)
    assert repr(shards) == repr(plan_shards(BIG_INDEX, max_requests=1))

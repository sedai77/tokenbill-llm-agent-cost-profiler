"""SPEC §11.2 steps 1–6 with ``FakeReplayer``: exact and Monte Carlo Shapley credits, interaction
groups, full-scope scaling, shard invariance and the never-summed standalone values."""

from __future__ import annotations

import dataclasses
from fractions import Fraction

import pytest

from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind, to_json
from tokenbill.core.shards import plan_shards, stratified_sample
from tokenbill.core.testing import FakeReplayer
from tokenbill.core.types import Policy, ShardKey
from tokenbill.plan.action_plan import build_action_plan, lever_selectors, touches

from .helpers import (
    A7,
    Loader,
    a7_findings,
    a7_replayer,
    ctx,
    finding,
    index_of,
    lane,
    plan_nanos,
)


def _plan(lanes, findings, replayer, **kw):
    index = index_of(lanes)
    loader = Loader(lanes)
    shards = kw.pop("shards", None) or plan_shards(index)
    context = kw.pop("context", None) or ctx(replayer)
    plan = build_action_plan(findings, index, loader, shards, context,
                             window_days=kw.pop("window_days", 30), **kw)
    return plan, loader


def _by_id(plan, cls_basis=None):
    return {r.lever_id: r for r in plan.levers
            if cls_basis is None or r.basis is cls_basis}


# ---------------------------------------------------------------------------------------------
# the three-lever game (Appendix A.7)
# ---------------------------------------------------------------------------------------------


def test_a7_exact_credits_sum_to_the_joint() -> None:
    plan, _ = _plan([lane("l1")], a7_findings(), a7_replayer())
    got = _by_id(plan)
    assert {k: got[k].shapley.nano for k in got} == {
        "cc.fast_mode_opt_in": 8, "endpoint.global": 18, "geo.global": 4}
    assert {k: got[k].standalone.nano for k in got} == {
        "cc.fast_mode_opt_in": 10, "endpoint.global": 20, "geo.global": 5}
    assert plan.joint_saving.nano == 30 == sum(r.shapley.nano for r in plan.levers)
    assert plan.method == "shapley-exact" and plan.shapley_se == ()
    assert plan.groups == (("billed:g1", ("cc.fast_mode_opt_in", "endpoint.global",
                                          "geo.global")),)
    assert {r.group for r in plan.levers} == {"billed:g1"}
    assert {r.params for r in plan.levers} == {"fast=off", "regional=global", "geo=global"}
    # rate levers: RR 1/1/1, 30-day window → the projections are the credits
    assert plan.headline_monthly.nano == 30
    assert (plan.headline_monthly.low_nano, plan.headline_monthly.high_nano) == (30, 30)
    assert plan.headline_monthly.evidence is Evidence.ESTIMATED
    assert plan.sample.startswith("shapley on 1/1 lanes (seed 0)")
    for r in plan.levers:
        assert r.shapley.evidence is Evidence.ESTIMATED and r.basis is Basis.LIST
        assert "never summed" in r.standalone.note
    assert plan.allowance_headroom_monthly is None and plan.pool_headroom_monthly is None


def test_standalone_sums_never_appear_in_any_plan_field() -> None:
    plan, _ = _plan([lane("l1")], a7_findings(), a7_replayer())
    standalone_sum = sum(r.standalone.nano for r in plan.levers)
    assert standalone_sum == 35 != plan.joint_saving.nano
    assert standalone_sum not in plan_nanos(plan)
    monthly = [r.projected_monthly.nano for r in plan.levers]
    assert plan.headline_monthly.nano == sum(monthly)


def test_weighted_game_on_a_larger_scope_is_scaled_to_the_full_scope_joint() -> None:
    weights = {f"l{i:02d}": w for i, w in enumerate([3, 7, 11, 2, 5, 13, 1, 17, 4, 9, 6, 8])}
    lanes = [lane(k) for k in weights]
    index = index_of(lanes)
    sample = stratified_sample(index, n=5, seed=11)
    w_sample = sum(weights[k] for k in sample)
    w_full = sum(weights.values())
    plan, loader = _plan(lanes, a7_findings(), a7_replayer(lambda ln: weights[ln.lane_key]),
                         sample_lanes=5, seed=11)
    got = _by_id(plan)
    joint = plan.joint_saving.nano
    assert joint == 30 * w_full
    assert sum(r.shapley.nano for r in plan.levers) == joint          # Σφ to the nano
    exact = {"cc.fast_mode_opt_in": Fraction(8, 30), "endpoint.global": Fraction(35, 60),
             "geo.global": Fraction(9, 60)}
    for lid, share in exact.items():
        assert abs(got[lid].shapley.nano - share * joint) <= 1        # proportions kept
    # standalone values: sample value × the same factor, labeled sample-scaled
    factor = Fraction(joint, 30 * w_sample)
    for lid, base in (("cc.fast_mode_opt_in", 10), ("endpoint.global", 20), ("geo.global", 5)):
        assert abs(got[lid].standalone.nano - base * w_sample * factor) <= 1
        assert "sample-scaled" in got[lid].standalone.note
    assert plan.sample == "shapley on 5/12 lanes (seed 11), scaled to full-scope joint replay"
    assert any(kind == "shard" for kind, _ in loader.calls)
    assert sum(r.standalone.nano for r in plan.levers) not in plan_nanos(plan)


def _ttl_replayer(weights: dict[str, int]) -> FakeReplayer:
    """Saves the lane's weight when a ``ttl=1h`` clause of the policy selects the lane."""

    def fn(ln, pol: Policy) -> int:
        hit = any(ttl == "1h" and touches((sel,), ln) for sel, ttl in pol.ttl)
        return weights.get(ln.lane_key, 0) if hit else 0

    return FakeReplayer.from_function(fn)


def test_disjoint_groups_add() -> None:
    lanes = [lane("m1"), lane("m2"), lane("a1", kind=LaneKind.API_RUN, product="agent_sdk")]
    weights = {"m1": 40, "m2": 60, "a1": 25}
    plan, _ = _plan(lanes, [], _ttl_replayer(weights))
    got = _by_id(plan)
    assert set(got) == {"cc.prompt_cache_ttl.main", "sdk.ttl"}
    assert got["cc.prompt_cache_ttl.main"].shapley.nano == 100 == \
        got["cc.prompt_cache_ttl.main"].standalone.nano
    assert got["sdk.ttl"].shapley.nano == 25 == got["sdk.ttl"].standalone.nano
    assert plan.joint_saving.nano == 125
    assert [g for g, _ in plan.groups] == ["billed:g1", "billed:g2"]
    assert got["cc.prompt_cache_ttl.main"].group != got["sdk.ttl"].group
    assert got["cc.prompt_cache_ttl.main"].params == \
        "ttl=1h@agent_product:claude_code,lane_kind:main"


def test_groups_keep_their_sample_proportions_when_scaled() -> None:
    weights = {}
    lanes = []
    for i in range(10):
        lanes.append(lane(f"m{i}"))
        weights[f"m{i}"] = 10 + i
        lanes.append(lane(f"a{i}", kind=LaneKind.API_RUN, product="agent_sdk"))
        weights[f"a{i}"] = 3 + 2 * i
    index = index_of(lanes)
    sample = stratified_sample(index, n=12, seed=3)
    main_s = sum(weights[k] for k in sample if k.startswith("m"))
    api_s = sum(weights[k] for k in sample if k.startswith("a"))
    assert main_s and api_s and len(sample) == 12
    plan, _ = _plan(lanes, [], _ttl_replayer(weights), sample_lanes=12, seed=3)
    got = _by_id(plan)
    joint = plan.joint_saving.nano
    assert joint == sum(weights.values())
    main, api = got["cc.prompt_cache_ttl.main"].shapley.nano, got["sdk.ttl"].shapley.nano
    assert main + api == joint
    assert abs(main - Fraction(main_s, main_s + api_s) * joint) <= 1


def _mc_value(pol: Policy) -> int:
    n = sum([pol.fast_off, pol.geo_global, pol.regional_to_global, pol.batch is not None,
             len(pol.repairs)])
    return 100 * n - 3 * n * n + (7 if pol.fast_off and pol.batch else 0)


MC_LEVERS = ("endpoint.global", "batch.eligible", "fanout.stagger", "retry.single_owner",
             "fallback.credit")


def test_seven_interacting_levers_use_monte_carlo_with_standard_errors() -> None:
    findings = [finding("x", [lid]) for lid in MC_LEVERS]
    replayer = FakeReplayer.from_function(lambda ln, pol: _mc_value(pol))
    plan, _ = _plan([lane("l1")], findings, replayer, seed=5)
    assert len(plan.levers) == 7 and len(plan.groups) == 1
    assert plan.method == "shapley-mc"
    assert {lid for lid, _ in plan.shapley_se} == {r.lever_id for r in plan.levers}
    assert any(se > 0 for _, se in plan.shapley_se)
    everything = Policy(name="x", fast_off=True, geo_global=True, regional_to_global=True,
                        batch="eligible", repairs=("fallback_credit", "retry_backoff_cap",
                                                   "stagger_fanout"))
    assert plan.joint_saving.nano == _mc_value(everything)
    assert sum(r.shapley.nano for r in plan.levers) == plan.joint_saving.nano
    assert all("Monte Carlo" in r.shapley.note for r in plan.levers)
    again, _ = _plan([lane("l1")], findings, replayer, seed=5)
    assert again == plan


def test_six_levers_stay_exact() -> None:
    findings = [finding("x", [lid]) for lid in MC_LEVERS[:4]]
    replayer = FakeReplayer.from_function(lambda ln, pol: _mc_value(pol))
    plan, _ = _plan([lane("l1")], findings, replayer)
    assert len(plan.levers) == 6 and plan.method == "shapley-exact"


def _fleet(n_per_kind: int = 6) -> list:
    lanes = []
    for i in range(n_per_kind):
        lanes.append(lane(f"m{i}", team="core"))
        lanes.append(lane(f"s{i}", team="core", kind=LaneKind.SUBAGENT))
        lanes.append(lane(f"w{i}", team="core", kind=LaneKind.WORKFLOW_AGENT))
        lanes.append(lane(f"a{i}", team="core", kind=LaneKind.API_RUN, product="agent_sdk"))
    return lanes


def _fleet_replayer() -> FakeReplayer:
    def fn(ln, pol: Policy) -> int:
        seed = sum(map(ord, ln.lane_key))
        total = 0
        if any(ttl == "1h" and touches((sel,), ln) for sel, ttl in pol.ttl):
            total += 1_000 + seed
        total += A7[flags_of(pol)] * (seed % 7 + 1)
        return total

    return FakeReplayer.from_function(fn)


def flags_of(pol: Policy) -> str:
    return ("A" if pol.fast_off else "") + ("B" if pol.regional_to_global else "") + (
        "C" if pol.geo_global else "")


def test_one_shard_and_four_shards_give_identical_plans() -> None:
    lanes = _fleet()
    index = index_of(lanes)
    one = plan_shards(index)
    four = plan_shards(index, max_requests=1)
    assert len(one) == 1 and len(four) == 4
    kw = dict(sample_lanes=9, seed=2)
    p1, l1 = _plan(lanes, a7_findings("core"), _fleet_replayer(), shards=one, **kw)
    p4, l4 = _plan(lanes, a7_findings("core"), _fleet_replayer(), shards=four, **kw)
    assert p1 == p4
    assert to_json(p1) == to_json(p4)
    assert sum(1 for k, _ in l4.calls if k == "shard") == 4
    assert sum(1 for k, _ in l1.calls if k == "shard") == 1
    assert sum(r.shapley.nano for r in p1.levers if ":" in r.group) == p1.joint_saving.nano


def test_parallel_map_shards_is_used_and_order_does_not_matter() -> None:
    lanes = _fleet()
    index = index_of(lanes)
    shards = plan_shards(index, max_requests=1)
    used = []

    def reversed_mapper(fn, keys):
        used.append(len(keys))
        return list(reversed([fn(k) for k in reversed(keys)]))[::-1]

    base, _ = _plan(lanes, a7_findings("core"), _fleet_replayer(), shards=shards,
                    sample_lanes=9, seed=2)
    mapped, _ = _plan(lanes, a7_findings("core"), _fleet_replayer(), shards=shards,
                      sample_lanes=9, seed=2, map_shards=reversed_mapper)
    assert used == [4] and mapped == base


def test_lever_selectors_follow_keyed_clauses() -> None:
    from tokenbill.core.catalog import lever
    from tokenbill.core.policy import parse_policy

    sub = lever("cc.prompt_cache_ttl.subagent")
    assert lever_selectors(sub, parse_policy(sub.grid[1])) == (
        "agent_product:claude_code,lane_kind:subagent",
        "agent_product:claude_code,lane_kind:workflow_agent")
    fast = lever("cc.fast_mode_opt_in")
    assert lever_selectors(fast, parse_policy("fast=off")) == ("all",)
    ka = lever("sdk.keepalive")
    assert lever_selectors(ka, parse_policy(ka.grid[0])) == ("agent_product:agent_sdk",)
    wf = lane("w", kind=LaneKind.WORKFLOW_AGENT)
    assert touches(lever_selectors(sub, parse_policy(sub.grid[1])), wf)
    assert not touches(("lane_kind:api_run",), wf)


def test_a_lever_acts_only_on_the_lanes_it_is_delivered_to() -> None:
    seen: list[tuple[str, str]] = []

    def fn(ln, pol: Policy) -> int:
        seen.append((ln.lane_key, pol.spec()))
        return 50 if pol.fast_off else 0

    lanes = [lane("m1"), lane("a1", kind=LaneKind.API_RUN, product="agent_sdk")]
    plan, _ = _plan(lanes, [], FakeReplayer.from_function(fn))
    # the TTL main lever's replays never include the api_run lane
    assert not [k for k, spec in seen if k == "a1" and spec.startswith("ttl=") and
                "lane_kind:main" in spec]
    assert _by_id(plan)["cc.fast_mode_opt_in"].shapley.nano == 100


def test_plan_objects_encode_without_floats() -> None:
    plan, _ = _plan([lane("l1")], a7_findings(), a7_replayer())
    doc = to_json(plan)

    def walk(x):
        assert not isinstance(x, float)
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(doc)
    assert dataclasses.replace(plan) == plan


def test_shards_argument_is_used_only_for_the_full_scope() -> None:
    lanes = [lane(f"l{i}") for i in range(4)]
    plan, loader = _plan(lanes, a7_findings(), a7_replayer(), shards=[ShardKey("t1", None)])
    assert plan.sample.endswith("the sample is the full scope (no scaling)")
    assert not [c for c in loader.calls if c[0] == "shard"]
    assert plan.joint_saving.nano == 4 * A7["ABC"]


@pytest.mark.parametrize("bad", [
    dict(window_days=0), dict(window_days=True), dict(sample_lanes=0), dict(seed="1"),
])
def test_invalid_arguments_raise_usage_error(bad) -> None:
    from tokenbill.core.errors import UsageError

    kw = dict(window_days=30)
    kw.update(bad)
    with pytest.raises(UsageError):
        build_action_plan([], index_of([lane("l1")]), Loader([lane("l1")]), [], ctx(None), **kw)


def test_invalid_inputs_raise_usage_error() -> None:
    from tokenbill.core.errors import UsageError

    lanes = [lane("l1")]
    with pytest.raises(UsageError):
        build_action_plan([], ["row"], Loader(lanes), [], ctx(None), window_days=30)
    with pytest.raises(UsageError):
        build_action_plan([], index_of(lanes), "loader", [], ctx(None), window_days=30)
    with pytest.raises(UsageError):
        build_action_plan([], index_of(lanes), Loader(lanes), [], object(), window_days=30)
    with pytest.raises(UsageError):
        build_action_plan(["f"], index_of(lanes), Loader(lanes), [], ctx(a7_replayer()),
                          window_days=30)

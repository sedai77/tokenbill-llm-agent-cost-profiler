"""Hypothesis properties: Σφ equals the full-scope joint for any fleet, sample and shard split;
merge and rollback patches round-trip any current settings; ``check_effect`` never raises on
well-typed lanes."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core.records import LaneKind, UsageBuckets
from tokenbill.core.shards import plan_shards
from tokenbill.core.testing import FakeReplayer
from tokenbill.core.types import Policy
from tokenbill.plan.action_plan import build_action_plan, touches
from tokenbill.plan.effectiveness import CHECKS, check_effect
from tokenbill.plan.policy_pack import apply_merge_patch, build_policy_packs

from .helpers import T0, Loader, a7_findings, ctx, index_of, lane

KINDS = (LaneKind.MAIN, LaneKind.SUBAGENT, LaneKind.API_RUN, LaneKind.WORKFLOW_AGENT)
PATHS = ("api_key", "subscription", "copilot_pool")


def _value(ln, pol: Policy) -> int:
    seed = sum(map(ord, ln.lane_key)) % 97 + 1
    total = 0
    if any(ttl == "1h" and touches((sel,), ln) for sel, ttl in pol.ttl):
        total += 50 * seed
    players = [pol.fast_off, pol.regional_to_global, pol.geo_global]
    n = sum(players)
    total += (40 * n - 6 * n * n + (5 if pol.fast_off and pol.geo_global else 0)) * seed
    return total


@st.composite
def fleets(draw):
    n = draw(st.integers(min_value=1, max_value=14))
    lanes = []
    for i in range(n):
        kind = draw(st.sampled_from(KINDS))
        team = draw(st.sampled_from(["a", "b", None]))
        path = draw(st.sampled_from(PATHS))
        product = "agent_sdk" if kind is LaneKind.API_RUN else "claude_code"
        lanes.append(lane(f"l{i}", team=team, kind=kind, billing_path=path, product=product,
                          n=draw(st.integers(min_value=1, max_value=3))))
    return lanes


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(fleets(), st.integers(min_value=1, max_value=16), st.integers(min_value=0, max_value=5))
def test_credits_sum_to_the_joint_for_any_sample_and_shard_split(lanes, sample, seed) -> None:
    index = index_of(lanes)
    replayer = FakeReplayer.from_function(_value)
    plans = []
    for shards in (plan_shards(index), plan_shards(index, max_requests=1)):
        plans.append(build_action_plan(a7_findings(), index, Loader(lanes), shards,
                                       ctx(replayer), window_days=14, sample_lanes=sample,
                                       seed=seed))
    assert plans[0] == plans[1]
    plan = plans[0]
    for cls, basis_is in (("billed", lambda r: r.group.startswith("billed:")),):
        joint = [r for r in plan.levers if basis_is(r)]
        if joint:
            assert sum(r.shapley.nano for r in joint) == plan.joint_saving.nano, cls
    for prefix, head in (("billed:", plan.headline_monthly),
                         ("allowance:", plan.allowance_headroom_monthly),
                         ("pool:", plan.pool_headroom_monthly)):
        members = [r for r in plan.levers if r.group.startswith(prefix)]
        if head is not None:
            assert head.nano == sum(r.projected_monthly.nano for r in members)
            assert head.low_nano == sum(r.projected_monthly.low_nano for r in members) or \
                not members
    for r in plan.levers:
        if r.shapley.nano is not None and r.projected_monthly.nano is not None:
            p = r.projected_monthly
            assert p.low_nano <= p.nano <= p.high_nano


_JSON_SCALARS = st.one_of(st.booleans(), st.integers(min_value=-10**6, max_value=10**6),
                          st.text(alphabet="abcxyz-_ .", max_size=8),
                          st.decimals(min_value=-1000, max_value=1000, places=3,
                                      allow_nan=False, allow_infinity=False))
_JSON = st.recursive(_JSON_SCALARS, lambda inner: st.one_of(
    st.lists(inner, max_size=3),
    st.dictionaries(st.text(alphabet="abcdef", min_size=1, max_size=4), inner, max_size=3)),
    max_leaves=10)
_CURRENT = st.fixed_dictionaries({}, optional={
    "promptCacheTtl": st.sampled_from(["5m", "1h"]),
    "env": st.dictionaries(st.sampled_from(["FOO", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                                            "ENABLE_TOOL_SEARCH"]),
                           st.text(alphabet="0123456789", min_size=1, max_size=6), max_size=3),
    "hooks": st.fixed_dictionaries({}, optional={"SessionStart": st.just([
        {"matcher": "startup", "hooks": [{"type": "command", "command": "true"}]}])}),
    "availableModels": st.just(["claude-opus-5-5"]),
    "other": _JSON,
})


@settings(max_examples=80, deadline=None)
@given(_CURRENT, st.booleans(), st.sets(st.sampled_from(
    ["ttl", "compact", "hook", "fast", "model", "tool"])))
def test_merge_and_rollback_patches_round_trip(current, include, chosen) -> None:
    import json

    from .test_policy_pack import COMPACT, FAST, HOOK, MODEL, TTL, lever_result, plan_of

    table = {"ttl": TTL, "compact": COMPACT, "hook": HOOK, "fast": FAST, "model": MODEL,
             "tool": lever_result("cc.tool_search", "", group="projection-only")}
    plan = plan_of(*(table[c] for c in sorted(chosen)))
    packs = build_policy_packs(plan, [], target="claude-code", current=current, cohort_by=None,
                               include_tradeoffs=include, contract=None)
    for pack in packs:
        patch = json.loads(pack.merge_patch_json, parse_float=Decimal)
        rollback = json.loads(pack.rollback_patch_json, parse_float=Decimal)
        patched = apply_merge_patch(current, patch)
        assert apply_merge_patch(patched, rollback) == current
        for key in set(current) - set(patch):
            assert patched[key] == current[key]


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 500), st.integers(0, 500),
                          st.sampled_from([None, "low", "medium", "high", "max", "weird"]),
                          st.sampled_from(["claude-sonnet-5", "claude-opus-5-5"])),
                min_size=0, max_size=6),
       st.sampled_from(CHECKS), st.integers(1, 30))
def test_check_effect_never_raises_on_valid_arguments(rows, lever_id, days) -> None:
    lanes = [lane(f"x{i}", usage=UsageBuckets(cache_write_1h=w1, cache_write_5m=w5, output=1),
                  effort=effort, model=model)
             for i, (w1, w5, effort, model) in enumerate(rows)]
    target = {"cc.autocompact_window": "400000", "cc.default_effort": "medium",
              "cc.max_effort": "high"}.get(lever_id)
    got = check_effect(lanes, lever_id=lever_id, cohort="all", since_ms=T0, days=days,
                       target=target)
    assert got is None or got.kind == "setting-not-effective"

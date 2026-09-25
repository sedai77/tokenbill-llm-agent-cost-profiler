"""Gate (merge gate 1): the action plan with REPLAY's real ``UsageReplayer`` on the SPEC Appendix A
lanes — the TTL lever's Shapley credit on A.1 equals its standalone saving ($1.1508); A.2 keeps
5m; A.2b moves to 5m ($0.318); A.6's negative compaction window is never chosen; sharded and
unsharded plans agree; a policy pack built from the plan is canary-free."""

from __future__ import annotations

import pytest

from tokenbill.core.builders import assert_no_canary
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind, to_json
from tokenbill.core.shards import plan_shards
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import AnalysisContext
from tokenbill.plan.action_plan import build_action_plan
from tokenbill.plan.policy_pack import build_policy_packs

from .helpers import A1_ROWS, A2_ROWS, A2B_ROWS, A6_ROWS, DAY_MS, MAIN_SEL, T0, Loader, table_lane

pytestmark = pytest.mark.gate
usage_replay = pytest.importorskip("tokenbill.sim.usage_replay")

PRICER = FakePricer()


def _ctx(**kw) -> AnalysisContext:
    return AnalysisContext(pricer=PRICER, rules=RulesTable(),
                           replayer=usage_replay.UsageReplayer(), calibration=None,
                           window=(T0, T0 + 30 * DAY_MS),
                           capabilities=frozenset({"usage_sequence", "timing", "ttl_split"}),
                           now_ms=T0 + 30 * DAY_MS, **kw)


def _index(lanes):
    from tokenbill.core.types import LaneIndexRow

    rows = []
    for ln in lanes:
        point = 0
        for r in ln.requests:
            for att in r.attempts:
                for inf in att.inferences:
                    point += PRICER.price_inference(inf, ts_ms=att.ts_start_ms).figure.nano or 0
        rows.append(LaneIndexRow(ln.lane_key, ln.team, ln.kind.value, ln.billing_class,
                                 len(ln.requests), point))
    return rows


def _plan(lanes, shards=None, **kw):
    index = _index(lanes)
    context = kw.pop("context", None) or _ctx()
    return build_action_plan([], index, Loader(lanes), shards or plan_shards(index), context,
                             window_days=30, **kw)


def _ttl(plan, basis=Basis.LIST):
    got = [r for r in plan.levers if r.lever_id == "cc.prompt_cache_ttl.main"
           and r.basis is basis]
    return got[0] if got else None


def test_a1_ttl_lever_shapley_equals_its_standalone_saving() -> None:
    plan = _plan([table_lane("A1", A1_ROWS)])
    ttl = _ttl(plan)
    assert ttl is not None and ttl.params == f"ttl=1h@{MAIN_SEL}"
    assert ttl.shapley.nano == ttl.standalone.nano == 1_150_800_000        # $1.1508
    assert plan.joint_saving.nano == 1_150_800_000
    assert ttl.shapley.evidence is Evidence.ESTIMATED
    assert ttl.projected_monthly.nano == 1_035_720_000                     # × 0.9
    assert plan.headline_monthly.nano == 1_035_720_000
    assert [r.lever_id for r in plan.levers] == ["cc.prompt_cache_ttl.main"]


def test_a2_keeps_5m_and_a2b_moves_to_5m() -> None:
    assert _ttl(_plan([table_lane("A2", A2_ROWS)])) is None
    ttl = _ttl(_plan([table_lane("A2b", A2B_ROWS)]))
    assert ttl is not None and ttl.params == f"ttl=5m@{MAIN_SEL}"
    assert ttl.shapley.nano == 318_000_000                                  # $0.318


def test_allowance_a1_lane_is_headroom_only() -> None:
    plan = _plan([table_lane("A1s", A1_ROWS, billing_path="subscription")])
    ttl = _ttl(plan, Basis.LIST_EQUIVALENT)
    assert ttl is not None and ttl.shapley.nano == 1_150_800_000
    assert plan.headline_monthly.nano == 0
    assert plan.allowance_headroom_monthly is not None
    assert plan.allowance_headroom_monthly.nano == 1_035_720_000


def test_a6_negative_compaction_window_is_never_chosen() -> None:
    lanes = [table_lane("A6", A6_ROWS, model="claude-sonnet-5")]
    plan = _plan(lanes, include_tradeoffs=True, context=_ctx(thresholds={
        "context.compaction-window.post_tokens": "20000",
        "context.compaction-window.min_compaction_window": "200000"}))
    assert all(r.lever_id != "cc.autocompact_window" for r in plan.levers)


def test_sharded_and_unsharded_plans_agree_with_the_real_replayer() -> None:
    lanes = []
    for i in range(3):
        lanes.append(table_lane(f"a1-{i}", A1_ROWS, team="payments", ts=T0 + i * DAY_MS))
        lanes.append(table_lane(f"a2b-{i}", A2B_ROWS, team="payments", ts=T0 + i * DAY_MS))
        lanes.append(table_lane(f"sub-{i}", A1_ROWS, team="payments", kind=LaneKind.SUBAGENT,
                                ts=T0 + i * DAY_MS))
        lanes.append(table_lane(f"api-{i}", A1_ROWS, team="payments", kind=LaneKind.API_RUN,
                                product="agent_sdk", ts=T0 + i * DAY_MS))
    index = _index(lanes)
    one, many = plan_shards(index), plan_shards(index, max_requests=1)
    assert len(one) == 1 and len(many) == 3
    a = _plan(lanes, one, sample_lanes=5, seed=4)
    b = _plan(lanes, many, sample_lanes=5, seed=4)
    assert a == b
    assert sum(r.shapley.nano for r in a.levers if ":" in r.group) == a.joint_saving.nano
    packs = build_policy_packs(a, [], target="claude-code", current=None, cohort_by=None,
                               include_tradeoffs=False, contract=None)
    assert packs and all(p.merge_patch_json for p in packs)
    for pack in packs:
        assert_no_canary(repr(pack))
    assert_no_canary(str(to_json(a)))

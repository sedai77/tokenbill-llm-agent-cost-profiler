"""Gate (merge gate 1): on ``synth.fleet.generate()`` the payments team's TTL lever
(``cc.prompt_cache_ttl.main``) gets a Shapley credit within ±5% of ``FleetTruth`` (the plant
``payments.ttl-1h``), with REPLAY's real ``UsageReplayer``; on several teams at once the credits
sum to the full-scope joint saving and sharded plans equal unsharded ones."""

from __future__ import annotations

from typing import Any

import pytest

from tokenbill.core.builders import assert_no_canary
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.labels import Basis
from tokenbill.core.records import to_json
from tokenbill.core.shards import plan_shards, shard_of_lanes
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import AnalysisContext, LaneIndexRow
from tokenbill.plan.action_plan import build_action_plan
from tokenbill.plan.policy_pack import build_policy_packs

pytestmark = pytest.mark.gate
fleet = pytest.importorskip("tokenbill.synth.fleet")
usage_replay = pytest.importorskip("tokenbill.sim.usage_replay")

PRICER = FakePricer()


@pytest.fixture(scope="module")
def world() -> Any:
    return fleet.generate()


def _index(lanes) -> list[LaneIndexRow]:
    rows = []
    for ln in lanes:
        point = 0
        for r in ln.requests:
            for att in r.attempts:
                for inf in att.inferences:
                    if inf.billable is False:
                        continue
                    point += PRICER.price_inference(inf, ts_ms=att.ts_start_ms).figure.nano or 0
        rows.append(LaneIndexRow(ln.lane_key, ln.team, ln.kind.value, ln.billing_class,
                                 len(ln.requests), point))
    return rows


def _plan(world, lanes, *, shards=None, **kw):
    index = _index(lanes)

    def load(keys, shard):
        if shard is None:
            wanted = set(keys or ())
            return [ln for ln in lanes if ln.lane_key in wanted]
        return shard_of_lanes(lanes, shard)

    ctx = AnalysisContext(pricer=PRICER, rules=RulesTable(),
                          replayer=usage_replay.UsageReplayer(), calibration=None,
                          window=(world.since_ms, world.until_ms),
                          capabilities=frozenset({"usage_sequence", "timing", "ttl_split"}),
                          now_ms=world.until_ms)
    return build_action_plan([], index, load, shards or plan_shards(index), ctx,
                             window_days=world.days, **kw)


def test_payments_ttl_lever_credit_matches_the_truth(world) -> None:
    plant = world.truth.plant("payments.ttl-1h")
    assert plant.detail("lever") == "cc.prompt_cache_ttl.main"
    plan = _plan(world, world.lanes("payments"))
    got = [r for r in plan.levers if r.lever_id == "cc.prompt_cache_ttl.main"
           and r.basis is Basis.LIST]
    assert len(got) == 1
    lever = got[0]
    assert lever.params == "ttl=1h@agent_product:claude_code,lane_kind:main"
    assert lever.shapley.nano is not None
    assert plant.within("shapley", lever.shapley.nano), (lever.shapley.nano,
                                                         plant.expected("shapley"))
    assert lever.shapley.nano == lever.standalone.nano       # no other lever touches them
    assert_no_canary(str(to_json(plan)))


def test_teams_together_credits_sum_to_the_joint_and_shards_agree(world) -> None:
    lanes = [ln for team in ("payments", "core", "tiny") for ln in world.lanes(team)]
    index = _index(lanes)
    kw = dict(sample_lanes=max(2, len(lanes) // 3), seed=7)
    a = _plan(world, lanes, shards=plan_shards(index), **kw)
    b = _plan(world, lanes, shards=plan_shards(index, max_requests=1), **kw)
    assert a == b
    joint = [r for r in a.levers if r.group.startswith("billed:")]
    if a.joint_saving.nano is not None and joint:
        assert sum(r.shapley.nano for r in joint) == a.joint_saving.nano
    packs = build_policy_packs(a, [], target="claude-code", current=None, cohort_by=None,
                               include_tradeoffs=False, contract=None)
    for pack in packs:
        assert_no_canary(repr(pack))

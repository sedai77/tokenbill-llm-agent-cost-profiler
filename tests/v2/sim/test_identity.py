"""Identity invariant, conformance and minimal change (SPEC §9.1 #1–#2, §3.18)."""

from __future__ import annotations

import json

import pytest

from tokenbill.core.builders import FlatRates
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Evidence, add, zero
from tokenbill.core.records import LaneKind, UsageSource, to_json
from tokenbill.core.testing import FakePricer, assert_replayer_conforms
from tokenbill.core.types import Policy
from tokenbill.sim.usage_replay import UsageReplayer

from .helpers import (
    PRICER,
    RULES,
    a1_lane,
    a2_lane,
    outcomes,
    priced_request,
    random_lanes,
    replay,
    same,
    table,
)


def test_conforms_with_fake_pricer() -> None:
    got = assert_replayer_conforms(UsageReplayer(), FakePricer())
    assert got["ttl_saving_nano"] > 0


def test_conforms_with_flat_rates_and_default_rules() -> None:
    assert_replayer_conforms(UsageReplayer(), FlatRates(), rules=RULES)


def _ledger(lanes: list, pricer=PRICER) -> object:
    basis = Basis.LIST
    total = zero(basis)
    for lane in lanes:
        for req in lane.requests:
            total = add(total, priced_request(req, pricer, basis))
    return total


@pytest.mark.parametrize("seed", range(4))
def test_observed_policy_is_the_identity_on_random_lanes(seed: int) -> None:
    lanes = random_lanes(seed, 50)
    kinds = {inf.usage_source for lane in lanes for r in lane.requests for a in r.attempts
             for inf in a.inferences}
    assert UsageSource.MESSAGE_START_ONLY in kinds
    assert any(inf.usage.cache_write_unknown for lane in lanes for r in lane.requests
               for a in r.attempts for inf in a.inferences)
    res = replay(lanes, Policy.observed())
    expected = _ledger(lanes)
    for attr in ("nano", "low_nano", "high_nano"):
        assert getattr(res.baseline, attr) == getattr(expected, attr)
        assert getattr(res.cost, attr) == getattr(res.baseline, attr)
    assert same(res.cost, res.baseline)
    assert res.saving.nano == 0 and res.saving.low_nano is None
    assert res.outcomes is not None and len(res.outcomes) == res.n_requests
    assert not any(o.changed for o in res.outcomes)
    by_id = {r.request_id: r for lane in lanes for r in lane.requests}
    for o in res.outcomes:
        fig = priced_request(by_id[o.request_id])
        assert (o.cost_nano, o.low_nano, o.high_nano) == (fig.nano, fig.low_nano, fig.high_nano)
    assert res.n_lanes == 50 and res.added_calls == 0 and res.keepalive_pings == 0


def test_baseline_evidence_follows_the_lines() -> None:
    exact = replay(a1_lane(), Policy.observed())
    assert exact.baseline.evidence is Evidence.EXACT and exact.baseline.low_nano is None
    res = replay(random_lanes(7, 30, allow_unpriced=False), Policy.observed())
    assert res.baseline.evidence is Evidence.ESTIMATED
    assert res.baseline.low_nano <= res.baseline.nano <= res.baseline.high_nano


def test_unpriced_lane_makes_totals_unpriced_but_not_the_saving() -> None:
    priced = a1_lane(lane_key="a")
    unknown = table([(0, 0, 50_000, 0, 0, 100), (30, 50_000, 1_000, 0, 0, 100)],
                    model="claude-not-a-model", lane_key="b")
    res = replay([priced, unknown], "ttl=1h")
    assert res.baseline.nano is None and res.baseline.note.startswith("unpriced:")
    assert res.cost.nano is None
    assert res.saving.nano is not None
    assert "excludes unpriced changed requests" in res.saving.note
    assert any("unpriced changed requests" in a for a in res.assumptions)
    assert "b" not in dict(res.per_lane) and "a" in dict(res.per_lane)   # int field: omitted
    only_unknown = replay([unknown], "ttl=1h")
    assert only_unknown.saving.nano == 0
    assert only_unknown.saving.evidence is Evidence.ESTIMATED


def test_minimal_change_under_a_scoped_ttl() -> None:
    main = a1_lane(lane_key="main")
    sub = a1_lane(lane_key="sub", kind=LaneKind.SUBAGENT)
    res = replay([main, sub], "ttl=1h@lane_kind:main")
    o = outcomes(res)
    for req in sub.requests:
        assert not o[req.request_id].changed
        assert o[req.request_id].cost_nano == priced_request(req).nano
    assert all(o[r.request_id].changed for r in main.requests)
    assert dict(res.per_lane)["sub"] == sum(priced_request(r).nano for r in sub.requests)


def test_saving_is_the_sum_of_per_request_savings() -> None:
    lanes = [a1_lane(lane_key="x"), a2_lane(lane_key="y")]
    res = replay(lanes, "ttl=1h")
    o = outcomes(res)
    by_id = {r.request_id: r for lane in lanes for r in lane.requests}
    total = sum(priced_request(by_id[rid]).nano - x.cost_nano for rid, x in o.items()
                if x.changed)
    assert res.saving.nano == total
    assert res.cost.nano == sum(x.cost_nano for x in o.values())
    assert dict(res.per_lane) == {
        "x": sum(o[r.request_id].cost_nano for r in lanes[0].requests),
        "y": sum(o[r.request_id].cost_nano for r in lanes[1].requests)}


def test_replay_is_deterministic_and_order_independent() -> None:
    lanes = random_lanes(11, 30)
    a = replay(lanes, "ttl=1h;fast=off;repair=fallback_credit")
    b = replay(list(reversed(lanes)), "ttl=1h;fast=off;repair=fallback_credit")
    assert json.dumps(to_json(a), sort_keys=True) == json.dumps(to_json(b), sort_keys=True)
    assert [k for k, _ in a.per_lane] == sorted(k for k, _ in a.per_lane)


def test_outcomes_are_dropped_unless_kept() -> None:
    res = replay(a1_lane(), "ttl=1h", keep=False)
    assert res.outcomes is None and res.saving.nano > 0


def test_mixed_billing_classes_raise() -> None:
    billed = a1_lane(lane_key="billed")
    allowance = a1_lane(lane_key="seat", billing_path="subscription")
    with pytest.raises(UsageError):
        replay([billed, allowance], "ttl=1h")


def test_allowance_lanes_replay_on_list_equivalent() -> None:
    lane = a1_lane(billing_path="subscription")
    res = replay(lane, "ttl=1h")
    assert res.baseline.basis is Basis.LIST_EQUIVALENT
    assert res.cost.basis is Basis.LIST_EQUIVALENT and res.saving.basis is Basis.LIST_EQUIVALENT
    assert not res.baseline.is_billed_eligible
    assert res.saving.nano == 1_150_800_000


def test_empty_input_and_empty_lanes() -> None:
    res = replay([], "ttl=1h")
    assert res.baseline.nano == 0 and res.cost.nano == 0 and res.saving.nano == 0
    assert res.n_lanes == 0 and res.per_lane == ()


@pytest.mark.parametrize("mode", ["", "measured", "CALIBRATED"])
def test_unknown_mode_raises(mode: str) -> None:
    with pytest.raises(UsageError):
        replay(a1_lane(), "ttl=1h", mode=mode)


def test_non_lane_input_and_malformed_policies_raise() -> None:
    with pytest.raises(UsageError):
        UsageReplayer().replay(["not a lane"], Policy.observed(), mode="documented",  # type: ignore[list-item]
                               pricer=PRICER, rules=RULES, calibration=None)
    bad = [Policy(name="x", ttl=(("all", "2h"),)),
           Policy(name="x", keepalive=("all", 0, 10)),
           Policy(name="x", compaction_window=("big", None)),  # type: ignore[arg-type]
           Policy(name="x", cold_resume=("nap", 5)),
           Policy(name="x", effort=(("all", "extreme", "0.5"),)),
           Policy(name="x", effort=(("all", "low", "2"),)),
           Policy(name="x", effort=(("all", "low", "abc"),)),
           Policy(name="x", batch="all"),
           Policy(name="x", repairs=("reboot",)),
           Policy(name="x", repairs=(3,)),  # type: ignore[arg-type]
           Policy(name="x", model_remap=(("all", ""),)),
           Policy(name="x", ttl=(("bogus:key", "1h"),))]
    for pol in bad:
        with pytest.raises(UsageError):
            replay(a1_lane(), pol)


def test_policy_must_be_a_policy() -> None:
    with pytest.raises(UsageError):
        UsageReplayer().replay([a1_lane()], "ttl=1h", mode="documented",  # type: ignore[arg-type]
                               pricer=PRICER, rules=RULES, calibration=None)


def test_rules_default_when_none() -> None:
    res = UsageReplayer().replay([a1_lane()], Policy(name="t", ttl=(("all", "1h"),)),
                                 mode="documented", pricer=PRICER, rules=None,  # type: ignore[arg-type]
                                 calibration=None)
    assert res.saving.nano == 1_150_800_000


def test_block_level_policies_are_skipped_with_a_reason() -> None:
    lane = a1_lane()
    res = replay(lane, "breakpoints=every_15;repair=block:tool-order")
    assert same(res.cost, res.baseline) and res.saving.nano == 0
    assert res.lanes_skipped and res.lanes_skipped[0][0] == lane.lane_key
    assert "block-level" in res.lanes_skipped[0][1]

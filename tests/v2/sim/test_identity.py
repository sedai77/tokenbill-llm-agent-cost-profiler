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


def test_an_unpriced_changed_request_makes_the_saving_unpriced() -> None:
    """R2 (unknown is not zero): the saving of a changed request whose observed or policy cost is
    unpriced is unknown, so the per-request sum is unpriced too (``core.labels.add``)."""
    priced = a1_lane(lane_key="a")
    unknown = table([(0, 0, 50_000, 0, 0, 100), (30, 50_000, 1_000, 0, 0, 100)],
                    model="claude-not-a-model", lane_key="b")
    res = replay([priced, unknown], "ttl=1h")
    assert res.baseline.nano is None and res.baseline.note.startswith("unpriced:")
    assert res.cost.nano is None
    assert res.saving.nano is None and res.saving.note.startswith("unpriced:")
    assert res.saving.evidence is Evidence.ESTIMATED and res.saving.low_nano is None
    assert any("the saving is unpriced" in a for a in res.assumptions)
    o = outcomes(res)
    assert all(o[r.request_id].changed and o[r.request_id].cost_nano is None
               for r in unknown.requests)
    assert "b" not in dict(res.per_lane) and "a" in dict(res.per_lane)   # int field: omitted
    only_unknown = replay([unknown], "ttl=1h")
    assert only_unknown.saving.nano is None
    # a policy cost that is unpriced (remap to a model without a rate row) is unknown as well
    remapped = replay([priced], "model=claude-not-a-model")
    assert remapped.baseline.nano is not None and remapped.cost.nano is None
    assert remapped.saving.nano is None
    # merging shard replays keeps it unpriced (core.labels.add: None if either is None)
    from tokenbill.core.shards import merge_replay

    merged = merge_replay([replay([priced], "ttl=1h"), replay([unknown], "ttl=1h")])
    assert merged.saving == res.saving and merged.cost == res.cost


def test_unchanged_unpriced_requests_save_exactly_zero() -> None:
    """An unpriced request the policy does not touch keeps its (unpriced) ledger figure and saves
    exactly 0: the baseline and cost are unpriced, the saving is not."""
    priced = a1_lane(lane_key="a")
    unknown = table([(0, 0, 50_000, 0, 0, 100), (30, 50_000, 1_000, 0, 0, 100)],
                    model="claude-not-a-model", lane_key="b", kind=LaneKind.SUBAGENT)
    res = replay([priced, unknown], "ttl=1h@lane_kind:main")
    assert res.baseline.nano is None and res.cost.nano is None
    assert not any(outcomes(res)[r.request_id].changed for r in unknown.requests)
    assert res.saving.nano == 1_150_800_000


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


# late change request A-6 (SPEC-v0.2-COPILOT §21.4a CC-COPILOT-2, ruling R-E23; gate-1 fixup 5)


def test_conforms_with_copilot_pool_lanes() -> None:
    got = assert_replayer_conforms(UsageReplayer(), FakePricer(), pool=True)
    assert got["pool_baseline_nano"] > 0


def test_pool_lanes_replay_on_list_equivalent_like_allowance() -> None:
    """A ``copilot_pool`` lane (billing class ``pool``) replays to LIST_EQUIVALENT figures,
    exactly as the same usage on the allowance path."""
    pool = a1_lane(billing_path="copilot_pool")
    assert pool.billing_class == "pool"
    res = replay(pool, "ttl=1h")
    allowance = replay(a1_lane(billing_path="subscription"), "ttl=1h")
    for figure in (res.baseline, res.cost, res.saving):
        assert figure.basis is Basis.LIST_EQUIVALENT and not figure.is_billed_eligible
    assert (res.baseline.nano, res.cost.nano, res.saving.nano) == (
        allowance.baseline.nano, allowance.cost.nano, allowance.saving.nano)
    observed = replay(pool, Policy.observed())
    assert observed.cost == observed.baseline and observed.saving.nano == 0


@pytest.mark.parametrize("paths", [("api_key", "subscription"), ("api_key", "copilot_pool"),
                                   ("subscription", "copilot_pool")])
def test_mixed_billing_classes_raise_listing_the_classes(paths: tuple[str, str]) -> None:
    lanes = [a1_lane(lane_key=f"L{i}", billing_path=p) for i, p in enumerate(paths)]
    with pytest.raises(UsageError, match=r"one billing class only \(billed \| allowance \| pool\)"):
        replay(lanes, "ttl=1h")


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


@pytest.mark.parametrize("lanes, calibration", [
    (None, None), (5, None), ("lanes", None), ([], "not a report"), ([], {"status": "pass"}),
])
def test_malformed_arguments_raise_usage_error(lanes: object, calibration: object) -> None:
    with pytest.raises(UsageError):
        UsageReplayer().replay(lanes, Policy.observed(), mode="documented",  # type: ignore[arg-type]
                               pricer=PRICER, rules=RULES,
                               calibration=calibration)  # type: ignore[arg-type]


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

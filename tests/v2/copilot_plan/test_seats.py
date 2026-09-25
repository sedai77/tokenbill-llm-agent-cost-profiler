"""Seat levers (addendum §9.2 step 4, DC19, DC20; Appendix C.P10–P12) and the seat-count reader
of seat findings (CONTRACT-CHANGE-CP-PLAN-1)."""

from __future__ import annotations

import pytest

from tokenbill.copilot.plan import SeatCounts, seat_counts
from tokenbill.core import catalog
from tokenbill.core import pool as core_pool
from tokenbill.core.errors import UsageError
from tokenbill.core.types import EvidenceItem

from .worlds import (
    USD,
    finding,
    has_lever,
    idle,
    lever,
    p1_world,
    plan_for,
    seat_finding,
)

SEAT = "copilot.seat_reclaim"
POLICY = "copilot.seat_policy_selected"
DOWNGRADE = "copilot.seat_downgrade"


# ---------- Appendix C.P10–P12 ----------


def test_p10_volume_entity_seat_lever_is_worth_zero_with_the_renewal_note() -> None:
    w = p1_world(2_000_000).flags(**{"billing_mode__enterprise": "volume",
                                     "renewal_date__enterprise": "2027-03-01"})
    pools = w.pools()
    assert pools[0].billing_mode == "volume"
    assert core_pool.realize_seat_change(pools[0], {"business": -10},
                                         month_fee={}) is None
    plan = plan_for(w, [idle(10)], pools=pools)
    seat = lever(plan, SEAT)
    assert seat.shapley.nano == 0 and seat.standalone.nano == 0
    assert "savings only at renewal (2027-03-01)" in seat.shapley.note
    assert plan.headline_monthly.nano == 0


def test_azure_entity_without_renewal_date_is_also_zero() -> None:
    w = p1_world(2_000_000).flags(**{"billing_mode__enterprise": "azure"})
    seat = lever(plan_for(w, [idle(10)]), SEAT)
    assert seat.shapley.nano == 0
    assert "billing mode azure - seat savings only at renewal; value 0" in seat.shapley.note


def test_p11_seat_policy_in_assign_all_org_is_190_needs_eval_and_trade_off() -> None:
    w = p1_world(2_000_000)
    fs = [seat_finding("seat-auto-assign", {"auto_assigned": 10}, entity="enterprise",
                       org="org-b", plan="business"),
          idle(0, entity="enterprise", org="org-b", team="t2")]   # removable 0 for org B
    plan = plan_for(w, fs, include_tradeoffs=True)
    pol = lever(plan, POLICY)
    assert pol.params == "copilot:seat_policy=assign_selected@org:org-b"
    assert catalog.to_aggregate_spec(catalog.parse_aggregate_spec(pol.params)) == pol.params
    assert pol.shapley.nano == 190 * USD
    assert pol.needs_eval
    assert not has_lever(plan, SEAT)                # nothing removable
    assert "copilot.seat_reclaim (linked, nothing to act on" in plan.headline_monthly.note
    # a trade-off lever: absent without include_tradeoffs, and the note says why
    plain = plan_for(w, fs)
    assert not has_lever(plain, POLICY)
    assert "copilot.seat_policy_selected (trade-off; include_tradeoffs off)" in (
        plain.headline_monthly.note)


def test_seat_policy_over_several_orgs_uses_the_all_scope() -> None:
    fs = [seat_finding("seat-auto-assign", {"idle": 10}, entity="enterprise", org="org-b",
                       plan="business"),
          seat_finding("seat-auto-assign", {"idle": 5}, entity="enterprise", org="org-c",
                       plan="business")]
    pol = lever(plan_for(p1_world(2_000_000), fs, include_tradeoffs=True), POLICY)
    assert pol.params == "copilot:seat_policy=assign_selected@all"
    assert pol.shapley.nano == 15 * 19 * USD


def test_p12_twenty_idle_eight_team_assigned_removes_twelve_for_228() -> None:
    plan = plan_for(p1_world(2_000_000), [idle(12, team_assigned=8)])
    seat = lever(plan, SEAT)
    assert seat.shapley.nano == 228 * USD
    assert "enterprise business -12" in seat.shapley.note
    assert not seat.upper_bound and not seat.needs_eval


# ---------- thresholds, plans, assignment ----------


def test_idle_threshold_30d_counts_both_idle_buckets_60d_only_none_90d() -> None:
    fs = [idle(20, bucket="31-90"), idle(5, bucket="none_90d", team="t2"),
          idle(7, bucket="0-7", team="t3")]                       # active seats never count
    plan = plan_for(p1_world(2_000_000), fs)
    seat = lever(plan, SEAT)
    assert seat.params == "copilot:seats_idle=30d@all"
    assert seat.shapley.nano == 25 * 19 * USD
    assert set(seat.finding_ids) == {f.finding_id for f in fs}


def test_idle_finding_without_bucket_counts_only_at_30d() -> None:
    f = seat_finding("idle-seat", {"removable": 4}, entity="enterprise", team="t1",
                     plan="business")
    seat = lever(plan_for(p1_world(2_000_000), [f]), SEAT)
    assert seat.params == "copilot:seats_idle=30d@all" and seat.shapley.nano == 76 * USD


def test_unknown_assignment_seats_are_an_upper_bound_needing_evaluation() -> None:
    seat = lever(plan_for(p1_world(2_000_000), [idle(2, unknown=10)]), SEAT)
    assert seat.shapley.nano == 12 * 19 * USD
    assert seat.upper_bound and seat.needs_eval
    assert seat.shapley.upper_bound
    assert "10 seats with unknown assignment counted as an upper bound" in seat.shapley.note


def test_only_a_total_count_is_treated_as_unknown_assignment() -> None:
    f = seat_finding("idle-seat", {"n": 6}, entity="enterprise", team="t1", plan="business",
                     bucket="none_90d")
    seat = lever(plan_for(p1_world(2_000_000), [f]), SEAT)
    assert seat.shapley.nano == 6 * 19 * USD and seat.upper_bound


def test_enterprise_seats_use_the_enterprise_price_and_allowance() -> None:
    # slack 680,000 credits; 10 idle Enterprise seats: fees $390, pool −39,000 → still slack
    seat = lever(plan_for(p1_world(2_000_000), [idle(10, plan="enterprise")]), SEAT)
    assert seat.shapley.nano == 390 * USD


def test_unknown_plan_seat_in_a_single_plan_entity_takes_that_plan() -> None:
    w = p1_world(1_000_000)
    w.lines = [ln for ln in w.lines if ln.sku != "copilot_enterprise"]    # Business only
    seat = lever(plan_for(w, [idle(10, plan="unknown")]), SEAT)
    assert seat.shapley.nano == 190 * USD


def test_unknown_plan_seat_in_a_mixed_entity_is_not_counted() -> None:
    plan = plan_for(p1_world(1_000_000), [idle(10, plan="unknown")])
    assert not has_lever(plan, SEAT)
    assert "copilot.seat_reclaim (linked, nothing to act on" in plan.joint_saving.note


def test_seat_finding_of_an_unknown_entity_is_skipped() -> None:
    plan = plan_for(p1_world(2_000_000), [idle(10, entity="org:elsewhere")])
    assert not has_lever(plan, SEAT)


def test_plan_mix_downgrade_known_plan_saves_the_fee_difference() -> None:
    f = seat_finding("plan-mix", {"enterprise": 20}, entity="enterprise", plan="enterprise")
    plan = plan_for(p1_world(2_000_000), [f], include_tradeoffs=True)
    down = lever(plan, DOWNGRADE)
    assert down.params == "copilot:plan=business@all"
    # fees 20 × ($39 − $19) = $400; pool −20 × 2,000 = −40,000 credits, still slack
    assert down.shapley.nano == 400 * USD
    assert down.needs_eval and "enterprise enterprise -20" in down.shapley.note
    # in the overage entity the downgrade saves nothing: fees −$400, overage +$400
    over = plan_for(p1_world(3_100_000), [f], include_tradeoffs=True)
    assert lever(over, DOWNGRADE).shapley.nano == 0


def test_seat_levers_ignore_findings_of_other_kinds() -> None:
    f = seat_finding("idle-seat", {"auto_assigned": 5}, entity="enterprise", org="org-b",
                     plan="business", bucket="none_90d")
    plan = plan_for(p1_world(2_000_000), [f], include_tradeoffs=True)
    assert not has_lever(plan, SEAT)          # auto-assigned seats belong to the policy lever
    assert not has_lever(plan, POLICY)        # which links seat-auto-assign findings only


def test_seat_lever_scoped_by_team_and_entity() -> None:
    from tokenbill.copilot import plan as cp
    spec = catalog.AggregateSpec("seats_idle", "30d", "team:t2")
    fs = [idle(3, team="t1"), idle(4, team="t2")]
    w = p1_world(2_000_000)
    pools = {pm.entity_id: pm for pm in w.pools()}
    ctx = cp._context(w.cells(), list(pools.values()), fs, cp_pricer(), lines=w.lines,
                      activity=[], config=[], month=w.month, scenario=None,
                      include_tradeoffs=False, forecast=False)
    player = cp._seat_player(ctx, catalog.lever(SEAT), spec, fs, pools)
    assert player.seat_delta == {("enterprise", "business"): -4}
    by_entity = cp._seat_player(ctx, catalog.lever(SEAT),
                                catalog.AggregateSpec("seats_idle", "30d", "entity:enterprise"),
                                fs, pools)
    assert by_entity.seat_delta == {("enterprise", "business"): -7}
    other = cp._seat_player(ctx, catalog.lever(SEAT),
                            catalog.AggregateSpec("seats_idle", "30d", "model:gpt-5.5"), fs,
                            pools)
    assert other.seat_delta == {}


def cp_pricer():  # noqa: ANN201 - small local helper
    from .worlds import PRICER
    return PRICER


# ---------- the evidence reader ----------


def test_seat_counts_reads_normalized_attribute_names() -> None:
    f = seat_finding("idle-seat", {"n_removable": 3, "team_assigned_seats": 2,
                                   "auto_assigned_count": 4, "unknown_assignment": 1},
                     entity="enterprise", plan="business", bucket="31-90")
    got = seat_counts(f)
    assert got == SeatCounts(removable=3, team_assigned=2, auto_assigned=4, unknown=1,
                             plan="business", bucket="31-90")
    assert got.classified


def test_seat_counts_sums_items_and_reads_classes_plan_and_bucket_from_attrs() -> None:
    items = (EvidenceItem("aggregate", "a", (("assignment", "removable"), ("n", 5))),
             EvidenceItem("aggregate", "b", (("assignment", "team-assigned"), ("count", 2))),
             EvidenceItem("aggregate", "c", (("plan", "Enterprise"), ("bucket", "none_90d"),
                                             ("removable", 1))),
             EvidenceItem("aggregate", "d", (("idle", 9),)))
    f = finding("idle-seat", detector="copilot.seats-budgets", evidence=items,
                entity="enterprise")
    got = seat_counts(f)
    assert (got.removable, got.team_assigned, got.total) == (6, 2, 9)
    assert got.plan == "enterprise" and got.bucket == "none_90d"


def test_seat_counts_ignores_bools_negatives_strings_and_unknown_keys() -> None:
    items = (EvidenceItem("aggregate", "a", (("removable", -3), ("team", "platform"),
                                             ("unknown_key", 7), ("plan", "gold"))),)
    f = finding("idle-seat", detector="copilot.seats-budgets", evidence=items,
                entity="enterprise")
    got = seat_counts(f)
    assert got == SeatCounts()
    assert not got.classified


def test_seat_counts_bounds_and_types() -> None:
    f = seat_finding("idle-seat", {"removable": 10**9 + 1}, entity="enterprise")
    with pytest.raises(UsageError):
        seat_counts(f)
    with pytest.raises(UsageError):
        seat_counts("not a finding")  # type: ignore[arg-type]

"""Plan unknown (owner answer 2; R17, ruling R-E22; Appendix C.P13, P13b, P13c): the whole game is
played once per plan scenario on that scenario's pool months, never averaged or summed."""

from __future__ import annotations

import pytest

from tokenbill.copilot.plan import SCENARIO_KNOWN, plan_copilot, plan_copilot_scenarios
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Figure
from tokenbill.core.types import ActionPlan

from .worlds import (
    PRICER,
    USD,
    C,
    World,
    finding,
    has_lever,
    idle,
    lever,
    p1_world,
    seat_finding,
)

SEAT = "copilot.seat_reclaim"
AUTO = "copilot.default_model_auto"
FAST = "copilot.fast_mode_off"
DOWNGRADE = "copilot.seat_downgrade"
TODAY = "2026-11-10"


def _p13_world() -> World:
    """C.P13: entity E, metered, standard month 2026-10 (closed), 100 activity-report seats (plan
    unknown), pooled use 250,000 credits: 150,000 Sonnet 5 + 100,000 fast-mode Opus 4.8 (20M
    output tokens: $1,000 fast, $500 standard → a 50,000-credit fast premium, C.P13c)."""
    w = World(month="2026-10").report_seats(100)
    w.flags(**{"billing_mode__enterprise": "metered"})
    w.usage(150_000, users=5)
    w.usage(100_000, model="Claude Opus 4.8 (fast mode)", output_tokens=20_000_000)
    return w


def _scenarios(w: World, findings: list, **kw: object) -> dict[str, ActionPlan]:
    pools = w.pools(today=TODAY)
    got = plan_copilot_scenarios(w.cells(), pools, findings, PRICER, lines=w.lines,
                                 activity=w.activity, config=w.config, month=w.month,
                                 **kw)  # type: ignore[arg-type]
    return dict(got)


def _figures(plan: ActionPlan) -> list[Figure]:
    figs = [plan.joint_saving, plan.headline_monthly]
    if plan.pool_headroom_monthly is not None:
        figs.append(plan.pool_headroom_monthly)
    for lv in plan.levers:
        figs += [lv.standalone, lv.shapley, lv.projected_monthly]
    return figs


def test_p13_pool_months_are_a_scenario_pair() -> None:
    pools = _p13_world().pools(today=TODAY)
    assert [(pm.plan_scenario, pm.pool_credits, pm.regime) for pm in pools] == [
        ("business", "190000", "overage"), ("enterprise", "390000", "slack")]


def test_p13b_idle_seats_are_worth_0_if_business_and_390_upper_bound_if_enterprise() -> None:
    fs = [idle(unknown=10, plan="unknown"), finding("auto-adoption", team="t1")]
    plans = _scenarios(_p13_world(), fs)
    assert list(plans) == ["business", "enterprise"]
    b_seat, e_seat = lever(plans["business"], SEAT), lever(plans["enterprise"], SEAT)
    assert b_seat.shapley.nano == 0 and b_seat.standalone.nano == 0
    assert b_seat.shapley.high_nano in (None, 0)
    assert e_seat.shapley.nano == 390 * USD and e_seat.standalone.nano == 390 * USD
    assert e_seat.shapley.nano <= 390 * USD
    for seat in (b_seat, e_seat):
        assert seat.upper_bound and seat.needs_eval
        assert "unknown assignment counted as an upper bound" in seat.shapley.note
    assert "plan unknown: pool rule" not in b_seat.shapley.note
    assert b_seat.shapley.note.startswith("If Business: plan unknown")
    assert e_seat.shapley.note.startswith("If Enterprise: plan unknown")


def test_p13c_credit_saving_is_invoice_if_business_and_headroom_if_enterprise() -> None:
    plans = _scenarios(_p13_world(), [finding("fast-mode", team="t1")])
    bus, ent = plans["business"], plans["enterprise"]
    assert lever(bus, FAST).shapley.nano == 500 * USD
    assert lever(bus, FAST, Basis.LIST_EQUIVALENT).shapley.nano == 0
    assert lever(ent, FAST).shapley.nano == 0
    assert lever(ent, FAST, Basis.LIST_EQUIVALENT).shapley.nano == 50_000 * C
    assert bus.headline_monthly.nano == 500 * USD and ent.headline_monthly.nano == 0
    assert ent.pool_headroom_monthly.nano == 500 * USD


def test_seat_downgrade_is_absent_from_both_scenarios_with_the_reason() -> None:
    fs = [idle(unknown=10, plan="unknown"),
          seat_finding("plan-mix", {"enterprise": 10}, entity="enterprise", plan="enterprise")]
    plans = _scenarios(_p13_world(), fs, include_tradeoffs=True)
    for scenario, plan in plans.items():
        assert not has_lever(plan, DOWNGRADE), scenario
        assert "copilot.seat_downgrade (plan unknown)" in plan.headline_monthly.note
        assert "copilot.seat_downgrade (plan unknown)" in plan.joint_saving.note


def test_no_figure_appears_outside_a_scenario() -> None:
    fs = [idle(unknown=10, plan="unknown"), finding("auto-adoption", team="t1"),
          finding("fast-mode", team="t1")]
    plans = _scenarios(_p13_world(), fs)
    assert SCENARIO_KNOWN not in plans
    for scenario, plan in plans.items():
        assert plan.sample == f"copilot cells, 2 cells, month 2026-10, scenario {scenario}"
        prefix = f"If {scenario.capitalize()}: plan unknown"
        for fig in _figures(plan):
            assert fig.note.startswith(prefix), fig.note
    # never averaged: the two headlines are the two scenarios' own numbers
    assert plans["business"].headline_monthly != plans["enterprise"].headline_monthly


def test_scenario_findings_are_used_only_in_their_scenario() -> None:
    fs = [idle(unknown=4, plan="unknown", plan_scenario="business"),
          idle(unknown=10, plan="unknown", plan_scenario="enterprise", team="t2")]
    plans = _scenarios(_p13_world(), fs)
    b_seat, e_seat = lever(plans["business"], SEAT), lever(plans["enterprise"], SEAT)
    assert b_seat.finding_ids == (fs[0].finding_id,)
    assert e_seat.finding_ids == (fs[1].finding_id,)
    assert e_seat.shapley.nano == 390 * USD


def test_known_plan_gives_one_plan_keyed_known() -> None:
    w = p1_world(2_000_000)
    stray = idle(9, plan_scenario="enterprise")          # a scenario finding is ignored
    got = plan_copilot_scenarios(w.cells(), w.pools(), [idle(50), stray], PRICER,
                                 lines=w.lines, activity=[], config=w.config, month=w.month)
    assert [k for k, _ in got] == [SCENARIO_KNOWN]
    plan = got[0][1]
    assert lever(plan, SEAT).shapley.nano == 950 * USD
    assert lever(plan, SEAT).finding_ids == (idle(50).finding_id,)
    assert "scenario" not in plan.sample
    assert not plan.headline_monthly.note.startswith("If ")


def test_plan_copilot_refuses_an_unknown_plan_without_a_scenario() -> None:
    w = _p13_world()
    with pytest.raises(UsageError, match="plan_copilot_scenarios"):
        plan_copilot(w.cells(), w.pools(today=TODAY), [], PRICER, lines=w.lines, activity=[],
                     month=w.month)
    with pytest.raises(UsageError):
        plan_copilot(w.cells(), w.pools(today=TODAY), [], PRICER, lines=w.lines, activity=[],
                     month=w.month, scenario="mixed")


def test_known_and_unknown_entities_in_org_mode() -> None:
    """Org A's plan is known (seat lines), org B's is not (activity report): the scenario plans
    share org A's pool month and differ only through org B."""
    w = World(month="2026-10", entity_mode="org")
    w.seats("business", 100, org="org-a").usage(150_000, org="org-a")
    w.report_seats(100, org="org-b").usage(250_000, org="org-b", team="t2")
    w.flags(**{"billing_mode__org:org-b": "metered"})
    pools = w.pools(today=TODAY)
    assert sorted((pm.entity_id, pm.plan_scenario) for pm in pools) == [
        ("org:org-a", None), ("org:org-b", "business"), ("org:org-b", "enterprise")]
    fs = [idle(10, entity="org:org-a"), idle(unknown=10, plan="unknown", entity="org:org-b",
                                               team="t2")]
    plans = dict(plan_copilot_scenarios(w.cells(), pools, fs, PRICER, lines=w.lines,
                                        activity=[], config=w.config, month=w.month))
    # org A slack (190,000 pool vs 150,000 use): 10 Business seats → $190 in both scenarios;
    # org B: $0 if Business (overage), $390 if Enterprise
    assert lever(plans["business"], SEAT).shapley.nano == 190 * USD
    assert lever(plans["enterprise"], SEAT).shapley.nano == (190 + 390) * USD
    assert "regime org:org-a slack, org:org-b overage" in plans["business"].joint_saving.note

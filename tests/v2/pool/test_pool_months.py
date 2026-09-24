"""Pool months (addendum CA-40 / CA-48, F-POOL brief item 6): Appendix C.P1, P6, P7, P9, P13–P15
and the brief's P14b / P15b through ``build_cells`` + ``pool_months``."""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from tokenbill.core import builders as b
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Evidence, combine_weakest, exact
from tokenbill.core.pool import (
    SEATS_LOWER_BOUND_NOTE,
    build_cells,
    capped_cost_centers,
    overage_total,
    pool_months,
)

from .worlds import (
    USD,
    C,
    activity_seats,
    api_seats,
    cost_center,
    daily,
    flags,
    p9_series,
    people,
    quota,
    rows,
    seat_count,
)

METERED = flags({"billing_mode.enterprise": "metered"})
POOL_FIELDS = {"seats", "pool_credits", "pool_nano", "pool_draw_nano", "discount_other_nano",
               "discount_unclassified_nano", "overage_observed_nano", "overage_forecast", "regime",
               "notes", "plan_scenario", "promo"}


def _months(cost: list, aggs: list, lics: list = (), conf: list = (), *, today: str, **kw):
    conf = list(conf)
    cells, _ = build_cells(aggs, cost, capped=capped_cost_centers(conf),
                           entity_mode=kw.pop("entity_mode", "enterprise"))
    return pool_months(cells, cost, list(lics), conf, today=today, **kw)


def _p1() -> tuple[list, list]:
    seats = [b.make_seat_line("business", "1000", date_utc="2026-10-01"),
             b.make_seat_line("enterprise", "200", date_utc="2026-10-01")]
    pooled, pooled_aggs = rows(2_680_000, date="2026-10-05", users=people(10),
                               discount=2_680_000)
    over, over_aggs = rows(420_000, date="2026-10-20", users=people(10))
    review, review_aggs = rows(15_000, date="2026-10-05", unattributed=True,
                               model="Copilot Code Review")
    return seats + pooled + over + review, pooled_aggs + over_aggs + review_aggs


def test_p1_closed_month_and_invoice() -> None:
    cost, aggs = _p1()
    [pm] = _months(cost, aggs, today="2026-11-10", gross_is_list=True)
    assert (pm.entity_id, pm.month, pm.billing_mode, pm.seats, pm.seats_source) == (
        "enterprise", "2026-10", "metered", (("business", "1000"), ("enterprise", "200")),
        "seat_lines")
    assert (pm.pool_credits, pm.pool_nano, pm.promo) == ("2680000", 2_680_000 * C, None)
    assert (pm.consumed_report_nano, pm.consumed_estimate_nano) == (3_100_000 * C, 0)
    assert (pm.pool_draw_nano, pm.discount_other_nano, pm.discount_unclassified_nano) == (
        2_680_000 * C, 0, 0)
    assert (pm.overage_observed_nano, pm.direct_net_nano, pm.direct_draws_pool) == (
        4_200 * USD, 150 * USD, "no")
    assert (pm.days_final, pm.days_provisional, pm.days_in_month, pm.finality) == (2, 0, 31,
                                                                                    "closed")
    assert (pm.forecast, pm.overage_forecast, pm.regime, pm.notes) == (None, None, "overage", ())
    assert (pm.plan_source, pm.plan_scenario, pm.plan_conflict) == ("seat_lines", None, False)
    seat_fees = [exact(x.amount_nano, Basis.LIST) for x in cost if x.cost_type == "seat"]
    total = combine_weakest([*seat_fees, exact(pm.overage_observed_nano, Basis.LIST),
                             exact(pm.direct_net_nano, Basis.LIST)], note="total.invoice")
    assert (total.nano, total.basis) == (31_150 * USD, Basis.LIST)   # INVOICE only reconciled
    # the discount stays unclassified without the reconciler's gross_is_list decision
    [pm] = _months(cost, aggs, today="2026-11-10")
    assert (pm.pool_draw_nano, pm.discount_unclassified_nano) == (None, 2_680_000 * C)
    # direct rows with a discount draw on the pool: said, never silently added to consumption
    drawn, drawn_aggs = rows(1_000, date="2026-10-06", unattributed=True, discount=1_000,
                             model="Copilot Code Review")
    [pm] = _months(cost + drawn, aggs + drawn_aggs, today="2026-11-10")
    assert (pm.direct_draws_pool, pm.consumed_report_nano) == ("yes", 3_100_000 * C)
    assert any(n.startswith("direct-org rows draw on the pool") for n in pm.notes)


def test_p9_open_month_forecast() -> None:
    series = p9_series() | {"2026-09-21": 100_000, "2026-09-22": 100_000}
    cost, aggs = daily(series, cutoff="2026-09-20")
    cost += [b.make_seat_line("business", "1000"), b.make_seat_line("enterprise", "200")]
    [pm] = _months(cost, aggs, today="2026-09-23")
    assert (pm.finality, pm.days_final, pm.days_provisional, pm.regime) == ("open", 20, 2,
                                                                           "overage")
    assert pm.consumed_report_nano == 2_200_000 * C
    fc, over = pm.forecast, pm.overage_forecast
    assert fc is not None and over is not None
    assert (fc.nano, fc.low_nano, fc.high_nano) == (3_100_000 * C, 2_920_000 * C, 3_280_000 * C)
    assert (fc.evidence, fc.basis) == (Evidence.ESTIMATED, Basis.LIST_EQUIVALENT)
    assert (over.nano, over.low_nano, over.high_nano) == (4_200 * USD, 2_400 * USD, 6_000 * USD)
    assert (over.evidence, over.basis, over.upper_bound) == (Evidence.ESTIMATED, Basis.LIST,
                                                             False)


def test_p6_promo_cliff() -> None:
    seats = [b.make_seat_line("business", "100", date_utc="2026-07-01"),
             b.make_seat_line("business", "100", date_utc="2026-09-01")]
    july, july_aggs = rows(250_000, date="2026-07-15", discount=250_000)
    sept = {}
    for day in range(1, 30):
        date = f"2026-09-{day:02d}"
        weekend = dt.date.fromisoformat(date).weekday() >= 5
        sept[date] = 3_750 if weekend else 10_000
    sept_cost, sept_aggs = daily(sept)
    july_pm, sept_pm = _months(seats + july + sept_cost, july_aggs + sept_aggs,
                               today="2026-10-02")
    assert (july_pm.month, july_pm.pool_credits, july_pm.regime, july_pm.promo) == (
        "2026-07", "300000", "slack", "promo:2026-06-01/2026-09-01")
    assert (sept_pm.month, sept_pm.pool_credits, sept_pm.promo, sept_pm.finality) == (
        "2026-09", "190000", None, "open")
    assert sept_pm.forecast is not None and sept_pm.forecast.nano == 250_000 * C
    over = sept_pm.overage_forecast
    assert over is not None and (over.nano, over.evidence) == (600 * USD, Evidence.ESTIMATED)
    assert sept_pm.regime == "overage"
    no_promo = _months(seats + july, july_aggs, [], [flags({"promo_eligible": False})],
                       today="2026-10-02")
    assert (no_promo[0].pool_credits, no_promo[0].promo) == ("190000", None)


def _p7(policy: str | None) -> tuple[list, list, list]:
    conf = [cost_center("A", "95000")]
    if policy is not None:
        conf.append(flags({"capped_policy.A": policy}))
    seats = [b.make_seat_line("business", "950", date_utc="2026-10-01"),
             b.make_seat_line("enterprise", "200", date_utc="2026-10-01"),
             b.make_seat_line("business", "50", date_utc="2026-10-01", cost_center="A")]
    ent, ent_aggs = rows(2_000_000, date="2026-10-05", users=people(20), discount=2_000_000)
    net = 35_000 if policy == "continue" else 0
    cc, cc_aggs = rows(95_000 + net, date="2026-10-05", users=people(5, "a"), discount=95_000,
                       cost_center="A")
    return seats + ent + cc, ent_aggs + cc_aggs, conf


def test_p7_capped_cost_center_pool_months() -> None:
    cost, aggs, conf = _p7("continue")
    cc, ent = _months(cost, aggs, [], conf, today="2026-11-10")
    assert (ent.entity_id, ent.pool_credits, ent.regime, ent.capped_policy) == (
        "enterprise", "2585000", "slack", None)
    assert ent.pool_nano - ent.consumed_report_nano == 585_000 * C       # shared slack
    assert (cc.entity_id, cc.pool_credits, cc.capped_policy, cc.regime) == (
        "cc:A", "95000", "continue", "overage")
    assert cc.overage_observed_nano == 350 * USD
    assert "capped cost center: cap 95000 credits, policy continue" in cc.notes
    use = {pm.entity_id: pm.consumed_report_nano for pm in (cc, ent)}
    pools = {pm.entity_id: pm.pool_nano for pm in (cc, ent)}
    assert overage_total(use, pools, {"A": 95_000 * C}, policies={"A": "continue"}) == (
        350 * USD, 350 * USD)


def test_p7_unknown_policy_open_month_widens_the_overage_forecast() -> None:
    cost, aggs, conf = _p7(None)
    cost = [x for x in cost if x.cost_center != "A" or x.cost_type == "seat"]
    extra, extra_aggs = rows(130_000, date="2026-10-05", users=people(5, "a"), discount=95_000,
                             cost_center="A")
    conf.append(cost_center("A", "90000", date="2026-09-01"))    # an older cap: superseded
    cc, _ = _months(cost + extra, aggs + extra_aggs, [], conf, today="2026-11-02",
                    recent_estimates=[("2026-10-29", "cc:A", 3 * C), ("2026-10-05", "cc:A", C)])
    over = cc.overage_forecast
    assert (cc.finality, cc.capped_policy) == ("open", "unknown")
    assert over is not None and (over.low_nano, over.nano, over.high_nano) == (0, 350 * USD,
                                                                               350 * USD)
    assert cc.consumed_estimate_nano == 3 * C


def test_p7_block_policy_forecast_reports_blocked_demand() -> None:
    cost, aggs, conf = _p7("block")
    extra, extra_aggs = rows(35_000, date="2026-10-06", users=people(5, "a"), cost_center="A")
    cc, _ = _months(cost + extra, aggs + extra_aggs, [], conf, today="2026-11-02")
    over = cc.overage_forecast
    assert over is not None and (over.nano, over.high_nano) == (0, 0)
    assert "blocked demand 35000 credits" in over.note


def test_p13_plan_unknown_two_scenarios() -> None:
    cost, aggs = rows(250_000, date="2026-10-10", users=people(50), discount=250_000)
    biz, ent = _months(cost, aggs, activity_seats(100), [METERED], today="2026-11-10")
    assert (biz.plan_scenario, ent.plan_scenario) == ("business", "enterprise")
    for pm in (biz, ent):
        assert (pm.seats, pm.seats_source, pm.billing_mode, pm.finality) == (
            (("unknown", "100"),), "licenses", "metered", "closed")
        assert (pm.plan_source, pm.plan_conflict) == ("none", False)
        assert pm.consumed_report_nano == 250_000 * C
    assert (biz.pool_credits, biz.overage_observed_nano, biz.regime) == ("190000", 600 * USD,
                                                                         "overage")
    assert (ent.pool_credits, ent.overage_observed_nano, ent.regime) == ("390000", 0, "slack")
    assert ent.pool_nano - ent.consumed_report_nano == 140_000 * C
    assert biz.notes[0] == "plan unknown: scenario business"
    assert ent.notes[0] == "plan unknown: scenario enterprise"
    fees = {s: Decimal(dict(pm.seats)["unknown"]) * Decimal(p) for s, pm, p in
            (("business", biz, 19), ("enterprise", ent, 39))}
    assert fees == {"business": Decimal(1_900), "enterprise": Decimal(3_900)}
    # the pair differs only in pool-dependent fields
    a, b_ = dataclasses.asdict(biz), dataclasses.asdict(ent)
    assert {k for k in a if a[k] != b_[k]} <= POOL_FIELDS


def test_p14_conflict_single_pool_month() -> None:
    cost = [b.make_seat_line("enterprise", "50", date_utc="2026-09-01")]
    usage, aggs = rows(100_000, date="2026-09-10", discount=100_000)
    [pm] = _months(cost + usage, aggs, api_seats(50, "business", date="2026-09-05"),
                   [flags({"plan.enterprise": "business"})], today="2026-10-10")
    assert (pm.pool_credits, pm.plan_source, pm.plan_conflict, pm.plan_scenario) == (
        "195000", "seat_lines", True, None)
    assert "plan evidence conflict (dq.copilot_plan_conflict)" in pm.notes


def test_p14b_org_mode_conflict() -> None:
    cost = [b.make_seat_line("business", "40", date_utc="2026-10-01", organization="A")]
    usage, aggs = rows(10_000, date="2026-10-10", org="A")
    [pm] = _months(cost + usage, aggs, api_seats(40, "enterprise", org="A"),
                   [flags({"plan.org:A": "enterprise"})], today="2026-11-10", entity_mode="org")
    assert (pm.entity_id, pm.pool_credits, pm.plan_conflict, pm.plan_source) == (
        "org:A", "76000", True, "seat_lines")


@pytest.mark.parametrize("quotas,pools", [
    ([(3900, 40)], {None: "156000"}),
    ([(1900, 30), (3900, 10)], {None: "96000"}),
    ([(3900, 36)], {"business": "148000", "enterprise": "156000"}),
    ([], {"business": "76000", "enterprise": "156000"}),
])
def test_p15_pools_from_report_quota(quotas: list[tuple[int, int]],
                                     pools: dict[str | None, str]) -> None:
    usage, aggs = rows(10_000, date="2026-09-10", users=people(40))
    got = _months(usage, aggs, activity_seats(40, date="2026-09-15"),
                  [quota("2026-09", q, n) for q, n in quotas], today="2026-10-10")
    assert {pm.plan_scenario: pm.pool_credits for pm in got} == pools
    assert {pm.seats_source for pm in got} == {"licenses"}


def test_p15b_report_users_lower_bound() -> None:
    usage, aggs = rows(300_000, date="2026-10-12", users=people(60))
    [pm] = _months(usage, aggs, [], [quota("2026-10", 3900, 60)], today="2026-11-10")
    assert (pm.pool_credits, pm.seats_source, pm.plan_source) == ("234000", "report_users",
                                                                  "report_quota")
    assert SEATS_LOWER_BOUND_NOTE in pm.notes
    biz, ent = _months(usage, aggs, [], [], today="2026-11-10")
    assert (biz.pool_credits, ent.pool_credits) == ("114000", "234000")
    assert all(SEATS_LOWER_BOUND_NOTE in pm.notes for pm in (biz, ent))
    # open month: the overage forecast is an upper bound
    biz, ent = _months(usage, aggs, [], [], today="2026-10-20")
    assert biz.overage_forecast is not None and biz.overage_forecast.upper_bound is True


def test_seat_sources_and_idle_months() -> None:
    conf = [flags({"pool_seats.enterprise.business": 10}), seat_count("org-a", "business", 7)]
    [pm] = pool_months([], [], [], conf[1:], today="2026-11-10")
    assert (pm.seats_source, pm.pool_credits, pm.plan_source, pm.regime) == (
        "seat_counts", "13300", "seats_api", "slack")
    # stated pool seats count (10); the seats API (data) places 3 Enterprise seats and conflicts
    # with the statement "all Business"; 7 stay unknown → a scenario pair
    biz, ent = pool_months([], [], api_seats(3, "enterprise"), conf, today="2026-11-10")
    assert (biz.seats_source, biz.plan_source, biz.plan_conflict) == ("run_flags", "seats_api",
                                                                      True)
    assert biz.seats == ent.seats == (("enterprise", "3"), ("unknown", "7"))
    assert (biz.pool_credits, ent.pool_credits) == ("25000", "39000")
    # direct usage without any seat: the pool is unknown, never 0
    direct, d_aggs = rows(100, date="2026-10-05", unattributed=True)
    [pm] = _months(direct, d_aggs, today="2026-11-10")
    assert (pm.seats, pm.seats_source, pm.regime, pm.overage_forecast) == ((), "none", "unknown",
                                                                           None)
    assert "seats unknown: pool and regime unknown" in pm.notes


def test_passed_plans_estimates_and_gross_is_list_mapping() -> None:
    cost, aggs = rows(250_000, date="2026-10-10", users=people(50), discount=150_000)
    cells, _ = build_cells(aggs, cost)
    plans = [b.make_plan_evidence(month="2026-10")]
    biz, ent = pool_months(cells, cost, [], [], today="2026-11-10", plans=plans * 2,
                           gross_is_list={"enterprise:2026-10": "true"},
                           recent_estimates=[("2026-10-28", None, 9 * C)])
    # report_users (50) is the census; the plan evidence (100 unknown) raises nothing here
    assert (biz.seats, biz.seats_source) == ((("unknown", "50"),), "report_users")
    # discount 150,000 fits the enterprise scenario's pool (195,000), not the business one (95,000)
    assert (biz.pool_draw_nano, ent.pool_draw_nano) == (None, 150_000 * C)
    assert biz.consumed_estimate_nano == 9 * C
    only_plan = pool_months([], [], [], [], today="2026-11-10", plans=plans)
    assert [(pm.seats, pm.seats_source) for pm in only_plan] == [
        ((("unknown", "100"),), "none")] * 2
    [pm] = pool_months(cells, cost, [], [], today="2026-11-10", plans=[
        b.make_plan_evidence(month="2026-10", plan="business", source="admin_statement",
                             seats={"business": 50})],
        gross_is_list={"gross_is_list:enterprise:2026-10": False})
    assert (pm.plan_source, pm.pool_credits, pm.pool_draw_nano) == ("admin_statement", "95000",
                                                                    None)
    with pytest.raises(UsageError):
        pool_months(cells, cost, [], [], today="2026-11-10",
                    plans=[plans[0], b.make_plan_evidence(month="2026-10", plan="business")])
    with pytest.raises(UsageError):
        pool_months(cells, cost, [], [], today="2026-11-10", gross_is_list="yes")  # type: ignore[arg-type]


def test_month_grain_org_inference_and_errors() -> None:
    cost, aggs = rows(100, date="2026-10-10", org="x")
    cost.append(b.make_seat_line("business", "1", date_utc="2026-10-01", organization="x"))
    cells, _ = build_cells(aggs, cost, grain="month", entity_mode="org")
    [closed] = pool_months(cells, cost, [], [], today="2026-11-10")
    assert (closed.entity_id, closed.finality, closed.days_final) == ("org:x", "closed", 31)
    [open_] = pool_months(cells, cost, [], [], today="2026-10-20",
                          recent_estimates=[("2026-10-11", None, 5)])
    assert (open_.finality, open_.forecast, open_.regime, open_.days_provisional) == (
        "open", None, "unknown", 0)
    assert open_.consumed_estimate_nano == 0      # org mode: an entity-less estimate is skipped
    for bad in ({"today": "2026-13-01"}, {"recent_estimates": [("2026-10-01", "team:x", 1)]},
                {"recent_estimates": [("2026-10-01", None, 1.0)]},
                {"recent_estimates": [("2026-10-01", None)]}, {"entity_mode": "team"},
                {"plans": ["x"]}):
        kw = {"today": "2026-11-10"} | bad
        with pytest.raises(UsageError):
            pool_months(cells, cost, [], [], **kw)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        pool_months(["x"], [], [], [], today="2026-11-10")  # type: ignore[list-item]


def test_volume_mode_note_and_cap_differs_note() -> None:
    conf = [flags({"billing_mode.enterprise": "volume"}), cost_center("A", "1000")]
    cost = [b.make_seat_line("business", "5", date_utc="2026-10-01"),
            b.make_seat_line("business", "1", date_utc="2026-10-01", cost_center="A")]
    cc, ent = pool_months([], cost, [], conf, today="2026-11-10")
    assert ent.billing_mode == "volume" and any("billing mode volume" in n for n in ent.notes)
    assert "cap differs from the seat allowance 1900 credits" in cc.notes

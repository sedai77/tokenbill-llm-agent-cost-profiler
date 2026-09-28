"""Budget design (addendum §11.3 "Budget design", DC19, R17; brief acceptance 4 and 5)."""

from __future__ import annotations

import pytest

from tokenbill.copilot.budgets import USER_LEVEL_SCOPES, budget_design
from tokenbill.core import builders as b
from tokenbill.core import pool as cpool
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, estimated, unpriced

from .helpers import USD, BudgetWorld, budget_pools, three_cost_centers


def _design(w: BudgetWorld, pools=None, **kw):
    return budget_design(pools if pools is not None else budget_pools(w), w.cells(), w.config,
                         k=5, cost_lines=w.lines, **kw)


def _budgets(specs, scope: str) -> dict[str, int]:
    return {s["request"]["body"]["budget_entity_name"]: s["request"]["body"]["budget_amount"]
            for s in specs if s["kind"] == "budget"
            and s["request"]["body"]["budget_scope"] == scope}


def test_three_cost_centers_one_at_180_percent_enables_its_pool_with_the_choice() -> None:
    specs = _design(three_cost_centers())
    pools = [s for s in specs if s["kind"] == "cost_center_pool"]
    assert len(pools) == 1
    text = pools[0]["text"]
    assert "Cost center A drew 180%" in text and "34,200 of 19,000 credits" in text
    assert "block its members" in text and "continue as paid overage" in text
    req = pools[0]["request"]
    assert req["method"] == "PATCH" and req["body"] == {"ai_credit_pool_enabled": True}
    assert req["path"].endswith("/cost-centers/{cost_center_id}")
    assert "block or continue" in req["note"]


def test_budgets_are_sized_as_specified() -> None:
    specs = _design(three_cost_centers())
    # overage $42 (61,200 − 57,000 credits), allocated by pooled credits, × 1.1, rounded up
    assert _budgets(specs, "cost_center") == {"A": 26, "B": 12, "C": 10}
    # cohort p99 of monthly per-user credits: A 7,200 → $72; B 1,500 → $15; C 1,200 → $12
    assert _budgets(specs, "multi_user_cost_center") == {"A": 72, "B": 15, "C": 12}
    # enterprise ≥ Σ cost-center budgets ($48) and overage p90 × 1.1 ($46.20), + direct $0
    assert _budgets(specs, "enterprise") == {"<enterprise slug>": 48}
    for s in specs:
        if s["request"] is None:
            continue
        body = s["request"]["body"]
        if "budget_amount" in body:
            assert type(body["budget_amount"]) is int and body["budget_amount"] >= 1
            assert body["prevent_further_usage"] is (body["budget_scope"] in USER_LEVEL_SCOPES)
            assert body["budget_type"] == "BundlePricing"
            assert body["budget_product_sku"] == "ai_credits"
            assert "trade-off" in s["request"]["note"] or body["prevent_further_usage"]
    check = [s["text"] for s in specs if s["kind"] == "sizing_check"]
    assert check == ["Sizing check (GitHub) for enterprise: user-level caps $990.00 - pool "
                     "$570.00 = max metered $420.00."]


def test_pool_months_from_core_pool_give_the_same_design() -> None:
    # report rows whose discounts are the pool draw (57,000 credits): net = the 4,200 overage
    w = BudgetWorld()
    w.cost_center("A", [3_000] * 9 + [7_200], seats=10, discounts=[3_000] * 9 + [3_000])
    w.cost_center("B", [1_500] * 10, seats=10, discounts=[1_500] * 10)
    w.cost_center("C", [1_200] * 10, seats=10, discounts=[1_200] * 10)
    pools = cpool.pool_months(w.cells(), w.lines, [], w.config, today="2026-10-20")
    assert pools[0].overage_observed_nano == 42 * USD
    assert [(pm.entity_id, pm.billing_mode, pm.regime, pm.pool_credits) for pm in pools] == [
        ("enterprise", "metered", "overage", "57000")]
    specs = _design(w, pools)
    assert _budgets(specs, "cost_center") == {"A": 26, "B": 12, "C": 10}
    assert _budgets(specs, "enterprise") == {"<enterprise slug>": 48}
    assert sum(s["kind"] == "cost_center_pool" for s in specs) == 1


def test_volume_billing_emits_no_cost_center_advice() -> None:
    w = three_cost_centers()
    specs = _design(w, budget_pools(w, billing_mode="volume"))
    assert {s["kind"] for s in specs} == {"budget", "note"}
    assert _budgets(specs, "cost_center") == {} and _budgets(specs, "multi_user_cost_center") == {}
    assert _budgets(specs, "enterprise") == {"<enterprise slug>": 47}
    assert any("VERIFY" in s["text"] and "only to metered usage" in s["text"] for s in specs)
    for mode in ("azure", "unknown"):
        other = _design(w, budget_pools(w, billing_mode=mode))
        assert not any(s["kind"] == "cost_center_pool" for s in other)
        assert _budgets(other, "cost_center") == {}


def test_a_cohort_of_three_users_gets_no_quantile() -> None:
    w = three_cost_centers()
    w.cost_center("D", [100, 200, 300], seats=3)
    specs = _design(w)
    assert "D" not in _budgets(specs, "multi_user_cost_center")
    assert "D" not in _budgets(specs, "cost_center")
    notes = [s["text"] for s in specs if s["kind"] == "note"]
    assert any("Cost center D: fewer than k=5 users" in t for t in notes)
    assert not any("300" in t for t in notes)
    # a larger configured cost center with only three users of usage: still no quantile
    w2 = three_cost_centers()
    w2.cost_center("E", [100, 200, 300], seats=8, n_users=8)
    specs2 = _design(w2)
    assert "E" not in _budgets(specs2, "multi_user_cost_center")
    assert "E" in _budgets(specs2, "cost_center")


def test_plan_unknown_returns_two_labelled_sets_and_known_neither() -> None:
    w = three_cost_centers()
    unknown = _design(w, budget_pools(w, scenarios=True))
    scenario_specs = [s for s in unknown if s["scenario"] is not None]
    assert {s["scenario"] for s in scenario_specs} == {"business", "enterprise"}
    for scenario in ("business", "enterprise"):
        mine = [s for s in scenario_specs if s["scenario"] == scenario]
        assert any(s["kind"] == "sizing_check" for s in mine)
        assert any(s["kind"] == "budget" for s in mine)
        prefix = f"if {scenario.capitalize()}: "
        assert all(s["text"].startswith(prefix) for s in mine)
        assert all(s["request"]["note"].startswith(prefix) for s in mine if s["request"])
    # pool-independent user-level budgets appear once, unlabelled
    user_level = [s for s in unknown if s["kind"] == "budget" and s["request"]["body"][
        "budget_scope"] == "multi_user_cost_center"]
    assert len(user_level) == 3 and all(s["scenario"] is None for s in user_level)
    # the Enterprise scenario is in slack: no pool advice, minimum budgets
    ent_pools = [s for s in scenario_specs if s["kind"] == "cost_center_pool"]
    assert [s["scenario"] for s in ent_pools] == ["business"]
    assert "minimum $1" in next(s["text"] for s in scenario_specs if s["scenario"] ==
                                "enterprise" and s["kind"] == "budget")
    known = _design(w)
    assert all(s["scenario"] is None for s in known)


def test_open_month_uses_the_forecast_p90_and_extrapolates_direct_rows() -> None:
    w = three_cost_centers()
    forecast = estimated(50 * USD, Basis.LIST, low=30 * USD, high=100 * USD, note="forecast")
    pm = b.make_pool_month(seats={"business": "30"}, consumed_report_nano=w.consumed_nano(),
                           finality="open", days_final=10, days_provisional=5,
                           overage_forecast=forecast, direct_net_nano=15 * USD)
    specs = _design(w, [pm])
    # cost-center budgets from p90 $100: $62 + $27 + $22 = $111 > $110; direct $15 over 15 of
    # 30 days → $30
    assert _budgets(specs, "enterprise") == {"<enterprise slug>": 141}
    assert any("month to date" in s["text"] for s in specs)
    unpriced_pm = b.make_pool_month(seats={"business": "30"},
                                    consumed_report_nano=w.consumed_nano(), finality="open",
                                    overage_forecast=unpriced("no forecast"))
    specs = _design(w, [unpriced_pm])
    assert _budgets(specs, "enterprise") == {} and _budgets(specs, "cost_center") == {}
    assert any("overage forecast unpriced" in s["text"] for s in specs)
    point = b.make_pool_month(seats={"business": "30"}, consumed_report_nano=w.consumed_nano(),
                              overage_forecast=estimated(20 * USD, Basis.LIST, note="point"))
    # point $20: cost-center budgets $13 + $6 + $5 = $24 > $22
    assert _budgets(_design(w, [point]), "enterprise")["<enterprise slug>"] == 24


def test_capped_cost_center_entities_and_org_entities() -> None:
    w = three_cost_centers()
    ent = b.make_pool_month(seats={"business": "20"}, consumed_report_nano=w.consumed_nano())
    capped = b.make_pool_month(entity_id="cc:A", seats={"business": "10"},
                               consumed_report_nano=34_200 * 10**7, capped_policy="block")
    specs = _design(w, [ent, capped])
    cc = [s for s in specs if s["entity_id"] == "cc:A" and s["kind"] == "budget"]
    assert len(cc) == 1 and "'block'" in cc[0]["text"]
    assert cc[0]["request"]["body"]["budget_amount"] == 168   # ($342 − $190) × 1.1
    org = b.make_pool_month(entity_id="org:acme", seats={"business": "30"},
                            consumed_report_nano=w.consumed_nano())
    specs = _design(w, [org])
    assert _budgets(specs, "organization") == {"acme": 47}
    vol_cc = b.make_pool_month(entity_id="cc:A", billing_mode="volume")
    assert [s["kind"] for s in _design(w, [vol_cc])] == ["note"]
    unpriced_cc = b.make_pool_month(entity_id="cc:A", finality="open",
                                    overage_forecast=unpriced("x"))
    assert any("overage forecast unpriced" in s["text"] for s in _design(w, [unpriced_cc]))


def test_allowance_falls_back_to_users_times_pool_per_seat_and_unknown_seats() -> None:
    w = BudgetWorld()
    w.cost_center("A", [3_000] * 9 + [7_200])            # no seat lines: 10 users × 1,900
    w.cost_center("B", [1_000] * 10)
    specs = _design(w, [b.make_pool_month(seats={"business": "20"},
                                          consumed_report_nano=w.consumed_nano())])
    assert any(s["kind"] == "cost_center_pool" and "A drew 180%" in s["text"] for s in specs)
    empty_pool = b.make_pool_month(seats={}, consumed_report_nano=w.consumed_nano(),
                                   regime="overage", pool_credits="0", pool_nano=0)
    specs = _design(w, [empty_pool])
    assert any("allowance unknown" in s["text"] for s in specs)


def test_latest_month_per_entity_and_no_per_user_data() -> None:
    w = three_cost_centers()
    old = b.make_pool_month(month="2026-08", seats={"business": "30"})
    specs = budget_design([old] + budget_pools(w), w.cells(), w.config, k=5)
    assert {s["month"] for s in specs} == {"2026-09"}
    assert _budgets(specs, "multi_user_cost_center") == {}
    assert any("no user-level budgets sized" in s["text"] for s in specs)


def test_order_independence_and_validation() -> None:
    w = three_cost_centers()
    pools = budget_pools(w, scenarios=True)
    a = budget_design(pools, w.cells(), w.config, k=5, cost_lines=w.lines)
    z = budget_design(list(reversed(pools)), list(reversed(w.cells())),
                      list(reversed(w.config)), k=5, cost_lines=list(reversed(w.lines)))
    assert a == z
    for bad in (dict(k=0), dict(k="5"), dict(k=True)):
        with pytest.raises(UsageError):
            budget_design(pools, [], [], **bad)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        budget_design(["pool"], [], [], k=5)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        budget_design("pools", [], [], k=5)  # type: ignore[arg-type]
    assert budget_design(None, None, None, k=5) == []  # type: ignore[arg-type]

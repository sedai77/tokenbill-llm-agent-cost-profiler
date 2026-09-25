"""Budget kinds (addendum §10.1 budget rows, R14, ruling R-E16): per-user budget facts only as
counts at team (≥ k) or entity level, never a ``p_``; metered-only cost-center advice; per-scenario
exposure and sizing checks."""

from __future__ import annotations

import json
import re

from tokenbill.core import kanon
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import to_json

from .helpers import (
    USD,
    C,
    budget,
    by_scenario,
    cost_center,
    ctx,
    detect,
    dims,
    evidence,
    flags,
    of_kind,
    p_plan,
    p_pool,
    people,
    rows,
    seats,
)
from .test_pool_kinds import _p9_ctx

_P_RE = re.compile(r"p_[0-9a-f]{20}")


def _no_person(findings: list) -> None:
    blob = json.dumps([to_json(f) for f in findings], sort_keys=True)
    assert not _P_RE.search(blob)


def _zero_budgets(n: int, team: str = "platform") -> list:
    return [budget(f"z{team}{i}", "user", 0, stop=True, team=team) for i in range(n)]


def test_zero_user_budgets_five_in_one_team_at_team_scope() -> None:
    found = detect(ctx(config=_zero_budgets(5)))
    [f] = of_kind(found, "budget-zero-user-budget")
    assert dims(f) == {"entity": "enterprise", "product": "copilot", "team": "platform"}
    assert (f.n_users, f.n_events) == (5, 5)
    assert evidence(f, "zero_user_budgets") == {"n": 5, "scope": "user"}
    assert f.cost_observed.nano is None and "count only" in f.cost_observed.note
    assert (f.category, f.recoverable) == ("aggregate", None)
    assert "always hard-stop" in f.summary
    _no_person(found)
    published = kanon.rescope_findings(found, k=5)
    assert any(x.kind == "budget-zero-user-budget" for x in published)


def test_zero_user_budgets_three_only_at_entity_scope() -> None:
    found = detect(ctx(config=_zero_budgets(3), licenses=seats(20, bucket="0-7")))
    [f] = of_kind(found, "budget-zero-user-budget")
    assert dims(f) == {"entity": "enterprise", "product": "copilot"}
    assert f.n_events == 3 and f.n_users == 20
    assert evidence(f, "zero_user_budgets") == {"n": 3, "multi_user": 0, "teams_published": 0}
    assert "team" not in f.summary.split("$0")[0] or "platform" not in f.summary
    assert "platform" not in json.dumps(to_json(f))
    _no_person(found)


def test_zero_user_budgets_mixed_teams_remainder_at_entity() -> None:
    conf = _zero_budgets(5, "platform") + _zero_budgets(2, "infra") + [
        budget("uni", "multi_user_customer", 0, stop=True)]
    found = of_kind(detect(ctx(config=conf)), "budget-zero-user-budget")
    by_team = {dims(f).get("team"): f for f in found}
    assert set(by_team) == {"platform", None}
    rest = by_team[None]
    assert evidence(rest, "zero_user_budgets") == {"n": 3, "multi_user": 1, "teams_published": 1}
    assert "multi-user budgets that block every user" in rest.summary
    assert "infra" not in json.dumps(to_json(rest))


def test_zero_budgets_ignore_non_copilot_skus_and_nonzero_amounts() -> None:
    conf = [budget("a", "user", 0, stop=True, team="t", sku="actions_linux"),
            budget("b", "user", 10, stop=True, team="t")]
    assert of_kind(detect(ctx(config=conf)), "budget-zero-user-budget") == []


def test_stop_usage_off_counts_metered_budgets() -> None:
    conf = [budget("e", "enterprise", 1000, stop=False),
            budget("c", "cost_center", 200, stop=None, target="cc:data"),
            budget("o", "organization", 300, stop=True, target="org:org-a"),
            budget("u", "user", 50, stop=True)]
    [f] = of_kind(detect(ctx(config=conf)), "budget-stop-usage-off")
    assert dims(f) == {"entity": "enterprise", "product": "copilot"}
    assert f.n_events == 2
    assert evidence(f, "scope:enterprise") == {"n": 1}
    assert evidence(f, "scope:cost_center") == {"n": 1}
    assert "charges continue past the limit" in f.summary


def test_paid_usage_uncapped_exposure_is_the_forecast_p90() -> None:
    found = detect(_p9_ctx(config=(budget("e", "enterprise", 1000, stop=False),)))
    [f] = of_kind(found, "budget-paid-usage-uncapped")
    fig = f.cost_observed
    assert (fig.nano, fig.evidence, fig.basis) == (6_000 * USD, Evidence.ESTIMATED, Basis.LIST)
    assert f.lever_ids == ("copilot.budget_plan",)
    assert "1 metered budgets without stop" in f.summary
    assert "POST /enterprises/{enterprise}/settings/billing/budgets" in f.fix.text  # type: ignore


def test_paid_usage_capped_by_a_stopping_budget_or_statement_or_policy() -> None:
    stop = (budget("e", "enterprise", 1000, stop=True),)
    assert of_kind(detect(_p9_ctx(config=stop)), "budget-paid-usage-uncapped") == []
    stated = (budget("e", "enterprise", 1000), flags({"budget_stop.enterprise": True}))
    assert of_kind(detect(_p9_ctx(config=stated)), "budget-paid-usage-uncapped") == []
    off = (budget("e", "enterprise", 1000), flags({"paid_usage_policy": "disabled"}))
    assert of_kind(detect(_p9_ctx(config=off)), "budget-paid-usage-uncapped") == []


def _scenario_pools(consumed: int = 100_000) -> list:
    return [p_pool(consumed, month="2026-09", seats_map={"unknown": "100"}, plan_scenario=s)
            for s in ("business", "enterprise")]


def test_ulb_gap_per_scenario() -> None:
    """Universal user-level budget $50 × 100 seats = $5,000; pool $1,900 (Business) or $3,900
    (Enterprise); metered budgets $1,000 → gaps $2,100 and $100."""
    conf = [budget("uni", "multi_user_customer", 50, stop=True),
            budget("e", "enterprise", 1000, stop=True)]
    plans = [p_plan(plan="unknown", source="none", seats_map={"unknown": 100})]
    gaps = by_scenario(of_kind(detect(ctx(pools=_scenario_pools(), plans=plans, config=conf)),
                               "budget-ulb-gap"))
    assert {s: f.cost_observed.nano for s, f in gaps.items()} == {
        "business": 2_100 * USD, "enterprise": 100 * USD}
    biz = gaps["business"]
    assert biz.cost_observed.evidence is Evidence.ESTIMATED
    assert evidence(biz, "caps") == {"nano": 5_000 * USD, "budgets": 1}
    assert "GitHub's sizing check" in biz.summary and biz.title.startswith("If Business: ")


def test_ulb_gap_with_cost_center_ulb_counts() -> None:
    conf = [budget("i1", "user", 100, stop=True), budget("i2", "user", 100, stop=True),
            budget("cc", "multi_user_cost_center", 20, stop=True, target="cc:data"),
            budget("uni", "multi_user_customer", 10, stop=True),
            cost_center("data", n_users=30)]
    pm = p_pool(0, seats_map={"business": "10"})     # 10 seats: pool 19,000 credits = $190
    found = of_kind(detect(ctx(pools=[pm], plans=[p_plan(plan="business", source="seats_api",
                                                          seats_map={"business": 100})],
                               config=conf)), "budget-ulb-gap")
    [f] = found
    # 100 seats: 2 × $100 + 30 × $20 + 68 × $10 = $1,480; − pool $190 = $1,290
    assert evidence(f, "caps")["nano"] == 1_480 * USD
    assert f.cost_observed.nano == 1_290 * USD


def test_ulb_gap_not_applicable_when_users_are_uncapped() -> None:
    conf = [budget("i1", "user", 5000, stop=True)]
    assert of_kind(detect(ctx(pools=[p_pool(0, seats_map={"business": "10"})], config=conf)),
                   "budget-ulb-gap") == []


def test_org_budgets_with_multi_org_seats() -> None:
    both = seats(3, org="org-a", seed="m") + seats(3, org="org-b", seed="m")
    conf = [budget("o", "organization", 300, target="org:org-a")]
    found = detect(ctx(config=conf, licenses=both + seats(10, seed="solo", bucket="0-7")))
    [f] = of_kind(found, "budget-org-multi-org-seats")
    assert evidence(f, "multi_org_users") == {"n": 3}
    assert evidence(f, "org_budgets") == {"n": 1}
    [dup] = of_kind(found, "duplicate-seat")
    assert dup.n_users == 3 and "bills such a seat once" in dup.summary
    _no_person(found)
    assert of_kind(detect(ctx(config=[budget("e", "enterprise", 10)], licenses=both)),
                   "budget-org-multi-org-seats") == []


def _cc_world(*, billing_mode: str = "metered", pool_enabled: bool = False,
              draw: int = 40_000) -> list:
    lics = seats(10, cost_center="data", seed="cc", bucket="0-7")
    usage, _ = rows(draw, date="2026-09-10", users=people(10, "cc"), cost_center="data")
    pm = p_pool(3_100_000, billing_mode=billing_mode)          # C.P1 entity in overage
    return detect(ctx(pools=[pm], plans=[p_plan()], licenses=lics, cost_lines=usage,
                      config=[cost_center("data", pool_enabled=pool_enabled)]))


def test_no_cost_center_pool_excess_draw_list_equivalent() -> None:
    [f] = of_kind(_cc_world(), "budget-no-cost-center-pool")
    assert dims(f) == {"cost_center": "data", "entity": "enterprise", "product": "copilot"}
    fig = f.cost_observed
    # allowance 10 × 1,900 = 19,000 credits; draw 40,000 → excess 21,000 credits = $210
    assert (fig.nano, fig.evidence, fig.basis) == (210 * USD, Evidence.ESTIMATED,
                                                   Basis.LIST_EQUIVALENT)
    assert evidence(f, "draw") == {"credits": "40000", "month": "2026-09"}
    assert evidence(f, "allowance") == {"credits": "19000", "seats": 10}
    assert "211%" in f.title and "list-equivalent" in f.summary
    assert f.lever_ids == ("copilot.budget_plan",)


def test_no_cost_center_pool_not_below_150_percent_or_with_pool_on() -> None:
    assert of_kind(_cc_world(draw=28_500), "budget-no-cost-center-pool") == []
    assert of_kind(_cc_world(pool_enabled=True), "budget-no-cost-center-pool") == []


def test_no_cost_center_pool_suppressed_for_volume_billing() -> None:
    found = _cc_world(billing_mode="volume")
    assert of_kind(found, "budget-no-cost-center-pool") == []
    [dq] = of_kind(found, "dq.skipped-kinds")
    assert "billing mode volume" in evidence(dq, "kind:budget-no-cost-center-pool")["missing"]


def test_enterprise_budget_misread() -> None:
    conf = [budget("e", "enterprise", 500, stop=False)]
    [f] = of_kind(detect(ctx(plans=[p_plan()], config=conf)), "budget-enterprise-misread")
    assert evidence(f, "seat_fees") == {"low_nano": 26_800 * USD, "high_nano": 26_800 * USD}
    assert "not the total bill" in f.summary and dims(f)["entity"] == "enterprise"
    assert of_kind(detect(ctx(plans=[p_plan()], config=[budget("e", "enterprise", 500,
                                                                 stop=True)])),
                   "budget-enterprise-misread") == []
    assert of_kind(detect(ctx(plans=[p_plan()], config=[budget("e", "enterprise", 30_000)])),
                   "budget-enterprise-misread") == []


def test_enterprise_misread_with_unknown_plan_uses_every_scenario() -> None:
    plans = [p_plan(plan="unknown", source="none", seats_map={"unknown": 100})]
    # fees $1,900 (Business) to $3,900 (Enterprise): $2,000 is below only one scenario
    assert of_kind(detect(ctx(plans=plans, config=[budget("e", "enterprise", 2000)])),
                   "budget-enterprise-misread") == []
    [f] = of_kind(detect(ctx(plans=plans, config=[budget("e", "enterprise", 1000)])),
                  "budget-enterprise-misread")
    assert "$1,900.00 if Business to $3,900.00 if Enterprise" in f.summary


def test_budget_findings_pass_kanon_privacy() -> None:
    conf = _zero_budgets(5) + [budget("e", "enterprise", 100),
                               budget("uni", "multi_user_customer", 0, stop=True)]
    found = detect(_p9_ctx(config=tuple(conf)))
    published = kanon.rescope_findings(found, k=5)
    assert {f.kind for f in published} >= {"budget-zero-user-budget", "budget-stop-usage-off",
                                           "budget-paid-usage-uncapped"}
    _no_person(published)
    assert C == 10**7

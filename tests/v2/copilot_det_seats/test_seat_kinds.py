"""Seat criteria and the person-level seat kinds (addendum §10.1 ``idle-seat`` criteria,
``completions-only-seat``, ``plan-mix``, ``duplicate-seat``; §10.1 "Seat kinds from the activity
report")."""

from __future__ import annotations

import dataclasses

from tokenbill.core import builders as b
from tokenbill.core.labels import Basis, Evidence

from .helpers import (
    USD,
    C,
    activity_report,
    ctx,
    detect,
    dims,
    evidence,
    of_kind,
    org_settings,
    p_plan,
    p_pool,
    people,
    rows,
    seats,
)

POLICY = [org_settings("org-a", "assign_selected")]
POOL = [p_pool(2_000_000)]         # C.P3's slack regime


def _idle(lics: list, *, cost: list | None = None, conf: list | None = None) -> list:
    cost = cost if cost is not None else rows(10, date="2026-09-20")[0]
    found = detect(ctx(pools=POOL, plans=[p_plan()], licenses=lics, cost_lines=cost,
                       config=POLICY if conf is None else conf, today="2026-10-05"))
    return of_kind(found, "idle-seat")


def test_idle_criteria_exclusions() -> None:
    lics = (seats(5, seed="ok")
            + seats(2, seed="pend", pending="2026-10-31")        # pending cancellation
            + seats(2, seed="young", created="2026-09-15")       # created < 30 days ago
            + seats(2, seed="active", bucket="8-30")             # active in the last 30 days
            + seats(2, seed="cost"))                             # reported cost in 30 days
    cost, _ = rows(40, date="2026-09-25", users=people(2, "cost"))
    [f] = _idle(lics, cost=cost)
    assert f.n_users == 5 and not f.needs_eval
    assert evidence(f, "bucket:none_90d") == {"n": 5}


def test_metrics_estimates_count_as_reported_cost() -> None:
    lics = seats(3, seed="m")
    act = [b.make_activity(p, date_utc="2026-09-28", reported_cost_nano=5 * C)
           for p in people(1, "m")]
    found = detect(ctx(pools=POOL, plans=[p_plan()], licenses=lics, activity=act,
                       config=POLICY, today="2026-10-05"))
    [f] = of_kind(found, "idle-seat")
    assert f.n_users == 2


def test_zero_cost_unverified_without_any_report() -> None:
    [f] = _idle(seats(5), cost=[])
    assert f.needs_eval and evidence(f, "caveats") == {"age_unknown": 0,
                                                      "zero_cost_unverified": 5}
    assert "Zero cost unverified" in f.summary


def test_seat_age_from_two_reports() -> None:
    now = seats(5, created=None, date="2026-09-30")
    earlier = seats(5, created=None, date="2026-08-25", bucket="8-30")
    [f] = _idle(now + earlier)
    assert not f.needs_eval
    [g] = _idle(now + seats(5, created=None, date="2026-09-10", bucket="8-30"))
    assert evidence(g, "caveats")["age_unknown"] == 5


def test_direct_seat_with_unknown_org_policy_is_unknown_assignment() -> None:
    [f] = _idle(seats(5), conf=[])
    assert evidence(f, "assignment:unknown")["n"] == 5
    assert f.recoverable is not None and f.recoverable.upper_bound
    assert f.recoverable.nano == 95 * USD and f.needs_eval


def test_multi_org_idle_seat_counted_once() -> None:
    both = seats(4, org="org-a", seed="m") + seats(4, org="org-b", seed="m")
    [f] = _idle(both, conf=POLICY + [org_settings("org-b", "assign_selected")])
    assert f.n_users == 4
    assert evidence(f, "org:org-a")["removable"] == 4


def test_seats_api_row_wins_over_the_activity_report() -> None:
    api = seats(5, seed="s", bucket="0-7")
    report = activity_report(5, seed="s", date="2026-09-30", bucket="none_90d")
    assert _idle(api + report) == []


def test_unknown_plan_seats_in_a_known_single_plan_entity() -> None:
    report = activity_report(5, seed="r", date="2026-09-30")
    pm = p_pool(20_000, seats_map={"business": "40"})
    found = detect(ctx(pools=[pm], plans=[p_plan(plan="business", source="seat_lines",
                                                  seats_map={"business": 40})],
                       licenses=report, cost_lines=rows(1, date="2026-09-20")[0],
                       config=[org_settings("org-a", "assign_all")], today="2026-10-05"))
    [f] = of_kind(found, "idle-seat")
    assert f.cost_observed.nano == 95 * USD        # unknown seats priced at the known plan
    [auto] = of_kind(found, "seat-auto-assign")
    assert auto.recoverable is not None and auto.recoverable.nano == 95 * USD


def test_unknown_plan_seats_in_a_mixed_entity_stay_unpriced() -> None:
    report = activity_report(5, seed="r", date="2026-09-30")
    [f] = _idle(report, conf=[org_settings("org-a", "assign_all")])
    fees = f.cost_observed
    assert (fees.nano, fees.low_nano, fees.high_nano) == (None, 95 * USD, 195 * USD)
    [auto] = of_kind(detect(ctx(pools=POOL, plans=[p_plan()], licenses=report,
                                cost_lines=rows(1, date="2026-09-20")[0],
                                config=[org_settings("org-a", "assign_all")])),
                     "seat-auto-assign")
    assert auto.recoverable is not None and auto.recoverable.nano is None
    assert "mixed entity" in auto.recoverable.note


def test_no_pool_month_projection_unpriced() -> None:
    found = detect(ctx(plans=[p_plan()], licenses=seats(5), config=POLICY,
                       cost_lines=rows(1, date="2026-09-20")[0]))
    [f] = of_kind(found, "idle-seat")
    assert f.recoverable is not None and f.recoverable.nano is None
    assert "no pool month" in f.recoverable.note


# ---------------------------------------------------------------------------------------------
# completions-only-seat
# ---------------------------------------------------------------------------------------------


def _completions(plan: str, plans: list, *, n: int = 6) -> list:
    users = people(n, "c")
    lics = seats(n, seed="c", plan=plan, bucket="0-7")
    act = [b.make_activity(p, date_utc="2026-09-20", counts={"code_generation": 40})
           for p in users]
    act.append(b.make_activity(users[0], date_utc="2026-09-21", counts={"interactions": 1},
                               flags=()))
    chat = people(3, "chat")
    lics += seats(3, seed="chat", plan=plan, bucket="0-7")
    act += [b.make_activity(p, date_utc="2026-09-20", counts={"code_generation": 5},
                            flags=("used_chat",)) for p in chat]
    act.append(b.make_activity(b.make_principal("noseat"), date_utc="2026-09-20",
                               counts={"code_generation": 5}))
    return of_kind(detect(ctx(plans=plans, licenses=lics, activity=act)),
                   "completions-only-seat")


def test_completions_only_seats_enterprise_advice() -> None:
    [f] = _completions("enterprise", [p_plan(plan="enterprise", source="seats_api",
                                             seats_map={"enterprise": 9})])
    assert (f.n_users, dims(f)) == (6, {"entity": "enterprise", "product": "copilot",
                                         "team": "t1"})
    assert evidence(f, "plan:enterprise") == {"n": 6}
    assert f.fix is not None and "Business seat instead of Enterprise" in f.fix.text
    assert (f.category, f.lever_class, f.cost_observed.nano) == ("lever", "none", None)


def test_completions_only_seats_no_plan_advice_while_unknown() -> None:
    [f] = _completions("unknown", [p_plan(plan="unknown", source="none",
                                          seats_map={"unknown": 9})])
    assert f.fix is not None and "no plan advice" in f.fix.text
    assert "Enterprise" not in f.fix.text and "No plan advice" in f.summary
    [g] = _completions("business", [p_plan(plan="business", source="seats_api",
                                           seats_map={"business": 9})])
    assert g.fix is not None and "instead of Enterprise" not in g.fix.text


# ---------------------------------------------------------------------------------------------
# plan-mix
# ---------------------------------------------------------------------------------------------

_MONTHS = ("2026-09", "2026-10", "2026-11")


def _plan_mix(**kw: object) -> list:
    return detect(_plan_mix_ctx(**kw))  # type: ignore[arg-type]


def _plan_mix_ctx(*, heavy: int = 2_500, months: tuple[str, ...] = _MONTHS,
                  plan: str = "mixed", estimates: bool = False):
    light, heavy_user = people(5, "light"), people(1, "heavy")
    lics = (seats(5, seed="light", plan="enterprise", bucket="0-7", date="2026-11-30")
            + seats(1, seed="heavy", plan="enterprise", bucket="0-7", date="2026-11-30"))
    cost, act = [], []
    for m in months:
        if estimates and m == months[0]:
            act += [b.make_activity(p, date_utc=f"{m}-10", reported_cost_nano=1_500 * C)
                    for p in light]
            continue
        cost += rows(1_800 * 5, date=f"{m}-10", users=light)[0]
        cost += rows(heavy, date=f"{m}-11", users=heavy_user)[0]
    pools = [p_pool(50_000, month=m, seats_map={"business": "40", "enterprise": "10"})
             for m in months]
    plans = [p_plan(month=m, plan=plan, seats_map={"business": 40, "enterprise": 10})
             for m in months]
    return ctx(pools=pools, plans=plans, licenses=lics, cost_lines=cost, activity=act,
               today="2026-12-10")


def test_plan_mix_enterprise_seats_within_the_business_allowance() -> None:
    [f] = of_kind(_plan_mix(), "plan-mix")
    assert dims(f) == {"entity": "enterprise", "plan": "enterprise", "product": "copilot",
                       "team": "t1"}
    assert f.n_users == 5
    assert (f.cost_observed.nano, f.cost_observed.evidence, f.cost_observed.basis) == (
        100 * USD, Evidence.ESTIMATED, Basis.LIST)
    rec = f.recoverable
    # slack: the downgrade saves 5 × $20; the pool shrinks by 5 × 2,000 credits, still slack
    assert rec is not None and rec.nano == 100 * USD and "trade-off" in rec.note
    assert f.needs_eval and f.lever_ids == ("copilot.seat_downgrade",)
    assert evidence(f, "months") == {"closed": ",".join(_MONTHS), "source": "report rows"}


def test_plan_mix_heavy_user_and_estimates() -> None:
    [f] = of_kind(_plan_mix(heavy=1_000), "plan-mix")
    assert f.n_users == 6
    [g] = of_kind(_plan_mix(estimates=True), "plan-mix")
    assert evidence(g, "months")["source"] == "metrics estimates"


def test_plan_mix_months_and_known_plan() -> None:
    [short] = of_kind(_plan_mix(months=_MONTHS[1:]), "plan-mix")
    assert "fewer than 3 closed months available" in short.summary
    assert short.confidence == "low" and evidence(short, "months")["closed"] == "2026-10,2026-11"
    assert of_kind(_plan_mix(months=_MONTHS[2:]), "plan-mix") == []
    assert of_kind(_plan_mix(plan="unknown"), "plan-mix") == []


def test_plan_mix_leaves_idle_enterprise_seats_to_idle_seat() -> None:
    idle = seats(3, seed="idle-e", plan="enterprise", date="2026-11-30")
    found = detect(dataclasses.replace(_plan_mix_ctx(), licenses=_plan_mix_ctx().licenses + tuple(
        idle)))
    [f] = of_kind(found, "plan-mix")
    assert f.n_users == 5

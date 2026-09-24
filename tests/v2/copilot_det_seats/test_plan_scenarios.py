"""Plan status and scenarios (R17, ruling R-E22; addendum §10.0 "Scenarios", §10.1 ``plan-status``;
Appendix C.P13, P13b, P14), with pool months and plan evidence from the real ``core.pool``."""

from __future__ import annotations

from tokenbill.core import builders as b
from tokenbill.core.labels import Basis, Evidence, Finality
from tokenbill.detect.copilot_seats import SCENARIO_KINDS

from .helpers import (
    METERED,
    USD,
    activity_report,
    by_scenario,
    ctx,
    detect,
    dims,
    enrich,
    evidence,
    flags,
    of_kind,
    org_settings,
    people,
    rows,
    seats,
)

_UNKNOWN_NOTE = ("assumes direct assignment in an assign_selected org; confirm with the seats API "
                 "assigning_team")


def _p13(*, earlier_report: bool = True, extra_conf: list | None = None):
    """C.P13: 2026-10 (closed), 100 activity-report seats (plan unknown), pooled use 250,000
    credits, metered; 10 of the seats idle."""
    users = people(90, "r")
    cost, aggs = rows(250_000, date="2026-10-10", users=users[:50], discount=250_000)
    lics = activity_report(90, seed="r", bucket="0-7") + activity_report(10, seed="idle")
    if earlier_report:   # listed in two reports ≥ 30 days apart: the seat age is known
        lics += activity_report(90, seed="r", date="2026-09-15", bucket="0-7")
        lics += activity_report(10, seed="idle", date="2026-09-15", bucket="31-90")
    conf = [METERED, *(extra_conf or [])]
    pms, plans = enrich(cost, aggs, lics, conf, today="2026-11-10")
    return ctx(pools=pms, plans=plans, licenses=lics, config=conf, cost_lines=cost,
               today="2026-11-10")


def test_p13_two_pool_regime_findings() -> None:
    found = detect(_p13())
    pr = by_scenario(of_kind(found, "pool-regime"))
    assert set(pr) == {"business", "enterprise"}
    biz, ent = pr["business"], pr["enterprise"]
    assert (biz.cost_observed.nano, biz.cost_observed.evidence, biz.cost_observed.basis) == (
        600 * USD, Evidence.ESTIMATED, Basis.LIST)
    assert (ent.cost_observed.nano, ent.cost_observed.evidence) == (0, Evidence.ESTIMATED)
    assert evidence(biz, "regime")["regime"] == "overage"
    assert evidence(ent, "regime")["regime"] == "slack"
    assert biz.title.startswith("If Business: ") and ent.title.startswith("If Enterprise: ")
    assert "if all unknown seats are Business" in biz.summary
    assert "if all unknown seats are Enterprise" in ent.summary
    assert evidence(biz, "pool")["credits"] == "190000"
    assert evidence(ent, "pool")["credits"] == "390000"
    assert dims(biz) == {"entity": "enterprise", "plan_scenario": "business",
                         "product": "copilot"}


def test_p13b_idle_seat_projection_per_scenario() -> None:
    idle = by_scenario(of_kind(detect(_p13()), "idle-seat"))
    assert set(idle) == {"business", "enterprise"}
    for scen, saving, fees in (("business", 0, 190), ("enterprise", 390, 390)):
        f = idle[scen]
        rec = f.recoverable
        assert rec is not None and (rec.nano, rec.evidence, rec.basis, rec.upper_bound) == (
            saving * USD, Evidence.ESTIMATED, Basis.LIST, True)
        assert _UNKNOWN_NOTE in rec.note and f"scenario {scen}" in rec.note
        assert f.needs_eval and f.cost_observed.nano == fees * USD
        assert evidence(f, "assignment:unknown")["n"] == 10
        assert evidence(f, "assignment:unknown")["upper_bound"] == "true"
        assert evidence(f, "assignment:removable") == {"n": 0, "projection": "none"}
        assert "Assignment not in the source" in f.summary
        assert "Seat age unknown" not in f.summary
        assert f.lever_ids == ("copilot.seat_reclaim",)
        assert "before 2026-12-01T00:00Z" in evidence(f, "deadline")["text"]


def test_p13_plan_status_unknown_and_suppressions() -> None:
    found = detect(_p13())
    [ps] = of_kind(found, "plan-status")
    assert dims(ps) == {"entity": "enterprise", "product": "copilot"}
    assert "unknown" in ps.title and "both scenarios" in ps.title
    fees = ps.cost_observed
    assert (fees.nano, fees.low_nano, fees.high_nano) == (None, 1_900 * USD, 3_900 * USD)
    assert (fees.evidence, fees.basis) == (Evidence.ESTIMATED, Basis.LIST)
    assert fees.note.startswith("unpriced: plan unknown")
    assert evidence(ps, "plan") == {"plan": "unknown", "source": "none", "conflict": "false",
                                    "month": "2026-10"}
    assert evidence(ps, "seats:unknown") == {"n": 100}
    assert "seats API plan_type" in ps.summary and "answers.json" in ps.summary
    assert ps.fix is not None and "--plan" in ps.fix.text
    assert (ps.category, ps.confidence, ps.recoverable) == ("aggregate", "low", None)
    assert of_kind(found, "plan-mix") == []
    # no finding without a plan_scenario dim carries a pool figure
    plain = [f for f in found if "plan_scenario" not in dims(f)]
    assert {f.kind for f in plain} & SCENARIO_KINDS == set()
    assert all(f.recoverable is None and f.headroom is None for f in plain)


def test_p13_seat_age_unknown_without_a_second_report() -> None:
    idle = by_scenario(of_kind(detect(_p13(earlier_report=False)), "idle-seat"))
    for f in idle.values():
        assert "Seat age unknown: 10." in f.summary and f.needs_eval
        assert evidence(f, "caveats") == {"age_unknown": 10, "zero_cost_unverified": 0}


def test_p13_org_policy_known_makes_activity_seats_auto_assigned() -> None:
    found = detect(_p13(extra_conf=[org_settings("org-a", "assign_all", stated=True)]))
    idle = by_scenario(of_kind(found, "idle-seat"))
    assert evidence(idle["business"], "assignment:auto")["n"] == 10
    auto = by_scenario(of_kind(found, "seat-auto-assign"))
    assert {s: f.recoverable.nano for s, f in auto.items()} == {  # type: ignore[union-attr]
        "business": 0, "enterprise": 390 * USD}


def _p14(*, reconciled: bool = False):
    cost = [b.make_seat_line("enterprise", "50", date_utc="2026-09-01")]
    usage, aggs = rows(100_000, date="2026-09-10", discount=100_000)
    lics = seats(50, plan="business", date="2026-09-05", bucket="0-7")
    conf = [flags({"plan.enterprise": "business"})]
    pms, plans = enrich(cost + usage, aggs, lics, conf, today="2026-10-10")
    return ctx(pools=pms, plans=plans, licenses=lics, config=conf, cost_lines=cost + usage,
               today="2026-10-10", reconciled=("github_copilot",) if reconciled else ())


def test_p14_conflict_named_seat_lines_won() -> None:
    found = detect(_p14())
    [ps] = of_kind(found, "plan-status")
    assert "enterprise from seat_lines" in ps.title and "conflicting evidence" in ps.title
    assert "decided by seat_lines" in ps.summary and "dq.copilot_plan_conflict" in ps.summary
    assert evidence(ps, "plan") == {"plan": "enterprise", "source": "seat_lines",
                                    "conflict": "true", "month": "2026-09"}
    lines = [dict(e.attrs)["line"] for e in ps.evidence if e.ref == "evidence"]
    assert "seat_lines: copilot_enterprise 50" in lines
    assert any(line.startswith("dq.copilot_plan_conflict: seat_lines vs") for line in lines)
    fees = ps.cost_observed
    assert (fees.nano, fees.evidence, fees.basis, fees.finality) == (
        1_950 * USD, Evidence.EXACT, Basis.LIST, Finality.FINAL)
    assert "unreconciled" in fees.note
    [pr] = of_kind(found, "pool-regime")
    assert "plan_scenario" not in dims(pr)
    assert evidence(pr, "pool")["credits"] == "195000"
    assert evidence(pr, "regime")["plan_conflict"] == "true"


def test_p14_reconciled_seat_lines_are_invoice() -> None:
    [ps] = of_kind(detect(_p14(reconciled=True)), "plan-status")
    assert (ps.cost_observed.basis, ps.cost_observed.evidence) == (Basis.INVOICE, Evidence.EXACT)


def test_known_plan_from_seats_api_no_recommendation() -> None:
    usage, aggs = rows(10_000, date="2026-09-10", discount=10_000)
    lics = seats(20, plan="business", date="2026-09-05", bucket="0-7")
    pms, plans = enrich(usage, aggs, lics, [METERED], today="2026-10-10")
    [ps] = of_kind(detect(ctx(pools=pms, plans=plans, licenses=lics, config=[METERED],
                              cost_lines=usage, today="2026-10-10")), "plan-status")
    assert ps.title == "Copilot plan of enterprise: business from seats_api (2026-09)"
    assert "No plan recommendation" in ps.summary
    assert (ps.cost_observed.nano, ps.cost_observed.evidence) == (380 * USD, Evidence.ESTIMATED)
    assert ps.confidence == "high"


def test_plan_status_falls_back_to_detect_plans() -> None:
    """Without enricher plans the detector asks ``core.pool.detect_plans`` itself."""
    lics = seats(12, plan="enterprise", date="2026-09-05", bucket="0-7")
    [ps] = of_kind(detect(ctx(licenses=lics)), "plan-status")
    assert evidence(ps, "plan")["plan"] == "enterprise"
    assert ps.cost_observed.nano == 12 * 39 * USD


def test_plan_status_lists_earlier_months() -> None:
    from .helpers import p_plan

    plans = [p_plan(month="2026-08", plan="unknown", source="none",
                    seats_map={"unknown": 100}),
             p_plan(month="2026-09", plan="business", source="seats_api",
                    seats_map={"business": 100})]
    [ps] = of_kind(detect(ctx(plans=plans)), "plan-status")
    assert evidence(ps, "plan")["month"] == "2026-09"
    assert evidence(ps, "month:2026-08") == {"plan": "unknown", "source": "none",
                                             "conflict": "false"}


def test_known_seats_plus_unknown_seats_range() -> None:
    from .helpers import p_plan

    pe = p_plan(plan="unknown", source="seats_api",
                seats_map={"business": 10, "unknown": 5})
    [ps] = of_kind(detect(ctx(plans=[pe])), "plan-status")
    fees = ps.cost_observed
    assert (fees.nano, fees.low_nano, fees.high_nano) == (None, 285 * USD, 385 * USD)
    assert "seats: business 10, unknown 5; source seats_api" in ps.summary


def test_scenario_kinds_carry_the_dim_and_estimated_figures() -> None:
    found = detect(_p13())
    for f in found:
        if f.kind in SCENARIO_KINDS:
            assert dims(f).get("plan_scenario") in ("business", "enterprise")
            for fig in (f.cost_observed, f.recoverable):
                if fig is not None and fig.nano is not None:
                    assert fig.evidence is Evidence.ESTIMATED


def test_provisional_seat_lines_are_provisional_list() -> None:
    cost = [b.make_seat_line("business", "10", date_utc="2026-09-01", finality="provisional")]
    [ps] = of_kind(detect(ctx(plans=[p_plan_known()], cost_lines=cost)), "plan-status")
    fig = ps.cost_observed
    assert (fig.nano, fig.evidence, fig.basis, fig.finality) == (
        190 * USD, Evidence.EXACT, Basis.LIST, Finality.PROVISIONAL)


def p_plan_known():
    from .helpers import p_plan

    return p_plan(plan="business", source="seat_lines", seats_map={"business": 10})

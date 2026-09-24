"""Plan detection (addendum CA-48, R17, ruling R-E22; F-POOL brief item 2): the precedence table,
Appendix C.P14 / P15 and the brief's P14b / P15b."""

from __future__ import annotations

import itertools

import pytest

from tokenbill.core import builders as b
from tokenbill.core.errors import UsageError
from tokenbill.core.pool import PLAN_CONFLICT_DQ, detect_plans, pool_months

from .worlds import (
    activity_seats,
    api_seats,
    cost_center,
    flags,
    org_settings,
    people,
    quota,
    rows,
    seat_count,
)

MONTH = "2026-10"
SOURCES = ("seat_lines", "seats_api", "org_settings", "report_quota", "admin_statement")
QUOTA = {"business": 1900, "enterprise": 3900}


def _source(name: str, plan: str) -> tuple[list, list, list]:
    if name == "seat_lines":
        return [b.make_seat_line(plan, "10", date_utc="2026-10-01")], [], []
    if name == "seats_api":
        return [], api_seats(10, plan), []
    if name == "org_settings":
        return [], [], [org_settings("org-a", plan)]
    if name == "report_quota":
        return [], [], [quota(MONTH, QUOTA[plan], 10)]
    return [], [], [flags({"plan.enterprise": plan})]


def _world(*parts: tuple[list, list, list]) -> tuple[list, list, list]:
    """Ten activity-report seat holders in org-a (the seat count) plus the given sources."""
    lines: list = []
    lics: list = activity_seats(10, date="2026-10-06")
    conf: list = []
    for part_lines, part_lics, part_conf in parts:
        lines += part_lines
        lics += part_lics
        conf += part_conf
    return lines, lics, conf


@pytest.mark.parametrize("hi,lo", list(itertools.combinations(SOURCES, 2)))
@pytest.mark.parametrize("plans", [("business", "enterprise"), ("enterprise", "business")])
def test_precedence_every_pair_both_orders(hi: str, lo: str, plans: tuple[str, str]) -> None:
    [ev] = detect_plans(*_world(_source(hi, plans[0]), _source(lo, plans[1])), month=MONTH)
    assert (ev.entity_id, ev.month, ev.plan, ev.source) == ("enterprise", MONTH, plans[0], hi)
    assert ev.seats == ((plans[0], 10),)
    assert ev.conflict is True
    assert any(line.startswith(hi) for line in ev.evidence)
    assert any(line.startswith(lo) for line in ev.evidence)
    assert ev.evidence[-1] == f"{PLAN_CONFLICT_DQ}: {hi} vs {lo}"


@pytest.mark.parametrize("hi,lo", list(itertools.combinations(SOURCES, 2)))
@pytest.mark.parametrize("plan", ["business", "enterprise"])
def test_agreeing_sources_are_no_conflict(hi: str, lo: str, plan: str) -> None:
    [ev] = detect_plans(*_world(_source(hi, plan), _source(lo, plan)), month=MONTH)
    assert (ev.plan, ev.source, ev.conflict) == (plan, hi, False)
    assert not any(PLAN_CONFLICT_DQ in line for line in ev.evidence)


@pytest.mark.parametrize("name", SOURCES)
def test_each_source_alone_decides(name: str) -> None:
    [ev] = detect_plans(*_world(_source(name, "enterprise")), month=MONTH)
    assert (ev.plan, ev.source, ev.seats, ev.conflict) == ("enterprise", name,
                                                           (("enterprise", 10),), False)


def test_no_evidence_is_unknown_p13() -> None:
    [ev] = detect_plans([], activity_seats(100, date="2026-10-15"), [], month=MONTH)
    assert (ev.plan, ev.source, ev.seats, ev.conflict) == ("unknown", "none",
                                                           (("unknown", 100),), False)
    assert ev.evidence == ()


def test_mixed_when_seat_lines_show_both_skus() -> None:
    lines = [b.make_seat_line("business", "6", date_utc="2026-10-01"),
             b.make_seat_line("enterprise", "4", date_utc="2026-10-01")]
    [ev] = detect_plans(lines, [], [], month=MONTH)
    assert (ev.plan, ev.source, ev.seats) == ("mixed", "seat_lines",
                                              (("business", 6), ("enterprise", 4)))
    assert ev.evidence == ("seat_lines: copilot_enterprise 4", "seat_lines: copilot_for_business 6")


def test_statement_mixed_leaves_seats_unknown_and_standalone_is_flagged() -> None:
    [ev] = detect_plans(*_world(([], [], [flags({"plan.enterprise": "mixed"})])), month=MONTH)
    assert (ev.plan, ev.source, ev.seats) == ("unknown", "admin_statement", (("unknown", 10),))
    lines = [b.make_seat_line("business", "3", date_utc="2026-10-02", sku="copilot_standalone")]
    [ev] = detect_plans(lines, [], [], month=MONTH)
    assert ev.plan == "business"
    assert ev.evidence == ("seat_lines: copilot_standalone 3 (plan business assumed, unverified)",)
    # a mixed statement conflicts with seat lines of a single plan
    [ev] = detect_plans(lines, [], [flags({"plan.enterprise": "mixed"})], month=MONTH)
    assert ev.conflict is True


def test_p14_conflicting_evidence() -> None:
    """C.P14: seats API business ×50, seat lines copilot_enterprise 50, statement business."""
    lines = [b.make_seat_line("enterprise", "50", date_utc="2026-09-01")]
    lics = api_seats(50, "business", date="2026-09-05")
    conf = [flags({"plan.enterprise": "business"})]
    [ev] = detect_plans(lines, lics, conf, month="2026-09")
    assert (ev.plan, ev.source, ev.conflict, ev.seats) == ("enterprise", "seat_lines", True,
                                                           (("enterprise", 50),))
    assert ev.evidence == (
        "seat_lines: copilot_enterprise 50", "seats_api: business 50",
        "admin_statement: plan.enterprise=business",
        f"{PLAN_CONFLICT_DQ}: seat_lines vs seats_api, admin_statement")


def test_p14b_mirror_conflict_in_org_mode() -> None:
    """F-POOL P14b: org A, seats API enterprise ×40, seat lines copilot_for_business 40, statement
    plan.org:A=enterprise → business from the seat lines, conflict naming all three."""
    lines = [b.make_seat_line("business", "40", date_utc="2026-10-01", organization="A")]
    lics = api_seats(40, "enterprise", org="A")
    conf = [flags({"plan.org:A": "enterprise"})]
    [ev] = detect_plans(lines, lics, conf, month=MONTH, entity_mode="org")
    assert (ev.entity_id, ev.plan, ev.source, ev.conflict) == ("org:A", "business", "seat_lines",
                                                               True)
    assert ev.evidence == ("seat_lines: copilot_for_business 40", "seats_api: enterprise 40",
                           "admin_statement: plan.org:A=enterprise",
                           f"{PLAN_CONFLICT_DQ}: seat_lines vs seats_api, admin_statement")


@pytest.mark.parametrize("quotas,month,plan,seats", [
    ([(3900, 40)], "2026-09", "enterprise", (("enterprise", 40),)),
    ([(1900, 30), (3900, 10)], "2026-09", "mixed", (("business", 30), ("enterprise", 10))),
    ([(7000, 40)], "2026-07", "enterprise", (("enterprise", 40),)),
    ([(3900, 36)], "2026-09", "unknown", (("enterprise", 36), ("unknown", 4))),
    ([], "2026-09", "unknown", (("unknown", 40),)),
])
def test_p15_plan_from_report_quota(quotas: list[tuple[int, int]], month: str, plan: str,
                                    seats: tuple) -> None:
    """C.P15: 40 activity-report seat holders; plan from ``plan_quota`` rows."""
    lics = activity_seats(40, date=f"{month}-15")
    conf = [quota(month, q, n) for q, n in quotas]
    [ev] = detect_plans([], lics, conf, month=month)
    assert (ev.plan, ev.seats) == (plan, seats)
    assert ev.source == ("report_quota" if quotas else "none")
    assert ev.conflict is False


def test_p15_promo_quota_outside_promo_months_is_unmapped() -> None:
    lics = activity_seats(40, date="2026-10-15")
    [ev] = detect_plans([], lics, [quota(MONTH, 7000, 40), quota(MONTH, 1234, 3)], month=MONTH)
    assert (ev.plan, ev.source) == ("unknown", "none")
    assert ev.evidence == ("report_quota: org:org-a quota 1234 x 3 (unmapped)",
                           "report_quota: org:org-a quota 7000 x 40 (unmapped)")


def test_p15b_report_users_with_and_without_quota() -> None:
    lines, _ = rows(600_000, date="2026-10-12", users=people(60))
    [ev] = detect_plans(lines, [], [quota(MONTH, 3900, 60)], month=MONTH)
    assert (ev.plan, ev.source, ev.seats) == ("enterprise", "report_quota", (("enterprise", 60),))
    [ev] = detect_plans(lines, [], [], month=MONTH)
    assert (ev.plan, ev.source, ev.seats) == ("unknown", "none", (("unknown", 60),))


def test_quota_counts_above_the_seat_count_raise_it() -> None:
    [ev] = detect_plans([], activity_seats(5), [quota(MONTH, 3900, 8)], month=MONTH)
    assert ev.seats == (("enterprise", 8),)
    assert "seat count raised from 5 to 8 by report_quota" in ev.evidence


def test_seats_api_partial_and_latest_snapshot_per_seat() -> None:
    """The seats API places the seats it knows; the rest stay unknown. The latest snapshot in the
    month per principal and org decides (an earlier plan is superseded)."""
    lics = activity_seats(10, date="2026-10-20")
    early = api_seats(6, "business", date="2026-10-03")
    late = api_seats(6, "enterprise", date="2026-10-09")
    unknown = api_seats(2, "unknown", date="2026-10-09", seed="z")
    [ev] = detect_plans([], lics + early + late + unknown, [], month=MONTH)
    # seat count: the snapshot date with the most seats (10 activity-report holders on 10-20)
    assert (ev.plan, ev.source, ev.seats) == ("unknown", "seats_api",
                                              (("enterprise", 6), ("unknown", 4)))
    # the quota (lower precedence) agrees partially: no conflict, it places nothing
    [ev] = detect_plans([], lics + late, [quota(MONTH, 3900, 10)], month=MONTH)
    assert (ev.source, ev.conflict, ev.seats) == ("seats_api", False,
                                                  (("enterprise", 6), ("unknown", 4)))


def test_seat_counts_carry_seats_api_plans_for_aggregate_only_bundles() -> None:
    conf = [seat_count("org-a", "business", 7), seat_count("org-a", "unknown", 3, team="t2"),
            b.make_config("seat_counts", {"team": "t1", "bucket": "*", "n_people": 10},
                          entity_id="org:org-a", source_kind="tokenbill.copilot_export")]
    [ev] = detect_plans([], [], conf, month=MONTH)
    assert (ev.source, ev.seats) == ("seats_api", (("business", 7), ("unknown", 3)))
    assert ev.evidence == ("seats_api (seat_counts): business 7",)


def test_org_settings_per_org_nearest_snapshot_and_statements_ignored() -> None:
    lics = activity_seats(6, org="org-a") + activity_seats(4, org="org-b", seed="v")
    conf = [org_settings("org-a", "business", date="2026-09-01"),
            org_settings("org-a", "enterprise", date="2026-12-01"),   # after the month: not used
            org_settings("org-b", "enterprise", date="2027-01-05"),   # only one: after the month
            org_settings("org-c", "business", source_kind="tokenbill.admin_answers")]
    [ev] = detect_plans([], lics, conf, month=MONTH)
    assert (ev.plan, ev.source, ev.seats) == ("mixed", "org_settings",
                                              (("business", 6), ("enterprise", 4)))
    assert ev.evidence == ("org_settings: org:org-a plan_type=business",
                           "org_settings: org:org-b plan_type=enterprise")
    # an org without settings leaves its seats unknown
    [ev] = detect_plans([], lics, conf[:1], month=MONTH)
    assert ev.seats == (("business", 6), ("unknown", 4))


def test_org_settings_without_org_split() -> None:
    """Enterprise-level activity reports carry no org: a unanimous plan_type covers all seats,
    disagreeing orgs place none; in org mode the org's own setting applies."""
    lics = activity_seats(5, org=None)
    [ev] = detect_plans([], lics, [org_settings("x", "business"), org_settings("y", "business")],
                        month=MONTH)
    assert (ev.plan, ev.seats) == ("business", (("business", 5),))
    [ev] = detect_plans([], lics, [org_settings("x", "business"), org_settings("y", "enterprise")],
                        month=MONTH)
    assert (ev.plan, ev.source, ev.seats) == ("unknown", "org_settings", (("unknown", 5),))
    conf = [flags({"pool_seats.org:x.unknown": 5}), org_settings("x", "enterprise")]
    [ev] = detect_plans([], [], conf, month=MONTH, entity_mode="org")
    assert (ev.entity_id, ev.plan, ev.source) == ("org:x", "enterprise", "org_settings")


def test_statements_pool_seats_and_enterprise_fallback() -> None:
    conf = [flags({"pool_seats.enterprise.business": 8, "pool_seats.enterprise.enterprise": 2})]
    [ev] = detect_plans([], [], conf, month=MONTH)
    assert (ev.plan, ev.source, ev.seats) == ("mixed", "admin_statement",
                                              (("business", 8), ("enterprise", 2)))
    # a capped cost center inherits the enterprise statement
    conf = [cost_center("A", "9500"), flags({"plan.enterprise": "business"})]
    lics = api_seats(5, "unknown", cost_center="A")
    [ev] = detect_plans([], lics, conf, month=MONTH)
    assert (ev.entity_id, ev.plan, ev.source) == ("cc:A", "business", "admin_statement")
    assert ev.evidence == ("admin_statement: plan.enterprise=business",)


def test_entities_and_months_are_separate() -> None:
    lines = [b.make_seat_line("business", "3", date_utc="2026-10-01", organization="a"),
             b.make_seat_line("enterprise", "2", date_utc="2026-10-01", organization="b"),
             b.make_seat_line("enterprise", "9", date_utc="2026-11-01", organization="b")]
    evs = detect_plans(lines, [], [], month=MONTH, entity_mode="org")
    assert [(e.entity_id, e.plan, e.seats) for e in evs] == [
        ("org:a", "business", (("business", 3),)), ("org:b", "enterprise", (("enterprise", 2),))]
    assert detect_plans(lines, [], [], month="2026-12") == []


def test_fractional_seat_months_count_as_whole_seats() -> None:
    lines = [b.make_seat_line("business", "99.5", date_utc="2026-10-01")]
    [ev] = detect_plans(lines, [], [], month=MONTH)
    assert ev.seats == (("business", 100),)


def test_bad_arguments() -> None:
    with pytest.raises(UsageError):
        detect_plans([], [], [], month="2026-13")
    with pytest.raises(UsageError):
        detect_plans([], [], [], month=MONTH, entity_mode="team")
    with pytest.raises(UsageError):
        detect_plans(["x"], [], [], month=MONTH)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        detect_plans([], ["x"], [], month=MONTH)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        detect_plans([], [], ["x"], month=MONTH)  # type: ignore[list-item]


def test_per_org_statements_apply_in_enterprise_mode() -> None:
    """The admin answers state ``plan_as_shown`` per org (run flags ``plan.org:<o>``); in enterprise
    mode each org's seats take its org's statement."""
    lics = activity_seats(6, org="org-a") + activity_seats(4, org="org-b", seed="v")
    answers = flags({"plan.org:org-a": "business", "plan.org:org-b": "enterprise"},
                    entity="admin_answers")
    [ev] = detect_plans([], lics, [answers], month=MONTH)
    assert (ev.plan, ev.source, ev.seats, ev.conflict) == (
        "mixed", "admin_statement", (("business", 6), ("enterprise", 4)), False)
    assert ev.evidence == ("admin_statement: plan.org:org-a=business",
                           "admin_statement: plan.org:org-b=enterprise")
    # an org stated "mixed" (or not stated) leaves its seats unknown
    answers = flags({"plan.org:org-a": "business", "plan.org:org-b": "mixed"},
                    entity="admin_answers")
    [ev] = detect_plans([], lics, [answers], month=MONTH)
    assert (ev.plan, ev.seats) == ("unknown", (("business", 6), ("unknown", 4)))
    # the enterprise's own statement comes first; data beats every statement
    both = flags({"plan.enterprise": "enterprise", "plan.org:org-a": "business"})
    [ev] = detect_plans([], lics, [both], month=MONTH)
    assert (ev.plan, ev.seats) == ("enterprise", (("enterprise", 10),))
    [ev] = detect_plans([b.make_seat_line("enterprise", "10", date_utc="2026-10-01")], lics,
                        [answers], month=MONTH)
    assert (ev.plan, ev.source, ev.conflict) == ("enterprise", "seat_lines", True)
    # seats without an org: a unanimous per-org statement covers them
    org_less = activity_seats(5, org=None)
    [ev] = detect_plans([], org_less, [flags({"plan.org:x": "business", "plan.org:y": "business"})],
                        month=MONTH)
    assert (ev.plan, ev.seats) == ("business", (("business", 5),))


@pytest.mark.parametrize("quota_rows,conflict", [
    ([(1900, 8)], True),      # 6 Enterprise (seats API) + 8 Business > 10 seats
    ([(1900, 4)], False),     # 6 + 4 ≤ 10: the quota may describe the other seats
    ([(3900, 10)], False),    # agrees (and covers the rest)
    ([(1900, 3), (3900, 7)], False),
    ([(1900, 5), (3900, 5)], True),   # 6 Enterprise + 5 Business > 10 seats
    ([(1900, 5), (3900, 7)], False),  # 12 quota users: at least 12 seats, 7 + 5 fits
])
def test_partial_sources_conflict_only_when_no_assignment_fits(
        quota_rows: list[tuple[int, int]], conflict: bool) -> None:
    lics = activity_seats(10, date="2026-10-20") + api_seats(6, "enterprise", date="2026-10-09")
    [ev] = detect_plans([], lics, [quota(MONTH, q, n) for q, n in quota_rows], month=MONTH)
    assert (ev.source, ev.seats, ev.conflict) == ("seats_api",
                                                  (("enterprise", 6), ("unknown", 4)), conflict)
    assert (f"{PLAN_CONFLICT_DQ}: seats_api vs report_quota" in ev.evidence) is conflict


def test_exact_mixed_counts_conflict_with_a_larger_partial_count() -> None:
    lines = [b.make_seat_line("business", "6", date_utc="2026-10-01"),
             b.make_seat_line("enterprise", "4", date_utc="2026-10-01")]
    [ev] = detect_plans(lines, api_seats(8, "business"), [], month=MONTH)
    assert (ev.plan, ev.source, ev.conflict) == ("mixed", "seat_lines", True)
    [ev] = detect_plans(lines, api_seats(5, "business"), [], month=MONTH)
    assert ev.conflict is False


def test_seat_lines_without_a_plan_leave_the_plan_to_lower_sources() -> None:
    """A seat SKU that maps to no plan counts seats but places none: the seats API decides, and
    pool months follow the evidence (no scenario pair for seats the seats API placed)."""
    odd = b.make_seat_line("business", "10", date_utc="2026-10-01", sku="copilot_other_seat")
    lics = api_seats(10, "enterprise")
    [ev] = detect_plans([odd], lics, [], month=MONTH)
    assert (ev.plan, ev.source, ev.seats, ev.conflict) == ("enterprise", "seats_api",
                                                           (("enterprise", 10),), False)
    assert ev.evidence == ("seat_lines: copilot_other_seat 10 (no plan)",
                           "seats_api: enterprise 10")
    [pm] = pool_months([], [odd], lics, [], today="2026-11-10")
    assert (pm.plan_scenario, pm.seats, pm.seats_source, pm.plan_source, pm.pool_credits) == (
        None, (("enterprise", "10"),), "seat_lines", "seats_api", "39000")
    # partly mapped seat lines speak only for the mapped seats: no conflict with the seats API
    # describing the others
    mapped = b.make_seat_line("business", "5", date_utc="2026-10-01")
    part = b.make_seat_line("business", "5", date_utc="2026-10-01", sku="copilot_other_seat")
    [ev] = detect_plans([mapped, part], api_seats(5, "enterprise", seed="z"), [], month=MONTH)
    assert (ev.source, ev.seats, ev.conflict) == ("seat_lines",
                                                  (("business", 5), ("unknown", 5)), False)
    [ev] = detect_plans([mapped, part], api_seats(8, "enterprise", seed="z"), [], month=MONTH)
    assert ev.conflict is True                               # 5 Business + 8 Enterprise > 10

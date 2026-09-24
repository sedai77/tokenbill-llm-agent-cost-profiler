"""Run flags, cost-center caps and policies, billing modes, entities and seat counting (F-POOL brief
item 3; addendum CA-40, DC19, §19.1 #10–#11, #24–#25)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokenbill.core import builders as b
from tokenbill.core.errors import UsageError
from tokenbill.core.pool import (
    billing_modes,
    capped_cost_centers,
    capped_policies,
    entity_of,
    run_flags,
    seat_months,
)

from .worlds import activity_seats, api_seats, cost_center, flags, people, rows, seat_count

MONTH = "2026-10"


def test_run_flags_cli_over_admin_answers_per_key() -> None:
    answers = flags({"plan.enterprise": "business", "promo_eligible": True,
                     "capped_policy.A": "block", "compliance": None}, entity="admin_answers")
    cli = flags({"plan.enterprise": "enterprise", "billing_mode.enterprise": "volume"})
    older_cli = flags({"billing_mode.enterprise": "metered", "pool_seats.enterprise.business": 3},
                      snapshot_ms=0)
    newer_cli = flags({"billing_mode.enterprise": "azure"}, snapshot_ms=5)
    got = run_flags([cli, answers])
    assert got == {"billing_mode.enterprise": "volume", "capped_policy.A": "block",
                   "plan.enterprise": "enterprise", "promo_eligible": True}
    assert run_flags([answers, cli]) == got
    assert run_flags([newer_cli, older_cli])["billing_mode.enterprise"] == "azure"
    assert run_flags([newer_cli, older_cli])["pool_seats.enterprise.business"] == 3
    assert run_flags([cost_center("A", "1")]) == {}


def test_capped_cost_centers_and_policies() -> None:
    conf = [cost_center("A", "95000"), cost_center("B", "10", enabled=False),
            cost_center("C", None), cost_center("D", "not-a-number"),
            cost_center("E", "500", date="2026-09-01"), cost_center("E", "700", date="2026-09-20"),
            b.make_config("cost_center", {"pool_enabled": False, "pool_target_credits": "1"},
                          entity_id="cc:F", source_kind="github.cost_centers",
                          snapshot_ms=10**12),
            b.make_config("cost_center", {"pool_enabled": True, "pool_target_credits": "2"},
                          entity_id="cc:F", source_kind="tokenbill.admin_answers",
                          snapshot_ms=2 * 10**12)]          # a later statement loses to data
    assert capped_cost_centers(conf) == {"A": Decimal(95_000), "E": Decimal(700)}
    conf += [flags({"capped_policy.A": "continue", "capped_policy.cc:Z": "block",
                    "capped_policy.Q": "throttle"})]
    assert capped_policies(conf) == {"A": "continue", "E": "unknown", "Q": "unknown",
                                     "Z": "block"}


def test_entity_of() -> None:
    capped = {"A": Decimal(1)}
    assert entity_of("A", "org-a", capped=capped) == "cc:A"
    assert entity_of("A", "org-a", capped={"cc:A": Decimal(1)}) == "cc:A"
    assert entity_of("B", "org-a", capped=capped) == "enterprise"
    assert entity_of(None, "org-a", capped=capped, entity_mode="org") == "org:org-a"
    assert entity_of("", "", capped=capped, entity_mode="org") == "enterprise"
    assert entity_of(None, "bad\x07org", capped={}, entity_mode="org") == "org:bad_org"
    with pytest.raises(UsageError):
        entity_of(None, None, capped={}, entity_mode="orgs")


def test_billing_modes() -> None:
    lines = [b.make_seat_line("business", "1", organization="a"),
             b.make_seat_line("business", "1", organization="c", cost_center="A")]
    lics = api_seats(1, "business", org="b")
    conf = [cost_center("A", "1900"), flags({"billing_mode.org:b": "volume",
                                             "billing_mode.bogus": "azure",
                                             "billing_mode.enterprise": "prepaid"})]
    assert billing_modes(lines, conf, lics) == {"cc:A": "metered", "enterprise": "metered",
                                                "org:a": "metered", "org:b": "volume"}
    assert billing_modes([], [], activity_seats(1, org=None)) == {"enterprise": "unknown"}


def test_seat_months_precedence() -> None:
    seat_lines = [b.make_seat_line("business", "99.5", date_utc="2026-10-01"),
                  b.make_seat_line("enterprise", "0.5", date_utc="2026-10-15"),
                  b.make_seat_line("business", "7", date_utc="2026-11-01")]      # other month
    stated = flags({"pool_seats.enterprise.enterprise": 12, "pool_seats.org:x.business": "4",
                    "pool_seats.bad": 1, "pool_seats.enterprise.pro": 1,
                    "pool_seats.enterprise.unknown": -3})
    lics = activity_seats(9)
    counts = [seat_count("org-a", "business", 6)]
    users, _ = rows(10, date="2026-10-03", users=people(5))
    everything = (seat_lines + users, lics, counts + [stated])
    assert seat_months(*everything, MONTH) == (
        {("enterprise", "business"): Decimal("99.5"), ("enterprise", "enterprise"): Decimal("0.5")},
        "seat_lines")
    # the enterprise's own statement wins over org-scoped ones (enterprise mode has no org pools)
    assert seat_months(users, lics, counts + [stated], MONTH) == (
        {("enterprise", "enterprise"): Decimal(12)}, "run_flags")
    per_org = flags({"pool_seats.org:x.business": 4, "pool_seats.org:y.business": 3,
                     "pool_seats.org:y.enterprise": 2, "pool_seats.cc:Z.business": 1})
    assert seat_months([], [], [per_org], MONTH) == (
        {("enterprise", "business"): Decimal(8), ("enterprise", "enterprise"): Decimal(2)},
        "run_flags")
    assert seat_months([], [], [per_org], MONTH, entity_mode="org") == (
        {("enterprise", "business"): Decimal(1), ("org:x", "business"): Decimal(4),
         ("org:y", "business"): Decimal(3), ("org:y", "enterprise"): Decimal(2)}, "run_flags")
    assert seat_months(users, lics, counts, MONTH) == ({("enterprise", "unknown"): Decimal(9)},
                                                       "licenses")
    assert seat_months(users, [], counts, MONTH) == ({("enterprise", "business"): Decimal(6)},
                                                     "seat_counts")
    assert seat_months(users, [], [], MONTH) == ({("enterprise", "unknown"): Decimal(5)},
                                                 "report_users")
    assert seat_months([], [], [], MONTH) == ({}, "none")
    # per entity: each entity uses its own best source; the map carries the weakest one
    assert seat_months(seat_lines[:1] + rows(1, date="2026-10-03", org="o2")[0], [], [], MONTH,
                       entity_mode="org") == ({("org:org-a", "business"): Decimal("99.5"),
                                               ("org:o2", "unknown"): Decimal(1)},
                                              "report_users")


def test_seat_months_licenses_counts_the_busiest_snapshot_once_per_person() -> None:
    p = people(3)
    lics = [b.make_license(p[0], snapshot_date="2026-10-01", org="a", plan="business"),
            b.make_license(p[0], snapshot_date="2026-10-01", org="b", plan="unknown"),
            b.make_license(p[1], snapshot_date="2026-10-01", org="a", plan="enterprise"),
            b.make_license(p[0], snapshot_date="2026-10-20", org="a", plan="business"),
            b.make_license(p[1], snapshot_date="2026-10-20", org="a", plan="enterprise"),
            b.make_license(p[2], snapshot_date="2026-10-20", org="a", plan="business"),
            b.make_license(p[2], snapshot_date="2026-09-30", org="a", plan="business"),
            b.make_license(p[2], snapshot_date="2026-10-02", org="a", plan="business",
                           product="other_product")]
    assert seat_months([], lics, [], MONTH) == ({("enterprise", "business"): Decimal(2),
                                                 ("enterprise", "enterprise"): Decimal(1)},
                                                "licenses")
    # a multi-org seat counts once in the enterprise and once per org in org mode
    assert seat_months([], lics[:3], [], MONTH, entity_mode="org")[0] == {
        ("org:a", "business"): Decimal(1), ("org:a", "enterprise"): Decimal(1),
        ("org:b", "unknown"): Decimal(1)}


def test_seat_months_seat_counts_rows_and_bad_inputs() -> None:
    conf = [seat_count("org-a", "business", 3, date="2026-10-02"),
            seat_count("org-a", "business", 5, date="2026-10-09"),
            seat_count("org-a", "enterprise", 1, date="2026-10-09", team="t2"),
            seat_count("org-a", "pro", 2, date="2026-10-09", team="t3"),
            b.make_config("seat_counts", {"team": "t", "plan": "business", "bucket": "0-7",
                                          "n": 0}, entity_id="org:org-a",
                          source_kind="tokenbill.copilot_export", snapshot_ms=0),
            b.make_config("seat_counts", {"team": "t", "plan": "business", "bucket": "0-7",
                                          "n": 4}, entity_id="cc:A",
                          source_kind="tokenbill.copilot_export",
                          snapshot_ms=1_791_000_000_000)]
    # the busiest snapshot date (10-09: 5 + 1 + 2) wins; an unknown plan attr counts as unknown
    got = seat_months([], [], conf, MONTH)
    assert got == ({("enterprise", "business"): Decimal(5),
                    ("enterprise", "enterprise"): Decimal(1),
                    ("enterprise", "unknown"): Decimal(2)}, "seat_counts")
    with pytest.raises(UsageError):
        seat_months([], [], [], "2026-00")
    with pytest.raises(UsageError):
        seat_months([], [], [], MONTH, entity_mode="x")
    for fn in (run_flags, capped_cost_centers, capped_policies):
        with pytest.raises(UsageError):
            fn(["x"])  # type: ignore[list-item]

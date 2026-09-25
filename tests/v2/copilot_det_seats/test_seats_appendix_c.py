"""Appendix C.P1–P4 and P10–P12 through the detector (addendum §10.1 ``idle-seat`` /
``seat-auto-assign``): enterprise E, metered, 1,000 Business + 200 Enterprise seats (pool
2,680,000 credits), seat lines present, standard month 2026-09, direct assignments in an
``assign_selected`` org unless stated."""

from __future__ import annotations

import pytest

from tokenbill.core.labels import Basis, Evidence

from .helpers import (
    USD,
    by_scenario,
    ctx,
    detect,
    dims,
    evidence,
    of_kind,
    org_settings,
    p_plan,
    p_pool,
    rows,
    seats,
)

ACTIVE, _ = rows(500, date="2026-09-20")     # a report row of an active user (zero-cost check)


def _world(consumed: int, idle: list, *, config: list | None = None, **pool_kw: object):
    conf = config if config is not None else [org_settings("org-a", "assign_selected")]
    active = seats(20, seed="active", bucket="0-7")
    return ctx(pools=[p_pool(consumed, **pool_kw)], plans=[p_plan()], licenses=idle + active,
               config=conf, cost_lines=ACTIVE, today="2026-10-05")


@pytest.mark.parametrize(("consumed", "saving_usd", "regime"), [
    (3_100_000, 0, "overage"),      # C.P2: fees -$950, overage +$950
    (2_000_000, 950, "slack"),      # C.P3: slack 680,000
    (2_650_000, 300, "straddling"),  # C.P4: new overage 65,000 ($650) → $300
])
def test_p2_p3_p4_idle_removable_business_seats(consumed: int, saving_usd: int,
                                                 regime: str) -> None:
    [f] = of_kind(detect(_world(consumed, seats(50))), "idle-seat")
    assert dims(f) == {"entity": "enterprise", "product": "copilot", "team": "t1"}
    assert (f.category, f.lever_class, f.n_users, f.n_events) == ("lever", "rate", 50, 50)
    assert (f.cost_observed.nano, f.cost_observed.evidence, f.cost_observed.basis) == (
        950 * USD, Evidence.ESTIMATED, Basis.LIST)
    assert "proration" in f.cost_observed.note and "2026-10-01" in f.cost_observed.note
    rec = f.recoverable
    assert rec is not None and (rec.nano, rec.evidence, rec.basis) == (
        saving_usd * USD, Evidence.ESTIMATED, Basis.LIST)
    assert f"regime {'overage' if regime == 'overage' else 'slack'}" in rec.note or \
        f"regime {regime}" in rec.note
    assert evidence(f, "assignment:removable")["n"] == 50
    assert evidence(f, "assignment:removable")["nano"] == saving_usd * USD
    assert evidence(f, "regime") == {"regime": "overage" if consumed > 2_680_000 else "slack",
                                     "month": "2026-09"}
    assert f.lever_ids == ("copilot.seat_reclaim",)
    assert (f.needs_eval, f.confidence) == (False, "high")
    assert "before 2026-11-01T00:00Z" in evidence(f, "deadline")["text"]
    assert f.fix is not None and f.fix.target == "github-copilot"
    assert "/orgs/{org}/copilot/billing/selected_users" in f.fix.text


def test_p2_still_emitted_with_count_and_regime_note() -> None:
    """In overage an idle seat saves about nothing — the finding is still shown (the seat fees
    pass ``min_usd``) with the count and the regime."""
    [f] = of_kind(detect(_world(3_100_000, seats(50))), "idle-seat")
    assert f.recoverable is not None and f.recoverable.nano == 0
    assert "50 idle seats" in f.summary and "regime overage" in f.summary
    assert "50 removable" in f.title


def test_p10_volume_billing_projection_none() -> None:
    [f] = of_kind(detect(_world(2_000_000, seats(10), billing_mode="volume")), "idle-seat")
    assert f.n_users == 10
    assert (f.cost_observed.nano, f.cost_observed.evidence) == (190 * USD, Evidence.ESTIMATED)
    assert f.recoverable is None
    assert evidence(f, "assignment:removable") == {"n": 10, "projection": "none"}
    assert "savings only at renewal" in evidence(f, "deadline")["text"]
    assert "renewal date unknown" in f.summary


def test_p10_volume_renewal_date_from_run_flag() -> None:
    from .helpers import flags

    conf = [org_settings("org-a", "assign_selected"),
            flags({"renewal_date.enterprise": "2027-03-01"})]
    [f] = of_kind(detect(_world(2_000_000, seats(10), config=conf, billing_mode="volume")),
                  "idle-seat")
    assert "(2027-03-01)" in evidence(f, "deadline")["text"]


def test_p11_assign_all_org_seat_auto_assign() -> None:
    idle_b = seats(10, org="org-b", seed="b", team="t2")
    conf = [org_settings("org-a", "assign_selected"), org_settings("org-b", "assign_all")]
    found = detect(_world(2_000_000, idle_b, config=conf))
    [auto] = of_kind(found, "seat-auto-assign")
    assert dims(auto) == {"entity": "enterprise", "org": "org-b", "product": "copilot"}
    assert auto.recoverable is not None
    assert (auto.recoverable.nano, auto.recoverable.evidence) == (190 * USD, Evidence.ESTIMATED)
    assert "trade-off" in auto.recoverable.note
    assert auto.needs_eval and auto.lever_ids == ("copilot.seat_policy_selected",)
    assert (auto.cost_observed.nano, auto.category) == (190 * USD, "lever")
    assert evidence(auto, "seat_policy") == {"setting": "assign_all", "source": "org settings"}
    [idle] = of_kind(found, "idle-seat")
    assert evidence(idle, "assignment:removable")["n"] == 0
    assert evidence(idle, "assignment:auto")["n"] == 10
    assert idle.recoverable is None and idle.lever_ids == ()
    assert evidence(idle, "org:org-b") == {"removable": 0, "team": 0, "auto": 10, "unknown": 0}


def test_p11_policy_stated_by_the_admin_answers() -> None:
    conf = [org_settings("org-b", "assign_all", stated=True)]
    [auto] = of_kind(detect(_world(2_000_000, seats(10, org="org-b"), config=conf)),
                     "seat-auto-assign")
    assert evidence(auto, "seat_policy")["source"] == "admin statement"
    assert "(admin statement)" in auto.summary


def test_p11_pulled_org_settings_beat_the_statement() -> None:
    conf = [org_settings("org-b", "assign_selected", date="2026-09-01"),
            org_settings("org-b", "assign_all", stated=True, date="2026-09-20")]
    found = detect(_world(2_000_000, seats(10, org="org-b"), config=conf))
    assert of_kind(found, "seat-auto-assign") == []
    [idle] = of_kind(found, "idle-seat")
    assert evidence(idle, "assignment:removable")["n"] == 10


def test_p12_team_assigned_seats_not_projected() -> None:
    idle = seats(12, seed="direct") + seats(8, seed="team", via_team=True)
    [f] = of_kind(detect(_world(2_000_000, idle)), "idle-seat")
    assert f.n_users == 20 and f.cost_observed.nano == 380 * USD
    assert f.recoverable is not None and f.recoverable.nano == 228 * USD
    assert evidence(f, "assignment:removable")["n"] == 12
    assert evidence(f, "assignment:removable")["nano"] == 228 * USD
    assert evidence(f, "assignment:team") == {"n": 8, "projection": "none",
                                              "lever": "copilot.seat_reclaim_team"}
    assert f.lever_ids == ("copilot.seat_reclaim", "copilot.seat_reclaim_team")
    assert "12 removable, 8 team-assigned" in f.summary


def test_idle_seats_per_team_and_without_team() -> None:
    idle = seats(6, team="platform", seed="p") + seats(5, team=None, seed="n")
    found = of_kind(detect(_world(2_000_000, idle)), "idle-seat")
    teams = {dims(f).get("team"): f.n_users for f in found}
    assert teams == {"platform": 6, None: 5}
    assert "no team" in next(f for f in found if "team" not in dims(f)).title


def test_enterprise_seats_priced_at_their_plan() -> None:
    idle = seats(3, plan="enterprise", seed="e") + seats(2, seed="b")
    [f] = of_kind(detect(_world(2_000_000, idle)), "idle-seat")
    assert f.cost_observed.nano == (3 * 39 + 2 * 19) * USD
    # slack: removing them saves their fees
    assert f.recoverable is not None and f.recoverable.nano == (3 * 39 + 2 * 19) * USD


def test_seat_fees_below_min_usd_not_emitted() -> None:
    found = detect(ctx(pools=[p_pool(2_000_000)], plans=[p_plan()], licenses=seats(1),
                       config=[org_settings("org-a", "assign_selected")], cost_lines=ACTIVE,
                       min_usd="20"))
    assert of_kind(found, "idle-seat") == []
    found = detect(ctx(pools=[p_pool(2_000_000)], plans=[p_plan()], licenses=seats(1),
                       config=[org_settings("org-a", "assign_selected")], cost_lines=ACTIVE,
                       min_usd="19"))
    assert len(of_kind(found, "idle-seat")) == 1


def test_known_plan_has_no_scenario_dims() -> None:
    found = detect(_world(2_000_000, seats(5)))
    assert all("plan_scenario" not in dims(f) for f in found)
    assert list(by_scenario(of_kind(found, "idle-seat"))) == [None]

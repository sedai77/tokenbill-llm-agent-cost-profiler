"""Gate tests (merge gate 1; SPEC §21 #4): the real Copilot detectors' findings drive this plan.

They pin the seam CONTRACT-CHANGE-CP-PLAN-1 describes — the seat counts CP-PLAN reads from
``idle-seat`` evidence and the ``plan_scenario`` dims of scenario findings — and are skipped until
CP-DET-SEATS / CP-DET-USAGE are merged (``pytest.importorskip``)."""

from __future__ import annotations

import datetime as dt

import pytest

from tokenbill.copilot.plan import plan_copilot, plan_copilot_scenarios
from tokenbill.core import builders as b
from tokenbill.core import pool
from tokenbill.core.types import AnalysisContext

from .worlds import PRICER, USD, World, lever, p1_world

SEAT = "copilot.seat_reclaim"
AUTO = "copilot.default_model_auto"


def _ms(date: str) -> int:
    return (dt.date.fromisoformat(date) - dt.date(1970, 1, 1)).days * 86_400_000


def _ctx(w: World, pools: list, *, today: str, caps: set[str]) -> AnalysisContext:
    plans = []
    for month in sorted({pm.month for pm in pools}):
        plans += pool.detect_plans(w.lines, w.licenses, w.config, month=month)
    return AnalysisContext(
        pricer=PRICER, rules=None, replayer=None, calibration=None,
        window=(_ms(f"{w.month}-01"), _ms(today)), capabilities=frozenset(caps),
        now_ms=_ms(today), aggregates=tuple(w.aggs), cost_lines=tuple(w.lines),
        licenses=tuple(w.licenses), activity=tuple(w.activity), config=tuple(w.config),
        pools=tuple(pools), plans=tuple(plans))


def _idle_licenses(w: World, n: int, *, team_assigned: int = 0, org: str = "org-a",
                   plan: str = "business", report: bool = False) -> None:
    for i in range(n):
        w.licenses.append(b.make_license(
            b.make_principal(f"idle{i}"), snapshot_date=f"{w.month}-15",
            plan="unknown" if report else plan, team="t1", org=org,
            seat_created=None if report else "2026-01-05", last_activity_bucket="none_90d",
            last_activity_surface="vscode",
            assigned_via_team=None if report else i < team_assigned,
            source_kind="github.copilot_activity_report" if report else "github.copilot_seats"))


@pytest.mark.gate
def test_gate_p12_idle_seat_findings_give_the_228_seat_lever() -> None:
    mod = pytest.importorskip("tokenbill.detect.copilot_seats")
    w = p1_world(2_000_000)
    _idle_licenses(w, 20, team_assigned=8)
    w.config.append(b.make_config("org_settings", {"seat_management_setting": "assign_selected"},
                                  entity_id="org:org-a",
                                  source_kind="github.org_copilot_settings",
                                  snapshot_ms=_ms("2026-09-20")))
    pools = w.pools()
    ctx = _ctx(w, pools, today="2026-10-20",
               caps={"ext:copilot", "licenses", "config", "copilot_billing", "aggregates"})
    findings = mod.CopilotSeatsBudgets().detect([], ctx)
    assert any(f.kind == "idle-seat" for f in findings)
    plan = plan_copilot(w.cells(), pools, findings, PRICER, lines=w.lines, activity=[],
                        month=w.month, config=w.config)
    assert lever(plan, SEAT).shapley.nano == 228 * USD


@pytest.mark.gate
def test_gate_p13b_scenario_findings_give_0_and_390() -> None:
    mod = pytest.importorskip("tokenbill.detect.copilot_seats")
    w = World(month="2026-10").report_seats(90)
    _idle_licenses(w, 10, report=True)
    w.flags(**{"billing_mode__enterprise": "metered"})
    w.usage(250_000, users=5)
    pools = w.pools(today="2026-11-10")
    ctx = _ctx(w, pools, today="2026-11-10",
               caps={"ext:copilot", "licenses", "config", "copilot_billing", "aggregates"})
    findings = mod.CopilotSeatsBudgets().detect([], ctx)
    plans = dict(plan_copilot_scenarios(w.cells(), pools, findings, PRICER, lines=w.lines,
                                        activity=[], config=w.config, month=w.month))
    assert lever(plans["business"], SEAT).shapley.nano == 0
    ent = lever(plans["enterprise"], SEAT)
    assert ent.shapley.nano == 390 * USD and ent.upper_bound and ent.needs_eval


@pytest.mark.gate
def test_gate_auto_adoption_findings_link_the_auto_lever_with_reach() -> None:
    mod = pytest.importorskip("tokenbill.detect.copilot_org")
    w = World().seats("business", 1000).seats("enterprise", 200)
    w.usage(1_000_000, users=5).usage(2_100_000, model="Auto: Claude Sonnet 5", users=5)
    for i in range(5):
        w.ide("t1", {"ide:intellij": 20, "ide:vscode": 80, "interactions": 100}, user=f"u{i}")
    pools = w.pools()
    ctx = _ctx(w, pools, today="2026-10-20",
               caps={"ext:copilot", "aggregates", "copilot_billing", "activity", "config"})
    findings = mod.CopilotOrgScan().detect([], ctx)
    assert any(f.kind == "auto-adoption" for f in findings)
    plan = plan_copilot(w.cells(), pools, findings, PRICER, lines=w.lines, activity=w.activity,
                        month=w.month, config=w.config)
    assert lever(plan, AUTO).shapley.nano == 800 * USD      # 0.8 × 10% × 1,000,000 credits

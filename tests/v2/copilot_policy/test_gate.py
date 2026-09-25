"""Gate tests (SPEC §21 #4): the real Copilot detectors (CP-DET-SEATS, CP-DET-USAGE) and the real
aggregate plan (CP-PLAN) feed the checklist and the pack. Skipped when a sibling is absent
(``pytest.importorskip``); they pass with the siblings on disk at this wave."""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from tokenbill.copilot.admin_actions import JETBRAINS_ACTION_ID, admin_actions
from tokenbill.copilot.budgets import budget_design
from tokenbill.copilot.policy import build_copilot_packs, team_counts
from tokenbill.core import builders as b
from tokenbill.core import pool
from tokenbill.core.findings import build_finding, make_scope
from tokenbill.core.labels import Basis, exact
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    UsageAggregate,
)
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding

from .helpers import plan as _plan
from .helpers import result as _result

PRICER = FakePricer()
P_RE = re.compile(r"p_[0-9a-f]{20}")


def _ms(date: str) -> int:
    return (dt.date.fromisoformat(date) - dt.date(1970, 1, 1)).days * 86_400_000


class _World:
    """One synthetic enterprise month for the gate tests (report rows, seats, activity)."""

    def __init__(self, month: str) -> None:
        self.month = month
        self.lines: list[CostLine] = []
        self.aggs: list[UsageAggregate] = []
        self.licenses: list[LicenseSnapshot] = []
        self.activity: list[ActivityDay] = []
        self.config: list[ConfigSnapshot] = []
        self._n = 0

    def usage(self, credits: int, *, users: int, model: str = "Claude Sonnet 5",
              team: str = "t1", cost_center: str | None = None) -> _World:
        for i in range(users):
            self._n += 1
            line, agg = b.make_ai_usage_row(
                date_utc=f"{self.month}-10", model=model,
                credits=str(credits // users + (credits % users if i == 0 else 0)),
                principal=b.make_principal(f"u{self._n}"), team=team, cost_center=cost_center)
            self.lines.append(line)
            self.aggs.append(agg)
        return self

    def cells(self) -> list:
        return pool.build_cells(self.aggs, self.lines, grain="month",
                                capped=pool.capped_cost_centers(self.config))[0]

    def pools(self, today: str) -> list:
        return pool.pool_months(self.cells(), self.lines, self.licenses, self.config,
                                today=today)

    def ctx(self, pools: list, today: str, caps: set[str]) -> AnalysisContext:
        plans = pool.detect_plans(self.lines, self.licenses, self.config, month=self.month)
        return AnalysisContext(
            pricer=PRICER, rules=None, replayer=None, calibration=None,
            window=(_ms(f"{self.month}-01"), _ms(today)), capabilities=frozenset(caps),
            now_ms=_ms(today), aggregates=tuple(self.aggs), cost_lines=tuple(self.lines),
            licenses=tuple(self.licenses), activity=tuple(self.activity),
            config=tuple(self.config), pools=tuple(pools), plans=tuple(plans))


def _p13b() -> _World:
    """C.P13b: 100 activity-report seats (plan unknown), 10 of them idle, metered, 250,000
    pooled credits in a closed month."""
    w = _World("2026-10")
    for i in range(90):
        w.licenses.append(b.make_license(
            b.make_principal(f"s{i}"), snapshot_date="2026-10-15", plan="unknown",
            assigned_via_team=None, source_kind="github.copilot_activity_report"))
    for i in range(10):
        w.licenses.append(b.make_license(
            b.make_principal(f"idle{i}"), snapshot_date="2026-10-15", plan="unknown", team="t1",
            last_activity_bucket="none_90d", assigned_via_team=None,
            source_kind="github.copilot_activity_report"))
    w.config.append(b.make_config("run_flags", {"billing_mode.enterprise": "metered"}))
    w.usage(250_000, users=5)
    return w


def _idle_seat_for_plan(scenario: str) -> Finding:
    """An ``idle-seat`` finding with the evidence names CP-PLAN's ``seat_counts`` reads
    (CONTRACT-CHANGE-CP-PLAN-1): 10 unknown-assignment idle seats of unknown plan."""
    return build_finding(
        detector_id="copilot.seats-budgets", kind="idle-seat", detector_version="1",
        category="lever", lever_class="rate", audience="org", title="idle seats (gate)",
        summary="synthetic", scope=make_scope(product="copilot", entity="enterprise", team="t1",
                                              plan="unknown", bucket="none_90d",
                                              plan_scenario=scenario),
        n_events=10, n_lanes=0, n_users=10, first_seen_ms=0,
        cost_observed=exact(0, Basis.LIST), recoverable=None,
        lever_ids=("copilot.seat_reclaim",),
        evidence=(EvidenceItem("aggregate", "idle-seat:seats", (
            ("assignment_unknown", 10), ("removable", 0), ("team_assigned", 0))),),
        references=("cp-policy-gate",))


@pytest.mark.gate
def test_gate_p13b_real_plan_scenarios_give_two_seat_projections() -> None:
    plan_mod = pytest.importorskip("tokenbill.copilot.plan")
    today = "2026-11-10"
    w = _p13b()
    pools = w.pools(today)
    findings = [_idle_seat_for_plan(s) for s in ("business", "enterprise")]
    plans = plan_mod.plan_copilot_scenarios(w.cells(), pools, findings, PRICER, lines=w.lines,
                                            activity=[], config=w.config, month=w.month)
    assert [k for k, _ in plans] == ["business", "enterprise"]
    evidence = pool.detect_plans(w.lines, w.licenses, w.config, month=w.month)
    acts = admin_actions(plans, findings, pools, plans=evidence, today=today)
    assert acts[0].action_id == "admin:plan_confirm"
    seat = next(a for a in acts if a.action_id == "rest:org_selected_users_delete")
    assert seat.projection is None
    assert "if Business: $0.00/month" in seat.what
    assert "if Enterprise: $390.00/month" in seat.what and "upper bound" in seat.what


@pytest.mark.gate
def test_gate_p13b_real_seat_findings_feed_the_checklist_and_pack() -> None:
    seats_mod = pytest.importorskip("tokenbill.detect.copilot_seats")
    today = "2026-11-10"
    w = _p13b()
    pools = w.pools(today)
    ctx = w.ctx(pools, today, {"ext:copilot", "licenses", "config", "copilot_billing",
                               "aggregates"})
    findings = seats_mod.CopilotSeatsBudgets().detect([], ctx)
    assert {f.kind for f in findings} >= {"plan-status", "idle-seat"}
    # the scenario plans CP-PLAN returns for this world (C.P13b), built by hand here so this
    # test does not depend on the CP-PLAN x CP-DET-SEATS evidence seam
    plans = (("business", _plan(_result("copilot.seat_reclaim", 0))),
             ("enterprise", _plan(_result("copilot.seat_reclaim", 390))))
    acts = admin_actions(plans, findings, pools, plans=list(ctx.plans), today=today)
    assert acts[0].action_id == "admin:plan_confirm"
    assert "admin:seat_plan_change" not in [a.action_id for a in acts]
    seat = next(a for a in acts if a.action_id == "rest:org_selected_users_delete")
    assert seat.projection is None
    assert "if Business: $0.00/month" in seat.what
    assert "if Enterprise: $390.00/month" in seat.what
    budgets = budget_design(pools, w.cells(), w.config, k=5, cost_lines=w.lines)
    assert {s["scenario"] for s in budgets} == {"business", "enterprise"}
    packs = build_copilot_packs(plans, findings, acts, current=None, cohort_by=None,
                                include_tradeoffs=False, teams=team_counts(
                                    w.activity, w.config, w.licenses), budgets=budgets,
                                today=today)
    text = "\n".join(t for _, t in packs[0].hooks) + packs[0].readme_md
    assert not P_RE.search(text) and b.CANARY_LOGIN not in text
    for line in dict(packs[0].hooks)["github/requests.jsonl"].splitlines():
        assert json.loads(line)["api_version"] == "2026-03-10"


@pytest.mark.gate
def test_gate_editor_mix_and_auto_adoption_findings_drive_the_jetbrains_item() -> None:
    org_mod = pytest.importorskip("tokenbill.detect.copilot_org")
    plan_mod = pytest.importorskip("tokenbill.copilot.plan")
    today = "2026-10-20"
    w = _World("2026-09")
    w.lines.append(b.make_seat_line("business", "1000", date_utc="2026-09-01"))
    w.usage(1_000_000, users=5).usage(2_100_000, model="Auto: Claude Sonnet 5", users=5)
    for i in range(6):
        w.activity.append(b.make_activity(
            b.make_principal(f"t1-{i}"), date_utc="2026-09-10", team="t1",
            counts={"ide:intellij": 30, "ide:vscode": 70, "interactions": 100}))
    pools = w.pools(today)
    ctx = w.ctx(pools, today, {"ext:copilot", "aggregates", "copilot_billing", "activity",
                               "config"})
    findings = org_mod.CopilotOrgScan().detect([], ctx)
    kinds = {f.kind for f in findings}
    assert "editor-mix" in kinds and "auto-adoption" in kinds
    plans = plan_mod.plan_copilot_scenarios(w.cells(), pools, findings, PRICER, lines=w.lines,
                                            activity=w.activity, config=w.config, month=w.month)
    acts = admin_actions(plans, findings, pools, plans=list(ctx.plans), today=today)
    ids = [a.action_id for a in acts]
    assert JETBRAINS_ACTION_ID in ids and "admin:model_policy" in ids
    assert "admin:communicate_auto_tier" in ids and "admin:plan_confirm" not in ids
    teams = team_counts(w.activity, w.config, w.licenses)
    assert teams["t1"]["ide:intellij"] == 180
    pack = build_copilot_packs(plans, findings, acts, current={"model": "gpt-5.4"},
                               cohort_by="team", include_tradeoffs=False, teams=teams,
                               budgets=None, today=today)[0]
    assert json.loads(pack.merge_patch_json) == {"model": {"overridable": "auto"}}
    assert "| t1 | 30% | 0.70 |" in pack.readme_md
    auto = next(e for e in pack.entries if e.key == "copilot.managed.model")
    assert auto.projection is not None and auto.projection.nano is not None

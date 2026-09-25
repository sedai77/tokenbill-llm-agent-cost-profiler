"""Synthetic Copilot worlds for CP-OUT tests (addendum Appendix C.P1, P13, P14; no real data).

Every record comes from the ``core.builders`` Copilot builders; money is exact integer nano. The
worlds use September 2026 (a standard month: no promotion) with ``today`` 2026-10-20 (closed) or
2026-09-23 (open). Principals are fixture pseudonyms (``make_principal``), teams are synthetic
labels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from tokenbill.copilot.summary import assemble_summary
from tokenbill.core import builders as b
from tokenbill.core import pool
from tokenbill.core.labels import Basis, estimated
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    ContentTier,
    CostLine,
    LicenseSnapshot,
    UsageAggregate,
)
from tokenbill.core.types import (
    ActionPlan,
    AdminAction,
    CopilotSummary,
    LeverResult,
    PlanEvidence,
    PoolMonth,
    PrivacyInfo,
    RunResult,
)

CLOSED_TODAY = "2026-10-20"
OPEN_TODAY = "2026-09-23"
WINDOW = ("2026-09-01", "2026-09-30")
RECONCILED = {"github_copilot": "reconciled", "github_actions": "reconciled",
              "github_sandbox": "reconciled"}


@dataclass
class World:
    lines: list[CostLine] = field(default_factory=list)
    aggs: list[UsageAggregate] = field(default_factory=list)
    licenses: list[LicenseSnapshot] = field(default_factory=list)
    activity: list[ActivityDay] = field(default_factory=list)
    config: list[ConfigSnapshot] = field(default_factory=list)
    entity_mode: str = "enterprise"

    def usage(self, credits: str, *, i: int, day: int = 10, discount: str = "0",
              team: str | None = None, org: str | None = "org-a", model: str = "Claude Opus 5.5",
              unattributed: bool = False, sku: str = "copilot_ai_credit",
              finality: str = "final") -> None:
        line, agg = b.make_ai_usage_row(
            date_utc=f"2026-09-{day:02d}", model=model, credits=credits,
            discount_credits=discount, principal=b.make_principal(i), unattributed=unattributed,
            organization=org, team=team, sku=sku, finality=finality)
        self.lines.append(line)
        self.aggs.append(agg)

    def pools(self, today: str = CLOSED_TODAY) -> list[PoolMonth]:
        cells, _ = pool.build_cells(self.aggs, self.lines, grain="day",
                                    entity_mode=self.entity_mode)
        return pool.pool_months(cells, self.lines, self.licenses, self.config, today=today,
                                entity_mode=self.entity_mode)

    def plans(self) -> list[PlanEvidence]:
        return pool.detect_plans(self.lines, self.licenses, self.config, month="2026-09",
                                 entity_mode=self.entity_mode)

    def summary(self, *, today: str = CLOSED_TODAY, verdicts: object = None, k: int = 5,
                plans_by_scenario: object = (), actions: Sequence[AdminAction] = ()
                ) -> CopilotSummary:
        return assemble_summary(
            cost_lines=self.lines, aggregates=self.aggs, licenses=self.licenses,
            activity=self.activity, pools=self.pools(today), plans=self.plans(),
            plans_by_scenario=plans_by_scenario, actions=actions,
            channel_verdicts=RECONCILED if verdicts is None else verdicts, window=WINDOW, k=k)


def p1_world(*, seat_lines: bool = True, org: str | None = "org-a") -> World:
    """C.P1: 1,000 Business + 200 Enterprise seats; pooled use 3,100,000 credits of which
    2,680,000 discounted (the pool) → overage 420,000 credits ($4,200); direct review 15,000 credits
    ($150); Actions minutes of code review ($6). Teams: alpha 11, beta 10, gamma 10 users."""
    w = World()
    if seat_lines:
        w.lines += [b.make_seat_line("business", "1000", date_utc="2026-09-01", organization=org),
                    b.make_seat_line("enterprise", "200", date_utc="2026-09-01",
                                     organization=org)]
    else:
        w.config.append(b.make_config("run_flags", {"pool_seats.enterprise.business": "1000",
                                                    "pool_seats.enterprise.enterprise": "200"}))
    teams = ("alpha", "beta", "gamma")
    for i in range(31):
        disc = "100000" if i < 26 else ("80000" if i == 26 else "0")
        w.usage("100000", i=i, day=(i % 28) + 1, discount=disc, team=teams[i % 3], org=org)
    w.usage("15000", i=900, day=15, unattributed=True, model="Code Review", org=org)
    w.lines.append(b.make_actions_line("1000", date_utc="2026-09-12", organization=org))
    return w


def p13_world() -> World:
    """C.P13: 100 seats from the activity report (plan unknown), pooled use 250,000 credits (net
    60,000 = GitHub's per-row net as if Business). Teams: alpha 58, beta 37, gamma 3 and delta 2
    seat holders; alpha works in VS Code, beta splits VS Code / JetBrains, gamma and delta use
    Neovim."""
    w = World()
    for i in range(100):
        team = ("alpha" if i < 58 else "beta" if i < 95 else "gamma" if i < 98 else "delta")
        w.licenses.append(b.make_license(
            b.make_principal(i), snapshot_date="2026-09-05", plan="unknown", team=team, org=None,
            source_kind="github.copilot_activity_report", assigned_via_team=None,
            last_activity_bucket="0-7" if i % 10 else "31-90"))
        w.usage("2500", i=i, day=10, discount="1900", team=team, org=None)
        if team == "alpha":
            ide = {"ide:vscode": 10}
        elif team == "beta":
            ide = {"ide:vscode": 5} if i % 2 else {"ide:intellij": 5}
        else:
            ide = {"ide:neovim": 3}
        w.activity.append(b.make_activity(b.make_principal(i), date_utc="2026-09-10", team=team,
                                          counts={"interactions": 10, **ide}))
    return w


def p14_world() -> World:
    """C.P14: 50 seats; seats API says Business, seat lines say Enterprise, statement Business."""
    w = World()
    for i in range(50):
        w.licenses.append(b.make_license(b.make_principal(i), snapshot_date="2026-09-05",
                                         plan="business", org="acme", team="alpha"))
        w.usage("1000", i=i, day=10, discount="1000", team="alpha", org="acme")
    w.lines.append(b.make_seat_line("enterprise", "50", date_utc="2026-09-01", organization="acme"))
    w.config.append(b.make_config("run_flags", {"plan.enterprise": "business"}))
    return w


def two_entity_world() -> World:
    """Two organizations (entity mode ``org``): acme with Enterprise seat lines, beta with an
    unknown plan (activity-report seats)."""
    w = World(entity_mode="org")
    w.lines.append(b.make_seat_line("enterprise", "10", date_utc="2026-09-01", organization="acme"))
    for i in range(10):
        w.usage("5000", i=i, day=5 + i, discount="3900", team="red", org="acme")
    for i in range(10, 30):
        w.licenses.append(b.make_license(
            b.make_principal(i), snapshot_date="2026-09-05", plan="unknown", team="blue",
            org="beta", source_kind="github.copilot_activity_report", assigned_via_team=None))
        w.usage("2000", i=i, day=12, discount="1900", team="blue", org="beta")
    w.lines.append(b.make_actions_line("500", date_utc="2026-09-12", organization="beta"))
    return w


def action_plan(*, headline: int = 100_000_000_000, headroom: int | None = 40_000_000_000,
                lever: str = "copilot.default_model_auto") -> ActionPlan:
    """A small Copilot aggregate plan (ESTIMATED list headline, list-equivalent headroom)."""
    shap = estimated(headline, Basis.LIST, note="aggregate cell replay")
    lv = LeverResult(lever_id=lever, lever_class="trajectory", params="auto", basis=Basis.LIST,
                     standalone=shap, shapley=shap, projected_monthly=shap, needs_eval=True,
                     upper_bound=False, group="g1", finding_ids=("f1",))
    return ActionPlan(
        joint_saving=shap, headline_monthly=shap, allowance_headroom_monthly=None, levers=(lv,),
        groups=(("g1", (lever,)),), method="shapley-exact", shapley_se=(), sample="cells",
        observed_rr=(("trajectory", "0.5", 3),),
        pool_headroom_monthly=None if headroom is None else estimated(
            headroom, Basis.LIST_EQUIVALENT, note="pool headroom"))


def admin_action(lever: str = "copilot.default_model_auto") -> AdminAction:
    return AdminAction(action_id="a1", lever_id=lever, admin_action="admin:model_policy",
                       where="enterprise settings", what="Set the default model to Auto.",
                       doc_url="https://docs.github.com/en/copilot/example", rest_file=None,
                       auth_note="enterprise owner", reach="0.8",
                       projection=estimated(50_000_000_000, Basis.LIST, note="projection"),
                       deadline="2026-10-01", needs_eval=True, tradeoff=False)


def result_of(summary: CopilotSummary | None) -> RunResult:
    return RunResult(command="copilot scan", window=(0, 0), inputs=(),
                     privacy=PrivacyInfo(content_tier=ContentTier.NONE, key_id=None,
                                         identity_mode="central", k=5, suppressed_groups=0),
                     rate_card=None, copilot=summary)


__all__ = ["CLOSED_TODAY", "OPEN_TODAY", "RECONCILED", "WINDOW", "World", "action_plan",
           "admin_action", "p13_world", "p14_world", "p1_world", "result_of",
           "two_entity_world"]

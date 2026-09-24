"""Area-local builders for the CP-PLAN acceptance worlds (addendum Appendix C.P1–P13 and the CP-PLAN
brief). Every record is synthetic and built with ``core.builders``; pools come from the real
``core.pool.build_cells`` / ``pool_months`` so the plan is tested on the shapes it receives."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from tokenbill.core import builders as b
from tokenbill.core import pool
from tokenbill.core.findings import build_finding, make_scope
from tokenbill.core.labels import Basis, exact
from tokenbill.core.pool import Cell
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    UsageAggregate,
)
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import ActionPlan, EvidenceItem, Finding, LeverResult, PoolMonth

#: nano-USD per AI credit ($0.01) and per USD.
C = 10**7
USD = 10**9
MONTH = "2026-09"
TODAY = "2026-10-20"          # 2026-09 is closed (past the 3-day report lag)
PRICER = FakePricer()


@dataclass
class World:
    """Report rows, seat lines, Actions lines, licenses, activity and configuration of one
    synthetic Copilot enterprise."""

    month: str = MONTH
    lines: list[CostLine] = field(default_factory=list)
    aggs: list[UsageAggregate] = field(default_factory=list)
    licenses: list[LicenseSnapshot] = field(default_factory=list)
    activity: list[ActivityDay] = field(default_factory=list)
    config: list[ConfigSnapshot] = field(default_factory=list)
    entity_mode: str = "enterprise"
    _n: int = 0

    def usage(self, credits: int, *, model: str = "Claude Sonnet 5", team: str | None = "t1",
              users: int = 1, date: str | None = None, org: str | None = "org-a",
              cost_center: str | None = None, unattributed: bool = False,
              discount: int = 0, input_tokens: int = 0, output_tokens: int = 0,
              cache_read: int = 0, finality: str = "final") -> World:
        """AI usage report rows totalling *credits* spread over *users* principals (the first
        takes the remainder); tokens go to the first row."""
        day = date or f"{self.month}-10"
        n = 1 if unattributed else users
        for i in range(n):
            share = credits // n + (credits % n if i == 0 else 0)
            disc = discount // n + (discount % n if i == 0 else 0)
            self._n += 1
            line, agg = b.make_ai_usage_row(
                date_utc=day, model=model, credits=str(share), discount_credits=str(disc),
                principal=None if unattributed else b.make_principal(f"u{self._n}"),
                unattributed=unattributed, organization=org, cost_center=cost_center,
                team=team, finality=finality,
                input_tokens=input_tokens if i == 0 else 0,
                output_tokens=output_tokens if i == 0 else 0,
                cache_read_tokens=cache_read if i == 0 else 0)
            self.lines.append(line)
            self.aggs.append(agg)
        return self

    def seats(self, plan: str, n: int, *, cost_center: str | None = None,
              org: str | None = "org-a") -> World:
        """A detailed-usage seat line (*n* seat-months of *plan*)."""
        self.lines.append(b.make_seat_line(plan, str(n), date_utc=f"{self.month}-01",
                                           cost_center=cost_center, organization=org))
        return self

    def report_seats(self, n: int, *, org: str = "org-a") -> World:
        """*n* activity-report seat holders (plan unknown, assignment unknown)."""
        for i in range(n):
            self.licenses.append(b.make_license(
                b.make_principal(f"s{i}"), snapshot_date=f"{self.month}-15", plan="unknown",
                org=org, assigned_via_team=None,
                source_kind="github.copilot_activity_report"))
        return self

    def flags(self, **pairs: str | int | bool) -> World:
        """Run flags of the CLI (``plan.<e>``, ``billing_mode.<e>``, … — dots as ``__``)."""
        attrs = {k.replace("__", "."): v for k, v in pairs.items()}
        self.config.append(b.make_config("run_flags", attrs, entity_id="run"))
        return self

    def cap(self, name: str, credits: int, *, policy: str | None = None) -> World:
        """A capped cost center (pool enabled at *credits*) and optionally its cap policy."""
        self.config.append(b.make_config(
            "cost_center", {"pool_enabled": True, "pool_target_credits": str(credits)},
            entity_id=f"cc:{name}", source_kind="github.cost_centers"))
        if policy is not None:
            self.config.append(b.make_config("run_flags", {f"capped_policy.{name}": policy},
                                             entity_id="run"))
        return self

    def ide(self, team: str | None, counts: Mapping[str, int], *, date: str | None = None,
            user: str = "a") -> World:
        """One metrics user-day of *team* with the given counts (``ide:*``, …)."""
        self.activity.append(b.make_activity(
            b.make_principal(f"{team}-{user}"), date_utc=date or f"{self.month}-10", team=team,
            counts=counts))
        return self

    def actions(self, minutes: str, *, sku: str = "linux_16_core", usd_per_minute: str = "0.042",
                workload: str | None = "copilot_code_review") -> World:
        self.lines.append(b.make_actions_line(minutes, sku=sku, usd_per_minute=usd_per_minute,
                                              workload=workload, date_utc=f"{self.month}-12"))
        return self

    def cells(self, *, grain: str = "month") -> list[Cell]:
        capped = pool.capped_cost_centers(self.config)
        return pool.build_cells(self.aggs, self.lines, grain=grain, capped=capped,
                                entity_mode=self.entity_mode)[0]

    def pools(self, *, today: str = TODAY, grain: str = "month") -> list[PoolMonth]:
        return pool.pool_months(self.cells(grain=grain), self.lines, self.licenses, self.config,
                                today=today, entity_mode=self.entity_mode)


def finding(kind: str, *, detector: str = "copilot.org-scan", evidence: Sequence[EvidenceItem] = (),
            lever_ids: Sequence[str] = (), **dims: str) -> Finding:
    """A valid Copilot finding of *kind* (``product=copilot`` scope with *dims*)."""
    return build_finding(
        detector_id=detector, kind=kind, detector_version="1", category="lever",
        lever_class="rate", audience="org", title=f"{kind} (test)", summary="synthetic finding",
        scope=make_scope(product="copilot", **dims), n_events=1, n_lanes=0, n_users=5,
        first_seen_ms=0, cost_observed=exact(0, Basis.LIST), recoverable=None,
        lever_ids=tuple(lever_ids), evidence=tuple(evidence), references=("cp-plan-test",))


def seat_finding(kind: str, counts: Mapping[str, int], *, strings: Mapping[str, str] | None = None,
                 **dims: str) -> Finding:
    """A seat finding of ``copilot.seats-budgets`` carrying *counts* as evidence attrs."""
    attrs: list[tuple[str, str | int]] = sorted(counts.items())
    attrs += sorted((strings or {}).items())
    return finding(kind, detector="copilot.seats-budgets",
                   evidence=(EvidenceItem("aggregate", f"{kind}:seats", tuple(attrs)),), **dims)


def idle(removable: int = 0, *, team_assigned: int = 0, unknown: int = 0, plan: str = "business",
         bucket: str = "none_90d", entity: str = "enterprise", team: str = "t1",
         **dims: str) -> Finding:
    counts = {"removable": removable, "team_assigned": team_assigned,
              "assignment_unknown": unknown}
    return seat_finding("idle-seat", counts, entity=entity, team=team, plan=plan, bucket=bucket,
                        **dims)


def plan_for(world: World, findings: Iterable[Finding], *, include_tradeoffs: bool = False,
             forecast: bool = False, pools: Sequence[PoolMonth] | None = None,
             scenario: str | None = None, today: str = TODAY, grain: str = "month"
             ) -> ActionPlan:
    from tokenbill.copilot.plan import plan_copilot
    return plan_copilot(world.cells(grain=grain),
                        pools if pools is not None else world.pools(today=today, grain=grain),
                        list(findings), PRICER, lines=world.lines, activity=world.activity,
                        month=world.month, include_tradeoffs=include_tradeoffs,
                        forecast=forecast, config=world.config, scenario=scenario)


def lever(plan: ActionPlan, lever_id: str, basis: Basis = Basis.LIST) -> LeverResult:
    """The plan's LeverResult of *lever_id* on *basis* (exactly one)."""
    got = [lv for lv in plan.levers if lv.lever_id == lever_id and lv.basis is basis]
    assert len(got) == 1, [(lv.lever_id, lv.basis) for lv in plan.levers]
    return got[0]


def has_lever(plan: ActionPlan, lever_id: str) -> bool:
    return any(lv.lever_id == lever_id for lv in plan.levers)


def p1_world(use: int, *, team: str = "t1") -> World:
    """Appendix C.P1 entity: 1,000 Business + 200 Enterprise seats (seat lines), *use* pooled
    credits of Claude Sonnet 5 on one team, closed month."""
    return World().seats("business", 1000).seats("enterprise", 200).usage(use, team=team,
                                                                          users=10)

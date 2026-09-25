"""Area-local builders for the CP-POLICY tests: synthetic plans, findings, pools and cells.

Every record is synthetic (``core.builders``); findings go through ``core.findings.build_finding``
(the real validator) with ``product=copilot`` scopes; cells and pools of the budget worlds come
from the real ``core.pool`` functions.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

from tokenbill.core import builders as b
from tokenbill.core import catalog
from tokenbill.core import pool as cpool
from tokenbill.core.findings import build_finding, make_scope
from tokenbill.core.labels import Basis, Figure, estimated, exact
from tokenbill.core.pool import Cell
from tokenbill.core.records import ConfigSnapshot, CostLine, UsageAggregate
from tokenbill.core.types import ActionPlan, EvidenceItem, Finding, Fix, LeverResult, PoolMonth

USD = 10**9
C = 10**7                      # nano-USD per AI credit
TODAY = "2026-09-25"
MONTH = "2026-09"


def fig(usd: int, *, basis: Basis = Basis.LIST, note: str = "test projection",
        low: int | None = None, high: int | None = None, upper_bound: bool = False) -> Figure:
    """An ESTIMATED figure of *usd* dollars (optionally with a dollar range)."""
    return estimated(usd * USD, basis, note=note, upper_bound=upper_bound,
                     low=None if low is None else low * USD,
                     high=None if high is None else high * USD)


def result(lever_id: str, usd: int, *, basis: Basis = Basis.LIST, params: str | None = None,
           upper_bound: bool = False) -> LeverResult:
    """A LeverResult of *lever_id* projecting *usd* per month."""
    lv = catalog.lever(lever_id)
    grid = catalog.AGGREGATE_GRIDS.get(lever_id, ("copilot:auto=on@all",))
    f = fig(usd, basis=basis, upper_bound=upper_bound)
    return LeverResult(lever_id=lever_id, lever_class=lv.lever_class, params=params or grid[0],
                       basis=basis, standalone=f, shapley=f, projected_monthly=f,
                       needs_eval=lv.needs_eval, upper_bound=upper_bound or lv.upper_bound,
                       group="copilot" if basis is not Basis.LIST_EQUIVALENT
                       else "copilot:pool_headroom", finding_ids=())


def plan(*levers: LeverResult) -> ActionPlan:
    """An ActionPlan holding *levers* (headline = Σ of the billed results)."""
    total = sum(lv.projected_monthly.nano or 0 for lv in levers
                if lv.basis is not Basis.LIST_EQUIVALENT)
    head = estimated(total, Basis.LIST, note="test headline")
    return ActionPlan(joint_saving=head, headline_monthly=head, allowance_headroom_monthly=None,
                      levers=tuple(levers),
                      groups=(("copilot", tuple(lv.lever_id for lv in levers)),),
                      method="shapley-exact", shapley_se=(), sample="test plan")


def ev(ref: str, **attrs: str | int) -> EvidenceItem:
    return EvidenceItem(kind="aggregate", ref=ref, attrs=tuple(attrs.items()))


def finding(kind: str, *, detector: str = "copilot.org-scan", evidence: Sequence[EvidenceItem] = (),
            lever_ids: Sequence[str] = (), fix: Fix | None = None, title: str | None = None,
            summary: str = "synthetic finding", **dims: str) -> Finding:
    """A valid Copilot finding of *kind* (``product=copilot`` scope; entity ``enterprise`` unless
    given). ``build_finding`` gives it the catalog's Copilot fix; *fix* replaces that afterwards
    (to test hostile fixes)."""
    dims.setdefault("entity", "enterprise")
    f = build_finding(
        detector_id=detector, kind=kind, detector_version="1", category="lever",
        lever_class="rate", audience="org", title=title or f"{kind} (test)", summary=summary,
        scope=make_scope(product="copilot", **dims), n_events=1, n_lanes=0, n_users=5,
        first_seen_ms=0, cost_observed=exact(0, Basis.LIST), recoverable=None,
        lever_ids=tuple(lever_ids), evidence=tuple(evidence), references=("cp-policy-test",))
    return dataclasses.replace(f, fix=fix) if fix is not None else f


def seats_finding(kind: str, **kw: object) -> Finding:
    return finding(kind, detector="copilot.seats-budgets", **kw)  # type: ignore[arg-type]


def idle_seat(*, removable: int = 0, team: int = 0, unknown: int = 0,
              lever_ids: Sequence[str] = ("copilot.seat_reclaim",), **dims: str) -> Finding:
    """An ``idle-seat`` finding with CP-DET-SEATS's evidence layout (``assignment:*`` refs)."""
    dims.setdefault("team", "t1")
    evidence = [ev("assignment:removable", n=removable), ev("assignment:team", n=team),
                ev("assignment:unknown", n=unknown)]
    return seats_finding("idle-seat", evidence=evidence, lever_ids=lever_ids, **dims)


def pools_known(**kw: object) -> list[PoolMonth]:
    return [b.make_pool_month(**kw)]  # type: ignore[arg-type]


def pools_unknown(seats: int = 100, consumed_usd: int = 2_500) -> list[PoolMonth]:
    """The C.P13 pair: *seats* unknown-plan seats, one pool month per scenario."""
    return [b.make_pool_month(seats={"unknown": str(seats)}, plan_scenario=s,
                              consumed_report_nano=consumed_usd * USD)
            for s in ("business", "enterprise")]


# ---------------------------------------------------------------------------------------------
# the 3-cost-center metered budget world
# ---------------------------------------------------------------------------------------------


class BudgetWorld:
    """Report rows (per user), seat lines and cost-center snapshots of one metered enterprise."""

    def __init__(self, month: str = MONTH) -> None:
        self.month = month
        self.lines: list[CostLine] = []
        self.aggs: list[UsageAggregate] = []
        self.config: list[ConfigSnapshot] = []
        self._n = 0

    def cost_center(self, name: str, per_user_credits: Sequence[int], *, seats: int | None = None,
                    n_users: int | None = None) -> BudgetWorld:
        for credits in per_user_credits:
            self._n += 1
            line, agg = b.make_ai_usage_row(
                date_utc=f"{self.month}-10", model="Claude Sonnet 5", credits=str(credits),
                principal=b.make_principal(f"{name}-{self._n}"), cost_center=name, team=None)
            self.lines.append(line)
            self.aggs.append(agg)
        if seats is not None:
            self.lines.append(b.make_seat_line("business", str(seats), date_utc=f"{self.month}-01",
                                               cost_center=name))
        users = n_users if n_users is not None else len(per_user_credits)
        self.config.append(b.make_config("cost_center", {"n_users": users,
                                                         "cost_center_id": f"id-{name}"},
                                         entity_id=f"cc:{name}"))
        return self

    def cells(self) -> list[Cell]:
        return cpool.build_cells(self.aggs, self.lines, grain="month")[0]

    def consumed_nano(self) -> int:
        return sum(line.list_amount_nano or 0 for line in self.lines
                   if line.cost_type == "ai_credit.user")


def three_cost_centers() -> BudgetWorld:
    """A (180% of 19,000 credits), B and C: 10 users and 10 Business seats each."""
    w = BudgetWorld()
    w.cost_center("A", [3_000] * 9 + [7_200], seats=10)
    w.cost_center("B", [1_500] * 10, seats=10)
    w.cost_center("C", [1_200] * 10, seats=10)
    return w


def budget_pools(w: BudgetWorld, *, billing_mode: str = "metered",
                 scenarios: bool = False) -> list[PoolMonth]:
    consumed = w.consumed_nano()
    if scenarios:
        return [b.make_pool_month(seats={"unknown": "30"}, plan_scenario=s,
                                  consumed_report_nano=consumed, billing_mode=billing_mode)
                for s in ("business", "enterprise")]
    return [b.make_pool_month(seats={"business": "30"}, consumed_report_nano=consumed,
                              billing_mode=billing_mode)]


def teams_with(shares: Mapping[str, Mapping[str, int]], people: int = 6) -> dict[str,
                                                                               dict[str, int]]:
    """Team counts with *people* per team."""
    return {team: {**counts, "n_people": people} for team, counts in shares.items()}

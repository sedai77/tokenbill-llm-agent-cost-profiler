"""Area-local builders for the CP-DET-SEATS acceptance worlds (addendum Appendix C.P1–P15, §10.1).

Every record is synthetic and built with ``core.builders``; pool months come either from
``core.builders.make_pool_month`` (the hand-computed Appendix C states) or from the real
``core.pool`` (``detect_plans`` / ``build_cells`` / ``pool_months``) on builder-made report rows,
licenses and configuration, exactly as the enricher (CP-STORE) fills ``AnalysisContext``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence

from tokenbill.core import builders as b
from tokenbill.core import pool as cpool
from tokenbill.core import testing as kit
from tokenbill.core.records import ConfigSnapshot, CostLine, LicenseSnapshot, UsageAggregate
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding, PlanEvidence, PoolMonth
from tokenbill.detect.copilot_seats import CopilotSeatsBudgets

#: nano-USD per AI credit ($0.01) and per USD.
C = 10**7
USD = 10**9
DAY_MS = 86_400_000


def ms(date: str) -> int:
    """Epoch milliseconds of a UTC date (the start of the day, plus one hour)."""
    return (dt.date.fromisoformat(date) - dt.date(1970, 1, 1)).days * DAY_MS + 3_600_000


def people(n: int, seed: str = "u") -> list[str]:
    return [b.make_principal(f"{seed}{i}") for i in range(n)]


def ctx(*, pools: Iterable[PoolMonth] = (), plans: Iterable[PlanEvidence] = (),
        licenses: Iterable[LicenseSnapshot] = (), config: Iterable[ConfigSnapshot] = (),
        activity: Iterable = (), cost_lines: Iterable[CostLine] = (),
        reconciled: Iterable[str] = (), today: str | None = None, min_usd: str | None = None,
        k: int = 5) -> AnalysisContext:
    thresholds = {"min_usd": min_usd} if min_usd is not None else {}
    return AnalysisContext(
        pricer=kit.FakePricer(), rules=None, replayer=None, calibration=None,
        window=(ms("2026-06-01"), ms("2026-12-01")), capabilities=frozenset({"ext:copilot"}),
        thresholds=thresholds, k_anonymity=k, now_ms=ms(today) if today else 0,
        pools=tuple(pools), plans=tuple(plans), licenses=tuple(licenses), config=tuple(config),
        activity=tuple(activity), cost_lines=tuple(cost_lines),
        reconciled_channels=frozenset(reconciled))


def detect(context: AnalysisContext) -> list[Finding]:
    return CopilotSeatsBudgets().detect([], context)


def of_kind(findings: Sequence[Finding], kind: str) -> list[Finding]:
    return [f for f in findings if f.kind == kind]


def dims(f: Finding) -> dict[str, str]:
    return dict(f.scope.dims)


def evidence(f: Finding, ref: str) -> dict[str, str | int]:
    """The attrs of *f*'s evidence item *ref* (KeyError when absent)."""
    item: EvidenceItem = next(e for e in f.evidence if e.ref == ref)
    return dict(item.attrs)


def by_scenario(findings: Sequence[Finding]) -> dict[str | None, Finding]:
    out = {dims(f).get("plan_scenario"): f for f in findings}
    assert len(out) == len(findings)
    return out


# ---------------------------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------------------------


def seats(n: int, *, plan: str = "business", org: str | None = "org-a", team: str | None = "t1",
          seed: str = "s", date: str = "2026-09-30", bucket: str = "none_90d",
          via_team: bool | None = False, created: str | None = "2026-01-05",
          cost_center: str | None = None, pending: str | None = None,
          source_kind: str = "github.copilot_seats") -> list[LicenseSnapshot]:
    """*n* seats (seats API by default; idle: last activity over 90 days ago)."""
    return [b.make_license(p, snapshot_date=date, plan=plan, team=team, org=org,
                           cost_center=cost_center, seat_created=created,
                           pending_cancellation=pending, last_activity_bucket=bucket,
                           assigned_via_team=via_team, source_kind=source_kind)
            for p in people(n, seed)]


def activity_report(n: int, *, seed: str = "r", date: str = "2026-10-31", team: str | None = "t1",
                    org: str | None = "org-a", bucket: str = "none_90d"
                    ) -> list[LicenseSnapshot]:
    """*n* activity-report seat holders (plan unknown, assignment unknown, age unknown)."""
    return [b.make_license(p, snapshot_date=date, plan="unknown", team=team, org=org,
                           seat_created=None, last_activity_bucket=bucket,
                           assigned_via_team=None,
                           source_kind="github.copilot_activity_report")
            for p in people(n, seed)]


def org_settings(org: str, policy: str, *, plan_type: str | None = None,
                 date: str = "2026-09-02", stated: bool = False) -> ConfigSnapshot:
    attrs: dict[str, str | int | bool | None] = {"seat_management_setting": policy}
    if plan_type is not None:
        attrs["plan_type"] = plan_type
    return b.make_config("org_settings", attrs, entity_id=f"org:{org}",
                         source_kind=("tokenbill.admin_answers" if stated
                                      else "github.org_copilot_settings"),
                         snapshot_ms=ms(date))


def flags(pairs: Mapping[str, str | int | bool | None], entity: str = "run") -> ConfigSnapshot:
    source = "tokenbill.admin_answers" if entity == "admin_answers" else "tokenbill.cli"
    return b.make_config("run_flags", dict(pairs), entity_id=entity, source_kind=source)


def budget(bid: str, scope: str, amount_usd: int, *, stop: bool | None = False,
           target: str | None = "enterprise", team: str | None = None,
           cost_center: str | None = None, sku: str | None = "ai_credits",
           date: str = "2026-09-20") -> ConfigSnapshot:
    attrs: dict[str, str | int | bool | None] = {
        "scope": scope, "type": "BundlePricing", "sku": sku, "amount_nano": amount_usd * USD,
        "prevent_further_usage": stop, "target": target, "team": team,
        "cost_center": cost_center}
    return b.make_config("budget", {k: v for k, v in attrs.items() if v is not None},
                         entity_id=f"budget:{bid}", source_kind="github.budgets",
                         snapshot_ms=ms(date))


def cost_center(name: str, *, pool_enabled: bool = False, cap: str | None = None,
                n_users: int | None = None, date: str = "2026-09-02") -> ConfigSnapshot:
    attrs: dict[str, str | int | bool | None] = {"pool_enabled": pool_enabled}
    if cap is not None:
        attrs["pool_target_credits"] = cap
    if n_users is not None:
        attrs["n_users"] = n_users
    return b.make_config("cost_center", attrs, entity_id=f"cc:{name}",
                         source_kind="github.cost_centers", snapshot_ms=ms(date))


def seat_count(org: str, n: int, *, team: str = "t1", plan: str = "business",
               bucket: str = "none_90d", via_team: bool | None = False,
               pending: bool = False, created: bool | None = True, zero_cost: bool | None = True,
               date: str = "2026-09-30") -> ConfigSnapshot:
    attrs: dict[str, str | int | bool | None] = {
        "team": team, "plan": plan, "bucket": bucket, "assigned_via_team": via_team,
        "pending_cancellation": pending, "created_over_30d": created,
        "zero_cost_30d": zero_cost, "n": n}
    return b.make_config("seat_counts", attrs, entity_id=f"org:{org}",
                         source_kind="tokenbill.copilot_export", snapshot_ms=ms(date))


def rows(credits: int, *, date: str, users: list[str] | None = None, discount: int = 0,
         model: str = "Claude Sonnet 5", org: str | None = "org-a",
         cost_center: str | None = None, team: str | None = None, finality: str = "final",
         unattributed: bool = False) -> tuple[list[CostLine], list[UsageAggregate]]:
    """AI usage report rows totalling *credits* (and *discount*) spread over *users*."""
    who: list[str | None] = [None] if unattributed else list(users or people(1, "x"))
    lines: list[CostLine] = []
    aggs: list[UsageAggregate] = []
    n = len(who)
    for i, principal in enumerate(who):
        share = credits // n + (credits % n if i == 0 else 0)
        disc = discount // n + (discount % n if i == 0 else 0)
        line, agg = b.make_ai_usage_row(
            date_utc=date, model=model, credits=str(share), discount_credits=str(disc),
            principal=principal, unattributed=unattributed, organization=org,
            cost_center=cost_center, team=team, finality=finality)
        lines.append(line)
        aggs.append(agg)
    return lines, aggs


# ---------------------------------------------------------------------------------------------
# pool months
# ---------------------------------------------------------------------------------------------

#: Appendix C.P1's entity: 1,000 Business + 200 Enterprise seats (pool 2,680,000 credits).
P1_SEATS = {"business": "1000", "enterprise": "200"}


def p_pool(consumed_credits: int, *, month: str = "2026-09", seats_map: Mapping[str, str] =
           P1_SEATS, **kw: object) -> PoolMonth:
    """A closed, metered Appendix C pool month (seat lines) at *consumed_credits*."""
    return b.make_pool_month(month=month, seats=dict(seats_map),
                             consumed_report_nano=consumed_credits * C, **kw)


def p_plan(*, month: str = "2026-09", plan: str = "mixed",
           seats_map: Mapping[str, int] | None = None, source: str = "seat_lines",
           entity: str = "enterprise", conflict: bool = False,
           lines: Iterable[str] = ()) -> PlanEvidence:
    return b.make_plan_evidence(entity_id=entity, month=month, plan=plan, source=source,
                                seats=dict(seats_map) if seats_map is not None
                                else {"business": 1000, "enterprise": 200},
                                conflict=conflict, evidence=tuple(lines))


def enrich(cost: Sequence[CostLine], aggs: Sequence[UsageAggregate],
           lics: Sequence[LicenseSnapshot] = (), conf: Sequence[ConfigSnapshot] = (), *,
           today: str, entity_mode: str = "enterprise",
           ) -> tuple[list[PoolMonth], list[PlanEvidence]]:
    """``core.pool`` as the enricher runs it: cells → pool months, and the plan evidence of every
    pool month."""
    conf = list(conf)
    cells, _ = cpool.build_cells(aggs, cost, capped=cpool.capped_cost_centers(conf),
                                 entity_mode=entity_mode)
    pms = cpool.pool_months(cells, cost, list(lics), conf, today=today, entity_mode=entity_mode)
    plans: list[PlanEvidence] = []
    for month in sorted({pm.month for pm in pms}):
        plans += cpool.detect_plans(cost, list(lics), conf, month=month, entity_mode=entity_mode)
    return pms, plans


METERED = flags({"billing_mode.enterprise": "metered"})

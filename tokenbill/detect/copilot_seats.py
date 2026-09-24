"""GitHub Copilot plan status, pool regime, forecast, promo cliff, seats and budgets (addendum
§10.1; package CP-DET-SEATS; registry id ``copilot.seats-budgets``).

:class:`CopilotSeatsBudgets` is an aggregate detector (``aggregate=True``, ``extension="copilot"``,
``families={"copilot"}``, ``requires=frozenset()``): it ignores lanes and reads the channel
extension's context fields — ``ctx.plans`` (:class:`~tokenbill.core.types.PlanEvidence`),
``ctx.pools`` (:class:`~tokenbill.core.types.PoolMonth`, two per entity × month while a plan is
unknown), ``ctx.licenses``, ``ctx.activity``, ``ctx.config`` and ``ctx.cost_lines``. Every pool
figure comes from ``core.pool`` (the pool months themselves, :func:`core.pool.realize_seat_change`
for seat projections); nothing here re-implements the pool rule (R11).

**Gating** (brief Build 1, replacing a whole-detector ``requires``): each kind lists the inputs it
needs in :data:`KIND_REQUIRES` (alternatives, any-of). ``plan-status`` always runs; pool kinds need
``ctx.pools``; seat kinds need licenses or ``seat_counts`` rows (aggregate-only bundles); the
person-level seat kinds need licenses; budget kinds need budget / cost-center snapshots. Every kind
that cannot run is named once in a single ``dq.skipped-kinds`` data-quality finding (no dollars).

**Scenarios** (R17, ruling R-E22). While an entity's plan is unknown, ``ctx.pools`` holds a
``business`` and an ``enterprise`` pool month; every pool-dependent kind (``pool-regime``,
``overage-forecast``, ``promo-cliff``, ``idle-seat``, ``seat-auto-assign``,
``budget-paid-usage-uncapped``, ``budget-ulb-gap``, ``budget-no-cost-center-pool``) is emitted once
per scenario with the scope dim ``plan_scenario``, every pool-derived figure ESTIMATED and the title
prefixed "If Business:" / "If Enterprise:". ``plan-mix`` and the "Business instead of Enterprise"
advice of ``completions-only-seat`` are suppressed while the plan is unknown.

**Labels** (R16, R-E20). Overage, direct-org and seat-line amounts are INVOICE only for a closed
month on a reconciled channel (``ctx.reconciled_channels`` ∋ ``github_copilot`` and
``PoolMonth.finality == "closed"``, which core.pool sets only when every day is final), else EXACT
LIST noted "unreconciled" (closed) or "provisional" (open). Seat fees of specific seats are count ×
list, ESTIMATED LIST; forecasts, conversions and the promo cliff are ESTIMATED. Credits
(LIST_EQUIVALENT) and dollars are never added in one figure. Categories: seat kinds ``lever``,
the others ``aggregate`` (k-anonymity follows the count source, ruling R-E16).

**Months.** Findings carry no month dim (``core.kanon`` would drop it and merge the months):
``plan-status`` and ``pool-regime`` describe each entity's latest month and list the earlier
months as evidence; seat projections use the entity's latest month with a known regime ("at
unchanged use").

People are never named: seat findings are counts per team (the admin gets a GitHub query, not a
list), per-user budgets are counts per team (≥ k) or at entity level, and REST text keeps GitHub's
``{org}`` / ``{enterprise}`` placeholders.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation

from tokenbill.core import catalog, facts
from tokenbill.core import pool as cpool
from tokenbill.core.errors import TokenbillError
from tokenbill.core.findings import (
    MAX_SUMMARY,
    MAX_TITLE,
    build_finding,
    make_scope,
    min_usd_gate,
    min_usd_nano,
)
from tokenbill.core.labels import (
    Basis,
    Calibration,
    Evidence,
    Figure,
    Finality,
    estimated,
    unpriced,
)
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, fmt_usd, nano_to_credits_str
from tokenbill.core.records import ConfigSnapshot, Lane, LicenseSnapshot
from tokenbill.core.types import (
    AnalysisContext,
    EvidenceItem,
    Finding,
    Fix,
    PlanEvidence,
    PoolMonth,
)

__all__ = [
    "BUDGET_KINDS",
    "DETECTOR_ID",
    "IDLE_BUCKETS",
    "KINDS",
    "KIND_REQUIRES",
    "POOL_KINDS",
    "PROMO_CLIFF_EXPIRES",
    "SCENARIO_KINDS",
    "SEAT_KINDS",
    "CopilotSeatsBudgets",
]

DETECTOR_ID = "copilot.seats-budgets"
VERSION = "1"

KINDS = ("plan-status", "pool-regime", "overage-forecast", "promo-cliff", "idle-seat",
         "seat-auto-assign", "completions-only-seat", "plan-mix", "duplicate-seat",
         "budget-paid-usage-uncapped", "budget-stop-usage-off", "budget-zero-user-budget",
         "budget-ulb-gap", "budget-org-multi-org-seats", "budget-no-cost-center-pool",
         "budget-enterprise-misread", "dq.skipped-kinds")
POOL_KINDS = ("pool-regime", "overage-forecast", "promo-cliff")
SEAT_KINDS = ("idle-seat", "seat-auto-assign", "completions-only-seat", "plan-mix",
              "duplicate-seat")
BUDGET_KINDS = ("budget-paid-usage-uncapped", "budget-stop-usage-off", "budget-zero-user-budget",
                "budget-ulb-gap", "budget-org-multi-org-seats", "budget-no-cost-center-pool",
                "budget-enterprise-misread")
#: Kinds emitted once per plan scenario while an entity's plan is unknown (R17).
SCENARIO_KINDS = frozenset({"pool-regime", "overage-forecast", "promo-cliff", "idle-seat",
                            "seat-auto-assign", "budget-paid-usage-uncapped", "budget-ulb-gap",
                            "budget-no-cost-center-pool"})


def _alts(*sets: Iterable[str]) -> tuple[frozenset[str], ...]:
    return tuple(frozenset(s) for s in sets)


#: Inputs each kind needs (alternatives, any-of: a kind runs when every input of one alternative is
#: present). Input names: ``pools``, ``plans``, ``licenses``, ``seat_counts``, ``activity``,
#: ``cost_lines``, ``budgets``, ``cost_centers``, ``org_settings``. ``plan-status`` needs nothing.
KIND_REQUIRES: Mapping[str, tuple[frozenset[str], ...]] = {
    "plan-status": _alts(()),
    "pool-regime": _alts({"pools"}),
    "overage-forecast": _alts({"pools"}),
    "promo-cliff": _alts({"pools"}),
    "idle-seat": _alts({"licenses"}, {"seat_counts"}),
    "seat-auto-assign": _alts({"licenses", "org_settings"}, {"seat_counts", "org_settings"}),
    "completions-only-seat": _alts({"licenses", "activity"}),
    "plan-mix": _alts({"licenses", "pools", "cost_lines"}, {"licenses", "pools", "activity"}),
    "duplicate-seat": _alts({"licenses"}),
    "budget-paid-usage-uncapped": _alts({"pools", "budgets"}),
    "budget-stop-usage-off": _alts({"budgets"}),
    "budget-zero-user-budget": _alts({"budgets"}),
    "budget-ulb-gap": _alts({"pools", "budgets"}),
    "budget-org-multi-org-seats": _alts({"budgets", "licenses"}),
    "budget-no-cost-center-pool": _alts({"cost_centers", "pools", "cost_lines", "licenses"}),
    "budget-enterprise-misread": _alts({"budgets", "plans"}),
}

#: ``last_activity_bucket`` values of an idle seat (no activity in the last 30 days).
IDLE_BUCKETS = ("31-90", "none_90d")
#: R15: ``promo-cliff`` is suppressed after this date (addendum §10.1).
PROMO_CLIFF_EXPIRES = "2026-11-30"

_COPILOT = "github_copilot"
_PRODUCT = "copilot"
_PLANS = ("business", "enterprise")
_POOLED = "ai_credit.user"
_SEATS_API = "github.copilot_seats"
_STATEMENTS = frozenset({"tokenbill.cli", "tokenbill.admin_answers"})
_USER_SCOPES = ("user", "multi_user_customer", "multi_user_cost_center")
_METERED_SCOPES = ("enterprise", "organization", "cost_center", "repository")
_NO_CHAT_FLAGS = frozenset({"used_chat", "used_agent", "used_cli", "used_copilot_app",
                            "used_cloud_agent"})
_CREDIT_NANO = 10_000_000
_DAY = _dt.timedelta(days=1)
_EPOCH = _dt.date(1970, 1, 1)
_WINDOW_DAYS = 30
_OVERDRAW = Decimal("1.5")        # budget-no-cost-center-pool: draw > 150% of the allowance
#: plan-mix looks at the last 3 closed months; with a shorter history (at least 2 closed months)
#: it uses those, says so and lowers its confidence.
_PLAN_MIX_MONTHS = 3
_PLAN_MIX_MIN_MONTHS = 2
_ASSIGNMENTS = ("removable", "team", "auto", "unknown")

_REFS: Mapping[str, tuple[str, ...]] = {
    "plan-status": ("copilot-billing/F1", "copilot-billing/F11", "addendum/R17"),
    "pool-regime": ("copilot-billing/F2", "copilot-cost-levers/F3", "addendum/R11"),
    "overage-forecast": ("copilot-billing/F8", "copilot-billing/F9", "addendum/R11"),
    "promo-cliff": ("copilot-billing/F13", "copilot-cost-levers/F5"),
    "idle-seat": ("copilot-cost-levers/F3", "copilot-cost-levers/F4", "copilot-billing/F11"),
    "seat-auto-assign": ("copilot-cost-levers/F4", "copilot-billing/F11"),
    "completions-only-seat": ("copilot-cost-levers/F16", "copilot-cost-levers/F3"),
    "plan-mix": ("copilot-cost-levers/F3", "copilot-billing/F1"),
    "duplicate-seat": ("copilot-billing/F11",),
    "budget-paid-usage-uncapped": ("copilot-billing/F8", "copilot-billing/F9"),
    "budget-stop-usage-off": ("copilot-billing/F9", "copilot-cost-levers/F6"),
    "budget-zero-user-budget": ("copilot-cost-levers/F7", "copilot-billing/F9"),
    "budget-ulb-gap": ("copilot-cost-levers/F6", "copilot-billing/F9"),
    "budget-org-multi-org-seats": ("copilot-billing/F9", "copilot-billing/F11"),
    "budget-no-cost-center-pool": ("copilot-billing/F9", "copilot-billing/F22"),
    "budget-enterprise-misread": ("copilot-cost-levers/F7", "copilot-billing/F9"),
    "dq.skipped-kinds": ("dq.skipped-kinds",),
}
_NO_FIX = "No mechanical fix"


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _date(text: object) -> _dt.date | None:
    """The date of a ``YYYY-MM-DD…`` string (ISO timestamps accepted), else None."""
    if not isinstance(text, str) or len(text) < 10:
        return None
    try:
        return _dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_of_ms(ms: int) -> _dt.date:
    return _EPOCH + _dt.timedelta(days=ms // 86_400_000)


def _month_end(month: str) -> _dt.date:
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 12:
        return _dt.date(year, 12, 31)
    return _dt.date(year, mon + 1, 1) - _DAY


def _next_first(day: _dt.date) -> str:
    year, mon = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return f"{year:04d}-{mon:02d}-01T00:00Z"


def _dec(value: object) -> Decimal | None:
    """A finite Decimal from an int or a decimal string (None otherwise; bools excluded)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str) and 0 < len(value.strip()) <= 64:
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            return None
        if d.is_finite() and abs(d) < Decimal(10) ** 18:
            return d
    return None


def _int_nano(value: object) -> int | None:
    """A nano amount from an int or an integral decimal string."""
    d = _dec(value)
    if d is None or d != d.to_integral_value():
        return None
    return int(d)


def _flag(value: object) -> bool | None:
    """A boolean from a bool or the strings ``true``/``false`` (``yes``/``no``, ``1``/``0``)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "yes", "1", "on", "enabled"):
            return True
        if text in ("false", "no", "0", "off", "disabled"):
            return False
    return None


def _seat_price(plan: str) -> Decimal:
    return facts.copilot_plans()[plan].seat_usd_per_month


def _usd_nano(amount: Decimal) -> int:
    return decimal_to_nano(amount)


def _credits(nano: int) -> str:
    return nano_to_credits_str(nano)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:") + "…"


def _title(scenario: str | None, body: str) -> str:
    prefix = f"If {scenario.capitalize()}: " if scenario else ""
    return prefix + _cut(body, MAX_TITLE - len(prefix))


def _summary(*parts: str) -> str:
    """Whole parts in order, most important first: a part that no longer fits is left out (never
    cut mid-sentence), unless it is the first, which is cut at a word boundary."""
    out: list[str] = []
    used = 0
    for part in (p.strip() for p in parts if p and p.strip()):
        extra = len(part) + (1 if out else 0)
        if used + extra <= MAX_SUMMARY:
            out.append(part)
            used += extra
        elif not out:
            return _cut(part, MAX_SUMMARY)
    return " ".join(out)


def _scenario_sentence(scenario: str | None) -> str:
    if scenario is None:
        return ""
    return f"Scenario: if all unknown seats are {scenario.capitalize()} (plan unknown)."


def _ev(ref: str, **attrs: str | int | bool | None) -> EvidenceItem:
    items: list[tuple[str, str | int]] = []
    for key, value in attrs.items():
        if value is None:
            continue
        if isinstance(value, bool):
            items.append((key, "true" if value else "false"))
        else:
            items.append((key, value))
    return EvidenceItem(kind="aggregate", ref=ref, attrs=tuple(items))


def _fig_attrs(fig: Figure | None) -> dict[str, str | int | bool | None]:
    """Evidence attrs of a figure: point, range and label."""
    if fig is None:
        return {"projection": "none"}
    return {"nano": fig.nano, "low_nano": fig.low_nano, "high_nano": fig.high_nano,
            "label": f"{fig.evidence.value} {fig.basis.value}",
            "upper_bound": fig.upper_bound or None}


def _with_note(fig: Figure, note: str) -> Figure:
    if not note or note in fig.note:
        return fig
    return dataclasses.replace(fig, note=f"{fig.note}; {note}" if fig.note else note)


def _money(nano: int | None) -> str:
    return fmt_usd(nano) if nano is not None else "unpriced"


# ---------------------------------------------------------------------------------------------
# seats: normalized idle-seat groups from licenses or seat_counts rows
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Group:
    """Idle seats of one (entity, team, org, plan, assignment) group."""

    n: int = 0
    age_unknown: int = 0
    cost_unknown: int = 0
    buckets: dict[str, int] = dataclasses.field(default_factory=dict)


_GroupKey = tuple[str, str | None, str | None, str, str]   # entity, team, org, plan, assignment


@dataclasses.dataclass
class _SeatView:
    """The current seats, their idle groups, multi-org principals and per-person facts."""

    source: str                                   # "licenses" | "seat_counts" | "none"
    idle: dict[_GroupKey, _Group]
    seats_by_entity: dict[str, int]
    multi_org: dict[str, list[tuple[str | None, int]]]   # entity → [(team, n principals)]
    current: dict[str, LicenseSnapshot]           # principal → representative seat
    all_rows: dict[str, list[LicenseSnapshot]]    # principal → its current rows (one per org)
    snapshot: _dt.date | None


class _Run:
    """One detection run over one context."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.config: list[ConfigSnapshot] = sorted(
            (c for c in ctx.config if isinstance(c, ConfigSnapshot)),
            key=lambda c: (c.kind, c.entity_id, c.snapshot_ms, c.fetched_ms, c.source_kind,
                           repr(c.attrs)))
        entity_ids = [pm.entity_id for pm in ctx.pools] + [pe.entity_id for pe in ctx.plans]
        self.mode = "org" if any(e.startswith("org:") for e in entity_ids) else "enterprise"
        self.capped = cpool.capped_cost_centers(self.config)
        self.flags = cpool.run_flags(self.config)
        self.pools: dict[str, dict[str, list[PoolMonth]]] = defaultdict(lambda: defaultdict(list))
        for pm in sorted(ctx.pools, key=lambda p: (p.entity_id, p.month, p.plan_scenario or "",
                                                   repr(p))):
            self.pools[pm.entity_id][pm.month].append(pm)
        self.plans = self._plans()
        self.licenses = sorted((x for x in ctx.licenses if x.product == _COPILOT),
                               key=_license_order)
        self.activity = sorted((a for a in ctx.activity if a.product == _COPILOT),
                               key=lambda a: (a.date_utc, a.principal, repr(a)))
        self.cost_lines = sorted((c for c in ctx.cost_lines if c.channel == _COPILOT),
                                 key=lambda c: (c.date_utc, c.line_id, repr(c)))
        self.inputs = self._inputs()
        self.min_usd = min_usd_nano(ctx)
        self.k = ctx.k_anonymity if isinstance(ctx.k_anonymity, int) else 5
        self.reconciled = _COPILOT in ctx.reconciled_channels
        self.org_policy = self._org_policies()
        self.seats = self._seat_view()
        self.today = (_date_of_ms(ctx.now_ms) if ctx.now_ms > 0 else self._data_today())
        self.skipped: list[tuple[str, str]] = []

    # ----- inputs ------------------------------------------------------------------------------

    def _plans(self) -> dict[str, dict[str, PlanEvidence]]:
        out: dict[str, dict[str, PlanEvidence]] = defaultdict(dict)
        plans = list(self.ctx.plans)
        if not plans:
            plans = self._detected_plans()
        for pe in sorted(plans, key=lambda p: (p.entity_id, p.month, repr(p))):
            out[pe.entity_id][pe.month] = pe
        return out

    def _detected_plans(self) -> list[PlanEvidence]:
        """``core.pool.detect_plans`` for every month with pool, seat or seat-count data (used
        only when the enricher supplied no plans)."""
        months = {pm.month for pm in self.ctx.pools}
        months.update(x.snapshot_date[:7] for x in self.ctx.licenses if x.product == _COPILOT)
        months.update(_date_of_ms(c.snapshot_ms).isoformat()[:7] for c in self.ctx.config
                      if c.kind == "seat_counts")
        out: list[PlanEvidence] = []
        for month in sorted(months):
            try:
                out.extend(cpool.detect_plans(self.ctx.cost_lines, self.ctx.licenses,
                                              self.config, month=month, entity_mode=self.mode))
            except TokenbillError:
                continue
        return out

    def _inputs(self) -> frozenset[str]:
        have: set[str] = set()
        if self.ctx.pools:
            have.add("pools")
        if any(self.plans.values()):
            have.add("plans")
        if self.licenses:
            have.add("licenses")
        if self.activity:
            have.add("activity")
        if any(c.cost_type == _POOLED for c in self.cost_lines):
            have.add("cost_lines")
        kinds = {c.kind for c in self.config}
        for kind, name in (("budget", "budgets"), ("cost_center", "cost_centers"),
                           ("seat_counts", "seat_counts")):
            if kind in kinds:
                have.add(name)
        if any(c.kind == "org_settings" and dict(c.attrs).get("seat_management_setting")
               for c in self.config):
            have.add("org_settings")
        return frozenset(have)

    def runs(self, kind: str) -> bool:
        """True when *kind*'s inputs are present; otherwise the kind is recorded as skipped."""
        alternatives = KIND_REQUIRES[kind]
        if any(alt <= self.inputs for alt in alternatives):
            return True
        missing = " or ".join("+".join(sorted(alt - self.inputs)) for alt in alternatives)
        self.skipped.append((kind, missing))
        return False

    def _data_today(self) -> _dt.date | None:
        dates = [_date(x.snapshot_date) for x in self.licenses]
        dates += [_date(a.date_utc) for a in self.activity]
        dates += [_date(c.date_utc) for c in self.cost_lines]
        dates += [_month_end(m) for months in self.pools.values() for m in months]
        known = [d for d in dates if d is not None]
        return max(known) if known else None

    # ----- entities, pools, plans --------------------------------------------------------------

    def entity(self, cost_center: str | None, org: str | None) -> str:
        return cpool.entity_of(cost_center, org, capped=self.capped, entity_mode=self.mode)

    def config_entity(self, entity_id: str | None) -> str | None:
        """The pool entity of a configuration row's target (``enterprise`` / ``org:`` / ``cc:``)."""
        if not isinstance(entity_id, str):
            return None
        if entity_id == "enterprise":
            return "enterprise"
        if entity_id.startswith("cc:") and len(entity_id) > 3:
            return self.entity(entity_id[3:], None)
        if entity_id.startswith("org:") and len(entity_id) > 4:
            return self.entity(None, entity_id[4:])
        return None

    def latest_pms(self, entity: str) -> list[PoolMonth]:
        months = self.pools.get(entity)
        if not months:
            return []
        return months[max(months)]

    def reference_pms(self, entity: str) -> list[PoolMonth]:
        """The pool months seat projections use: the latest month with a known regime, else the
        latest month."""
        months = self.pools.get(entity)
        if not months:
            return []
        for month in sorted(months, reverse=True):
            if all(pm.regime != "unknown" for pm in months[month]):
                return months[month]
        return months[max(months)]

    def latest_plan(self, entity: str) -> PlanEvidence | None:
        months = self.plans.get(entity)
        if not months:
            return None
        return months[max(months)]

    def plan_unknown(self, entity: str) -> bool:
        """True while the entity's plan is unknown (scenario pool months or unknown seats)."""
        if any(pm.plan_scenario is not None for pm in self.latest_pms(entity)):
            return True
        pe = self.latest_plan(entity)
        return pe is not None and (pe.plan == "unknown" or dict(pe.seats).get("unknown", 0) > 0)

    def scenarios(self, entity: str) -> tuple[str | None, ...]:
        """``(None,)`` for a known plan, else ``("business", "enterprise")``."""
        pms = self.reference_pms(entity)
        found = sorted({pm.plan_scenario for pm in pms}, key=lambda s: s or "")
        if pms:
            return tuple(found)
        return cpool.SCENARIOS if self.plan_unknown(entity) else (None,)

    def pm_for(self, entity: str, scenario: str | None) -> PoolMonth | None:
        for pm in self.reference_pms(entity):
            if pm.plan_scenario == scenario:
                return pm
        return None

    def unknown_plan_as(self, entity: str, scenario: str | None) -> str | None:
        """The plan unknown seats count under: the scenario, else the entity's single known
        plan, else None (a mixed entity cannot place them)."""
        if scenario is not None:
            return scenario
        pe = self.latest_plan(entity)
        if pe is not None and pe.plan in _PLANS:
            return pe.plan
        pms = self.reference_pms(entity)
        known = {p for pm in pms for p, n in pm.seats if p in _PLANS and _dec(n)}
        return known.pop() if len(known) == 1 else None

    def billing_mode(self, entity: str) -> str:
        pms = self.latest_pms(entity)
        if pms:
            return pms[0].billing_mode
        return cpool.billing_modes(self.cost_lines, self.config, self.licenses).get(
            entity, "unknown")

    def renewal(self, entity: str) -> str | None:
        value = self.flags.get(f"renewal_date.{entity}")
        return value if isinstance(value, str) and _date(value) is not None else None

    def r16(self, nano: int, pm: PoolMonth, what: str) -> Figure:
        """An invoice-side amount of *pm* labelled per R16."""
        if pm.plan_scenario is not None:
            return estimated(nano, Basis.LIST,
                             note=f"{what}: scenario {pm.plan_scenario} (plan unknown)")
        if pm.finality == "closed":
            if self.reconciled:
                return Figure(nano=nano, evidence=Evidence.EXACT, basis=Basis.INVOICE,
                              finality=Finality.FINAL,
                              note=f"{what}: closed month, reconciled channel")
            return Figure(nano=nano, evidence=Evidence.EXACT, basis=Basis.LIST,
                          finality=Finality.FINAL, note=f"{what}: unreconciled")
        return Figure(nano=nano, evidence=Evidence.EXACT, basis=Basis.LIST,
                      finality=Finality.PROVISIONAL, note=f"{what}: provisional (month open)")

    # ----- org seat policy --------------------------------------------------------------------

    def _org_policies(self) -> dict[str, tuple[str, bool]]:
        """Org login → (seat_management_setting, stated): pulled org settings beat the admin
        answers; the latest snapshot wins."""
        best: dict[str, tuple[tuple, str, bool]] = {}
        for c in self.config:
            if c.kind != "org_settings" or not c.entity_id.startswith("org:"):
                continue
            value = dict(c.attrs).get("seat_management_setting")
            if not isinstance(value, str) or not value:
                continue
            stated = c.source_kind in _STATEMENTS
            rank = (not stated, c.snapshot_ms, c.fetched_ms, value)
            cur = best.get(c.entity_id[4:])
            if cur is None or rank > cur[0]:
                best[c.entity_id[4:]] = (rank, value, stated)
        return {org: (v[1], v[2]) for org, v in sorted(best.items())}

    def assignment(self, org: str | None, via_team: bool | None) -> str:
        policy = self.org_policy.get(org, ("", False))[0] if org is not None else ""
        if policy == "assign_all":
            return "auto"
        if via_team is True:
            return "team"
        if via_team is False and policy == "assign_selected":
            return "removable"
        return "unknown"

    # ----- seat view --------------------------------------------------------------------------

    def _seat_view(self) -> _SeatView:
        if self.licenses:
            return self._license_view()
        rows = [c for c in self.config if c.kind == "seat_counts"]
        if rows:
            return self._count_view(rows)
        return _SeatView("none", {}, {}, {}, {}, {}, None)

    def _license_view(self) -> _SeatView:
        latest: dict[str, str] = {}
        for lic in self.licenses:
            key = lic.org or ""
            latest[key] = max(latest.get(key, ""), lic.snapshot_date)
        rows: dict[tuple[str, str], LicenseSnapshot] = {}
        for lic in self.licenses:
            if lic.snapshot_date != latest[lic.org or ""]:
                continue
            key = (lic.principal, lic.org or "")
            cur = rows.get(key)
            if cur is None or _license_rank(lic) > _license_rank(cur):
                rows[key] = lic
        by_person: dict[str, list[LicenseSnapshot]] = defaultdict(list)
        for (principal, _), lic in sorted(rows.items()):
            by_person[principal].append(lic)
        for principal, items in by_person.items():
            if any(x.source_kind == _SEATS_API for x in items):
                by_person[principal] = [x for x in items if x.source_kind == _SEATS_API]
        first_seen: dict[str, _dt.date] = {}
        for lic in self.licenses:
            d = _date(lic.snapshot_date)
            if d is not None and (lic.principal not in first_seen or d < first_seen[lic.principal]):
                first_seen[lic.principal] = d
        costs = self._cost_days()
        have_cost_data = bool(self.cost_lines) or bool(self.activity)
        idle: dict[_GroupKey, _Group] = defaultdict(_Group)
        seats: dict[str, int] = defaultdict(int)
        multi: dict[str, dict[str | None, int]] = defaultdict(lambda: defaultdict(int))
        current: dict[str, LicenseSnapshot] = {}
        snaps: list[_dt.date] = []
        for principal in sorted(by_person):
            items = sorted(by_person[principal], key=lambda x: (x.org or "", x.source_kind))
            rep = items[0]
            current[principal] = rep
            entity = self.entity(rep.cost_center, rep.org)
            seats[entity] += 1
            snap = _date(rep.snapshot_date)
            if snap is not None:
                snaps.append(snap)
            if len({x.org for x in items if x.org is not None}) >= 2:
                multi[entity][rep.team] += 1
            live = [x for x in items if x.pending_cancellation is None]
            if not live or snap is None:
                continue
            if any(x.last_activity_bucket not in IDLE_BUCKETS for x in live):
                continue
            created = [d for d in (_date(x.seat_created) for x in live) if d is not None]
            cutoff = snap - _dt.timedelta(days=_WINDOW_DAYS)
            if created:
                if min(created) > cutoff:
                    continue                      # seat younger than 30 days
                age_unknown = False
            else:
                seen = first_seen.get(principal)
                age_unknown = seen is None or seen > cutoff
            if any(cutoff < d <= snap for d in costs.get(principal, ())):
                continue                          # reported cost in the last 30 days
            plan = ("enterprise" if any(x.plan == "enterprise" for x in live) else
                    "business" if any(x.plan == "business" for x in live) else "unknown")
            kinds = {self.assignment(x.org, x.assigned_via_team) for x in live}
            assignment = next(a for a in ("auto", "team", "unknown", "removable") if a in kinds)
            group = idle[(entity, rep.team, rep.org, plan, assignment)]
            group.n += 1
            group.age_unknown += int(age_unknown)
            group.cost_unknown += int(not have_cost_data)
            bucket = rep.last_activity_bucket
            group.buckets[bucket] = group.buckets.get(bucket, 0) + 1
        return _SeatView("licenses", dict(idle), dict(seats),
                         {e: sorted(t.items(), key=lambda kv: kv[0] or "") for e, t in
                          multi.items()},
                         current, dict(by_person), max(snaps) if snaps else None)

    def _cost_days(self) -> dict[str, list[_dt.date]]:
        """Principal → the days with reported Copilot cost (report rows or metrics estimates)."""
        out: dict[str, list[_dt.date]] = defaultdict(list)
        for line in self.cost_lines:
            if line.principal is None or line.cost_type != _POOLED:
                continue
            gross = line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano
            qty = _dec(line.quantity)
            if gross > 0 or (qty is not None and qty > 0):
                d = _date(line.date_utc)
                if d is not None:
                    out[line.principal].append(d)
        for day in self.activity:
            if day.reported_cost_nano:
                d = _date(day.date_utc)
                if d is not None:
                    out[day.principal].append(d)
        return out

    def _count_view(self, rows: list[ConfigSnapshot]) -> _SeatView:
        latest: dict[str, int] = {}
        for c in rows:
            day = c.snapshot_ms // 86_400_000
            latest[c.entity_id] = max(latest.get(c.entity_id, day), day)
        idle: dict[_GroupKey, _Group] = defaultdict(_Group)
        seats: dict[str, int] = defaultdict(int)
        snaps: list[_dt.date] = []
        for c in rows:
            if c.snapshot_ms // 86_400_000 != latest[c.entity_id]:
                continue
            attrs = dict(c.attrs)
            if attrs.get("bucket") == "*":
                continue
            n = _int_nano(attrs.get("n"))
            if n is None or n <= 0:
                continue
            org = c.entity_id[4:] if c.entity_id.startswith("org:") else None
            entity = self.config_entity(c.entity_id) or "enterprise"
            seats[entity] += n
            snaps.append(_date_of_ms(c.snapshot_ms))
            if attrs.get("bucket") not in IDLE_BUCKETS:
                continue
            if _flag(attrs.get("pending_cancellation")) is True:
                continue
            created = _flag(attrs.get("created_over_30d"))
            zero_cost = _flag(attrs.get("zero_cost_30d"))
            if created is False or zero_cost is False:
                continue
            team = attrs.get("team") if isinstance(attrs.get("team"), str) else None
            plan = attrs.get("plan") if attrs.get("plan") in _PLANS else "unknown"
            assignment = self.assignment(org, _flag(attrs.get("assigned_via_team")))
            group = idle[(entity, team, org, str(plan), assignment)]
            group.n += n
            group.age_unknown += n if created is None else 0
            group.cost_unknown += n if zero_cost is None else 0
            bucket = str(attrs.get("bucket"))
            group.buckets[bucket] = group.buckets.get(bucket, 0) + n
        return _SeatView("seat_counts", dict(idle), dict(seats), {}, {}, {},
                         max(snaps) if snaps else None)

    def entity_seats(self, entity: str) -> int:
        """Seats of an entity: the seat view, else the plan evidence, else the pool month."""
        n = self.seats.seats_by_entity.get(entity, 0)
        pe = self.latest_plan(entity)
        if pe is not None:
            n = max(n, sum(v for _, v in pe.seats))
        for pm in self.latest_pms(entity)[:1]:
            total = sum((_dec(v) or Decimal(0) for _, v in pm.seats), Decimal(0))
            n = max(n, math.ceil(total))
        return n


def _license_order(x: LicenseSnapshot) -> tuple:
    return (x.snapshot_date, x.principal, x.org or "", repr(x))


def _license_rank(x: LicenseSnapshot) -> tuple:
    """Which of two rows of one (principal, org, date) wins: the seats API, the later fetch, a
    known plan, then a total order (so the input order never matters)."""
    return (x.source_kind == _SEATS_API, x.fetched_ms, x.plan != "unknown", repr(x))


# ---------------------------------------------------------------------------------------------
# the detector
# ---------------------------------------------------------------------------------------------


class CopilotSeatsBudgets:
    """``copilot.seats-budgets`` (addendum §10.1). See the module docstring.

    Thresholds: ``min_usd`` (default $1.00) gates the dollar kinds — seat kinds on their seat-fee
    ``cost_observed`` (an idle seat in an overage entity is still shown with invoice saving $0),
    ``overage-forecast`` on its p90, the rest through ``core.findings.min_usd_gate``; status and
    count kinds (``plan-status``, ``pool-regime``, the budget lints, ``completions-only-seat``,
    ``duplicate-seat``) always report.
    """

    id = DETECTOR_ID
    version = VERSION
    kinds = KINDS
    requires: frozenset[str] = frozenset()
    aggregate = True
    extension = "copilot"
    families = frozenset({"copilot"})
    kind_requires = KIND_REQUIRES

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Every kind whose inputs are present (lanes are ignored: an aggregate detector)."""
        del lanes
        run = _Run(ctx)
        out: list[Finding] = []
        out += _plan_status(run)
        if run.runs("pool-regime"):
            out += _pool_regime(run)
        if run.runs("overage-forecast"):
            out += _overage_forecast(run)
        if run.runs("promo-cliff"):
            out += _promo_cliff(run)
        if run.runs("idle-seat"):
            out += _idle_seat(run)
        if run.runs("seat-auto-assign"):
            out += _seat_auto_assign(run)
        if run.runs("completions-only-seat"):
            out += _completions_only(run)
        if run.runs("plan-mix"):
            out += _plan_mix(run)
        if run.runs("duplicate-seat"):
            out += _duplicate_seat(run)
        budgets = _Budgets(run)
        if run.runs("budget-paid-usage-uncapped"):
            out += budgets.paid_usage_uncapped()
        if run.runs("budget-stop-usage-off"):
            out += budgets.stop_usage_off()
        if run.runs("budget-zero-user-budget"):
            out += budgets.zero_user_budget()
        if run.runs("budget-ulb-gap"):
            out += budgets.ulb_gap()
        if run.runs("budget-org-multi-org-seats"):
            out += budgets.org_multi_org_seats()
        if run.runs("budget-no-cost-center-pool"):
            out += budgets.no_cost_center_pool()
        if run.runs("budget-enterprise-misread"):
            out += budgets.enterprise_misread()
        out += _skipped(run)
        order = {k: i for i, k in enumerate(KINDS)}
        return sorted(out, key=lambda f: (order[f.kind], f.finding_id))


# ---------------------------------------------------------------------------------------------
# finding assembly
# ---------------------------------------------------------------------------------------------


def _finding(run: _Run, kind: str, *, scope: dict[str, str | None], title: str, summary: str,
             cost: Figure, recoverable: Figure | None = None, n_users: int = 0,
             n_events: int = 0, evidence: Sequence[EvidenceItem] = (),
             lever_ids: Sequence[str] = (), needs_eval: bool = False,
             confidence: str = "medium", fix: Fix | None = None,
             use_catalog_fix: bool = True) -> Finding:
    category = ("data-quality" if kind.startswith("dq.") else
                "lever" if kind in SEAT_KINDS else "aggregate")
    levers = tuple(sorted(set(lever_ids)))
    lever_class = "none"
    if levers:
        lever_class = catalog.lever(levers[0]).lever_class
    if fix is None and use_catalog_fix:
        fix = catalog.fix_for(DETECTOR_ID, kind, _PRODUCT)
    return build_finding(
        detector_id=DETECTOR_ID, kind=kind, detector_version=VERSION, category=category,
        lever_class=lever_class, audience="org", title=title, summary=summary,
        scope=make_scope(product=_PRODUCT, **scope), n_events=max(0, n_events),
        n_lanes=0, n_users=max(0, n_users), first_seen_ms=run.ctx.window[0],
        cost_observed=cost, recoverable=recoverable, lever_ids=levers,
        evidence=tuple(evidence[:20]), fix=fix, confidence=confidence, needs_eval=needs_eval,
        references=_REFS[kind])


def _count_only(what: str) -> Figure:
    return unpriced(f"count only ({what}); no dollar figure")


# ---------------------------------------------------------------------------------------------
# plan-status
# ---------------------------------------------------------------------------------------------

_HOW_TO = ("Find out: seats API plan_type, detailed-report seat SKU (copilot_for_business / "
           "copilot_enterprise) or Enterprise settings > Licensing; then answers.json or --plan.")


def _seat_lines(run: _Run, entity: str, month: str) -> list:
    return [c for c in run.cost_lines
            if c.cost_type == "seat" and c.date_utc[:7] == month
            and run.entity(c.cost_center, c.workspace_id) == entity]


def _seat_fees(run: _Run, entity: str, pe: PlanEvidence) -> tuple[Figure, list[EvidenceItem]]:
    """Seat fees of an entity-month: seat lines (R16) when present, else count × list (ESTIMATED
    LIST); unknown seats as the range [n × Business; n × Enterprise] without a point."""
    seats = dict(pe.seats)
    lines = _seat_lines(run, entity, pe.month)
    pms = run.pools.get(entity, {}).get(pe.month, [])
    evidence = []
    if lines:
        net = sum(c.amount_nano for c in lines)
        final = all(c.finality == "final" for c in lines)
        pm = pms[0] if pms else None
        if pm is not None and pm.plan_scenario is None and final:
            fig = run.r16(net, pm, "seat lines")
        else:
            closed = final and pm is not None and pm.finality == "closed"
            fig = Figure(nano=net, evidence=Evidence.EXACT, basis=Basis.LIST,
                         finality=Finality.FINAL if closed else Finality.PROVISIONAL,
                         note="seat lines: unreconciled" if closed else "seat lines: provisional")
        evidence.append(_ev("fees:seat_lines", n=len(lines), nano=net,
                            label=f"{fig.evidence.value} {fig.basis.value}"))
        return fig, evidence
    known = sum((_seat_price(p) * n for p, n in seats.items() if p in _PLANS), Decimal(0))
    known_nano = _usd_nano(known)
    n_unknown = seats.get("unknown", 0)
    note = ("count x list price per month; proration, upfront charges and volume/EA pricing not "
            "modeled")
    if n_unknown:
        low = known_nano + _usd_nano(_seat_price("business") * n_unknown)
        high = known_nano + _usd_nano(_seat_price("enterprise") * n_unknown)
        for scen, value in (("business", low), ("enterprise", high)):
            evidence.append(_ev(f"fees:scenario:{scen}", nano=value, label="estimated list"))
        fig = Figure(nano=None, evidence=Evidence.ESTIMATED, basis=Basis.LIST, low_nano=low,
                     high_nano=high, calibration=Calibration.UNCALIBRATED,
                     note=(f"unpriced: plan unknown, seat fees by scenario {fmt_usd(low)} if "
                           f"Business / {fmt_usd(high)} if Enterprise ({note}); no scenario is "
                           f"chosen"))
        return fig, evidence
    fig = estimated(known_nano, Basis.LIST, note=note)
    evidence.append(_ev("fees:count_x_list", nano=known_nano, label="estimated list"))
    return fig, evidence


def _plan_status(run: _Run) -> list[Finding]:
    out: list[Finding] = []
    for entity in sorted(run.plans):
        months = run.plans[entity]
        pe = months[max(months)]
        seats = dict(pe.seats)
        n_seats = sum(seats.values())
        fees, evidence = _seat_fees(run, entity, pe)
        evidence.insert(0, _ev("plan", plan=pe.plan, source=pe.source, conflict=pe.conflict,
                               month=pe.month))
        for plan in ("business", "enterprise", "unknown"):
            if seats.get(plan):
                evidence.append(_ev(f"seats:{plan}", n=seats[plan]))
        for line in pe.evidence[:10]:
            evidence.append(_ev("evidence", line=_cut(line, 200)))
        for month in sorted(months, reverse=True)[1:6]:
            older = months[month]
            evidence.append(_ev(f"month:{month}", plan=older.plan, source=older.source,
                                conflict=older.conflict))
        unknown = pe.plan == "unknown" or seats.get("unknown", 0) > 0
        split = ", ".join(f"{p} {n}" for p, n in sorted(seats.items()) if n)
        if unknown:
            title = f"Copilot plan of {entity} unknown: both scenarios shown ({pe.month})"
            lead = (f"Plan of {entity} in {pe.month}: unknown (seats: {split}; source "
                    f"{pe.source}). Pool figures are shown per scenario (all Business / all "
                    f"Enterprise), estimated; none is chosen.")
        elif pe.conflict:
            title = (f"Copilot plan of {entity}: {pe.plan} from {pe.source} (conflicting "
                     f"evidence, {pe.month})")
            lead = (f"Plan of {entity} in {pe.month}: {pe.plan}, decided by {pe.source} (data "
                    f"beats a statement); another source disagrees ({cpool.PLAN_CONFLICT_DQ}).")
        else:
            title = f"Copilot plan of {entity}: {pe.plan} from {pe.source} ({pe.month})"
            lead = (f"Plan of {entity} in {pe.month}: {pe.plan} ({n_seats} seats), detected "
                    f"from {pe.source}.")
        fee_text = (f"Seat fees {_money(fees.low_nano)} if Business to {_money(fees.high_nano)} "
                    f"if Enterprise (estimated list)." if fees.nano is None else
                    f"Seat fees {_money(fees.nano)} per month ({fees.evidence.value} "
                    f"{fees.basis.value}).")
        tail = _HOW_TO if unknown or pe.conflict else "No plan recommendation is made."
        confidence = ("high" if pe.source in ("seat_lines", "seats_api") and not pe.conflict
                      else "low" if unknown else "medium")
        out.append(_finding(
            run, "plan-status", scope={"entity": entity}, title=title,
            summary=_summary(lead, tail, fee_text), cost=fees, n_users=n_seats,
            n_events=n_seats, evidence=evidence, confidence=confidence))
    return out


# ---------------------------------------------------------------------------------------------
# pool kinds
# ---------------------------------------------------------------------------------------------


def _overage_figure(run: _Run, pm: PoolMonth) -> Figure:
    """The observed overage of a pool month: R16 labels for a known plan; ESTIMATED in a scenario
    (consumption over the scenario pool; an upper bound when the pool is a lower bound)."""
    if pm.plan_scenario is None:
        return run.r16(pm.overage_observed_nano, pm, "overage (net of pooled rows)")
    upper = pm.seats_source == "report_users"
    return estimated(pm.overage_observed_nano, Basis.LIST, upper_bound=upper,
                     note=(f"overage = consumption - scenario pool at $0.01 per credit; "
                           f"scenario {pm.plan_scenario} (plan unknown)"
                           + ("; seats lower bound: an upper bound" if upper else "")))


def _pool_regime(run: _Run) -> list[Finding]:
    out: list[Finding] = []
    for entity in sorted(run.pools):
        months = run.pools[entity]
        latest = max(months)
        for pm in months[latest]:
            scen = pm.plan_scenario
            overage = _overage_figure(run, pm)
            evidence = [
                _ev("pool", month=pm.month, credits=pm.pool_credits, nano=pm.pool_nano,
                    promo=pm.promo, seats_source=pm.seats_source, label="list_equivalent"),
                _ev("consumed", nano=pm.consumed_report_nano, credits=_credits(
                    pm.consumed_report_nano), label="exact list_equivalent",
                    estimate_nano=pm.consumed_estimate_nano or None),
                _ev("overage", **_fig_attrs(overage)),
                _ev("direct", nano=pm.direct_net_nano, draws_pool=pm.direct_draws_pool,
                    label=_label_text(run.r16(pm.direct_net_nano, pm, "direct-org net"))),
                _ev("regime", regime=pm.regime, billing_mode=pm.billing_mode,
                    finality=pm.finality, days_final=pm.days_final,
                    days_provisional=pm.days_provisional, capped_policy=pm.capped_policy,
                    plan_source=pm.plan_source, plan_conflict=pm.plan_conflict),
            ]
            for plan, n in pm.seats:
                evidence.append(_ev(f"seats:{plan}", n=n))
            if pm.forecast is not None:
                evidence.append(_ev("forecast", **_fig_attrs(pm.forecast)))
            if pm.overage_forecast is not None:
                evidence.append(_ev("overage_forecast", **_fig_attrs(pm.overage_forecast)))
            for month in sorted(months, reverse=True)[1:6]:
                older = next((x for x in months[month] if x.plan_scenario == scen),
                             months[month][0])
                evidence.append(_ev(f"month:{month}", regime=older.regime,
                                    finality=older.finality, overage_nano=(
                                        older.overage_observed_nano),
                                    pool_nano=older.pool_nano,
                                    consumed_nano=older.consumed_report_nano))
            slack = max(0, pm.pool_nano - pm.consumed_report_nano)
            lead = (f"{entity} {pm.month}: regime {pm.regime}; pool {pm.pool_credits} credits, "
                    f"consumed {_credits(pm.consumed_report_nano)} credits (list-equivalent), "
                    f"overage {_money(overage.nano)} ({overage.evidence.value} "
                    f"{overage.basis.value}).")
            if pm.regime == "slack":
                act = (f"Slack {_credits(slack)} credits: seat removals cut the invoice, credit "
                       f"savings only free list-equivalent pool headroom.")
            elif pm.regime == "overage":
                act = ("In overage: credit savings cut the invoice at $0.01 per credit; seat "
                       "removals save about nothing.")
            elif pm.regime == "straddling":
                act = "Straddling the pool: savings convert partly, see the forecast range."
            else:
                act = "Regime unknown (no observed day or no seats)."
            fc = ""
            if pm.finality == "open" and pm.overage_forecast is not None:
                of = pm.overage_forecast
                fc = (f"Forecast overage {_money(of.nano)} ({_money(of.low_nano)} to "
                      f"{_money(of.high_nano)}), estimated.")
            out.append(_finding(
                run, "pool-regime", scope={"entity": entity, "plan_scenario": scen},
                title=_title(scen, f"Copilot pool of {entity} in {pm.month}: {pm.regime}"),
                summary=_summary(_scenario_sentence(scen), lead, act, fc),
                cost=overage, n_users=run.entity_seats(entity), evidence=evidence,
                confidence="high" if scen is None and pm.finality == "closed" else "medium"))
    return out


def _label_text(fig: Figure) -> str:
    return f"{fig.evidence.value} {fig.basis.value}"


def _overage_forecast(run: _Run) -> list[Finding]:
    out: list[Finding] = []
    for entity in sorted(run.pools):
        months = run.pools[entity]
        open_months = [m for m in sorted(months) if any(pm.finality == "open"
                                                        for pm in months[m])]
        if not open_months:
            continue
        for pm in months[open_months[-1]]:
            fig = pm.overage_forecast
            if pm.finality != "open" or fig is None or fig.nano is None:
                continue
            high = fig.high_nano if fig.high_nano is not None else fig.nano
            if high <= 0 or high < run.min_usd:
                continue
            scen = pm.plan_scenario
            cost = _with_note(fig, f"scenario {scen} (plan unknown)" if scen else "")
            policy = (f"; cap policy {pm.capped_policy}" if pm.capped_policy is not None else "")
            evidence = [_ev("overage_forecast", **_fig_attrs(cost)),
                        _ev("regime", regime=pm.regime, month=pm.month,
                            billing_mode=pm.billing_mode, capped_policy=pm.capped_policy,
                            pool_nano=pm.pool_nano, observed_nano=pm.consumed_report_nano)]
            if pm.forecast is not None:
                evidence.append(_ev("forecast", **_fig_attrs(pm.forecast)))
            cc_note = ("" if pm.billing_mode == "metered" else
                       " Cost-center pools apply only to metered billing.")
            out.append(_finding(
                run, "overage-forecast", scope={"entity": entity, "plan_scenario": scen},
                title=_title(scen, f"Copilot overage forecast for {entity} in {pm.month}: "
                                   f"{_money(cost.nano)}"),
                summary=_summary(
                    _scenario_sentence(scen),
                    f"Month-end overage forecast {_money(cost.nano)} ({_money(cost.low_nano)} to "
                    f"{_money(high)}, p10 to p90), estimated at unchanged use{policy}.",
                    f"Metered at $0.01 per credit after the pool.{cc_note}"),
                cost=cost, n_users=run.entity_seats(entity), evidence=evidence,
                lever_ids=[lv.lever_id for lv in catalog.levers_for_kind(
                    "overage-forecast", family=_PRODUCT)]))
    return out


def _promo_cliff(run: _Run) -> list[Finding]:
    if run.today is not None and run.today > _dt.date.fromisoformat(PROMO_CLIFF_EXPIRES):
        return []
    out: list[Finding] = []
    for entity in sorted(run.pools):
        months = run.pools[entity]
        promo = [m for m in sorted(months) if any(pm.promo for pm in months[m])]
        if not promo:
            continue
        later = [m for m in sorted(months) if m > promo[-1]
                 and all(pm.promo is None for pm in months[m])]
        if not later:
            continue
        standard = later[-1]
        for spm in months[standard]:
            scen = spm.plan_scenario
            ppm = next((x for x in months[promo[-1]] if x.plan_scenario == scen), None)
            if ppm is None:
                ppm = next((x for x in months[promo[-1]] if x.plan_scenario is None), None)
            if ppm is None:
                continue
            use = ppm.consumed_report_nano
            before = max(0, use - ppm.pool_nano)
            after = max(0, use - spm.pool_nano)
            delta = after - before
            if delta < run.min_usd or delta <= 0:
                continue
            cost = estimated(delta, Basis.LIST, note=(
                f"at unchanged use: {promo[-1]} consumption against the {standard} standard "
                f"pool; expires {PROMO_CLIFF_EXPIRES}"
                + (f"; scenario {scen} (plan unknown)" if scen else "")))
            evidence = [
                _ev(f"promo:{promo[-1]}", pool_nano=ppm.pool_nano, consumed_nano=use,
                    overage_nano=before, promo=ppm.promo),
                _ev(f"standard:{standard}", pool_nano=spm.pool_nano, overage_nano=after,
                    regime=spm.regime),
                _ev("expires", date=PROMO_CLIFF_EXPIRES),
            ]
            out.append(_finding(
                run, "promo-cliff", scope={"entity": entity, "plan_scenario": scen},
                title=_title(scen, f"Copilot promo cliff for {entity}: {_money(delta)}/month "
                                   f"at unchanged use"),
                summary=_summary(
                    _scenario_sentence(scen),
                    f"The promotional pool ended on {facts.copilot_dates().get('promo_end', '')}: "
                    f"{promo[-1]} use {_credits(use)} credits against the {standard} pool "
                    f"{spm.pool_credits} credits costs {_money(delta)} more per month at unchanged "
                    f"use (estimated).",
                    f"Dated finding; expires {PROMO_CLIFF_EXPIRES}."),
                cost=cost, n_users=run.entity_seats(entity), evidence=evidence))
    return out


# ---------------------------------------------------------------------------------------------
# seat kinds
# ---------------------------------------------------------------------------------------------

_UNKNOWN_ASSIGNMENT_NOTE = ("assumes direct assignment in an assign_selected org; confirm with the "
                            "seats API assigning_team")
_SEAT_FEE_NOTE = ("count x list price per month; proration, upfront charges from {upfront} and "
                  "volume/EA pricing not modeled")


def _fee_note() -> str:
    return _SEAT_FEE_NOTE.format(upfront=facts.copilot_dates().get("upfront_seat_charges",
                                                                   "2026-10-01"))


def _fees(counts: Mapping[str, int], unknown_as: str | None) -> Figure:
    """Seat fees of specific seats (count × list, ESTIMATED LIST); unknown-plan seats priced at
    *unknown_as*, or as a range when None."""
    known = sum((_seat_price(p) * n for p, n in counts.items() if p in _PLANS), Decimal(0))
    n_unknown = counts.get("unknown", 0)
    if n_unknown and unknown_as is None:
        low = _usd_nano(known + _seat_price("business") * n_unknown)
        high = _usd_nano(known + _seat_price("enterprise") * n_unknown)
        return Figure(nano=None, evidence=Evidence.ESTIMATED, basis=Basis.LIST, low_nano=low,
                      high_nano=high, calibration=Calibration.UNCALIBRATED,
                      note=f"unpriced: seat plan unknown; {_fee_note()}")
    if n_unknown:
        known += _seat_price(unknown_as) * n_unknown  # type: ignore[arg-type]
    return estimated(_usd_nano(known), Basis.LIST, note=_fee_note())


def _fee_gate(run: _Run, fees: Figure) -> bool:
    point = fees.nano if fees.nano is not None else fees.low_nano
    return point is not None and point >= run.min_usd


def _project(run: _Run, entity: str, scenario: str | None, counts: Mapping[str, int],
             *, upgrade: bool = False) -> Figure | None:
    """``core.pool.realize_seat_change`` for removing *counts* seats (or, with *upgrade*,
    downgrading them from Enterprise to Business) in the entity's reference pool month of
    *scenario*; None for volume / azure entities; unpriced without a pool month."""
    pm = run.pm_for(entity, scenario)
    if pm is None:
        return unpriced("no pool month for this entity: projection unavailable")
    delta: dict[str, int] = {}
    for plan, n in counts.items():
        if n <= 0:
            continue
        key = plan
        if plan == "unknown" and pm.plan_scenario is None:
            resolved = run.unknown_plan_as(entity, None)
            if resolved is None:
                return unpriced("seat plan unknown in a mixed entity")
            key = resolved
        delta[key] = delta.get(key, 0) - n
    if upgrade:
        n = -delta.pop("enterprise", 0)
        delta = {"enterprise": -n, "business": n}
    if not delta:
        return None
    return cpool.realize_seat_change(pm, delta, month_fee={})


def _deadline(run: _Run, entity: str) -> tuple[str, str | None]:
    mode = run.billing_mode(entity)
    if mode in ("volume", "azure"):
        renewal = run.renewal(entity)
        text = (f"billing mode {mode}: savings only at renewal"
                + (f" ({renewal})" if renewal else " (renewal date unknown)"))
        return text, renewal
    if run.today is None:
        return "before the next 1st 00:00 UTC (removals are billed to cycle end)", None
    date = _next_first(run.today)
    prefix = "" if mode == "metered" else f"billing mode {mode}; if metered: "
    return f"{prefix}before {date} (removals are billed to cycle end)", date


def _plan_counts(groups: Iterable[tuple[_GroupKey, _Group]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for key, group in groups:
        out[key[3]] += group.n
    return dict(out)


def _idle_seat(run: _Run) -> list[Finding]:
    by_team: dict[tuple[str, str | None], list[tuple[_GroupKey, _Group]]] = defaultdict(list)
    for key, group in sorted(run.seats.idle.items(), key=lambda kv: tuple(
            (v is None, v or "") for v in kv[0])):
        by_team[(key[0], key[1])].append((key, group))
    out: list[Finding] = []
    for (entity, team), groups in sorted(by_team.items(), key=lambda kv: (kv[0][0],
                                                                          kv[0][1] or "")):
        split = {a: [(k, g) for k, g in groups if k[4] == a] for a in _ASSIGNMENTS}
        count = {a: sum(g.n for _, g in split[a]) for a in _ASSIGNMENTS}
        total = sum(count.values())
        age_unknown = sum(g.age_unknown for _, g in groups)
        cost_unknown = sum(g.cost_unknown for _, g in groups)
        mode = run.billing_mode(entity)
        deadline, _ = _deadline(run, entity)
        for scen in run.scenarios(entity):
            unknown_as = run.unknown_plan_as(entity, scen)
            fees = _fees(_plan_counts(groups), unknown_as)
            if not _fee_gate(run, fees):
                continue
            removable = _plan_counts(split["removable"])
            project = {p: removable.get(p, 0) + _plan_counts(split["unknown"]).get(p, 0)
                       for p in set(removable) | set(_plan_counts(split["unknown"]))}
            rec_removable = _project(run, entity, scen, removable) if count["removable"] else None
            rec = rec_removable
            if count["unknown"]:
                rec = _project(run, entity, scen, project)
                if rec is not None and rec.nano is not None:
                    rec = dataclasses.replace(
                        _with_note(rec, _UNKNOWN_ASSIGNMENT_NOTE), upper_bound=True)
            if mode in ("volume", "azure"):
                rec = None
            evidence = [
                _ev("assignment:removable", n=count["removable"],
                    **(_fig_attrs(rec_removable) if count["removable"] and mode not in (
                        "volume", "azure") else {"projection": "none"})),
                _ev("assignment:team", n=count["team"], projection="none",
                    lever="copilot.seat_reclaim_team"),
                _ev("assignment:auto", n=count["auto"], see="seat-auto-assign"),
                _ev("assignment:unknown", n=count["unknown"],
                    **(_fig_attrs(rec) if count["unknown"] and rec is not None else
                       {"projection": "none"})),
                _ev("fees", n=total, **_fig_attrs(fees)),
                _ev("deadline", text=deadline, billing_mode=mode),
            ]
            buckets: dict[str, int] = defaultdict(int)
            orgs: dict[str | None, dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for key, group in groups:
                for b, n in group.buckets.items():
                    buckets[b] += n
                orgs[key[2]][key[4]] += group.n
            for b in IDLE_BUCKETS:
                if buckets.get(b):
                    evidence.append(_ev(f"bucket:{b}", n=buckets[b]))
            for org, split_counts in sorted(orgs.items(), key=lambda kv: kv[0] or ""):
                evidence.append(_ev(f"org:{org}" if org else "org:(unknown)",
                                    **{a: split_counts.get(a, 0) for a in _ASSIGNMENTS}))
            pm = run.pm_for(entity, scen)
            if pm is not None:
                evidence.append(_ev("regime", regime=pm.regime, month=pm.month))
            if age_unknown or cost_unknown:
                evidence.append(_ev("caveats", age_unknown=age_unknown,
                                    zero_cost_unverified=cost_unknown))
            notes: list[str] = []
            if count["unknown"]:
                notes.append("Assignment not in the source: the seats API or org seat policy "
                             "settles it.")
            if age_unknown:
                notes.append(f"Seat age unknown: {age_unknown}.")
            if cost_unknown:
                notes.append(f"Zero cost unverified (no report): {cost_unknown}.")
            regime = f" in regime {pm.regime}" if pm is not None else ""
            rec_text = (f"saving {_money(rec.nano)}/month{regime} "
                        f"({_label_text(rec)}{', upper bound' if rec.upper_bound else ''})"
                        if rec is not None else
                        "no projection" + (" (savings only at renewal)"
                                           if mode in ("volume", "azure") else ""))
            lever_ids = []
            if count["removable"] or count["unknown"]:
                lever_ids.append("copilot.seat_reclaim")
            if count["team"]:
                lever_ids.append("copilot.seat_reclaim_team")
            where = f"team {team}" if team else "no team"
            out.append(_finding(
                run, "idle-seat",
                scope={"entity": entity, "team": team, "plan_scenario": scen},
                title=_title(scen, f"Idle Copilot seats in {where} ({entity}): "
                                   f"{count['removable']} removable of {total}"),
                summary=_summary(
                    _scenario_sentence(scen),
                    f"{total} idle seats (30+ days, zero cost): {count['removable']} "
                    f"removable, {count['team']} team-assigned, {count['auto']} auto, "
                    f"{count['unknown']} unknown assignment.",
                    f"Fees {_money(fees.nano if fees.nano is not None else fees.low_nano)}"
                    f"/month (estimated list); {rec_text}.",
                    *notes, f"Deadline: {deadline}."),
                cost=fees, recoverable=rec, n_users=total, n_events=total, evidence=evidence,
                lever_ids=lever_ids,
                needs_eval=bool(count["unknown"] or age_unknown or cost_unknown),
                confidence="high" if not (count["unknown"] or age_unknown or cost_unknown)
                else "medium"))
    return out


def _seat_auto_assign(run: _Run) -> list[Finding]:
    by_org: dict[tuple[str, str], list[tuple[_GroupKey, _Group]]] = defaultdict(list)
    for key, group in run.seats.idle.items():
        if key[4] == "auto" and key[2] is not None:
            by_org[(key[0], key[2])].append((key, group))
    out: list[Finding] = []
    for (entity, org), groups in sorted(by_org.items()):
        total = sum(g.n for _, g in groups)
        counts = _plan_counts(groups)
        mode = run.billing_mode(entity)
        stated = run.org_policy.get(org, ("", False))[1]
        for scen in run.scenarios(entity):
            fees = _fees(counts, run.unknown_plan_as(entity, scen))
            if not _fee_gate(run, fees):
                continue
            rec = None if mode in ("volume", "azure") else _project(run, entity, scen, counts)
            if rec is not None and rec.nano is not None:
                rec = _with_note(rec, "trade-off: every other member of the org loses automatic "
                                      "access; the outcome of the switch for existing seats is "
                                      "unverified")
            deadline, _ = _deadline(run, entity)
            evidence = [_ev("idle", n=total, **_fig_attrs(fees)),
                        _ev("projection", **_fig_attrs(rec)),
                        _ev("seat_policy", setting="assign_all",
                            source="admin statement" if stated else "org settings"),
                        _ev("deadline", text=deadline, billing_mode=mode)]
            out.append(_finding(
                run, "seat-auto-assign",
                scope={"entity": entity, "org": org, "plan_scenario": scen},
                title=_title(scen, f"Org {org} assigns Copilot to all members: {total} idle "
                                   f"seats"),
                summary=_summary(
                    _scenario_sentence(scen),
                    f"Org {org} ({entity}) uses seat policy assign_all"
                    f"{' (admin statement)' if stated else ''}: {total} idle seats cost "
                    f"{_money(fees.nano if fees.nano is not None else fees.low_nano)}/month "
                    f"(estimated list); switching to selected members and unassigning them "
                    f"projects {_money(rec.nano) if rec is not None else 'no saving'}"
                    f"/month (estimated, needs evaluation, trade-off).",
                    f"Deadline: {deadline}."),
                cost=fees, recoverable=rec, n_users=total, n_events=total, evidence=evidence,
                lever_ids=["copilot.seat_policy_selected"], needs_eval=True))
    return out


def _completions_only(run: _Run) -> list[Finding]:
    days = [a for a in run.activity if _date(a.date_utc) is not None]
    if not days:
        return []
    ref = max(_date(a.date_utc) for a in days)  # type: ignore[type-var]
    start = ref - _dt.timedelta(days=_WINDOW_DAYS)  # type: ignore[operator]
    per: dict[str, list] = defaultdict(list)
    for a in days:
        if start < _date(a.date_utc) <= ref:  # type: ignore[operator]
            per[a.principal].append(a)
    costs = run._cost_days()
    groups: dict[tuple[str, str | None], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for principal in sorted(per):
        lic = run.seats.current.get(principal)
        if lic is None:
            continue
        items = per[principal]
        generation = sum(dict(a.counts).get("code_generation", 0) for a in items)
        flags = {f for a in items for f in a.flags}
        if generation <= 0 or flags & _NO_CHAT_FLAGS:
            continue
        if any(start < d <= ref for d in costs.get(principal, ())):  # type: ignore[operator]
            continue
        plans = {x.plan for x in run.seats.all_rows.get(principal, [lic])}
        plan = "enterprise" if "enterprise" in plans else "business" if "business" in plans \
            else "unknown"
        groups[(run.entity(lic.cost_center, lic.org), lic.team)][plan] += 1
    out: list[Finding] = []
    for (entity, team), counts in sorted(groups.items(), key=lambda kv: (kv[0][0],
                                                                         kv[0][1] or "")):
        total = sum(counts.values())
        unknown = run.plan_unknown(entity)
        if unknown:
            fix = Fix(text=("Completions are free and these seats still feed the pool; no plan "
                            "advice while the plan is unknown (confirm it first: seats API "
                            "plan_type, detailed-report seat SKU or Licensing)."),
                      config_patch=None, target="github-copilot",
                      doc_url=catalog.ADMIN_ACTIONS["admin:plan_confirm"].doc_url)
            advice = "No plan advice while the plan is unknown."
        elif counts.get("enterprise"):
            fix = None
            advice = ("Where applicable, a Business seat instead of Enterprise (the seat still "
                      "feeds the pool).")
        else:
            fix = Fix(text=("Completions are free and these seats still feed the pool; review "
                            "whether the users need a seat."),
                      config_patch=None, target="github-copilot",
                      doc_url=catalog.ADMIN_ACTIONS["admin:plan_confirm"].doc_url)
            advice = "The seats are Business seats: no plan change applies."
        evidence = [_ev(f"plan:{p}", n=n) for p, n in sorted(counts.items())]
        evidence.append(_ev("window", start=start.isoformat(), end=ref.isoformat()))  # type: ignore[union-attr]
        where = f"team {team}" if team else "no team"
        out.append(_finding(
            run, "completions-only-seat", scope={"entity": entity, "team": team},
            title=f"Completions-only Copilot seats in {where} ({entity}): {total}",
            summary=_summary(
                f"{total} seated users used only code completions in the last 30 days (no chat, "
                f"agent, CLI, app or cloud agent; zero reported cost): completions are free, so "
                f"their seats feed the pool at zero credit use.", advice,
                "Count only; no dollar figure."),
            cost=_count_only("completions-only seats"), n_users=total, n_events=total,
            evidence=evidence, fix=fix, confidence="medium"))
    return out


def _plan_mix(run: _Run) -> list[Finding]:
    limit = Decimal(facts.copilot_plans()["business"].included_credits)
    report_months: set[str] = set()
    use: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for line in run.cost_lines:
        if line.cost_type != _POOLED:
            continue
        report_months.add(line.date_utc[:7])
        if line.principal is None:
            continue
        qty = _dec(line.quantity)
        if qty is None:
            gross = (line.list_amount_nano if line.list_amount_nano is not None
                     else line.amount_nano)
            qty = EXACT_CTX.divide(Decimal(gross), Decimal(_CREDIT_NANO))
        use[(line.principal, line.date_utc[:7])] = EXACT_CTX.add(
            use[(line.principal, line.date_utc[:7])], qty)
    estimates: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for a in run.activity:
        if a.reported_cost_nano:
            key = (a.principal, a.date_utc[:7])
            estimates[key] = EXACT_CTX.add(
                estimates[key], EXACT_CTX.divide(Decimal(a.reported_cost_nano),
                                                 Decimal(_CREDIT_NANO)))
    groups: dict[tuple[str, str | None], list[str]] = defaultdict(list)
    windows: dict[str, tuple[str, ...]] = {}
    for principal, lic in sorted(run.seats.current.items()):
        rows = run.seats.all_rows.get(principal, [lic])
        if not any(x.plan == "enterprise" for x in rows):
            continue
        if all(x.last_activity_bucket in IDLE_BUCKETS for x in rows):
            continue                          # an idle seat is for idle-seat (removal)
        entity = run.entity(lic.cost_center, lic.org)
        if run.plan_unknown(entity):
            continue
        closed = sorted(m for m, pms in run.pools.get(entity, {}).items()
                        if all(pm.finality == "closed" for pm in pms))[-_PLAN_MIX_MONTHS:]
        if len(closed) < _PLAN_MIX_MIN_MONTHS:
            continue
        windows[entity] = tuple(closed)
        fits = True
        for month in closed:
            credits = (use.get((principal, month), Decimal(0)) if month in report_months
                       else estimates.get((principal, month), Decimal(0)))
            if credits > limit:
                fits = False
                break
        if fits:
            groups[(entity, lic.team)].append(principal)
    out: list[Finding] = []
    diff = _seat_price("enterprise") - _seat_price("business")
    for (entity, team), members in sorted(groups.items(), key=lambda kv: (kv[0][0],
                                                                          kv[0][1] or "")):
        n = len(members)
        cost = estimated(_usd_nano(diff * n), Basis.LIST, note=(
            f"({fmt_usd(_usd_nano(_seat_price('enterprise')))} - "
            f"{fmt_usd(_usd_nano(_seat_price('business')))}) x {n} seats per month at list "
            f"prices"))
        if not _fee_gate(run, cost):
            continue
        rec = _project(run, entity, None, {"enterprise": n}, upgrade=True)
        if rec is not None and rec.nano is not None:
            rec = _with_note(rec, "trade-off: Enterprise-only features are lost")
        months = windows[entity]
        short = len(months) < _PLAN_MIX_MONTHS
        estimate_used = any(m not in report_months for m in months)
        where = f"team {team}" if team else "no team"
        evidence = [_ev("seats", n=n, plan="enterprise"),
                    _ev("months", closed=",".join(months),
                        source="metrics estimates" if estimate_used else "report rows"),
                    _ev("projection", **_fig_attrs(rec))]
        out.append(_finding(
            run, "plan-mix", scope={"entity": entity, "team": team, "plan": "enterprise"},
            title=f"Enterprise seats within the Business allowance in {where} ({entity}): {n}",
            summary=_summary(
                f"{n} Enterprise seats drew at most {limit} credits in each of the last "
                f"{len(months)} closed months ({', '.join(months)}"
                f"{'; metrics estimates, no report' if estimate_used else ''}"
                f"{'; fewer than 3 closed months available' if short else ''}).",
                f"Downgrade difference {_money(cost.nano)}/month (estimated list); projected "
                f"invoice saving {_money(rec.nano) if rec is not None else 'none'}/month "
                f"(estimated, needs evaluation, trade-off: Enterprise-only features)."),
            cost=cost, recoverable=rec, n_users=n, n_events=n, evidence=evidence,
            lever_ids=["copilot.seat_downgrade"], needs_eval=True,
            confidence="low" if short or estimate_used else "medium"))
    return out


def _duplicate_seat(run: _Run) -> list[Finding]:
    out: list[Finding] = []
    for entity, teams in sorted(run.seats.multi_org.items()):
        for team, n in teams:
            where = f"team {team}" if team else "no team"
            out.append(_finding(
                run, "duplicate-seat", scope={"entity": entity, "team": team},
                title=f"Copilot seats assigned via several orgs in {where} ({entity}): {n}",
                summary=_summary(
                    f"{n} users hold Copilot seats through two or more organizations; GitHub "
                    f"bills such a seat once, via an organization chosen at random each cycle, "
                    f"so org budgets and chargeback are unpredictable.",
                    "Count only; no dollar figure."),
                cost=_count_only("multi-org seats"), n_users=n, n_events=n,
                evidence=[_ev("multi_org", n=n)], confidence="high"))
    return out


# ---------------------------------------------------------------------------------------------
# budget kinds
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Budget:
    budget_id: str
    scope: str
    amount_nano: int | None
    stop: bool | None
    entity: str | None
    target: str | None
    team: str | None
    cost_center: str | None
    copilot: bool


class _Budgets:
    """Budget lints over ``ConfigSnapshot(kind="budget")`` rows (latest snapshot per budget)."""

    def __init__(self, run: _Run) -> None:
        self.run = run
        latest: dict[str, ConfigSnapshot] = {}
        for c in run.config:
            if c.kind != "budget":
                continue
            cur = latest.get(c.entity_id)
            if cur is None or (c.snapshot_ms, c.fetched_ms) >= (cur.snapshot_ms, cur.fetched_ms):
                latest[c.entity_id] = c
        self.items = [self._parse(bid, c) for bid, c in sorted(latest.items())]
        users: dict[str, int] = {}
        for c in run.config:
            if c.kind == "budget_users":
                n = _int_nano(dict(c.attrs).get("n_users"))
                if n is not None:
                    users[c.entity_id] = n
        self.budget_users = users
        self.cc_users: dict[str, int] = {}
        for c in run.config:
            if c.kind == "cost_center" and c.entity_id.startswith("cc:"):
                n = _int_nano(dict(c.attrs).get("n_users"))
                if n is not None:
                    self.cc_users[c.entity_id] = n

    def _parse(self, bid: str, c: ConfigSnapshot) -> _Budget:
        attrs = dict(c.attrs)
        scope = attrs.get("scope") if isinstance(attrs.get("scope"), str) else ""
        sku = attrs.get("sku")
        text = sku.lower() if isinstance(sku, str) else ""
        copilot = (not text) or "ai_credit" in text or "copilot" in text
        target = attrs.get("target") if isinstance(attrs.get("target"), str) else None
        if target is None and scope in ("enterprise", "user", "multi_user_customer"):
            target = "enterprise"
        entity = self.run.config_entity(target)
        return _Budget(budget_id=bid, scope=str(scope), amount_nano=_int_nano(
            attrs.get("amount_nano")), stop=_flag(attrs.get("prevent_further_usage")),
            entity=entity, target=target,
            team=attrs.get("team") if isinstance(attrs.get("team"), str) else None,
            cost_center=(attrs.get("cost_center") if isinstance(attrs.get("cost_center"), str)
                         else None),
            copilot=copilot)

    def _covers(self, b: _Budget, entity: str) -> bool:
        return b.entity == entity or b.target == "enterprise"

    def metered(self, entity: str | None = None) -> list[_Budget]:
        return [b for b in self.items if b.copilot and b.scope in _METERED_SCOPES
                and (entity is None or self._covers(b, entity))]

    def user_level(self, entity: str | None = None) -> list[_Budget]:
        return [b for b in self.items if b.copilot and b.scope in _USER_SCOPES
                and (entity is None or self._covers(b, entity))]

    # ----- kinds ------------------------------------------------------------------------------

    def paid_usage_uncapped(self) -> list[Finding]:
        run = self.run
        policy = run.flags.get("paid_usage_policy")
        if _flag(policy) is False:
            return []
        out: list[Finding] = []
        for entity in sorted(run.pools):
            stopping = [b for b in self.metered(entity) if b.stop is True]
            stated = _flag(run.flags.get(f"budget_stop.{entity}"))
            if stopping or stated is True:
                continue
            months = run.pools[entity]
            latest = max(months)
            for pm in months[latest]:
                fig = pm.overage_forecast
                if pm.finality != "open" or fig is None or fig.nano is None:
                    continue
                p90 = fig.high_nano if fig.high_nano is not None else fig.nano
                if p90 <= 0:
                    continue
                scen = pm.plan_scenario
                cost = estimated(p90, Basis.LIST, upper_bound=fig.upper_bound, note=(
                    "exposure: forecast overage p90 with no metered budget that stops usage"
                    + (f"; scenario {scen} (plan unknown)" if scen else "")))
                metered = self.metered(entity)
                _keep(run, out, _finding(
                    run, "budget-paid-usage-uncapped",
                    scope={"entity": entity, "plan_scenario": scen},
                    title=_title(scen, f"Copilot paid usage uncapped for {entity}: exposure "
                                       f"{_money(p90)} in {pm.month}"),
                    summary=_summary(
                        _scenario_sentence(scen),
                        f"No metered budget with 'Stop usage when budget limit is reached' "
                        f"covers {entity} ({len(metered)} metered budgets without stop); the "
                        f"forecast overage p90 is {_money(p90)} (estimated exposure).",
                        "AI credits paid usage is on by default." if policy is None else ""),
                    cost=cost, n_users=run.entity_seats(entity),
                    evidence=[_ev("exposure", **_fig_attrs(cost)),
                              _ev("metered_budgets", n=len(metered), stopping=0),
                              _ev("paid_usage_policy", value=str(policy or "unknown"))],
                    lever_ids=[lv.lever_id for lv in catalog.levers_for_kind(
                        "budget-paid-usage-uncapped", family=_PRODUCT)]))
        return out

    def stop_usage_off(self) -> list[Finding]:
        run = self.run
        per: dict[str, list[_Budget]] = defaultdict(list)
        for b in self.metered():
            if b.stop is not True:
                per[b.entity or "enterprise"].append(b)
        out: list[Finding] = []
        for entity, items in sorted(per.items()):
            scopes: dict[str, int] = defaultdict(int)
            for b in items:
                scopes[b.scope] += 1
            out.append(_finding(
                run, "budget-stop-usage-off", scope={"entity": entity},
                title=f"Copilot metered budgets without 'Stop usage' for {entity}: {len(items)}",
                summary=_summary(
                    f"{len(items)} metered Copilot budgets of {entity} have 'Stop usage when "
                    f"budget limit is reached' off (the default): charges continue past the "
                    f"limit, the budget only alerts.", "Count only; no dollar figure."),
                cost=_count_only("budgets without stop"),
                n_users=run.entity_seats(entity), n_events=len(items),
                evidence=[_ev(f"scope:{s}", n=n) for s, n in sorted(scopes.items())],
                confidence="high"))
        return out

    def zero_user_budget(self) -> list[Finding]:
        run = self.run
        zero = [b for b in self.user_level() if b.amount_nano == 0]
        per_entity: dict[str, list[_Budget]] = defaultdict(list)
        for b in zero:
            per_entity[b.entity or "enterprise"].append(b)
        out: list[Finding] = []
        for entity, items in sorted(per_entity.items()):
            teams: dict[str, int] = defaultdict(int)
            for b in items:
                if b.scope == "user" and b.team:
                    teams[b.team] += 1
            published = {t: n for t, n in teams.items() if n >= run.k}
            for team, n in sorted(published.items()):
                out.append(_finding(
                    run, "budget-zero-user-budget", scope={"entity": entity, "team": team},
                    title=f"User-level Copilot budgets set to $0 in team {team} ({entity}): {n}",
                    summary=_summary(
                        f"{n} user-level budgets in team {team} are $0: user-level budgets "
                        f"always hard-stop and cover pool and metered usage, so these users "
                        f"are blocked at once.", "Count only; no dollar figure."),
                    cost=_count_only("$0 user-level budgets"), n_users=n, n_events=n,
                    evidence=[_ev("zero_user_budgets", n=n, scope="user")], confidence="high"))
            rest = len(items) - sum(published.values())
            if rest <= 0:
                continue
            universal = sum(1 for b in items if b.scope != "user")
            out.append(_finding(
                run, "budget-zero-user-budget", scope={"entity": entity},
                title=f"User-level Copilot budgets set to $0 for {entity}: {rest}",
                summary=_summary(
                    f"{rest} user-level budgets of {entity} are $0"
                    f"{' (in teams below k or without a team)' if published else ''}: "
                    f"user-level budgets always hard-stop and cover pool and metered usage.",
                    f"{universal} of them are multi-user budgets that block every user they "
                    f"cover." if universal else "",
                    "Count only at entity level; no dollar figure."),
                cost=_count_only("$0 user-level budgets"),
                n_users=max(rest, run.entity_seats(entity)), n_events=rest,
                evidence=[_ev("zero_user_budgets", n=rest, multi_user=universal,
                              teams_published=len(published))], confidence="high"))
        return out

    def ulb_gap(self) -> list[Finding]:
        run = self.run
        out: list[Finding] = []
        for entity in sorted(run.pools):
            ulbs = self.user_level(entity)
            if not ulbs:
                continue
            n_seats = run.entity_seats(entity)
            individual = [b for b in ulbs if b.scope == "user" and b.amount_nano is not None]
            cc_ulbs = [b for b in ulbs if b.scope == "multi_user_cost_center"
                       and b.amount_nano is not None]
            universal = [b for b in ulbs if b.scope == "multi_user_customer"
                         and b.amount_nano is not None]
            caps = sum(b.amount_nano for b in individual)  # type: ignore[misc]
            covered = len(individual)
            for b in cc_ulbs:
                n = self.budget_users.get(b.budget_id, self.cc_users.get(b.target or "", 0))
                caps += b.amount_nano * n  # type: ignore[operator]
                covered += n
            if universal:
                amount = min(b.amount_nano for b in universal)  # type: ignore[type-var]
                caps += amount * max(0, n_seats - covered)  # type: ignore[operator]
            elif covered < n_seats:
                continue            # some users have no user-level cap: the check does not apply
            metered = sum(b.amount_nano or 0 for b in self.metered(entity))
            for pm in run.latest_pms(entity):
                scen = pm.plan_scenario
                gap = caps - pm.pool_nano - metered
                if gap <= 0:
                    continue
                cost = estimated(gap, Basis.LIST, note=(
                    "sum of user-level caps - pool value - metered budgets; overlapping "
                    "user-level budgets approximated" + (f"; scenario {scen} (plan unknown)"
                                                         if scen else "")))
                _keep(run, out, _finding(
                    run, "budget-ulb-gap", scope={"entity": entity, "plan_scenario": scen},
                    title=_title(scen, f"Copilot user-level caps exceed pool plus metered "
                                       f"budgets for {entity}: {_money(gap)}"),
                    summary=_summary(
                        _scenario_sentence(scen),
                        f"GitHub's sizing check: user-level caps {_money(caps)} - pool "
                        f"{_money(pm.pool_nano)} = maximum metered {_money(caps - pm.pool_nano)}"
                        f", above the metered budgets {_money(metered)} by {_money(gap)} "
                        f"(estimated)."),
                    cost=cost, n_users=n_seats,
                    evidence=[_ev("caps", nano=caps, budgets=len(ulbs)),
                              _ev("pool", nano=pm.pool_nano, month=pm.month),
                              _ev("metered", nano=metered)]))
        return out

    def org_multi_org_seats(self) -> list[Finding]:
        run = self.run
        org_budgets = [b for b in self.metered() if b.scope == "organization"]
        if not org_budgets:
            return []
        out: list[Finding] = []
        for entity, teams in sorted(run.seats.multi_org.items()):
            relevant = [b for b in org_budgets if b.entity == entity]
            if not relevant:
                continue
            n = sum(v for _, v in teams)
            out.append(_finding(
                run, "budget-org-multi-org-seats", scope={"entity": entity},
                title=f"Org budgets while Copilot seats span several orgs ({entity})",
                summary=_summary(
                    f"{len(relevant)} organization budgets while {n} users hold seats through "
                    f"several organizations: GitHub bills such a seat via a random organization "
                    f"each cycle, so org budgets enforce unpredictably.",
                    "Count only; no dollar figure."),
                cost=_count_only("org budgets with multi-org seats"),
                n_users=max(n, run.entity_seats(entity)), n_events=n,
                evidence=[_ev("org_budgets", n=len(relevant)), _ev("multi_org_users", n=n)]))
        return out

    def no_cost_center_pool(self) -> list[Finding]:
        run = self.run
        latest: dict[str, ConfigSnapshot] = {}
        for c in run.config:
            if c.kind == "cost_center" and c.entity_id.startswith("cc:"):
                cur = latest.get(c.entity_id)
                if cur is None or (c.snapshot_ms, c.fetched_ms) >= (cur.snapshot_ms,
                                                                    cur.fetched_ms):
                    latest[c.entity_id] = c
        seats: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for principal, lic in run.seats.current.items():
            if lic.cost_center:
                entity = run.entity(None, lic.org)
                plans = {x.plan for x in run.seats.all_rows.get(principal, [lic])}
                plan = "enterprise" if "enterprise" in plans else (
                    "business" if "business" in plans else "unknown")
                seats[(entity, lic.cost_center)][plan] += 1
        out: list[Finding] = []
        for cc_id, snap in sorted(latest.items()):
            if _flag(dict(snap.attrs).get("pool_enabled")) is True:
                continue
            name = cc_id[3:]
            for (entity, cc), counts in sorted(seats.items()):
                if cc != name:
                    continue
                if run.billing_mode(entity) != "metered":
                    run.skipped.append(("budget-no-cost-center-pool",
                                        f"billing mode {run.billing_mode(entity)} of {entity} "
                                        f"(cost centers apply only to metered usage)"))
                    continue
                out += self._cc_findings(entity, name, counts)
        return out

    def _cc_findings(self, entity: str, name: str, counts: Mapping[str, int]) -> list[Finding]:
        run = self.run
        out: list[Finding] = []
        months = run.pools.get(entity, {})
        closed = [m for m in sorted(months) if all(pm.finality == "closed" for pm in months[m])]
        month = closed[-1] if closed else (max(months) if months else None)
        if month is None:
            return out
        draw = Decimal(0)
        for line in run.cost_lines:
            if (line.cost_type == _POOLED and line.cost_center == name
                    and line.date_utc[:7] == month):
                qty = _dec(line.quantity)
                if qty is None:
                    gross = (line.list_amount_nano if line.list_amount_nano is not None
                             else line.amount_nano)
                    qty = EXACT_CTX.divide(Decimal(gross), Decimal(_CREDIT_NANO))
                draw = EXACT_CTX.add(draw, qty)
        for pm in months[month]:
            if pm.regime != "overage":
                continue
            scen = pm.plan_scenario
            unknown_as = run.unknown_plan_as(entity, scen)
            allowance = Decimal(0)
            for plan, n in counts.items():
                target = unknown_as if plan == "unknown" else plan
                if target not in _PLANS:
                    allowance = Decimal(-1)
                    break
                per_seat, _ = catalog.copilot_allowance(target, month,
                                                        promo_eligible=pm.promo is not None)
                allowance = EXACT_CTX.add(allowance, EXACT_CTX.multiply(per_seat, Decimal(n)))
            if allowance <= 0 or draw <= allowance * _OVERDRAW:
                continue
            excess = _usd_nano(EXACT_CTX.multiply(draw - allowance, Decimal("0.01")))
            cost = estimated(excess, Basis.LIST_EQUIVALENT, note=(
                f"cost center draw beyond its licences' allowance in {month}, list-equivalent "
                f"credits" + (f"; scenario {scen} (plan unknown)" if scen else "")))
            _keep(run, out, _finding(
                run, "budget-no-cost-center-pool",
                scope={"entity": entity, "cost_center": name, "plan_scenario": scen},
                title=_title(scen, f"Cost center {name} draws {_ratio(draw, allowance)} of its "
                                   f"Copilot allowance ({month})"),
                summary=_summary(
                    _scenario_sentence(scen),
                    f"Cost center {name} drew {_credit_text(draw)} credits in {month} against "
                    f"its licences' allowance of {_credit_text(allowance)} credits while "
                    f"{entity} is in overage and the cost center's AI credit pool is off: excess "
                    f"{_money(excess)} (list-equivalent credit value, estimated)."),
                cost=cost, n_users=sum(counts.values()),
                evidence=[_ev("draw", credits=_credit_text(draw), month=month),
                          _ev("allowance", credits=_credit_text(allowance),
                              seats=sum(counts.values())),
                          _ev("regime", regime=pm.regime, month=pm.month)],
                lever_ids=[lv.lever_id for lv in catalog.levers_for_kind(
                    "budget-no-cost-center-pool", family=_PRODUCT)]))
        return out

    def enterprise_misread(self) -> list[Finding]:
        run = self.run
        budgets = [b for b in self.metered() if b.scope == "enterprise" and b.stop is not True
                   and b.amount_nano is not None]
        if not budgets:
            return []
        low = high = 0
        for entity in sorted(run.plans):
            fees, _ = _seat_fees(run, entity, run.latest_plan(entity))  # type: ignore[arg-type]
            low += fees.nano if fees.nano is not None else fees.low_nano or 0
            high += fees.nano if fees.nano is not None else fees.high_nano or 0
        out: list[Finding] = []
        for b in budgets:
            if b.amount_nano >= low or low <= 0:  # type: ignore[operator]
                continue
            fee_text = (_money(low) if low == high else
                        f"{_money(low)} if Business to {_money(high)} if Enterprise")
            out.append(_finding(
                run, "budget-enterprise-misread", scope={"entity": "enterprise"},
                title=f"Enterprise Copilot budget {_money(b.amount_nano)} below the seat fees",
                summary=_summary(
                    f"An enterprise budget of {_money(b.amount_nano)} without 'Stop usage' is "
                    f"below the monthly seat fees ({fee_text}): the enterprise budget governs "
                    f"metered usage after the pool, not the total bill (maximum bill = seat "
                    f"fees + budget).", "Count only; no dollar figure."),
                cost=_count_only("enterprise budget misread"),
                n_users=sum(run.entity_seats(e) for e in run.plans), n_events=1,
                evidence=[_ev("budget", amount_nano=b.amount_nano, stop=False),
                          _ev("seat_fees", low_nano=low, high_nano=high)]))
            break
        return out


def _keep(run: _Run, out: list[Finding], finding: Finding) -> None:
    """Append *finding* when it reaches ``min_usd`` (``core.findings.min_usd_gate``)."""
    if min_usd_gate(finding, run.ctx):
        out.append(finding)


def _credit_text(credits: Decimal) -> str:
    if credits == credits.to_integral_value():
        return str(int(credits))
    return format(EXACT_CTX.normalize(credits), "f")


def _ratio(draw: Decimal, allowance: Decimal) -> str:
    twice = EXACT_CTX.multiply(draw, Decimal(200))
    pct = int(EXACT_CTX.divide_int(EXACT_CTX.add(twice, allowance),
                                   EXACT_CTX.multiply(allowance, Decimal(2))))
    return f"{pct}%"


# ---------------------------------------------------------------------------------------------
# dq.skipped-kinds
# ---------------------------------------------------------------------------------------------


def _skipped(run: _Run) -> list[Finding]:
    if not run.skipped:
        return []
    reasons: dict[str, list[str]] = defaultdict(list)
    for kind, why in run.skipped:
        if why not in reasons[kind]:
            reasons[kind].append(why)
    entities = sorted(set(run.pools) | set(run.plans) | set(run.seats.seats_by_entity))
    entity = (entities[0] if len(entities) == 1 else
              "enterprise" if run.mode == "enterprise" else None)
    kinds = [k for k in KINDS if k in reasons]
    evidence = [_ev(f"kind:{k}", missing="; ".join(reasons[k])) for k in kinds]
    head = (f"{_NO_FIX}: {len(kinds)} kinds could not run for missing inputs (each named with "
            f"its missing input in the evidence); load that data or accept the gap.")
    listing = _cut("Skipped: " + ", ".join(kinds) + ".", MAX_SUMMARY - len(head) - 1)
    return [_finding(
        run, "dq.skipped-kinds", scope={"entity": entity},
        title=f"Copilot seats and budgets: {len(kinds)} kinds skipped (missing inputs)",
        summary=_summary(head, listing),
        cost=unpriced("data-quality note; no dollars"), n_users=0, evidence=evidence,
        confidence="high", use_catalog_fix=False)]

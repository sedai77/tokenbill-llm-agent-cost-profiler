"""GitHub Copilot budget design: cost-center pools, budgets and GitHub's sizing check (CP-POLICY).

:func:`budget_design` sizes the budgets of addendum §11.3 per pool entity (its latest month) and
returns them as request specs that are **never executed** (DC13). Money is integer nano-USD;
every budget amount is whole dollars, rounded up.

**Metered entities** (DC19: cost centers apply only to metered usage), for every cost center with
at least *k* users (users from the cost center's ``ConfigSnapshot(kind="cost_center")`` ``n_users``,
else distinct principals of its cost lines, else the largest per-cell user count):

* **Cost-center pool.** When the entity is in overage (regime ``overage`` or ``straddling``) and
  the cost center drew more than 100% of its licences' allowance (pooled credits of its cells vs
  its seats × included credits: seat SKU lines when present, else its users × the entity's pool
  per seat), a ``PATCH …/cost-centers/{cost_center_id}`` spec enables its AI credit pool and states
  the choice GitHub asks for at the cap: **block** its members or let them **continue** as paid
  overage (REST exposes only ``ai_credit_pool_enabled``; the choice is made in the billing UI).
* **Cost-center budget** (scope ``cost_center``): forecast overage p90 × 1.1, rounded up to whole
  dollars (at least $1). The entity's p90 (``PoolMonth.overage_forecast`` high, else the observed
  overage of a closed month) is allocated pro rata by pooled credits (ESTIMATED allocation; a
  capped cost-center entity ``cc:<n>`` uses its own). ``prevent_further_usage`` is false, with the
  trade-off note (true would stop the cost center's paid usage at the budget).
* **User-level budget** (scope ``multi_user_cost_center``): the cost center's nearest-rank p99 of
  monthly per-user credits (cost lines with a principal; the principals never leave this module),
  computed only from at least *k* users, never below $1; only the amount is published.
  ``prevent_further_usage`` is true — user-level budgets always hard-stop, and the API requires it
  for the ``user``, ``multi_user_customer`` and user-level cost-center scopes.
* **Enterprise / organization budget**: at least Σ cost-center budgets (and at least the entity's
  own overage p90 × 1.1) plus the direct-org forecast (``direct_net_nano``; an open month
  extrapolated linearly over its observed days, ESTIMATED).
* **Sizing check** (GitHub's): max metered = Σ user-level caps − pool, printed per entity.

**Volume / azure / unknown billing mode**: no cost-center pool, cost-center budget or user-level
advice (§19.5 #27: whether volume-licensed seats' included credits can be assigned to cost centers
is unverified), only the enterprise / organization budget with a VERIFY note.

**Plan unknown (R17).** Pool months come per scenario; every pool-dependent spec (cost-center pool
advice, cost-center and enterprise budgets, the sizing check) is emitted once per scenario with
``scenario`` = ``business`` | ``enterprise`` and ``if Business:`` / ``if Enterprise:`` in its text
and request note. User-level budgets do not depend on the pool and appear once (``scenario`` None).
With every plan known no spec carries a scenario.

A spec is a plain dict: ``kind`` (``cost_center_pool`` | ``budget`` | ``sizing_check`` | ``note``),
``scenario``, ``entity_id``, ``month``, ``billing_mode``, ``text`` (content-free) and ``request``
(``{"method", "path", "api_version", "body", "note", "lever_id", "auth"}`` or None). Paths keep
GitHub's ``{enterprise}`` / ``{cost_center_id}`` placeholders; alert recipients are a placeholder,
never a login. Deterministic and independent of input order; malformed input → ``UsageError``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from tokenbill.core import catalog
from tokenbill.core.errors import UsageError
from tokenbill.core.money import fmt_usd
from tokenbill.core.pool import Cell
from tokenbill.core.records import ConfigSnapshot, CostLine
from tokenbill.core.types import PoolMonth

__all__ = [
    "BUDGET_LEVER",
    "OVERAGE_FACTOR",
    "SPEC_KINDS",
    "USER_LEVEL_SCOPES",
    "budget_design",
]

#: The lever every budget spec belongs to (addendum §11.1).
BUDGET_LEVER = "copilot.budget_plan"
#: Metered budgets are forecast overage p90 × 1.1 (addendum §11.3).
OVERAGE_FACTOR = (11, 10)
#: Budget scopes for which the API requires ``prevent_further_usage: true`` (user-level budgets
#: always hard-stop).
USER_LEVEL_SCOPES = frozenset({"user", "multi_user_customer", "multi_user_cost_center"})
#: ``kind`` values of the returned specs, in output order per entity.
SPEC_KINDS = ("cost_center_pool", "budget", "sizing_check", "note")

_API_VERSION = "2026-03-10"
_BUDGETS_PATH = "/enterprises/{enterprise}/settings/billing/budgets"
_CC_PATCH_PATH = "/enterprises/{enterprise}/settings/billing/cost-centers/{cost_center_id}"
_POOLED = "ai_credit.user"
_CHANNEL = "github_copilot"
_NANO_PER_USD = 10**9
_IN_OVERAGE = ("overage", "straddling")
_SCENARIO_ORDER = {None: 0, "business": 1, "enterprise": 2}
_MAX_COUNT = 10**9
_TRADEOFF = ("prevent_further_usage false: alerts only (trade-off: true would stop further paid "
             "usage at the budget; set it only where blocking is acceptable)")
_HARD_STOP = ("prevent_further_usage true: user-level budgets always hard-stop and the API "
              "requires it for user-level scopes")
_VOLUME_NOTE = ("cost centers apply only to metered usage; no cost-center pool, cost-center or "
                "user-level budget advice (VERIFY whether volume-licensed seats' included credits "
                "can be assigned to cost centers)")


def _auth() -> str:
    auth = catalog.ADMIN_ACTIONS["rest:budget_create"].auth_note
    return auth if auth is not None else "enterprise admin or billing manager"


def _check_k(k: object) -> int:
    if type(k) is not int or k < 1:
        raise UsageError("k-anonymity threshold k must be an int >= 1")
    return k


def _as_list(items: object, cls: type, what: str) -> list:
    if items is None:
        return []
    if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        raise UsageError(f"{what} must be a sequence")
    out = list(items)
    if any(not isinstance(x, cls) for x in out):
        raise UsageError(f"{what} must hold {cls.__name__} records")
    return out


def _ceil_usd(nano: int) -> int:
    """Whole dollars, rounded up (nano ≤ 0 → 0)."""
    return max(0, -(-nano // _NANO_PER_USD))


def _times_factor(nano: int) -> int:
    num, den = OVERAGE_FACTOR
    return -(-nano * num // den)


def _dec(value: object) -> Fraction | None:
    """A non-negative exact number below 10**15 from an int or a decimal string, else None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(value) if isinstance(value, int) else Decimal(str(value).strip()[:40])
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or not 0 <= d < Decimal(10) ** 15:
        return None
    return Fraction(d)


def _scen_prefix(scenario: str | None) -> str:
    return f"if {scenario.capitalize()}: " if scenario else ""


def _overage_p90(pm: PoolMonth) -> int | None:
    """Forecast overage p90 (the forecast's high bound, else its point) of an open month; the
    observed overage of a closed month; None when the forecast is unpriced."""
    fig = pm.overage_forecast
    if fig is not None:
        if fig.high_nano is not None:
            return max(0, fig.high_nano)
        return None if fig.nano is None else max(0, fig.nano)
    return max(0, pm.overage_observed_nano)


def _direct_forecast(pm: PoolMonth) -> int:
    """Direct-org net forecast: observed in a closed month, linear over observed days else."""
    days = pm.days_final + pm.days_provisional
    if pm.finality == "closed" or days <= 0 or days >= pm.days_in_month:
        return max(0, pm.direct_net_nano)
    return max(0, -(-pm.direct_net_nano * pm.days_in_month // days))


def _per_seat_credits(pm: PoolMonth) -> Fraction | None:
    seats = Fraction(0)
    for _, n in pm.seats:
        d = _dec(n)
        if d is None:
            return None
        seats += d
    pool = _dec(pm.pool_credits)
    if pool is None or seats <= 0:
        return None
    return pool / seats


class _Inputs:
    """The per-cost-center views of the cells, cost lines and configuration."""

    def __init__(self, cells: Sequence[Cell], config: Sequence[ConfigSnapshot],
                 cost_lines: Sequence[CostLine]) -> None:
        self.draw: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
        self.entity_draw: dict[tuple[str, str], Fraction] = defaultdict(Fraction)
        self.cell_users: dict[tuple[str, str, str], int] = defaultdict(int)
        for c in cells:
            if c.cost_type != _POOLED:
                continue
            credits = _dec(c.credits) or Fraction(0)
            self.entity_draw[(c.entity_id, c.month)] += credits
            if c.cost_center:
                key = (c.entity_id, c.month, c.cost_center)
                self.draw[key] += credits
                self.cell_users[key] = max(self.cell_users[key], c.n_users)
        self.cc_users: dict[str, int] = {}
        latest: dict[str, tuple] = {}
        for snap in config:
            if snap.kind != "cost_center" or not snap.entity_id.startswith("cc:"):
                continue
            name = snap.entity_id[3:]
            order = (snap.snapshot_ms, snap.fetched_ms, snap.source_kind)
            if name in latest and latest[name] >= order:
                continue
            latest[name] = order
            n = dict(snap.attrs).get("n_users")
            if type(n) is int and 0 <= n <= _MAX_COUNT:
                self.cc_users[name] = n
            else:
                self.cc_users.pop(name, None)
        self.user_credits: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: defaultdict(int))
        self.seat_lines: dict[tuple[str, str], dict[str, Fraction]] = defaultdict(
            lambda: defaultdict(Fraction))
        for line in cost_lines:
            if line.channel != _CHANNEL or not line.cost_center:
                continue
            month = line.date_utc[:7]
            if line.cost_type == _POOLED and line.principal is not None:
                gross = (line.list_amount_nano if line.list_amount_nano is not None
                         else line.amount_nano)
                self.user_credits[(month, line.cost_center)][line.principal] += gross
            elif line.cost_type == "seat":
                plan = catalog.copilot_seat_plan(line.sku)
                seats = _dec(line.quantity) if line.quantity is not None else None
                if seats is not None:
                    self.seat_lines[(month, line.cost_center)][plan or "unknown"] += seats

    def users(self, entity: str, month: str, cc: str) -> int:
        if cc in self.cc_users:
            return self.cc_users[cc]
        people = self.user_credits.get((month, cc))
        if people:
            return len(people)
        return self.cell_users.get((entity, month, cc), 0)

    def cost_centers(self, entity: str, month: str) -> list[str]:
        return sorted({cc for (e, m, cc) in self.draw if e == entity and m == month})


def _allowance(inp: _Inputs, pm: PoolMonth, cc: str, users: int) -> Fraction | None:
    """The cost center's licences' allowance in credits for *pm*'s month and scenario."""
    lines = inp.seat_lines.get((pm.month, cc))
    per_seat = _per_seat_credits(pm)
    promo = pm.promo is not None
    if lines:
        total = Fraction(0)
        for plan, seats in sorted(lines.items()):
            if plan in ("business", "enterprise"):
                per = Fraction(catalog.copilot_allowance(plan, pm.month, promo_eligible=promo)[0])
            elif per_seat is not None:
                per = per_seat
            else:
                return None
            total += seats * per
        return total
    if per_seat is None or users <= 0:
        return None
    return users * per_seat


def _request(method: str, path: str, body: Mapping[str, object], note: str) -> dict[str, object]:
    return {"method": method, "path": path, "api_version": _API_VERSION, "body": dict(body),
            "note": note, "lever_id": BUDGET_LEVER, "auth": _auth()}


def _budget_body(amount: int, scope: str, entity_name: str) -> dict[str, object]:
    return {
        "budget_amount": amount,
        "prevent_further_usage": scope in USER_LEVEL_SCOPES,
        "budget_alerting": {"will_alert": True,
                            "alert_recipients": ["<login of a billing manager to alert>"]},
        "budget_scope": scope,
        "budget_type": "BundlePricing",
        "budget_product_sku": "ai_credits",
        "budget_entity_name": entity_name,
    }


def _spec(kind: str, pm: PoolMonth, scenario: str | None, text: str,
          request: dict[str, object] | None = None) -> dict[str, object]:
    return {"kind": kind, "scenario": scenario, "entity_id": pm.entity_id, "month": pm.month,
            "billing_mode": pm.billing_mode, "text": text, "request": request}


def _pct(num: Fraction, den: Fraction) -> str:
    return str(math.floor(num * 100 / den))


def _entity_scope(entity: str) -> tuple[str, str]:
    if entity.startswith("org:"):
        return "organization", entity[4:]
    if entity.startswith("cc:"):
        return "cost_center", entity[3:]
    return "enterprise", "<enterprise slug>"


def _p99(values: Sequence[int]) -> int:
    ordered = sorted(values)
    return ordered[(99 * len(ordered) + 99) // 100 - 1]


def _user_level(inp: _Inputs, pm: PoolMonth, k: int) -> tuple[list[dict[str, object]],
                                                               dict[str, int]]:
    """User-level budget specs (scenario-independent) and the per-cost-center cap totals."""
    specs: list[dict[str, object]] = []
    caps: dict[str, int] = {}
    for cc in inp.cost_centers(pm.entity_id, pm.month):
        people = inp.user_credits.get((pm.month, cc), {})
        if not people:
            continue
        if len(people) < k:
            specs.append(_spec("note", pm, None,
                               f"Cost center {cc}: fewer than k={k} users with usage, so no "
                               "user-level budget amount is computed (k-anonymity)."))
            continue
        amount = max(1, _ceil_usd(_p99(list(people.values()))))
        caps[cc] = amount * _NANO_PER_USD * inp.users(pm.entity_id, pm.month, cc)
        month_note = " (open month: month to date, may understate)" if pm.finality == "open" \
            else ""
        text = (f"Cost center {cc}: user-level budget ${amount} per user (cohort p99 of monthly "
                f"credits from at least k={k} users{month_note}); {_HARD_STOP}.")
        specs.append(_spec("budget", pm, None, text, _request(
            "POST", _BUDGETS_PATH, _budget_body(amount, "multi_user_cost_center", cc),
            f"User-level budget for cost center {cc}: ${amount} per user = cohort p99, never "
            f"below $1; {_HARD_STOP}.")))
    return specs, caps


def _metered(inp: _Inputs, group: Sequence[PoolMonth], k: int,
             caps: Mapping[tuple[str, str], dict[str, int]]) -> list[dict[str, object]]:
    """Scenario-dependent specs of one billing group (an enterprise with its capped cost-center
    entities, or one organization) in one month and scenario."""
    specs: list[dict[str, object]] = []
    cc_budget_total = 0
    lead = group[0]
    scenario = lead.plan_scenario
    pre = _scen_prefix(scenario)
    for pm in group:
        p90 = _overage_p90(pm)
        if pm.entity_id.startswith("cc:"):
            if p90 is None:
                specs.append(_spec("note", pm, scenario, f"{pre}capped cost center "
                                   f"{pm.entity_id[3:]}: overage forecast unpriced, no budget."))
                continue
            amount = max(1, _ceil_usd(_times_factor(p90)))
            cc_budget_total += amount * _NANO_PER_USD
            policy = pm.capped_policy or "unknown"
            specs.append(_spec("budget", pm, scenario, (
                f"{pre}Capped cost center {pm.entity_id[3:]}: budget ${amount} = forecast "
                f"overage p90 {fmt_usd(p90)} x 1.1, rounded up; at the cap its members are "
                f"'{policy}' (block or continue); {_TRADEOFF}."), _request(
                "POST", _BUDGETS_PATH, _budget_body(amount, "cost_center", pm.entity_id[3:]),
                f"{pre}cost-center budget: forecast overage p90 x 1.1 rounded up; {_TRADEOFF}.")))
            continue
        entity_draw = inp.entity_draw.get((pm.entity_id, pm.month), Fraction(0))
        in_overage = pm.regime in _IN_OVERAGE
        for cc in inp.cost_centers(pm.entity_id, pm.month):
            users = inp.users(pm.entity_id, pm.month, cc)
            if users < k:
                specs.append(_spec("note", pm, scenario, f"{pre}Cost center {cc}: fewer than "
                                   f"k={k} users, no cost-center figures (k-anonymity)."))
                continue
            draw = inp.draw[(pm.entity_id, pm.month, cc)]
            allowance = _allowance(inp, pm, cc, users)
            if allowance is None:
                specs.append(_spec("note", pm, scenario, f"{pre}Cost center {cc}: licences' "
                                   "allowance unknown (no seat counts), pool advice skipped."))
            elif in_overage and draw > allowance:
                pct = _pct(draw, allowance) if allowance > 0 else "unbounded"
                text = (f"{pre}Cost center {cc} drew {pct}% of its licences' allowance "
                        f"({_fmt_credits(draw)} of {_fmt_credits(allowance)} credits) while "
                        f"{pm.entity_id} is in {pm.regime}: enable its AI credit pool and choose "
                        "what happens at the cap - block its members, or let them continue as "
                        "paid overage (chosen in the billing UI; REST sets only "
                        "ai_credit_pool_enabled).")
                specs.append(_spec("cost_center_pool", pm, scenario, text, {
                    "method": "PATCH", "path": _CC_PATCH_PATH, "api_version": _API_VERSION,
                    "body": {"ai_credit_pool_enabled": True},
                    "note": (f"{pre}cost center {cc}: {pct}% of its licences' allowance in an "
                             "overage month; enable its AI credit pool, then choose block or "
                             "continue at the cap in the billing UI (cost_center_id from GET "
                             "/enterprises/{enterprise}/settings/billing/cost-centers)."),
                    "lever_id": BUDGET_LEVER, "auth": _auth()}))
            if p90 is None:
                continue
            share = draw / entity_draw if entity_draw > 0 else Fraction(0)
            alloc = math.ceil(p90 * share)
            amount = max(1, _ceil_usd(_times_factor(alloc)))
            cc_budget_total += amount * _NANO_PER_USD
            specs.append(_spec("budget", pm, scenario, (
                f"{pre}Cost center {cc}: metered budget ${amount} = its pro-rata share of the "
                f"forecast overage p90 ({fmt_usd(alloc)}, estimated allocation by pooled "
                f"credits) x 1.1, rounded up; {_TRADEOFF}."), _request(
                "POST", _BUDGETS_PATH, _budget_body(amount, "cost_center", cc),
                f"{pre}cost-center metered budget for {cc}: forecast overage p90 share x 1.1 "
                f"rounded up (estimated); {_TRADEOFF}.")))
    if not lead.entity_id.startswith("cc:"):
        specs += _top_budget(group, cc_budget_total, caps)
    return specs


def _fmt_credits(credits: Fraction) -> str:
    return f"{math.floor(credits):,}"


def _top_budget(group: Sequence[PoolMonth], cc_budget_total: int,
                caps: Mapping[tuple[str, str], dict[str, int]]) -> list[dict[str, object]]:
    lead = group[0]
    scenario = lead.plan_scenario
    pre = _scen_prefix(scenario)
    p90 = _overage_p90(lead)
    specs: list[dict[str, object]] = []
    if p90 is None:
        specs.append(_spec("note", lead, scenario, f"{pre}{lead.entity_id}: overage forecast "
                           "unpriced, no enterprise / organization budget sized."))
    else:
        direct = sum(_direct_forecast(pm) for pm in group)
        base = max(cc_budget_total, _times_factor(p90))
        amount = max(1, _ceil_usd(base + direct))
        scope, name = _entity_scope(lead.entity_id)
        specs.append(_spec("budget", lead, scenario, (
            f"{pre}{lead.entity_id}: {scope} budget ${amount} >= the cost-center budgets "
            f"({fmt_usd(cc_budget_total)}) and the forecast overage p90 x 1.1 "
            f"({fmt_usd(_times_factor(p90))}), plus the direct-org forecast ({fmt_usd(direct)}); "
            f"{_TRADEOFF}."), _request(
            "POST", _BUDGETS_PATH, _budget_body(amount, scope, name),
            f"{pre}{scope} budget: at least the cost-center budgets plus the direct-org forecast; "
            f"{_TRADEOFF}.")))
    cap_total = sum(sum(caps.get((pm.entity_id, pm.month), {}).values()) for pm in group)
    pool = sum(pm.pool_nano for pm in group)
    if cap_total:
        max_metered = max(0, cap_total - pool)
        text = (f"{pre}Sizing check (GitHub) for {lead.entity_id}: user-level caps "
                f"{fmt_usd(cap_total)} - pool {fmt_usd(pool)} = max metered "
                f"{fmt_usd(max_metered)}.")
    else:
        text = (f"{pre}Sizing check (GitHub) for {lead.entity_id}: no user-level budgets sized, "
                f"so max metered = user-level caps - pool {fmt_usd(pool)} cannot be computed.")
    specs.append(_spec("sizing_check", lead, scenario, text))
    return specs


def _unmetered(pm: PoolMonth) -> list[dict[str, object]]:
    scenario = pm.plan_scenario
    pre = _scen_prefix(scenario)
    if pm.entity_id.startswith("cc:"):
        return [_spec("note", pm, scenario, f"{pre}{pm.entity_id}: billing mode "
                      f"{pm.billing_mode}; {_VOLUME_NOTE}.")]
    p90 = _overage_p90(pm)
    specs = [_spec("note", pm, scenario, f"{pre}{pm.entity_id}: billing mode {pm.billing_mode}; "
                   f"{_VOLUME_NOTE}.")]
    if p90 is None:
        return specs
    direct = _direct_forecast(pm)
    amount = max(1, _ceil_usd(_times_factor(p90) + direct))
    scope, name = _entity_scope(pm.entity_id)
    specs.append(_spec("budget", pm, scenario, (
        f"{pre}{pm.entity_id}: {scope} budget ${amount} = forecast overage p90 x 1.1 plus the "
        f"direct-org forecast ({fmt_usd(direct)}), rounded up; VERIFY how {pm.billing_mode} "
        f"billing invoices overage; {_TRADEOFF}."), _request(
        "POST", _BUDGETS_PATH, _budget_body(amount, scope, name),
        f"{pre}{scope} budget ({pm.billing_mode} billing, VERIFY): forecast overage p90 x 1.1 "
        f"plus the direct-org forecast; {_TRADEOFF}.")))
    return specs


def _latest(pools: Sequence[PoolMonth]) -> list[PoolMonth]:
    """The latest month of every (entity, scenario)."""
    best: dict[tuple[str, str | None], PoolMonth] = {}
    for pm in pools:
        key = (pm.entity_id, pm.plan_scenario)
        if key not in best or pm.month > best[key].month:
            best[key] = pm
    return list(best.values())


def _groups(pools: Sequence[PoolMonth]) -> list[list[PoolMonth]]:
    """Billing groups per (month, scenario): the enterprise with its capped cost centers (lead
    first), or each organization alone."""
    by_key: dict[tuple[str, str | None], list[PoolMonth]] = defaultdict(list)
    orgs: list[list[PoolMonth]] = []
    for pm in pools:
        if pm.entity_id.startswith("org:"):
            orgs.append([pm])
        else:
            by_key[(pm.month, pm.plan_scenario)].append(pm)
    out = []
    for members in by_key.values():
        members.sort(key=lambda p: (p.entity_id != "enterprise", p.entity_id))
        out.append(members)
    return out + orgs


def budget_design(pools: Sequence[PoolMonth], cells: Sequence[Cell],
                  config: Sequence[ConfigSnapshot], *, k: int,
                  cost_lines: Sequence[CostLine] = ()) -> list[dict[str, object]]:
    """Budget request specs per pool entity (module docstring): cost-center pools with the
    block-or-continue choice, cost-center, user-level and enterprise / organization budgets in
    whole dollars and GitHub's sizing check, once per plan scenario while a plan is unknown.

    *pools* are ``core.pool`` pool months (the latest month of each entity and scenario is
    designed), *cells* the Copilot cell table, *config* the configuration snapshots (cost-center
    user counts), *k* the k-anonymity threshold; additive keyword *cost_lines* (AI usage report
    lines with principals and seat lines) enables the per-user p99 and seat-line allowances.
    Nothing is executed."""
    k = _check_k(k)
    pool_list = _latest(_as_list(pools, PoolMonth, "pools"))
    inp = _Inputs(_as_list(cells, Cell, "cells"), _as_list(config, ConfigSnapshot, "config"),
                  _as_list(cost_lines, CostLine, "cost_lines"))
    specs: list[dict[str, object]] = []
    caps: dict[tuple[str, str], dict[str, int]] = {}
    done_user_level: set[tuple[str, str]] = set()
    for pm in sorted(pool_list, key=lambda p: (p.entity_id, p.month,
                                               _SCENARIO_ORDER[p.plan_scenario])):
        if pm.billing_mode != "metered" or pm.entity_id.startswith("cc:"):
            continue
        key = (pm.entity_id, pm.month)
        if key in done_user_level:
            continue
        done_user_level.add(key)
        user_specs, caps[key] = _user_level(inp, pm, k)
        specs += user_specs
    metered = [pm for pm in pool_list if pm.billing_mode == "metered"]
    for pm in pool_list:
        if pm.billing_mode != "metered":
            specs += _unmetered(pm)
    for group in _groups(metered):
        specs += _metered(inp, group, k, caps)
    kind_order = {kind: i for i, kind in enumerate(SPEC_KINDS)}
    specs.sort(key=lambda s: (_SCENARIO_ORDER[s["scenario"]], str(s["entity_id"]),  # type: ignore[index]
                              str(s["month"]), kind_order[str(s["kind"])], str(s["text"])))
    return specs

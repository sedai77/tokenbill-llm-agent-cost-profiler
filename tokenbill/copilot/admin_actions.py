"""GitHub Copilot admin checklist: what an admin changes, where, and with which rights (CP-POLICY).

:func:`admin_actions` turns the Copilot aggregate plan(s) and the Copilot findings into
:class:`~tokenbill.core.types.AdminAction` items (addendum §11.3, §9.3, §19.4; DC13, DC19, DC20,
DC21; R17, ruling R-E22). Nothing is ever executed (DC13): REST items point at the request file
``github/requests.jsonl`` that :mod:`tokenbill.copilot.policy` writes.

**Triggers.** An item exists for every ``core.catalog.ADMIN_ACTIONS`` id reached by (a) a lever of
the plan (every ``LeverResult`` of the known plan or of either scenario plan), (b) a lever a
Copilot finding links (``Finding.lever_ids`` and ``levers_for_kind(kind, family="copilot")``) or
(c) a finding kind of :data:`KIND_ACTIONS` (budget, paid-usage, CLI billing and review items that
no lever delivers). Levers deliver through their ``patch_keys`` that are ``ADMIN_ACTIONS`` ids;
``COPILOT_ALLOWLIST`` keys are the managed-settings patch, not checklist items.

**Special items.**

* ``admin:plan_confirm`` is **first** whenever a ``PlanEvidence`` is ``unknown`` or has a
  ``conflict``, a pool month or finding carries a ``plan_scenario``, or scenario plans are given.
* :data:`JETBRAINS_ACTION_ID` ("JetBrains limits") follows whenever any team's JetBrains
  interaction share is above 0 (``teams`` counts or ``editor-mix`` findings): managed ``model`` is
  not applied in JetBrains and managed telemetry support for JetBrains is documented
  inconsistently, so those teams get the server-side model policy (reach 1) and Auto tier
  communication; both items are added too.
* ``admin:vscode_db_exporter_optin`` (the developer opt-in of the VS Code usage database, a user
  setting no managed key can enforce) is always listed last among the enablers.

**Plan unknown (R17).** ``admin:seat_plan_change`` and ``copilot.seat_downgrade`` — every
"Business instead of Enterprise" item — are omitted and named once by
:data:`NOT_ASSESSED_PLAN_UNKNOWN` (the pack prints it); a projected item shows both scenario values
in ``what`` ("if Business: …; if Enterprise: …") and carries ``projection=None``, because a single
figure would merge or choose a scenario.

**Billing mode (DC19).** Cost-center pool items need a ``metered`` pool entity; volume, azure and
unknown entities get enterprise / org budget items only.

**Figures.** A lever's projection is the sum of its billed-basis ``LeverResult.projected_monthly``
(``LIST_EQUIVALENT`` pool-headroom results are never shown as savings, R11) on the lever's first
checklist item only, so no saving appears twice. Reach (§9.3) is ``"1"`` for server-side items
that change what every surface can use (:data:`REACH_ONE`) and None for communication, budget and
behavioral items (not projected).

**Deadlines.** Seat items of metered entities: the next 1st (UTC), because removals are billed to
the end of the cycle; volume / azure entities: the ``renewal_date.<entity>`` run flag when given,
else none. The code-review Lite item: ``2026-09-28`` (``core.facts``) while today is before it.

Content-free: ``what`` holds catalog text plus counts and dates; no finding text, name or login is
copied. Pure and deterministic: the output does not depend on the order of any input.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation

from tokenbill.core import catalog
from tokenbill.core import facts as _facts
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Basis, Figure, add
from tokenbill.core.money import fmt_usd
from tokenbill.core.pool import run_flags
from tokenbill.core.records import ConfigSnapshot
from tokenbill.core.types import ActionPlan, AdminAction, Finding, PlanEvidence, PoolMonth

__all__ = [
    "API_VERSION",
    "EXCLUDED_EDITOR_FAMILIES",
    "JETBRAINS_ACTION_ID",
    "JETBRAINS_HEAVY_SHARE",
    "KIND_ACTIONS",
    "NOT_ASSESSED_PLAN_UNKNOWN",
    "REACH_ONE",
    "REQUESTS_FILE",
    "SCENARIOS",
    "SEAT_ACTIONS",
    "admin_actions",
    "figure_text",
    "is_copilot_finding",
    "lever_projection",
    "plan_unknown",
    "scenario_projection_text",
    "split_plans",
    "team_editor_shares",
]

#: ``X-GitHub-Api-Version`` of every emitted request (addendum §11.3, DC16).
API_VERSION = "2026-03-10"
#: The request file every REST item points at.
REQUESTS_FILE = "github/requests.jsonl"
#: Plan scenarios while a plan is unknown (R17).
SCENARIOS = ("business", "enterprise")
#: ``action_id`` of the JetBrains-limits item.
JETBRAINS_ACTION_ID = "copilot:jetbrains-limits"
#: Editor families the managed ``model`` key does not reach (§9.3): JetBrains (fact-checked);
#: Visual Studio, Xcode and Eclipse are unverified and therefore excluded.
EXCLUDED_EDITOR_FAMILIES = ("jetbrains", "visual_studio", "xcode", "eclipse")
#: A team is JetBrains-heavy at this JetBrains interaction share (§9.3, revision 3).
JETBRAINS_HEAVY_SHARE = Decimal("0.5")
#: Printed once instead of every "Business instead of Enterprise" item while a plan is unknown.
NOT_ASSESSED_PLAN_UNKNOWN = ("Not assessed: plan unknown. Seat plan changes (Copilot Business "
                             "instead of Enterprise, lever copilot.seat_downgrade) are only "
                             "assessed once the plan is confirmed.")
#: Server-side items whose delivery reaches every surface (addendum §9.3: reach 1).
REACH_ONE = frozenset({
    "admin:model_policy", "admin:model_policy_fast", "admin:org_seat_policy",
    "admin:seat_plan_change", "admin:runner_type", "rest:org_selected_users_delete",
})
#: Seat items: their deadline follows the billing mode (addendum §10.0).
SEAT_ACTIONS = frozenset({
    "rest:org_selected_users_delete", "rest:org_selected_teams_delete", "admin:org_seat_policy",
    "admin:seat_plan_change", "admin:team_membership_review",
})
#: Checklist items a finding kind asks for beyond its levers' ``patch_keys`` (addendum §10.1,
#: §10.2 fix columns).
KIND_ACTIONS: Mapping[str, tuple[str, ...]] = {
    "plan-status": ("admin:plan_confirm",),
    "overage-forecast": ("admin:budget_stop", "admin:paid_usage_policy", "admin:cost_center_pool"),
    "budget-paid-usage-uncapped": ("admin:budget_stop", "admin:paid_usage_policy"),
    "budget-stop-usage-off": ("admin:budget_stop",),
    "budget-zero-user-budget": ("admin:budget_stop",),
    "budget-ulb-gap": ("admin:budget_stop",),
    "budget-org-multi-org-seats": ("admin:budget_stop",),
    "budget-enterprise-misread": ("admin:budget_stop",),
    "budget-no-cost-center-pool": ("admin:cost_center_pool", "rest:cost_center_patch"),
    "plan-mix": ("admin:seat_plan_change",),
    "direct-org-usage": ("admin:review_unlicensed_policy", "admin:org_cli_billing_policy",
                         "admin:ci_limits_snippet"),
    "agentic-workflow-cost": ("admin:aw_triggers", "admin:org_cli_billing_policy"),
    "ci-uncapped": ("admin:ci_limits_snippet",),
    "review-cost": ("admin:review_effort_default", "admin:communicate_personal_review_settings"),
    "review-default-balanced": ("admin:review_effort_default",
                                "admin:communicate_personal_review_settings"),
    "review-drivers": ("admin:repo_review_mcp_off", "admin:review_instructions",
                       "admin:review_triggers", "admin:communicate_personal_review_settings"),
    "cloud-agent-cost": ("admin:runner_type", "rest:coding_agent_policy"),
    "unattributed-spend": ("rest:cost_center_create",),
    "auto-adoption": ("admin:communicate_auto_tier",),
    "cache-health": ("admin:vscode_db_exporter_optin",),
}
#: Items behind a kind-only trigger are credited to this lever (the budget lever of §11.1).
_KIND_ONLY_LEVER: Mapping[str, str] = {
    "admin:budget_stop": "copilot.budget_plan",
    "admin:paid_usage_policy": "copilot.budget_plan",
    "admin:cost_center_pool": "copilot.budget_plan",
    "rest:coding_agent_policy": "copilot.agent_runner_standard",
}
_PLAN_ADVICE = frozenset({"admin:seat_plan_change"})
_DOWNGRADE_LEVER = "copilot.seat_downgrade"
_COST_CENTER_ITEMS = frozenset({"admin:cost_center_pool", "rest:cost_center_patch",
                                "rest:cost_center_create"})
_OPTIN = "admin:vscode_db_exporter_optin"
_PLAN_CONFIRM = "admin:plan_confirm"
_MODEL_POLICY = "admin:model_policy"
_AUTO_TIER = "admin:communicate_auto_tier"
_REVIEW_LITE = "admin:review_effort_default"
_SEAT_DELETE = "rest:org_selected_users_delete"
_AW_TRIGGERS = "admin:aw_triggers"
_CI_LIMITS = "admin:ci_limits_snippet"
_KNOWN_KEYS = (None, "known", "all")
_WHAT_MAX = 400
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_IDLE_RE = re.compile(r"copilot:seats_idle=(\d{1,4})d@")
_MAX_COUNT = 10**9
_LEVER_ORDER: Mapping[str, int] = {lv.lever_id: i for i, lv in enumerate(catalog.COPILOT_LEVERS)}


# ---------------------------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------------------------


def split_plans(plans_by_scenario: object) -> tuple[ActionPlan | None,
                                                     tuple[tuple[str, ActionPlan], ...]]:
    """``(known plan or None, ((scenario, plan), …))`` from CP-PLAN's scenario pairs (a sequence
    of ``(key, ActionPlan)`` or a mapping): keys ``None`` / ``"known"`` / ``"all"`` name the single
    plan of known plans, ``"business"`` / ``"enterprise"`` the scenario plans (R17). A known plan
    beside scenario plans, a repeated key or any other key → ``UsageError``."""
    if plans_by_scenario is None:
        return None, ()
    if isinstance(plans_by_scenario, Mapping):
        items = list(plans_by_scenario.items())
    elif isinstance(plans_by_scenario, (list, tuple)):
        items = list(plans_by_scenario)
    else:
        raise UsageError("plans_by_scenario must be (scenario, ActionPlan) pairs")
    known: ActionPlan | None = None
    scen: dict[str, ActionPlan] = {}
    seen: set[object] = set()
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise UsageError("plans_by_scenario must be (scenario, ActionPlan) pairs")
        key, plan = item
        if not isinstance(plan, ActionPlan):
            raise UsageError("plans_by_scenario values must be ActionPlans")
        norm = None if key in _KNOWN_KEYS else key
        if norm in seen:
            raise UsageError("plans_by_scenario repeats a scenario")
        seen.add(norm)
        if norm is None:
            known = plan
        elif norm in SCENARIOS:
            scen[norm] = plan  # type: ignore[index]
        else:
            raise UsageError("plans_by_scenario keys must be known | business | enterprise")
    if known is not None and scen:
        raise UsageError("a known plan and scenario plans cannot be given together (R17)")
    return known, tuple(sorted(scen.items()))


def _as_list(items: object, cls: type, what: str) -> list:
    if items is None:
        return []
    if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        raise UsageError(f"{what} must be a sequence")
    out = list(items)
    if any(not isinstance(x, cls) for x in out):
        raise UsageError(f"{what} must hold {cls.__name__} records")
    return out


def _today(today: object) -> _dt.date:
    if isinstance(today, _dt.datetime):
        return today.date()
    if isinstance(today, _dt.date):
        return today
    if isinstance(today, str) and _DATE_RE.match(today[:10]):
        try:
            return _dt.date.fromisoformat(today[:10])
        except ValueError:
            pass
    raise UsageError("today must be a YYYY-MM-DD date")


def is_copilot_finding(f: Finding) -> bool:
    """A finding of a Copilot detector (``copilot.*``) or with a ``product=copilot`` scope."""
    return f.detector_id.startswith("copilot.") or ("product", "copilot") in f.scope.dims


def _scenario_of(f: Finding) -> str | None:
    return dict(f.scope.dims).get("plan_scenario")


def plan_unknown(plans: Sequence[PlanEvidence], pools: Sequence[PoolMonth],
                 scenario_plans: Sequence[tuple[str, ActionPlan]] = (),
                 findings: Sequence[Finding] = ()) -> bool:
    """True while any entity's plan is unknown (R17): an ``unknown`` ``PlanEvidence``, a pool
    month or finding in a ``plan_scenario``, or scenario plans."""
    return (bool(scenario_plans) or any(pe.plan == "unknown" for pe in plans)
            or any(pm.plan_scenario is not None for pm in pools)
            or any(_scenario_of(f) is not None for f in findings))


def _int_attr(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_COUNT else None
    if isinstance(value, str) and value.isdigit() and len(value) <= 10:
        n = int(value)
        return n if n <= _MAX_COUNT else None
    return None


def _share_attr(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        d = Decimal(str(value).strip()[:32])
    except InvalidOperation:
        return None
    return d if d.is_finite() and 0 <= d <= 1 else None


def _evidence_ints(f: Finding, name: str, *, ref_prefix: str | None = None) -> list[int]:
    out = []
    for item in f.evidence:
        if ref_prefix is not None and not item.ref.startswith(ref_prefix):
            continue
        for key, value in item.attrs:
            if key == name:
                n = _int_attr(value)
                if n is not None:
                    out.append(n)
    return out


def team_editor_shares(teams: Mapping[str, Mapping[str, object]] | None) -> dict[
        str, tuple[Decimal, int, int | None]]:
    """Team → (JetBrains interaction share, interactions, people or None) from the ``teams``
    counts (see :func:`tokenbill.copilot.policy.team_counts`): editor keys are ``ide:<name>``,
    editor-family names, ``cli`` and ``app``; families through ``core.catalog.editor_family``.
    Teams without interactions are left out; malformed input → ``UsageError``."""
    if teams is None:
        return {}
    if not isinstance(teams, Mapping):
        raise UsageError("teams must map a team to its counts")
    out: dict[str, tuple[Decimal, int, int | None]] = {}
    for team, counts in teams.items():
        if not isinstance(team, str) or not team or not isinstance(counts, Mapping):
            raise UsageError("teams must map a team name to a counts mapping")
        total = jb = 0
        people: int | None = None
        for key, value in counts.items():
            if not isinstance(key, str):
                raise UsageError("team count keys must be strings")
            n = _int_attr(value)
            if n is None:
                raise UsageError("team counts must be non-negative integers")
            if key == "n_people":
                people = n
                continue
            if key in ("seats", "idle_seats"):
                continue
            family = _family(key)
            if family is None:
                continue
            total += n
            if family == "jetbrains":
                jb += n
        if total > 0:
            out[team] = (Decimal(jb) / Decimal(total), total, people)
    return dict(sorted(out.items()))


def _family(key: str) -> str | None:
    """The editor family of a team count key, None for keys that are not interaction counts."""
    if key == "cli":
        return "cli"
    if key == "app":
        return "copilot_app"
    if key.startswith("ide:") or key in catalog.EDITOR_FAMILIES:
        return catalog.editor_family(key)
    return None


def _jetbrains(findings: Sequence[Finding],
               teams: Mapping[str, Mapping[str, object]] | None) -> tuple[int, int]:
    """(teams with JetBrains usage, of them JetBrains-heavy) from the team counts and the
    ``editor-mix`` findings (one count per team; the team counts win)."""
    shares: dict[str, Decimal] = {
        team: share for team, (share, _, _) in team_editor_shares(teams).items()}
    for f in findings:
        if f.kind != "editor-mix":
            continue
        team = dict(f.scope.dims).get("team", "")
        if team in shares:
            continue
        for item in f.evidence:
            for key, value in item.attrs:
                if key == "jetbrains_share":
                    share = _share_attr(value)
                    if share is not None:
                        shares[team] = max(shares.get(team, Decimal(0)), share)
    with_jb = [s for s in shares.values() if s > 0]
    return len(with_jb), sum(1 for s in with_jb if s >= JETBRAINS_HEAVY_SHARE)


# ---------------------------------------------------------------------------------------------
# figures
# ---------------------------------------------------------------------------------------------


def lever_projection(plan: ActionPlan | None, lever_id: str) -> Figure | None:
    """The monthly projection of *lever_id* in *plan*: the sum of its billed-basis
    ``LeverResult.projected_monthly`` (several players of one lever add, their Shapley credits
    being additive); pool-headroom results (``LIST_EQUIVALENT``) are not savings (R11). None when
    the plan has no such result."""
    if plan is None:
        return None
    figs = [lv.projected_monthly for lv in plan.levers
            if lv.lever_id == lever_id and lv.projected_monthly.basis is not Basis.LIST_EQUIVALENT]
    if not figs:
        return None
    total = figs[0]
    for fig in figs[1:]:
        try:
            total = add(total, fig)
        except ContractViolation as exc:
            raise UsageError(f"lever {lever_id}: projections on different bases") from exc
    return total


def figure_text(fig: Figure) -> str:
    """``$390.00/month (estimated, list, range $0.00–$390.00, upper bound)``: the point, the
    label and the range; ``unpriced`` when the figure has no point (R2)."""
    label = [fig.evidence.value, fig.basis.value.replace("_", "-")]
    if fig.low_nano is not None and fig.high_nano is not None:
        label.append(f"range {fmt_usd(fig.low_nano)}–{fmt_usd(fig.high_nano)}")
    if fig.upper_bound:
        label.append("upper bound")
    if fig.nano is None:
        return f"unpriced ({', '.join(label)})"
    return f"{fmt_usd(fig.nano)}/month ({', '.join(label)})"


def scenario_projection_text(scenario_plans: Sequence[tuple[str, ActionPlan]],
                             lever_id: str) -> str | None:
    """``if Business: …; if Enterprise: …`` for *lever_id* over the scenario plans (never one
    number, R17); None when no scenario projects the lever."""
    parts = []
    any_fig = False
    for scenario, plan in scenario_plans:
        fig = lever_projection(plan, lever_id)
        any_fig = any_fig or fig is not None
        text = figure_text(fig) if fig is not None else "not projected"
        parts.append(f"if {scenario.capitalize()}: {text}")
    return "; ".join(parts) if any_fig else None


# ---------------------------------------------------------------------------------------------
# the checklist
# ---------------------------------------------------------------------------------------------


def _fit(text: str, limit: int = _WHAT_MAX) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:") + "…"


def _compose(template: str, *extras: str) -> str:
    """The catalog template followed by the extras that still fit (whole extras only; the first
    extra is cut at a word boundary when nothing else fits)."""
    out = template
    for extra in (e for e in extras if e):
        if len(out) + 1 + len(extra) <= _WHAT_MAX:
            out = f"{out} {extra}"
        elif out == template:
            out = _fit(f"{out} {extra}")
    return _fit(out)


def _next_first(day: _dt.date) -> str:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return f"{year:04d}-{month:02d}-01"


def _renewal_dates(config: Sequence[ConfigSnapshot]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in run_flags(config).items():
        if key.startswith("renewal_date.") and isinstance(value, str) and _DATE_RE.match(
                value[:10]):
            out[key[len("renewal_date."):]] = value[:10]
    return out


def _seat_deadline(pools: Sequence[PoolMonth], config: Sequence[ConfigSnapshot],
                   today: _dt.date) -> str | None:
    modes = {pm.entity_id: pm.billing_mode for pm in pools}
    if not modes:
        return None
    if any(m == "metered" for m in modes.values()):
        return _next_first(today)
    renewals = _renewal_dates(config)
    dates = sorted(renewals[e] for e in modes if e in renewals and renewals[e] >= today.isoformat())
    return dates[0] if dates else None


def _idle_cutoff(known: ActionPlan | None, scenario_plans: Sequence[tuple[str, ActionPlan]],
                 today: _dt.date) -> str:
    """``last_activity_at`` cut-off of the seat query: today − the plan's idle threshold (30 days
    unless the seat-reclaim player chose 60)."""
    days = 30
    plans = [known] if known is not None else [p for _, p in scenario_plans]
    for plan in plans:
        for lv in plan.levers:  # type: ignore[union-attr]
            if lv.lever_id == "copilot.seat_reclaim":
                m = _IDLE_RE.match(lv.params)
                if m:
                    days = max(days, int(m.group(1)))
    return (today - _dt.timedelta(days=days)).isoformat()


class _Collector:
    """Action id → the levers and finding kinds that reached it."""

    def __init__(self) -> None:
        self.levers: dict[str, set[str]] = {}
        self.kinds: dict[str, set[str]] = {}

    def add(self, action_id: str, lever_id: str | None = None, kind: str | None = None) -> None:
        if action_id not in catalog.ADMIN_ACTIONS:
            return
        self.levers.setdefault(action_id, set())
        self.kinds.setdefault(action_id, set())
        if lever_id is not None:
            self.levers[action_id].add(lever_id)
        if kind is not None:
            self.kinds[action_id].add(kind)

    def add_lever(self, lever_id: str, kind: str | None = None) -> None:
        try:
            lv = catalog.lever(lever_id)
        except UsageError:
            return
        if lever_id not in _LEVER_ORDER:
            return
        for key in lv.patch_keys:
            self.add(key, lever_id, kind)

    def drop(self, action_id: str) -> None:
        self.levers.pop(action_id, None)
        self.kinds.pop(action_id, None)


def _primary_lever(action_id: str, levers: set[str]) -> str | None:
    if levers:
        return min(levers, key=lambda lid: (_LEVER_ORDER.get(lid, 10**6), lid))
    return _KIND_ONLY_LEVER.get(action_id)


def admin_actions(plans_by_scenario: object, findings: Sequence[Finding],
                  pools: Sequence[PoolMonth], *, plans: Sequence[PlanEvidence], today: object,
                  teams: Mapping[str, Mapping[str, object]] | None = None,
                  config: Sequence[ConfigSnapshot] = ()) -> tuple[AdminAction, ...]:
    """The admin checklist items of the Copilot policy pack (addendum §11.3).

    *plans_by_scenario* are CP-PLAN's ``(key, ActionPlan)`` pairs (:func:`split_plans`),
    *findings* every finding of the run (only Copilot ones are read), *pools* the pool months
    (both scenarios while a plan is unknown), *plans* the plan evidence and *today* a
    ``YYYY-MM-DD`` date. Additive keywords: *teams* (team → counts, for the JetBrains-limits
    item) and *config* (``renewal_date.<entity>`` run flags for volume / azure deadlines).

    One item per ``ADMIN_ACTIONS`` id reached (module docstring); order: plan confirmation,
    JetBrains limits, then catalog order, the VS Code opt-in last. Malformed input →
    ``UsageError``."""
    known, scenario_plans = split_plans(plans_by_scenario)
    fs = sorted((f for f in _as_list(findings, Finding, "findings") if is_copilot_finding(f)),
                key=lambda f: f.finding_id)
    pool_list = _as_list(pools, PoolMonth, "pools")
    evidence = _as_list(plans, PlanEvidence, "plans")
    config_list = _as_list(config, ConfigSnapshot, "config")
    day = _today(today)
    unknown = plan_unknown(evidence, pool_list, scenario_plans, fs)
    confirm = unknown or any(pe.conflict for pe in evidence)
    metered = any(pm.billing_mode == "metered" for pm in pool_list)

    col = _Collector()
    for plan in ([known] if known is not None else [p for _, p in scenario_plans]):
        for lv in plan.levers:
            col.add_lever(lv.lever_id)
    for f in fs:
        linked = set(f.lever_ids) | {lv.lever_id for lv in catalog.levers_for_kind(
            f.kind, family="copilot")}
        for lever_id in sorted(linked):
            col.add_lever(lever_id, f.kind)
        for action_id in KIND_ACTIONS.get(f.kind, ()):
            col.add(action_id, None, f.kind)
    kinds = {f.kind for f in fs}
    if confirm:
        col.add(_PLAN_CONFIRM, None, "plan-status")
    elif _PLAN_CONFIRM in col.levers:
        col.drop(_PLAN_CONFIRM)          # plan-status of a known plan: nothing to confirm
    n_jb, n_heavy = _jetbrains(fs, teams)
    if n_jb:
        col.add(_MODEL_POLICY, "copilot.model_policy", "editor-mix")
        col.add(_AUTO_TIER, "copilot.auto_tier", "editor-mix")
    col.add(_OPTIN, "copilot.vscode_traces_optin")
    if unknown:
        for action_id in _PLAN_ADVICE:
            col.drop(action_id)
    if not metered:
        for action_id in _COST_CENTER_ITEMS - {"rest:cost_center_create"}:
            col.drop(action_id)
    if "unattributed-spend" not in kinds:
        col.drop("rest:cost_center_create")

    seat_deadline = _seat_deadline(pool_list, config_list, day)
    review_date = _facts.copilot_dates().get("review_default_balanced")
    cutoff = _idle_cutoff(known, scenario_plans, day)
    removable = sum(sum(_evidence_ints(f, "n", ref_prefix="assignment:removable"))
                    for f in fs if f.kind == "idle-seat")
    suggested_cap = max((n for f in fs if f.kind == "agentic-workflow-cost"
                         for n in _evidence_ints(f, "suggested_max_ai_credits")), default=None)
    ci_cap = max((n for f in fs if f.kind == "ci-uncapped"
                  for n in _evidence_ints(f, "suggested_max_ai_credits")), default=None)

    shown_levers: set[str] = set()
    out: list[AdminAction] = []
    order = [_PLAN_CONFIRM] + [a for a in catalog.ADMIN_ACTIONS if a not in (_PLAN_CONFIRM, _OPTIN)]
    order.append(_OPTIN)
    for action_id in order:
        if action_id not in col.levers:
            continue
        spec = catalog.ADMIN_ACTIONS[action_id]
        lever_id = _primary_lever(action_id, col.levers[action_id])
        lv = catalog.lever(lever_id) if lever_id is not None else None
        projected = lv is not None and lv.lever_class != "behavioral" and lever_id not in \
            shown_levers
        projection: Figure | None = None
        extras: list[str] = []
        if projected and lever_id is not None:
            if known is not None:
                projection = lever_projection(known, lever_id)
            elif scenario_plans:
                text = scenario_projection_text(scenario_plans, lever_id)
                if text is not None:
                    extras.append(f"Projection per scenario (plan unknown): {text}.")
            if projection is not None or extras:
                shown_levers.add(lever_id)
        deadline = None
        if action_id in SEAT_ACTIONS:
            deadline = seat_deadline
        elif (action_id == _REVIEW_LITE and review_date is not None
              and day.isoformat() < review_date):
            deadline = review_date
        if action_id == _PLAN_CONFIRM:
            n_unknown = len({pe.entity_id for pe in evidence if pe.plan == "unknown"})
            n_conflict = len({pe.entity_id for pe in evidence if pe.conflict})
            extras.insert(0, f"Plan status: {n_unknown} entities unknown, {n_conflict} with "
                             "conflicting evidence; until confirmed every pool figure is shown "
                             "for both plans.")
        elif action_id == _SEAT_DELETE:
            extras.insert(0, f"Cut-off: last_activity_at before {cutoff}; {removable} removable "
                             "seats counted (counts only, never a list).")
        elif action_id == _AW_TRIGGERS and suggested_cap is not None:
            extras.insert(0, f"Suggested max-ai-credits: {suggested_cap} (p99 x 1.5 of priced "
                             "runs).")
        elif action_id == _CI_LIMITS and ci_cap is not None:
            extras.insert(0, f"Suggested --max-ai-credits: {max(30, ci_cap)}.")
        elif action_id == _MODEL_POLICY and n_jb:
            extras.insert(0, "Recommended for teams with JetBrains usage (managed model does not "
                             "reach JetBrains).")
        reach = "1" if action_id in REACH_ONE else None
        out.append(AdminAction(
            action_id=action_id, lever_id=lever_id, admin_action=action_id, where=spec.where,
            what=_compose(spec.template, *extras), doc_url=spec.doc_url,
            rest_file=REQUESTS_FILE if spec.rest_method is not None else None,
            auth_note=spec.auth_note, reach=reach, projection=projection, deadline=deadline,
            needs_eval=bool(lv.needs_eval) if lv is not None else False,
            tradeoff=bool(lv.tradeoff) if lv is not None else False))
    if n_jb:
        policy = catalog.lever("copilot.model_policy")
        jb_item = AdminAction(
            action_id=JETBRAINS_ACTION_ID, lever_id="copilot.model_policy",
            admin_action=_MODEL_POLICY, where="enterprise settings",
            what=_fit(
                f"JetBrains limits: {n_jb} teams use JetBrains ({n_heavy} at 50% or more of their "
                "interactions). Managed \"model\" is not applied in JetBrains and managed "
                "telemetry for JetBrains is documented inconsistently, so the managed-settings "
                "patch does not reach these users: use the server-side model policy (reach 1) and "
                "communicate Auto tier guidance instead."),
            doc_url=catalog.copilot_allowed("copilot.managed.model").source, rest_file=None,
            auth_note=catalog.ADMIN_ACTIONS[_MODEL_POLICY].auth_note, reach="1", projection=None,
            deadline=None, needs_eval=policy.needs_eval, tradeoff=policy.tradeoff)
        out.insert(1 if out and out[0].action_id == _PLAN_CONFIRM else 0, jb_item)
    return tuple(out)

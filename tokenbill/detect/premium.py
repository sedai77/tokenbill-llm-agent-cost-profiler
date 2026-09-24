"""Premium detectors (SPEC §10.2 ``premium.modifiers`` and ``premium.sticky-escalation``;
package DETECT-OTHER).

* :class:`PremiumModifiers` — the price modifiers a cohort paid on identical tokens: fast mode,
  US data residency (``inference_geo``), regional endpoints and the priority/fast service tiers.
  Each premium is ``cost − cost at standard/global/standard tier`` on the same billed tokens —
  pure rate arithmetic, EXACT — and so is its recoverable (lever class ``rate``). A premium that
  a data-residency policy requires (``ctx.thresholds["policy.residency_required[.<team>]"]``) is
  flagged, not offered as a saving.
* :class:`StickyEscalation` — principals whose Claude Code sessions stay escalated: fast mode, or a
  session effort above the org default (``ctx.thresholds["defaults.effort"]``, default
  ``medium``), on more than 5 distinct days. Self view: the principal's own finding. Org view:
  the count of such principals per team only when it reaches ``k`` — never who.

See ``detect.context`` for the shared conventions (cohorts, allowance labeling, ``min_usd``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence

from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Figure
from tokenbill.core.records import Inference, Lane, LaneKind, PricingContext
from tokenbill.core.types import AnalysisContext, Finding, Fix
from tokenbill.detect.context import (
    DAY_MS,
    DETECTOR_VERSION,
    EFFORT_RANK,
    Cohort,
    Emit,
    Money,
    Prices,
    Tally,
    applicable_levers,
    cc_gates,
    cohorts,
    combine,
    emit,
    evidence_item,
    int_threshold,
    is_claude_code,
    patch,
    settings_doc,
    sort_findings,
)

__all__ = ["PREMIUMS", "Premium", "PremiumModifiers", "StickyEscalation", "default_effort",
           "premium_of", "residency_required"]

_PREMIUM_REFS = ("anth-modifiers-geo-fast-priority", "premium-modifiers", "oai-service-tiers")
_STICKY_REFS = ("cc-sticky-escalation",)
_PRICING_DOC = "https://platform.claude.com/docs/en/about-claude/pricing"


def _no_fast(ctx: PricingContext) -> PricingContext:
    return dataclasses.replace(ctx, speed="standard")


def _no_geo(ctx: PricingContext) -> PricingContext:
    return dataclasses.replace(ctx, inference_geo=None)


def _no_regional(ctx: PricingContext) -> PricingContext:
    return dataclasses.replace(ctx, endpoint_scope="global")


def _no_tier(ctx: PricingContext) -> PricingContext:
    return dataclasses.replace(ctx, service_tier="standard")


@dataclasses.dataclass(frozen=True)
class Premium:
    """One premium kind: when it applies, the counterfactual context, its lever spec and text."""

    kind: str
    applies: Callable[[PricingContext], bool]
    counterfactual: Callable[[PricingContext], PricingContext]
    policy: str | None
    what: str
    residency: bool
    fix_cc: str
    fix_api: str
    fix_target: str


#: The premium kinds (a table: an additive modifier is one more row).
PREMIUMS: tuple[Premium, ...] = (
    Premium("fast-premium", lambda c: c.speed == "fast", _no_fast, "fast=off", "fast mode",
            False,
            "Make fast mode a per-session opt-in: fastModePerSessionOptIn (or "
            "CLAUDE_CODE_DISABLE_FAST_MODE=1 where it is never needed).",
            "Send speed standard unless the latency is worth the fast-mode price.", "code"),
    Premium("geo-premium", lambda c: c.inference_geo == "us", _no_geo, "geo=global",
            "US-only inference (inference_geo us)", True,
            "Use the global inference_geo where the data-residency policy allows it.",
            "Use the global inference_geo where the data-residency policy allows it.", "code"),
    Premium("regional-premium", lambda c: c.endpoint_scope in ("regional", "multi_region"),
            _no_regional, "regional=global", "regional endpoints", True,
            "Route through global endpoints (e.g. global. inference profiles) where policy "
            "allows.",
            "Route through global endpoints (e.g. global. inference profiles) where policy "
            "allows.", "gateway"),
    Premium("tier-premium", lambda c: c.service_tier in ("priority", "fast"), _no_tier, None,
            "the priority service tier", False,
            "Reserve the priority tier for latency-critical traffic.",
            "Reserve the priority tier for latency-critical traffic.", "code"),
)
_TRUE = frozenset({"1", "true", "yes", "on"})


def residency_required(ctx: AnalysisContext, team: str | None) -> bool:
    """Whether a data-residency policy covers *team*: ``ctx.thresholds["policy.residency_
    required.<team>"]`` or the org-wide ``"policy.residency_required"`` is ``1``/``true`` (read
    raw: a policy value, not a decimal)."""
    if not ctx.thresholds:
        return False
    keys = ["policy.residency_required"]
    if team:
        keys.insert(0, f"policy.residency_required.{team}")
    for key in keys:
        value = ctx.thresholds.get(key)
        if value is not None:
            return str(value).strip().lower() in _TRUE
    return False


def premium_of(prices: Prices, inf: Inference, ts_ms: int, premium: Premium) -> Money | None:
    """``cost(inf) − cost(inf on premium.counterfactual(context))`` on identical tokens (None
    when either side is unpriceable)."""
    billed = prices.usage(inf.usage, inf.pricing, ts_ms, billable=inf.billable,
                          source=inf.usage_source, upper=inf.output_upper)
    standard = prices.usage(inf.usage, premium.counterfactual(inf.pricing), ts_ms,
                            billable=inf.billable, source=inf.usage_source,
                            upper=inf.output_upper)
    return combine((1, billed), (-1, standard))


class PremiumModifiers:
    """``premium.modifiers`` (SPEC §10.2): per cohort and premium kind of :data:`PREMIUMS`, every
    billable inference whose pricing context carries the modifier (fast speed, ``inference_geo``
    ``us``, a regional or multi-region endpoint, the priority/fast service tier) is priced as
    billed and again on the counterfactual context on identical tokens. ``cost_observed`` =
    ``recoverable`` = the difference — EXACT (a range only when a line is a range) — lever class
    ``rate`` (``fast=off``, ``geo=global``, ``regional=global``). With a data-residency policy
    covering the team, geo and regional premiums carry no recoverable and say so. Needs only
    ``usage_sequence``.
    """

    id = "premium.modifiers"
    version = DETECTOR_VERSION
    kinds = tuple(p.kind for p in PREMIUMS)
    requires = frozenset({"usage_sequence"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """At most one finding per premium kind and cohort at or above ``min_usd``."""
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            for premium in PREMIUMS:
                found = self._premium(ctx, prices, cohort, premium)
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    def _premium(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort,
                 premium: Premium) -> Finding | None:
        tally = Tally()
        per_model: dict[str, int] = {}
        for lane in cohort.lanes:
            for req in lane.requests:
                for att in req.attempts:
                    for inf in att.inferences:
                        if inf.billable is False or not premium.applies(inf.pricing):
                            continue
                        tally.hit(lane, att.ts_start_ms)
                        money = premium_of(prices, inf, att.ts_start_ms, premium)
                        if money is None:
                            tally.unpriced += 1
                            continue
                        tally.cost.add_money(money)
                        per_model[inf.pricing.model] = per_model.get(inf.pricing.model, 0) + \
                            money.point
        if tally.events == tally.unpriced or tally.cost.point <= 0:
            return None
        basis = cohort.basis(ctx.pricer)
        cost = tally.cost.billed(basis)
        required = premium.residency and residency_required(ctx, cohort.team)
        recoverable: Figure | None = None if required else cost
        items = [evidence_item("aggregate", f"premium:{model}", nano=nano)
                 for model, nano in sorted(per_model.items())]
        lanes = tally.lane_list()
        claude_code = any(is_claude_code(lane) for lane in lanes)
        fix = self._fix(premium, claude_code)
        note = (" A data-residency policy covers this team: the premium is the price of "
                "compliance, not a saving.") if required else ""
        spec = Emit(
            kind=premium.kind, category="premium", lever_class="rate",
            title=f"Premium for {premium.what} in {cohort.label()} lanes",
            summary=(f"{tally.events} inferences in {cohort.label()} lanes were billed with "
                     f"{premium.what}; the premium over the same tokens at the standard price "
                     f"is exact rate arithmetic."),
            references=_PREMIUM_REFS, fix=fix, confidence="high", note=note,
            lever_ids=() if required else applicable_levers(premium.kind, lanes))
        return emit(self, ctx, cohort, tally, spec, cost, recoverable, items)

    @staticmethod
    def _fix(premium: Premium, claude_code: bool) -> Fix:
        if premium.kind == "fast-premium" and claude_code:
            return Fix(text=premium.fix_cc,
                       config_patch=patch(("fastModePerSessionOptIn", "true")),
                       target="claude-code-managed-settings",
                       doc_url=settings_doc("fastModePerSessionOptIn"),
                       gates=cc_gates("fastModePerSessionOptIn"))
        return Fix(text=premium.fix_cc if claude_code else premium.fix_api, config_patch=None,
                   target=premium.fix_target, doc_url=_PRICING_DOC)


# =============================================================================================
# premium.sticky-escalation
# =============================================================================================

_DEFAULT_EFFORT = "medium"


def default_effort(ctx: AnalysisContext) -> str:
    """The org default effort: ``ctx.thresholds["defaults.effort"]`` (a level, read raw), else
    ``medium`` (the Claude Code default on Opus 5.5, SPEC §19.1). An unknown level raises
    ``UsageError``."""
    raw = ctx.thresholds.get("defaults.effort") if ctx.thresholds else None
    level = str(raw).strip().lower() if raw is not None else _DEFAULT_EFFORT
    if level not in EFFORT_RANK:
        raise UsageError("threshold defaults.effort: not an effort level")
    return level


@dataclasses.dataclass
class _Principal:
    """One principal's escalation days and money inside a cohort."""

    fast_days: set[int] = dataclasses.field(default_factory=set)
    effort_days: set[int] = dataclasses.field(default_factory=set)
    fast_premium: Money = dataclasses.field(default_factory=Money)
    effort_spend: Money = dataclasses.field(default_factory=Money)
    lanes: dict[str, Lane] = dataclasses.field(default_factory=dict)
    first_seen: int | None = None
    unpriced: int = 0


class StickyEscalation:
    """``premium.sticky-escalation`` (SPEC §10.2): on MAIN lanes, a principal is sticky when fast
    mode was on, or the Claude Code session effort ran above the org default, on more than
    ``premium.sticky-escalation.min_days`` (default 5) distinct UTC days.

    * ``sticky-escalation`` (self view only, ``ctx.self_principal``): the principal's own fast
      premium (EXACT rate arithmetic) when fast mode is sticky, else the spend of the requests run
      at the escalated session effort (EXACT; the premium over the default effort is not
      observable).
    * ``sticky-escalation-count`` (org view): the number of sticky principals per cohort, only
      when it reaches ``ctx.k_anonymity``; ``cost_observed`` sums their figures. Counts only.

    No recoverable (behavioral). Needs ``params``.
    """

    id = "premium.sticky-escalation"
    version = DETECTOR_VERSION
    kinds = ("sticky-escalation", "sticky-escalation-count")
    requires = frozenset({"params"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Self: at most one finding per MAIN cohort of the self principal; org: at most one
        count per MAIN cohort."""
        prices = Prices(ctx.pricer)
        default = default_effort(ctx)
        min_days = int_threshold(ctx, f"{self.id}.min_days", 5)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            if cohort.lane_kind != LaneKind.MAIN.value:
                continue
            people = self._principals(prices, cohort, EFFORT_RANK[default])
            sticky = {p: rec for p, rec in people.items()
                      if len(rec.fast_days) > min_days or len(rec.effort_days) > min_days}
            if ctx.self_principal is not None:
                rec = sticky.get(ctx.self_principal)
                if rec is not None:
                    found = self._self(ctx, cohort, rec, min_days, default)
                    if found is not None:
                        out.append(found)
            elif len(sticky) >= ctx.k_anonymity:
                found = self._org(ctx, cohort, sticky, min_days, default)
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    @staticmethod
    def _principals(prices: Prices, cohort: Cohort, rank: int) -> dict[str, _Principal]:
        fast = next(p for p in PREMIUMS if p.kind == "fast-premium")
        people: dict[str, _Principal] = {}
        for lane in cohort.lanes:
            for req in lane.requests:
                principal = req.attribution.principal
                inf = req.serving_inference
                if principal is None or inf is None:
                    continue
                day = req.ts_start_ms // DAY_MS
                rec = people.setdefault(principal, _Principal())
                is_fast = inf.pricing.speed == "fast" or req.params.speed == "fast"
                effort = req.params.session_effort
                escalated = effort is not None and EFFORT_RANK.get(effort, -1) > rank
                if not (is_fast or escalated):
                    continue
                rec.lanes[lane.lane_key] = lane
                rec.first_seen = req.ts_start_ms if rec.first_seen is None else \
                    min(rec.first_seen, req.ts_start_ms)
                if is_fast:
                    rec.fast_days.add(day)
                    for att in req.attempts:
                        for billed in att.inferences:
                            if billed.billable is False or not fast.applies(billed.pricing):
                                continue
                            money = premium_of(prices, billed, att.ts_start_ms, fast)
                            if money is None:
                                rec.unpriced += 1
                            else:
                                rec.fast_premium.add_money(money)
                if escalated:
                    rec.effort_days.add(day)
                    money = prices.request(req)
                    if money is None:
                        rec.unpriced += 1
                    else:
                        rec.effort_spend.add_money(money)
        return people

    @staticmethod
    def _figure(rec: _Principal, min_days: int) -> tuple[Money, str]:
        if len(rec.fast_days) > min_days:
            return rec.fast_premium, "fast"
        return rec.effort_spend, "effort"

    def _tally(self, recs: Mapping[str, _Principal], min_days: int) -> tuple[Tally, dict[str, int]]:
        tally = Tally()
        counts = {"fast": 0, "effort": 0}
        for principal, rec in sorted(recs.items()):
            money, why = self._figure(rec, min_days)
            counts[why] += 1
            tally.cost.add_money(money)
            tally.unpriced += rec.unpriced
            tally.events += len(rec.fast_days | rec.effort_days)
            for lane in rec.lanes.values():
                tally.touch(lane, rec.first_seen)
            tally.principals.add(principal)
        return tally, counts

    def _fix(self, fast: bool) -> Fix:
        text = ("Keep escalation session-only: raise /effort for one session without saving it as "
                "the default, and make fast mode a per-session opt-in "
                "(fastModePerSessionOptIn).")
        if fast:
            return Fix(text=text, config_patch=patch(("fastModePerSessionOptIn", "true")),
                       target="claude-code-managed-settings",
                       doc_url=settings_doc("fastModePerSessionOptIn"),
                       gates=cc_gates("fastModePerSessionOptIn"))
        return Fix(text=text, config_patch=None, target="claude-code-managed-settings",
                   doc_url=settings_doc("effortLevel"))

    def _self(self, ctx: AnalysisContext, cohort: Cohort, rec: _Principal, min_days: int,
              default: str) -> Finding | None:
        assert ctx.self_principal is not None
        tally, counts = self._tally({ctx.self_principal: rec}, min_days)
        basis = cohort.basis(ctx.pricer)
        money, why = self._figure(rec, min_days)
        what = ("fast mode" if why == "fast" else f"a session effort above {default}")
        item = evidence_item("aggregate", "sticky:days", fast_days=len(rec.fast_days),
                             effort_days=len(rec.effort_days), min_days=min_days,
                             nano=money.point)
        spec = Emit(
            kind="sticky-escalation", category="premium", lever_class="behavioral",
            title=f"Your sessions stay escalated ({'fast mode' if why == 'fast' else 'effort'})",
            summary=(f"You ran {what} on {max(len(rec.fast_days), len(rec.effort_days))} days "
                     f"(more than {min_days}): "
                     + ("your fast-mode premium is exact rate arithmetic." if why == "fast" else
                        "the spend of those requests is shown; the premium over the default "
                        "effort is not observable.")
                     + " Escalate per session instead."),
            references=_STICKY_REFS, fix=self._fix(why == "fast"), confidence="high",
            n_users=1)
        return emit(self, ctx, cohort, tally, spec, money.billed(basis), None, [item])

    def _org(self, ctx: AnalysisContext, cohort: Cohort, sticky: Mapping[str, _Principal],
             min_days: int, default: str) -> Finding | None:
        tally, counts = self._tally(sticky, min_days)
        basis = cohort.basis(ctx.pricer)
        item = evidence_item("aggregate", "sticky:count", principals=len(sticky),
                             fast=counts["fast"], effort=counts["effort"], min_days=min_days,
                             nano=tally.cost.point)
        spec = Emit(
            kind="sticky-escalation-count", category="premium", lever_class="behavioral",
            title=f"{len(sticky)} developers keep sessions escalated in {cohort.label()} lanes",
            summary=(f"{len(sticky)} principals ran fast mode or a session effort above {default} "
                     f"on more than {min_days} days ({counts['fast']} fast, {counts['effort']} "
                     f"effort; counts only): their fast premium or escalated spend."),
            references=_STICKY_REFS, fix=self._fix(counts["fast"] > 0), confidence="medium",
            n_users=len(sticky))
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), None, [item])

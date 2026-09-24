"""Model-routing detector (SPEC §10.2 ``model.routing``, D34; package DETECT-OTHER).

:class:`Routing` emits six kinds per cohort:

* ``delegation-routing`` — subagent / workflow-agent lanes on Opus/Fable/Mythos-class models,
  replayed on Sonnet 5 (``model=claude-sonnet-5@lane_kind:<kind>``, the tokenizer band applied by
  the replayer; Explore agents also on Haiku 4.5, in the evidence, with its retirement date).
* ``same-tier-upgrade`` — lanes on a model with a ``core.catalog.successor`` (same tier, same
  tokenizer): rate arithmetic on identical tokens, exact to the nano but labeled ESTIMATED
  ("behavior unvalidated"). Never a cross-family move (no Opus 4.8 → Opus 5.5).
* ``default-model`` — Claude Code MAIN lanes where Opus/Fable/Mythos-class models serve at least
  half of the cohort's main spend: ``model=claude-sonnet-5@agent_product:claude_code,
  lane_kind:main`` (the ``cc.default_model`` lever).
* ``default-effort`` (needs ``params``) — Claude Code MAIN lanes where at least half of the main
  output ran at effort ≥ high on an effort-capable model (one that reports an effort):
  ``effort=medium,scale=0.5@agent_product:claude_code,lane_kind:main`` (``cc.default_effort``).
* ``effort-mix`` (info, needs ``params``) — spend share by effort level and the thinking share.
* ``rebaseline`` (info) — a change of the cohort's dominant serving model: the 14 days before vs
  after (≥ 50 requests each), Δ tokens per request and Δ$ per request at the rate card's rates
  (observational, ESTIMATED), the tokenizer-family and thinking-default notes and the
  stale-prompt flag at +20% output per call (``STALE_PROMPT_OUTPUT_DELTA``).

Replay-based kinds (delegation, default model, default effort) are trade-offs: ESTIMATED,
``upper_bound``, ``needs_eval`` and price-only; without a replayer they are not emitted. See
``detect.context`` for the shared conventions.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction

from tokenbill.core import catalog
from tokenbill.core.errors import TokenbillError
from tokenbill.core.evidence import STALE_PROMPT_OUTPUT_DELTA, TOKENIZER_BAND
from tokenbill.core.labels import Figure, estimated, unpriced
from tokenbill.core.policy import parse_policy, to_spec
from tokenbill.core.records import Lane, LaneKind, PricingContext, Request, UsageBuckets
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding, Fix, Policy
from tokenbill.detect.context import (
    DAY_MS,
    DETECTOR_VERSION,
    EFFORT_RANK,
    MISSING_KIND,
    Cohort,
    Emit,
    Money,
    Prices,
    Tally,
    applicable_levers,
    capability_notes,
    cc_gates,
    cohorts,
    decimal_str,
    emit,
    evidence_item,
    int_threshold,
    is_claude_code,
    lane_model,
    lever_spec,
    model_family,
    patch,
    pct,
    premium_family,
    replay,
    round_fraction,
    saving_figure,
    serving,
    serving_steps,
    settings_doc,
    share_threshold,
    sort_findings,
)

__all__ = ["THINKING_ON_BY_DEFAULT", "Routing"]

_REFS_DELEGATION = ("cc-delegation-model-routing", "anth-tokenizer-inflation")
_REFS_SAME_TIER = ("cc-same-tier-upgrade",)
_REFS_DEFAULTS = ("org-defaults-datadog", "datadog-1m-month-case", "cc-model-effort-mix")
_REFS_EFFORT_MIX = ("cc-model-effort-mix",)
_REFS_REBASELINE = ("anth-tokenizer-inflation", "anth-prompt-audit-migration",
                    "opus5-thinking-effort-rebaseline", "tokenizer-inflation-47plus")
_DELEGATION_KINDS = frozenset({LaneKind.SUBAGENT.value, LaneKind.WORKFLOW_AGENT.value})
_EXPLORE = "Explore"
#: Models that think by default (``core/facts.json`` rate-row notes; SPEC §10.2 rebaseline fix).
THINKING_ON_BY_DEFAULT = frozenset({"claude-opus-5"})
#: Days of the rebaseline comparison windows and of the "within 7 days" move (SPEC §10.2).
_COMPARE_DAYS = 14
_MOVE_DAYS = 7
_RETIREMENT_HORIZON_DAYS = 365
_ENV_DEFAULT_MODEL = {"opus": "env.ANTHROPIC_DEFAULT_OPUS_MODEL",
                      "sonnet": "env.ANTHROPIC_DEFAULT_SONNET_MODEL",
                      "haiku": "env.ANTHROPIC_DEFAULT_HAIKU_MODEL"}
_PRICE_ONLY = "price-only: quality and trajectory changes are not modeled"


def _remap_targets(lever_id: str) -> tuple[str, ...]:
    """The target models of a catalog lever's grid, in grid order."""
    out: list[str] = []
    for spec in catalog.lever(lever_id).grid:
        for _, target in parse_policy(spec).model_remap:
            if target not in out:
                out.append(target)
    return tuple(out)


def _now(ctx: AnalysisContext) -> int:
    return ctx.now_ms or ctx.window[1]


def _retirement_note(model: str, ctx: AnalysisContext) -> str:
    date = catalog.retiring_within(model, _now(ctx), _RETIREMENT_HORIZON_DAYS)
    return f" {model} retires not sooner than {date}." if date else ""


def _date(ts_ms: int) -> str:
    return (_dt.date(1970, 1, 1) + _dt.timedelta(days=ts_ms // DAY_MS)).isoformat()


@dataclasses.dataclass
class _Window:
    """Means of one rebaseline comparison window."""

    n: int = 0
    total_input: int = 0
    output: int = 0
    reasoning: int = 0
    reasoning_n: int = 0
    cost: int = 0
    priced: int = 0


class Routing:
    """``model.routing`` (SPEC §10.2, D34). See the module docstring for the six kinds.

    ``cost_observed`` is the spend of the affected lanes (EXACT) for delegation, same-tier and
    the defaults; the cohort spend for ``effort-mix``; the observational Δ$ over the after window
    (``(mean $ after − mean $ before) × requests after``, ESTIMATED, signed) for ``rebaseline``.
    Thresholds: ``model.routing.default_model_share`` (0.5), ``model.routing.default_effort_share``
    (0.5), ``model.routing.min_requests`` (50, each side of a rebaseline), ``model.routing.
    move_share`` (0.5).
    """

    id = "model.routing"
    version = DETECTOR_VERSION
    kinds = ("delegation-routing", "same-tier-upgrade", "default-model", "default-effort",
             "effort-mix", "rebaseline", MISSING_KIND)
    requires = frozenset({"usage_sequence"})
    kind_requires: Mapping[str, frozenset[str]] = {"default-effort": frozenset({"params"}),
                                                   "effort-mix": frozenset({"params"})}

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Every kind per cohort, each at or above ``min_usd``."""
        if not lanes:
            return capability_notes(self, ctx, self.kind_requires)
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            found: list[Finding | None] = []
            if cohort.lane_kind in _DELEGATION_KINDS:
                found.append(self._delegation(ctx, prices, cohort))
            found.extend(self._same_tier(ctx, prices, cohort))
            if cohort.lane_kind == LaneKind.MAIN.value:
                found.append(self._default_model(ctx, prices, cohort))
                found.append(self._default_effort(ctx, prices, cohort))
            found.append(self._effort_mix(ctx, prices, cohort))
            found.append(self._rebaseline(ctx, prices, cohort))
            out.extend(f for f in found if f is not None)
        return sort_findings(out)

    # ---------- shared ----------

    @staticmethod
    def _spend(prices: Prices, lanes: Sequence[Lane], tally: Tally) -> Money:
        total = Money()
        for lane in lanes:
            for req in lane.requests:
                money = prices.request(req)
                tally.hit(lane, req.ts_start_ms)
                if money is None:
                    tally.unpriced += 1
                    continue
                total.add_money(money)
        return total

    def _replayed(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort,
                  lanes: Sequence[Lane], spec: str, emit_spec: Emit,
                  extra: Sequence[EvidenceItem] = ()) -> Finding | None:
        targets = [lane for lane in lanes if prices.priceable(lane)]
        if not targets or ctx.replayer is None:
            return None
        result = replay(ctx, targets, spec)
        basis = cohort.basis(ctx.pricer)
        recoverable = saving_figure(result, basis, upper_bound=True, note=_PRICE_ONLY)
        if recoverable is None:
            return None
        tally = Tally()
        cost = self._spend(prices, targets, tally).billed(basis)
        dropped = len(lanes) - len(targets)
        note = (f" {dropped} lanes with an unpriced model were left out of the replay."
                if dropped else "")
        items = [evidence_item("aggregate", "replay:policy", policy=to_spec(parse_policy(spec)),
                               lanes=len(targets),
                               saving_nano=recoverable.nano if recoverable.nano is not None
                               else None), *extra]
        return emit(self, ctx, cohort, tally, dataclasses.replace(emit_spec, note=note), cost,
                    recoverable, items)

    # ---------- delegation-routing ----------

    def _delegation(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        affected = [lane for lane in cohort.lanes if premium_family(lane_model(lane))]
        if not affected:
            return None
        targets = _remap_targets("cc.subagent_model")
        main_target = targets[0]
        spec = to_spec(Policy(name="", model_remap=((f"lane_kind:{cohort.lane_kind}",
                                                     main_target),)))
        extra: list[EvidenceItem] = []
        explore = [lane for lane in affected
                   if lane.requests[0].attribution.agent_type == _EXPLORE]
        explore_note = ""
        if explore and len(targets) > 1 and ctx.replayer is not None:
            alt = targets[1]
            alt_spec = to_spec(Policy(name="", model_remap=((
                f"agent_type:{_EXPLORE},lane_kind:{cohort.lane_kind}", alt),)))
            result = replay(ctx, [lane for lane in explore if prices.priceable(lane)], alt_spec)
            retires = catalog.retiring_within(alt, _now(ctx), _RETIREMENT_HORIZON_DAYS)
            if result is not None:
                extra.append(evidence_item("aggregate", f"explore:{alt}", lanes=len(explore),
                                           saving_nano=result.saving.nano,
                                           retires=retires))
            explore_note = f" Explore agents can also run on {alt}." + (
                f" {alt} retires not sooner than {retires}." if retires else "")
        claude_code = any(is_claude_code(lane) for lane in affected)
        text = (f"Route {cohort.lane_kind} agents to {main_target}: CLAUDE_CODE_SUBAGENT_MODEL or "
                f"a per-agent model: field. A trade-off (quality): validate with ab / measure."
                f"{explore_note}")
        if claude_code:
            fix = Fix(text=text,
                      config_patch=patch(("env.CLAUDE_CODE_SUBAGENT_MODEL", f'"{main_target}"')),
                      target="claude-code-managed-settings",
                      doc_url=settings_doc("env.CLAUDE_CODE_SUBAGENT_MODEL"),
                      gates=cc_gates("env.CLAUDE_CODE_SUBAGENT_MODEL"))
        else:
            fix = Fix(text=text, config_patch=None, target="sdk", doc_url=None)
        emit_spec = Emit(
            kind="delegation-routing", category="lever", lever_class="trajectory",
            title=f"{cohort.label()} agents run on top-tier models",
            summary=(f"{len(affected)} {cohort.lane_kind} lanes ran on Opus/Fable/Mythos-class "
                     f"models; replayed on {main_target} (price-only, tokenizer band applied, "
                     f"upper bound; needs an eval)."),
            references=_REFS_DELEGATION, fix=fix, needs_eval=True, confidence="medium",
            lever_ids=applicable_levers("delegation-routing", affected))
        return self._replayed(ctx, prices, cohort, affected, spec, emit_spec, extra)

    # ---------- same-tier-upgrade ----------

    def _same_tier(self, ctx: AnalysisContext, prices: Prices,
                   cohort: Cohort) -> list[Finding | None]:
        groups: dict[str, list[Lane]] = {}
        for lane in cohort.lanes:
            model = lane_model(lane)
            if catalog.successor(model) is not None:
                groups.setdefault(model, []).append(lane)
        return [self._upgrade(ctx, prices, cohort, model, members)
                for model, members in sorted(groups.items())]

    @staticmethod
    def _successor_usage(prices: Prices, usage: UsageBuckets, target: PricingContext,
                         ts: int) -> UsageBuckets:
        """The usage under the successor: identical tokens, except that a prompt below the
        target's minimum cacheable prefix is sent uncached (the replay's min-prefix gate)."""
        try:
            minimum = prices.pricer.min_cacheable_tokens(target, ts_ms=ts) or 0
        except TokenbillError:
            minimum = 0
        cached = usage.cache_read + usage.cache_write
        if not cached or usage.total_input >= minimum:
            return usage
        return dataclasses.replace(usage, uncached_input=usage.total_input, cache_read=0,
                                   cache_write_5m=0, cache_write_1h=0, cache_write_other=0,
                                   cache_write_other_ttl_s=None, cache_write_unknown=0)

    def _upgrade(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort, model: str,
                 lanes: Sequence[Lane]) -> Finding | None:
        succ = catalog.successor(model)
        assert succ is not None
        tally = Tally()
        cost = Money()
        saving: Money | None = Money()
        for lane in lanes:
            for req in lane.requests:
                tally.hit(lane, req.ts_start_ms)
                req_cost = prices.request(req)
                if req_cost is None:
                    tally.unpriced += 1
                    continue
                cost.add_money(req_cost)
                for att in req.attempts:
                    for inf in att.inferences:
                        if inf.billable is False or saving is None:
                            continue
                        billed = prices.inference(inf, att.ts_start_ms)
                        target = dataclasses.replace(inf.pricing, model=succ, model_raw=succ)
                        usage = self._successor_usage(prices, inf.usage, target, att.ts_start_ms)
                        moved = prices.usage(usage, target, att.ts_start_ms,
                                             billable=inf.billable, source=inf.usage_source,
                                             upper=inf.output_upper)
                        if billed is None or moved is None:
                            saving = None
                            continue
                        saving.add_money(billed)
                        saving.add_money(moved, -1)
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        recoverable: Figure
        if saving is None:
            recoverable = unpriced(f"{succ} is not priced on this channel", basis)
        else:
            recoverable = saving.estimate(basis, "rate arithmetic exact on identical tokens; "
                                                 "behavior unvalidated")
        family = model_family(model) or ""
        key = _ENV_DEFAULT_MODEL.get(family)
        claude_code = any(is_claude_code(lane) for lane in lanes)
        text = (f"Pin the successor id {succ} (same tier, same tokenizer) where {model} is "
                f"configured; validate behavior with ab before switching."
                f"{_retirement_note(model, ctx)}")
        if claude_code and key is not None:
            fix = Fix(text=text, config_patch=patch((key, f'"{succ}"')),
                      target="claude-code-managed-settings", doc_url=settings_doc(key),
                      gates=cc_gates(key))
        else:
            fix = Fix(text=text, config_patch=None, target="code", doc_url=None)
        item = evidence_item("aggregate", f"same-tier:{model}", successor=succ,
                             nano=saving.point if saving is not None else None)
        spec = Emit(
            kind="same-tier-upgrade", category="lever", lever_class="trajectory",
            title=f"Same-tier upgrade {model} -> {succ} in {cohort.label()} lanes",
            summary=(f"{len(lanes)} {cohort.label()} lanes ran {model}; its documented same-tier "
                     f"successor {succ} prices the same tokens differently (exact arithmetic, "
                     f"labeled estimated: behavior unvalidated, needs an eval)."),
            references=_REFS_SAME_TIER, fix=fix, needs_eval=True, confidence="medium",
            scope_extra={"model": model},
            lever_ids=applicable_levers("same-tier-upgrade", lanes))
        return emit(self, ctx, cohort, tally, spec, cost.billed(basis), recoverable, [item])

    # ---------- default-model ----------

    def _default_model(self, ctx: AnalysisContext, prices: Prices,
                       cohort: Cohort) -> Finding | None:
        lanes = [lane for lane in cohort.lanes if is_claude_code(lane)]
        top = total = 0
        for lane in lanes:
            for req in lane.requests:
                money = prices.request(req)
                if money is None:
                    continue
                total += money.point
                if premium_family(req.model):
                    top += money.point
        share = share_threshold(ctx, f"{self.id}.default_model_share", "0.5")
        if total <= 0 or Decimal(top) < share * total:
            return None
        spec = lever_spec("cc.default_model")
        target = parse_policy(spec).model_remap[0][1]
        text = (f"Set the managed default model to {target} (model; availableModels with "
                f"enforceAvailableModels to restrict). A trade-off: gate it through ab / measure "
                f"before rolling out.")
        fix = Fix(text=text, config_patch=patch(("model", f'"{target}"')),
                  target="claude-code-managed-settings", doc_url=settings_doc("model"),
                  gates=cc_gates("model"))
        emit_spec = Emit(
            kind="default-model", category="lever", lever_class="trajectory",
            title=f"Default model of {cohort.label()} sessions is top-tier",
            summary=(f"Opus/Fable/Mythos-class models serve {pct(top, total)}% of Claude Code "
                     f"main spend in this cohort; a {target} default replayed on these lanes "
                     f"(price-only upper bound; needs an eval)."),
            references=_REFS_DEFAULTS, fix=fix, needs_eval=True, confidence="medium",
            lever_ids=applicable_levers("default-model", lanes))
        share_item = evidence_item("aggregate", "default-model:share", top_nano=top,
                                   spend_nano=total, share_pct=pct(top, total))
        return self._replayed(ctx, prices, cohort, lanes, spec, emit_spec, [share_item])

    # ---------- default-effort ----------

    def _default_effort(self, ctx: AnalysisContext, prices: Prices,
                        cohort: Cohort) -> Finding | None:
        lanes = [lane for lane in cohort.lanes if is_claude_code(lane)]
        high = total = 0
        for lane in lanes:
            for req in serving_steps(lane):
                out = serving(req).usage.output
                total += out
                effort = req.params.effort
                if effort is not None and EFFORT_RANK.get(effort, -1) >= EFFORT_RANK["high"]:
                    high += out
        share = share_threshold(ctx, f"{self.id}.default_effort_share", "0.5")
        if total <= 0 or high <= 0 or Decimal(high) < share * total:
            return None
        spec = lever_spec("cc.default_effort")
        level = parse_policy(spec).effort[0][1]
        text = (f"Set the managed default effort to {level} (effortLevel; maxEffortLevel caps "
                f"what developers can select). A trade-off: gate it through ab / measure.")
        fix = Fix(text=text, config_patch=patch(("effortLevel", f'"{level}"')),
                  target="claude-code-managed-settings", doc_url=settings_doc("effortLevel"),
                  gates=cc_gates("effortLevel"))
        emit_spec = Emit(
            kind="default-effort", category="lever", lever_class="trajectory",
            title=f"{cohort.label()} sessions default to high effort",
            summary=(f"{pct(high, total)}% of Claude Code main output ran at effort high or "
                     f"above; an effort-{level} default replayed with half the thinking tokens "
                     f"(upper bound: deliberate escalations would stay; needs an eval)."),
            references=_REFS_DEFAULTS, fix=fix, needs_eval=True, confidence="medium",
            lever_ids=applicable_levers("default-effort", lanes))
        share_item = evidence_item("aggregate", "default-effort:share", output_high=high,
                                   output=total, share_pct=pct(high, total))
        return self._replayed(ctx, prices, cohort, lanes, spec, emit_spec, [share_item])

    # ---------- effort-mix ----------

    def _effort_mix(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        spend: Counter[str] = Counter()
        count: Counter[str] = Counter()
        tally = Tally()
        output = reasoning = 0
        any_effort = False
        for lane in cohort.lanes:
            for req in lane.requests:
                effort = req.params.effort
                any_effort = any_effort or effort is not None
                level = effort if effort in EFFORT_RANK else ("unset" if effort is None
                                                              else "other")
                money = prices.request(req)
                tally.touch(lane, req.ts_start_ms)
                tally.events += 1
                if money is None:
                    tally.unpriced += 1
                    continue
                tally.cost.add_money(money)
                spend[level] += money.point
                count[level] += 1
                inf = req.serving_inference
                if inf is not None and inf.usage.output_reasoning is not None:
                    output += inf.usage.output
                    reasoning += inf.usage.output_reasoning
        if not any_effort or tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        total = sum(spend.values())
        order = sorted(spend, key=lambda lv: (EFFORT_RANK.get(lv, 9), lv))
        items = [evidence_item("aggregate", f"effort:{level}", requests=count[level],
                               nano=spend[level], share_pct=pct(spend[level], total))
                 for level in order]
        thinking = pct(reasoning, output) if output else "n/a"
        items.append(evidence_item("aggregate", "effort-mix:thinking", output_tokens=output,
                                   reasoning_tokens=reasoning, thinking_share_pct=thinking))
        mix = ", ".join(f"{level} {pct(spend[level], total)}%" for level in order)
        fix = Fix(text=("maxEffortLevel caps the effort developers can select (a trade-off, "
                        "cc.max_effort); per-message effort keeps escalations local."),
                  config_patch=None, target="claude-code-managed-settings",
                  doc_url=settings_doc("maxEffortLevel"))
        spec = Emit(
            kind="effort-mix", category="attribution", lever_class="none",
            title=f"Effort mix of {cohort.label()} lanes",
            summary=(f"Spend by effort: {mix}; thinking is {thinking}% of output where "
                     f"reported."),
            references=_REFS_EFFORT_MIX, fix=fix, triage=True, confidence="high",
            lever_ids=applicable_levers("effort-mix", cohort.lanes))
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), None, items)

    # ---------- rebaseline ----------

    def _rebaseline(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        steps: list[tuple[int, str, Request, Lane]] = sorted(
            ((req.ts_start_ms, req.model, req, lane) for lane in cohort.lanes
             for req in serving_steps(lane)), key=lambda x: (x[0], x[2].request_id))
        min_requests = int_threshold(ctx, f"{self.id}.min_requests", 50)
        move_share = share_threshold(ctx, f"{self.id}.move_share", "0.5")
        if len(steps) < 2 * max(1, min_requests):
            return None
        change = self._change_day(steps, min_requests, move_share)
        if change is None:
            return None
        day, old, new = change
        change_ms = day * DAY_MS
        lo, hi = change_ms - _COMPARE_DAYS * DAY_MS, change_ms + _COMPARE_DAYS * DAY_MS
        before, after = _Window(), _Window()
        tally = Tally()
        for ts, _model, req, lane in steps:
            if not lo <= ts < hi:
                continue
            win = before if ts < change_ms else after
            u = serving(req).usage
            win.n += 1
            win.total_input += u.total_input
            win.output += u.output
            if u.output_reasoning is not None:
                win.reasoning += u.output_reasoning
                win.reasoning_n += 1
            money = prices.request(req)
            if money is not None:
                win.cost += money.point
                win.priced += 1
            if win is after:
                tally.hit(lane, ts)
                if money is None:
                    tally.unpriced += 1
        if before.n < min_requests or after.n < min_requests or not before.priced or \
                not after.priced:
            return None
        return self._rebaseline_finding(ctx, prices, cohort, tally, day, old, new, before, after)

    @staticmethod
    def _change_day(steps: Sequence[tuple[int, str, Request, Lane]], min_requests: int,
                    move_share: Decimal) -> tuple[int, str, str] | None:
        """The first UTC day ``d`` on which a model ``B`` serves at least *move_share* of the
        day's requests and of the requests of ``[d, d + 7 days)``, while it served less than
        that share of the 14 days before, whose plurality model ``A ≠ B`` — "≥ 50% of the
        cohort's requests move to a new model within 7 days". Returns ``(d, A, B)``."""
        per_day: dict[int, Counter[str]] = {}
        for ts, model, _req, _lane in steps:
            per_day.setdefault(ts // DAY_MS, Counter())[model] += 1
        days = sorted(per_day)

        def window(start: int, end: int) -> Counter[str]:
            total: Counter[str] = Counter()
            for d in days:
                if start <= d < end:
                    total.update(per_day[d])
            return total

        for d in days:
            today = per_day[d]
            new, n_new = min(today.items(), key=lambda kv: (-kv[1], kv[0]))
            if Decimal(n_new) < move_share * sum(today.values()):
                continue
            prior = window(d - _COMPARE_DAYS, d)
            n_prior = sum(prior.values())
            if n_prior < min_requests:
                continue
            old = min(prior.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if old == new or Decimal(prior.get(new, 0)) >= move_share * n_prior:
                continue
            ahead = window(d, d + _MOVE_DAYS)
            if Decimal(ahead.get(new, 0)) >= move_share * sum(ahead.values()):
                return d, old, new
        return None

    def _rebaseline_finding(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort,
                            tally: Tally, day: int, old: str, new: str, before: _Window,
                            after: _Window) -> Finding | None:
        basis = cohort.basis(ctx.pricer)
        mean_in_b, mean_in_a = Fraction(before.total_input, before.n), \
            Fraction(after.total_input, after.n)
        mean_out_b, mean_out_a = Fraction(before.output, before.n), Fraction(after.output, after.n)
        cost_b, cost_a = Fraction(before.cost, before.priced), Fraction(after.cost, after.priced)
        delta_cost = (cost_a - cost_b) * after.n
        figure = estimated(round_fraction(delta_cost), basis, note=(
            "observational: (mean $ per request after − before) × requests after, at the rate "
            "card's rates"))
        stale_share = STALE_PROMPT_OUTPUT_DELTA.value
        assert isinstance(stale_share, Decimal)
        ratio = mean_out_a / mean_out_b if mean_out_b else None
        stale = ratio is not None and ratio - 1 > Fraction(stale_share)
        fam_old = self._family(ctx, cohort, old)
        fam_new = self._family(ctx, cohort, new)
        notes: list[str] = []
        if fam_old and fam_new and fam_old != fam_new:
            band = TOKENIZER_BAND.value
            assert isinstance(band, tuple)
            notes.append(f"tokenizer change ({fam_old} -> {fam_new}) expected x{band[0]}-"
                         f"x{band[1]} on input")
        if new in THINKING_ON_BY_DEFAULT:
            notes.append(f"thinking is on by default on {new}")
        if stale:
            notes.append("output per call up more than "
                         f"{decimal_str(Decimal(100) * stale_share, 0)}% suggests stale prompts: "
                         f"audit them (a support-desk audit cut cost 14%)")
        items = [
            evidence_item("aggregate", "rebaseline:change", from_model=old, to_model=new,
                          change_date=_date(day * DAY_MS), requests_before=before.n,
                          requests_after=after.n, magnitude=2),
            evidence_item("aggregate", "rebaseline:tokens",
                          mean_input_before=decimal_str(mean_in_b, 6),
                          mean_input_after=decimal_str(mean_in_a, 6),
                          delta_input_per_request=decimal_str(mean_in_a - mean_in_b, 6),
                          mean_output_before=decimal_str(mean_out_b, 6),
                          mean_output_after=decimal_str(mean_out_a, 6),
                          delta_output_per_request=decimal_str(mean_out_a - mean_out_b, 6),
                          output_ratio=decimal_str(ratio, 4) if ratio is not None else None,
                          stale_prompts="yes" if stale else "no", magnitude=1),
            evidence_item("aggregate", "rebaseline:cost",
                          usd_per_request_before_nano=round_fraction(cost_b),
                          usd_per_request_after_nano=round_fraction(cost_a),
                          delta_usd_per_request_nano=round_fraction(cost_a - cost_b),
                          magnitude=0),
        ]
        if before.reasoning_n and after.reasoning_n:
            items.append(evidence_item(
                "aggregate", "rebaseline:reasoning",
                mean_reasoning_before=decimal_str(Fraction(before.reasoning,
                                                           before.reasoning_n), 6),
                mean_reasoning_after=decimal_str(Fraction(after.reasoning, after.reasoning_n), 6),
                magnitude=0))
        change = (f"{old} -> {new} on {_date(day * DAY_MS)}: output per request "
                  f"{decimal_str(mean_out_b, 0)} -> {decimal_str(mean_out_a, 0)}, input "
                  f"{decimal_str(mean_in_b, 0)} -> {decimal_str(mean_in_a, 0)} tokens (14 days "
                  f"before vs after, observational).")
        text = ("Rebaseline budgets and alerts on the new model; "
                + ("; ".join(notes) if notes else "compare per-request tokens after migrating")
                + ".")
        fix = Fix(text=text, config_patch=None, target="code", doc_url=None)
        spec = Emit(
            kind="rebaseline", category="attribution", lever_class="behavioral",
            title=f"Model change in {cohort.label()} lanes: rebaseline",
            summary=change, references=_REFS_REBASELINE, fix=fix, triage=True,
            confidence="medium", absolute=True)
        return emit(self, ctx, cohort, tally, spec, figure, None, items)

    @staticmethod
    def _family(ctx: AnalysisContext, cohort: Cohort, model: str) -> str | None:
        """The tokenizer family of *model* as priced on its first request in the cohort."""
        for lane in cohort.lanes:
            for req in serving_steps(lane):
                if req.model == model:
                    try:
                        return ctx.pricer.tokenizer_family(serving(req).pricing,
                                                           ts_ms=req.ts_start_ms)
                    except TokenbillError:
                        return None
        return None  # pragma: no cover - the model came from these lanes


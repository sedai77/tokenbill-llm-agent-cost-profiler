"""Reference usage-level replay: the independent oracle (SPEC §9.2–§9.4, §9.8; SYNTH-ORACLE).

:class:`ReferenceReplay` is a deliberately simple, readable, per-request implementation of the
usage-level replay semantics, written from the SPEC text alone (never from REPLAY's code) so that
the fast engine can be checked against it to the nano (``tests/v2/synth_oracle``). It prices every
line through :meth:`Pricer.price_usage` (the Decimal path) and never uses integer unit rates.

How one lane is replayed (SPEC §9.3 application order, one request at a time):

1. **rate transforms** on every billable inference: model remap (with the tokenizer band), effort
   cap on the serving output, ``fast_off``, ``geo_global``, ``regional_to_global``;
2. **context transforms** on the serving inference: compaction window, cold resume;
3. **cache-state transforms**: repairs, TTL two-way flips, keepalive flips, ``fast_off`` flips;
4. **tail**: TTL re-rating of every write, the minimum-prefix gate, batch;
5. **pricing** of every billable inference (serving, passthrough and inserted calls).

Ranges come from *scenarios*: each lane is replayed as a **point** scenario (every rule as written)
and, when anything in the lane is range-sensitive, as a **low** scenario (ambiguous transitions
alive, low tokenizer band, effort scale ``s − 0.25``, batch hit band 0.98) and a **high** scenario
(ambiguous transitions expired, high band, ``s + 0.25``, hit band 0.30). A request's cost is the
point scenario's point; its bounds span every scenario's priced bounds, so ``low ≤ point ≤ high``
always holds. The interpretations of SPEC text that the SPEC leaves open are listed (O-1 …) in
``tests/v2/synth_oracle/README.md`` and ``CONTRACT-CHANGE-SYNTH-ORACLE-1.md``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from functools import partial

from tokenbill.core.cache_rules import effort_change_keeps_cache
from tokenbill.core.errors import UsageError
from tokenbill.core.evidence import (
    BATCH_CACHE_HIT_BAND,
    COMPACTION_SUMMARY_TOKENS_DEFAULT,
    THINKING_SHARE_PRIOR,
    TOKENIZER_BAND,
)
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, add, unpriced, zero
from tokenbill.core.policy import EFFORT_LEVELS, lane_matches
from tokenbill.core.protocols import CacheRulesProvider, Pricer
from tokenbill.core.records import (
    Attempt,
    Inference,
    InferenceKind,
    Lane,
    LaneEventKind,
    LaneKind,
    PricingContext,
    Request,
    UsageBuckets,
    UsageSource,
    WorkloadClass,
)
from tokenbill.core.transitions import classify_transitions
from tokenbill.core.types import (
    CalibrationReport,
    Policy,
    ReplayRequestOutcome,
    ReplayResult,
    Transition,
)

__all__ = [
    "AMBIGUITY_MS",
    "BATCH_HIT_POINT",
    "FANOUT_WINDOW_MS",
    "ReferenceReplay",
    "outcome_bounds",
    "ping_count",
    "request_costs",
    "round_half_even",
]

TTL_5M_S = 300
TTL_1H_S = 3600
AMBIGUITY_MS = 10_000
FANOUT_WINDOW_MS = 10_000
KEEPALIVE_TTL_S = TTL_5M_S
BATCH_HIT_POINT = Fraction(64, 100)
_BATCH_HIT_LOW, _BATCH_HIT_HIGH = (Fraction(v) for v in BATCH_CACHE_HIT_BAND.value)  # 0.30, 0.98
_BAND_LOW, _BAND_HIGH = (Fraction(v) for v in TOKENIZER_BAND.value)                  # 1.00, 1.35
_THINKING_SHARE = Fraction(THINKING_SHARE_PRIOR.value)                               # 0.505
_SUMMARY_DEFAULT: int = COMPACTION_SUMMARY_TOKENS_DEFAULT.value  # type: ignore[assignment]
_EFFORT_RANGE = Fraction(1, 4)
_BATCH_WORKLOADS = frozenset({WorkloadClass.CI, WorkloadClass.EVAL, WorkloadClass.SCHEDULED,
                              WorkloadClass.SERVICE})
_TRANSITION_RESETS = frozenset({LaneEventKind.COMPACTION, LaneEventKind.CLEAR,
                                LaneEventKind.CONTEXT_EDIT})
_CONTEXT_RESETS = frozenset({LaneEventKind.COMPACTION, LaneEventKind.CLEAR})
_LEGACY = "claude-legacy"
_NEW = "claude-4.7+"
_TRAJECTORY_REPAIRS = frozenset({"stagger_fanout", "shared_ci_prefix"})
_USAGE_REPAIRS = ("restore_caching", "stagger_fanout", "retry_backoff_cap", "fallback_credit",
                  "shared_ci_prefix")
_BAND_NUMBER_RE = re.compile(r"(\d+)")


# =============================================================================================
# small exact helpers
# =============================================================================================


def round_half_even(value: Fraction) -> int:
    """*value* rounded to the nearest int, ties to even (the rounding rule of SPEC D6)."""
    floor = value.numerator // value.denominator
    rest = value - floor
    if rest > Fraction(1, 2) or (rest == Fraction(1, 2) and floor % 2 == 1):
        return floor + 1
    return floor


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def ping_count(gap_ms: int, interval_s: int, max_idle_s: int) -> int:
    """Pings the non-clairvoyant keepalive daemon sends in one idle gap (SPEC D9, §9.3.2):
    ``min(ceil(gap/κ) − 1, floor(M/κ))``, and 0 when ``gap ≤ κ``."""
    step = interval_s * 1000
    if gap_ms <= step:
        return 0
    return min(-(-gap_ms // step) - 1, max_idle_s // interval_s)


def _scale(tokens: int, factor: Fraction) -> int:
    return tokens if factor == 1 else round_half_even(tokens * factor)


def _scale_usage(u: UsageBuckets, factor: Fraction) -> UsageBuckets:
    """Every token quantity of *u* times *factor*, each rounded half-even (server-tool request
    counts are not tokens and stay)."""
    if factor == 1:
        return u
    return replace(
        u,
        uncached_input=_scale(u.uncached_input, factor),
        cache_read=_scale(u.cache_read, factor),
        cache_write_5m=_scale(u.cache_write_5m, factor),
        cache_write_1h=_scale(u.cache_write_1h, factor),
        cache_write_other=_scale(u.cache_write_other, factor),
        cache_write_unknown=_scale(u.cache_write_unknown, factor),
        output=_scale(u.output, factor),
        output_reasoning=None if u.output_reasoning is None
        else _scale(u.output_reasoning, factor),
    )


# ---- write classes: where write tokens are placed ----

_WClass = tuple[str, int | None]   # ("5m" | "1h" | "other" | "unknown", other TTL seconds)


def _class_for_ttl(ttl_s: int) -> _WClass:
    return ("1h", None) if ttl_s == TTL_1H_S else ("5m", None)


def _own_class(u: UsageBuckets) -> _WClass | None:
    """The single write class of *u*, or None when it wrote nothing or several classes."""
    classes = [c for c, n in ((("5m", None), u.cache_write_5m), (("1h", None), u.cache_write_1h),
                              (("other", u.cache_write_other_ttl_s), u.cache_write_other),
                              (("unknown", None), u.cache_write_unknown)) if n > 0]
    return classes[0] if len(classes) == 1 else None


def _own_ttl_s(u: UsageBuckets, hint: str | None) -> int | None:
    """TTL (s) of *u*'s own writes, shortest class first (as ``core.lanes.ttl_of_last_write``)."""
    ttls = []
    if u.cache_write_5m:
        ttls.append(TTL_5M_S)
    if u.cache_write_1h:
        ttls.append(TTL_1H_S)
    if u.cache_write_other and u.cache_write_other_ttl_s:
        ttls.append(u.cache_write_other_ttl_s)
    if u.cache_write_unknown and hint in ("5m", "1h"):
        ttls.append(TTL_5M_S if hint == "5m" else TTL_1H_S)
    return min(ttls) if ttls else None


def _with_writes(u: UsageBuckets, total: int, wclass: _WClass) -> UsageBuckets:
    """*u* with every write bucket replaced by *total* tokens in *wclass*."""
    kind, ttl = wclass
    return replace(
        u,
        cache_write_5m=total if kind == "5m" else 0,
        cache_write_1h=total if kind == "1h" else 0,
        cache_write_other=total if kind == "other" else 0,
        cache_write_other_ttl_s=ttl if kind == "other" and total else None,
        cache_write_unknown=total if kind == "unknown" else 0,
    )


def _rerate(u: UsageBuckets, wclass: _WClass) -> UsageBuckets:
    """*u* with all of its write tokens placed in *wclass* (TTL re-rating, §9.3.1 / §9.3.7)."""
    return _with_writes(u, u.cache_write, wclass) if u.cache_write else u


def _effort_rank(level: str | None) -> int:
    return EFFORT_LEVELS.index(level) if level in EFFORT_LEVELS else -1


# =============================================================================================
# scenarios, plans and per-request records
# =============================================================================================


@dataclass(frozen=True)
class _Scenario:
    """One consistent evaluation of a lane: ``point`` follows every rule; ``low``/``high`` take
    the favorable/unfavorable side of every range source (SPEC §9.2 ambiguity, §9.3.5 bands)."""

    name: str
    ambiguous_alive: bool | None     # None: follow the rule
    band: int                        # 0 point (1.00), 1 low, 2 high tokenizer factor
    effort_shift: Fraction           # added to the thinking scale s
    batch_hit: Fraction              # cache-hit band h for batch


_POINT = _Scenario("point", None, 0, Fraction(0), BATCH_HIT_POINT)
_LOW = _Scenario("low", True, 1, -_EFFORT_RANGE, _BATCH_HIT_HIGH)
_HIGH = _Scenario("high", False, 2, _EFFORT_RANGE, _BATCH_HIT_LOW)


@dataclass
class _LanePlan:
    """What the policy does to one lane (selectors resolved once per lane)."""

    ttl_s: int | None = None                       # policy TTL τπ (TTL clause matched)
    keepalive: tuple[int, int] | None = None       # (κ, M) seconds
    remap: str | None = None                       # target model id
    effort: tuple[int, Fraction] | None = None     # (max level rank, thinking scale s)
    compaction: tuple[int, int] | None = None      # (window w, summary S_c)
    cold_resume: tuple[str, int, int] | None = None   # (action, min context, S_c)
    restore_caching: bool = False
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Item:
    """One billable inference ready to price."""

    usage: UsageBuckets
    ctx: PricingContext
    billable: bool | None
    usage_source: UsageSource
    output_upper: int | None
    ts_ms: int


@dataclass
class _Prev:
    """Chain state of the previous request with a serving inference (policy values)."""

    request: Request
    ts_ms: int
    total_obs: int          # observed T (scenario-scaled)
    total: int              # T'
    uncached: int           # U'
    prefix: int             # P' = R' + W'
    model: str              # serving model after remap
    ctx: PricingContext     # serving context after rate transforms


@dataclass
class _ReqOut:
    """One request replayed in one scenario."""

    request: Request
    usage: UsageBuckets
    extras: tuple[Inference, ...]
    figure: Figure
    changed: bool
    flipped: bool = False
    gap_ms: int | None = None
    nohit: Figure | None = None


@dataclass
class _Run:
    """Everything shared by the lanes of one replay call."""

    policy: Policy
    pricer: Pricer
    basis: Basis
    floor: Mapping[tuple[str, str], int]
    transitions: dict[str, dict[str, Transition]]
    observed: dict[str, Figure]                     # request_id → observed priced figure
    fanout: dict[str, int]                          # request_id → shared tokens (stagger_fanout)
    shared_ci: dict[str, int]                       # request_id → S_ci (shared_ci_prefix)
    summary_tokens: int
    calibrated: bool
    assumptions: set[str] = field(default_factory=set)
    added_calls: int = 0
    pings: int = 0
    hindsight_pings: int = 0
    flips: int = 0


# =============================================================================================
# the oracle
# =============================================================================================


class ReferenceReplay:
    """The reference :class:`~tokenbill.core.protocols.Replayer` (SPEC §9.8).

    ``replay`` implements §9.2–§9.4 for TTL two-way flips, keepalive, the compaction window, cold
    resume, model remap, effort (selector-scoped), ``fast_off``, ``geo_global``,
    ``regional_to_global``, batch and the usage-level repairs (``restore_caching``,
    ``stagger_fanout``, ``retry_backoff_cap``, ``fallback_credit``, ``shared_ci_prefix``).
    Block-level fields (``breakpoint_policy``, ``block:*`` repairs) are ignored with an assumption.
    """

    # ------------------------------------------------------------------ public API

    def replay(self, lanes: Sequence[Lane], policy: Policy, *, mode: str, pricer: Pricer,
               rules: CacheRulesProvider | None, calibration: CalibrationReport | None,
               static_prefix_floor: Mapping[tuple[str, str], int] | None = None,
               keep_outcomes: bool = False) -> ReplayResult:
        """Replay *lanes* under *policy* (see the module docstring). Raises :class:`UsageError`
        for an unknown mode or lanes of mixed billing classes (SPEC §9.1 #5)."""
        if mode not in ("documented", "calibrated"):
            raise UsageError(f"unknown replay mode {mode!r}")
        classes = {lane.billing_class for lane in lanes if lane.requests}
        if len(classes) > 1:
            raise UsageError("replay: lanes of one billing class only (billed | allowance)")
        if classes == {"allowance"}:
            basis = Basis.LIST_EQUIVALENT
        else:
            basis = pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST
        passing = calibration is not None and calibration.status == "pass"
        calibrated = mode == "calibrated" and passing
        label = Calibration.CALIBRATED if passing and (
            calibrated or calibration.mode_used == mode) else Calibration.UNCALIBRATED  # type: ignore[union-attr]

        ordered = sorted(lanes, key=lambda lane: lane.lane_key)
        floor = dict(static_prefix_floor or {})
        run = _Run(policy=policy, pricer=pricer, basis=basis, floor=floor, transitions={},
                   observed={}, fanout={}, shared_ci={},
                   summary_tokens=self._summary_tokens(policy),
                   calibrated=calibrated)
        for lane in ordered:
            run.transitions[lane.lane_key] = {
                t.request_id: t for t in classify_transitions(
                    lane, pricer=pricer, rules=rules,  # type: ignore[arg-type]
                    static_prefix_floor=floor)}
            for req in lane.requests:
                run.observed[req.request_id] = self._observed_figure(req, pricer, basis)
        baseline = zero(basis)
        for lane in ordered:
            for req in lane.requests:
                baseline = add(baseline, run.observed[req.request_id])

        if policy.is_observed():
            return self._identity(ordered, policy, baseline, basis, label, keep_outcomes, run)

        self._note_policy(policy, run)
        if "stagger_fanout" in policy.repairs:
            run.fanout = self._stagger_groups(ordered)
        plans = {lane.lane_key: self._plan(lane, run) for lane in ordered}
        if "shared_ci_prefix" in policy.repairs:
            run.shared_ci = self._shared_ci_runs(ordered, plans, run)

        outcomes: list[ReplayRequestOutcome] = []
        per_lane: list[tuple[str, int]] = []
        skipped: list[tuple[str, str]] = []
        points: list[int | None] = []
        lows: list[int | None] = []
        highs: list[int | None] = []
        pure_rate_exact = self._pure_rate_policy(policy)
        for lane in ordered:
            plan = plans[lane.lane_key]
            skipped.extend((lane.lane_key, reason) for reason in plan.skipped)
            point_run = self._replay_lane(lane, plan, run, _POINT, count=True)
            if point_run[1]:   # range-sensitive: evaluate the low and high scenarios too
                low_run = self._replay_lane(lane, plan, run, _LOW)[0]
                high_run = self._replay_lane(lane, plan, run, _HIGH)[0]
            else:
                low_run = high_run = point_run[0]
            lane_point: int | None = 0
            for p_out, l_out, h_out in zip(point_run[0], low_run, high_run, strict=True):
                point, low, high = self._request_bounds(p_out, l_out, h_out, run, calibration)
                obs = run.observed[p_out.request.request_id]
                obs_bounds = outcome_bounds(obs.nano, obs.low_nano, obs.high_nano)
                changed = p_out.changed or (point, low, high) != obs_bounds
                pure_rate_exact = pure_rate_exact and not p_out.flipped
                points.append(point)
                lows.append(low)
                highs.append(high)
                lane_point = None if lane_point is None or point is None else lane_point + point
                if keep_outcomes:
                    ranged = (low, high) != (point, point) or p_out.figure.low_nano is not None
                    outcomes.append(ReplayRequestOutcome(
                        request_id=p_out.request.request_id, usage=p_out.usage,
                        extra=p_out.extras, cost_nano=point,
                        low_nano=low if ranged and point is not None else None,
                        high_nano=high if ranged and point is not None else None,
                        changed=changed))
            if lane_point is not None:
                per_lane.append((lane.lane_key, lane_point))

        cost = self._cost_figure(points, lows, highs, baseline, basis, label, policy,
                                 exact=pure_rate_exact and baseline.evidence is Evidence.EXACT)
        saving = self._saving_figure(baseline, cost, policy)
        if policy.keepalive is not None:
            run.assumptions.add("keepalive hindsight minimum pings (lower bound): "
                                f"{run.hindsight_pings}")
        return ReplayResult(
            policy=policy, mode="calibrated" if calibrated else "documented", baseline=baseline,
            cost=cost, saving=saving, per_lane=tuple(per_lane),
            outcomes=tuple(outcomes) if keep_outcomes else None,
            assumptions=tuple(sorted(a for a in run.assumptions if a)), calibration=(
                Calibration.NA if cost.evidence is Evidence.EXACT else label),
            added_calls=run.added_calls, keepalive_pings=run.pings,
            lanes_skipped=tuple(sorted(skipped)), n_lanes=len(lanes),
            n_requests=sum(len(lane.requests) for lane in lanes))

    # ------------------------------------------------------------------ baseline and identity

    @staticmethod
    def _observed_figure(req: Request, pricer: Pricer, basis: Basis) -> Figure:
        """Σ ``PricedInference.figure`` over the billable inferences of *req* (SPEC §9.1 #2)."""
        fig = zero(basis)
        for att in req.attempts:
            for inf in att.inferences:
                if inf.billable is not False:
                    fig = add(fig, pricer.price_inference(inf, ts_ms=att.ts_start_ms).figure)
        return fig

    @staticmethod
    def _identity(lanes: Sequence[Lane], policy: Policy, baseline: Figure, basis: Basis,
                  label: Calibration, keep_outcomes: bool, run: _Run) -> ReplayResult:
        """The observed policy: ``cost == baseline`` and every outcome unchanged (§9.1 #2)."""
        outcomes: list[ReplayRequestOutcome] = []
        per_lane: list[tuple[str, int]] = []
        for lane in lanes:
            total: int | None = 0
            for req in lane.requests:
                fig = run.observed[req.request_id]
                total = None if total is None or fig.nano is None else total + fig.nano
                if keep_outcomes:
                    si = req.serving_inference
                    outcomes.append(ReplayRequestOutcome(
                        request_id=req.request_id,
                        usage=si.usage if si is not None else UsageBuckets(), extra=(),
                        cost_nano=fig.nano, low_nano=fig.low_nano, high_nano=fig.high_nano,
                        changed=False))
            if total is not None:
                per_lane.append((lane.lane_key, total))
        saving = zero(basis) if baseline.nano is not None else unpriced("baseline unpriced",
                                                                         basis)
        return ReplayResult(
            policy=policy, mode="documented", baseline=baseline, cost=baseline, saving=saving,
            per_lane=tuple(per_lane), outcomes=tuple(outcomes) if keep_outcomes else None,
            assumptions=("observed policy: identity replay",),
            calibration=Calibration.NA if baseline.evidence is Evidence.EXACT else label,
            added_calls=0, keepalive_pings=0, lanes_skipped=(), n_lanes=len(lanes),
            n_requests=sum(len(lane.requests) for lane in lanes))

    # ------------------------------------------------------------------ policy-level setup

    @staticmethod
    def _summary_tokens(policy: Policy) -> int:
        """S_c: the policy's ``post`` (where the caller passes the org median ``post_tokens`` of
        COMPACTION events), else COMPACTION_SUMMARY_TOKENS_DEFAULT (§9.3.3). The replay never
        derives it from the lanes it is given: results must not depend on how lanes are sharded
        (§9.1 #6)."""
        if policy.compaction_window is not None and policy.compaction_window[1] is not None:
            return policy.compaction_window[1]
        return _SUMMARY_DEFAULT

    @staticmethod
    def _note_policy(policy: Policy, run: _Run) -> None:
        """Assumption strings (content-free) for the levers the policy uses."""
        notes = run.assumptions
        notes.add(f"reference replay (oracle) of policy {policy.spec() or 'observed'}")
        if policy.model_remap:
            notes.add("model remap: token quantities scaled by the tokenizer band "
                      f"[{TOKENIZER_BAND.value[0]}, {TOKENIZER_BAND.value[1]}] between tokenizer "
                      "families (point 1.00); price-only, needs eval")
        if policy.effort:
            notes.add(f"effort cap: thinking share {THINKING_SHARE_PRIOR.value} of output when "
                      "output_reasoning is unknown; scale range s ± 0.25; upper bound, needs eval")
        if policy.compaction_window is not None or policy.cold_resume is not None:
            if policy.compaction_window is not None and policy.compaction_window[1] is not None:
                notes.add(f"compaction summary tokens S_c = {run.summary_tokens} (policy)")
            else:
                notes.add(f"compaction summary tokens S_c = {_SUMMARY_DEFAULT} "
                          "(COMPACTION_SUMMARY_TOKENS_DEFAULT)")
            notes.add("context transform: trajectory lever, upper bound (ignores re-work and "
                      "quality), needs eval")
        if policy.batch is not None:
            notes.add("batch: cache-hit band h = 0.64 (range 0.98 / 0.30)")
        if policy.breakpoint_policy is not None or any(r.startswith("block:")
                                                       for r in policy.repairs):
            notes.add("block-level fields (breakpoints, block:* repairs) ignored by the "
                      "usage-level oracle")
        if any(r in _TRAJECTORY_REPAIRS for r in policy.repairs):
            notes.add("stagger_fanout / shared_ci_prefix: upper bound")

    @staticmethod
    def _pure_rate_policy(policy: Policy) -> bool:
        """True for policies made only of exact rate transforms (fast_off, geo_global,
        regional_to_global): their saving is EXACT rate arithmetic when no transition flips and
        no line is a range (SPEC §9.3.5)."""
        rest = replace(policy, fast_off=False, geo_global=False, regional_to_global=False,
                       name="observed")
        return rest.is_observed() and (policy.fast_off or policy.geo_global
                                       or policy.regional_to_global)

    def _plan(self, lane: Lane, run: _Run) -> _LanePlan:
        """Resolve every lane-level decision of the policy for *lane*."""
        policy, pricer = run.policy, run.pricer
        plan = _LanePlan()
        first = next((r for r in lane.requests if r.serving_inference is not None), None)
        if first is None:
            return plan
        serving = first.serving_inference
        assert serving is not None
        ts = first.ts_start_ms
        for selector, target in policy.model_remap:
            if lane_matches(selector, lane):
                plan.remap = target
                break
        for selector, level, scale in policy.effort:
            if lane_matches(selector, lane):
                plan.effort = (_effort_rank(level), Fraction(scale))
                break
        if policy.keepalive is not None and lane_matches(policy.keepalive[0], lane):
            reason = self._keepalive_block(lane)
            if reason is None:
                plan.keepalive = (policy.keepalive[1], policy.keepalive[2])
            else:
                plan.skipped.append(reason)
        for selector, ttl in policy.ttl:
            if lane_matches(selector, lane):
                if serving.pricing.provider != "anthropic":
                    plan.skipped.append("ttl policy applies to Anthropic channels only")
                elif plan.keepalive is not None:
                    plan.skipped.append("ttl policy ignored: keepalive lane stays on the 5m TTL")
                else:
                    plan.ttl_s = TTL_1H_S if ttl == "1h" else TTL_5M_S
                break
        ctx = serving.pricing if plan.remap is None else replace(serving.pricing,
                                                                 model=plan.remap)
        if policy.compaction_window is not None and lane.kind is LaneKind.MAIN:
            if pricer.supports(ctx, "1m_context", ts_ms=ts):
                plan.compaction = (policy.compaction_window[0], run.summary_tokens)
            else:
                plan.skipped.append("compaction window needs a model with 1m_context")
        if policy.cold_resume is not None and lane.kind is LaneKind.MAIN:
            plan.cold_resume = (policy.cold_resume[0], policy.cold_resume[1], run.summary_tokens)
        if "restore_caching" in policy.repairs:
            plan.restore_caching = self._restore_eligible(lane, pricer, ctx, ts)
        return plan

    @staticmethod
    def _keepalive_block(lane: Lane) -> str | None:
        """Why keepalive cannot run on *lane* (§9.3.2), or None."""
        if lane.requests and lane.requests[0].attribution.agent_product == "claude_code":
            return "keepalive not allowed for claude_code"
        for req in lane.requests:
            p = req.params
            si = req.serving_inference
            if p.output_format is not None:
                return "keepalive: structured outputs"
            if p.tool_choice is not None and (p.tool_choice == "any"
                                              or p.tool_choice.startswith("tool:")):
                return "keepalive: forced tool_choice"
            if p.thinking is not None and p.thinking.startswith("enabled:"):
                return "keepalive: thinking enabled (rejected with max_tokens 0)"
            if (si is not None and si.pricing.service_tier == "batch") \
                    or p.service_tier_requested == "batch":
                return "keepalive: batch requests"
        return None

    @staticmethod
    def _restore_eligible(lane: Lane, pricer: Pricer, ctx: PricingContext, ts: int) -> bool:
        """``restore_caching`` applies to lanes with ≥ 5 requests, median ``T ≥ max(min
        cacheable, 4096)`` and no cache reads or writes at all (§9.3.6)."""
        serving = [r.serving_inference for r in lane.requests if r.serving_inference is not None]
        if len(serving) < 5:
            return False
        if any(inf.usage.cache_read or inf.usage.cache_write
               for req in lane.requests for inf in req.billable_inferences):
            return False
        totals = sorted(inf.usage.total_input for inf in serving if inf is not None)
        mid = len(totals) // 2
        median = Fraction(totals[mid]) if len(totals) % 2 else Fraction(
            totals[mid - 1] + totals[mid], 2)
        minimum = pricer.min_cacheable_tokens(ctx, ts_ms=ts) or 0
        return median >= max(minimum, 4096)

    @staticmethod
    def _stagger_groups(lanes: Sequence[Lane]) -> dict[str, int]:
        """``stagger_fanout`` (§9.3.6): lane-first requests with ``W ≥ 0.8·T`` of one (scope,
        model, cwd_key), grouped when they start within 10 s of the group's first member; every
        member after the first reads ``shared = min W`` of its group."""
        candidates: dict[tuple[str, str, str | None], list[tuple[int, str, str, int]]] = {}
        for lane in lanes:
            first = next((r for r in lane.requests if r.serving_inference is not None), None)
            if first is None:
                continue
            u = first.serving_inference.usage  # type: ignore[union-attr]
            if u.total_input == 0 or 5 * u.cache_write < 4 * u.total_input:
                continue
            key = (lane.cache_scope_key, first.model, first.attribution.cwd_key)
            candidates.setdefault(key, []).append(
                (first.ts_start_ms, lane.lane_key, first.request_id, u.cache_write))
        shared: dict[str, int] = {}
        for key in sorted(candidates, key=repr):
            members = sorted(candidates[key])
            groups: list[list[tuple[int, str, str, int]]] = []
            for member in members:
                if groups and member[0] - groups[-1][0][0] <= FANOUT_WINDOW_MS:
                    groups[-1].append(member)
                else:
                    groups.append([member])
            for group in groups:
                if len(group) < 2:
                    continue
                common = min(m[3] for m in group)
                for member in group[1:]:
                    shared[member[2]] = common
        return shared

    def _shared_ci_runs(self, lanes: Sequence[Lane], plans: Mapping[str, _LanePlan],
                        run: _Run) -> dict[str, int]:
        """``shared_ci_prefix`` (§9.3.6): CI lanes whose first request wrote ``≥ 0.8·T``, per
        (scope, model) in start order; a run starting within τπ of the previous run's start
        reads ``S_ci`` on its first request (``S`` when known, else ``floor(0.8·min first W)``)."""
        groups: dict[tuple[str, str], list[tuple[int, str, str, int, int]]] = {}
        for lane in lanes:
            first = next((r for r in lane.requests if r.serving_inference is not None), None)
            if first is None or first.attribution.workload_class is not WorkloadClass.CI:
                continue
            si = first.serving_inference
            assert si is not None
            u = si.usage
            if u.total_input == 0 or 5 * u.cache_write < 4 * u.total_input:
                continue
            plan = plans[lane.lane_key]
            tau = plan.ttl_s or _own_ttl_s(u, si.pricing.write_ttl_hint) or TTL_5M_S
            groups.setdefault((lane.cache_scope_key, first.model), []).append(
                (first.ts_start_ms, lane.lane_key, first.request_id, u.cache_write, tau))
        out: dict[str, int] = {}
        for key in sorted(groups):
            members = sorted(groups[key])
            known = run.floor.get(key)
            s_ci = known if known else _floor(Fraction(4, 5) * min(m[3] for m in members))
            for prev, cur in zip(members, members[1:], strict=False):
                if cur[0] - prev[0] <= cur[4] * 1000:
                    out[cur[2]] = s_ci
        return out

    # ------------------------------------------------------------------ the lane replay

    def _replay_lane(self, lane: Lane, plan: _LanePlan, run: _Run, scen: _Scenario, *,
                     count: bool = False) -> tuple[list[_ReqOut], bool]:
        """Replay one lane in one scenario; returns the per-request records and whether anything
        in the lane is range-sensitive (so the low/high scenarios must be evaluated)."""
        policy, pricer = run.policy, run.pricer
        transitions = run.transitions[lane.lane_key]
        outs: list[_ReqOut] = []
        sensitive = False
        prev: _Prev | None = None
        removed = 0
        first_total: int | None = None
        for req in lane.requests:
            t = transitions.get(req.request_id)
            attempts = self._kept_attempts(req, t, policy)
            source = req.serving_inference
            # ---- (1) rate transforms on every billable inference -------------------------
            items: list[tuple[Inference, _Item]] = []
            for att in attempts:
                for inf in att.inferences:
                    if inf.billable is False and inf is not source:
                        continue
                    item, band_sensitive = self._rate_transform(inf, att.ts_start_ms, plan, scen,
                                                                pricer, policy)
                    sensitive = sensitive or band_sensitive
                    items.append((inf, item))
            if source is None:
                outs.append(self._passthrough_only(req, items, plan, run))
                continue
            serving = next(item for inf, item in items if inf is source)
            others = [(inf, item) for inf, item in items if inf is not source]
            s_usage = serving.usage
            if plan.effort is not None and _effort_rank(req.params.effort) > plan.effort[0]:
                s_usage = self._effort_cap(s_usage, plan.effort[1], scen)
                sensitive = True
            ts = req.ts_start_ms
            reads, writes, uncached = s_usage.cache_read, s_usage.cache_write, \
                s_usage.uncached_input
            total_obs = reads + writes + uncached
            r_p, w_p, u_p, t_p = reads, writes, uncached, total_obs
            gap = ts - prev.ts_ms if prev is not None else None
            extras: list[tuple[Inference, int]] = []   # (inserted inference, priced at ts)
            flipped = False
            nohit_split: tuple[int, int, int] | None = None
            skip_cache_state = False
            model_p = serving.ctx.model or req.model
            state = {"sensitive": False}
            alive: Callable[[], bool] = _never if prev is None or gap is None else partial(
                self._alive, req, prev, model_p, lane, t, gap, plan, run, scen, state)
            created = self._created_class(plan, source, t)
            # ---- (2) context transforms ---------------------------------------------------
            if plan.compaction is not None or plan.cold_resume is not None:
                if prev is not None and (self._context_reset(lane, prev.ts_ms, ts)
                                         or 2 * total_obs < prev.total_obs):
                    removed = 0
                if first_total is None:
                    first_total = total_obs
                effective = total_obs - removed
                new = total_obs if prev is None else max(0, total_obs - prev.total_obs)
                is_alive = prev is not None and alive()
                if plan.compaction is not None and effective > plan.compaction[0]:
                    summary = plan.compaction[1]
                    cache_read = min(prev.prefix, effective) if is_alive else 0  # type: ignore[union-attr]
                    comp_usage = _with_writes(
                        UsageBuckets(cache_read=cache_read, output=summary),
                        effective - cache_read, created)
                    extras.append((self._extra(req, InferenceKind.COMPACTION, 0, comp_usage,
                                               serving.ctx), ts))
                    t_p = summary + new
                    r_p, u_p = 0, min(uncached, t_p)
                    w_p = t_p - u_p
                    removed = total_obs - t_p
                    skip_cache_state = True
                elif (plan.cold_resume is not None and prev is not None and not is_alive
                      and effective > plan.cold_resume[1]):
                    action, _minimum, summary = plan.cold_resume
                    if action == "compact":
                        extras.append((self._extra(
                            req, InferenceKind.OTHER, 0,
                            UsageBuckets(uncached_input=effective, output=summary), serving.ctx),
                            ts))
                        t_p = summary + new
                    else:
                        t_p = (first_total or 0) + new
                    r_p, u_p = 0, min(uncached, t_p)
                    w_p = t_p - u_p
                    removed = total_obs - t_p
                    skip_cache_state = True
                elif removed > 0:
                    r_p = max(0, reads - removed)
                    w_p = writes if reads >= removed else max(0, writes - (removed - reads))
                    t_p = r_p + w_p + uncached
            # ---- (3) cache-state transforms -----------------------------------------------
            if not skip_cache_state:
                before = (r_p, w_p, u_p)
                r_p, w_p, u_p, flipped = self._cache_state(
                    req, lane, t, prev, gap, r_p, w_p, u_p, t_p, source, plan, run, scen,
                    alive, state)
                if flipped:
                    nohit_split = before
            # keepalive pings for the idle gap before this request (non-clairvoyant daemon)
            if plan.keepalive is not None and prev is not None and gap is not None:
                interval, max_idle = plan.keepalive
                n = ping_count(gap, interval, max_idle)
                for k in range(1, n + 1):
                    sent = prev.ts_ms + k * interval * 1000
                    extras.append((self._extra(
                        req, InferenceKind.KEEPALIVE, k,
                        UsageBuckets(cache_read=prev.prefix, uncached_input=prev.uncached),
                        prev.ctx, ts_ms=sent), sent))
                if count:
                    run.pings += n
                    run.hindsight_pings += max(0, -(-(gap - KEEPALIVE_TTL_S * 1000)
                                                    // (interval * 1000)))
            sensitive = sensitive or state["sensitive"]
            # ---- (4) tail and (5) pricing -------------------------------------------------
            final, r_p, w_p, u_p, batch_used = self._finish(
                req, lane, source, serving, s_usage, r_p, w_p, u_p, created, plan, run, scen)
            sensitive = sensitive or batch_used
            priced_items = [
                _Item(final, serving.ctx if not batch_used else
                      replace(serving.ctx, service_tier="batch"), serving.billable,
                      serving.usage_source, serving.output_upper, serving.ts_ms)]
            if source.billable is False:
                priced_items = []
            for _inf, item in others:     # passthrough: observed pricing unless a rate transform
                priced_items.append(self._tail_item(item, plan, batch_used, rerate=False))
            for extra, sent in extras:
                priced_items.append(self._tail_item(
                    _Item(extra.usage, extra.pricing, True, UsageSource.FINAL, None, sent),
                    plan, batch_used, rerate=True))
            extras_final = tuple(
                replace(e, usage=_rerate(e.usage, _class_for_ttl(plan.ttl_s))
                        if plan.ttl_s else e.usage) for e, _sent in extras)
            unchanged = (final == source.usage and serving.ctx == source.pricing
                         and not extras and not batch_used and len(attempts) == len(req.attempts)
                         and all(item.usage == inf.usage and item.ctx == inf.pricing
                                 for inf, item in others))
            figure = run.observed[req.request_id] if unchanged else self._price(
                priced_items, run)
            nohit = None
            if flipped and run.calibrated and nohit_split is not None:
                nohit_usage = self._finish(req, lane, source, serving, s_usage, *nohit_split,
                                           created, plan, run, scen)[0]
                nohit = self._price([replace(priced_items[0], usage=nohit_usage),
                                     *priced_items[1:]], run) if priced_items else figure
            if count:
                run.added_calls += sum(1 for e, _ in extras
                                       if e.kind is not InferenceKind.KEEPALIVE)
                run.flips += int(flipped)
            outs.append(_ReqOut(request=req, usage=final, extras=extras_final, figure=figure,
                                changed=not unchanged, flipped=flipped, gap_ms=gap, nohit=nohit))
            prev = _Prev(request=req, ts_ms=ts, total_obs=total_obs, total=r_p + w_p + u_p,
                         uncached=u_p, prefix=r_p + w_p, model=model_p, ctx=serving.ctx)
        return outs, sensitive

    # ---- step 1 ------------------------------------------------------------------------------

    @staticmethod
    def _rate_transform(inf: Inference, ts_ms: int, plan: _LanePlan, scen: _Scenario,
                        pricer: Pricer, policy: Policy) -> tuple[_Item, bool]:
        """Model remap (with the tokenizer band of this scenario), ``fast_off``, ``geo_global``,
        ``regional_to_global`` on one inference (§9.3.5). Returns the item and whether the
        tokenizer band makes this inference range-sensitive."""
        ctx, usage, upper = inf.pricing, inf.usage, inf.output_upper
        band_sensitive = False
        if plan.remap is not None and ctx.model != plan.remap:
            target = replace(ctx, model=plan.remap, model_raw=plan.remap)
            src = pricer.tokenizer_family(ctx, ts_ms=ts_ms)
            dst = pricer.tokenizer_family(target, ts_ms=ts_ms)
            band = (Fraction(1), Fraction(1), Fraction(1))
            if src == _LEGACY and dst == _NEW:
                band = (Fraction(1), _BAND_LOW, _BAND_HIGH)
            elif src == _NEW and dst == _LEGACY:
                band = (Fraction(1), 1 / _BAND_HIGH, 1 / _BAND_LOW)
            band_sensitive = band[1] != 1 or band[2] != 1
            factor = band[scen.band]
            usage = _scale_usage(usage, factor)
            if upper is not None:
                upper = max(usage.output, _scale(upper, factor))
            ctx = target
        if policy.fast_off and ctx.speed != "standard":
            ctx = replace(ctx, speed="standard")
        if policy.geo_global and ctx.inference_geo is not None:
            ctx = replace(ctx, inference_geo=None)
        if policy.regional_to_global and ctx.endpoint_scope != "global":
            ctx = replace(ctx, endpoint_scope="global")
        return _Item(usage, ctx, inf.billable, inf.usage_source, upper, ts_ms), band_sensitive

    @staticmethod
    def _effort_cap(u: UsageBuckets, scale: Fraction, scen: _Scenario) -> UsageBuckets:
        """``O' = O − th·(1 − s)`` with ``th = output_reasoning`` if known else ``floor(0.505·O)``;
        the reduction is floored (§9.3.5 effort)."""
        s = min(Fraction(1), max(Fraction(0), scale + scen.effort_shift))
        thinking = u.output_reasoning if u.output_reasoning is not None \
            else _floor(_THINKING_SHARE * u.output)
        reduction = _floor(thinking * (1 - s))
        return replace(u, output=u.output - reduction,
                       output_reasoning=None if u.output_reasoning is None
                       else u.output_reasoning - reduction)

    # ---- alive_π -----------------------------------------------------------------------------

    @staticmethod
    def _tau_here(plan: _LanePlan, t: Transition | None, source: Inference) -> int:
        """τπ of this request: the policy TTL, else the observed τ of the transition, else the
        TTL of the request's own writes, else 300 s (the 5m point of R5)."""
        if plan.keepalive is not None:
            return KEEPALIVE_TTL_S
        if plan.ttl_s is not None:
            return plan.ttl_s
        if t is not None and t.ttl_s is not None:
            return t.ttl_s
        own = _own_ttl_s(source.usage, source.pricing.write_ttl_hint)
        return own if own is not None else TTL_5M_S

    @staticmethod
    def _within(gap_ms: int, tau_s: int, scen: _Scenario, state: dict[str, bool]) -> bool:
        """``gap ≤ τ``; within ±10 s of τ the transition is ambiguous: the point follows the rule,
        the low scenario takes the hit and the high scenario the miss (§9.2)."""
        limit = tau_s * 1000
        if abs(gap_ms - limit) <= AMBIGUITY_MS:
            state["sensitive"] = True
            if scen.ambiguous_alive is not None:
                return scen.ambiguous_alive
        return gap_ms <= limit

    def _alive(self, req: Request, prev: _Prev, model_p: str, lane: Lane, t: Transition | None,
               gap: int, plan: _LanePlan, run: _Run, scen: _Scenario,
               state: dict[str, bool]) -> bool:
        """alive_π(i) (§9.2): same serving model after remap, no reset, no un-repaired parameter
        change, same cache scope (always, within a lane), and the keepalive horizon or
        ``gap ≤ τπ``."""
        if model_p != prev.model:
            return False
        if self._transition_reset(lane, prev.ts_ms, req):
            return False
        change = _param_change(req, prev.request)
        if change is not None and not (change == "fast-toggle" and run.policy.fast_off):
            return False
        if plan.keepalive is not None:
            interval, max_idle = plan.keepalive
            n = ping_count(gap, interval, max_idle)
            return gap <= KEEPALIVE_TTL_S * 1000 or \
                n * interval * 1000 + KEEPALIVE_TTL_S * 1000 >= gap
        return self._within(gap, self._tau_here(plan, t, req.serving_inference), scen,  # type: ignore[arg-type]
                            state)

    @staticmethod
    def _transition_reset(lane: Lane, prev_ts: int, req: Request) -> bool:
        """§3.15 rule 1: a COMPACTION / CLEAR / CONTEXT_EDIT event in ``(ts_{i−1}, ts_i]``, applied
        context edits or dropped thinking on request ``i``."""
        ts = req.ts_start_ms
        if any(ev.kind in _TRANSITION_RESETS and prev_ts < ev.ts_ms <= ts for ev in lane.events):
            return True
        return any(att.applied_edits or att.thinking_dropped > 0 for att in req.attempts)

    @staticmethod
    def _context_reset(lane: Lane, prev_ts: int, ts: int) -> bool:
        """§9.3.3: an observed COMPACTION or CLEAR event in ``(ts_{i−1}, ts_i]``."""
        return any(ev.kind in _CONTEXT_RESETS and prev_ts < ev.ts_ms <= ts for ev in lane.events)

    # ---- step 3 ------------------------------------------------------------------------------

    def _cache_state(self, req: Request, lane: Lane, t: Transition | None, prev: _Prev | None,
                     gap: int | None, r_p: int, w_p: int, u_p: int, t_p: int,
                     source: Inference, plan: _LanePlan, run: _Run, scen: _Scenario,
                     alive: Callable[[], bool], state: dict[str, bool]
                     ) -> tuple[int, int, int, bool]:
        """Repairs, TTL two-way flips, keepalive flips and ``fast_off`` flips (§9.3.1, §9.3.2,
        §9.3.5, §9.3.6). Returns ``(R', W', U', flipped_to_hit)``."""
        policy = run.policy

        def warm() -> tuple[int, int, int, bool]:
            expected = min(prev.prefix, t_p) if prev is not None else 0
            reads = min(expected, t_p - u_p)
            return reads, t_p - u_p - reads, u_p, True

        # restore_caching rewrites every request of an eligible lane
        if plan.restore_caching:
            if prev is None:
                return 0, t_p, 0, False
            tau = plan.ttl_s or TTL_5M_S
            if gap is not None and self._within(gap, tau, scen, state):
                reads = min(prev.total, t_p)
                return reads, t_p - reads, 0, True
            return 0, t_p, 0, False
        # lane-first repairs
        rid = req.request_id
        if prev is None:
            if rid in run.fanout:
                moved = min(w_p, run.fanout[rid])
                return r_p + moved, w_p - moved, u_p, moved > 0
            if rid in run.shared_ci:
                reads = min(run.shared_ci[rid], t_p - u_p)
                return reads, t_p - u_p - reads, u_p, True
            return r_p, w_p, u_p, False
        if t is None:
            return r_p, w_p, u_p, False
        assert gap is not None
        tau_obs = t.ttl_s if t.ttl_s is not None else TTL_5M_S
        if t.is_miss_event:
            if plan.keepalive is not None:
                if t.cause == "ttl-expiry" and alive():
                    return warm()
            elif plan.ttl_s is not None and plan.ttl_s > tau_obs:
                if t.cause == "ttl-expiry" and alive():
                    return warm()
            if policy.fast_off and t.cause == "param-change" and t.sub_cause == "fast-toggle" \
                    and alive():
                return warm()
            if "fallback_credit" in policy.repairs and t.cause == "model-switch" \
                    and t.sub_cause == "refusal-fallback" \
                    and 5 * source.usage.cache_write >= 4 * t.expected_reuse:
                return warm()
        elif plan.ttl_s is not None and plan.ttl_s < tau_obs:
            if not self._within(gap, plan.ttl_s, scen, state) and gap <= tau_obs * 1000:
                floor = run.floor.get((lane.cache_scope_key, req.model), 0)
                reads = min(floor, t_p - u_p)
                return reads, t_p - u_p - reads, u_p, False
        if self._retry_affected(req, t, policy):
            return warm()
        return r_p, w_p, u_p, False

    @staticmethod
    def _retry_affected(req: Request, t: Transition | None, policy: Policy) -> bool:
        """``retry_backoff_cap`` (§9.3.6): ≥ 2 attempts, the final one started more than τ_obs
        after the first, and the serving inference wrote ≥ 0.5·E."""
        if "retry_backoff_cap" not in policy.repairs or t is None or len(req.attempts) < 2:
            return False
        source = req.serving_inference
        if source is None:
            return False
        tau_obs = t.ttl_s if t.ttl_s is not None else TTL_5M_S
        span = req.attempts[-1].ts_start_ms - req.attempts[0].ts_start_ms
        return span > tau_obs * 1000 and 2 * source.usage.cache_write >= t.expected_reuse \
            and t.expected_reuse > 0

    def _kept_attempts(self, req: Request, t: Transition | None,
                       policy: Policy) -> tuple[Attempt, ...]:
        """Every attempt, except that ``retry_backoff_cap`` drops attempts beyond 3 of an
        affected request (keeping the first two and the final one)."""
        if len(req.attempts) > 3 and self._retry_affected(req, t, policy):
            return (*req.attempts[:2], req.attempts[-1])
        return req.attempts

    # ---- step 4 ------------------------------------------------------------------------------

    @staticmethod
    def _created_class(plan: _LanePlan, source: Inference, t: Transition | None) -> _WClass:
        """Where the policy places a request's writes when their total changes (and the writes
        of an inserted compaction call): the policy TTL, the keepalive 5m TTL, else the request's
        own observed write class (unknown-TTL writes stay unknown: a range, R5), else the class
        of the observed τ (5m when unknown)."""
        if plan.ttl_s is not None:
            return _class_for_ttl(plan.ttl_s)
        if plan.keepalive is not None:
            return ("5m", None)
        own = _own_class(source.usage)
        if own is not None:
            return own
        tau = t.ttl_s if t is not None and t.ttl_s is not None else TTL_5M_S
        return _class_for_ttl(tau)

    def _finish(self, req: Request, lane: Lane, source: Inference, serving: _Item,
                s_usage: UsageBuckets, r_p: int, w_p: int, u_p: int, created: _WClass,
                plan: _LanePlan, run: _Run, scen: _Scenario
                ) -> tuple[UsageBuckets, int, int, int, bool]:
        """Compose the serving usage from the policy split, then TTL re-rating, the minimum-prefix
        gate and batch (§9.3.7 (4)). Returns ``(usage, R', W', U', batch_applied)``."""
        usage = self._compose(s_usage, r_p, w_p, u_p, created, plan)
        t_p = r_p + w_p + u_p
        if (usage != source.usage or serving.ctx != source.pricing) and (r_p or w_p):
            minimum = run.pricer.min_cacheable_tokens(serving.ctx, ts_ms=serving.ts_ms)
            if minimum is not None and t_p < minimum:
                r_p, w_p, u_p = 0, 0, t_p
                usage = self._compose(s_usage, 0, 0, t_p, created, plan)
        batch_used = False
        if run.policy.batch == "eligible" and self._batch_eligible(lane, req, serving.ctx):
            batch_used = True
            if serving.ctx.channel == "bedrock":
                r_p, w_p, u_p = 0, 0, t_p
                usage = self._compose(s_usage, 0, 0, t_p, created, plan)
            else:
                kept = _floor(scen.batch_hit * r_p)
                moved = r_p - kept
                usage = replace(usage, cache_read=kept,
                                cache_write_5m=usage.cache_write_5m + moved)
                r_p, w_p = kept, w_p + moved
        return usage, r_p, w_p, u_p, batch_used

    @staticmethod
    def _compose(s_usage: UsageBuckets, r_p: int, w_p: int, u_p: int, created: _WClass,
                 plan: _LanePlan) -> UsageBuckets:
        """The serving usage with the policy split; write buckets are kept when the write total
        is unchanged (re-rated to τπ on TTL lanes), else every write goes to *created*."""
        usage = replace(s_usage, cache_read=r_p, uncached_input=u_p)
        if w_p != s_usage.cache_write:
            usage = _with_writes(usage, w_p, created)
        if plan.ttl_s is not None:
            usage = _rerate(usage, _class_for_ttl(plan.ttl_s))
        return usage

    @staticmethod
    def _batch_eligible(lane: Lane, req: Request, ctx: PricingContext) -> bool:
        """§9.3.5 batch predicate: lane length 1, not already batch, not fast, workload class in
        {ci, eval, scheduled, service}, no Managed Agents entrypoint."""
        entry = req.attribution.entrypoint or ""
        return (len(lane.requests) == 1 and ctx.service_tier != "batch" and ctx.speed != "fast"
                and req.attribution.workload_class in _BATCH_WORKLOADS
                and "managed" not in entry.lower())

    @staticmethod
    def _tail_item(item: _Item, plan: _LanePlan, batch_used: bool, *, rerate: bool) -> _Item:
        """The batch tier (a rate transform: every inference of the request) and, for inserted
        inferences only, TTL re-rating. Passthrough inferences keep their observed pricing unless a
        rate transform applies (§9.2, §9.3.7 (5))."""
        usage = _rerate(item.usage, _class_for_ttl(plan.ttl_s)) \
            if rerate and plan.ttl_s else item.usage
        ctx = replace(item.ctx, service_tier="batch") if batch_used else item.ctx
        return replace(item, usage=usage, ctx=ctx)

    def _passthrough_only(self, req: Request, items: list[tuple[Inference, _Item]],
                          plan: _LanePlan, run: _Run) -> _ReqOut:
        """A request without a serving inference: every inference is passthrough (only rate
        transforms apply)."""
        final = [self._tail_item(item, plan, False, rerate=False) for _inf, item in items]
        unchanged = all(item.usage == inf.usage and item.ctx == inf.pricing
                        for (inf, _), item in zip(items, final, strict=True))
        figure = run.observed[req.request_id] if unchanged else self._price(final, run)
        return _ReqOut(request=req, usage=UsageBuckets(), extras=(), figure=figure,
                       changed=not unchanged)

    # ---- inserted inferences -----------------------------------------------------------------

    @staticmethod
    def _extra(req: Request, kind: InferenceKind, k: int, usage: UsageBuckets,
               ctx: PricingContext, *, ts_ms: int | None = None) -> Inference:
        """A counterfactual inference (keepalive ping, compaction or summarization call)."""
        iid = stable_id("inf", "oracle", req.request_id, kind.value, k,
                        ts_ms if ts_ms is not None else req.ts_start_ms)
        return Inference(inference_id=iid, kind=kind, usage=usage, pricing=ctx)

    # ---- pricing -----------------------------------------------------------------------------

    @staticmethod
    def _price(items: Iterable[_Item], run: _Run) -> Figure:
        """Σ of ``Pricer.price_usage`` figures (the Decimal path) over *items*."""
        fig = zero(run.basis)
        for item in items:
            if item.billable is False:
                continue
            priced = run.pricer.price_usage(item.usage, item.ctx, ts_ms=item.ts_ms,
                                            billable=item.billable,
                                            usage_source=item.usage_source,
                                            output_upper=item.output_upper)
            fig = add(fig, priced.figure)
        return fig

    def _request_bounds(self, p_out: _ReqOut, l_out: _ReqOut, h_out: _ReqOut, run: _Run,
                        calibration: CalibrationReport | None
                        ) -> tuple[int | None, int | None, int | None]:
        """Point of the point scenario; bounds spanning every scenario's priced bounds. In
        calibrated mode a flipped request costs ``ρ·hit + (1 − ρ)·no-hit`` (§9.4)."""
        triples = []
        for out in (p_out, l_out, h_out):
            fig = out.figure
            if run.calibrated and out.flipped and out.nohit is not None and out.gap_ms is not None:
                rho = _rho(calibration, out.gap_ms)
                fig = _mix(fig, out.nohit, rho)
            triples.append(outcome_bounds(fig.nano, fig.low_nano, fig.high_nano))
        point = triples[0][0]
        if point is None or any(tr[0] is None for tr in triples):
            return None, None, None
        low = min(tr[1] for tr in triples)  # type: ignore[type-var]
        high = max(tr[2] for tr in triples)  # type: ignore[type-var]
        return point, low, high

    # ---- result figures ----------------------------------------------------------------------

    @staticmethod
    def _cost_figure(points: list[int | None], lows: list[int | None], highs: list[int | None],
                     baseline: Figure, basis: Basis, label: Calibration, policy: Policy, *,
                     exact: bool) -> Figure:
        note = f"reference replay: {policy.spec() or 'observed'}"
        if any(p is None for p in points):
            return Figure(nano=None, evidence=Evidence.ESTIMATED, basis=basis, calibration=label,
                          note="unpriced: some replayed usage is unpriced")
        point = sum(p for p in points if p is not None)
        low = sum(v for v in lows if v is not None)
        high = sum(v for v in highs if v is not None)
        if exact and low == point == high:
            return Figure(nano=point, evidence=Evidence.EXACT, basis=basis,
                          provenance=("oracle",))
        ranged = low != point or high != point
        return Figure(nano=point, evidence=Evidence.ESTIMATED, basis=basis,
                      low_nano=low if ranged else None, high_nano=high if ranged else None,
                      calibration=label, upper_bound=False, provenance=("oracle",), note=note)

    @staticmethod
    def _saving_figure(baseline: Figure, cost: Figure, policy: Policy) -> Figure:
        """``baseline − cost`` with ranges crosswise; trajectory levers are upper bounds."""
        if baseline.nano is None or cost.nano is None:
            return Figure(nano=None, evidence=Evidence.ESTIMATED, basis=baseline.basis,
                          calibration=cost.calibration if cost.calibration is not Calibration.NA
                          else Calibration.UNCALIBRATED,
                          note="unpriced: baseline or policy cost unpriced")
        if cost.evidence is Evidence.EXACT and baseline.evidence is Evidence.EXACT:
            return Figure(nano=baseline.nano - cost.nano, evidence=Evidence.EXACT,
                          basis=baseline.basis, provenance=("oracle",))
        b_lo, b_hi = outcome_bounds(baseline.nano, baseline.low_nano, baseline.high_nano)[1:]
        c_lo, c_hi = outcome_bounds(cost.nano, cost.low_nano, cost.high_nano)[1:]
        ranged = baseline.low_nano is not None or cost.low_nano is not None
        upper = (policy.compaction_window is not None or policy.cold_resume is not None
                 or bool(policy.effort) or any(r in _TRAJECTORY_REPAIRS for r in policy.repairs))
        return Figure(
            nano=baseline.nano - cost.nano, evidence=Evidence.ESTIMATED, basis=baseline.basis,
            low_nano=b_lo - c_hi if ranged else None,  # type: ignore[operator]
            high_nano=b_hi - c_lo if ranged else None,  # type: ignore[operator]
            calibration=cost.calibration if cost.calibration is not Calibration.NA
            else Calibration.UNCALIBRATED,
            upper_bound=upper, provenance=("oracle",),
            note=f"saving vs observed: {policy.spec() or 'observed'}")


# =============================================================================================
# module-level helpers used by the oracle and its tests
# =============================================================================================


def _param_change(req: Request, prev: Request) -> str | None:
    """§3.15 rule 4 on the observed requests: the first invalidating parameter change, or None.
    A parameter differs only when both requests report it; the served speed always counts."""
    cur_inf, prev_inf = req.serving_inference, prev.serving_inference
    if cur_inf is None or prev_inf is None:
        return None
    if cur_inf.pricing.speed != prev_inf.pricing.speed:
        return "fast-toggle"
    p, pp = req.params, prev.params

    def differs(a: object, b: object) -> bool:
        return a is not None and b is not None and a != b

    if (differs(p.effort, pp.effort) or differs(p.thinking, pp.thinking)) and \
            not effort_change_keeps_cache(agent_product=req.attribution.agent_product,
                                          model=cur_inf.pricing.model or req.model,
                                          channel=cur_inf.pricing.channel,
                                          client_version=req.attribution.client_version,
                                          betas=p.betas):
        return "effort-change"
    if differs(req.attribution.client_version, prev.attribution.client_version):
        return "client-upgrade"
    if differs(req.attribution.cwd_key, prev.attribution.cwd_key):
        return "directory-change"
    return None


def _never() -> bool:
    return False


def outcome_bounds(nano: int | None, low: int | None, high: int | None
                   ) -> tuple[int | None, int | None, int | None]:
    """``(point, low, high)`` with a missing range normalized to the point (a point is its own
    range); all None when unpriced."""
    if nano is None:
        return None, None, None
    return nano, low if low is not None else nano, high if high is not None else nano


def request_costs(result: ReplayResult) -> dict[str, tuple[int | None, int | None, int | None]]:
    """``request_id → (point, low, high)`` of a replay kept with ``keep_outcomes=True`` (bounds
    normalized by :func:`outcome_bounds`)."""
    if result.outcomes is None:
        raise UsageError("request_costs needs a replay run with keep_outcomes=True")
    return {o.request_id: outcome_bounds(o.cost_nano, o.low_nano, o.high_nano)
            for o in result.outcomes}


def _rho(calibration: CalibrationReport | None, gap_ms: int) -> Fraction:
    """ρ of the gap band containing *gap_ms* (bands with < 30 trials use the pooled ρ; §9.6 #4).
    Band labels are read by their first integer (the lower bound in seconds)."""
    if calibration is None or not calibration.rho:
        return Fraction(1)
    bands = []
    for label, hits, trials, _lo, _hi in calibration.rho:
        m = _BAND_NUMBER_RE.search(label)
        bands.append((int(m.group(1)) if m else 0, hits, trials))
    bands.sort()
    total_hits = sum(b[1] for b in bands)
    total_trials = sum(b[2] for b in bands)
    pooled = Fraction(total_hits, total_trials) if total_trials else Fraction(1)
    gap_s = gap_ms // 1000
    chosen = None
    for lower, hits, trials in bands:
        if lower <= gap_s:
            chosen = (hits, trials)
    if chosen is None or chosen[1] < 30:
        return pooled
    return Fraction(chosen[0], chosen[1])


def _mix(hit: Figure, nohit: Figure, rho: Fraction) -> Figure:
    """``ρ·hit + (1 − ρ)·no-hit`` per bound, rounded once (half-even) to nano (§9.4)."""
    h = outcome_bounds(hit.nano, hit.low_nano, hit.high_nano)
    n = outcome_bounds(nohit.nano, nohit.low_nano, nohit.high_nano)
    if h[0] is None or n[0] is None:
        return hit
    mixed = [round_half_even(rho * a + (1 - rho) * b)  # type: ignore[operator]
             for a, b in zip(h, n, strict=True)]
    ranged = hit.low_nano is not None or nohit.low_nano is not None
    return Figure(nano=mixed[0], evidence=Evidence.ESTIMATED, basis=hit.basis,
                  low_nano=min(mixed) if ranged else None,
                  high_nano=max(mixed) if ranged else None,
                  calibration=Calibration.CALIBRATED, note="calibrated mixture")

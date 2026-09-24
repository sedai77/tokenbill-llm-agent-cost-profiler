"""Closed-form truth of the synthetic fleet's plants (SPEC §18, SYNTH-FLEET).

Every expected figure is computed here at generation time, **independently of REPLAY and DETECT**:
each function below applies one SPEC formula (§9.3 policy semantics, §10.2 detector figures, the
Appendix A fixtures) request by request to lanes whose shape the generator controls, prices the
observed and counterfactual usage with :class:`tokenbill.core.testing.FakePricer` (one rounding
per line, exactly as ``Pricer.price_usage``) and sums int nano-USD. Only ``core`` modules are used:
``core.transitions.is_miss_event`` for the miss rule, ``core.testing.FakePricer`` for prices.

Each closed form states the preconditions under which it equals the full replay semantics and
raises :class:`~tokenbill.core.errors.ContractViolation` when a lane violates them (the generator
never builds such lanes for a plant). The gate test ``test_gate_truth_vs_oracle.py`` cross-checks
every replay-based figure against ``synth.oracle.ReferenceReplay`` within 1 nano.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Basis
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    Inference,
    Lane,
    LaneEventKind,
    LaneKind,
    PricingContext,
    Request,
    UsageBuckets,
    UsageSource,
    billing_class,
)
from tokenbill.core.testing import FakePricer
from tokenbill.core.transitions import is_miss_event
from tokenbill.core.types import UnitRates

__all__ = [
    "Coster",
    "FleetTruth",
    "PlantTruth",
    "ReconTruth",
    "SourceTruth",
    "TeamTruth",
    "TokenTotals",
    "batch_saving",
    "build_truth",
    "cold_resume",
    "compaction_window_saving",
    "edit_churn",
    "effort_saving",
    "empty_truth",
    "keepalive_saving",
    "model_remap_saving",
    "rate_premium",
    "rebaseline_delta",
    "restore_caching_saving",
    "runaway",
    "size_tax",
    "spend",
    "token_totals",
    "tool_defs_bloat",
    "truncation",
    "ttl_1h_saving",
    "with_sources",
]

_MIN = 60_000
_RESETS = frozenset({LaneEventKind.COMPACTION, LaneEventKind.CLEAR, LaneEventKind.CONTEXT_EDIT})
_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
_WRITE_BUCKETS = ("cache_write_5m", "cache_write_1h", "cache_write_other", "cache_write_unknown")


# ---------------------------------------------------------------------------------------------
# truth records
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlantTruth:
    """The expected recovery of one plant (SPEC §18 table row, or one of its cohorts).

    Money in int nano-USD at FakePricer list prices (basis ``list`` or ``list_equivalent``).
    ``tolerances`` maps a figure name (``cost_observed``, ``recoverable``, ``shapley``,
    ``delta_output_per_request``) to a relative tolerance as a decimal string (``"0"`` = exact to
    the nano, ``"0.05"`` = ±5%) or to ``"range"`` (the detector's range must contain the truth).
    ``policy`` is the replay policy spec for replay-based figures (the oracle gate re-runs it on
    ``lane_keys``); ``details`` carry secondary figures as decimal strings.
    """

    plant_id: str
    team: str
    detector_id: str
    kind: str
    scope: tuple[tuple[str, str], ...]
    basis: str
    policy: str | None
    lane_keys: tuple[str, ...]
    cost_observed_nano: int | None
    recoverable_nano: int | None
    shapley_nano: int | None
    tolerances: tuple[tuple[str, str], ...]
    details: tuple[tuple[str, str], ...] = ()
    note: str = ""

    def tolerance(self, figure: str) -> str | None:
        """The tolerance of *figure* (``"0"``, ``"0.05"``, ``"range"``) or None (not checked)."""
        return dict(self.tolerances).get(figure)

    def detail(self, key: str) -> str | None:
        """A secondary figure by name (decimal string) or None."""
        return dict(self.details).get(key)

    def expected(self, figure: str) -> int | None:
        """The expected nano of ``cost_observed`` / ``recoverable`` / ``shapley``."""
        return {"cost_observed": self.cost_observed_nano, "recoverable": self.recoverable_nano,
                "shapley": self.shapley_nano}.get(figure)

    def within(self, figure: str, value: int, *, low: int | None = None,
               high: int | None = None) -> bool:
        """Whether a detector/plan *value* recovers *figure* within its tolerance. For a
        ``"range"`` tolerance pass the detector's ``low``/``high`` bounds (the truth must lie
        inside them)."""
        tol = self.tolerance(figure)
        truth = self.expected(figure)
        if tol is None or truth is None:
            raise UsageError(f"{self.plant_id}: no checked figure {figure!r}")
        if tol == "range":
            if low is None or high is None:
                raise UsageError("a range tolerance needs low and high")
            return low <= truth <= high
        return within(truth, value, Decimal(tol))


def within(truth: int, value: int, rel: Decimal) -> bool:
    """``|value − truth| ≤ rel·|truth|`` (exact when *rel* is 0)."""
    return abs(value - truth) * 1 <= rel * abs(truth)


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """Summed usage buckets (every inference of every attempt)."""

    uncached_input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_unknown: int = 0
    output: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h + self.cache_write_unknown

    def __add__(self, other: TokenTotals) -> TokenTotals:
        return TokenTotals(*(a + b for a, b in zip(self.as_tuple(), other.as_tuple(), strict=True)))

    def as_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (self.uncached_input, self.cache_read, self.cache_write_5m, self.cache_write_1h,
                self.cache_write_unknown, self.output)

    def merged_writes(self) -> TokenTotals:
        """The same totals with every write in ``cache_write_unknown`` (sources without a TTL
        split, e.g. Claude Code OTel)."""
        return TokenTotals(self.uncached_input, self.cache_read, 0, 0, self.cache_write,
                           self.output)

    @classmethod
    def of(cls, usage: UsageBuckets) -> TokenTotals:
        return cls(usage.uncached_input, usage.cache_read, usage.cache_write_5m,
                   usage.cache_write_1h, usage.cache_write_unknown + usage.cache_write_other,
                   usage.output)


@dataclass(frozen=True, slots=True)
class TeamTruth:
    team: str
    devs: int
    principals: tuple[str, ...]
    channel: str
    billing_path: str
    workspace_id: str
    lanes: int
    requests: int
    spend_billed_nano: int          # exact points on billed paths (FakePricer list)
    spend_allowance_nano: int       # LIST_EQUIVALENT points (subscription path)


@dataclass(frozen=True, slots=True)
class ReconTruth:
    """What reconciliation must find on the reference provider records."""

    contract_multipliers: tuple[tuple[str, str], ...]   # channel → multiplier ("0.85", "0.90")
    invoice_nano: tuple[tuple[str, int], ...]           # channel → Σ invoice lines
    provider_list_nano: tuple[tuple[str, int], ...]     # channel → list price of invoiced usage
    priority_list_nano: int        # list price of the Priority-tier bucket (no cost_report line)
    seat_allowance_nano: int       # LIST_EQUIVALENT ledger dollars (no invoice line)
    provisional_dates: tuple[str, ...]
    unpriced_models: tuple[str, ...]
    unpriced_tokens: int
    overage_dates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceTruth:
    """One written source family: its files, the teams and sessions it covers and the token totals
    the real adapter must reproduce."""

    family: str                    # "claude-code" | "claude-code-headless" | "otlp" | "trace@2"
                                   # | "anthropic-usage-report" | "anthropic-cost-report"
                                   # | "anthropic-cc-analytics" | "aws-cur"
    adapter: str                   # registry name of the reading adapter
    keys: tuple[str, ...]          # keys into FleetWorld.source_files
    teams: tuple[str, ...]
    session_keys: tuple[str, ...]
    totals: tuple[tuple[str, TokenTotals], ...]   # label (team or "all") → totals
    ttl_split: bool                # False: compare merged writes (no TTL split in the source)
    cost_nano: int | None = None   # invoice-side sources: Σ amount of the canonical cost lines
    note: str = ""


@dataclass(frozen=True)
class FleetTruth:
    """Every plant's expected figures plus fleet-level expectations (SPEC §18)."""

    seed: int
    days: int
    window_start: str
    window_end: str
    today: str
    plants: tuple[PlantTruth, ...]
    teams: tuple[TeamTruth, ...]
    recon: ReconTruth | None
    sources: tuple[SourceTruth, ...] = ()
    expectations: tuple[tuple[str, str], ...] = ()
    team_map: tuple[tuple[str, str], ...] = ()
    control_team: str = "core"
    tiny_team: str = "tiny"
    migration_date: str | None = None
    note: str = ""

    def plant(self, plant_id: str) -> PlantTruth:
        """The plant with this id (``UsageError`` when unknown)."""
        for p in self.plants:
            if p.plant_id == plant_id:
                return p
        raise UsageError(f"no plant {plant_id!r}")

    def plants_for(self, *, team: str | None = None, detector_id: str | None = None,
                   kind: str | None = None) -> tuple[PlantTruth, ...]:
        """Plants filtered by team, detector id and kind."""
        return tuple(p for p in self.plants
                     if (team is None or p.team == team)
                     and (detector_id is None or p.detector_id == detector_id)
                     and (kind is None or p.kind == kind))

    def team(self, name: str) -> TeamTruth:
        for t in self.teams:
            if t.team == name:
                return t
        raise UsageError(f"no team {name!r}")

    def source(self, family: str) -> SourceTruth:
        for s in self.sources:
            if s.family == family:
                return s
        raise UsageError(f"no source family {family!r}")


# ---------------------------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------------------------


class Coster:
    """Point prices of inferences through a :class:`Pricer` (FakePricer by default): exact integer
    unit rates for exact lines (identical to ``price_usage`` per line, SPEC §6.4), ``price_usage``
    otherwise."""

    def __init__(self, pricer: Pricer | None = None) -> None:
        self.pricer: Pricer = pricer if pricer is not None else FakePricer()
        self._units: dict[tuple[PricingContext, int], UnitRates | None] = {}

    def unit(self, ctx: PricingContext, ts_ms: int) -> UnitRates | None:
        key = (ctx, ts_ms // 86_400_000)
        if key not in self._units:
            self._units[key] = self.pricer.unit_rates(ctx, ts_ms=ts_ms)
        return self._units[key]

    def line(self, bucket: str, tokens: int, ctx: PricingContext, ts_ms: int) -> int:
        """One exact priced line (``bucket`` a PricedLine bucket name)."""
        unit = self.unit(ctx, ts_ms)
        if unit is None:
            raise ContractViolation(f"unpriced model {ctx.model!r} in a closed form")
        return unit.bucket_nano(bucket, tokens) if tokens else 0

    def usage(self, usage: UsageBuckets, ctx: PricingContext, ts_ms: int) -> int:
        """Point cost of exact FINAL usage (every line exact)."""
        if usage.cache_write_unknown or usage.cache_write_other or (
                ctx.endpoint_scope == "unknown" and ctx.channel in ("bedrock", "vertex")):
            return self._fallback(usage, ctx, ts_ms)
        return (self.line("uncached_input", usage.uncached_input, ctx, ts_ms)
                + self.line("cache_read", usage.cache_read, ctx, ts_ms)
                + self.line("cache_write_5m", usage.cache_write_5m, ctx, ts_ms)
                + self.line("cache_write_1h", usage.cache_write_1h, ctx, ts_ms)
                + self.line("output", usage.output, ctx, ts_ms)
                + self.line("web_search", usage.web_search_requests, ctx, ts_ms))

    def _fallback(self, usage: UsageBuckets, ctx: PricingContext, ts_ms: int) -> int:
        priced = self.pricer.price_usage(usage, ctx, ts_ms=ts_ms)
        if priced.figure.nano is None:
            raise ContractViolation(f"unpriced model {ctx.model!r} in a closed form")
        return priced.figure.nano

    def inference(self, inf: Inference, ts_ms: int) -> int | None:
        """Point nano of any inference (``figure.nano`` of ``price_inference``), 0 when not
        billable, None when unpriced."""
        if inf.billable is False:
            return 0
        if inf.billable is True and inf.usage_source is UsageSource.FINAL:
            unit = self.unit(inf.pricing, ts_ms)
            if unit is None:
                return None
            return self.usage(inf.usage, inf.pricing, ts_ms)
        return self.pricer.price_inference(inf, ts_ms=ts_ms).figure.nano

    def request(self, req: Request) -> int:
        """Point nano of every billable inference of a request (unpriced ones count 0)."""
        total = 0
        for att in req.attempts:
            for inf in att.inferences:
                total += self.inference(inf, att.ts_start_ms) or 0
        return total


def _serving(req: Request) -> Inference:
    inf = req.serving_inference
    if inf is None:
        raise ContractViolation("closed form: request without a serving inference")
    return inf


def _plain(lane: Lane) -> list[Request]:
    """Requests with a serving inference; asserts the plant preconditions every closed form needs:
    one attempt, one inference, FINAL billable usage."""
    out = []
    for req in lane.requests:
        if req.serving_inference is None:
            continue
        if len(req.attempts) != 1 or len(req.attempts[0].inferences) != 1:
            raise ContractViolation("closed form: plant requests have one attempt and inference")
        inf = req.attempts[0].inferences[0]
        if inf.billable is not True or inf.usage_source is not UsageSource.FINAL:
            raise ContractViolation("closed form: plant inferences are FINAL and billable")
        out.append(req)
    return out


def _total(u: UsageBuckets) -> int:
    return u.cache_read + u.cache_write + u.uncached_input


def _has_reset(lane: Lane, lo_ms: int, hi_ms: int) -> bool:
    return any(ev.kind in _RESETS and lo_ms < ev.ts_ms <= hi_ms for ev in lane.events)


def _nano(value: Fraction | Decimal) -> int:
    if isinstance(value, Fraction):
        value = Decimal(value.numerator) / Decimal(value.denominator)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _dec(value: Fraction, places: int = 6) -> str:
    d = Decimal(value.numerator) / Decimal(value.denominator)
    return str(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN))


# ---------------------------------------------------------------------------------------------
# closed forms (replay-based: SPEC §9.3; detector figures: §10.2; Appendix A)
# ---------------------------------------------------------------------------------------------


def spend(lanes: Iterable[Lane], coster: Coster) -> int:
    """Σ point cost of every billable inference of the lanes."""
    return sum(coster.request(req) for lane in lanes for req in lane.requests)


def ttl_1h_saving(lanes: Iterable[Lane], coster: Coster) -> int:
    """``ttl=1h`` on lanes billed 5m (SPEC §9.3.1, Appendix A.1): every write re-rated to 1h; a
    TTL-expiry miss whose gap is ≤ 1 h flips to a hit reading ``E = min(P_{i−1}, T_i)`` and writing
    the appended tokens. Preconditions: one model per lane, 5m writes only, no reset events, no
    gap within 10 s of 300 s or 3,600 s."""
    saving = 0
    for lane in lanes:
        reqs = _plain(lane)
        prev: tuple[int, int, str] | None = None     # (ts, P, model)
        for req in reqs:
            inf = _serving(req)
            u = inf.usage
            if u.cache_write_1h or u.cache_write_unknown or u.cache_write_other:
                raise ContractViolation("ttl_1h_saving: lanes must be billed 5m")
            ts = req.ts_start_ms
            total = _total(u)
            reads, writes = u.cache_read, u.cache_write
            if prev is not None:
                gap = ts - prev[0]
                if abs(gap - 300_000) <= 10_000 or abs(gap - 3_600_000) <= 10_000:
                    raise ContractViolation("ttl_1h_saving: ambiguous gap")
                if prev[2] != req.model or _has_reset(lane, prev[0], ts):
                    raise ContractViolation("ttl_1h_saving: model switch or reset in a lane")
                expected = min(prev[1], total)
                missed = max(0, expected - reads)
                edited = any(a.applied_edits or a.thinking_dropped for a in req.attempts)
                if is_miss_event(missed, expected) and 300_000 < gap <= 3_600_000 \
                        and not edited:
                    reads = min(expected, total - u.uncached_input)
                    writes = total - u.uncached_input - reads
            policy = UsageBuckets(uncached_input=u.uncached_input, cache_read=reads,
                                  cache_write_1h=writes, output=u.output,
                                  output_reasoning=u.output_reasoning)
            saving += coster.usage(u, inf.pricing, ts) - coster.usage(policy, inf.pricing, ts)
            prev = (ts, u.cache_read + u.cache_write, req.model)
    return saving


def restore_caching_saving(lanes: Iterable[Lane], coster: Coster, *, ttl_s: int = 300) -> int:
    """``repair=restore_caching`` (SPEC §9.3.6) on lanes with ≥ 5 requests, median ``T ≥
    max(min_cacheable, 4,096)`` and no reads or writes: request 0 writes ``T``; a request within the
    TTL reads ``min(T'_{i−1}, T_i)`` and writes the rest; after a longer gap it writes ``T``."""
    saving = 0
    for lane in lanes:
        reqs = _plain(lane)
        totals = sorted(_total(_serving(q).usage) for q in reqs)
        if len(reqs) < 5 or any(_serving(q).usage.cache_read or _serving(q).usage.cache_write
                                for q in reqs):
            continue
        first = _serving(reqs[0])
        floor = coster.pricer.min_cacheable_tokens(first.pricing, ts_ms=reqs[0].ts_start_ms) or 0
        mid = len(totals) // 2
        median = totals[mid] if len(totals) % 2 else Fraction(totals[mid - 1] + totals[mid], 2)
        if median < max(floor, 4_096):
            continue
        prev_total: int | None = None
        prev_ts = 0
        for req in reqs:
            inf = _serving(req)
            u = inf.usage
            ts = req.ts_start_ms
            total = _total(u)
            if prev_total is not None and abs(ts - prev_ts - ttl_s * 1000) <= 10_000:
                raise ContractViolation("restore_caching_saving: ambiguous gap")
            if prev_total is None:
                reads, writes = 0, total
            elif ts - prev_ts <= ttl_s * 1000:
                reads = min(prev_total, total)
                writes = total - reads
            else:
                reads, writes = 0, total
            minimum = coster.pricer.min_cacheable_tokens(inf.pricing, ts_ms=ts) or 0
            if total < minimum:
                reads = writes = 0
            policy = UsageBuckets(uncached_input=total - reads - writes, cache_read=reads,
                                  cache_write_5m=writes, output=u.output)
            saving += coster.usage(u, inf.pricing, ts) - coster.usage(policy, inf.pricing, ts)
            prev_total, prev_ts = total, ts
    return saving


def compaction_window_saving(lanes: Iterable[Lane], coster: Coster, *, window: int,
                             summary: int) -> tuple[int, int]:
    """``compact-window=<window>`` (SPEC §9.3.3, Appendix A.6) with summary size ``S_c``.
    Returns ``(saving, added compaction calls)``. Preconditions: MAIN lanes, one write bucket
    (the lane's TTL), one model."""
    saving = 0
    added = 0
    for lane in lanes:
        if lane.kind is not LaneKind.MAIN:
            raise ContractViolation("compaction_window_saving: MAIN lanes only")
        reqs = _plain(lane)
        removed = 0
        prev: tuple[int, int, int, str] | None = None   # (ts, T, P', model)
        for req in reqs:
            inf = _serving(req)
            u = inf.usage
            bucket = _single_write_bucket(u, lane)
            ts = req.ts_start_ms
            total = _total(u)
            ttl_ms = 3_600_000 if bucket == "cache_write_1h" else 300_000
            if prev is not None and (_has_reset(lane, prev[0], ts) or 2 * total < prev[1]):
                removed = 0
            t_eff = total - removed
            new = total if prev is None else max(0, total - prev[1])
            extra = 0
            if t_eff > window:
                alive = (prev is not None and prev[3] == req.model
                         and not _has_reset(lane, prev[0], ts) and ts - prev[0] <= ttl_ms)
                comp_read = min(prev[2], t_eff) if alive and prev is not None else 0
                comp = UsageBuckets(cache_read=comp_read, output=summary,
                                    **{bucket: t_eff - comp_read})
                extra = coster.usage(comp, inf.pricing, ts)
                t_new = summary + new
                uncached = min(u.uncached_input, t_new)
                reads, writes = 0, t_new - uncached
                removed = total - t_new
                added += 1
            else:
                reads = max(0, u.cache_read - removed)
                writes = u.cache_write if u.cache_read >= removed else max(
                    0, u.cache_write - (removed - u.cache_read))
                uncached = u.uncached_input
            policy = UsageBuckets(uncached_input=uncached, cache_read=reads, output=u.output,
                                  **{bucket: writes})
            saving += coster.usage(u, inf.pricing, ts) - coster.usage(policy, inf.pricing, ts)
            saving -= extra
            prev = (ts, total, reads + writes, req.model)
    return saving, added


def _single_write_bucket(u: UsageBuckets, lane: Lane) -> str:
    if u.cache_write_unknown or u.cache_write_other or (u.cache_write_5m and u.cache_write_1h):
        raise ContractViolation("closed form: one known write bucket per request")
    if u.cache_write_1h or (not u.cache_write_5m and lane.ttl_observed == "1h"):
        return "cache_write_1h"
    return "cache_write_5m"


def cold_resume(lanes: Iterable[Lane], coster: Coster) -> tuple[int, int, int]:
    """``cache.cold-resume`` (SPEC §10.2, Appendix A.5): MAIN-lane TTL-expiry misses with
    ``W_i ≥ 0.5·P_{i−1}`` and ``P_{i−1} ≥ 100,000``. Returns ``(cost_observed, premium, events)``:
    the rewrite billed ``min(W_i, P_{i−1})·w`` (EXACT) and its premium over a warm read
    ``min(W_i, P_{i−1})·(w − r)`` (ESTIMATED)."""
    observed = premium = events = 0
    for lane in lanes:
        if lane.kind is not LaneKind.MAIN:
            continue
        prev: tuple[int, int, int] | None = None   # (ts, P, ttl ms of the last write)
        for req in _plain(lane):
            inf = _serving(req)
            u = inf.usage
            ts = req.ts_start_ms
            total = _total(u)
            if prev is not None:
                expected = min(prev[1], total)
                missed = max(0, expected - u.cache_read)
                gap = ts - prev[0]
                if (is_miss_event(missed, expected) and gap > prev[2]
                        and not _has_reset(lane, prev[0], ts)
                        and 2 * u.cache_write >= prev[1] and prev[1] >= 100_000):
                    bucket = _single_write_bucket(u, lane)
                    tokens = min(u.cache_write, prev[1])
                    w = coster.line(bucket, tokens, inf.pricing, ts)
                    observed += w
                    premium += w - coster.line("cache_read", tokens, inf.pricing, ts)
                    events += 1
            ttl = prev[2] if prev is not None else 300_000
            if u.cache_write:
                ttl = 3_600_000 if _single_write_bucket(u, lane) == "cache_write_1h" else 300_000
            prev = (ts, u.cache_read + u.cache_write, ttl)
    return observed, premium, events


def rate_premium(lanes: Iterable[Lane], coster: Coster,
                 transform: Callable[[PricingContext], PricingContext]) -> int:
    """Pure rate arithmetic on identical tokens (SPEC §9.3.5 ``fast_off`` / ``regional_to_global``,
    §10.2 ``premium.modifiers``): Σ cost(ctx) − cost(transform(ctx)) over every billable
    inference whose context the transform changes."""
    total = 0
    for lane in lanes:
        for req in lane.requests:
            for att in req.attempts:
                for inf in att.inferences:
                    new = transform(inf.pricing)
                    if new == inf.pricing or inf.billable is False:
                        continue
                    if inf.billable is not True or inf.usage_source is not UsageSource.FINAL:
                        raise ContractViolation("rate_premium: exact inferences only")
                    total += (coster.usage(inf.usage, inf.pricing, att.ts_start_ms)
                              - coster.usage(inf.usage, new, att.ts_start_ms))
    return total


def fast_to_standard(ctx: PricingContext) -> PricingContext:
    return replace(ctx, speed="standard") if ctx.speed == "fast" else ctx


def regional_to_global(ctx: PricingContext) -> PricingContext:
    return replace(ctx, endpoint_scope="global") if ctx.endpoint_scope in (
        "regional", "multi_region") else ctx


def model_remap_saving(lanes: Iterable[Lane], coster: Coster, target: str) -> int:
    """``model=<target>`` (SPEC §9.3.5) within one tokenizer family (no band): every inference
    repriced at *target* on identical tokens; the min-prefix gate uses the target's minimum."""
    saving = 0
    for lane in lanes:
        for req in _plain(lane):
            inf = _serving(req)
            ts = req.ts_start_ms
            new_ctx = replace(inf.pricing, model=target, model_raw=target)
            fam_a = coster.pricer.tokenizer_family(inf.pricing, ts_ms=ts)
            fam_b = coster.pricer.tokenizer_family(new_ctx, ts_ms=ts)
            if fam_a != fam_b:
                raise ContractViolation("model_remap_saving: same tokenizer family only")
            u = inf.usage
            policy = u
            minimum = coster.pricer.min_cacheable_tokens(new_ctx, ts_ms=ts) or 0
            if _total(u) < minimum:
                policy = UsageBuckets(uncached_input=_total(u), output=u.output)
            saving += coster.usage(u, inf.pricing, ts) - coster.usage(policy, new_ctx, ts)
    return saving


def effort_saving(lanes: Iterable[Lane], coster: Coster, *, max_level: str,
                  scale: Fraction) -> int:
    """``effort=<max_level>,scale=<s>`` (SPEC §9.3.5): requests whose ``params.effort`` ranks above
    *max_level* keep ``O − th·(1 − s)`` output tokens, ``th`` = reasoning tokens when known else
    ``floor(0.505·O)``. Precondition: ``th·(1 − s)`` integral (the generator makes ``th`` a
    multiple of 4)."""
    saving = 0
    cap = _EFFORT_RANK[max_level]
    for lane in lanes:
        for req in _plain(lane):
            effort = req.params.effort
            if effort is None or _EFFORT_RANK.get(effort, -1) <= cap:
                continue
            inf = _serving(req)
            u = inf.usage
            th = u.output_reasoning if u.output_reasoning is not None else u.output * 505 // 1000
            cut = th * (1 - scale)
            if cut.denominator != 1:
                raise ContractViolation("effort_saving: fractional output tokens")
            out = u.output - int(cut)
            policy = replace(u, output=out, output_reasoning=th - int(cut))
            saving += (coster.usage(u, inf.pricing, req.ts_start_ms)
                       - coster.usage(policy, inf.pricing, req.ts_start_ms))
    return saving


def keepalive_saving(lanes: Iterable[Lane], coster: Coster, *, kappa_s: int = 240,
                     max_idle_s: int = 3_600) -> tuple[int, int]:
    """``keepalive=κ,max=M`` (SPEC §9.3.2, D9, Appendix A.4) on non-Claude-Code 5m lanes: for a gap
    ``> κ`` the daemon sends ``n = min(ceil(gap/κ) − 1, floor(M/κ))`` pings reading ``P'_{i−1}``
    (plus ``U'_{i−1}`` uncached); the transition is alive iff ``gap ≤ 300 s`` or
    ``n·κ + 300 s ≥ gap``; alive TTL-expiry misses read ``min(E, T − U)``. Returns
    ``(saving, pings)``."""
    saving = 0
    pings_total = 0
    k_ms = kappa_s * 1000
    for lane in lanes:
        reqs = _plain(lane)
        if reqs and reqs[0].attribution.agent_product == "claude_code":
            raise ContractViolation("keepalive_saving: Claude Code lanes are never pinged")
        prev: tuple[int, int, int, str, PricingContext] | None = None  # ts, P', U', model, ctx
        for req in reqs:
            inf = _serving(req)
            u = inf.usage
            if u.cache_write_1h or u.cache_write_unknown or u.cache_write_other:
                raise ContractViolation("keepalive_saving: lanes billed 5m")
            ts = req.ts_start_ms
            total = _total(u)
            reads, writes = u.cache_read, u.cache_write
            cost_pings = 0
            if prev is not None:
                gap = ts - prev[0]
                n = 0
                if gap > k_ms:
                    n = min(-(-gap // k_ms) - 1, max_idle_s * 1000 // k_ms)
                    ping = UsageBuckets(cache_read=prev[1], uncached_input=prev[2])
                    cost_pings = n * coster.usage(ping, prev[4], ts)
                    pings_total += n
                alive = (gap <= 300_000 or n * k_ms + 300_000 >= gap) and \
                    prev[3] == req.model and not _has_reset(lane, prev[0], ts) and \
                    not any(a.applied_edits or a.thinking_dropped for a in req.attempts)
                expected = min(prev[1], total)
                missed = max(0, expected - reads)
                if alive and gap > 300_000 and is_miss_event(missed, expected):
                    reads = min(expected, total - u.uncached_input)
                    writes = total - u.uncached_input - reads
            policy = UsageBuckets(uncached_input=u.uncached_input, cache_read=reads,
                                  cache_write_5m=writes, output=u.output,
                                  output_reasoning=u.output_reasoning)
            saving += (coster.usage(u, inf.pricing, ts) - coster.usage(policy, inf.pricing, ts)
                       - cost_pings)
            prev = (ts, reads + writes, u.uncached_input, req.model, inf.pricing)
    return saving, pings_total


def edit_churn(lanes: Iterable[Lane], coster: Coster) -> tuple[int, int, list[Fraction]]:
    """``cache.rebuild`` edit-churn (SPEC §10.2, Appendix A.10): for each context edit on a warm
    request (``gap ≤ τ``) clearing ``X`` tokens, ``S = W_i`` rewritten, ``K_rem`` later requests
    before the next reset. Returns ``(cost_observed = Σ S·w_billed, net loss = Σ max(0, S·(w − r)
    − X·r·K_rem), [K* = S(w − r)/(X·r)])``."""
    observed = loss = 0
    kstars: list[Fraction] = []
    for lane in lanes:
        reqs = _plain(lane)
        tau = 300_000
        for i, req in enumerate(reqs):
            cleared = sum(n for a in req.attempts for _, n in a.applied_edits)
            warm = i > 0 and req.ts_start_ms - reqs[i - 1].ts_start_ms <= tau
            u_now = _serving(req).usage
            if u_now.cache_write:     # τ for the next request: the TTL of this write
                tau = 3_600_000 if _single_write_bucket(u_now, lane) == "cache_write_1h" \
                    else 300_000
            if not cleared or not warm:
                continue
            inf = _serving(req)
            u = inf.usage
            ts = req.ts_start_ms
            bucket = _single_write_bucket(u, lane)
            s = u.cache_write
            k_rem = 0
            for later in reqs[i + 1:]:
                if any(a.applied_edits for a in later.attempts) or _has_reset(
                        lane, ts, later.ts_start_ms):
                    break
                k_rem += 1
            w = coster.line(bucket, s, inf.pricing, ts)
            r = coster.line("cache_read", s, inf.pricing, ts)
            benefit = coster.line("cache_read", cleared * k_rem, inf.pricing, ts)
            observed += w
            loss += max(0, w - r - benefit)
            unit = coster.unit(inf.pricing, ts)
            assert unit is not None
            w_rate = getattr(unit, bucket)
            kstars.append(Fraction(s * (w_rate - unit.cache_read), cleared * unit.cache_read))
    return observed, loss, kstars


def truncation(lanes: Iterable[Lane], coster: Coster, *, within_ms: int = 120_000,
               share: Fraction = Fraction(95, 100)) -> tuple[int, int, int, int]:
    """``failure.path`` max-tokens-truncation (SPEC §10.2, Appendix A.11): billed cost of attempts
    stopped at ``max_tokens`` (EXACT) and of those followed within 120 s by a same-lane request
    with ``T ≥ 0.95·T_trunc`` (the recoverable upper bound). Returns ``(cost_observed,
    recoverable, truncated, retried)``."""
    observed = recoverable = n_trunc = n_retry = 0
    for lane in lanes:
        reqs = [q for q in lane.requests if q.serving_inference is not None]
        for i, req in enumerate(reqs):
            if req.final_attempt.stop_reason != "max_tokens":
                continue
            cost = coster.request(req)
            observed += cost
            n_trunc += 1
            t_trunc = _total(_serving(req).usage)
            if i + 1 < len(reqs):
                nxt = reqs[i + 1]
                if nxt.ts_start_ms - req.ts_start_ms <= within_ms and \
                        _total(_serving(nxt).usage) >= share * t_trunc:
                    recoverable += cost
                    n_retry += 1
    return observed, recoverable, n_trunc, n_retry


def tool_defs_bloat(lanes: Iterable[Lane], coster: Coster, *,
                    reduction: Fraction = Fraction(7, 10), threshold: int = 10_000
                    ) -> tuple[int, int, int, int]:
    """``context.static-prefix`` tool-defs-bloat truth (SPEC §10.2): the synthetic tool search
    removes *reduction* (70%) of the non-deferred tool-definition tokens (rescaled to billed
    ``total_input``) from every request with more than 10,000 of them; they sit at the start of
    the prompt, so a request that read at least that prefix saves reads, otherwise writes.
    Returns ``(truth, D_physical, D_proportional, requests)`` where ``D_physical`` prices the
    tool tokens by prefix position and ``D_proportional`` by the request's billed read/write mix
    (both conventions put the truth inside the detector's [0.50, 0.85] band)."""
    d_phys = Fraction(0)
    d_prop = Fraction(0)
    n = 0
    for lane in lanes:
        for req in _plain(lane):
            fp = req.fingerprint
            if fp is None:
                continue
            tools = [bl for bl in fp.blocks if bl.tier == "tools" and bl.kind == "tool_def"]
            if any(bl.deferred for bl in tools):
                continue
            est_total = sum(bl.est_tokens or 0 for bl in fp.blocks)
            inf = _serving(req)
            u = inf.usage
            total = _total(u)
            est_tools = sum(bl.est_tokens or 0 for bl in tools)
            if not est_total:
                continue
            tokens = Fraction(est_tools * total, est_total)
            if tokens <= threshold:
                continue
            n += 1
            unit = coster.unit(inf.pricing, req.ts_start_ms)
            assert unit is not None
            scale = Fraction(10**9, 10**unit.scale_exp)
            bucket = _single_write_bucket(u, lane) if u.cache_write else "cache_write_5m"
            w = getattr(unit, bucket)
            r = unit.cache_read
            d_phys += tokens * (r if u.cache_read >= tokens else w) * scale
            prefix = u.cache_read + u.cache_write
            if prefix:
                d_prop += tokens * Fraction(r * u.cache_read + w * u.cache_write, prefix) * scale
    return _nano(reduction * d_phys), _nano(d_phys), _nano(d_prop), n


def size_tax(lanes: Iterable[Lane], coster: Coster, threshold: int) -> int:
    """``context.size-tax`` exact decomposition above ``X`` (SPEC §10.2): Σ max(0, R − X)·r +
    Σ max(0, min(W, T − X))·wτ."""
    total = 0
    for lane in lanes:
        for req in _plain(lane):
            inf = _serving(req)
            u = inf.usage
            ts = req.ts_start_ms
            t = _total(u)
            total += coster.line("cache_read", max(0, u.cache_read - threshold), inf.pricing, ts)
            if u.cache_write:
                bucket = _single_write_bucket(u, lane)
                total += coster.line(bucket, max(0, min(u.cache_write, t - threshold)),
                                     inf.pricing, ts)
    return total


def runaway(lanes: Sequence[Lane], coster: Coster, session_key: str) -> dict[str, int]:
    """``tail.runaway`` figures for *session_key* within its cohort's lanes (SPEC §10.2): the
    session's exact cost, its maximum rolling-1h cost, the cohort's p95 session cost and p99 of
    per-session maximum rolling-1h cost (nearest rank), and the rule's threshold
    ``max($50, 5·p99)``."""
    by_session: dict[str, list[tuple[int, int]]] = {}
    for lane in lanes:
        for req in lane.requests:
            by_session.setdefault(lane.session_key, []).append(
                (req.ts_start_ms, coster.request(req)))
    costs: dict[str, int] = {}
    peaks: dict[str, int] = {}
    for sk, items in by_session.items():
        items.sort()
        costs[sk] = sum(c for _, c in items)
        best = 0
        j = 0
        window = 0
        for i in range(len(items)):
            while j < len(items) and items[j][0] < items[i][0] + 3_600_000:
                window += items[j][1]
                j += 1
            best = max(best, window)
            window -= items[i][1]
        peaks[sk] = best

    def nearest(values: list[int], p: Fraction) -> int:
        values = sorted(values)
        return values[max(0, math.ceil(p * len(values)) - 1)]

    p95 = nearest(list(costs.values()), Fraction(95, 100))
    p99 = nearest(list(peaks.values()), Fraction(99, 100))
    return {"session_nano": costs[session_key], "rolling_1h_max_nano": peaks[session_key],
            "cohort_p95_session_nano": p95, "cohort_p99_hourly_nano": p99,
            "threshold_nano": max(50 * 10**9, 5 * p99), "sessions": len(costs)}


def batch_saving(lanes: Iterable[Lane], coster: Coster) -> tuple[int, int, int]:
    """``batch=eligible`` (SPEC §9.3.5) on single-request lanes with a CI/eval/scheduled/service
    workload and no cached tokens: batch tier (0.5×) on identical tokens. Returns
    ``(eligible spend, saving, lanes)``."""
    eligible = saving = n = 0
    for lane in lanes:
        reqs = _plain(lane)
        if len(reqs) != 1:
            continue
        req = reqs[0]
        if req.attribution.workload_class.value not in ("ci", "eval", "scheduled", "service"):
            continue
        inf = _serving(req)
        if inf.pricing.service_tier == "batch" or inf.pricing.speed == "fast":
            continue
        if inf.usage.cache_read:
            raise ContractViolation("batch_saving: the hit band applies to cached reads")
        ts = req.ts_start_ms
        cost = coster.usage(inf.usage, inf.pricing, ts)
        u = inf.usage
        policy = UsageBuckets(uncached_input=u.uncached_input, cache_write_5m=u.cache_write,
                              output=u.output)
        batch_cost = coster.usage(policy, replace(inf.pricing, service_tier="batch"), ts)
        eligible += cost
        saving += cost - batch_cost
        n += 1
    return eligible, saving, n


def rebaseline_delta(lanes: Iterable[Lane], change_ms: int, *, days: int = 14
                     ) -> tuple[Fraction, Fraction, int, int]:
    """Mean output tokens per request in the ``days`` before and after *change_ms* (SPEC §10.2
    ``rebaseline``). Returns ``(mean_before, mean_after, n_before, n_after)``."""
    lo, hi = change_ms - days * 86_400_000, change_ms + days * 86_400_000
    before: list[int] = []
    after: list[int] = []
    for lane in lanes:
        for req in lane.requests:
            inf = req.serving_inference
            if inf is None:
                continue
            ts = req.ts_start_ms
            if lo <= ts < change_ms:
                before.append(inf.usage.output)
            elif change_ms <= ts < hi:
                after.append(inf.usage.output)
    if not before or not after:
        raise ContractViolation("rebaseline_delta: need requests on both sides")
    return (Fraction(sum(before), len(before)), Fraction(sum(after), len(after)), len(before),
            len(after))


# ---------------------------------------------------------------------------------------------
# token totals
# ---------------------------------------------------------------------------------------------


def token_totals(requests: Iterable[Request], *, provider: bool = False) -> TokenTotals:
    """Σ usage buckets over every inference of every attempt. With ``provider=True`` the provider's
    billed view: declined attempts that were not billable are left out and placeholder outputs
    count their true size (``output_upper``)."""
    t = [0, 0, 0, 0, 0, 0]
    for req in requests:
        for att in req.attempts:
            for inf in att.inferences:
                if provider and inf.billable is False:
                    continue
                u = inf.usage
                out = u.output
                if provider and inf.usage_source is UsageSource.MESSAGE_START_ONLY:
                    out = max(out, inf.output_upper or out)
                t[0] += u.uncached_input
                t[1] += u.cache_read
                t[2] += u.cache_write_5m
                t[3] += u.cache_write_1h
                t[4] += u.cache_write_unknown + u.cache_write_other
                t[5] += out
    return TokenTotals(*t)


# ---------------------------------------------------------------------------------------------
# assembling the fleet truth
# ---------------------------------------------------------------------------------------------


def _scope(**dims: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(dims.items()))


def _keys(lanes: Iterable[Lane]) -> tuple[str, ...]:
    return tuple(sorted(lane.lane_key for lane in lanes))


def _lanes_of(lanes: Sequence[Lane], team: str, kind: LaneKind | None = None,
              cls: str | None = None) -> list[Lane]:
    return [ln for ln in lanes if ln.requests and ln.team == team
            and (kind is None or ln.kind is kind) and (cls is None or ln.billing_class == cls)]


def _plant(plant_id: str, team: str, detector: str, kind: str, scope: tuple, basis: str,
           policy: str | None, lanes: Sequence[Lane], *, observed: int | None = None,
           recoverable: int | None = None, shapley: int | None = None,
           tolerances: Mapping[str, str], details: Mapping[str, object] | None = None,
           note: str = "") -> PlantTruth:
    return PlantTruth(
        plant_id=plant_id, team=team, detector_id=detector, kind=kind, scope=scope, basis=basis,
        policy=policy, lane_keys=_keys(lanes), cost_observed_nano=observed,
        recoverable_nano=recoverable, shapley_nano=shapley,
        tolerances=tuple(sorted(tolerances.items())),
        details=tuple(sorted((k, str(v)) for k, v in (details or {}).items())), note=note)


def build_truth(world: Any, lanes: Sequence[Lane], provider: Any,
                coster: Coster | None = None) -> FleetTruth:
    """Compute every plant's truth for a generated world (called by ``fleet.generate``)."""
    from tokenbill.synth import fleet as _fleet

    c = coster or Coster()
    hints = world.hints
    days = world.days
    plants: list[PlantTruth] = []
    list_ = Basis.LIST.value
    leq = Basis.LIST_EQUIVALENT.value

    # platform: gateway strips caching
    lanes_p = _lanes_of(lanes, "platform", LaneKind.MAIN)
    uncached = sum(c.line("uncached_input", _serving(q).usage.uncached_input,
                          _serving(q).pricing, q.ts_start_ms)
                   for ln in lanes_p for q in _plain(ln))
    plants.append(_plant(
        "platform.no-cache", "platform", "cache.gateway-disabled", "no-cache",
        _scope(team="platform", lane_kind="main"), list_, "repair=restore_caching", lanes_p,
        observed=uncached, recoverable=restore_caching_saving(lanes_p, c),
        tolerances={"cost_observed": "0", "recoverable": "0.05"},
        details={"gateway": "litellm-proxy", "tool_search": "disabled behind the gateway"}))

    # payments: 5m → 1h
    lanes_pay = _lanes_of(lanes, "payments", LaneKind.MAIN)
    ttl_saving = ttl_1h_saving(lanes_pay, c)
    plants.append(_plant(
        "payments.ttl-1h", "payments", "cache.ttl-advisor", "ttl-1h-recommended",
        _scope(team="payments", lane_kind="main"), list_,
        "ttl=1h@agent_product:claude_code,lane_kind:main", lanes_pay,
        observed=spend(lanes_pay, c), recoverable=ttl_saving, shapley=ttl_saving,
        tolerances={"cost_observed": "0", "recoverable": "0.05", "shapley": "0.05"},
        details={"gap_share_5_60min": _dec(_gap_share(lanes_pay), 4),
                 "lever": "cc.prompt_cache_ttl.main"},
        note="Shapley credit of cc.prompt_cache_ttl.main on the payments main lanes equals its "
             "standalone saving: no other non-trade-off lever touches them"))

    # search: size tax and compaction window (allowance)
    lanes_s = _lanes_of(lanes, "search", LaneKind.MAIN)
    taxes = {x: size_tax(lanes_s, c, x) for x in (100_000, 200_000, 400_000)}
    plants.append(_plant(
        "search.size-tax", "search", "context.size-tax", "context-tax",
        _scope(team="search", lane_kind="main", billing_class="allowance"), leq, None, lanes_s,
        observed=taxes[200_000], tolerances={"cost_observed": "0"},
        details={"x100k_nano": taxes[100_000], "x200k_nano": taxes[200_000],
                 "x400k_nano": taxes[400_000], "max_context": max(
                     _total(_serving(q).usage) for ln in lanes_s for q in _plain(ln))},
        note="EXACT decomposition above X for X in {100k, 200k, 400k}; cost_observed is X=200k"))
    cw_saving, cw_added = compaction_window_saving(
        lanes_s, c, window=400_000, summary=_fleet.COMPACTION_POST_TOKENS)
    plants.append(_plant(
        "search.compaction-window", "search", "context.compaction-window", "compaction-window",
        _scope(team="search", lane_kind="main", billing_class="allowance"), leq,
        "compact-window=400000", lanes_s, recoverable=cw_saving,
        tolerances={"recoverable": "0.05"},
        details={"window": 400_000, "summary_tokens": _fleet.COMPACTION_POST_TOKENS,
                 "added_compactions": cw_added, "sessions": len(lanes_s)},
        note="allowance headroom (list-equivalent), point estimate at w = 400k"))

    # mobile: cold resumes, allowance and overage cohorts
    for cls, basis, suffix in (("allowance", leq, "allowance"), ("billed", list_, "overage")):
        lanes_m = _lanes_of(lanes, "mobile", LaneKind.MAIN, cls)
        obs, prem, n_ev = cold_resume(lanes_m, c)
        dims = {"team": "mobile", "lane_kind": "main"}
        if cls == "allowance":
            dims["billing_class"] = "allowance"
        plants.append(_plant(
            f"mobile.cold-resume.{suffix}", "mobile", "cache.cold-resume", "cold-resume",
            _scope(**dims), basis, None, lanes_m, observed=obs, recoverable=prem,
            tolerances={"cost_observed": "0", "recoverable": "0.02"},
            details={"events": n_ev, "billing_path": "subscription" if cls == "allowance"
                     else "usage_credits"},
            note="recoverable = premium vs a warm read (ESTIMATED)"))

    # infra: fast premium, sticky escalation
    lanes_i = _lanes_of(lanes, "infra")
    fast = rate_premium(lanes_i, c, fast_to_standard)
    per_dev: dict[str, int] = {}
    for ln in lanes_i:
        prem = rate_premium([ln], c, fast_to_standard)
        if prem:
            p = ln.requests[0].attribution.principal or ""
            per_dev[p] = per_dev.get(p, 0) + prem
    plants.append(_plant(
        "infra.fast-premium", "infra", "premium.modifiers", "fast-premium",
        _scope(team="infra", lane_kind="main"), list_, "fast=off",
        [ln for ln in lanes_i if rate_premium([ln], c, fast_to_standard)],
        observed=fast, recoverable=fast,
        tolerances={"cost_observed": "0", "recoverable": "0"},
        details={f"self:{p}": v for p, v in sorted(per_dev.items())}))
    sticky = {hints.devs[f"infra-{i:02d}"].principal: why for i, why in
              ((0, "fast"), (1, "fast"), (2, "xhigh"))}
    plants.append(_plant(
        "infra.sticky-escalation", "infra", "premium.sticky-escalation", "sticky-escalation",
        _scope(team="infra", lane_kind="main"), list_, None, _lanes_of(lanes, "infra",
                                                                       LaneKind.MAIN),
        tolerances={}, details={**{f"self:{p}": why for p, why in sticky.items()},
                                "org_count": len(sticky), "org_count_suppressed": "true"},
        note="self findings for the three devs; the org count (3 < k) is suppressed"))

    # data: routing
    lanes_dm = _lanes_of(lanes, "data", LaneKind.MAIN)
    lanes_dw = _lanes_of(lanes, "data", LaneKind.WORKFLOW_AGENT)
    plants.append(_plant(
        "data.delegation", "data", "model.routing", "delegation-routing",
        _scope(team="data", lane_kind="workflow_agent"), list_,
        "model=claude-sonnet-5@lane_kind:workflow_agent", lanes_dw,
        observed=spend(lanes_dw, c), recoverable=model_remap_saving(lanes_dw, c,
                                                                     "claude-sonnet-5"),
        tolerances={"cost_observed": "0", "recoverable": "0.05"},
        note="trade-off, needs eval; same tokenizer family (no band)"))
    for kind_name, kind_lanes in (("main", lanes_dm), ("workflow_agent", lanes_dw)):
        for model, succ in (("claude-opus-5", "claude-opus-5-5"),
                            ("claude-fable-5", "claude-fable-5-1")):
            sel = [ln for ln in kind_lanes if ln.requests[0].model == model]
            if not sel:
                continue
            saving = model_remap_saving(sel, c, succ)
            plants.append(_plant(
                f"data.same-tier.{kind_name}.{model}", "data", "model.routing",
                "same-tier-upgrade", _scope(team="data", lane_kind=kind_name, model=model),
                list_, f"model={succ}@model:{model}", sel, observed=spend(sel, c),
                recoverable=saving, tolerances={"cost_observed": "0", "recoverable": "0"},
                details={"successor": succ},
                note="rate arithmetic on identical tokens (figure labeled ESTIMATED)"))
    plants.append(_plant(
        "data.default-model", "data", "model.routing", "default-model",
        _scope(team="data", lane_kind="main"), list_,
        "model=claude-sonnet-5@agent_product:claude_code,lane_kind:main", lanes_dm,
        observed=spend(lanes_dm, c), recoverable=model_remap_saving(lanes_dm, c,
                                                                     "claude-sonnet-5"),
        tolerances={"cost_observed": "0", "recoverable": "0.05"},
        note="trade-off, needs eval; point estimate"))
    plants.append(_plant(
        "data.default-effort", "data", "model.routing", "default-effort",
        _scope(team="data", lane_kind="main"), list_,
        "effort=medium,scale=0.5@agent_product:claude_code,lane_kind:main", lanes_dm,
        recoverable=effort_saving(lanes_dm, c, max_level="medium", scale=Fraction(1, 2)),
        tolerances={"recoverable": "0.05"}, note="trade-off, needs eval, upper bound"))
    mig_ms = world.since_ms + hints.migration_day * 86_400_000
    before, after, n_b, n_a = rebaseline_delta(lanes_dm, mig_ms)
    mig_lanes = [ln for ln in lanes_dm if ln.requests[0].model in ("claude-opus-4-8",
                                                                   "claude-opus-5")]
    mb, ma, _, _ = rebaseline_delta(mig_lanes, mig_ms)
    plants.append(_plant(
        "data.rebaseline", "data", "model.routing", "rebaseline",
        _scope(team="data", lane_kind="main"), list_, None, lanes_dm,
        tolerances={"delta_output_per_request": "0.02"},
        details={"delta_output_per_request": _dec(after - before),
                 "mean_output_before": _dec(before), "mean_output_after": _dec(after),
                 "requests_before": n_b, "requests_after": n_a,
                 "migrated_delta_output_per_request": _dec(ma - mb),
                 "migrated_output_ratio": _dec(ma / mb, 4),
                 "from_model": "claude-opus-4-8", "to_model": "claude-opus-5",
                 "change_date": _fleet.date_of(mig_ms)},
        note="info (behavioral): cohort Δ output per request, 14 days before vs after"))

    # ops: regional premium, runaway
    lanes_o = _lanes_of(lanes, "ops")
    regional = rate_premium(lanes_o, c, regional_to_global)
    plants.append(_plant(
        "ops.regional-premium", "ops", "premium.modifiers", "regional-premium",
        _scope(team="ops", lane_kind="main"), list_, "regional=global", lanes_o,
        observed=regional, recoverable=regional,
        tolerances={"cost_observed": "0", "recoverable": "0"}))
    if hints.runaway_session is not None:
        figs = runaway(_lanes_of(lanes, "ops", LaneKind.MAIN), c, hints.runaway_session)
        plants.append(_plant(
            "ops.runaway", "ops", "tail.runaway", "runaway-session", _scope(team="ops"),
            list_, None, [ln for ln in lanes_o if ln.session_key == hints.runaway_session],
            observed=figs["session_nano"] - figs["cohort_p95_session_nano"],
            tolerances={"cost_observed": "0.05"}, details=figs,
            note="team only (no session pseudonym without --break-glass); cost_observed = "
                 "session cost above the cohort p95 session cost (nearest rank)"))

    # ci-bots: truncation, cross-run, batch, run cost
    lanes_c = _lanes_of(lanes, "ci-bots", LaneKind.MAIN)
    t_obs, t_rec, n_t, n_r = truncation(lanes_c, c)
    plants.append(_plant(
        "ci-bots.truncation", "ci-bots", "failure.path", "max-tokens-truncation",
        _scope(team="ci-bots", lane_kind="main"), list_, None, lanes_c, observed=t_obs,
        recoverable=t_rec, tolerances={"cost_observed": "0", "recoverable": "0.05"},
        details={"truncated": n_t, "followed_within_120s": n_r, "max_tokens": 16_384}))
    first_writes = sum(c.line("cache_write_5m", _plain(ln)[0].serving_inference.usage
                              .cache_write_5m,  # type: ignore[union-attr]
                              _serving(_plain(ln)[0]).pricing, ln.requests[0].ts_start_ms)
                       for ln in lanes_c)
    plants.append(_plant(
        "ci-bots.ci-cross-run", "ci-bots", "automation", "ci-cross-run",
        _scope(team="ci-bots", lane_kind="main"), list_, "repair=shared_ci_prefix", lanes_c,
        observed=first_writes, tolerances={},
        details={"runs": len(lanes_c), "versions": "2.1.268,2.1.270,2.1.271"},
        note="presence: every run's first call rewrites its prompt (dynamic system sections)"))
    lanes_b = _lanes_of(lanes, "ci-bots", LaneKind.API_RUN)
    b_eligible, b_saving, n_b1 = batch_saving(lanes_b, c)
    plants.append(_plant(
        "ci-bots.batch-eligible", "ci-bots", "automation", "batch-eligible",
        _scope(team="ci-bots", lane_kind="api_run"), list_, "batch=eligible", lanes_b,
        observed=b_eligible, recoverable=b_saving,
        tolerances={"cost_observed": "0", "recoverable": "0.05"}, details={"lanes": n_b1}))
    plants.append(_plant(
        "ci-bots.ci-run-cost", "ci-bots", "automation", "ci-run-cost",
        _scope(team="ci-bots", lane_kind="main"), list_, None, lanes_c,
        observed=spend(lanes_c, c), tolerances={},
        details={"runs": len(lanes_c), "interactive_nano": 0},
        note="info: CI spend of the team (no interactive spend)"))

    # agents: keepalive, edit churn, tool-defs bloat, no block breakers
    lanes_a = _lanes_of(lanes, "agents", LaneKind.API_RUN)
    ka_saving, pings = keepalive_saving(lanes_a, c)
    plants.append(_plant(
        "agents.keepalive", "agents", "cache.ttl-advisor", "keepalive-recommended",
        _scope(team="agents", lane_kind="api_run"), list_,
        "keepalive=240s,max=3600s@agent_product:agent_sdk", lanes_a,
        observed=spend(lanes_a, c), recoverable=ka_saving,
        tolerances={"cost_observed": "0", "recoverable": "0.05"},
        details={"pings": pings, "ttl_1h_saving_nano": ttl_1h_saving(lanes_a, c),
                 "lever": "sdk.keepalive"},
        note="keepalive beats ttl=1h (details) and the observed 5m on this cohort"))
    e_obs, e_loss, kstars = edit_churn(lanes_a, c)
    plants.append(_plant(
        "agents.edit-churn", "agents", "cache.rebuild", "edit-churn",
        _scope(team="agents", lane_kind="api_run"), list_, None, lanes_a, observed=e_obs,
        recoverable=e_loss, tolerances={"cost_observed": "0.05", "recoverable": "0.05"},
        details={"edits": len(kstars),
                 "kstar_median": _dec(sorted(kstars)[len(kstars) // 2]) if kstars else "n/a"}))
    tb_truth, d_phys, d_prop, n_tb = tool_defs_bloat(lanes_a, c)
    plants.append(_plant(
        "agents.tool-defs-bloat", "agents", "context.static-prefix", "tool-defs-bloat",
        _scope(team="agents", lane_kind="api_run"), list_, None, lanes_a,
        recoverable=tb_truth, tolerances={"recoverable": "range"},
        details={"d_physical_nano": d_phys, "d_proportional_nano": d_prop, "requests": n_tb,
                 "tool_def_tokens": 14_000, "true_reduction": "0.70"},
        note="the detector's [0.50, 0.85] band range must contain the truth"))

    teams = _team_truths(world, lanes, c)
    expectations = (
        ("agents", "no block.breakers finding (stable fingerprints, markers at the end)"),
        ("core", "no finding with recoverable p50 >= min_usd; no miss event in any lane"),
        ("tiny", "suppressed or merged (3 principals < k) in showback and findings"),
    )
    return FleetTruth(
        seed=world.seed, days=days, window_start=world.window_start,
        window_end=world.window_end, today=world.today, plants=tuple(plants), teams=teams,
        recon=provider.recon, expectations=expectations,
        team_map=tuple(sorted(hints.team_map.items())),
        migration_date=_fleet.date_of(mig_ms))


def _gap_share(lanes: Iterable[Lane]) -> Fraction:
    """Share of transitions with a gap in (5, 60] minutes."""
    n = hit = 0
    for lane in lanes:
        reqs = lane.requests
        for a, b in pairwise(reqs):
            gap = b.ts_start_ms - a.ts_start_ms
            n += 1
            hit += 300_000 < gap <= 3_600_000
    return Fraction(hit, n or 1)


def _team_truths(world: Any, lanes: Sequence[Lane], c: Coster) -> tuple[TeamTruth, ...]:
    from tokenbill.synth import fleet as _fleet

    sizes: dict[str, int] = {}
    for dev in world.hints.devs.values():
        sizes[dev.team] = sizes.get(dev.team, 0) + 1
    out = []
    for spec in _fleet.TEAMS:
        tl = [ln for ln in lanes if ln.team == spec.name]
        billed = allowance = 0
        principals = set()
        n_req = 0
        for ln in tl:
            for req in ln.requests:
                n_req += 1
                if req.attribution.principal:
                    principals.add(req.attribution.principal)
                cost = c.request(req)
                if billing_class(req.attribution.billing_path) == "allowance":
                    allowance += cost
                else:
                    billed += cost
        out.append(TeamTruth(
            team=spec.name, devs=sizes.get(spec.name, spec.devs),
            principals=tuple(sorted(principals)), channel=spec.channel,
            billing_path=spec.billing_path, workspace_id=world.hints.workspaces[spec.name],
            lanes=len(tl), requests=n_req, spend_billed_nano=billed,
            spend_allowance_nano=allowance))
    return tuple(out)


def empty_truth(seed: int, days: int, today: str) -> FleetTruth:
    """The truth of a scale-mode world: no plants (nothing is computed at scale)."""
    from tokenbill.synth import fleet as _fleet

    return FleetTruth(seed=seed, days=days, window_start=_fleet.WINDOW_START,
                      window_end=_fleet._date_add(_fleet.WINDOW_START, days - 1), today=today,
                      plants=(), teams=(), recon=None,
                      note="scale mode: canonical records only; no plant truth")


def with_sources(truth: FleetTruth, world: Any, lanes: Sequence[Lane],
                 files: Mapping[str, Path]) -> FleetTruth:
    """Attach the written source families and the token totals their adapters must reproduce."""
    hints = world.hints
    by_session: dict[str, list[Request]] = {}
    for req in world.requests:
        by_session.setdefault(req.session_key, []).append(req)

    def totals_for(sessions: Iterable[str]) -> dict[str, TokenTotals]:
        out: dict[str, TokenTotals] = {}
        for sk in sessions:
            team = hints.session_team[sk]
            out[team] = out.get(team, TokenTotals()) + token_totals(by_session.get(sk, ()))
        return out

    def keys(prefix: str) -> tuple[str, ...]:
        return tuple(sorted(k for k in files if k.startswith(prefix)))

    sources: list[SourceTruth] = []
    cc_sessions = tuple(hints.transcript_sessions)
    sources.append(SourceTruth(
        family="claude-code", adapter="claude-code", keys=keys("claude-code/"), teams=("infra",),
        session_keys=cc_sessions, totals=tuple(sorted(totals_for(cc_sessions).items())),
        ttl_split=True, note="transcript sample: infra days 0-3 plus the unpriced-model session"))
    hl = tuple(hints.headless_sessions)
    sources.append(SourceTruth(
        family="claude-code-headless", adapter="claude-code-headless",
        keys=keys("claude-code-headless/"), teams=("ci-bots",), session_keys=hl,
        totals=tuple(sorted(totals_for(hl).items())), ttl_split=True,
        note="per-step outputs are placeholders; the result's modelUsage restores the totals"))
    core = tuple(sorted(sk for sk, t in hints.session_team.items() if t == "core"))
    sources.append(SourceTruth(
        family="otlp", adapter="otlp", keys=keys("otlp/"), teams=("core",), session_keys=core,
        totals=tuple(sorted((k, v.merged_writes()) for k, v in totals_for(core).items())),
        ttl_split=False, note="claude_code.api_request carries no TTL split"))
    agents = tuple(sorted(sk for sk, t in hints.session_team.items() if t == "agents"))
    sources.append(SourceTruth(
        family="trace@2", adapter="trace@2", keys=keys("trace2/"), teams=("agents",),
        session_keys=agents, totals=tuple(sorted(totals_for(agents).items())), ttl_split=True))
    usage = TokenTotals()
    for agg in world.aggregates:
        if agg.source_kind == "anthropic.usage_report":
            usage = usage + TokenTotals.of(agg.usage)
    sources.append(SourceTruth(
        family="anthropic-usage-report", adapter="anthropic-usage-report",
        keys=keys("admin/usage_report"), teams=(), session_keys=(), totals=(("all", usage),),
        ttl_split=True, note="billed anthropic_api usage incl. the Priority-tier bucket"))
    cost = sum(cl.amount_nano for cl in world.cost_lines
               if cl.source_kind == "anthropic.cost_report")
    sources.append(SourceTruth(
        family="anthropic-cost-report", adapter="anthropic-cost-report",
        keys=keys("admin/cost_report"), teams=(), session_keys=(), totals=(), ttl_split=True,
        cost_nano=cost, note="15% contract discount; Priority tier and seat allowance absent"))
    analytics = TokenTotals()
    for agg in world.aggregates:
        if agg.source_kind == "anthropic.cc_analytics":
            analytics = analytics + TokenTotals.of(agg.usage)
    sources.append(SourceTruth(
        family="anthropic-cc-analytics", adapter="anthropic-cc-analytics",
        keys=keys("admin/claude_code"), teams=(), session_keys=(),
        totals=(("all", analytics),), ttl_split=False,
        note="aggregated to (date, team) with k = 5 (core.kanon.merge_small_groups)"))
    cur_tokens = TokenTotals()
    for agg in world.aggregates:
        if agg.source_kind == "aws.cur2":
            cur_tokens = cur_tokens + TokenTotals.of(agg.usage)
    cur_cost = sum(cl.amount_nano for cl in world.cost_lines if cl.source_kind == "aws.cur2")
    sources.append(SourceTruth(
        family="aws-cur", adapter="aws-cur", keys=keys("cur/"), teams=("ops",), session_keys=(),
        totals=(("all", cur_tokens),), ttl_split=False, cost_nano=cur_cost,
        note="usage-type rules unverified: tokens land in uncached_input with dq.unmapped_sku"))
    return replace(truth, sources=tuple(sources))


def fraction_str(value: Fraction, places: int = 6) -> str:
    """A fraction as a rounded decimal string (half-even)."""
    return _dec(value, places)


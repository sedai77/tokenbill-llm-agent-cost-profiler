"""Context detectors and the machinery shared by the DETECT-OTHER detectors (SPEC §10.1, §10.2;
package DETECT-OTHER).

Detectors (registry paths of SPEC §3.7):

* :class:`SizeTax` (``context.size-tax``, info) — the exact decomposition of what the context above
  ``X`` tokens cost (``X`` ∈ {100k, 200k, 400k}), the context p50/p90 and the share of calls and
  dollars at ≥ 200k / ≥ 400k.
* :class:`CompactionWindow` (``context.compaction-window``) — the ``compact-window`` replay grid
  on the main lanes of 1M-context models, with the extra-compactions guard (trade-off, needs eval,
  upper bound).
* :class:`StaticPrefix` (``context.static-prefix``) — the harness cost of the static prefix
  ``S = ctx.static_prefix_floor[(scope, model)]`` (info) and ``tool-defs-bloat``: non-deferred
  tool definitions above 10,000 tokens (from fingerprints) priced at the billed read/write mix,
  with the tool-search reduction band [0.50, 0.85] as an upper-bound saving.
* :class:`Carry` (``attrib.carry``) — tool output carried in the context (``tool-output-carry``) and
  the first context injection of each type per lane (``config-tax``), bytes → tokens with the
  ``core.findings.fit_cpt`` fit.

The first half of this module is the machinery shared by ``detect.context``, ``detect.premium``,
``detect.model``, ``detect.failure``, ``detect.automation`` and ``detect.tail`` (bucket prices,
the money accumulator, cohorts, finding assembly, thresholds, lever applicability, replays, the
per-kind capability notes); it is internal to DETECT-OTHER. It follows the conventions of the
cache detectors (SPEC §10.1): cross-lane logic stays inside ``core.findings.cohort_key`` cohorts
``(team, lane_kind, billing_class)`` (or finer keys inside one), so per-shard runs concatenate to
one run; findings aggregate at (team, lane kind, kind[, model]) plus ``billing_class`` when it is
not the default ``billed`` — list-equivalent classes carry basis LIST_EQUIVALENT, an "Allowance
headroom:" title and a "list-equivalent, not invoice dollars" summary (D26); ``cost_observed`` is
EXACT only for billed arithmetic; money is int nano-USD (no floats); ``min_usd`` gates every
finding (the recoverable point, else ``cost_observed`` for triage and info kinds); findings are
returned unpublished (the caller applies ``core.kanon.rescope_findings``). Only token counts,
byte counts, timestamps, enums and pseudonymous ids are read — never content.

Per-kind capability needs (R-E32) are documented on each detector; a detector called with **no
lanes** (the pipeline's single ``emit_missing`` call of ``core.registry.run_detectors``) returns
one data-quality ``missing-capabilities`` finding per kind whose extra capability is absent from
``ctx.capabilities`` (scope ``kind=<kind>``), so the note appears once per run and never per shard.
"""

from __future__ import annotations

import dataclasses
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction

from tokenbill.core import catalog
from tokenbill.core.errors import TokenbillError, UsageError
from tokenbill.core.evidence import (
    CC_AUTOCOMPACT_DEFAULT_TOKENS,
    TOOL_DEFS_DEFER_THRESHOLD_TOKENS,
    TOOL_SEARCH_REDUCTION_BAND,
)
from tokenbill.core.findings import (
    MAX_SUMMARY,
    MAX_TITLE,
    build_finding,
    cohort_key,
    fit_cpt,
    make_scope,
    min_usd_nano,
    threshold,
    top_evidence,
)
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, estimated, exact, unpriced
from tokenbill.core.money import RATIO_CTX
from tokenbill.core.policy import lane_matches, parse_policy, to_spec
from tokenbill.core.protocols import Detector, Pricer
from tokenbill.core.records import (
    Attempt,
    Inference,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    PricingContext,
    Request,
    UsageBuckets,
    UsageSource,
)
from tokenbill.core.textsafe import sanitize
from tokenbill.core.transitions import classify_transitions
from tokenbill.core.types import (
    AnalysisContext,
    EvidenceItem,
    Finding,
    Fix,
    Policy,
    ReplayResult,
    Transition,
    UnitRates,
)

__all__ = [
    "Carry",
    "CompactionWindow",
    "SizeTax",
    "StaticPrefix",
]

# =============================================================================================
# shared machinery (DETECT-OTHER internal)
# =============================================================================================

DETECTOR_VERSION = "1"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
DEFAULT_TTL_S = 300
#: The default billing class, left out of finding scopes (SPEC §10.1).
BILLED_CLASS = "billed"
#: The seat-allowance billing class (D26).
ALLOWANCE_CLASS = "allowance"
#: Billing classes whose figures are list-equivalent values, never invoice dollars: the seat
#: allowance (D26) and GitHub Copilot's pooled credits (R-E20). A table, so additive classes need
#: no code change.
LIST_EQUIVALENT_CLASSES = frozenset({ALLOWANCE_CLASS, "pool"})
ALLOWANCE_TITLE = "Allowance headroom: "
ALLOWANCE_SUMMARY = " Figures are list-equivalent, not invoice dollars."
#: Generated summaries stay this short so ``core.kanon.rescope_findings`` can prefix its
#: "[re-scoped for k-anonymity …]" note (≈ 60 chars) within 400 chars without cutting the
#: allowance statement (D26) kept at the end.
SUMMARY_BUDGET = MAX_SUMMARY - 70
RANGE_NOTE = "billed tokens; some lines are priced as a range (unknown TTL or endpoint scope)"
NO_FIX = "No mechanical fix"
#: The data-quality kind of the per-kind capability notes (R-E32).
MISSING_KIND = "missing-capabilities"
API_CACHE_DOC = "https://platform.claude.com/docs/en/build-with-claude/prompt-caching"
RESET_EVENTS = frozenset({LaneEventKind.COMPACTION, LaneEventKind.CLEAR,
                          LaneEventKind.CONTEXT_EDIT})
#: Write buckets in the order a prefix is allocated to them: longer TTLs sit before shorter ones
#: in the prompt (SPEC §19.3), so the start of a prompt is its 1h part.
WRITE_ORDER = ("cache_write_1h", "cache_write_5m", "cache_write_other", "cache_write_unknown")
#: The reverse: the end of a prompt (the tail above a context size) is its shortest-TTL part.
TAIL_ORDER = tuple(reversed(WRITE_ORDER))
#: Model families priced at the top tier (SPEC §10.2 "Opus/Fable/Mythos-class"): a table.
PREMIUM_FAMILIES = ("opus", "fable", "mythos")
#: Effort levels in rank order (SPEC §9.3.5: low < medium < high < xhigh < max).
EFFORT_RANK: Mapping[str, int] = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}

_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
            "cache_write_other", "output", "web_search")
_FIELD = {"uncached_input": "uncached_input", "cache_read": "cache_read",
          "cache_write_5m": "cache_write_5m", "cache_write_1h": "cache_write_1h",
          "cache_write_other": "cache_write_other", "cache_write_unknown": "cache_write_unknown",
          "output": "output", "web_search": "web_search_requests"}
_PROBE_TOKENS = 1_000
#: Total input added to the second exactness probe: a long-context band applies above a
#: threshold, so a rate that is the unit rate at this size is the unit rate below it too.
_PROBE_BAND_TOKENS = 1_000_000
_FAST_MAX_INPUT = _PROBE_BAND_TOKENS
_HASH_PREFIX = "h_"


@dataclasses.dataclass
class Money:
    """An int nano-USD accumulator with an optional range (unknown-TTL or other range lines)."""

    point: int = 0
    low: int = 0
    high: int = 0
    ranged: bool = False

    def add(self, nano: int) -> None:
        """Add an exact amount."""
        self.point += nano
        self.low += nano
        self.high += nano

    def add_range(self, point: int, low: int, high: int) -> None:
        """Add a range amount (``low ≤ point ≤ high``)."""
        self.point += point
        self.low += min(low, point)
        self.high += max(high, point)
        self.ranged = True

    def add_money(self, other: Money, sign: int = 1) -> None:
        """Add (``sign=1``) or subtract (``sign=-1``, ranges crosswise) another accumulator."""
        if sign >= 0:
            self.point += other.point
            self.low += other.low
            self.high += other.high
        else:
            self.point -= other.point
            self.low -= other.high
            self.high -= other.low
        self.ranged = self.ranged or other.ranged

    def billed(self, basis: Basis) -> Figure:
        """Billed arithmetic: EXACT, or ESTIMATED with its range when a line is a range."""
        if not self.ranged:
            return exact(self.point, basis)
        return Figure(nano=self.point, evidence=Evidence.ESTIMATED, basis=basis,
                      low_nano=min(self.low, self.point), high_nano=max(self.high, self.point),
                      calibration=Calibration.NA, note=RANGE_NOTE)

    def estimate(self, basis: Basis, note: str, *, upper_bound: bool = False) -> Figure:
        """An ESTIMATED (uncalibrated) figure naming its assumption in *note*."""
        low = high = None
        if self.ranged:
            low, high = min(self.low, self.point), max(self.high, self.point)
        return estimated(self.point, basis, low=low, high=high,
                         calibration=Calibration.UNCALIBRATED, upper_bound=upper_bound,
                         note=note)


def round_fraction(value: Fraction | int) -> int:
    """*value* rounded half-even to an int (one rounding)."""
    if isinstance(value, int):
        return value
    q, r = divmod(value.numerator, value.denominator)
    twice = 2 * r
    if twice > value.denominator or (twice == value.denominator and q % 2 == 1):
        q += 1
    return q


def combine(*parts: tuple[int, Money | None]) -> Money | None:
    """``Σ sign·money`` (ranges crosswise for negative signs); None if any part is None."""
    total = Money()
    for sign, money in parts:
        if money is None:
            return None
        total.add_money(money, sign)
    return total


def _probe_usage(bucket: str, tokens: int, extra_input: int) -> UsageBuckets:
    kwargs: dict[str, int] = {_FIELD[bucket]: tokens}
    if bucket == "cache_write_other":
        kwargs["cache_write_other_ttl_s"] = 1800   # informational: the rate is per bucket
    if extra_input:
        other = "cache_read" if bucket == "uncached_input" else "uncached_input"
        kwargs[other] = kwargs.get(other, 0) + extra_input
    return UsageBuckets(**kwargs)  # type: ignore[arg-type]


class Prices:
    """Prices through a :class:`~tokenbill.core.protocols.Pricer`: bucket lines (the ``r``, ``w5``,
    ``w1``, ``u``, ``o`` rates of the SPEC §10.1 formulas for a request's pricing context), whole
    usages, inferences, attempts, requests and lanes, as :class:`Money`.

    A bucket of a pricing context (per UTC day) takes the exact integer unit-rate path only after
    two probes proved that ``Pricer.price_usage`` prices it exactly at the unit rate — once alone
    and once beside 1,000,000 tokens of input, so a long-context band cannot hide behind it; the
    two paths then agree to the nano (§6.4). Everything else (unknown-TTL writes, unknown endpoint
    scope, uncertain billing, placeholder output, band rates) goes through ``price_usage`` with the
    pricer's own ranges. ``None`` means unpriceable: unknown is never zero (R2).
    """

    def __init__(self, pricer: Pricer) -> None:
        self.pricer = pricer
        self._units: dict[tuple[PricingContext, int], UnitRates | None] = {}
        self._fast: dict[tuple[PricingContext, int, str], bool] = {}

    def unit(self, pricing: PricingContext, ts_ms: int) -> UnitRates | None:
        """The pricer's exact unit rates for *pricing* on the day of *ts_ms* (cached)."""
        key = (pricing, ts_ms // DAY_MS)
        if key not in self._units:
            try:
                self._units[key] = self.pricer.unit_rates(pricing, ts_ms=ts_ms)
            except TokenbillError:
                self._units[key] = None
        return self._units[key]

    def _price(self, usage: UsageBuckets, pricing: PricingContext, ts_ms: int, *,
               billable: bool | None = True, source: UsageSource = UsageSource.FINAL,
               upper: int | None = None) -> Money | None:
        try:
            priced = self.pricer.price_usage(usage, pricing, ts_ms=ts_ms, billable=billable,
                                             usage_source=source, output_upper=upper)
        except TokenbillError:
            return None
        fig = priced.figure
        if priced.unpriced_reason is not None or fig.nano is None:
            return None
        money = Money()
        if fig.low_nano is not None and fig.high_nano is not None:
            money.add_range(fig.nano, fig.low_nano, fig.high_nano)
        elif fig.evidence is Evidence.EXACT:
            money.add(fig.nano)
        else:
            money.add_range(fig.nano, fig.nano, fig.nano)
        return money

    def fast(self, pricing: PricingContext, ts_ms: int, bucket: str) -> bool:
        """Whether *bucket* of *pricing* (on the day of *ts_ms*) is priced exactly at the unit
        rate at any total input up to 1,000,000 tokens (probed once, cached)."""
        key = (pricing, ts_ms // DAY_MS, bucket)
        got = self._fast.get(key)
        if got is None:
            got = self._probe(pricing, ts_ms, bucket)
            self._fast[key] = got
        return got

    def _probe(self, pricing: PricingContext, ts_ms: int, bucket: str) -> bool:
        unit = self.unit(pricing, ts_ms)
        if unit is None:
            return False
        for extra in (0, _PROBE_BAND_TOKENS):
            try:
                priced = self.pricer.price_usage(_probe_usage(bucket, _PROBE_TOKENS, extra),
                                                 pricing, ts_ms=ts_ms)
            except TokenbillError:
                return False
            if priced.unpriced_reason is not None:
                return False
            line = next((ln for ln in priced.lines if ln.bucket == bucket), None)
            if line is None or not line.exact or \
                    line.amount_nano != unit.bucket_nano(bucket, _PROBE_TOKENS):
                return False
        return True

    def line(self, pricing: PricingContext, ts_ms: int, bucket: str,
             tokens: int) -> Money | None:
        """*tokens* of *bucket* at *pricing*'s rates on the day of *ts_ms* (one rounding)."""
        money = Money()
        if tokens <= 0:
            return money
        if self.fast(pricing, ts_ms, bucket):
            unit = self.unit(pricing, ts_ms)
            assert unit is not None
            money.add(unit.bucket_nano(bucket, tokens))
            return money
        try:
            priced = self.pricer.price_usage(_probe_usage(bucket, tokens, 0), pricing,
                                             ts_ms=ts_ms)
        except TokenbillError:
            return None
        if priced.unpriced_reason is not None or priced.figure.nano is None:
            return None
        ln = next((x for x in priced.lines if x.bucket == bucket), None)
        if ln is None:
            return None
        if ln.exact:
            money.add(ln.amount_nano)
        else:
            low = ln.low_nano if ln.low_nano is not None else ln.amount_nano
            high = ln.high_nano if ln.high_nano is not None else ln.amount_nano
            money.add_range(ln.amount_nano, low, high)
        return money

    def usage(self, usage: UsageBuckets, pricing: PricingContext, ts_ms: int, *,
              billable: bool | None = True, source: UsageSource = UsageSource.FINAL,
              upper: int | None = None) -> Money | None:
        """The point (and range) cost of one usage, as ``Pricer.price_usage`` prices it."""
        if billable is False:
            return Money()
        if billable is True and source is UsageSource.FINAL and not usage.cache_write_unknown \
                and usage.total_input <= _FAST_MAX_INPUT:
            unit = self.unit(pricing, ts_ms)
            total = 0
            for bucket in _BUCKETS:
                qty = getattr(usage, _FIELD[bucket])
                if not qty:
                    continue
                if unit is None or not self.fast(pricing, ts_ms, bucket):
                    break
                total += unit.bucket_nano(bucket, qty)
            else:
                money = Money()
                money.add(total)
                return money
        return self._price(usage, pricing, ts_ms, billable=billable, source=source, upper=upper)

    def inference(self, inf: Inference, ts_ms: int) -> Money | None:
        """One inference priced at *ts_ms* (0 when not billable)."""
        return self.usage(inf.usage, inf.pricing, ts_ms, billable=inf.billable,
                          source=inf.usage_source, upper=inf.output_upper)

    def attempt(self, att: Attempt) -> Money | None:
        """Every billable inference of one attempt, priced at the attempt's start."""
        total = Money()
        for inf in att.inferences:
            if inf.billable is False:
                continue
            money = self.inference(inf, att.ts_start_ms)
            if money is None:
                return None
            total.add_money(money)
        return total

    def request(self, req: Request) -> Money | None:
        """Every billable inference of every attempt of a request."""
        total = Money()
        for att in req.attempts:
            money = self.attempt(att)
            if money is None:
                return None
            total.add_money(money)
        return total

    def lanes(self, lanes: Iterable[Lane]) -> Money | None:
        """The spend of *lanes* (None when any request is unpriceable)."""
        total = Money()
        for lane in lanes:
            for req in lane.requests:
                money = self.request(req)
                if money is None:
                    return None
                total.add_money(money)
        return total

    def written(self, pricing: PricingContext, ts_ms: int, usage: UsageBuckets, tokens: int,
                order: Sequence[str] = WRITE_ORDER) -> Money | None:
        """*tokens* of a request's billed writes at the billed write rates: allocated to the
        write buckets in *order* (the prompt's start first by default; :data:`TAIL_ORDER` for its
        end); unknown-TTL tokens are the pricer's [5m, 1h] range (R5). None when unpriceable."""
        remaining = max(0, min(tokens, usage.cache_write))
        money = Money()
        for bucket in order:
            take = min(getattr(usage, bucket), remaining)
            if take <= 0:
                continue
            remaining -= take
            part = self.line(pricing, ts_ms, bucket, take)
            if part is None:
                return None
            money.add_money(part)
        return money

    def priceable(self, lane: Lane) -> bool:
        """Whether every billable inference of *lane* has a priced rate."""
        return all(self.line(inf.pricing, att.ts_start_ms, "uncached_input", 1) is not None
                   for req in lane.requests for att in req.attempts for inf in att.inferences
                   if inf.billable is not False)


def write_bucket(usage: UsageBuckets) -> str:
    """The write bucket a request's writes were (mostly) billed at: the first non-empty bucket of
    :data:`WRITE_ORDER`, else ``cache_write_5m``."""
    for bucket in WRITE_ORDER:
        if getattr(usage, bucket):
            return bucket
    return "cache_write_5m"


def team_label(team: str | None) -> str:
    """The team name for generated text: never truncated (``core.kanon`` scrubs dropped scope
    values as whole tokens), so a name over 40 characters reads "the team" instead."""
    if not team:
        return "unattributed"
    clean = sanitize(team)
    return clean if len(clean) <= 40 else "the team"


@dataclasses.dataclass(frozen=True)
class Cohort:
    """One ``core.findings.cohort_key`` cohort: (team, lane kind, billing class) and its lanes."""

    team: str | None
    lane_kind: str
    billing_class: str
    lanes: tuple[Lane, ...]

    @property
    def allowance(self) -> bool:
        """True for a list-equivalent cohort (seat allowance, D26; pooled credits, R-E20)."""
        return self.billing_class in LIST_EQUIVALENT_CLASSES

    def basis(self, pricer: Pricer) -> Basis:
        """LIST_EQUIVALENT for the list-equivalent billing classes, else the pricer's billed
        basis."""
        if self.allowance:
            return Basis.LIST_EQUIVALENT
        return pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST

    def scope_dims(self, **extra: str | None) -> dict[str, str | None]:
        """Scope dims: team, lane kind, ``billing_class`` unless it is the default ``billed``,
        plus *extra* (a ``None`` value drops a dim)."""
        dims: dict[str, str | None] = {
            "team": self.team, "lane_kind": self.lane_kind,
            "billing_class": None if self.billing_class == BILLED_CLASS else self.billing_class}
        dims.update(extra)
        return dims

    def label(self) -> str:
        """``"<team> <lane kind>"`` for generated text."""
        return f"{team_label(self.team)} {self.lane_kind}"

    def fit(self, text: str, limit: int) -> str:
        """*text* within *limit* chars without ever cutting a scope value (``core.kanon`` scrubs
        dropped scope values as whole tokens): too long, the team is dropped from the label (the
        scope still names it); still too long, the text is cut at a word boundary."""
        if len(text) <= limit:
            return text
        label = self.label()
        if label in text:
            text = text.replace(label, self.lane_kind)
            if len(text) <= limit:
                return text
        head = text[:limit - 1]
        if " " in head:
            head = head.rsplit(" ", 1)[0]
        return head + "…"

    def title(self, text: str) -> str:
        """*text* with the allowance prefix when needed, at most 120 chars."""
        prefix = ALLOWANCE_TITLE if self.allowance else ""
        return prefix + self.fit(text, MAX_TITLE - len(prefix))

    def summary(self, text: str, note: str = "") -> str:
        """*text*, then *note* and (for list-equivalent cohorts) the "list-equivalent, not invoice
        dollars" statement, within :data:`SUMMARY_BUDGET` chars; only *text* is shortened."""
        tail = note + (ALLOWANCE_SUMMARY if self.allowance else "")
        return self.fit(text, SUMMARY_BUDGET - len(tail)) + tail


def in_view(lane: Lane, ctx: AnalysisContext) -> bool:
    """Whether *lane* is analyzed: it has requests and, in a self view (``ctx.self_principal``),
    belongs to that principal or carries no principal (per lane, so shard invariance holds)."""
    if not lane.requests:
        return False
    if ctx.self_principal is None:
        return True
    return lane.requests[0].attribution.principal in (None, ctx.self_principal)


def cohorts(lanes: Iterable[Lane], ctx: AnalysisContext) -> list[Cohort]:
    """The cohorts of *lanes* in a deterministic order (lanes sorted by key inside each)."""
    groups: dict[tuple[str | None, str, str], list[Lane]] = {}
    for lane in lanes:
        if in_view(lane, ctx):
            groups.setdefault(cohort_key(lane), []).append(lane)
    out = []
    for (team, kind, bclass), members in sorted(
            groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][0] is None, kv[0][1],
                                            kv[0][2])):
        out.append(Cohort(team, kind, bclass,
                          tuple(sorted(members, key=lambda lane: lane.lane_key))))
    return out


def audience(ctx: AnalysisContext) -> str:
    """``self`` in a self view (``ctx.self_principal``), else ``org``."""
    return "self" if ctx.self_principal is not None else "org"


def lane_principals(lane: Lane) -> set[str]:
    """Distinct principals of a lane's requests (pseudonyms; never listed in output)."""
    return {r.attribution.principal for r in lane.requests if r.attribution.principal}


def transitions(lane: Lane, ctx: AnalysisContext) -> list[Transition]:
    """``core.transitions.classify_transitions`` of *lane* under *ctx*."""
    return classify_transitions(lane, pricer=ctx.pricer, rules=ctx.rules,
                                static_prefix_floor=ctx.static_prefix_floor or None)


def serving_steps(lane: Lane) -> list[Request]:
    """The lane's requests that have a serving inference (the positions ``Transition.index``
    counts)."""
    return [r for r in lane.requests if r.serving_inference is not None]


def serving(req: Request) -> Inference:
    """The serving inference of a request that has one."""
    inf = req.serving_inference
    if inf is None:  # pragma: no cover - callers pass serving steps only
        raise ValueError("request without a serving inference")
    return inf


def event_attr(event: LaneEvent, key: str) -> object:
    """One attr of a lane event (None when absent)."""
    for k, v in event.attrs:
        if k == key:
            return v
    return None


def is_claude_code(lane: Lane) -> bool:
    """True when the lane's first request comes from Claude Code."""
    return bool(lane.requests) and lane.requests[0].attribution.agent_product == "claude_code"


def lane_model(lane: Lane) -> str:
    """The model of the lane's first request with a serving inference (else of its first
    request) — the lane a ``model:`` selector matches."""
    for req in lane.requests:
        if req.serving_inference is not None:
            return req.model
    return lane.requests[0].model if lane.requests else ""


def model_family(model: str) -> str | None:
    """``opus`` / ``sonnet`` / ``haiku`` / ``fable`` / ``mythos`` from a normalized Claude id."""
    for family in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if family in model:
            return family
    return None


def premium_family(model: str) -> bool:
    """True for Opus/Fable/Mythos-class models (:data:`PREMIUM_FAMILIES`)."""
    return model_family(model) in PREMIUM_FAMILIES


#: Upper bound of every numeric threshold (tokens, seconds, counts, USD, multiples): larger
#: values are nonsense and would only produce giant integers in generated text.
_THRESHOLD_MAX = Decimal(2**53)


def _bounded(ctx: AnalysisContext, key: str, default: str) -> Decimal:
    value = threshold(ctx, key, default)
    if abs(value) > _THRESHOLD_MAX:
        raise UsageError(f"threshold {key}: out of range")
    return value


def int_threshold(ctx: AnalysisContext, key: str, default: int) -> int:
    """A non-negative integer threshold ``ctx.thresholds[key]`` (else *default*); decimals are
    truncated toward zero; negative or absurdly large (> 2**53) values raise ``UsageError``."""
    value = _bounded(ctx, key, str(default))
    if value < 0:
        raise UsageError(f"threshold {key}: must not be negative")
    return int(value)


def share_threshold(ctx: AnalysisContext, key: str, default: str) -> Decimal:
    """A share threshold in [0, 1] ``ctx.thresholds[key]`` (else *default*); out of range raises
    ``UsageError``."""
    value = threshold(ctx, key, default)
    if not 0 <= value <= 1:
        raise UsageError(f"threshold {key}: must be a share between 0 and 1")
    return value


def positive_threshold(ctx: AnalysisContext, key: str, default: str) -> Decimal:
    """A threshold that must be > 0 (a multiple, a rate) and ≤ 2**53; otherwise
    ``UsageError``."""
    value = _bounded(ctx, key, default)
    if value <= 0:
        raise UsageError(f"threshold {key}: must be positive")
    return value


def usd_threshold_nano(ctx: AnalysisContext, key: str, default: str) -> int:
    """A non-negative USD threshold (≤ 2**53 USD) as int nano-USD, rounded half-even once."""
    value = _bounded(ctx, key, default)
    if value < 0:
        raise UsageError(f"threshold {key}: must not be negative")
    return round_fraction(Fraction(value) * 10**9)


def evidence_item(kind: str, ref: str, **attrs: str | int | None) -> EvidenceItem:
    """An :class:`EvidenceItem` with attrs sorted by key (None values dropped)."""
    pairs = tuple(sorted((k, v) for k, v in attrs.items() if v is not None))
    return EvidenceItem(kind=kind, ref=ref, attrs=pairs)


def decimal_str(value: Fraction | Decimal | int, places: int = 1) -> str:
    """A half-even decimal string of *value* with *places* decimals (display only)."""
    if isinstance(value, Fraction):
        value = RATIO_CTX.divide(Decimal(value.numerator), Decimal(value.denominator))
    elif isinstance(value, int):
        value = Decimal(value)
    if not value.is_finite() or value.adjusted() > 60:
        return str(value)
    context = Context(prec=max(28, value.adjusted() + places + 3), rounding=ROUND_HALF_EVEN)
    return str(value.quantize(Decimal(1).scaleb(-places), context=context))


def pct(num: int, den: int) -> str:
    """``100·num/den`` with one decimal (``"0.0"`` when *den* is 0)."""
    return decimal_str(Fraction(100 * num, den), 1) if den else "0.0"


def nearest_rank(values: Sequence[int], p: int) -> int:
    """Nearest-rank percentile ``p`` (0–100) of a non-empty sequence of ints."""
    ordered = sorted(values)
    rank = max(1, -(-p * len(ordered) // 100))
    return ordered[min(rank, len(ordered)) - 1]


def opaque_ref(prefix: str, *values: str | None) -> str:
    """A content-free reference for attribution values: a single ``h_`` pseudonym verbatim,
    anything else as ``stable_id(prefix, …)`` (never a raw name)."""
    if len(values) == 1 and isinstance(values[0], str) and values[0].startswith(_HASH_PREFIX) \
            and len(values[0]) == 22 and values[0][2:].isalnum():
        return values[0]
    return stable_id(prefix, *(v or "" for v in values))


@dataclasses.dataclass
class Tally:
    """What one finding aggregates: events, lanes, principals, first sighting, observed cost,
    recoverable amount and per-event evidence."""

    events: int = 0
    lanes: dict[str, Lane] = dataclasses.field(default_factory=dict)
    principals: set[str] = dataclasses.field(default_factory=set)
    first_seen: int | None = None
    cost: Money = dataclasses.field(default_factory=Money)
    rec: Money = dataclasses.field(default_factory=Money)
    items: list[EvidenceItem] = dataclasses.field(default_factory=list)
    unpriced: int = 0

    def hit(self, lane: Lane, ts_ms: int) -> None:
        """Count one event of *lane* at *ts_ms*."""
        self.events += 1
        self.touch(lane, ts_ms)

    def touch(self, lane: Lane, ts_ms: int | None = None) -> None:
        """Record *lane* (and its principals) as affected without counting an event."""
        if lane.lane_key not in self.lanes:
            self.lanes[lane.lane_key] = lane
            self.principals |= lane_principals(lane)
        if ts_ms is not None:
            self.first_seen = ts_ms if self.first_seen is None else min(self.first_seen, ts_ms)

    def lane_list(self) -> list[Lane]:
        """The affected lanes, sorted by key."""
        return [self.lanes[k] for k in sorted(self.lanes)]


def passes_min_usd(fig: Figure | None, ctx: AnalysisContext) -> bool:
    """True when *fig* is priced and its point is at least ``min_usd``."""
    return fig is not None and fig.nano is not None and fig.nano >= min_usd_nano(ctx)


_SELECTORS: dict[str, tuple[str, ...]] = {}


def _lever_selectors(lever: catalog.LeverDef) -> tuple[str, ...]:
    got = _SELECTORS.get(lever.lever_id)
    if got is not None:
        return got
    sels = [lever.selector]
    for spec in lever.grid:
        try:
            policy = parse_policy(spec)
        except TokenbillError:  # pragma: no cover - catalog grids parse (gate F)
            continue
        sels.extend(sel for sel, _ in policy.ttl)
        sels.extend(sel for sel, _ in policy.model_remap)
        sels.extend(sel for sel, _, _ in policy.effort)
        if policy.keepalive is not None:
            sels.append(policy.keepalive[0])
    out = tuple(dict.fromkeys(sels))
    _SELECTORS[lever.lever_id] = out
    return out


def applicable_levers(kind: str, lanes: Iterable[Lane]) -> tuple[str, ...]:
    """Lever ids of ``core.catalog.levers_for_kind(kind)`` whose selector (or a grid selector)
    matches at least one of *lanes*."""
    lanes = list(lanes)
    out = []
    for lever in catalog.levers_for_kind(kind):
        sels = _lever_selectors(lever)
        if any(lane_matches(sel, lane) for sel in sels for lane in lanes):
            out.append(lever.lever_id)
    return tuple(out)


def lever_spec(lever_id: str, index: int = 0) -> str:
    """Grid entry *index* of a catalog lever (a complete policy spec, SPEC §11.1)."""
    return catalog.lever(lever_id).grid[index]


def patch(*pairs: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    """A ``Fix.config_patch`` whose keys are checked against ``core.catalog.ALLOWLIST``."""
    for key, _ in pairs:
        catalog.allowed(key)
    return tuple(pairs)


def cc_gates(*keys: str) -> tuple[str, ...]:
    """Applicability gate ``claude-code>=<version>`` from the highest allowlisted minimum version
    of *keys* (empty when none has one)."""
    versions = [catalog.allowed(k).min_version for k in keys]
    known = [v for v in versions if v]
    if not known:
        return ()
    best = max(known, key=lambda v: tuple(int(p) for p in v.split(".") if p.isdigit()))
    return (f"claude-code>={best}",)


def settings_doc(key: str) -> str | None:
    """The documentation URL of an allowlisted settings key."""
    return catalog.allowed(key).source or None


def replay_mode(ctx: AnalysisContext) -> str:
    """``calibrated`` when the model gate passed, else ``documented`` (SPEC §9.4)."""
    cal = ctx.calibration
    return "calibrated" if cal is not None and cal.status == "pass" else "documented"


def replay(ctx: AnalysisContext, lanes: Sequence[Lane], spec: str | Policy) -> ReplayResult | None:
    """``ctx.replayer.replay`` of *spec* on *lanes* (one cohort, one billing class); None without
    a replayer, without lanes or when the replayer refuses the input."""
    if ctx.replayer is None or not lanes:
        return None
    policy = parse_policy(spec) if isinstance(spec, str) else spec
    try:
        return ctx.replayer.replay(
            list(lanes), policy, mode=replay_mode(ctx), pricer=ctx.pricer, rules=ctx.rules,
            calibration=ctx.calibration, static_prefix_floor=ctx.static_prefix_floor or None)
    except TokenbillError:
        return None


def saving_figure(result: ReplayResult | None, basis: Basis, *, upper_bound: bool = False,
                  note: str = "") -> Figure | None:
    """The replay's saving as a recoverable figure on *basis* (None without a result or when the
    replayer labeled another basis); trajectory/upper-bound levers flag ``upper_bound``; *note*
    is appended to the replay's note."""
    if result is None:
        return None
    fig = result.saving
    if fig.basis is not basis:
        return None
    changes: dict[str, object] = {}
    if upper_bound and fig.evidence is Evidence.ESTIMATED and not fig.upper_bound:
        changes["upper_bound"] = True
    if note and fig.nano is not None:
        changes["note"] = f"{fig.note}; {note}" if fig.note else note
    return dataclasses.replace(fig, **changes) if changes else fig


@dataclasses.dataclass(frozen=True)
class Emit:
    """Everything a finding needs besides its tally."""

    kind: str
    category: str
    lever_class: str
    title: str
    summary: str
    references: tuple[str, ...]
    fix: Fix | None
    lever_ids: tuple[str, ...] = ()
    confidence: str = "medium"
    needs_eval: bool = False
    triage: bool = False
    scope_extra: Mapping[str, str | None] = dataclasses.field(default_factory=dict)
    note: str = ""          # a disclosure kept whole at the end of the summary
    n_users: int | None = None
    absolute: bool = False  # gate on |figure| (signed info figures, e.g. a rebaseline delta)


def emit(detector: Detector, ctx: AnalysisContext, cohort: Cohort, tally: Tally, spec: Emit,
         cost: Figure, recoverable: Figure | None,
         extra_evidence: Iterable[EvidenceItem] = ()) -> Finding | None:
    """Build one finding, or None when it is below ``min_usd``: the recoverable point is
    compared, or ``cost_observed`` for triage/info kinds, findings without a recoverable and
    findings whose recoverable is unpriced (kept as "unpriced", never zero, R2). Events that
    could not be priced are counted and disclosed in the summary."""
    unpriced_rec = recoverable is not None and recoverable.nano is None
    gate = cost if spec.triage or recoverable is None or unpriced_rec else recoverable
    if spec.absolute and gate.nano is not None:
        gate = dataclasses.replace(gate, nano=abs(gate.nano), low_nano=None, high_nano=None,
                                   note=gate.note or "absolute value")
    if not passes_min_usd(gate, ctx):
        return None
    scope = make_scope(**cohort.scope_dims(**spec.scope_extra))
    items = list(tally.items) + list(extra_evidence)
    note = spec.note
    if tally.unpriced:
        note += (f" {tally.unpriced} of the {tally.events} events had no priced rate and are "
                 f"left out of the dollars.")
    return build_finding(
        detector_id=detector.id,
        kind=spec.kind,
        detector_version=detector.version,
        category=spec.category,
        lever_class=spec.lever_class,
        audience=audience(ctx),
        title=cohort.title(spec.title),
        summary=cohort.summary(spec.summary, note),
        scope=scope,
        n_events=tally.events,
        n_lanes=len(tally.lanes),
        n_users=spec.n_users if spec.n_users is not None else len(tally.principals),
        first_seen_ms=tally.first_seen or 0,
        cost_observed=cost,
        recoverable=recoverable,
        lever_ids=spec.lever_ids,
        evidence=top_evidence(items),
        fix=spec.fix,
        confidence=spec.confidence,
        needs_eval=spec.needs_eval,
        references=spec.references,
    )


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """The registry order: (−recoverable point or 0, detector id, finding id)."""
    def key(f: Finding) -> tuple[int, str, str]:
        p50 = f.recoverable.nano if f.recoverable is not None and \
            f.recoverable.nano is not None else 0
        return (-p50, f.detector_id, f.finding_id)
    return sorted(findings, key=key)


def kind_enabled(ctx: AnalysisContext, needs: Mapping[str, frozenset[str]], kind: str) -> bool:
    """Whether *kind*'s extra capabilities (``needs[kind]``, R-E32) are all in
    ``ctx.capabilities``."""
    return needs.get(kind, frozenset()) <= frozenset(ctx.capabilities)


def capability_notes(detector: Detector, ctx: AnalysisContext,
                     needs: Mapping[str, frozenset[str]]) -> list[Finding]:
    """One data-quality ``missing-capabilities`` finding per kind of *needs* whose extra
    capabilities are absent from ``ctx.capabilities`` (R-E32). Detectors return these only when
    called with no lanes (the pipeline's single ``emit_missing`` call), never per shard."""
    out = []
    for kind in sorted(needs):
        missing = sorted(needs[kind] - frozenset(ctx.capabilities))
        if not missing:
            continue
        names = ", ".join(missing)
        out.append(build_finding(
            detector_id=detector.id, kind=MISSING_KIND, detector_version=detector.version,
            category="data-quality", lever_class="none", audience=audience(ctx),
            title=f"{detector.id}: {kind} needs {names}"[:MAX_TITLE],
            summary=(f"Kind {kind} of {detector.id} did not run: the loaded sources lack {names}. "
                     f"{NO_FIX}; add a source that provides them.")[:MAX_SUMMARY],
            scope=make_scope(kind=kind), n_events=0, n_lanes=0, n_users=0,
            first_seen_ms=ctx.window[0], cost_observed=unpriced("capabilities missing"),
            recoverable=None, confidence="high", references=("dq.missing-capabilities",)))
    return out


# =============================================================================================
# context.size-tax
# =============================================================================================

_SIZE_GRID = (100_000, 200_000, 400_000)
_SIZE_REFS = ("cc-context-size-driver", "context-tax-residency")


class SizeTax:
    """``context.size-tax`` (SPEC §10.2, info): per cohort, the exact decomposition of what the
    context above ``X`` tokens cost,
    ``Σ max(0, R_i − X)·r + Σ max(0, min(W_i, T_i − X))·wτ`` for X ∈ {100k, 200k, 400k}
    (writes above ``X`` are the prompt's tail, priced at their billed write buckets),
    on each request's serving inference; the context p50/p90; the share of calls and of dollars
    at ≥ 200k and ≥ 400k. ``cost_observed`` is the decomposition at ``X`` =
    ``context.size-tax.threshold_tokens`` (default 200,000) — EXACT (a range only for
    unknown-TTL writes); no recoverable (links ``cc.autocompact_window``). Needs only
    ``usage_sequence``.
    """

    id = "context.size-tax"
    version = DETECTOR_VERSION
    kinds = ("context-tax",)
    requires = frozenset({"usage_sequence"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """At most one ``context-tax`` finding per cohort at or above ``min_usd``."""
        prices = Prices(ctx.pricer)
        x_main = int_threshold(ctx, f"{self.id}.threshold_tokens", 200_000)
        grid = tuple(sorted({*_SIZE_GRID, x_main}))
        out = []
        for cohort in cohorts(lanes, ctx):
            found = self._cohort(ctx, prices, cohort, x_main, grid)
            if found is not None:
                out.append(found)
        return sort_findings(out)

    def _cohort(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort, x_main: int,
                grid: tuple[int, ...]) -> Finding | None:
        taxes = {x: Money() for x in grid}
        tally = Tally()
        sizes: list[int] = []
        spend = 0
        calls_at = Counter[int]()
        spend_at = Counter[int]()
        for lane in cohort.lanes:
            for req in lane.requests:
                inf = req.serving_inference
                if inf is None:
                    continue
                u = inf.usage
                total = u.total_input
                ts = req.ts_start_ms
                cost = prices.request(req)
                parts = {}
                for x in grid:
                    parts[x] = combine(
                        (1, prices.line(inf.pricing, ts, "cache_read", max(0, u.cache_read - x))),
                        (1, prices.written(inf.pricing, ts, u,
                                           max(0, min(u.cache_write, total - x)), TAIL_ORDER)))
                if cost is None or any(p is None for p in parts.values()):
                    if total > x_main:
                        tally.hit(lane, ts)
                        tally.unpriced += 1
                    continue
                sizes.append(total)
                spend += cost.point
                for x in grid:
                    part = parts[x]
                    assert part is not None
                    taxes[x].add_money(part)
                for x in (200_000, 400_000):
                    if total >= x:
                        calls_at[x] += 1
                        spend_at[x] += cost.point
                if total > x_main:
                    tally.hit(lane, ts)
        if not sizes or tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        cost_fig = taxes[x_main].billed(basis)
        p50, p90 = nearest_rank(sizes, 50), nearest_rank(sizes, 90)
        items = [evidence_item("aggregate", f"size-tax:x{x // 1000}k", x_tokens=x,
                               nano=taxes[x].point) for x in grid]
        items.append(evidence_item("aggregate", "size-tax:context", calls=len(sizes),
                                   p50_tokens=p50, p90_tokens=p90, max_tokens=max(sizes)))
        for x in (200_000, 400_000):
            items.append(evidence_item("aggregate", f"size-tax:share-{x // 1000}k",
                                       calls_pct=pct(calls_at[x], len(sizes)),
                                       spend_pct=pct(spend_at[x], spend)))
        claude_code = any(is_claude_code(lane) for lane in cohort.lanes)
        text = ("Context beyond the working set is re-read on every call: compact or /clear at "
                "task boundaries; autoCompactWindow lowers the automatic compaction point "
                "(trade-off, see context.compaction-window).")
        fix = Fix(text=text, config_patch=None,
                  target="claude-code-managed-settings" if claude_code else "code",
                  doc_url=settings_doc("autoCompactWindow") if claude_code else None)
        spec = Emit(
            kind="context-tax", category="attribution", lever_class="none",
            title=f"Context size tax in {cohort.label()} lanes",
            summary=(f"Context above {x_main // 1000}k tokens: exact decomposition of its billed "
                     f"reads and writes over {len(sizes)} calls; context p50 {p50:,}, p90 "
                     f"{p90:,} tokens; {pct(calls_at[200_000], len(sizes))}% of calls and "
                     f"{pct(spend_at[200_000], spend)}% of spend at >= 200k."),
            references=_SIZE_REFS, fix=fix, triage=True, confidence="high",
            lever_ids=applicable_levers("context-tax", cohort.lanes))
        return emit(self, ctx, cohort, tally, spec, cost_fig, None, items)


# =============================================================================================
# context.compaction-window
# =============================================================================================

_WINDOW_GRID = (200_000, 300_000, 400_000, 500_000, 700_000)
_WINDOW_REFS = ("cc-compaction-threshold-sim", "cc-autocompact-window")
#: Trajectory realization-rate prior p50 (SPEC §11.2): the grid is ranked RR-adjusted.
_TRAJECTORY_RR_P50 = Fraction(1, 2)


class CompactionWindow:
    """``context.compaction-window`` (SPEC §10.2, §9.3.3): per MAIN cohort, the main lanes of
    1M-context models (``Pricer.supports(…, "1m_context")``) whose largest context exceeds the
    smallest grid value are replayed (``ctx.replayer``) under ``compact-window=w`` for w ∈ {200k,
    300k, 400k, 500k, 700k} (with ``post=<S_c>`` when ``ctx.thresholds[
    "context.compaction-window.post_tokens"]`` carries the org median, R-E24). A window is
    eligible when ``w ≥ min_window`` (``context.compaction-window.min_window``, default 300,000)
    and the projected extra compactions per session are at most
    ``context.compaction-window.max_extra_compactions`` (default 3); the best RR-adjusted p50
    saving is recommended. ``cost_observed`` is the replayed lanes' observed spend; the
    recoverable is ESTIMATED, trade-off, ``needs_eval``, ``upper_bound``; the full curve is in the
    evidence. No findings without a replayer. Needs ``usage_sequence``, ``timing``.
    """

    id = "context.compaction-window"
    version = DETECTOR_VERSION
    kinds = ("compaction-window",)
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """At most one recommendation per MAIN cohort."""
        if ctx.replayer is None:
            return []
        prices = Prices(ctx.pricer)
        min_window = int_threshold(ctx, f"{self.id}.min_window", 300_000)
        max_extra = Fraction(positive_threshold(ctx, f"{self.id}.max_extra_compactions", "3"))
        post = ctx.thresholds.get(f"{self.id}.post_tokens") if ctx.thresholds else None
        post_tokens = int_threshold(ctx, f"{self.id}.post_tokens", 0) if post is not None else 0
        out = []
        for cohort in cohorts(lanes, ctx):
            if cohort.lane_kind != LaneKind.MAIN.value:
                continue
            found = self._cohort(ctx, prices, cohort, min_window, max_extra, post_tokens)
            if found is not None:
                out.append(found)
        return sort_findings(out)

    def _eligible(self, ctx: AnalysisContext, lane: Lane) -> bool:
        steps = serving_steps(lane)
        if not steps:
            return False
        first = steps[0]
        try:
            supports = ctx.pricer.supports(serving(first).pricing, "1m_context",
                                           ts_ms=first.ts_start_ms)
        except TokenbillError:
            return False
        return supports and max(serving(r).usage.total_input for r in steps) > _WINDOW_GRID[0]

    def _cohort(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort, min_window: int,
                max_extra: Fraction, post_tokens: int) -> Finding | None:
        candidates = [lane for lane in cohort.lanes if self._eligible(ctx, lane)]
        targets = [lane for lane in candidates if prices.priceable(lane)]
        if not targets:
            return None
        basis = cohort.basis(ctx.pricer)
        sessions = len({lane.session_key for lane in targets})
        curve: list[tuple[int, ReplayResult | None, Fraction, bool]] = []
        for window in _WINDOW_GRID:
            policy = Policy(name="", compaction_window=(window, post_tokens or None))
            result = replay(ctx, targets, to_spec(policy))
            if result is None or result.saving.basis is not basis:
                curve.append((window, None, Fraction(0), False))
                continue
            per_session = Fraction(result.added_calls, sessions)
            ok = (window >= min_window and per_session <= max_extra
                  and result.saving.nano is not None and result.saving.nano > 0)
            curve.append((window, result, per_session, ok))
        eligible = [(w, r) for w, r, _, ok in curve if ok and r is not None]
        if not eligible:
            return None
        best_w, best = max(eligible, key=lambda wr: (
            (wr[1].saving.nano or 0) * _TRAJECTORY_RR_P50, wr[0]))
        recoverable = saving_figure(best, basis, upper_bound=True)
        if recoverable is None:  # pragma: no cover - guarded above
            return None
        tally = Tally()
        for lane in targets:
            tally.touch(lane, lane.requests[0].ts_start_ms)
        tally.events = best.added_calls
        items = []
        for window, result, per_session, ok in curve:
            items.append(evidence_item(
                "aggregate", f"compaction-window:{window // 1000}k", window=window,
                saving_nano=result.saving.nano if result is not None else None,
                added_compactions=result.added_calls if result is not None else None,
                per_session=decimal_str(per_session, 2), eligible="yes" if ok else "no",
                recommended="yes" if window == best_w else "no",
                magnitude=len(_WINDOW_GRID) - _WINDOW_GRID.index(window)))
        default_ctx = CC_AUTOCOMPACT_DEFAULT_TOKENS.value
        items.append(evidence_item("aggregate", "compaction-window:guard", sessions=sessions,
                                   min_window=min_window,
                                   max_extra_per_session=decimal_str(max_extra, 2),
                                   default_window=default_ctx if isinstance(default_ctx, int)
                                   else None, summary_tokens=post_tokens or None))
        fix = self._fix(best_w, targets)
        spec = Emit(
            kind="compaction-window", category="lever", lever_class="trajectory",
            title=f"Compact {cohort.label()} sessions at {best_w // 1000}k tokens (trade-off)",
            summary=(f"{len(targets)} main lanes of 1M-context models replayed with compaction "
                     f"windows 200k-700k: {best_w // 1000}k saves the most among windows >= "
                     f"{min_window // 1000}k with at most {decimal_str(max_extra, 0)} extra "
                     f"compactions per session (upper bound; ignores re-work and quality; needs "
                     f"an eval)."),
            references=_WINDOW_REFS, fix=fix, needs_eval=True, confidence="medium",
            lever_ids=applicable_levers("compaction-window", targets))
        return emit(self, ctx, cohort, tally, spec, best.baseline, recoverable, items)

    @staticmethod
    def _fix(window: int, lanes: Sequence[Lane]) -> Fix:
        if any(is_claude_code(lane) for lane in lanes):
            text = (f"Set autoCompactWindow to {window} (and CLAUDE_CODE_AUTO_COMPACT_WINDOW as a "
                    f"plain integer) for these developers; a trade-off: validate with ab/measure "
                    f"before rolling out.")
            return Fix(text=text,
                       config_patch=patch(("autoCompactWindow", str(window)),
                                          ("env.CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                                           f'"{window}"')),
                       target="claude-code-managed-settings",
                       doc_url=settings_doc("autoCompactWindow"),
                       gates=cc_gates("autoCompactWindow", "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"))
        text = (f"Compact (or summarize) these agents' context once it exceeds {window:,} tokens; "
                f"a trade-off: validate with ab before rolling out.")
        return Fix(text=text, config_patch=None, target="code", doc_url=None)


# =============================================================================================
# context.static-prefix
# =============================================================================================

_STATIC_REFS = ("anth-tool-search-defer", "tool-schema-bloat", "static-prefix-compression",
                "token-not-cost")


def _band() -> tuple[Fraction, Fraction, Fraction]:
    value = TOOL_SEARCH_REDUCTION_BAND.value
    assert isinstance(value, tuple)
    low, high = Fraction(value[0]), Fraction(value[1])
    return low, Fraction(7, 10), high


class StaticPrefix:
    """``context.static-prefix`` (SPEC §10.2).

    * ``static-prefix`` (info): per (team, lane kind, model, cache scope) with ``S =
      ctx.static_prefix_floor[(scope, model)]`` known, the harness cost ``Σ min(S, R_i)·r +
      Σ_{lane-first or miss} min(S, W_i)·wτ`` — ESTIMATED (``S`` is an estimate) — and its share
      of the group's spend; no recoverable (links ``cc.tool_search``).
    * ``tool-defs-bloat`` (needs ``blocks``: request fingerprints): requests whose non-deferred
      tool definitions (tools-tier ``tool_def`` blocks) exceed ``TOOL_DEFS_DEFER_THRESHOLD_TOKENS``
      (10,000; ``context.static-prefix.tool_defs_threshold``) after rescaling the blocks'
      ``est_tokens`` to the billed ``total_input``, and that defer no tool. ``cost_observed`` =
      ``Σ tokens_def × (billed reads $ + billed writes $)/(R + W)`` (the billed read/write mix;
      uncached input when nothing was cached) — ESTIMATED; ``recoverable`` = that × the tool-search
      reduction band [0.50, 0.85], point 0.70 — ESTIMATED ``upper_bound`` (levers
      ``cc.tool_search`` / ``sdk.defer_loading``).
    """

    id = "context.static-prefix"
    version = DETECTOR_VERSION
    kinds = ("static-prefix", "tool-defs-bloat", MISSING_KIND)
    requires = frozenset({"usage_sequence"})
    kind_requires: Mapping[str, frozenset[str]] = {"tool-defs-bloat": frozenset({"blocks"})}

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Static-prefix info per (model, cache scope) group and one tool-defs-bloat finding per
        cohort, each at or above ``min_usd``."""
        if not lanes:
            return capability_notes(self, ctx, self.kind_requires)
        prices = Prices(ctx.pricer)
        default = TOOL_DEFS_DEFER_THRESHOLD_TOKENS.value
        assert isinstance(default, int)
        limit = int_threshold(ctx, f"{self.id}.tool_defs_threshold", default)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            if ctx.static_prefix_floor:
                out.extend(self._static(ctx, prices, cohort))
            if kind_enabled(ctx, self.kind_requires, "tool-defs-bloat"):
                found = self._bloat(ctx, prices, cohort, limit)
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    # ---------- static-prefix ----------

    def _static(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> list[Finding]:
        floor = ctx.static_prefix_floor
        groups: dict[tuple[str, str], Tally] = {}
        spends: dict[tuple[str, str], int] = {}
        for lane in cohort.lanes:
            misses = {t.request_id for t in transitions(lane, ctx) if t.is_miss_event}
            for pos, req in enumerate(serving_steps(lane)):
                model = req.model
                s = floor.get((lane.cache_scope_key, model), 0)
                if s <= 0:
                    continue
                key = (lane.cache_scope_key, model)
                tally = groups.setdefault(key, Tally())
                inf = serving(req)
                ts = req.ts_start_ms
                u = inf.usage
                parts = [(1, prices.line(inf.pricing, ts, "cache_read", min(s, u.cache_read)))]
                if pos == 0 or req.request_id in misses:
                    parts.append((1, prices.written(inf.pricing, ts, u, min(s, u.cache_write))))
                harness = combine(*parts)
                cost = prices.request(req)
                tally.hit(lane, ts)
                if harness is None or cost is None:
                    tally.unpriced += 1
                    continue
                tally.cost.add_money(harness)
                spends[key] = spends.get(key, 0) + cost.point
        out = []
        basis = cohort.basis(ctx.pricer)
        for (scope_key, model), tally in sorted(groups.items()):
            if tally.events == tally.unpriced:
                continue
            s = floor[(scope_key, model)]
            spend = spends.get((scope_key, model), 0)
            share = pct(tally.cost.point, spend)
            cost = tally.cost.estimate(basis, "S is the static-prefix floor (median lane-first "
                                              "read), an estimate")
            item = evidence_item("aggregate", "static-prefix:floor", tokens=s,
                                 nano=tally.cost.point, spend_nano=spend, share_pct=share)
            claude_code = any(is_claude_code(lane) for lane in tally.lanes.values())
            text = ("Attribution: the harness prefix (system prompt, tool definitions, CLAUDE.md, "
                    "skills) is re-sent on every call. Fewer always-on MCP servers, "
                    "skillListingBudgetFraction, claudeMdExcludes and tool search shrink it.")
            fix = Fix(text=text, config_patch=None,
                      target="claude-code-managed-settings" if claude_code else "code",
                      doc_url=settings_doc("env.ENABLE_TOOL_SEARCH") if claude_code else None)
            spec = Emit(
                kind="static-prefix", category="attribution", lever_class="none",
                title=f"Static prefix of {s:,} tokens in {cohort.label()} lanes",
                summary=(f"The static prefix (S = {s:,} tokens, estimated) is read or written on "
                         f"every call of {model}: {share}% of this group's spend."),
                references=_STATIC_REFS, fix=fix, triage=True, confidence="low",
                lever_ids=applicable_levers("static-prefix", tally.lane_list()),
                scope_extra={"model": model, "cache_scope": scope_key})
            found = emit(self, ctx, cohort, tally, spec, cost, None, [item])
            if found is not None:
                out.append(found)
        return out

    # ---------- tool-defs-bloat ----------

    def _bloat(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort,
               limit: int) -> Finding | None:
        tally = Tally()
        d_point = Fraction(0)
        d_low = Fraction(0)
        d_high = Fraction(0)
        ranged = False
        token_sum = Fraction(0)
        for lane in cohort.lanes:
            for req in serving_steps(lane):
                fp = req.fingerprint
                if fp is None:
                    continue
                tools = [b for b in fp.blocks if b.tier == "tools" and b.kind == "tool_def"]
                if not tools or any(b.deferred for b in tools):
                    continue
                est_total = sum(b.est_tokens or 0 for b in fp.blocks)
                est_tools = sum(b.est_tokens or 0 for b in tools)
                inf = serving(req)
                u = inf.usage
                if est_total <= 0 or est_tools <= 0 or u.total_input <= 0:
                    continue
                tokens = Fraction(est_tools * u.total_input, est_total)
                if tokens <= limit:
                    continue
                ts = req.ts_start_ms
                prefix = u.cache_read + u.cache_write
                if prefix:
                    mix = combine((1, prices.line(inf.pricing, ts, "cache_read", u.cache_read)),
                                  (1, prices.written(inf.pricing, ts, u, u.cache_write)))
                    den = prefix
                else:
                    mix = prices.line(inf.pricing, ts, "uncached_input", u.uncached_input)
                    den = u.uncached_input
                tally.hit(lane, ts)
                if mix is None or den <= 0:
                    tally.unpriced += 1
                    continue
                token_sum += tokens
                d_point += tokens * Fraction(mix.point, den)
                d_low += tokens * Fraction(mix.low, den)
                d_high += tokens * Fraction(mix.high, den)
                ranged = ranged or mix.ranged
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        observed = Money(point=round_fraction(d_point), low=round_fraction(d_low),
                         high=round_fraction(d_high), ranged=ranged)
        cost = observed.estimate(basis, "tool-definition tokens (est_tokens rescaled to billed "
                                        "input) at the billed read/write mix")
        low_b, point_b, high_b = _band()
        rec = estimated(round_fraction(d_point * point_b), basis,
                        low=round_fraction(d_low * low_b), high=round_fraction(d_high * high_b),
                        upper_bound=True,
                        note=f"tool-search reduction band [{decimal_str(low_b, 2)}, "
                             f"{decimal_str(high_b, 2)}], point {decimal_str(point_b, 2)}")
        mean = token_sum / max(1, tally.events - tally.unpriced)
        item = evidence_item("aggregate", "tool-defs-bloat:requests",
                             requests=tally.events - tally.unpriced,
                             mean_tool_def_tokens=round_fraction(mean), threshold_tokens=limit,
                             nano=observed.point)
        lanes = tally.lane_list()
        claude_code = any(is_claude_code(lane) for lane in lanes)
        if claude_code:
            fix = Fix(text=("Turn on tool search (ENABLE_TOOL_SEARCH=true, also behind "
                            "non-first-party gateways) and trim always-on MCP servers."),
                      config_patch=patch(("env.ENABLE_TOOL_SEARCH", '"true"')),
                      target="claude-code-managed-settings",
                      doc_url=settings_doc("env.ENABLE_TOOL_SEARCH"))
        else:
            fix = Fix(text=("Load tools on demand: defer_loading: true on the tool definitions "
                            "(tool search) when an agent has 10 or more tools or more than 10k "
                            "tokens of definitions."),
                      config_patch=None, target="sdk", doc_url=API_CACHE_DOC)
        spec = Emit(
            kind="tool-defs-bloat", category="lever", lever_class="cache_transform",
            title=f"Tool definitions bloat every {cohort.label()} call",
            summary=(f"{tally.events} calls carried more than {limit:,} tokens of non-deferred "
                     f"tool definitions (mean {round_fraction(mean):,}); tool search removes "
                     f"50-85% of them (estimated upper bound)."),
            references=_STATIC_REFS, fix=fix, confidence="medium",
            lever_ids=applicable_levers("tool-defs-bloat", lanes))
        return emit(self, ctx, cohort, tally, spec, cost, rec, [item])


# =============================================================================================
# attrib.carry
# =============================================================================================

_CARRY_REFS = ("cc-tool-output-carry", "cc-config-sprawl-tax", "verified-savings-gap-rtk")
_TOP_SHARE = Fraction(5, 100)


@dataclasses.dataclass
class _Item:
    """One carried item: its group name, tokens and carry cost."""

    name: str
    tokens: int
    cost: Money
    later: int


class Carry:
    """``attrib.carry`` (SPEC §10.2): what content entering the context costs while it stays.

    Bytes become tokens with ``t = ceil(n_bytes / cpt)``, ``cpt`` from ``core.findings.fit_cpt``
    over the cohort's lanes of each tokenizer family (≥ 30 samples, else the published default
    2.5 / 3.3 bytes per token). An item entering at request ``i`` costs ``t·wτ`` (written once at
    request ``i``'s billed write bucket) plus ``t·r`` on each later request of the lane before the
    next reset (COMPACTION / CLEAR / CONTEXT_EDIT event, applied edits) — ESTIMATED.

    * ``tool-output-carry`` (needs ``appended``): every appended ``tool_result`` item, with the
      per-tool breakdown and the share of the top 5% of items in the evidence.
    * ``config-tax`` (needs ``events``): the first CONTEXT_INJECTION of each attachment type per
      lane, landing on the first request at or after the event.

    No recoverable (trajectory; verify with ``ab``).
    """

    id = "attrib.carry"
    version = DETECTOR_VERSION
    kinds = ("tool-output-carry", "config-tax", MISSING_KIND)
    requires = frozenset({"usage_sequence", "appended"})
    kind_requires: Mapping[str, frozenset[str]] = {"config-tax": frozenset({"events"})}

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """One finding per kind and cohort at or above ``min_usd``."""
        if not lanes:
            return capability_notes(self, ctx, self.kind_requires)
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            cpts = self._cpts(ctx, cohort)
            for kind in ("tool-output-carry", "config-tax"):
                if not kind_enabled(ctx, self.kind_requires, kind):
                    continue
                found = self._carry(ctx, prices, cohort, cpts, kind)
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    @staticmethod
    def _family(ctx: AnalysisContext, lane: Lane) -> str:
        steps = serving_steps(lane)
        if not steps:
            return "claude-4.7+"
        try:
            fam = ctx.pricer.tokenizer_family(serving(steps[0]).pricing,
                                              ts_ms=steps[0].ts_start_ms)
        except TokenbillError:
            fam = None
        return fam or "claude-4.7+"

    def _cpts(self, ctx: AnalysisContext, cohort: Cohort) -> dict[str, tuple[Decimal, int]]:
        by_family: dict[str, list[Lane]] = {}
        for lane in cohort.lanes:
            by_family.setdefault(self._family(ctx, lane), []).append(lane)
        return {fam: fit_cpt(members, fam) for fam, members in sorted(by_family.items())}

    def _items(self, ctx: AnalysisContext, prices: Prices, lane: Lane, cpt: Decimal,
               kind: str) -> list[tuple[Request, _Item | None]]:
        steps = serving_steps(lane)
        resets = [ev.ts_ms for ev in lane.events if ev.kind in RESET_EVENTS]
        # later[i]: requests after i before the next reset (a reset event in (ts_{j-1}, ts_j] or
        # a request with applied edits / dropped thinking), in one backward pass
        later = [0] * len(steps)
        run = 0
        for i in range(len(steps) - 1, 0, -1):
            later[i] = run
            lo, hi = steps[i - 1].ts_start_ms, steps[i].ts_start_ms
            first = bisect_right(resets, lo)
            reset = any(a.applied_edits or a.thinking_dropped for a in steps[i].attempts) or (
                first < len(resets) and resets[first] <= hi)
            run = 0 if reset else run + 1
        if steps:
            later[0] = run
        found: list[tuple[int, str, int]] = []   # (step index, name, bytes)
        if kind == "tool-output-carry":
            for i, req in enumerate(steps):
                for item in req.appended:
                    if item.kind == "tool_result" and item.n_bytes > 0:
                        found.append((i, item.name or "unnamed", item.n_bytes))
        else:
            seen: set[str] = set()
            starts = [r.ts_start_ms for r in steps]
            for ev in lane.events:
                if ev.kind is not LaneEventKind.CONTEXT_INJECTION:
                    continue
                att = event_attr(ev, "att_type")
                size = event_attr(ev, "n_bytes")
                if not isinstance(att, str) or type(size) is not int or size <= 0 or att in seen:
                    continue
                seen.add(att)
                idx = next((j for j, ts in enumerate(starts) if ts >= ev.ts_ms), None)
                if idx is not None:
                    found.append((idx, att, size))
        out: list[tuple[Request, _Item | None]] = []
        for i, name, n_bytes in found:
            req = steps[i]
            inf = serving(req)
            tokens = int((Decimal(n_bytes) / cpt).to_integral_value(rounding=ROUND_CEILING))
            ts = req.ts_start_ms
            cost = combine((1, prices.line(inf.pricing, ts, write_bucket(inf.usage), tokens)),
                           (1, prices.line(inf.pricing, ts, "cache_read", tokens * later[i])))
            out.append((req, None if cost is None else _Item(name, tokens, cost, later[i])))
        return out

    def _carry(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort,
               cpts: Mapping[str, tuple[Decimal, int]], kind: str) -> Finding | None:
        tally = Tally()
        items: list[_Item] = []
        for lane in cohort.lanes:
            cpt, _ = cpts[self._family(ctx, lane)]
            for req, item in self._items(ctx, prices, lane, cpt, kind):
                tally.hit(lane, req.ts_start_ms)
                if item is None:
                    tally.unpriced += 1
                    continue
                items.append(item)
                tally.cost.add_money(item.cost)
        if not items:
            return None
        basis = cohort.basis(ctx.pricer)
        fits = ", ".join(f"{fam} {decimal_str(c, 2)} ({'fit' if n >= 30 else 'default'})"
                         for fam, (c, n) in sorted(cpts.items()))
        cost = tally.cost.estimate(basis, f"bytes per token: {fits}; carried until the next "
                                          f"reset")
        by_name: dict[str, list[_Item]] = {}
        for item in items:
            by_name.setdefault(item.name, []).append(item)
        ranked = sorted((c.cost.point for c in items), reverse=True)
        top_n = max(1, round_fraction(len(ranked) * _TOP_SHARE))
        top_share = pct(sum(ranked[:top_n]), sum(ranked))
        prefix = "tool" if kind == "tool-output-carry" else "type"
        groups = sorted(by_name.items(), key=lambda kv: (-sum(g.cost.point for g in kv[1]),
                                                         kv[0]))
        evidence = [evidence_item("aggregate", f"{prefix}:{name}", items=len(group),
                                  tokens=sum(g.tokens for g in group),
                                  nano=sum(g.cost.point for g in group))
                    for name, group in groups[:19]]
        evidence.append(evidence_item("aggregate", "carry:distribution", items=len(items),
                                      top_5pct_items=top_n, top_5pct_share_pct=top_share,
                                      bytes_per_token=fits))
        if kind == "tool-output-carry":
            title = f"Tool output carried through {cohort.label()} conversations"
            summary = (f"{len(items)} tool results stayed in context until the next reset "
                       f"(bytes to tokens with the fitted or published bytes-per-token); the "
                       f"largest 5% of results carry {top_share}% of the cost (estimated).")
            text = ("Cap tool output: bashOutputMaxChars and MAX_MCP_OUTPUT_TOKENS, PostToolUse "
                    "filter hooks for noisy tools; verify the change with ab (trajectory).")
        else:
            title = f"Context injections re-sent in {cohort.label()} conversations"
            summary = (f"{len(items)} first context injections per lane (CLAUDE.md, skills, MCP "
                       f"listings) are carried on every later call until the next reset "
                       f"(estimated).")
            text = ("Trim what is injected at session start: skillListingBudgetFraction, "
                    "claudeMdExcludes, fewer always-on MCP servers; verify with ab.")
        claude_code = any(is_claude_code(lane) for lane in tally.lanes.values())
        fix = Fix(text=text, config_patch=None,
                  target="claude-code-managed-settings" if claude_code else "code",
                  doc_url=settings_doc("bashOutputMaxChars") if claude_code else None)
        spec = Emit(kind=kind, category="attribution", lever_class="trajectory", title=title,
                    summary=summary, references=_CARRY_REFS, fix=fix, triage=True,
                    confidence="low")
        return emit(self, ctx, cohort, tally, spec, cost, None, evidence)

"""Cache-miss detectors and the machinery shared by the cache detectors (SPEC §10.1, §10.2;
package DETECT-CACHE).

Detectors (registry paths of SPEC §3.7):

* :class:`MissByCause` (``cache.miss-by-cause``) — every miss event of ``core.transitions`` by
  cause: the rewrite actually billed (EXACT) and the triage premium over a warm read (ESTIMATED,
  upper bound; none for ``compaction``, an expected rebuild).
* :class:`SwitchChurn` (``cache.switch-churn``) — model-switch and parameter-change misses by
  sub-cause (refusal fallback without credit, availability ping-pong, opusplan plan toggles, user
  switches, fast-mode toggles, effort changes that invalidate the cache per D28), with the linked
  repair replayed where one exists.
* :class:`RebuildEvents` (``cache.rebuild``) — compactions after the cache went cold and context
  edits made while warm that do not pay back (edit churn, with ``K* = S(α − β)/(Xβ)``).

The helpers in the first half of this module (bucket prices, the money accumulator, cohorts,
finding assembly, lever applicability, replay) are shared with ``detect.cache_ttl`` and
``detect.cache_structure``; they are internal to DETECT-CACHE.

Conventions (SPEC §10.1): cross-lane logic stays inside ``core.findings.cohort_key`` cohorts
``(team, lane_kind, billing_class)`` (or a finer key inside one), so running per shard and
concatenating equals one run; findings aggregate at (team, lane kind, kind) plus ``billing_class``
when it is ``allowance`` — those carry basis LIST_EQUIVALENT, an "Allowance headroom:" title and a
"list-equivalent, not invoice dollars" summary; ``cost_observed`` is EXACT only for billed
arithmetic; money is int nano-USD (no floats); ``min_usd`` gates every finding (the recoverable
point, or ``cost_observed`` for triage and info kinds); findings are returned unpublished (the
caller applies ``core.kanon.rescope_findings``). Only token counts, timestamps, enums and
pseudonymous ids are read — never content.
"""

from __future__ import annotations

import dataclasses
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from tokenbill.core import catalog
from tokenbill.core.errors import TokenbillError, UsageError
from tokenbill.core.findings import (
    MAX_SUMMARY,
    MAX_TITLE,
    build_finding,
    cohort_key,
    make_scope,
    min_usd_nano,
    miss_waste,
    threshold,
    top_evidence,
)
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, estimated, exact
from tokenbill.core.money import scaled_to_nano
from tokenbill.core.policy import lane_matches, parse_policy
from tokenbill.core.protocols import Detector, Pricer
from tokenbill.core.records import (
    Lane,
    LaneEvent,
    LaneEventKind,
    PricingContext,
    Request,
    UsageBuckets,
)
from tokenbill.core.textsafe import sanitize
from tokenbill.core.transitions import classify_transitions
from tokenbill.core.types import (
    AnalysisContext,
    EvidenceItem,
    Finding,
    Fix,
    ReplayResult,
    Transition,
    UnitRates,
)

__all__ = [
    "MissByCause",
    "RebuildEvents",
    "SwitchChurn",
]

DETECTOR_VERSION = "1"
DAY_MS = 86_400_000
DEFAULT_TTL_S = 300
#: The default billing class, left out of finding scopes (SPEC §10.1).
BILLED_CLASS = "billed"
#: The seat-allowance billing class (D26): "Allowance headroom:" titles and the list-equivalent
#: statement in summaries.
ALLOWANCE_CLASS = "allowance"
#: Billing classes whose figures are list-equivalent values, never invoice dollars: the seat
#: allowance (D26) and GitHub Copilot's pooled credits (R-E20). A table, so additive classes need
#: no code change.
LIST_EQUIVALENT_CLASSES = frozenset({ALLOWANCE_CLASS, "pool"})
ALLOWANCE_TITLE = "Allowance headroom: "
ALLOWANCE_SUMMARY = " Figures are list-equivalent, not invoice dollars."
#: Generated summaries stay this short so ``core.kanon.rescope_findings`` can prefix its
#: "[re-scoped for k-anonymity …]" note (≈ 60 chars) within the 400-char limit without cutting the
#: allowance statement (D26) at the end.
SUMMARY_BUDGET = MAX_SUMMARY - 70
RANGE_NOTE = "billed tokens; some lines are priced as a range (unknown TTL or endpoint scope)"
API_CACHE_DOC = "https://platform.claude.com/docs/en/build-with-claude/prompt-caching"
CC_CACHE_DOC = "https://code.claude.com/docs/en/prompt-caching"
RESET_EVENTS = frozenset({LaneEventKind.COMPACTION, LaneEventKind.CLEAR,
                          LaneEventKind.CONTEXT_EDIT})
#: Write buckets in the order a rewrite is allocated to them: longer TTLs sit before shorter ones
#: in the prompt (SPEC §19.3), so the missed prefix is the 1h part first.
WRITE_ORDER = ("cache_write_1h", "cache_write_5m", "cache_write_other", "cache_write_unknown")
_PROBE_TOKENS = 1_000
_FIELD = {"uncached_input": "uncached_input", "cache_read": "cache_read",
          "cache_write_5m": "cache_write_5m", "cache_write_1h": "cache_write_1h",
          "cache_write_other": "cache_write_other", "cache_write_unknown": "cache_write_unknown",
          "output": "output"}
_UNIT_ATTR = {"uncached_input": "uncached", "cache_read": "cache_read",
              "cache_write_5m": "cache_write_5m", "cache_write_1h": "cache_write_1h",
              "cache_write_other": "cache_write_other", "output": "output"}


# =============================================================================================
# shared machinery (DETECT-CACHE internal)
# =============================================================================================


class Prices:
    """Bucket prices through a :class:`~tokenbill.core.protocols.Pricer` — the ``r``, ``w5``,
    ``w1``, ``u`` rates of the SPEC §10.1 formulas for a request's pricing context — as
    :class:`Money`.

    A line is exact exactly when the pricer prices it exactly (R9): those use the pricer's exact
    integer unit rates, cached per (pricing context, UTC day), which agree with
    ``Pricer.price_usage`` to the nano (§6.4); range lines (unknown-TTL writes, unknown endpoint
    scope, …) carry the pricer's own ``[low, high]``. ``None`` means unpriceable: unknown is
    never zero.
    """

    def __init__(self, pricer: Pricer) -> None:
        self.pricer = pricer
        self._units: dict[tuple[PricingContext, int], UnitRates | None] = {}
        self._exact: dict[tuple[PricingContext, int, str], bool | None] = {}

    def unit(self, pricing: PricingContext, ts_ms: int) -> UnitRates | None:
        """The pricer's exact unit rates for *pricing* on the day of *ts_ms* (cached)."""
        key = (pricing, ts_ms // DAY_MS)
        if key not in self._units:
            try:
                self._units[key] = self.pricer.unit_rates(pricing, ts_ms=ts_ms)
            except TokenbillError:
                self._units[key] = None
        return self._units[key]

    def _priced(self, pricing: PricingContext, ts_ms: int, bucket: str,
                tokens: int) -> tuple[int, int, int, bool] | None:
        """``(point, low, high, exact)`` of *tokens* of *bucket* through ``price_usage``."""
        kwargs: dict[str, int] = {_FIELD[bucket]: tokens}
        if bucket == "cache_write_other":
            kwargs["cache_write_other_ttl_s"] = 1800   # informational: the rate is per bucket
        try:
            priced = self.pricer.price_usage(UsageBuckets(**kwargs), pricing, ts_ms=ts_ms)
        except TokenbillError:
            return None
        if priced.unpriced_reason is not None or priced.figure.nano is None:
            return None
        for ln in priced.lines:
            if ln.bucket == bucket:
                low = ln.low_nano if ln.low_nano is not None else ln.amount_nano
                high = ln.high_nano if ln.high_nano is not None else ln.amount_nano
                return (ln.amount_nano, min(low, ln.amount_nano), max(high, ln.amount_nano),
                        ln.exact)
        return None

    def line(self, pricing: PricingContext, ts_ms: int, bucket: str,
             tokens: int) -> Money | None:
        """*tokens* of *bucket* at *pricing*'s rates on the day of *ts_ms*."""
        money = Money()
        if tokens <= 0:
            return money
        key = (pricing, ts_ms // DAY_MS, bucket)
        if key not in self._exact:
            probe = self._priced(pricing, ts_ms, bucket, _PROBE_TOKENS)
            self._exact[key] = None if probe is None else probe[3]
        exact = self._exact[key]
        if exact is None:
            return None
        attr = _UNIT_ATTR.get(bucket)
        unit = self.unit(pricing, ts_ms) if exact and attr is not None else None
        if unit is not None and attr is not None:
            money.add(scaled_to_nano(tokens * getattr(unit, attr), unit.scale_exp))
            return money
        priced = self._priced(pricing, ts_ms, bucket, tokens)
        if priced is None:
            return None
        point, low, high, is_exact = priced
        if is_exact:
            money.add(point)
        else:
            money.add_range(point, low, high)
        return money

    def written(self, pricing: PricingContext, ts_ms: int, usage: UsageBuckets,
                tokens: int) -> Money | None:
        """*tokens* of a request's billed writes at the billed write rates (``w_billed``): the
        tokens are allocated to the 1h, 5m, other-TTL and unknown-TTL buckets in that order (see
        :data:`WRITE_ORDER`); unknown-TTL tokens are the pricer's [5m, 1h] range (R5). None when
        unpriceable."""
        remaining = max(0, min(tokens, usage.cache_write))
        money = Money()
        for bucket in WRITE_ORDER:
            take = min(getattr(usage, bucket), remaining)
            if take <= 0:
                continue
            remaining -= take
            part = self.line(pricing, ts_ms, bucket, take)
            if part is None:
                return None
            money.add_money(part)
        return money

    def write_rate_bucket(self, usage: UsageBuckets) -> str:
        """The write bucket a request's writes were (mostly) billed at: the first non-empty
        bucket of :data:`WRITE_ORDER`, else ``cache_write_5m``."""
        for bucket in WRITE_ORDER:
            if getattr(usage, bucket):
                return bucket
        return "cache_write_5m"


def priceable_lanes(prices: Prices, lanes: Iterable[Lane]) -> tuple[list[Lane], int]:
    """``(lanes whose every billable inference has a priced rate, number left out)``: a replay
    over a lane with an unpriced inference has an unpriced baseline (R2), so cohort-wide replays
    run on the priceable lanes and disclose the rest instead of losing the whole cohort."""
    keep: list[Lane] = []
    dropped = 0
    for lane in lanes:
        if all(prices.line(inf.pricing, req.ts_start_ms, "uncached_input", 1) is not None
               for req in lane.requests for inf in req.billable_inferences):
            keep.append(lane)
        else:
            dropped += 1
    return keep, dropped


def combine(*parts: tuple[int, Money | None]) -> Money | None:
    """``Σ sign·money`` (ranges crosswise for negative signs); None if any part is None."""
    total = Money()
    for sign, money in parts:
        if money is None:
            return None
        total.add_money(money, sign)
    return total


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
        self.low += low
        self.high += high
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

    def floor_at_zero(self) -> None:
        """Clamp the point and both bounds at 0 (a loss is never negative)."""
        self.point, self.low, self.high = max(0, self.point), max(0, self.low), max(0, self.high)

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
        """True for a seat-allowance cohort (D26)."""
        return self.billing_class == ALLOWANCE_CLASS

    def basis(self, pricer: Pricer) -> Basis:
        """LIST_EQUIVALENT for the list-equivalent billing classes
        (:data:`LIST_EQUIVALENT_CLASSES`), else the pricer's billed basis."""
        if self.billing_class in LIST_EQUIVALENT_CLASSES:
            return Basis.LIST_EQUIVALENT
        return pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST

    def scope_dims(self, **extra: str | None) -> dict[str, str | None]:
        """Scope dims: team, lane kind, ``billing_class`` unless it is the default ``billed``
        (so an allowance cohort — or any additive class — never shares a finding id with the
        billed cohort of the same team and lane kind), plus *extra*."""
        dims: dict[str, str | None] = {
            "team": self.team, "lane_kind": self.lane_kind,
            "billing_class": None if self.billing_class == BILLED_CLASS else self.billing_class}
        dims.update(extra)
        return dims

    def label(self) -> str:
        """``"<team> <lane kind>"`` for generated text."""
        return f"{team_label(self.team)} {self.lane_kind}"

    def fit(self, text: str, limit: int) -> str:
        """*text* within *limit* chars without ever cutting a scope value: ``core.kanon`` scrubs
        dropped scope values from generated text as whole tokens, so a team name cut in half
        would survive re-scoping. Too long, the team is dropped from the label (the scope still
        names it); still too long, the text is cut at a word boundary."""
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
        """*text* with the allowance prefix when needed, at most 120 chars (see :meth:`fit`)."""
        prefix = ALLOWANCE_TITLE if self.allowance else ""
        return prefix + self.fit(text, MAX_TITLE - len(prefix))

    def summary(self, text: str, note: str = "") -> str:
        """*text*, then *note* and (for allowance cohorts) the "list-equivalent, not invoice
        dollars" statement, within :data:`SUMMARY_BUDGET` chars: only *text* is ever shortened
        (see :meth:`fit`), so the note and the allowance statement always survive."""
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


def usage_of(req: Request) -> UsageBuckets:
    """The serving inference's usage of a request that has one."""
    inf = req.serving_inference
    if inf is None:  # pragma: no cover - callers pass serving steps only
        raise ValueError("request without a serving inference")
    return inf.usage


def serving_steps(lane: Lane) -> list[Request]:
    """The lane's requests that have a serving inference (the positions ``Transition.index``
    counts)."""
    return [r for r in lane.requests if r.serving_inference is not None]


def events_between(lane: Lane, lo_ms: int, hi_ms: int) -> list[LaneEvent]:
    """Lane events with ``lo_ms < ts ≤ hi_ms``."""
    return EventIndex(lane).between(lo_ms, hi_ms)


class EventIndex:
    """A lane's events with binary search on their timestamps (``Lane`` keeps events sorted by
    ``(ts, kind, attrs)``), so per-transition windows cost O(log m) instead of a scan."""

    def __init__(self, lane: Lane) -> None:
        self.events = lane.events
        self.ts = [ev.ts_ms for ev in lane.events]
        self.resets = [ev.ts_ms for ev in lane.events if ev.kind in RESET_EVENTS]

    def between(self, lo_ms: int, hi_ms: int) -> list[LaneEvent]:
        """Events with ``lo_ms < ts ≤ hi_ms``."""
        return list(self.events[bisect_right(self.ts, lo_ms):bisect_right(self.ts, hi_ms)])

    def reset_between(self, lo_ms: int, hi_ms: int) -> bool:
        """Whether a COMPACTION, CLEAR or CONTEXT_EDIT event has ``lo_ms < ts ≤ hi_ms``."""
        first = bisect_right(self.resets, lo_ms)
        return first < len(self.resets) and self.resets[first] <= hi_ms


def int_threshold(ctx: AnalysisContext, key: str, default: int) -> int:
    """A non-negative integer threshold ``ctx.thresholds[key]`` (else *default*); decimals are
    truncated toward zero, negative values raise ``UsageError``."""
    value = threshold(ctx, key, str(default))
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


def event_attr(event: LaneEvent, key: str) -> object:
    """One attr of a lane event (None when absent)."""
    for k, v in event.attrs:
        if k == key:
            return v
    return None


def gateway_of(lane: Lane) -> str | None:
    """``attribution.extra["gateway"]`` of the lane's first request (a non-first-party base URL)."""
    if not lane.requests:
        return None
    for key, value in lane.requests[0].attribution.extra:
        if key == "gateway" and value:
            return value
    return None


def is_claude_code(lane: Lane) -> bool:
    """True when the lane's first request comes from Claude Code."""
    return bool(lane.requests) and lane.requests[0].attribution.agent_product == "claude_code"


def evidence_item(kind: str, ref: str, **attrs: str | int | None) -> EvidenceItem:
    """An :class:`EvidenceItem` with attrs sorted by key (None values dropped)."""
    pairs = tuple(sorted((k, v) for k, v in attrs.items() if v is not None))
    return EvidenceItem(kind=kind, ref=ref, attrs=pairs)


def decimal_str(value: Fraction | Decimal, places: int = 1) -> str:
    """A half-even decimal string of *value* with *places* decimals."""
    if isinstance(value, Fraction):
        value = Decimal(value.numerator) / Decimal(value.denominator)
    return str(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN))


def percentile(values: Sequence[Fraction | int], p: int) -> Fraction | int:
    """Nearest-rank percentile ``p`` (0–100) of a non-empty sequence."""
    ordered = sorted(values)
    rank = max(1, -(-p * len(ordered) // 100))
    return ordered[min(rank, len(ordered)) - 1]


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
    labels: Counter[str] = dataclasses.field(default_factory=Counter)

    def hit(self, lane: Lane, ts_ms: int) -> None:
        """Count one event of *lane* at *ts_ms*."""
        self.events += 1
        self.touch(lane)
        self.first_seen = ts_ms if self.first_seen is None else min(self.first_seen, ts_ms)

    def touch(self, lane: Lane) -> None:
        """Record *lane* (and its principals) as affected without counting an event."""
        if lane.lane_key not in self.lanes:
            self.lanes[lane.lane_key] = lane
            self.principals |= lane_principals(lane)

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
    matches at least one of *lanes* — e.g. the Claude Code main TTL lever only for Claude Code
    main lanes."""
    lanes = list(lanes)
    out = []
    for lever in catalog.levers_for_kind(kind):
        sels = _lever_selectors(lever)
        if any(lane_matches(sel, lane) for sel in sels for lane in lanes):
            out.append(lever.lever_id)
    return tuple(out)


def patch(*pairs: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    """A ``Fix.config_patch`` whose keys are checked against ``core.catalog.ALLOWLIST``
    (an unknown key raises ``UsageError``)."""
    for key, _ in pairs:
        catalog.allowed(key)
    return tuple(pairs)


def cc_gates(*keys: str) -> tuple[str, ...]:
    """Applicability gate ``claude-code>=<version>`` from the highest allowlisted minimum
    version of *keys* (empty when none has one)."""
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


def replay(ctx: AnalysisContext, lanes: Sequence[Lane], spec: str) -> ReplayResult | None:
    """``ctx.replayer.replay`` of the policy *spec* on *lanes* (one cohort, one billing class);
    None without a replayer or when the replayer refuses the input."""
    if ctx.replayer is None or not lanes:
        return None
    policy = parse_policy(spec)
    try:
        return ctx.replayer.replay(
            list(lanes), policy, mode=replay_mode(ctx), pricer=ctx.pricer, rules=ctx.rules,
            calibration=ctx.calibration, static_prefix_floor=ctx.static_prefix_floor or None)
    except TokenbillError:
        return None


def saving_figure(result: ReplayResult | None, basis: Basis, *,
                  upper_bound: bool = False) -> Figure | None:
    """The replay's saving as a recoverable figure on *basis* (None without a result or when the
    replayer labeled another basis); trajectory/upper-bound levers flag ``upper_bound``."""
    if result is None:
        return None
    fig = result.saving
    if fig.basis is not basis:
        return None
    if upper_bound and fig.evidence is Evidence.ESTIMATED and not fig.upper_bound:
        fig = dataclasses.replace(fig, upper_bound=True)
    return fig


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
    validated_against: str | None = None
    needs_eval: bool = False
    triage: bool = False
    scope_extra: Mapping[str, str | None] = dataclasses.field(default_factory=dict)
    note: str = ""          # a disclosure kept whole at the end of the summary


def emit(detector: Detector, ctx: AnalysisContext, cohort: Cohort, tally: Tally, spec: Emit,
         cost: Figure, recoverable: Figure | None,
         extra_evidence: Iterable[EvidenceItem] = ()) -> Finding | None:
    """Build one finding, or None when it is below ``min_usd``: the recoverable point is
    compared, or ``cost_observed`` for triage/info kinds, findings without a recoverable and
    findings whose replayed recoverable is unpriced (kept as "unpriced", never zero, R2). Events
    that could not be priced are counted and disclosed in the summary."""
    unpriced_rec = recoverable is not None and recoverable.nano is None
    gate = cost if spec.triage or recoverable is None or unpriced_rec else recoverable
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
        n_users=len(tally.principals),
        first_seen_ms=tally.first_seen or 0,
        cost_observed=cost,
        recoverable=recoverable,
        lever_ids=spec.lever_ids,
        evidence=top_evidence(items),
        fix=spec.fix,
        confidence=spec.confidence,
        validated_against=spec.validated_against,
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


def miss_money(prices: Prices, t: Transition, req: Request) -> tuple[Money, Money] | None:
    """The billed rewrite of a miss, ``mw·w_billed + mu·u`` with ``(mw, mu) =
    core.findings.miss_waste`` (EXACT unless unknown-TTL writes), and its triage premium over a
    warm read ``mw·(w − r) + mu·(u − r)`` (the same lines minus the read price of ``mw + mu``).
    None when unpriceable."""
    inf = req.serving_inference
    if inf is None:
        return None
    mw, mu = miss_waste(t, req)
    ts = req.ts_start_ms
    rewrite = combine((1, prices.written(inf.pricing, ts, inf.usage, mw)),
                      (1, prices.line(inf.pricing, ts, "uncached_input", mu)))
    premium = combine((1, rewrite), (-1, prices.line(inf.pricing, ts, "cache_read", mw + mu)))
    if rewrite is None or premium is None:
        return None
    return rewrite, premium


def validated(labels: Counter[str], expected: str | None) -> str | None:
    """``"cache_miss_reason <reason>: agree/labeled"`` when server diagnostics labeled some of
    the events (SPEC §3.5 ``Finding.validated_against``)."""
    labeled = sum(labels.values())
    if expected is None or labeled == 0:
        return None
    return f"cache_miss_reason {expected}: {labels.get(expected, 0)}/{labeled}"


# =============================================================================================
# cache.miss-by-cause
# =============================================================================================

_MISS_KINDS = ("ttl-expiry", "model-switch", "param-change", "compaction", "tools-changed",
               "system-changed", "messages-changed", "context-shrank", "unexplained")
#: The canonical server diagnostic that corroborates each cause (validated_against).
_EXPECTED_DIAG = {
    "ttl-expiry": "previous_message_not_found", "model-switch": "model_changed",
    "param-change": "param_changed", "compaction": "compacted", "tools-changed": "tools_changed",
    "system-changed": "system_changed", "messages-changed": "messages_changed",
}
_MISS_REFS = ("cc-miss-taxonomy-ground-truth", "cc-usage-likely-cause",
              "anth-invalidation-hierarchy")
_MISS_TEXT: Mapping[str, tuple[str, str, str, str]] = {
    # kind: (title phrase, lever class, fix text, fix target)
    "ttl-expiry": (
        "Cache TTL expiries", "cache_transform",
        "Idle gaps longer than the cache TTL rewrote the cached prefix. Evaluate a 1h TTL "
        "(promptCacheTtl / subagentPromptCacheTtl in Claude Code, cache_control ttl in the SDK) "
        "or an SDK keepalive; the cache.ttl-advisor finding carries the replayed saving.",
        "claude-code-managed-settings"),
    "model-switch": (
        "Model switches rebuilding the cache", "behavioral",
        "Keep one model per conversation: switch models at /clear or through a subagent; send the "
        "fallback-credit beta header or use same-family fallbacks for refusal fallbacks.",
        "code"),
    "param-change": (
        "Parameter changes invalidating the cache", "behavioral",
        "Pin prompt-affecting parameters (speed, effort, thinking) within a conversation: "
        "fastModePerSessionOptIn for fast mode, per-message effort (beta) where supported.",
        "code"),
    "compaction": (
        "Cache rebuilds after compaction, clears and context edits", "hygiene",
        "Expected rebuilds: compact while the cache is warm, /clear instead of compacting when "
        "cold, and batch context clears (see cache.rebuild).",
        "code"),
    "tools-changed": (
        "Tool-definition changes invalidating the cache", "hygiene",
        "Keep tool definitions stable: sort tools by name, serialize schemas deterministically, "
        "defer rarely used tools (tool search / defer_loading).",
        "code"),
    "system-changed": (
        "System-prompt changes invalidating the cache", "hygiene",
        "Pin the system prompt; move per-turn values into the latest user message or a "
        "mid-conversation system message (Opus 5/5.5/4.8, Fable, Mythos; not Sonnet 5).",
        "code"),
    "messages-changed": (
        "History rewrites invalidating the cache", "hygiene",
        "Keep message history append-only; batch clears with clear_at_least instead of editing "
        "earlier turns.",
        "code"),
    "context-shrank": (
        "Context shrinks without a recorded reset", "hygiene",
        "The client dropped part of the history without a compaction or clear; truncate at a "
        "stable boundary so the remaining prefix is still read from cache.",
        "code"),
    "unexplained": (
        "Unexplained cache misses", "none",
        "Enable cache diagnostics (the cache-diagnosis beta header) to name the invalidated "
        "component, then apply the matching fix.",
        "code"),
}


class MissByCause:
    """``cache.miss-by-cause`` (SPEC §10.2): every miss event (§3.15: ``M > 0.05·E`` and
    ``M ≥ 2,000``) by cause, one finding per (cohort, cause).

    ``cost_observed`` is the rewrite actually billed, ``mw·w_billed + mu·u`` (EXACT);
    ``recoverable`` the triage premium over a warm read ``mw·(w − r) + mu·(u − r)`` (ESTIMATED,
    upper bound) — none for ``compaction``. Triage kinds: ``min_usd`` applies to
    ``cost_observed``.
    """

    id = "cache.miss-by-cause"
    version = DETECTOR_VERSION
    kinds = _MISS_KINDS
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """One finding per (cohort, cause) whose billed rewrites reach ``min_usd``."""
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            tallies: dict[str, Tally] = {}
            subs: dict[str, Counter[str]] = {}
            ambiguous: Counter[str] = Counter()
            for lane in cohort.lanes:
                reqs = {r.request_id: r for r in lane.requests}
                for t in transitions(lane, ctx):
                    if not t.is_miss_event or t.cause not in _MISS_TEXT:
                        continue
                    req = reqs[t.request_id]
                    tally = tallies.setdefault(t.cause, Tally())
                    tally.hit(lane, req.ts_start_ms)
                    if t.diag_reason in ("previous_message_not_found", "model_changed",
                                         "param_changed", "compacted", "tools_changed",
                                         "system_changed", "messages_changed", "key_changed"):
                        tally.labels[t.diag_reason] += 1
                    if t.sub_cause:
                        subs.setdefault(t.cause, Counter())[t.sub_cause] += 1
                    if t.ambiguous:
                        ambiguous[t.cause] += 1
                    priced = miss_money(prices, t, req)
                    if priced is None:
                        tally.unpriced += 1
                        continue
                    rewrite, premium = priced
                    tally.cost.add_money(rewrite)
                    tally.rec.add_money(premium)
                    tally.items.append(evidence_item(
                        "transition", t.request_id, cause=t.cause, sub_cause=t.sub_cause,
                        gap_ms=t.gap_ms, tokens=t.missed, nano=rewrite.point,
                        ambiguous=1 if t.ambiguous else None))
            for kind in _MISS_KINDS:
                tally = tallies.get(kind)
                if tally is None or tally.events == tally.unpriced:
                    continue
                finding = self._finding(ctx, cohort, kind, tally, subs.get(kind, Counter()),
                                        ambiguous[kind])
                if finding is not None:
                    out.append(finding)
        return sort_findings(out)

    def _finding(self, ctx: AnalysisContext, cohort: Cohort, kind: str, tally: Tally,
                 subs: Counter[str], ambiguous: int) -> Finding | None:
        basis = cohort.basis(ctx.pricer)
        phrase, lever_class, fix_text, target = _MISS_TEXT[kind]
        if target == "claude-code-managed-settings" and not any(
                is_claude_code(lane) for lane in tally.lanes.values()):
            target = "sdk"
        cost = tally.cost.billed(basis)
        recoverable = None
        if kind != "compaction":
            recoverable = tally.rec.estimate(
                basis, "triage premium: rewrite versus a warm read of the missed tokens",
                upper_bound=True)
        lanes = tally.lane_list()
        levers = applicable_levers(kind, lanes)
        if kind == "model-switch" and not subs.get("refusal-fallback"):
            levers = tuple(lv for lv in levers if lv != "fallback.credit")
        if kind == "param-change" and not subs.get("fast-toggle"):
            levers = tuple(lv for lv in levers if lv != "cc.fast_mode_opt_in")
        extra = [evidence_item("aggregate", f"{kind}:summary", events=tally.events,
                               unpriced_events=tally.unpriced or None,
                               ambiguous_events=ambiguous or None, nano=cost.nano or 0,
                               **{f"sub_{k}": v for k, v in sorted(subs.items())})]
        summary = (f"{tally.events} miss events ({phrase.lower()}) in {cohort.label()} lanes "
                   f"re-wrote their cached prefix; the rewrite billed is exact, the premium over "
                   f"a warm read is a triage estimate (upper bound).")
        if kind == "compaction":
            summary = (f"{tally.events} expected cache rebuilds after compaction, clears or "
                       f"context edits in {cohort.label()} lanes; cache.rebuild prices the "
                       f"avoidable part.")
        spec = Emit(
            kind=kind, category="breaker", lever_class=lever_class,
            title=f"{phrase} in {cohort.label()} lanes", summary=summary,
            references=_MISS_REFS, lever_ids=levers,
            fix=Fix(text=fix_text, config_patch=None, target=target, doc_url=API_CACHE_DOC),
            confidence="low" if kind == "unexplained" else "high",
            validated_against=validated(tally.labels, _EXPECTED_DIAG.get(kind)), triage=True)
        return emit(self, ctx, cohort, tally, spec, cost, recoverable, extra)


# =============================================================================================
# cache.switch-churn
# =============================================================================================

_SWITCH_KINDS: Mapping[tuple[str, str], str] = {
    ("model-switch", "refusal-fallback"): "refusal-fallback-no-credit",
    ("model-switch", "availability-fallback"): "availability-ping-pong",
    ("model-switch", "ping-pong"): "availability-ping-pong",
    ("model-switch", "plan-toggle"): "plan-toggle",
    ("model-switch", "user"): "user-model-switch",
    ("param-change", "fast-toggle"): "fast-toggle",
    ("param-change", "effort-change"): "effort-change",
}
_SWITCH_ORDER = ("refusal-fallback-no-credit", "availability-ping-pong", "plan-toggle",
                 "user-model-switch", "fast-toggle", "effort-change")
#: kind → the repair replayed for ``recoverable`` (kinds absent here are trade-offs: none).
_SWITCH_REPAIR = {"refusal-fallback-no-credit": "repair=fallback_credit",
                  "fast-toggle": "fast=off"}
#: Behavior changes that trade quality or convenience for cache hits: evaluate before rollout.
_SWITCH_TRADEOFFS = frozenset({"plan-toggle", "user-model-switch", "effort-change"})
_SWITCH_REFS = ("cc-model-switch-cost", "fp-fallback-credit", "cc-cache-breakers",
                "cc-model-effort-mix")
_PER_MESSAGE_EFFORT = ("Use per-message effort (beta header "
                       "mid-conversation-output-config-2026-07-01; Fable 5.1, Mythos 5.1, Opus 5, "
                       "Opus 5.5) instead of changing the top-level effort mid-conversation; in "
                       "Claude Code, effort changes keep the cache only on Opus 5.5 and Fable 5.1 "
                       "outside Bedrock/Vertex (v2.1.260+).")
_SWITCH_TEXT: Mapping[str, tuple[str, str, str]] = {
    # kind: (title phrase, lever class, fix text)
    "refusal-fallback-no-credit": (
        "Refusal fallbacks rebuilding the cache without credit", "cache_transform",
        "Send the fallback-credit beta header so the fallback model's first call is credited, or "
        "configure same-family fallbacks."),
    "availability-ping-pong": (
        "Availability fallbacks bouncing between models", "behavioral",
        "Prefer same-family fallbacks and retry the primary model with backoff before switching; "
        "switching back and forth rebuilds the cache on both models."),
    "plan-toggle": (
        "Plan-mode model toggles (likely opusplan)", "behavioral",
        "opusplan switches models on every plan-mode toggle and rebuilds the cache — prefer one "
        "model or the Plan subagent."),
    "user-model-switch": (
        "Mid-conversation model switches", "behavioral",
        "Switch models at /clear or delegate to a subagent instead of switching mid-conversation; "
        "every switch rebuilds the cache on the new model."),
    "fast-toggle": (
        "Fast-mode toggles invalidating the cache", "rate",
        "Set fastModePerSessionOptIn so fast mode is a per-session opt-in instead of a sticky "
        "toggle."),
    "effort-change": (
        "Effort changes invalidating the cache", "behavioral", _PER_MESSAGE_EFFORT),
}


class SwitchChurn:
    """``cache.switch-churn`` (SPEC §10.2, D28, D34): miss events whose cause is
    ``model-switch`` or ``param-change``, one finding per (cohort, sub-kind).

    ``cost_observed`` is the rewrite billed (EXACT). ``recoverable``: refusal fallbacks replay
    ``repair=fallback_credit`` (a free win), fast-mode toggles replay ``fast=off``; plan toggles,
    user switches, availability ping-pong and effort changes have none (trade-offs). An effort
    change counts only when ``core.cache_rules.effort_change_keeps_cache`` is False (the
    classification of ``core.transitions``).
    """

    id = "cache.switch-churn"
    version = DETECTOR_VERSION
    kinds = _SWITCH_ORDER
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """One finding per (cohort, sub-kind) at or above ``min_usd``."""
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            tallies: dict[str, Tally] = {}
            for lane in cohort.lanes:
                reqs = {r.request_id: r for r in lane.requests}
                for t in transitions(lane, ctx):
                    if not t.is_miss_event or t.sub_cause is None:
                        continue
                    kind = _SWITCH_KINDS.get((t.cause, t.sub_cause))
                    if kind is None:
                        continue
                    req = reqs[t.request_id]
                    if kind == "refusal-fallback-no-credit" and _credited(lane, req, t):
                        continue
                    tally = tallies.setdefault(kind, Tally())
                    tally.hit(lane, req.ts_start_ms)
                    priced = miss_money(prices, t, req)
                    if priced is None:
                        tally.unpriced += 1
                        continue
                    rewrite, _premium = priced
                    tally.cost.add_money(rewrite)
                    tally.items.append(evidence_item(
                        "transition", t.request_id, cause=t.cause, sub_cause=t.sub_cause,
                        gap_ms=t.gap_ms, tokens=t.missed, nano=rewrite.point))
            for kind in _SWITCH_ORDER:
                tally = tallies.get(kind)
                if tally is None or tally.events == tally.unpriced:
                    continue
                finding = self._finding(ctx, cohort, kind, tally)
                if finding is not None:
                    out.append(finding)
        return sort_findings(out)

    def _finding(self, ctx: AnalysisContext, cohort: Cohort, kind: str,
                 tally: Tally) -> Finding | None:
        basis = cohort.basis(ctx.pricer)
        phrase, lever_class, fix_text = _SWITCH_TEXT[kind]
        lanes = tally.lane_list()
        cost = tally.cost.billed(basis)
        recoverable = None
        repair = _SWITCH_REPAIR.get(kind)
        if repair is not None:
            recoverable = saving_figure(replay(ctx, lanes, repair), basis)
        config = None
        target = "code"
        gates: tuple[str, ...] = ()
        doc = API_CACHE_DOC
        if kind == "fast-toggle" and any(is_claude_code(lane) for lane in lanes):
            config = patch(("fastModePerSessionOptIn", "true"))
            target, gates = "claude-code-managed-settings", cc_gates("fastModePerSessionOptIn")
            doc = settings_doc("fastModePerSessionOptIn") or doc
        elif kind == "refusal-fallback-no-credit":
            target = "sdk"
        summary = (f"{tally.events} {phrase.lower()} in {cohort.label()} lanes rebuilt the cache; "
                   f"the rewrite billed is exact.")
        if kind in _SWITCH_TRADEOFFS:
            summary += " No replayed saving: changing this behavior is a trade-off."
        elif repair is None:
            summary += " No mechanical repair is replayed (not a trade-off: see the fix)."
        spec = Emit(
            kind=kind, category="breaker", lever_class=lever_class,
            title=f"{phrase} in {cohort.label()} lanes", summary=summary,
            references=_SWITCH_REFS, lever_ids=applicable_levers(kind, lanes),
            fix=Fix(text=fix_text, config_patch=config, target=target, doc_url=doc, gates=gates),
            confidence="high" if kind in ("refusal-fallback-no-credit", "fast-toggle") else
            "medium",
            needs_eval=kind in _SWITCH_TRADEOFFS)
        return emit(self, ctx, cohort, tally, spec, cost, recoverable)


def _credited(lane: Lane, req: Request, t: Transition) -> bool:
    """True when a MODEL_FALLBACK event of this transition reports the fallback as credited."""
    ts = req.ts_start_ms
    return any(ev.kind is LaneEventKind.MODEL_FALLBACK and event_attr(ev, "credited") is True
               for ev in events_between(lane, ts - t.gap_ms, ts))


# =============================================================================================
# cache.rebuild
# =============================================================================================

_REBUILD_KINDS = ("compaction-cold", "edit-churn")
_COMPACTION_REFS = ("compaction-timing", "anth-compaction-iterations-billing")
_EDIT_REFS = ("context-editing-cost", "anth-context-editing-cache",
              "anth-context-editing-not-savings", "history-rewrite-kstar")
_HINT_TTL_S = {"5m": 300, "1h": 3600}


def write_ttl_s(usage: UsageBuckets, hint: str | None) -> int | None:
    """TTL (s) of a billed write (the shortest class when several; ``core.lanes`` semantics)."""
    ttls = []
    if usage.cache_write_5m:
        ttls.append(300)
    if usage.cache_write_1h:
        ttls.append(3600)
    if usage.cache_write_other and usage.cache_write_other_ttl_s is not None:
        ttls.append(usage.cache_write_other_ttl_s)
    if usage.cache_write_unknown and hint in _HINT_TTL_S:
        ttls.append(_HINT_TTL_S[hint])
    return min(ttls) if ttls else None


def ttl_bucket(ttl_s: int) -> str:
    """The write bucket priced at a TTL (``wτ``): 1h for 3,600 s, else 5m."""
    return "cache_write_1h" if ttl_s == 3600 else "cache_write_5m"


class RebuildEvents:
    """``cache.rebuild`` (SPEC §10.2): avoidable cache rebuilds.

    * ``compaction-cold`` (needs the ``events`` capability): a COMPACTION event whose preceding
      request started more than ``τ`` earlier — the compaction re-read the whole context cold.
      ``cost_observed`` = ``pre_tokens·wτ`` (the compaction input at the write rate, an estimated
      line); ``recoverable`` = ``pre_tokens·(wτ − r)`` (ESTIMATED).
    * ``edit-churn`` (``events`` or ``attempts``): a context edit (``applied_edits``, else
      CONTEXT_EDIT events; cleared ``X`` = Σ ``cleared_input_tokens``) on request ``i`` while warm
      (``gap_i ≤ τ``): ``S = W_i`` tokens after the edit point are rewritten, ``K_rem`` later
      requests remain before the next reset. ``cost_observed`` = ``S·w_billed`` (EXACT);
      ``recoverable`` = Σ ``max(0, S·(w − r) − X·r·K_rem)`` (ESTIMATED); the payback call count
      ``K* = S(α − β)/(Xβ)`` (α = w/u, β = r/u) and the ``K_rem`` distribution are evidence.
    """

    id = "cache.rebuild"
    version = DETECTOR_VERSION
    kinds = _REBUILD_KINDS
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Per cohort: one ``compaction-cold`` and one ``edit-churn`` finding at or above
        ``min_usd``."""
        prices = Prices(ctx.pricer)
        caps = ctx.capabilities
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            if "events" in caps:
                found = self._compaction_cold(ctx, prices, cohort)
                if found is not None:
                    out.append(found)
            if "events" in caps or "attempts" in caps:
                found = self._edit_churn(ctx, prices, cohort)
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    # ---------- compaction-cold ----------

    def _compaction_cold(self, ctx: AnalysisContext, prices: Prices,
                         cohort: Cohort) -> Finding | None:
        tally = Tally()
        for lane in cohort.lanes:
            comps = [ev for ev in lane.events if ev.kind is LaneEventKind.COMPACTION]
            if not comps:
                continue
            steps = serving_steps(lane)
            starts = [req.ts_start_ms for req in steps]
            ttl_after: list[int | None] = []
            last: int | None = None
            for req in steps:
                for inf in req.billable_inferences:
                    if inf.usage.cache_write > 0:
                        last = write_ttl_s(inf.usage, inf.pricing.write_ttl_hint)
                si = req.serving_inference
                hint = _HINT_TTL_S.get(si.pricing.write_ttl_hint or "") if si else None
                ttl_after.append(last if last is not None else hint)
            for ev in comps:
                idx = _last_before(starts, ev.ts_ms)
                if idx is None:
                    continue
                prev = steps[idx]
                tau = ttl_after[idx]
                if tau is None or ev.ts_ms - prev.ts_start_ms <= tau * 1000:
                    continue
                inf = prev.serving_inference
                assert inf is not None
                pre = event_attr(ev, "pre_tokens")
                tokens = pre if type(pre) is int and pre > 0 else inf.usage.total_input
                write = prices.line(inf.pricing, ev.ts_ms, ttl_bucket(tau), tokens)
                premium = combine((1, write),
                                  (-1, prices.line(inf.pricing, ev.ts_ms, "cache_read", tokens)))
                tally.hit(lane, ev.ts_ms)
                if write is None or premium is None:
                    tally.unpriced += 1
                    continue
                tally.cost.add_money(write)
                tally.rec.add_money(premium)
                tally.items.append(evidence_item(
                    "event", stable_id("ev", lane.lane_key, ev.ts_ms, "compaction"),
                    idle_ms=ev.ts_ms - prev.ts_start_ms, ttl_s=tau, tokens=tokens,
                    nano=write.point))
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        lanes = tally.lane_list()
        cost = tally.cost.estimate(basis, "compaction input priced at the cache write rate")
        recoverable = tally.rec.estimate(basis, "premium of a cold compaction over a warm read")
        spec = Emit(
            kind="compaction-cold", category="breaker", lever_class="behavioral",
            title=f"Compactions after the cache went cold in {cohort.label()} lanes",
            summary=(f"{tally.events} compactions in {cohort.label()} lanes started after the "
                     f"cache TTL had expired and re-read the whole context at the write rate."),
            references=_COMPACTION_REFS,
            lever_ids=applicable_levers("compaction-cold", lanes),
            fix=Fix(text="Compact while the cache is warm (before stepping away); after a long "
                         "idle use /clear for a new task instead of compacting a cold context.",
                    config_patch=None, target="code", doc_url=CC_CACHE_DOC))
        return emit(self, ctx, cohort, tally, spec, cost, recoverable)

    # ---------- edit-churn ----------

    def _edit_churn(self, ctx: AnalysisContext, prices: Prices,
                    cohort: Cohort) -> Finding | None:
        tally = Tally()
        kstars: list[Fraction] = []
        krems: list[int] = []
        losing = 0
        for lane in cohort.lanes:
            steps = serving_steps(lane)
            if len(steps) < 2:
                continue
            index = EventIndex(lane)
            remaining: list[int] | None = None
            trans: dict[str, Transition] | None = None
            for i in range(1, len(steps)):
                req = steps[i]
                prev_ts = steps[i - 1].ts_start_ms
                ts = req.ts_start_ms
                cleared = _cleared(index, req, prev_ts)
                if cleared <= 0:
                    continue
                if trans is None:
                    trans = {t.request_id: t for t in transitions(lane, ctx)}
                t = trans.get(req.request_id)
                tau = t.ttl_s if t is not None and t.ttl_s is not None else DEFAULT_TTL_S
                if ts - prev_ts > tau * 1000:
                    continue
                inf = req.serving_inference
                assert inf is not None
                rewritten = inf.usage.cache_write
                if rewritten <= 0:
                    continue
                if remaining is None:
                    remaining = _remaining_counts(index, steps)
                k_rem = remaining[i]
                billed = prices.written(inf.pricing, ts, inf.usage, rewritten)
                read_s = prices.line(inf.pricing, ts, "cache_read", rewritten)
                loss = combine((1, billed), (-1, read_s),
                               (-1, prices.line(inf.pricing, ts, "cache_read", cleared * k_rem)))
                read_x = prices.line(inf.pricing, ts, "cache_read", cleared)
                tally.hit(lane, ts)
                if billed is None or read_s is None or loss is None or read_x is None or \
                        read_x.point <= 0:
                    tally.unpriced += 1
                    continue
                loss.floor_at_zero()
                tally.cost.add_money(billed)
                tally.rec.add_money(loss)
                kstar = _kstar(prices, inf.pricing, ts, inf.usage, rewritten, cleared)
                if kstar is None:
                    kstar = Fraction(billed.point - read_s.point, read_x.point)
                kstars.append(kstar)
                krems.append(k_rem)
                losing += 1 if k_rem < kstar else 0
                tally.items.append(evidence_item(
                    "event", req.request_id, cleared=cleared, rewritten=rewritten, k_rem=k_rem,
                    kstar=decimal_str(kstar), nano=loss.point))
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        cost = tally.cost.billed(basis)
        recoverable = tally.rec.estimate(
            basis, "net loss of context edits that did not pay back within the remaining calls")
        kstar_p50 = percentile(kstars, 50)
        summary_item = evidence_item(
            "aggregate", "edit-churn:distribution", edits=len(kstars), edits_not_paying_back=losing,
            kstar_p10=decimal_str(percentile(kstars, 10)),
            kstar_p50=decimal_str(kstar_p50), kstar_p90=decimal_str(percentile(kstars, 90)),
            k_rem_p10=int(percentile(krems, 10)), k_rem_p50=int(percentile(krems, 50)),
            k_rem_p90=int(percentile(krems, 90)), nano=recoverable.nano or 0)
        spec = Emit(
            kind="edit-churn", category="breaker", lever_class="hygiene",
            title=f"Context edits that do not pay back in {cohort.label()} lanes",
            summary=(f"{tally.events} context edits in {cohort.label()} lanes cleared tokens while "
                     f"the cache was warm and rewrote the rest of the prefix; {losing} of them had "
                     f"fewer remaining calls than the payback count K* (median "
                     f"{decimal_str(kstar_p50)})."),
            references=_EDIT_REFS, lever_ids=applicable_levers("edit-churn", tally.lane_list()),
            fix=Fix(text=(f"Clear less often and more at a time: set clear_at_least so each clear "
                          f"removes enough to pay back within the typical remaining calls "
                          f"(median K* ≈ {decimal_str(kstar_p50)} calls), raise the trigger, "
                          f"prune at task boundaries; context editing is a context-window tool, "
                          f"not a savings lever."),
                    config_patch=None, target="sdk", doc_url=API_CACHE_DOC))
        return emit(self, ctx, cohort, tally, spec, cost, recoverable, (summary_item,))


def _last_before(starts: Sequence[int], ts_ms: int) -> int | None:
    """Index of the last step starting at or before *ts_ms* (*starts* sorted ascending)."""
    idx = bisect_right(starts, ts_ms) - 1
    return idx if idx >= 0 else None


def _cleared(index: EventIndex, req: Request, prev_ts: int) -> int:
    """Tokens cleared by context edits on *req*: Σ ``applied_edits``, else Σ
    ``cleared_input_tokens`` of CONTEXT_EDIT events in ``(prev_ts, ts]``."""
    applied = sum(n for att in req.attempts for _, n in att.applied_edits)
    if applied > 0:
        return applied
    total = 0
    for ev in index.between(prev_ts, req.ts_start_ms):
        if ev.kind is LaneEventKind.CONTEXT_EDIT:
            n = event_attr(ev, "cleared_input_tokens")
            if type(n) is int and n > 0:
                total += n
    return total


def _remaining_counts(index: EventIndex, steps: Sequence[Request]) -> list[int]:
    """``K_rem`` for every step ``i``: the later requests of the lane before the next reset — a
    reset event after step ``i``, or a request carrying context edits or dropped thinking blocks.
    A reset event in ``(ts_i, ts_j]`` is one in some ``(ts_{j'−1}, ts_{j'}]`` with ``i < j' ≤ j``,
    so one backward pass over per-step break flags gives every count in O(n log m)."""
    n = len(steps)
    breaks = [False] * n
    for j in range(1, n):
        later = steps[j]
        breaks[j] = any(att.applied_edits or att.thinking_dropped for att in later.attempts) or \
            index.reset_between(steps[j - 1].ts_start_ms, later.ts_start_ms)
    counts = [0] * n
    first_break = n
    for i in range(n - 1, -1, -1):
        counts[i] = first_break - i - 1
        if breaks[i]:
            first_break = i
    return counts


def _kstar(prices: Prices, pricing: PricingContext, ts_ms: int, usage: UsageBuckets,
           rewritten: int, cleared: int) -> Fraction | None:
    """``K* = S(w − r)/(X·r)`` from exact unit rates (the write rate weighted over the billed
    write buckets; unknown-TTL writes at the 5m rate); None without unit rates."""
    unit = prices.unit(pricing, ts_ms)
    if unit is None or unit.cache_read <= 0:
        return None
    weighted = (usage.cache_write_1h * unit.cache_write_1h
                + usage.cache_write_5m * unit.cache_write_5m
                + usage.cache_write_other * unit.cache_write_other
                + usage.cache_write_unknown * unit.cache_write_5m)
    total = usage.cache_write
    if total <= 0:
        return None
    w = Fraction(weighted, total)
    return Fraction(rewritten) * (w - unit.cache_read) / (cleared * unit.cache_read)

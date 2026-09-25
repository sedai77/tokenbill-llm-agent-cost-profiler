"""GitHub Copilot lane detector (addendum §10.3, package CP-DET-LANES): ``copilot.lanes``.

Names the request-level causes of Copilot AI-credit spend that billing data cannot show, from
per-request Copilot lanes — VS Code ``agent-traces.db`` extracts and OTel first (the adopting
developers use VS Code and IntelliJ), then managed OTel, Copilot CLI events / session store and
gh-aw runs:

* ``long-context-band`` — the premium of the long-context band over the default tier on
  identical tokens, through the pricer: every priced line of ``Pricer.price_inference`` (resolved
  rates: hypothesis A, or the A/B range of addendum §6.2 #4 when the lane's context tier
  disagrees) minus the same line at ``Pricer.unit_rates`` (base rates, never band rates). EXACT
  when hypothesis A alone decided, an ESTIMATED range when A and B disagree (Appendix C.G5 /
  G5b). ``recoverable`` = the premium (the standalone counterfactual "every banded request at the
  default tier"), ESTIMATED ``upper_bound``; the lever ``copilot.context_default`` carries its
  delivery reach as evidence (``reach:copilot.context_default``: the repository ``contextTier``
  default reaches the Copilot CLI only, in trusted directories, trusted share unknown; addendum
  §9.3 — projections multiply by it, the finding does not).
* ``compaction-cost`` — COMPACTION inferences priced exactly, joined within the lane to their
  COMPACTION events by timestamp for ``copilot_trigger``; the forced share (``context_limit_retry``,
  ``memory_pressure``) is counted over every compaction (an event without an inference counts,
  with no money). No recoverable.
* ``static-overhead`` — the static prefix ``system_tokens + tool_definitions_tokens`` of the
  lane's COMPACTION events (else the static-prefix floor ``ctx.static_prefix_floor[(scope,
  model)]``), carried by every request at its own read / write / uncached mix (the prefix is read
  first, then written, then sent uncached), ESTIMATED. ``recoverable`` = the tool-definition
  carry (tools tier first) × ``TOOL_SEARCH_REDUCTION_BAND`` [0.50, 0.85], point 0.70, ESTIMATED
  ``upper_bound``; None when only the floor is known (the tool share is unknown). The reach of
  ``copilot.mcp_trim`` (CLI, VS Code, JetBrains, Copilot app; not the cloud agent) is evidence.
* ``subagent-share`` (info) — SUBAGENT lane spend by ``agent_type`` inside the cohort.
* ``ci-uncapped`` (info) — Copilot CI lanes (``workload_class=ci``: CLI ``--ci`` lanes, gh-aw
  lanes), per agent product: spend per session p50 / p90 and the share of CLI sessions without a
  ``credit_limit_nano`` (SESSION_META); gh-aw runs compared with their cap (default 1,000 AIC per
  run, ``core.facts``) at p99. ``cost_observed`` = the spend of the uncapped CLI sessions / of the
  runs at the default cap.

**Gating (brief).** ``requires = {"credits"}`` (every Copilot lane source declares it) and
``extension = "copilot"``; instead of per-kind capability gates each kind checks its own inputs
per lane (:data:`KIND_INPUTS`), and a lane lacking a kind's input is skipped for that kind only:
``long-context-band`` needs a request whose serving inference carries input tokens (VS Code, OTel,
store — not events-only CLI sessions); ``compaction-cost`` COMPACTION inferences or events
(events-only CLI qualifies); ``static-overhead`` COMPACTION static-token attrs or a static-prefix
floor; ``ci-uncapped`` ``workload_class=ci`` lanes; ``subagent-share`` SUBAGENT lanes. There is no
skipped-kinds note: whether one shard holds a kind's input is not a data-quality fact, and a lane
detector's output must not depend on sharding.

**Money and labels.** Lane pricing on the Copilot billing paths is LIST_EQUIVALENT; lane findings
carry no pool conversion (``recoverable`` stays LIST_EQUIVALENT, ``headroom`` None: the aggregate
plan converts to invoice dollars). Scopes are the cohort ``(team, lane_kind, billing_class)`` plus
``product: copilot`` (and ``model`` / ``agent_product`` / ``workload_class`` where named);
``core.findings.build_finding`` adds the "Copilot credits:" labels, validates the R-E20 bases and
takes each fix from ``core.catalog.fix_for`` (``fix=None`` here). Categories: ``lever`` for
``long-context-band``, ``compaction-cost`` and ``static-overhead``; ``aggregate`` for
``subagent-share`` and ``ci-uncapped`` (k-anonymity from ``COUNT_SOURCE`` = ``requests``).
``min_usd`` through ``core.findings.min_usd_gate``.

**Shard invariance.** Every computation stays inside one cohort ``core.findings.cohort_key``
(billing class ``pool`` already isolates Copilot lanes); only Copilot-family lanes
(``core.findings.product_family``) are read, so Claude Code lanes of the same team never enter.
Money is int nano and exact fractions, rounded half-even once per figure; no float.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import NamedTuple

from tokenbill.core import catalog
from tokenbill.core import facts as _facts
from tokenbill.core.evidence import TOOL_SEARCH_REDUCTION_BAND
from tokenbill.core.findings import (
    COPILOT_FAMILY,
    build_finding,
    cohort_key,
    make_scope,
    min_usd_gate,
    product_family,
    top_evidence,
)
from tokenbill.core.labels import Basis, Evidence, Figure, estimated, exact
from tokenbill.core.money import NANO_USD_PER_CREDIT, nano_to_credits_str
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Request,
    UsageSource,
    WorkloadClass,
)
from tokenbill.core.types import (
    AnalysisContext,
    EvidenceItem,
    Finding,
    PricedInference,
    PricedLine,
    UnitRates,
)

__all__ = [
    "CI_PRODUCT_GH_AW",
    "CONTEXT_DEFAULT_PRODUCTS",
    "DETECTOR_ID",
    "FORCED_TRIGGERS",
    "KINDS",
    "KIND_INPUTS",
    "MCP_TRIM_PRODUCTS",
    "BandPremium",
    "CopilotLanes",
    "band_premium",
    "default_run_cap_nano",
    "static_carry",
]

DETECTOR_ID = "copilot.lanes"
_VERSION = "1"

#: The kinds of addendum §10.3 in table order.
KINDS = ("long-context-band", "compaction-cost", "static-overhead", "subagent-share",
         "ci-uncapped")

#: The per-lane input each kind checks (brief step 3); a lane without it is skipped for that kind.
KIND_INPUTS: Mapping[str, str] = MappingProxyType({
    "long-context-band": "a request whose serving inference carries input tokens",
    "compaction-cost": "COMPACTION inferences or COMPACTION events",
    "static-overhead": "COMPACTION static-token attrs or a static-prefix floor",
    "subagent-share": "a SUBAGENT lane",
    "ci-uncapped": "a workload_class=ci lane",
})

#: ``copilot_trigger`` values of forced compactions (the context limit or memory pressure forced
#: them; addendum §10.3).
FORCED_TRIGGERS = frozenset({"context_limit_retry", "memory_pressure"})
#: Agent products the repository ``contextTier`` default reaches (addendum §9.3: Copilot CLI only,
#: in trusted working directories).
CONTEXT_DEFAULT_PRODUCTS = frozenset({"copilot_cli"})
#: Agent products managed ``deniedMcpServers`` / ``allowedMcpServers`` reach (addendum §9.3: CLI,
#: VS Code, Copilot app, JetBrains; not the cloud agent).
MCP_TRIM_PRODUCTS = frozenset({"copilot_cli", "copilot_vscode", "copilot_jetbrains",
                               "copilot_app"})
#: The gh-aw agent product (addendum §5.14): runs capped by ``max-ai-credits`` (default 1,000 AIC).
CI_PRODUCT_GH_AW = "copilot_gh_aw"

#: Behavioural levers a ``ci-uncapped`` finding links (``COPILOT_LEVERS``, finding kind
#: ``ci-uncapped``): CLI session limits, gh-aw ``max-ai-credits`` caps. Never projected.
_CI_LEVER_CLI = "copilot.session_limits"
_CI_LEVER_GH_AW = "copilot.agentic_workflow_caps"
_COMPACTION_JOIN_MS = 60_000     # a COMPACTION inference joins its event within ±60 s
#: Names (agent types, compaction triggers) are echoed only when they look like identifiers; a
#: free-text value (a custom name with spaces, content) is reported as ``custom``.
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}\Z")
_BAND_POINT = Fraction(7, 10)     # SPEC §10.2 tool-defs-bloat: band [0.50, 0.85], point 0.70
_CAP_FACTOR = Fraction(3, 2)      # max-ai-credits N ≈ p99 × 1.5 (addendum §10.3 fix)
_CI_PERCENTILES = (50, 90, 99)
_UNKNOWN = "unknown"
_BANDED = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
           "cache_write_other", "output")
_WRITE_BUCKETS = frozenset({"cache_write_5m", "cache_write_1h", "cache_write_other",
                            "cache_write_unknown"})
_PRICED_SOURCES = frozenset({UsageSource.FINAL, UsageSource.ESTIMATED})

_REFS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "long-context-band": ("copilot-billing-long-context", "copilot-billing-rates",
                          "copilot-cost-levers-context"),
    "compaction-cost": ("copilot-client-telemetry", "copilot-cost-levers-context",
                        "compaction-timing"),
    "static-overhead": ("copilot-cost-levers-mcp", "tool-schema-bloat", "anth-tool-search-defer"),
    "subagent-share": ("copilot-cost-levers-subagents", "cc-delegation-model-routing"),
    "ci-uncapped": ("copilot-billing-session-limits", "copilot-billing-agentic-workflows"),
})


# ---------------------------------------------------------------------------------------------
# small exact helpers
# ---------------------------------------------------------------------------------------------


def _round(value: Fraction) -> int:
    """Half-even rounding of an exact fraction to an int (the one rounding of a figure)."""
    q, r = divmod(value.numerator, value.denominator)
    twice = 2 * r
    if twice > value.denominator or (twice == value.denominator and q % 2 == 1):
        q += 1
    return q


def _pct(num: int, den: int) -> str:
    """``num / den`` as a percentage with one decimal, half-even (``"62.5"``); ``"0.0"`` when
    *den* is 0."""
    if den <= 0:
        return "0.0"
    tenths = _round(Fraction(1000 * num, den))
    return f"{tenths // 10}.{tenths % 10}"


def _safe_name(value: object) -> str | None:
    """*value* when it is an identifier-like name, ``custom`` for any other non-empty string,
    None when absent (free text never reaches a finding)."""
    if not isinstance(value, str) or not value:
        return None
    return value if _SAFE_NAME.match(value) else "custom"


def _nearest_rank(values: Sequence[int], pct: int) -> int:
    """Nearest-rank *pct*-th percentile of *values* (non-empty)."""
    ordered = sorted(values)
    rank = -(-pct * len(ordered) // 100)
    return ordered[max(rank, 1) - 1]


def _band() -> tuple[Fraction, Fraction, Fraction]:
    """``TOOL_SEARCH_REDUCTION_BAND`` as (low, point, high) fractions."""
    value = TOOL_SEARCH_REDUCTION_BAND.value
    assert isinstance(value, tuple)
    return Fraction(value[0]), _BAND_POINT, Fraction(value[1])


def default_run_cap_nano() -> int:
    """The default gh-aw per-run cap (``core.facts`` ``aic_default_cap_per_run``, 1,000 AI
    credits) in nano-USD (1 credit = $0.01)."""
    return _facts.load().copilot.aic_default_cap_per_run * NANO_USD_PER_CREDIT


class _Acc:
    """A money accumulator in int nano: point, low and high sums, whether every part is EXACT,
    the number of priced and unpriced parts."""

    __slots__ = ("exact", "high", "low", "n", "point", "unpriced")

    def __init__(self) -> None:
        self.point = self.low = self.high = 0
        self.exact = True
        self.n = self.unpriced = 0

    def add(self, point: int, low: int, high: int, is_exact: bool) -> None:
        """Add one priced part."""
        self.point += point
        self.low += low
        self.high += high
        self.exact = self.exact and is_exact
        self.n += 1

    def add_figure(self, fig: Figure) -> None:
        """Add a priced figure (its range when it has one); an unpriced figure is counted."""
        if fig.nano is None:
            self.unpriced += 1
            return
        low = fig.low_nano if fig.low_nano is not None else fig.nano
        high = fig.high_nano if fig.high_nano is not None else fig.nano
        self.add(fig.nano, low, high, fig.evidence is Evidence.EXACT)

    def merge(self, other: _Acc) -> None:
        """Add every part of *other*."""
        self.point += other.point
        self.low += other.low
        self.high += other.high
        self.exact = self.exact and other.exact
        self.n += other.n
        self.unpriced += other.unpriced

    def figure(self, basis: Basis, note: str) -> Figure:
        """EXACT when every part was exact, else ESTIMATED with the range when it has width."""
        if self.exact:
            return exact(self.point, basis)
        ranged = self.low != self.point or self.high != self.point
        return estimated(self.point, basis, low=self.low if ranged else None,
                         high=self.high if ranged else None, note=note)


# ---------------------------------------------------------------------------------------------
# long-context band premium (Appendix C.G5 / G5b)
# ---------------------------------------------------------------------------------------------


class BandPremium(NamedTuple):
    """The long-context band premium of one inference (int nano): the point (hypothesis A), the
    range over the A/B hypotheses, whether it is exact rate arithmetic (A alone decided) and
    whether the context tier was known."""

    point: int
    low: int
    high: int
    exact: bool
    tier_known: bool


def _default_line(unit: UnitRates, line: PricedLine,
                  hint_1h: bool) -> tuple[int, int, int] | None:
    """(point, low, high) of *line*'s tokens at the default-tier unit rates (the non-band ranges
    of unknown-TTL writes kept), or None for a bucket no band touches."""
    qty = line.quantity
    bucket = line.bucket
    if bucket == "cache_write_unknown":
        low = unit.bucket_nano("cache_write_5m", qty)
        high = unit.bucket_nano("cache_write_1h", qty)
        return (high if hint_1h else low), low, high
    if bucket in _BANDED:
        value = unit.bucket_nano(bucket, qty)
        return value, value, value
    if bucket == "web_search":
        value = unit.bucket_nano("web_search", qty)
        return value, value, value
    return None


def band_premium(pricer: Pricer, inf: Inference, ts_ms: int) -> BandPremium | None:
    """Price at the resolved rates − price at default-tier rates on identical tokens (addendum
    §10.3 ``long-context-band``), line by line through *pricer*.

    The resolved side is ``pricer.price_inference`` (hypothesis A prices the point; when the
    context tier disagrees, every line is the A/B range, §6.2 #4); the default side is the same
    line quantities at ``pricer.unit_rates`` (base, never band, rates). Only billable inferences
    with final or estimated usage are analysed (a billing-uncertain or partial line carries a
    [0, full] range that is not a band); None when either side is unpriced. The premium's low is
    never below 0 and point ⊆ [low, high]."""
    if inf.billable is not True or inf.usage_source not in _PRICED_SOURCES:
        return None
    priced: PricedInference = pricer.price_inference(inf, ts_ms=ts_ms)
    if priced.unpriced_reason is not None or priced.figure.nano is None:
        return None
    unit = pricer.unit_rates(inf.pricing, ts_ms=ts_ms)
    if unit is None:
        return None
    hint_1h = inf.pricing.write_ttl_hint == "1h"
    point = low = high = 0
    for line in priced.lines:
        default = _default_line(unit, line, hint_1h)
        if default is None:
            continue
        d_point, d_low, d_high = default
        l_low = line.low_nano if line.low_nano is not None else line.amount_nano
        l_high = line.high_nano if line.high_nano is not None else line.amount_nano
        point += line.amount_nano - d_point
        low += l_low - d_low
        high += l_high - d_high
    low = max(0, low)
    high = max(high, low)
    point = min(max(point, low), high)
    return BandPremium(point, low, high, priced.figure.evidence is Evidence.EXACT,
                       inf.pricing.context_tier is not None)


# ---------------------------------------------------------------------------------------------
# static carry (static-overhead)
# ---------------------------------------------------------------------------------------------


def static_carry(priced: PricedInference, inf: Inference, static_tokens: int) -> Fraction:
    """The exact list-equivalent cost of *static_tokens* prefix tokens inside *inf*'s billed
    input: read first (``min(S, R)`` at the request's read price per token), then written
    (``min(S − read, W)`` at its write price per token), then uncached (``min(rest, U)``), each
    class priced at the average of the request's own priced lines of that class (the lane's
    read / write mix)."""
    usage = inf.usage
    classes = (("read", usage.cache_read), ("write", usage.cache_write),
               ("uncached", usage.uncached_input))
    paid = {"read": 0, "write": 0, "uncached": 0}
    for line in priced.lines:
        if line.bucket == "cache_read":
            paid["read"] += line.amount_nano
        elif line.bucket in _WRITE_BUCKETS:
            paid["write"] += line.amount_nano
        elif line.bucket == "uncached_input":
            paid["uncached"] += line.amount_nano
    remaining = max(0, static_tokens)
    total = Fraction(0)
    for name, tokens in classes:
        part = min(remaining, tokens)
        if part > 0:
            total += Fraction(part * paid[name], tokens)
        remaining -= part
    return total


# ---------------------------------------------------------------------------------------------
# lanes and cohorts
# ---------------------------------------------------------------------------------------------


def _agent_product(lane: Lane) -> str:
    """The agent product of *lane*'s first request (``unknown`` when absent)."""
    if not lane.requests:
        return _UNKNOWN
    return lane.requests[0].attribution.agent_product or _UNKNOWN


def _principals(requests: Iterable[Request]) -> set[str]:
    return {r.attribution.principal for r in requests if r.attribution.principal}


@dataclass
class _Tally:
    """Counts shared by every kind: events, lanes, principals and the first request time."""

    events: int = 0
    lanes: set[str] = field(default_factory=set)
    principals: set[str] = field(default_factory=set)
    first_seen: int | None = None

    def touch(self, lane: Lane, requests: Iterable[Request], ts_ms: int | None) -> None:
        self.lanes.add(lane.lane_key)
        self.principals |= _principals(requests)
        if ts_ms is not None:
            self.first_seen = ts_ms if self.first_seen is None else min(self.first_seen, ts_ms)


@dataclass(frozen=True)
class _Cohort:
    """One cohort ``(team, lane_kind, billing_class)`` of Copilot lanes, lanes sorted by key."""

    team: str | None
    lane_kind: str
    billing_class: str
    lanes: tuple[Lane, ...]

    @property
    def basis(self) -> Basis:
        """LIST_EQUIVALENT on the Copilot billing paths (class ``pool``), else LIST."""
        return Basis.LIST_EQUIVALENT if self.billing_class == "pool" else Basis.LIST

    def label(self) -> str:
        """``"<team> <lane kind> lanes"`` (``unattributed`` without a team)."""
        return f"{self.team or 'unattributed'} {self.lane_kind.replace('_', ' ')} lanes"

    def dims(self, **extra: str | None) -> dict[str, str | None]:
        return {"team": self.team, "lane_kind": self.lane_kind,
                "billing_class": self.billing_class, "product": COPILOT_FAMILY, **extra}


def _in_view(lane: Lane, ctx: AnalysisContext) -> bool:
    """A Copilot-family lane with requests (the seat-allowance class is never Copilot), belonging
    to the self principal in a self view (per lane, so shard invariance holds)."""
    if not lane.requests or product_family(lane) != COPILOT_FAMILY:
        return False
    if lane.billing_class == "allowance":
        return False
    if ctx.self_principal is None:
        return True
    return lane.requests[0].attribution.principal in (None, ctx.self_principal)


def _cohorts(lanes: Iterable[Lane], ctx: AnalysisContext) -> list[_Cohort]:
    groups: dict[tuple[str | None, str, str], list[Lane]] = {}
    for lane in lanes:
        if _in_view(lane, ctx):
            groups.setdefault(cohort_key(lane), []).append(lane)
    return [_Cohort(team, kind, bclass, tuple(sorted(members, key=lambda ln: ln.lane_key)))
            for (team, kind, bclass), members in sorted(
                groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][0] is None, kv[0][1],
                                                kv[0][2]))]


def _spend(ctx: AnalysisContext, requests: Iterable[Request]) -> _Acc:
    """Σ price of every billable inference of *requests* (billing-uncertain ones as ranges)."""
    acc = _Acc()
    for req in requests:
        for att in req.attempts:
            for inf in att.inferences:
                if inf.billable is False:
                    continue
                acc.add_figure(ctx.pricer.price_inference(inf, ts_ms=att.ts_start_ms).figure)
    return acc


def _item(ref: str, **attrs: str | int) -> EvidenceItem:
    return EvidenceItem(kind="aggregate", ref=ref, attrs=tuple(sorted(attrs.items())))


def _levers(kind: str) -> tuple[tuple[str, ...], str, bool]:
    """(lever ids, lever class, needs_eval) of *kind* from ``COPILOT_LEVERS``."""
    levers = catalog.levers_for_kind(kind, family=COPILOT_FAMILY)
    if not levers:
        return (), "none", False
    return (tuple(lv.lever_id for lv in levers), levers[0].lever_class,
            any(lv.needs_eval for lv in levers))


@dataclass(frozen=True)
class _Spec:
    """The text and classification of one finding."""

    kind: str
    category: str
    title: str
    summary: str
    confidence: str
    lever_ids: tuple[str, ...] = ()
    lever_class: str = "none"
    needs_eval: bool = False


def _unpriced_note(acc: _Acc) -> str:
    if not acc.unpriced:
        return ""
    return f" {acc.unpriced} inferences had no priced rate and are left out of the money."


def _emit(detector: CopilotLanes, ctx: AnalysisContext, cohort: _Cohort, spec: _Spec,
          tally: _Tally, cost: Figure, recoverable: Figure | None,
          items: Iterable[EvidenceItem], **scope_extra: str | None) -> Finding | None:
    """Build the finding and keep it when it passes ``min_usd`` (``core.findings.min_usd_gate``)."""
    finding = build_finding(
        detector_id=detector.id, kind=spec.kind, detector_version=detector.version,
        category=spec.category, lever_class=spec.lever_class,
        audience="self" if ctx.self_principal is not None else "org",
        title=spec.title, summary=spec.summary,
        scope=make_scope(**cohort.dims(**scope_extra)),
        n_events=tally.events, n_lanes=len(tally.lanes), n_users=len(tally.principals),
        first_seen_ms=tally.first_seen or 0, cost_observed=cost, recoverable=recoverable,
        lever_ids=spec.lever_ids, evidence=top_evidence(items), fix=None,
        confidence=spec.confidence, needs_eval=spec.needs_eval, references=_REFS[spec.kind])
    return finding if min_usd_gate(finding, ctx) else None


# ---------------------------------------------------------------------------------------------
# long-context-band
# ---------------------------------------------------------------------------------------------


@dataclass
class _BandModel:
    premium: _Acc = field(default_factory=_Acc)
    reached: _Acc = field(default_factory=_Acc)
    tally: _Tally = field(default_factory=_Tally)
    ranged: int = 0
    tier_known: int = 0
    reached_lanes: set[str] = field(default_factory=set)


def _has_input(lane: Lane) -> bool:
    return any(r.serving_inference is not None and r.serving_inference.usage.total_input > 0
               for r in lane.requests)


def _long_context(det: CopilotLanes, ctx: AnalysisContext, cohort: _Cohort) -> list[Finding]:
    models: dict[str, _BandModel] = {}
    for lane in cohort.lanes:
        if not _has_input(lane):
            continue
        reached = _agent_product(lane) in CONTEXT_DEFAULT_PRODUCTS
        for req in lane.requests:
            for att in req.attempts:
                for inf in att.inferences:
                    prem = band_premium(ctx.pricer, inf, att.ts_start_ms)
                    if prem is None or prem.high <= 0:
                        continue
                    model = _safe_name(inf.pricing.model) or _UNKNOWN
                    rec = models.setdefault(model, _BandModel())
                    rec.premium.add(prem.point, prem.low, prem.high, prem.exact)
                    rec.ranged += prem.low != prem.high
                    rec.tier_known += prem.tier_known
                    rec.tally.events += 1
                    rec.tally.touch(lane, (req,), att.ts_start_ms)
                    if reached:
                        rec.reached.add(prem.point, prem.low, prem.high, prem.exact)
                        rec.reached_lanes.add(lane.lane_key)
    lever_ids, lever_class, needs_eval = _levers("long-context-band")
    out = []
    for model, rec in sorted(models.items()):
        cost = rec.premium.figure(cohort.basis, "long-context band hypotheses A/B differ: "
                                                "range, point A (dq.copilot_band_hypothesis)")
        prem = rec.premium
        ranged = prem.low != prem.point or prem.high != prem.point
        recoverable = estimated(
            prem.point, cohort.basis, low=prem.low if ranged else None,
            high=prem.high if ranged else None, upper_bound=True,
            note="upper bound: the band premium if every banded request had stayed at the "
                 "default tier; the managed delivery reaches CLI lanes only (reach evidence)")
        how = ("exact rate arithmetic on identical tokens" if cost.evidence is Evidence.EXACT
               else f"a range: on {rec.ranged} of them the context tier and the request size "
                    f"disagree about the band")
        reach = (" The repository contextTier default reaches only the Copilot CLI (trusted "
                 "directories); elsewhere keep the default tier and /compact at task boundaries."
                 if len(rec.reached_lanes) < len(rec.tally.lanes) else "")
        spec = _Spec(
            kind="long-context-band", category="lever",
            title=f"Long-context band premium on {model} in {cohort.label()}",
            summary=(f"{rec.tally.events} inferences on {model} in {cohort.label()} were priced "
                     f"at long-context band rates; the premium over default-tier rates on the "
                     f"same tokens is {how}.{reach}"),
            confidence="high" if cost.evidence is Evidence.EXACT else "medium",
            lever_ids=lever_ids, lever_class=lever_class, needs_eval=needs_eval)
        items = [
            _item(f"band:{model}", inferences=rec.tally.events, ranged=rec.ranged,
                  tier_known=rec.tier_known, nano=rec.premium.point, low_nano=rec.premium.low,
                  high_nano=rec.premium.high),
            _item("reach:copilot.context_default", reached_lanes=len(rec.reached_lanes),
                  lanes=len(rec.tally.lanes), reached_nano=rec.reached.point,
                  reached_high_nano=rec.reached.high,
                  reached_share_pct=_pct(rec.reached.high, prem.high), trusted_share=_UNKNOWN),
        ]
        found = _emit(det, ctx, cohort, spec, rec.tally, cost, recoverable, items, model=model)
        if found is not None:
            out.append(found)
    return out


# ---------------------------------------------------------------------------------------------
# compaction-cost
# ---------------------------------------------------------------------------------------------


def _trigger(ev: LaneEvent) -> str:
    attrs = dict(ev.attrs)
    for key in ("copilot_trigger", "trigger"):
        name = _safe_name(attrs.get(key))
        if name is not None:
            return name
    return _UNKNOWN


def _join_compactions(infs: Sequence[tuple[int, Inference]], events: Sequence[LaneEvent]
                      ) -> list[tuple[str, tuple[int, Inference] | None]]:
    """Pair each COMPACTION inference with the unmatched COMPACTION event nearest in time (within
    ±60 s; ties → the earlier event), in time order; unmatched events and inferences are kept
    alone (an inference alone has trigger ``unknown``)."""
    ordered_events = sorted(events, key=lambda ev: ev.ts_ms)
    used = [False] * len(ordered_events)
    out: list[tuple[str, tuple[int, Inference] | None]] = []
    for ts_ms, inf in sorted(infs, key=lambda p: (p[0], p[1].inference_id)):
        best: tuple[int, int, int] | None = None
        for idx, ev in enumerate(ordered_events):
            gap = abs(ev.ts_ms - ts_ms)
            if used[idx] or gap > _COMPACTION_JOIN_MS:
                continue
            cand = (gap, ev.ts_ms, idx)
            if best is None or cand < best:
                best = cand
        if best is None:
            out.append((_UNKNOWN, (ts_ms, inf)))
        else:
            used[best[2]] = True
            out.append((_trigger(ordered_events[best[2]]), (ts_ms, inf)))
    out.extend((_trigger(ev), None) for idx, ev in enumerate(ordered_events) if not used[idx])
    return out


@dataclass
class _Trigger:
    count: int = 0
    money: _Acc = field(default_factory=_Acc)


def _compaction(det: CopilotLanes, ctx: AnalysisContext, cohort: _Cohort) -> list[Finding]:
    triggers: dict[str, _Trigger] = {}
    tally = _Tally()
    total = _Acc()
    for lane in cohort.lanes:
        infs: list[tuple[int, Inference]] = []
        owners: dict[str, Request] = {}
        for req in lane.requests:
            for att in req.attempts:
                for inf in att.inferences:
                    if inf.kind is InferenceKind.COMPACTION and inf.billable is not False:
                        infs.append((att.ts_start_ms, inf))
                        owners[inf.inference_id] = req
        events = [ev for ev in lane.events if ev.kind is LaneEventKind.COMPACTION]
        if not infs and not events:
            continue
        for name, pair in _join_compactions(infs, events):
            rec = triggers.setdefault(name, _Trigger())
            rec.count += 1
            tally.events += 1
            if pair is None:
                tally.touch(lane, lane.requests, None)
                continue
            ts_ms, inf = pair
            fig = ctx.pricer.price_inference(inf, ts_ms=ts_ms).figure
            rec.money.add_figure(fig)
            total.add_figure(fig)
            tally.touch(lane, (owners[inf.inference_id],), ts_ms)
    if not total.n:
        return []
    forced = sum(t.count for name, t in triggers.items() if name in FORCED_TRIGGERS)
    forced_money = _Acc()
    for name, t in triggers.items():
        if name in FORCED_TRIGGERS:
            forced_money.merge(t.money)
    share = _pct(forced, tally.events)
    cost = total.figure(cohort.basis, "compaction credits include estimated lines")
    spec = _Spec(
        kind="compaction-cost", category="lever",
        title=f"Compaction spend in {cohort.label()} ({share}% forced)",
        summary=(f"{tally.events} compactions in {cohort.label()}, {forced} forced by the context "
                 f"limit or memory pressure ({share}%); the compaction calls are priced at their "
                 f"billed tokens.{_unpriced_note(total)}"),
        confidence="high" if cost.evidence is Evidence.EXACT else "medium")
    items = [_item(f"trigger:{name}", compactions=t.count, priced=t.money.n, nano=t.money.point)
             for name, t in sorted(triggers.items())]
    items.append(_item("forced", compactions=forced, total=tally.events, share_pct=share,
                       nano=forced_money.point, magnitude=forced_money.point))
    found = _emit(det, ctx, cohort, spec, tally, cost, None, items)
    return [found] if found is not None else []


# ---------------------------------------------------------------------------------------------
# static-overhead
# ---------------------------------------------------------------------------------------------


class _Static(NamedTuple):
    ts_ms: int
    system: int
    tools: int | None


def _static_events(lane: Lane) -> list[_Static]:
    out = []
    for ev in lane.events:
        if ev.kind is not LaneEventKind.COMPACTION:
            continue
        attrs = dict(ev.attrs)
        system, tools = attrs.get("system_tokens"), attrs.get("tool_definitions_tokens")
        system = system if type(system) is int and system > 0 else None
        tools = tools if type(tools) is int and tools > 0 else None
        if system is None and tools is None:
            continue
        out.append(_Static(ev.ts_ms, system or 0, tools))
    return sorted(out, key=lambda st: st.ts_ms)


def _static_for(statics: Sequence[_Static], ts_ms: int) -> _Static:
    """The latest static report at or before *ts_ms*, else the first (the static prefix is
    session configuration, reported at each compaction)."""
    chosen = statics[0]
    for st in statics:
        if st.ts_ms <= ts_ms:
            chosen = st
    return chosen


@dataclass
class _StaticRec:
    carry: Fraction = Fraction(0)
    tools_carry: Fraction = Fraction(0)
    tools_reached_carry: Fraction = Fraction(0)
    tools_known: bool = False
    reached_lanes: set[str] = field(default_factory=set)
    unpriced: int = 0
    from_events: set[str] = field(default_factory=set)
    from_floor: set[str] = field(default_factory=set)
    static_max: int = 0
    tools_max: int = 0
    tally: _Tally = field(default_factory=_Tally)


def _static_overhead(det: CopilotLanes, ctx: AnalysisContext, cohort: _Cohort) -> list[Finding]:
    rec = _StaticRec()
    floors = ctx.static_prefix_floor or {}
    for lane in cohort.lanes:
        statics = _static_events(lane)
        reached = _agent_product(lane) in MCP_TRIM_PRODUCTS
        for req in lane.requests:
            inf = req.serving_inference
            if inf is None or inf.billable is False or inf.usage.total_input <= 0:
                continue
            if statics:
                st = _static_for(statics, req.ts_start_ms)
                static, tools = st.system + (st.tools or 0), st.tools
                rec.from_events.add(lane.lane_key)
            else:
                floor = floors.get((lane.cache_scope_key, inf.pricing.model))
                if not floor or floor <= 0:
                    continue
                static, tools = floor, None
                rec.from_floor.add(lane.lane_key)
            priced = ctx.pricer.price_inference(inf, ts_ms=req.final_attempt.ts_start_ms)
            if priced.figure.nano is None:
                rec.unpriced += 1
                continue
            rec.carry += static_carry(priced, inf, static)
            rec.static_max = max(rec.static_max, static)
            if tools is not None:
                carry = static_carry(priced, inf, tools)
                rec.tools_carry += carry
                rec.tools_max = max(rec.tools_max, tools)
                rec.tools_known = True
                if reached:
                    rec.tools_reached_carry += carry
                    rec.reached_lanes.add(lane.lane_key)
            rec.tally.events += 1
            rec.tally.touch(lane, (req,), req.ts_start_ms)
    if not rec.tally.events:
        return []
    source = ("COMPACTION events" if not rec.from_floor else
              "the static-prefix floor" if not rec.from_events else
              "COMPACTION events and the static-prefix floor")
    cost = estimated(_round(rec.carry), cohort.basis,
                     note=f"static tokens from {source}, carried at each request's read / write "
                          f"/ uncached mix")
    recoverable = None
    low, point, high = _band()
    if rec.tools_known:
        recoverable = estimated(
            _round(rec.tools_carry * point), cohort.basis, low=_round(rec.tools_carry * low),
            high=_round(rec.tools_carry * high), upper_bound=True,
            note="upper bound: tool-definition carry x tool-search reduction band "
                 "[0.50, 0.85], point 0.70")
    lever_ids, lever_class, needs_eval = _levers("static-overhead")
    tail = ("" if recoverable is not None else
            " The tool-definition share is unknown (no COMPACTION static-token report), so no "
            "reduction is projected.")
    unpriced = (f" {rec.unpriced} requests had no priced rate and are left out."
                if rec.unpriced else "")
    spec = _Spec(
        kind="static-overhead", category="lever",
        title=f"Static prompt overhead (system + tool definitions) in {cohort.label()}",
        summary=(f"{rec.tally.events} requests in {cohort.label()} re-sent a static prefix of up "
                 f"to {rec.static_max} tokens (up to {rec.tools_max} of them tool definitions, "
                 f"from {source}); its carry at the lanes' read / write mix is an estimate."
                 f"{tail}{unpriced}"),
        confidence="medium" if not rec.from_floor else "low",
        lever_ids=lever_ids, lever_class=lever_class, needs_eval=needs_eval)
    items = [
        _item("static:prefix", requests=rec.tally.events, static_tokens_max=rec.static_max,
              lanes_from_events=len(rec.from_events), lanes_from_floor=len(rec.from_floor),
              nano=_round(rec.carry)),
        _item("static:tool_definitions", tool_definitions_tokens_max=rec.tools_max,
              carry_nano=_round(rec.tools_carry), band_low="0.50", band_point="0.70",
              band_high="0.85"),
        _item("reach:copilot.mcp_trim", reached_lanes=len(rec.reached_lanes),
              lanes=len(rec.from_events), reached_carry_nano=_round(rec.tools_reached_carry)),
    ]
    found = _emit(det, ctx, cohort, spec, rec.tally, cost, recoverable, items)
    return [found] if found is not None else []


# ---------------------------------------------------------------------------------------------
# subagent-share
# ---------------------------------------------------------------------------------------------


def _agent_type(lane: Lane) -> str:
    """The first request's ``agent_type`` (else a SESSION_META ``agent_type``), identifier-like
    names only (:func:`_safe_name`)."""
    for req in lane.requests:
        name = _safe_name(req.attribution.agent_type)
        if name is not None:
            return name
    for ev in lane.events:
        if ev.kind is LaneEventKind.SESSION_META:
            name = _safe_name(dict(ev.attrs).get("agent_type"))
            if name is not None:
                return name
    return _UNKNOWN


@dataclass
class _AgentRec:
    money: _Acc = field(default_factory=_Acc)
    lanes: int = 0
    requests: int = 0


def _subagent_share(det: CopilotLanes, ctx: AnalysisContext, cohort: _Cohort) -> list[Finding]:
    if cohort.lane_kind != LaneKind.SUBAGENT.value:
        return []
    types: dict[str, _AgentRec] = {}
    tally = _Tally()
    total = _Acc()
    for lane in cohort.lanes:
        rec = types.setdefault(_agent_type(lane), _AgentRec())
        money = _spend(ctx, lane.requests)
        rec.money.merge(money)
        total.merge(money)
        rec.lanes += 1
        rec.requests += len(lane.requests)
        tally.events += len(lane.requests)
        tally.touch(lane, lane.requests, lane.requests[0].ts_start_ms)
    if not total.n:
        return []
    ranked = sorted(types.items(), key=lambda kv: (-kv[1].money.point, kv[0]))
    top_name, top = ranked[0]
    top_share = _pct(top.money.point, total.point)
    cost = total.figure(cohort.basis, "subagent spend includes estimated lines")
    spec = _Spec(
        kind="subagent-share", category="aggregate",
        title=f"Subagent spend by agent type in {cohort.label()}",
        summary=(f"{tally.events} subagent requests on {len(tally.lanes)} lanes in "
                 f"{cohort.label()}; agent type {top_name} carries {top_share}% of the subagent "
                 f"spend. Cheaper subagent models cut it (price-only; evaluate quality first)."
                 f"{_unpriced_note(total)}"),
        confidence="high" if cost.evidence is Evidence.EXACT else "medium")
    items = [_item(f"agent_type:{name}", nano=r.money.point,
                   share_pct=_pct(r.money.point, total.point), lanes=r.lanes,
                   requests=r.requests)
             for name, r in ranked]
    found = _emit(det, ctx, cohort, spec, tally, cost, None, items)
    return [found] if found is not None else []


# ---------------------------------------------------------------------------------------------
# ci-uncapped
# ---------------------------------------------------------------------------------------------


def _is_ci(lane: Lane) -> bool:
    return lane.requests[0].attribution.workload_class is WorkloadClass.CI


def _credit_limit(lane: Lane) -> int | None:
    limits = [value for ev in lane.events if ev.kind is LaneEventKind.SESSION_META
              for key, value in ev.attrs
              if key == "credit_limit_nano" and type(value) is int and value > 0]
    return min(limits) if limits else None


@dataclass
class _Session:
    money: _Acc = field(default_factory=_Acc)
    limit: int | None = None
    lanes: list[Lane] = field(default_factory=list)


def _credits(nano: int) -> str:
    return nano_to_credits_str(nano)


def _ci_uncapped(det: CopilotLanes, ctx: AnalysisContext, cohort: _Cohort) -> list[Finding]:
    products: dict[str, dict[str, _Session]] = {}
    for lane in cohort.lanes:
        if not _is_ci(lane):
            continue
        sessions = products.setdefault(_agent_product(lane), {})
        session = sessions.setdefault(lane.session_key, _Session())
        session.money.merge(_spend(ctx, lane.requests))
        session.lanes.append(lane)
        limit = _credit_limit(lane)
        if limit is not None:
            session.limit = limit if session.limit is None else min(session.limit, limit)
    out = []
    for product, sessions in sorted(products.items()):
        found = _ci_finding(det, ctx, cohort, product, sessions)
        if found is not None:
            out.append(found)
    return out


def _ci_finding(det: CopilotLanes, ctx: AnalysisContext, cohort: _Cohort, product: str,
                sessions: Mapping[str, _Session]) -> Finding | None:
    gh_aw = product == CI_PRODUCT_GH_AW
    default_cap = default_run_cap_nano()
    spends = [s.money.point for _, s in sorted(sessions.items())]
    p50, p90, p99 = (_nearest_rank(spends, p) for p in _CI_PERCENTILES)
    suggested = -(-_round(Fraction(p99) * _CAP_FACTOR) // NANO_USD_PER_CREDIT)
    tally = _Tally()
    uncapped = _Acc()
    n_uncapped = over_cap = 0
    for _, session in sorted(sessions.items()):
        # gh-aw runs without an explicit cap run at the default cap; CLI sessions without a
        # credit limit are uncapped
        cap = session.limit if session.limit is not None else (default_cap if gh_aw else None)
        if cap is not None and session.money.point >= cap:
            over_cap += 1
        if session.limit is not None:
            continue
        n_uncapped += 1
        uncapped.merge(session.money)
        tally.events += 1
        for lane in session.lanes:
            tally.touch(lane, lane.requests, lane.requests[0].ts_start_ms)
    if not n_uncapped or not uncapped.n:
        return None
    share = _pct(n_uncapped, len(sessions))
    cost = uncapped.figure(cohort.basis, "CI spend includes estimated lines")
    what = "gh-aw runs" if gh_aw else "CI sessions"
    if gh_aw:
        detail = (f"{n_uncapped} of {len(sessions)} {what} ({share}%) set no max-ai-credits and "
                  f"run at the default cap of {_credits(default_cap)} AI credits per run, against "
                  f"a p99 of {_credits(p99)} credits per run")
        cap_item = _item("ci:cap", cap_nano=default_cap, cap_credits=_credits(default_cap),
                         cap_source="default", p99_nano=p99, over_cap=over_cap,
                         suggested_max_ai_credits=suggested)
    else:
        detail = (f"{n_uncapped} of {len(sessions)} {what} ({share}%) ran without a credit "
                  f"limit (--max-ai-credits)")
        cap_item = _item("ci:cap", cap_source="session_limit", p99_nano=p99, over_cap=over_cap,
                         suggested_max_ai_credits=suggested)
    wanted = _CI_LEVER_GH_AW if gh_aw else _CI_LEVER_CLI
    levers = [lv for lv in catalog.levers_for_kind("ci-uncapped", family=COPILOT_FAMILY)
              if lv.lever_id == wanted]
    spec = _Spec(
        kind="ci-uncapped", category="aggregate",
        title=f"Uncapped Copilot {what} in {cohort.label()}",
        summary=(f"{detail}; spend per session p50 {_credits(p50)} and p90 {_credits(p90)} "
                 f"credits. A cap near p99 x 1.5 ({suggested} credits) bounds a runaway run."
                 f"{_unpriced_note(uncapped)}"),
        confidence="high" if cost.evidence is Evidence.EXACT else "medium",
        lever_ids=tuple(lv.lever_id for lv in levers),
        lever_class=levers[0].lever_class if levers else "none")
    items = [
        _item("ci:sessions", sessions=len(sessions), uncapped=n_uncapped, uncapped_share_pct=share,
              p50_nano=p50, p90_nano=p90, nano=uncapped.point),
        cap_item,
    ]
    return _emit(det, ctx, cohort, spec, tally, cost, None, items, workload_class="ci",
                 agent_product=product)


# ---------------------------------------------------------------------------------------------
# the detector
# ---------------------------------------------------------------------------------------------

_KindFn = Callable[["CopilotLanes", AnalysisContext, _Cohort], list[Finding]]
_KIND_FUNCS: tuple[_KindFn, ...] = (_long_context, _compaction, _static_overhead,
                                    _subagent_share, _ci_uncapped)


class CopilotLanes:
    """``copilot.lanes`` (addendum §10.3): the request-level causes of Copilot credit spend on
    Copilot lanes — ``long-context-band``, ``compaction-cost``, ``static-overhead``,
    ``subagent-share``, ``ci-uncapped``. A lane detector (shard-invariant within
    ``core.findings.cohort_key``) of family ``copilot`` behind the ``copilot`` extension; each
    kind checks its own per-lane inputs (:data:`KIND_INPUTS`)."""

    id = DETECTOR_ID
    version = _VERSION
    kinds = KINDS
    requires = frozenset({"credits"})
    extension = "copilot"
    aggregate = False
    families = frozenset({COPILOT_FAMILY})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Every kind whose inputs a cohort holds, at or above ``min_usd``, sorted by id."""
        out: list[Finding] = []
        for cohort in _cohorts(lanes, ctx):
            for fn in _KIND_FUNCS:
                out.extend(fn(self, ctx, cohort))
        return sorted(out, key=lambda f: f.finding_id)

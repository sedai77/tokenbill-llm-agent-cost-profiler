"""GitHub Copilot org scan (addendum §10.2, package CP-DET-USAGE): ``copilot.org-scan``.

Names the causes of Copilot AI-credit and Actions spend from GitHub's own billing data (the AI
usage report, the detailed usage report, usage metrics, agent tasks and gh-aw runs) — 100% of
billed usage, no collectors. Lanes are never read (``aggregate=True``, ruling R-E17).

**Inputs.** Cells come from ``core.pool.build_cells`` over ``ctx.aggregates`` + ``ctx.cost_lines``
at day grain with the report token convention CP-RECON decided (``ctx.recon_decisions``
``convention:<source>``: all ``incl`` → ``incl``; no decision → ``excl`` by default; undecidable or
mixed → ``excl`` and token-derived figures ESTIMATED, ruling R-E46). Pool months come from
``ctx.pools`` (two per entity × month while the plan is unknown, R-E22), activity from
``ctx.activity`` or, in aggregate-only bundles, ``ConfigSnapshot(kind="activity_counts")`` rows,
outcomes from ``ctx.outcomes``.

**Money (R11, R16, R-E20).** Credit valuations (report gross, cell repricing) are LIST_EQUIVALENT.
Every credit saving is converted through ``core.pool.realize_credit_saving`` with the entity's
pool month of each month: ``recoverable`` = the invoice part (ESTIMATED, LIST), ``headroom`` = the
rest (LIST_EQUIVALENT); while the plan is unknown each pool-dependent finding is emitted once per
scenario (scope dim ``plan_scenario``, title "If Business:" / "If Enterprise:"), never merged.
Direct-org credits (``ai_credit.direct``) are metered to the organization: their saving is invoice
dollars unless the rows show they draw the pool (``PoolMonth.direct_draws_pool``). Dollar amounts
(direct net, Actions net) are INVOICE only for final rows of a closed month on a reconciled
channel, else EXACT LIST with "unreconciled" / "provisional"; mixed sets go through
``core.labels.combine_weakest``. Credits and dollars are never added in one figure.

**Gating.** ``requires = {"aggregates"}`` (the brief); each kind is further gated any-of by
:data:`KIND_REQUIRES` (alternative capability sets, each all-of) and by its own inputs; every kind
that cannot run is named once in a single ``dq.skipped-kinds`` data-quality finding.

**Privacy.** Scopes carry ``product: copilot``, ``entity``, ``team`` (or ``cost_center`` for
report rows without a team), ``model`` and ``plan_scenario`` only; no principal, login or ``p_``
value ever reaches a finding (people are only counted for ``n_users``). Every finding is category
``aggregate`` (the brief; the skipped-kinds note is ``data-quality``), and publication is
``core.kanon.rescope_findings`` with the catalog's count sources (R-E16: team-scoped findings are
never exempt, whatever their category).

**Thresholds** (``ctx.thresholds["copilot.org-scan.<name>"]``, decimal strings): ``min_usd``
(the shared one), ``jetbrains_policy_share`` (0.5), ``mcp_heavy_distinct`` (5), ``mcp_heavy_share``
(0.25), ``cli_heavy_tokens`` (100000), ``review_window_days`` (30).
"""

from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import NamedTuple

from tokenbill.core import catalog, pool
from tokenbill.core import facts as _facts
from tokenbill.core.errors import PricingError, UsageError
from tokenbill.core.evidence import TOKENIZER_BAND
from tokenbill.core.findings import (
    MAX_SUMMARY,
    MAX_TITLE,
    build_finding,
    make_scope,
    min_usd_gate,
    min_usd_nano,
    threshold,
    top_evidence,
)
from tokenbill.core.labels import (
    Basis,
    Calibration,
    Evidence,
    Figure,
    Finality,
    add,
    combine_weakest,
    exact,
    unpriced,
)
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, fmt_usd, nano_to_credits_str
from tokenbill.core.records import (
    COPILOT_WORKLOADS,
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    Lane,
    PricingContext,
    UsageAggregate,
    UsageBuckets,
)
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import (
    AnalysisContext,
    EvidenceItem,
    Finding,
    Fix,
    PoolMonth,
    UnitRates,
)

__all__ = [
    "DETECTOR_ID",
    "FAILED_STATES",
    "KINDS",
    "KIND_REQUIRES",
    "MODEL_KEY_UNREACHED",
    "CopilotOrgScan",
    "Reach",
    "team_reach",
]

DETECTOR_ID = "copilot.org-scan"
_VERSION = "1"

#: The kinds of addendum §10.2 in table order, plus the data-quality note of skipped kinds.
KINDS = (
    "premium-model-share", "fast-mode", "auto-adoption", "forced-migration", "compliance-uplift",
    "cache-health", "review-cost", "review-default-balanced", "review-drivers",
    "direct-org-usage", "agentic-workflow-cost", "larger-runner", "cloud-agent-cost",
    "agent-failed-sessions", "unattributed-spend", "mcp-sprawl", "context-heavy-cli",
    "editor-mix", "dq.skipped-kinds",
)

_REPORT = frozenset({"aggregates", "copilot_billing"})
#: Per-kind capability gate (addendum §10.0): a kind runs when at least one of its capability sets
#: (each needed all-of) is a subset of ``ctx.capabilities``; otherwise it is named in
#: ``dq.skipped-kinds``. Kinds absent here always run.
KIND_REQUIRES: Mapping[str, tuple[frozenset[str], ...]] = MappingProxyType({
    **{k: (_REPORT,) for k in (
        "premium-model-share", "fast-mode", "auto-adoption", "forced-migration",
        "compliance-uplift", "cache-health", "review-cost", "review-default-balanced",
        "review-drivers", "direct-org-usage", "cloud-agent-cost", "unattributed-spend")},
    "larger-runner": (frozenset({"copilot_billing"}), frozenset({"cost"})),
    "agentic-workflow-cost": (frozenset({"copilot_billing"}), frozenset({"aggregates"})),
    "agent-failed-sessions": (frozenset({"aggregates"}),),
    "mcp-sprawl": (frozenset({"activity"}),),
    "context-heavy-cli": (frozenset({"activity"}),),
    "editor-mix": (frozenset({"activity"}), frozenset({"licenses"}), frozenset({"config"})),
})

#: Editor families the managed-settings ``model`` key does not reach (§9.3): JetBrains (verified
#: not to), Visual Studio / Xcode / Eclipse (unverified → excluded).
MODEL_KEY_UNREACHED = ("jetbrains", "visual_studio", "xcode", "eclipse")
#: Agent-task session states counted as failed (addendum §10.2).
FAILED_STATES = ("failed", "timed_out", "cancelled")

_CREDIT_TYPES = ("ai_credit.user", "ai_credit.direct")
_DIRECT = "ai_credit.direct"
_COPILOT_CHANNEL = "github_copilot"
_ACTIONS_CHANNEL = "github_actions"
_AGENT_TASKS = "github.agent_tasks"
_GH_AW_RUN = "gh_aw.run"
_METRICS_OUTCOMES = "github.copilot_metrics"
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_MAX_DATE_MS = 253_402_300_799_999
_NANO_PER_CREDIT = 10_000_000
_AUTO_DISCOUNT = Fraction(1, 10)
_COMPLIANCE_DIVISOR = 11      # uplift = observed × (1 − 1/1.1) = observed / 11
_STANDARD_RUNNER = "actions_linux"
_SCENARIO_TITLE = {"business": "If Business: ", "enterprise": "If Enterprise: "}
_SCENARIO_TEXT = {"business": "if all unknown seats are Business",
                  "enterprise": "if all unknown seats are Enterprise"}
_BAND = tuple(Fraction(str(v)) for v in TOKENIZER_BAND.value)  # type: ignore[union-attr]

_REFS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "premium-model-share": ("copilot-cost-levers-model-policy", "copilot-billing-rates"),
    "fast-mode": ("copilot-billing-fast-mode", "copilot-billing-rates"),
    "auto-adoption": ("copilot-billing-auto-discount", "copilot-cost-levers-auto-reach"),
    "forced-migration": ("copilot-billing-retirements", "copilot-billing-rates"),
    "compliance-uplift": ("copilot-billing-compliance-uplift",),
    "cache-health": ("copilot-cost-levers-cache", "copilot-client-telemetry"),
    "review-cost": ("copilot-billing-code-review",),
    "review-default-balanced": ("copilot-billing-code-review", "copilot-review-default-balanced"),
    "review-drivers": ("copilot-cost-levers-review-drivers",),
    "direct-org-usage": ("copilot-billing-direct-org",),
    "agentic-workflow-cost": ("copilot-billing-agentic-workflows",),
    "larger-runner": ("copilot-billing-actions-runners",),
    "cloud-agent-cost": ("copilot-billing-cloud-agent", "copilot-data-apis-metrics"),
    "agent-failed-sessions": ("copilot-data-apis-agent-tasks",),
    "unattributed-spend": ("copilot-billing-cost-centers",),
    "mcp-sprawl": ("copilot-data-apis-metrics", "copilot-cost-levers-mcp"),
    "context-heavy-cli": ("copilot-data-apis-metrics", "copilot-cost-levers-context"),
    "editor-mix": ("copilot-data-apis-metrics", "copilot-cost-levers-auto-reach"),
    "dq.skipped-kinds": ("dq.skipped-kinds",),
})

_Triple = list[int]   # [low, point, high] in nano
_PRICED_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
                   "cache_write_other", "output")


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _round(value: Fraction) -> int:
    """Half-even rounding of an exact fraction to an int."""
    q, r = divmod(value.numerator, value.denominator)
    twice = 2 * r
    if twice > value.denominator or (twice == value.denominator and q % 2 == 1):
        q += 1
    return q


def _dec(value: Fraction, places: int = 4) -> str:
    """A fraction as a fixed-point decimal string (half-even), trailing zeros stripped."""
    scaled = _round(value * 10**places)
    sign = "-" if scaled < 0 else ""
    whole, frac = divmod(abs(scaled), 10**places)
    text = f"{sign}{whole}.{str(frac).rjust(places, '0')}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def _pct(value: Fraction) -> str:
    return _dec(value * 100, 1) + "%"


def _credits(nano: int) -> str:
    return nano_to_credits_str(_round(Fraction(nano, _NANO_PER_CREDIT)) * _NANO_PER_CREDIT)


def _rank(values: Sequence[int], q: Fraction) -> int:
    """Nearest-rank percentile: index ``ceil(q·n) − 1`` of the ascending series."""
    ordered = sorted(values)
    n = len(ordered)
    idx = max(0, -((-q.numerator * n) // q.denominator) - 1)
    return ordered[min(idx, n - 1)]


def _median(values: Sequence[Fraction]) -> Fraction:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _day_ms(date_utc: str) -> int:
    return (_dt.date.fromisoformat(date_utc) - _EPOCH).days * _DAY_MS


def _date_of_ms(ms: int) -> _dt.date | None:
    if ms < 0 or ms > _MAX_DATE_MS:
        return None
    return _EPOCH + _dt.timedelta(days=ms // _DAY_MS)


def _last_day(month: str) -> _dt.date:
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 12:
        return _dt.date(year, 12, 31)
    return _dt.date(year, mon + 1, 1) - _dt.timedelta(days=1)


def _label(name: str | None, fallback: str = "unattributed") -> str:
    """A team / model name for generated text: control characters removed, never cut (``kanon``
    scrubs dropped scope values as whole tokens); over 40 characters reads *fallback*."""
    if not name:
        return fallback
    clean = sanitize(name)
    return clean if len(clean) <= 40 else "the team"


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


def _item(ref: str, **attrs: str | int | None) -> EvidenceItem:
    pairs = tuple(sorted((k, v) for k, v in attrs.items() if v is not None))
    return EvidenceItem(kind="aggregate", ref=ref, attrs=pairs)


def _estimated(tri: Sequence[int], basis: Basis, note: str, *, upper: bool = False,
               evidence_exact: bool = False) -> Figure:
    """A figure from ``[low, point, high]``: EXACT when *evidence_exact* and zero-width, else
    ESTIMATED (uncalibrated) with the range when it is not zero-width."""
    lo, pt, hi = min(tri), tri[1], max(tri)
    if evidence_exact and lo == pt == hi and not upper:
        return Figure(nano=pt, evidence=Evidence.EXACT, basis=basis, note=note)
    ranged = lo != hi
    return Figure(nano=pt, evidence=Evidence.ESTIMATED, basis=basis,
                  low_nano=lo if ranged else None, high_nano=hi if ranged else None,
                  calibration=Calibration.UNCALIBRATED, upper_bound=upper, note=note)


def _add_tri(a: _Triple, b: Sequence[int], sign: int = 1) -> None:
    for i in range(3):
        a[i] += sign * b[i]


def _threshold(inp: _In, name: str, default: str, high: int, *, low: int = 0) -> Decimal:
    """``ctx.thresholds["copilot.org-scan.<name>"]`` within ``[low, high]`` (else
    ``UsageError`` naming the key), so a hostile value can never stall the arithmetic."""
    key = f"{DETECTOR_ID}.{name}"
    value = threshold(inp.ctx, key, default)
    if not low <= value <= high:
        raise UsageError(f"threshold {key}: must be in [{low}, {high}]")
    return value


def _lever_class(lever_ids: Sequence[str]) -> str:
    return catalog.lever(lever_ids[0]).lever_class if lever_ids else "none"


class _Row(NamedTuple):
    """The cell-key view of a cost line (the same attribute names as ``core.pool.Cell``)."""

    entity_id: str
    team: str | None
    cost_center: str | None
    org: str | None
    sku: str
    cost_type: str
    model: str
    routing: str
    speed: str
    pseudo: str | None
    workload: str | None
    date_utc: str


# ---------------------------------------------------------------------------------------------
# delivery reach (§9.3)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reach:
    """Share of a team's affected credits the managed ``model`` key reaches (§9.3).

    ``source``: ``metrics`` (``ActivityDay`` ``ide:*`` counts), ``activity_counts`` (aggregate-only
    bundles), ``cli_app`` (only CLI / Copilot app activity: reached), ``none`` (reach unknown: point
    and low 0, high 1)."""

    point: Fraction
    low: Fraction
    high: Fraction
    source: str
    shares: tuple[tuple[str, Fraction], ...] = ()    # editor family → share of interactions
    interactions: int = 0

    @property
    def known(self) -> bool:
        return self.source != "none"

    @property
    def jetbrains_share(self) -> Fraction:
        return dict(self.shares).get("jetbrains", Fraction(0))

    @property
    def unreached_share(self) -> Fraction:
        return sum((s for f, s in self.shares if f in MODEL_KEY_UNREACHED), Fraction(0))


_UNKNOWN_REACH = Reach(Fraction(0), Fraction(0), Fraction(1), "none")


def _family_counts(pairs: Iterable[tuple[str, object]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for key, value in pairs:
        if key.startswith("ide:") and type(value) is int and value > 0:
            out[catalog.editor_family(key)] += value
    return out


def team_reach(activity: Iterable[ActivityDay], config: Iterable[ConfigSnapshot],
               team: str | None) -> Reach:
    """The managed-``model`` reach of *team* (None = unattributed): 1 − the share of its ``ide:*``
    interactions on :data:`MODEL_KEY_UNREACHED` families (``core.catalog.editor_family``), from
    per-user activity days, else from ``activity_counts`` rows; a team with only CLI / Copilot app
    activity is reached (1); no activity → unknown (point 0, range [0, 1])."""
    counts: dict[str, int] = defaultdict(int)
    other = 0
    for day in activity:
        if (day.team or None) != team:
            continue
        for fam, n in _family_counts(day.counts).items():
            counts[fam] += n
        c = dict(day.counts)
        other += c.get("cli_requests", 0) + c.get("app_requests", 0)
    source = "metrics"
    if not counts:
        source = "activity_counts"
        for snap in config:
            attrs = dict(snap.attrs)
            if snap.kind != "activity_counts" or (attrs.get("team") or None) != team:
                continue
            for fam, n in _family_counts(attrs.items()).items():
                counts[fam] += n
            for key in ("cli_requests", "app_interactions"):
                v = attrs.get(key)
                other += v if type(v) is int and v > 0 else 0
    total = sum(counts.values())
    if total == 0:
        if other > 0:
            return Reach(Fraction(1), Fraction(1), Fraction(1), "cli_app")
        return _UNKNOWN_REACH
    shares = tuple(sorted((fam, Fraction(n, total)) for fam, n in counts.items()))
    unreached = sum((Fraction(n, total) for fam, n in counts.items()
                     if fam in MODEL_KEY_UNREACHED), Fraction(0))
    r = 1 - unreached
    return Reach(r, r, r, source, shares, total)


# ---------------------------------------------------------------------------------------------
# prepared inputs
# ---------------------------------------------------------------------------------------------


@dataclass
class _Savings:
    """A credit saving per month, split into pooled and direct-org credits."""

    pooled: dict[str, _Triple] = field(default_factory=dict)
    direct: dict[str, _Triple] = field(default_factory=dict)
    unpriced: str | None = None
    upper: bool = False

    def add(self, month: str, cost_type: str, tri: Sequence[int]) -> None:
        book = self.direct if cost_type == _DIRECT else self.pooled
        _add_tri(book.setdefault(month, [0, 0, 0]), tri)

    def total(self) -> _Triple:
        out = [0, 0, 0]
        for book in (self.pooled, self.direct):
            for tri in book.values():
                _add_tri(out, tri)
        return out

    def months(self) -> list[str]:
        return sorted(set(self.pooled) | set(self.direct))


class _In:
    """Everything the kinds read, prepared once per ``detect`` call."""

    def __init__(self, ctx: AnalysisContext) -> None:
        self.ctx = ctx
        self.today = self._today(ctx)
        self.lag = _facts.copilot_report_lag_days()
        self.min_usd = min_usd_nano(ctx)
        self.config = tuple(c for c in ctx.config if isinstance(c, ConfigSnapshot))
        self.flags = pool.run_flags(self.config)
        compliance = self.flags.get("compliance")
        self.compliance = compliance if compliance in ("data_residency", "fedramp") else None
        self.capped = pool.capped_cost_centers(self.config)
        ids = {p.entity_id for p in ctx.pools} | {p.entity_id for p in ctx.plans}
        self.mode = "org" if any(e.startswith("org:") for e in ids) else "enterprise"
        self.convention, self.convention_state = self._convention(ctx.recon_decisions)
        self.cells, _ = pool.build_cells(ctx.aggregates, ctx.cost_lines, grain="day",
                                         convention=self.convention, capped=self.capped,
                                         entity_mode=self.mode)
        self.credit_cells = [c for c in self.cells if c.cost_type in _CREDIT_TYPES]
        self.lines: list[tuple[_Row, CostLine]] = []
        self.actions: list[tuple[str, CostLine]] = []
        for line in ctx.cost_lines:
            if line.channel == _COPILOT_CHANNEL and line.cost_type in pool.CELL_COST_TYPES:
                self.lines.append((self._row(line), line))
            elif line.channel == _ACTIONS_CHANNEL and line.cost_type == "actions":
                self.actions.append((self._entity(line.cost_center, line.workspace_id), line))
        self.pools: dict[tuple[str, str], list[PoolMonth]] = defaultdict(list)
        for pm in ctx.pools:
            self.pools[(pm.entity_id, pm.month)].append(pm)
        self.activity = tuple(a for a in ctx.activity if isinstance(a, ActivityDay))
        self._reach: dict[str | None, Reach] = {}
        self._rates: dict[tuple[str, ...], tuple[UnitRates, bool] | None] = {}

    # --- context ---------------------------------------------------------------------------

    @staticmethod
    def _today(ctx: AnalysisContext) -> _dt.date | None:
        if ctx.now_ms > 0:
            return _date_of_ms(ctx.now_ms)
        end = ctx.window[1] if ctx.window else 0
        return _date_of_ms(end - 1) if end > 0 else None

    @staticmethod
    def _convention(decisions: Iterable[tuple[str, str]]) -> tuple[str, str]:
        values = {v for k, v in decisions if k.startswith("convention:")}
        if not values:
            return "excl", "default"
        if values == {"incl"}:
            return "incl", "decided"
        if values == {"excl"}:
            return "excl", "decided"
        return "excl", "uncertain"

    def _entity(self, cost_center: str | None, org: str | None) -> str:
        return pool.entity_of(cost_center, org, capped=self.capped, entity_mode=self.mode)

    def _row(self, line: CostLine) -> _Row:
        return _Row(self._entity(line.cost_center, line.workspace_id), line.team or None,
                    line.cost_center or None, line.workspace_id or None, line.sku or "",
                    line.cost_type or "other", line.model or "", line.routing or "unknown",
                    line.speed or "standard", line.pseudo or None, line.workload, line.date_utc)

    def reach(self, team: str | None) -> Reach:
        if team not in self._reach:
            self._reach[team] = team_reach(self.activity, self.config, team)
        return self._reach[team]

    # --- people, dates ---------------------------------------------------------------------

    def users(self, entity: str, pred: Callable[[_Row], bool], *,
              unit: _Unit | None = None) -> int:
        """Distinct principals of the AI-credit cost lines of *entity* matching *pred* (and
        *unit*): counted, never exported."""
        return len({line.principal for row, line in self.lines
                    if line.principal is not None and row.entity_id == entity
                    and (unit is None or _unit_of(row) == unit) and pred(row)})

    def month_closed(self, entity: str, month: str) -> bool:
        pms = self.pools.get((entity, month))
        if pms:
            return all(pm.finality == "closed" for pm in pms)
        if self.today is None:
            return False
        return _last_day(month) <= self.today - _dt.timedelta(days=self.lag)

    # --- pools -----------------------------------------------------------------------------

    def scenarios(self, entity: str, months: Iterable[str]) -> tuple[str | None, ...]:
        for month in months:
            if any(pm.plan_scenario is not None for pm in self.pools.get((entity, month), ())):
                return ("business", "enterprise")
        return (None,)

    def pool_month(self, entity: str, month: str, scenario: str | None) -> PoolMonth | None:
        pms = self.pools.get((entity, month), [])
        for pm in pms:
            if pm.plan_scenario == scenario:
                return pm
        for pm in pms:
            if pm.plan_scenario is None:
                return pm
        return None

    # --- pricing ---------------------------------------------------------------------------

    def rates(self, model: str, *, date_utc: str, speed: str, routing: str,
              cost_type: str) -> tuple[UnitRates, bool] | None:
        """``(unit rates, writes priced)`` of *model* on *date_utc*: the pricer's default-tier
        rates (``Pricer.resolve`` / ``unit_rates``, modifiers applied: Auto, compliance from the
        run flags, fast mode) on the cost type's Copilot billing path; None when unpriced.
        Report cells aggregate many requests, so per-request long-context bands never apply."""
        path = "copilot_direct" if cost_type == _DIRECT else "copilot_pool"
        key = (model, date_utc, speed, routing, path)
        if key not in self._rates:
            result: tuple[UnitRates, bool] | None = None
            try:
                ctx = PricingContext(provider="github", channel=_COPILOT_CHANNEL, model=model,
                                     model_raw=model, speed=speed, routing=routing,
                                     compliance=self.compliance, billing_path=path)
                ts = _day_ms(date_utc)
                resolved = self.ctx.pricer.resolve(ctx, ts_ms=ts)
                unit = self.ctx.pricer.unit_rates(ctx, ts_ms=ts)
                if resolved is not None and unit is not None:
                    result = (unit, resolved.cache_write_5m is not None)
            except (PricingError, UsageError, ValueError):
                result = None
            self._rates[key] = result
        return self._rates[key]

    def price(self, usage: UsageBuckets, model: str, *, date_utc: str, speed: str, routing: str,
              cost_type: str) -> tuple[_Triple, bool] | None:
        """``([low, point, high], exact)`` nano of *usage* at :meth:`rates` (rounded once per
        bucket, like a priced line): unknown-TTL writes span the 5m (low, point) and 1h (high)
        write rates; writes on a row without a write price are folded into input by the pricer
        (not exact). None when unpriced."""
        got = self.rates(model, date_utc=date_utc, speed=speed, routing=routing,
                         cost_type=cost_type)
        if got is None:
            return None
        unit, writes_priced = got
        base = sum(unit.bucket_nano(b, getattr(usage, b)) for b in _PRICED_BUCKETS)
        unknown = usage.cache_write_unknown
        lo = base + unit.bucket_nano("cache_write_5m", unknown)
        hi = base + unit.bucket_nano("cache_write_1h", unknown)
        exact_ = lo == hi and (writes_priced or usage.cache_write == 0)
        return [lo, lo, hi], exact_

    # --- money -----------------------------------------------------------------------------

    def dollars(self, entity: str, lines: Sequence[CostLine]) -> Figure:
        """Σ net of invoice-side *lines* with the R16 label: INVOICE for final rows of a closed
        month on a reconciled channel, else EXACT LIST ("unreconciled" / "provisional")."""
        invoice = listed = 0
        n_inv = n_list = 0
        reasons: set[str] = set()
        final = True
        for line in lines:
            final = final and line.finality == "final"
            ok = True
            if line.channel not in self.ctx.reconciled_channels:
                reasons.add("unreconciled")
                ok = False
            if line.finality != "final" or not self.month_closed(entity, line.date_utc[:7]):
                reasons.add("provisional")
                ok = False
            if ok:
                invoice += line.amount_nano
                n_inv += 1
            else:
                listed += line.amount_nano
                n_list += 1
        fin = Finality.FINAL if final else Finality.PROVISIONAL
        inv_fig = Figure(nano=invoice, evidence=Evidence.EXACT, basis=Basis.INVOICE,
                         finality=Finality.FINAL,
                         note="invoice: final rows, closed month, reconciled channel")
        why = " and ".join(sorted(reasons))
        list_fig = Figure(nano=listed, evidence=Evidence.EXACT, basis=Basis.LIST, finality=fin,
                          note=f"list ({why}): invoice-side amounts, not yet invoice-grade")
        if n_list == 0:
            return inv_fig
        if n_inv == 0:
            return list_fig
        return combine_weakest([inv_fig, list_fig], note="R16: weakest label of the rows")

    def realize(self, entity: str, savings: _Savings, scenario: str | None,
                note: str) -> tuple[Figure, Figure]:
        """(invoice, headroom) of *savings* over its months (R11): pooled credits through
        ``core.pool.realize_credit_saving``; direct-org credits are invoice dollars unless the
        rows show they draw the pool."""
        if savings.unpriced is not None:
            reason = f"unpriced: {savings.unpriced}"
            return unpriced(reason, Basis.LIST), unpriced(reason, Basis.LIST_EQUIVALENT)
        invoice: Figure | None = None
        headroom: Figure | None = None
        for month in savings.months():
            pm = self.pool_month(entity, month, scenario)
            pooled = list(savings.pooled.get(month, [0, 0, 0]))
            direct = list(savings.direct.get(month, [0, 0, 0]))
            draws = pm.direct_draws_pool if pm is not None else "unknown"
            if draws == "yes":
                _add_tri(pooled, direct)
                direct = [0, 0, 0]
            parts: list[tuple[Figure, Figure]] = []
            if any(pooled):
                if pm is None:
                    reason = f"unpriced: no pool month for {entity} {month}"
                    parts.append((unpriced(reason, Basis.LIST),
                                  unpriced(reason, Basis.LIST_EQUIVALENT)))
                else:
                    parts.append(pool.realize_credit_saving(
                        _estimated(pooled, Basis.LIST_EQUIVALENT, note, upper=savings.upper),
                        pm))
            if any(direct):
                unknown = draws == "unknown"
                d_note = ("direct-org credits are metered to the organization: invoice dollars"
                          + ("; whether they draw the pool is unknown (upper bound)"
                             if unknown else ""))
                parts.append((_estimated(direct, Basis.LIST, d_note,
                                         upper=unknown or savings.upper),
                              _estimated([0, 0, 0], Basis.LIST_EQUIVALENT,
                                         "no pool headroom for direct-org credits")))
            for inv, head in parts:
                invoice = inv if invoice is None else add(invoice, inv)
                headroom = head if headroom is None else add(headroom, head)
        if invoice is None or headroom is None:
            none = "no saving in any pool month"
            return (_estimated([0, 0, 0], Basis.LIST, none),
                    _estimated([0, 0, 0], Basis.LIST_EQUIVALENT, none))
        return invoice, headroom


# ---------------------------------------------------------------------------------------------
# finding assembly
# ---------------------------------------------------------------------------------------------


@dataclass
class _Spec:
    kind: str
    category: str
    title: str
    summary: str
    cost: Figure
    entity: str | None = None
    team: str | None = None
    cost_center: str | None = None
    model: str | None = None
    scenario: str | None = None
    recoverable: Figure | None = None
    headroom: Figure | None = None
    projected: Figure | None = None
    lever_ids: tuple[str, ...] = ()
    n_events: int = 0
    n_users: int = 0
    first_seen_ms: int = 0
    evidence: tuple[EvidenceItem, ...] = ()
    fix: Fix | None = None
    confidence: str = "medium"
    needs_eval: bool = False


def _scope_dims(spec: _Spec) -> dict[str, str | None]:
    """``product: copilot`` plus entity, team (or cost center), model and plan scenario (None
    values dropped: unattributed usage has neither a team nor a cost-center dim)."""
    return {"product": "copilot", "entity": spec.entity, "team": spec.team,
            "cost_center": spec.cost_center, "model": spec.model,
            "plan_scenario": spec.scenario}


def _build(spec: _Spec) -> Finding:
    title = spec.title
    summary = spec.summary
    if spec.scenario is not None:
        title = _SCENARIO_TITLE[spec.scenario] + title
        summary = f"{summary} Pool figures {_SCENARIO_TEXT[spec.scenario]} (plan unknown)."
    return build_finding(
        detector_id=DETECTOR_ID, kind=spec.kind, detector_version=_VERSION,
        category=spec.category, lever_class=_lever_class(spec.lever_ids), audience="org",
        title=_cut(title, MAX_TITLE), summary=_cut(summary, MAX_SUMMARY),
        scope=make_scope(**_scope_dims(spec)), n_events=spec.n_events, n_lanes=0,
        n_users=spec.n_users, first_seen_ms=spec.first_seen_ms, cost_observed=spec.cost,
        recoverable=spec.recoverable, projected_monthly=spec.projected,
        lever_ids=spec.lever_ids, evidence=top_evidence(spec.evidence), fix=spec.fix,
        confidence=spec.confidence, needs_eval=spec.needs_eval, references=_REFS[spec.kind],
        headroom=spec.headroom)


def _first_seen(dates: Iterable[str | None]) -> int:
    known = [d for d in dates if d]
    return _day_ms(min(known)) if known else 0


def _gated(inp: _In, f: Finding, *extra_nano: int | None) -> bool:
    """``min_usd_gate`` or one of the kind's other dollar / credit amounts at ``min_usd``."""
    return min_usd_gate(f, inp.ctx) or any(n is not None and n >= inp.min_usd
                                            for n in extra_nano)


_Unit = tuple[str, "str | None", "str | None"]   # (entity, team, cost center if no team)


def _unit_of(c: pool.Cell | _Row) -> _Unit:
    return (c.entity_id, c.team, c.cost_center if c.team is None else None)


def _team_groups(cells: Iterable[pool.Cell]) -> list[tuple[_Unit, list[pool.Cell]]]:
    """Cells per unit — (entity, team), or (entity, cost center) for rows without a team —
    sorted (unattributed first)."""
    groups: dict[_Unit, list[pool.Cell]] = defaultdict(list)
    for c in cells:
        groups[_unit_of(c)].append(c)
    return sorted(groups.items(), key=lambda kv: tuple(v or "" for v in kv[0]))


def _who(team: str | None, cost_center: str | None) -> str:
    """The unit for generated text: ``team <t>``, ``cost center <c>`` or ``unattributed``."""
    if team:
        return f"team {_label(team)}"
    return f"cost center {_label(cost_center)}" if cost_center else "unattributed usage"


def _entity_groups(cells: Iterable[pool.Cell]) -> dict[str, list[pool.Cell]]:
    groups: dict[str, list[pool.Cell]] = defaultdict(list)
    for c in cells:
        groups[c.entity_id].append(c)
    return dict(sorted(groups.items()))


def _gross(cells: Iterable[pool.Cell]) -> int:
    return sum(c.gross_nano for c in cells)


def _has_tokens(u: UsageBuckets) -> bool:
    return u.total_input + u.output > 0


def _pooled_findings(inp: _In, spec: _Spec, savings: _Savings, note: str,
                     *extra_gate: int | None) -> list[Finding]:
    """One finding per scenario of *spec*'s entity (R-E22) with the realized saving."""
    out: list[Finding] = []
    assert spec.entity is not None
    for scenario in inp.scenarios(spec.entity, savings.months()):
        invoice, headroom = inp.realize(spec.entity, savings, scenario, note)
        spec.scenario, spec.recoverable, spec.headroom = scenario, invoice, headroom
        f = _build(spec)
        if _gated(inp, f, *extra_gate):
            out.append(f)
    return out


def _convention_note(inp: _In) -> str:
    if inp.convention_state == "uncertain":
        return "report token convention undecided (excl assumed): token figures ESTIMATED"
    if inp.convention_state == "default":
        return "report token convention excl (default, not reconciled)"
    return f"report token convention {inp.convention} (reconciled)"


# ---------------------------------------------------------------------------------------------
# premium-model-share, fast-mode, auto-adoption, cache-health (team, pool-converted)
# ---------------------------------------------------------------------------------------------


def _is_premium(c: pool.Cell | _Row) -> bool:
    return (c.cost_type in _CREDIT_TYPES and c.pseudo is None and bool(c.model)
            and catalog.copilot_category(c.model) == "Powerful"
            and catalog.copilot_remap(c.model) is not None)


def _remap_saving(inp: _In, c: pool.Cell) -> tuple[_Triple | None, str | None]:
    """Price-only saving of *c* at its remap target (§9.2 step 1): tokens × band point 1.00
    (``TOKENIZER_BAND`` when the tokenizers differ) at the target's rates on the cell's date."""
    remap = catalog.copilot_remap(c.model)
    assert remap is not None and c.date_utc is not None
    target, same = remap
    if not _has_tokens(c.usage):
        return None, "report rows without token columns"
    here = inp.price(c.usage, c.model, date_utc=c.date_utc, speed="standard",
                     routing=c.routing, cost_type=c.cost_type)
    there = inp.price(c.usage, target, date_utc=c.date_utc, speed="standard",
                      routing=c.routing, cost_type=c.cost_type)
    if here is None or there is None:
        return None, f"{c.model} or {target} not priced on {c.date_utc}"
    m, t = here[0], there[0]
    bands = (Fraction(1),) if same else _BAND
    candidates = [m[i] - _round(t[i] * b) for i in (0, 2) for b in bands]
    point = m[1] - t[1]
    return [min(candidates + [point]), point, max(candidates + [point])], None


def _premium_model_share(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    unit_totals = {k: _gross(v) for k, v in _team_groups(inp.credit_cells)}
    for unit, cells in _team_groups(c for c in inp.credit_cells if _is_premium(c)):
        entity, team, cc = unit
        credits = _gross(cells)
        if credits <= 0:
            continue
        savings = _Savings()
        per_model: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for c in cells:
            tri, why = _remap_saving(inp, c)
            per_model[c.model][0] += c.gross_nano
            if tri is None:
                savings.unpriced = savings.unpriced or why
                continue
            savings.add(c.month, c.cost_type, tri)
            per_model[c.model][1] += tri[1]
        if savings.unpriced is None and savings.total()[1] <= 0:
            continue
        share = Fraction(credits, unit_totals[unit] or credits)
        items = tuple(_item(f"model:{m}", credits_nano=v[0], saving_nano=v[1],
                            target=catalog.copilot_remap(m)[0],  # type: ignore[index]
                            magnitude=v[0]) for m, v in sorted(per_model.items()))
        items += (_item("team:share", premium_share=_dec(share), convention=inp.convention,
                        magnitude=0),)
        models = ", ".join(_label(m, "model") for m in sorted(per_model))
        note = (f"price-only remap to the same-vendor model-policy target on identical tokens; "
                f"{_convention_note(inp)}")
        spec = _Spec(
            kind="premium-model-share", category="aggregate", entity=entity, team=team,
            cost_center=cc,
            title=f"Premium models carry {_pct(share)} of {_who(team, cc)} Copilot credits",
            summary=(f"{_credits(credits)} AI credits ({fmt_usd(credits)} list-equivalent) of "
                     f"{_who(team, cc)} ran on Powerful models with a cheaper same-vendor "
                     f"option ({models}). Price-only estimate: quality unvalidated, evaluate "
                     f"before a model policy; the invoice part follows the pool rule, the rest "
                     f"is pool headroom (list-equivalent, not invoice dollars)."),
            cost=exact(credits, Basis.LIST_EQUIVALENT), lever_ids=("copilot.model_policy",),
            n_events=len(cells), n_users=inp.users(entity, _is_premium, unit=unit),
            first_seen_ms=_first_seen(c.date_utc for c in cells), evidence=items,
            confidence="medium", needs_eval=True)
        out.extend(_pooled_findings(inp, spec, savings, note))
    return out


def _is_fast(c: pool.Cell | _Row) -> bool:
    return c.cost_type in _CREDIT_TYPES and c.speed == "fast" and bool(c.model)


def _fast_mode(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    uncertain = inp.convention_state == "uncertain"
    for unit, cells in _team_groups(c for c in inp.credit_cells if _is_fast(c)):
        entity, team, cc = unit
        savings = _Savings()
        premium = [0, 0, 0]
        all_exact = True
        per_model: dict[str, int] = defaultdict(int)
        for c in cells:
            assert c.date_utc is not None
            if not _has_tokens(c.usage):
                savings.unpriced = savings.unpriced or "report rows without token columns"
                continue
            fast = inp.price(c.usage, c.model, date_utc=c.date_utc, speed="fast",
                             routing=c.routing, cost_type=c.cost_type)
            std = inp.price(c.usage, c.model, date_utc=c.date_utc, speed="standard",
                            routing=c.routing, cost_type=c.cost_type)
            if fast is None or std is None:
                savings.unpriced = savings.unpriced or f"{c.model} not priced on {c.date_utc}"
                continue
            tri = [fast[0][i] - std[0][i] for i in range(3)]
            all_exact = all_exact and fast[1] and std[1]
            _add_tri(premium, tri)
            savings.add(c.month, c.cost_type, tri)
            per_model[c.model] += tri[1]
        if savings.unpriced is not None:
            cost = unpriced(f"unpriced: {savings.unpriced}", Basis.LIST_EQUIVALENT)
        elif premium[1] <= 0:
            continue
        else:
            cost = _estimated(premium, Basis.LIST_EQUIVALENT,
                              "fast premium vs standard rates on identical tokens"
                              + ("; " + _convention_note(inp) if uncertain else ""),
                              evidence_exact=all_exact and not uncertain)
        items = tuple(_item(f"model:{m}", premium_nano=v, magnitude=v)
                      for m, v in sorted(per_model.items()))
        spec = _Spec(
            kind="fast-mode", category="aggregate", entity=entity, team=team,
            cost_center=cc, title=f"Fast mode premium in {_who(team, cc)} Copilot usage",
            summary=(f"Fast-mode requests of {_who(team, cc)} cost {fmt_usd(cost.nano)} "
                     f"list-equivalent more than the same tokens at standard speed (pure rate "
                     f"arithmetic). Disabling the fast-mode model saves the invoice part per the "
                     f"pool rule; the rest is pool headroom (list-equivalent, not invoice "
                     f"dollars)."),
            cost=cost, lever_ids=("copilot.fast_mode_off",), n_events=len(cells),
            n_users=inp.users(entity, _is_fast, unit=unit),
            first_seen_ms=_first_seen(c.date_utc for c in cells), evidence=items,
            confidence="high" if all_exact else "medium")
        out.extend(_pooled_findings(inp, spec, savings,
                                    "fast mode off: the premium on identical tokens"))
    return out


def _is_auto_eligible(c: pool.Cell | _Row) -> bool:
    return (c.cost_type in _CREDIT_TYPES and c.routing == "direct"
            and c.pseudo != "code_review" and c.workload != "copilot_code_review")


def _is_cloud_agent(c: pool.Cell | _Row) -> bool:
    return (c.pseudo == "cloud_agent" or c.workload == "copilot_cloud_agent"
            or c.sku == "coding_agent_ai_credit")


def _jetbrains_heavy(inp: _In, shares: Sequence[tuple[str, Fraction]]) -> bool:
    """The team's JetBrains share reaches ``jetbrains_policy_share`` (default 0.5)."""
    limit = Fraction(_threshold(inp, "jetbrains_policy_share", "0.5", 1))
    return bool(shares) and dict(shares).get("jetbrains", Fraction(0)) >= limit


def _policy_fix(reach: Reach) -> Fix:
    base = catalog.fix_for(DETECTOR_ID, "premium-model-share", "copilot")
    doc = base.doc_url if base is not None else None
    return Fix(text=(f"JetBrains carries {_pct(reach.jetbrains_share)} of this team's "
                     "interactions and the managed model key does not reach JetBrains: set the "
                     "server-side model policy (every editor, reach 1) instead of relying on "
                     "the managed key, and communicate the Auto tiers (Efficiency for routine "
                     "work) to JetBrains users."),
               config_patch=None, target="github-copilot", doc_url=doc)


def _auto_adoption(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    for unit, cells in _team_groups(c for c in inp.credit_cells if _is_auto_eligible(c)):
        entity, team, cc = unit
        credits = _gross(cells)
        if credits <= 0:
            continue
        reach = inp.reach(team)
        savings = _Savings()
        agent = 0
        for c in cells:
            if _is_cloud_agent(c):
                r_lo = r_pt = r_hi = Fraction(1)
                agent += c.gross_nano
            else:
                r_lo, r_pt, r_hi = reach.low, reach.point, reach.high
            base = c.gross_nano * _AUTO_DISCOUNT
            savings.add(c.month, c.cost_type,
                        [_round(base * r_lo), _round(base * r_pt), _round(base * r_hi)])
        if savings.total()[2] <= 0:
            continue
        heavy = _jetbrains_heavy(inp, reach.shares)
        reach_text = (f"reach {_dec(reach.point)} ({_pct(reach.jetbrains_share)} JetBrains)"
                      if reach.known else "reach unknown: no usage metrics, range to full")
        items = (_item("reach", reach=_dec(reach.point) if reach.known else "unknown",
                       reach_low=_dec(reach.low), reach_high=_dec(reach.high),
                       reach_source=reach.source, jetbrains_share=_dec(reach.jetbrains_share),
                       unreached_share=_dec(reach.unreached_share),
                       interactions=reach.interactions, magnitude=1),
                 _item("credits:eligible", credits_nano=credits, cloud_agent_nano=agent,
                       magnitude=credits))
        levers = (("copilot.model_policy", "copilot.auto_tier", "copilot.default_model_auto")
                  if heavy else ("copilot.default_model_auto", "copilot.auto_tier"))
        spec = _Spec(
            kind="auto-adoption", category="aggregate", entity=entity, team=team,
            cost_center=cc,
            title=f"Auto model selection unused on {_who(team, cc)} Copilot credits",
            summary=(f"{_credits(credits)} AI credits of {_who(team, cc)} were directly "
                     f"routed; Auto is billed 10% lower on paid plans. Estimate: 10% x credits x "
                     f"{reach_text}; which model Auto picks is not modeled (evaluate). Invoice "
                     f"part per the pool rule; the rest is pool headroom (list-equivalent, not "
                     f"invoice dollars)."),
            cost=exact(credits, Basis.LIST_EQUIVALENT), lever_ids=levers,
            n_events=len(cells), n_users=inp.users(entity, _is_auto_eligible, unit=unit),
            first_seen_ms=_first_seen(c.date_utc for c in cells), evidence=items,
            fix=_policy_fix(reach) if heavy else None,
            confidence="medium" if reach.known else "low", needs_eval=True)
        out.extend(_pooled_findings(
            inp, spec, savings,
            f"Auto discount 10% x eligible credits x managed-model reach ({reach.source})",
            savings.total()[2]))
    return out


def _cache_health(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    """Per team × model read share ``R / (U + R + W)`` below the org's median for the model."""
    groups: dict[tuple[_Unit, str], list[pool.Cell]] = defaultdict(list)
    for c in inp.credit_cells:
        if c.pseudo is None and c.model and c.usage.total_input > 0:
            groups[(_unit_of(c), c.model)].append(c)
    shares: dict[tuple[_Unit, str], tuple[int, int]] = {}
    by_model: dict[str, list[Fraction]] = defaultdict(list)
    for key, cells in groups.items():
        total = sum(c.usage.total_input for c in cells)
        read = sum(c.usage.cache_read for c in cells)
        shares[key] = (read, total)
        by_model[key[1]].append(Fraction(read, total))
    out: list[Finding] = []
    for key in sorted(groups, key=lambda k: (*(v or "" for v in k[0]), k[1])):
        unit, model = key
        entity, team, cc = unit
        read, total = shares[key]
        median = _median(by_model[model])
        own = Fraction(read, total)
        if len(by_model[model]) < 2 or own >= median:
            continue
        cells = groups[key]
        savings = _Savings(upper=True)
        for c in cells:
            assert c.date_utc is not None
            extra = _round(median * c.usage.total_input) - c.usage.cache_read
            if extra <= 0:
                continue
            got = inp.rates(model, date_utc=c.date_utc, speed=c.speed, routing=c.routing,
                            cost_type=c.cost_type)
            if got is None:
                savings.unpriced = savings.unpriced or f"{model} not priced on {c.date_utc}"
                continue
            prem = (got[0].bucket_nano("cache_write_5m", extra)
                    - got[0].bucket_nano("cache_read", extra))
            savings.add(c.month, c.cost_type, [0, max(0, prem), max(0, prem)])
        if savings.unpriced is None and savings.total()[1] <= 0:
            continue
        spend = _gross(cells)
        items = (_item(f"model:{model}", read_share=_dec(own), org_median=_dec(median),
                       input_tokens=total, read_tokens=read, magnitude=spend),)
        spec = _Spec(
            kind="cache-health", category="aggregate", entity=entity, team=team,
            cost_center=cc, model=model,
            title=f"Low cache-read share for {_label(model, 'model')} in {_who(team, cc)}",
            summary=(f"{_who(team, cc).capitalize()} read {_pct(own)} of its "
                     f"{_label(model, 'model')} input from cache vs the organization's "
                     f"median {_pct(median)}. Upper "
                     f"bound (median share x input - reads) x (write - read rate), estimated; "
                     f"install collectors or OpenTelemetry for causes. Invoice part per the pool "
                     f"rule; the rest is pool headroom (list-equivalent, not invoice dollars)."),
            cost=exact(spend, Basis.LIST_EQUIVALENT),
            lever_ids=("copilot.telemetry_on", "copilot.vscode_traces_optin"),
            n_events=len(cells),
            n_users=inp.users(entity, lambda r, m=model: r.model == m and r.pseudo is None,
                              unit=unit),
            first_seen_ms=_first_seen(c.date_utc for c in cells), evidence=items)
        out.extend(_pooled_findings(inp, spec, savings,
                                    "cache-health upper bound to the org median read share"))
    return out


# ---------------------------------------------------------------------------------------------
# forced-migration, compliance-uplift (entity info)
# ---------------------------------------------------------------------------------------------


def _forced_migration(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    if inp.today is None:
        skipped["forced-migration"] = "no reference date (now_ms / window)"
        return []
    today = inp.today.isoformat()
    groups: dict[tuple[str, str], list[pool.Cell]] = defaultdict(list)
    for c in inp.credit_cells:
        info = catalog.COPILOT_RETIREMENTS.get(c.model) if c.model else None
        if info is not None and info[1] is not None and today <= info[0]:
            groups[(c.entity_id, c.model)].append(c)
    all_days = sorted({c.date_utc for c in inp.credit_cells if c.date_utc})
    span = ((_dt.date.fromisoformat(all_days[-1]) - _dt.date.fromisoformat(all_days[0])).days
            + 1) if all_days else 1
    out: list[Finding] = []
    for (entity, model), cells in sorted(groups.items()):
        retire_on, successor = catalog.COPILOT_RETIREMENTS[model]
        assert successor is not None
        remap = catalog.copilot_remap(successor)
        alt = remap[0] if remap is not None else model
        delta = [0, 0, 0]
        vs_current = 0
        all_exact = True
        why = None
        for c in cells:
            if not _has_tokens(c.usage):
                why = "report rows without token columns"
                break
            succ = inp.price(c.usage, successor, date_utc=today, speed="standard",
                             routing=c.routing, cost_type=c.cost_type)
            other = inp.price(c.usage, alt, date_utc=today, speed="standard",
                              routing=c.routing, cost_type=c.cost_type)
            if succ is None or other is None:
                why = f"{successor} or {alt} not priced on {today}"
                break
            _add_tri(delta, [succ[0][i] - other[0][i] for i in range(3)])
            all_exact = all_exact and succ[1] and other[1]
            if alt != model:
                cur = inp.price(c.usage, model, date_utc=today, speed="standard",
                                routing=c.routing, cost_type=c.cost_type)
                vs_current += succ[0][1] - cur[0][1] if cur is not None else 0
        credits = _gross(cells)
        if why is not None:
            cost = unpriced(f"unpriced: {why}", Basis.LIST_EQUIVALENT)
            projected = None
        else:
            if delta[1] <= 0:
                continue
            cost = _estimated(delta, Basis.LIST_EQUIVALENT,
                              f"rate arithmetic on identical tokens at {today} rates",
                              evidence_exact=all_exact)
            proj = [_round(Fraction(v * 30, span)) for v in delta]
            projected = _estimated(proj, Basis.LIST_EQUIVALENT,
                                   f"x 30 / {span} observed days at unchanged use")
        compare = (f"than the cheaper same-vendor {_label(alt, 'model')}"
                   if alt != model else f"than {_label(model, 'model')} today")
        items = (_item(f"model:{model}", retire_on=retire_on, successor=successor,
                       alternative=alt, credits_nano=credits, delta_nano=delta[1],
                       delta_vs_current_nano=vs_current if alt != model else None,
                       magnitude=credits),)
        f = _build(_Spec(
            kind="forced-migration", category="aggregate", entity=entity, model=model,
            title=f"{_label(model, 'model')} retires on {retire_on}: choose the successor",
            summary=(f"{_label(model, 'model')} retires on {retire_on}; GitHub suggests "
                     f"{_label(successor, 'model')}. On the same tokens it costs "
                     f"{fmt_usd(cost.nano)} more {compare} (list-equivalent, not invoice "
                     f"dollars; projection estimated at unchanged use). Choose deliberately "
                     f"before the date."),
            cost=cost, projected=projected, n_events=len(cells),
            n_users=inp.users(entity, lambda r, m=model: r.model == m),
            first_seen_ms=_first_seen(c.date_utc for c in cells), evidence=items,
            confidence="high" if all_exact else "medium"))
        if _gated(inp, f):
            out.append(f)
    return out


def _compliance_uplift(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    flag = inp.flags.get("compliance")
    if inp.compliance is None:
        return []
    out: list[Finding] = []
    for entity, cells in _entity_groups(inp.credit_cells).items():
        observed = _gross(cells)
        if observed <= 0:
            continue
        uplift = _round(Fraction(observed, _COMPLIANCE_DIVISOR))
        cost = Figure(nano=uplift, evidence=Evidence.ESTIMATED, basis=Basis.LIST_EQUIVALENT,
                      calibration=Calibration.UNCALIBRATED,
                      note=("uplift = observed credits x (1 - 1/1.1) = observed / 11; the policy "
                            "scope and stacking with Auto are assumed (VERIFY)"))
        items = (_item("compliance", policy=str(flag), observed_nano=observed,
                       uplift_nano=uplift, magnitude=uplift),)
        f = _build(_Spec(
            kind="compliance-uplift", category="aggregate", entity=entity,
            title=f"Compliance policy uplift: {_credits(uplift)} AI credits",
            summary=(f"A restrict-to-compliant-models policy ({sanitize(str(flag))}) adds 10% "
                     f"to AI credits: of {_credits(observed)} observed credits about "
                     f"{_credits(uplift)} ({fmt_usd(uplift)} list-equivalent, estimated) are "
                     f"the uplift. Not a recommendation to disable the policy; prefer cheaper "
                     f"compliant models."),
            cost=cost, n_events=len(cells), n_users=inp.users(entity, lambda r: True),
            first_seen_ms=_first_seen(c.date_utc for c in cells), evidence=items))
        if _gated(inp, f):
            out.append(f)
    return out


# ---------------------------------------------------------------------------------------------
# code review (entity info)
# ---------------------------------------------------------------------------------------------


def _is_review(c: pool.Cell | _Row) -> bool:
    return c.cost_type in _CREDIT_TYPES and (c.pseudo == "code_review"
                                             or c.workload == "copilot_code_review")


def _actions_of(inp: _In, entity: str, workload: str) -> list[CostLine]:
    return [line for e, line in inp.actions if e == entity and line.workload == workload]


def _minutes(lines: Iterable[CostLine]) -> Decimal | None:
    total = Decimal(0)
    for line in lines:
        if line.quantity is None:
            return None
        total = EXACT_CTX.add(total, Decimal(line.quantity))
    return total


def _dollar_text(fig: Figure) -> str:
    label = {Basis.INVOICE: "invoice"}.get(fig.basis, "list, not yet invoice")
    return f"{fmt_usd(fig.nano)} ({label})"


@dataclass
class _Spend:
    """A workload's AI-credit cells and Copilot-workload Actions lines of one entity: credits
    (LIST_EQUIVALENT) and Actions dollars (R16) are two figures, never added."""

    cells: list[pool.Cell]
    actions: list[CostLine]
    credits: int
    dollars: Figure | None

    @property
    def cost(self) -> Figure:
        """The credits, or the Actions dollars when there are no credits."""
        if self.credits > 0 or self.dollars is None:
            return exact(self.credits, Basis.LIST_EQUIVALENT)
        return self.dollars


def _spends(inp: _In, pred: Callable[[pool.Cell], bool], workload: str) -> dict[str, _Spend]:
    ents = {c.entity_id for c in inp.credit_cells if pred(c)}
    ents |= {e for e, line in inp.actions if line.workload == workload}
    out: dict[str, _Spend] = {}
    for entity in sorted(ents):
        cells = [c for c in inp.credit_cells if c.entity_id == entity and pred(c)]
        actions = _actions_of(inp, entity, workload)
        out[entity] = _Spend(cells, actions, _gross(cells),
                             inp.dollars(entity, actions) if actions else None)
    return out


def _review_cost(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    for entity, sp in _spends(inp, _is_review, "copilot_code_review").items():
        cells, credits, actions, dollars = sp.cells, sp.credits, sp.actions, sp.dollars
        minutes = _minutes(actions)
        act = (f" and {minutes.normalize():f} Actions minutes costing {_dollar_text(dollars)}"
               if dollars is not None and minutes is not None else
               f" and Actions costing {_dollar_text(dollars)}" if dollars is not None else "")
        items = (_item("review:credits", credits_nano=credits, rows=len(cells),
                       magnitude=credits),
                 _item("review:actions", actions_net_nano=dollars.nano if dollars else None,
                       actions_basis=dollars.basis.value if dollars else None,
                       actions_minutes=f"{minutes.normalize():f}" if minutes is not None
                       else None, magnitude=0))
        f = _build(_Spec(
            kind="review-cost", category="aggregate", entity=entity,
            title="Copilot code review cost: credits and Actions minutes",
            summary=(f"Copilot code review used {_credits(credits)} AI credits "
                     f"({fmt_usd(credits)} list-equivalent){act}. Credits and dollars are "
                     f"separate figures, never added. Review the effort, triggers, MCP tools "
                     f"and custom instructions."),
            cost=sp.cost, lever_ids=("copilot.review_effort_lite", "copilot.review_triggers"),
            n_events=len(cells) + len(actions), n_users=inp.users(entity, _is_review),
            first_seen_ms=_first_seen([c.date_utc for c in cells]
                                      + [line.date_utc for line in actions]),
            evidence=items, confidence="high"))
        if _gated(inp, f, credits, dollars.nano if dollars else None):
            out.append(f)
    return out


def _review_default_balanced(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    if inp.today is None:
        skipped["review-default-balanced"] = "no reference date (now_ms / window)"
        return []
    dates = _facts.copilot_dates()
    flip = _dt.date.fromisoformat(dates["review_default_balanced"])
    expires = flip + _dt.timedelta(days=30)
    if inp.today >= expires:
        return []
    days = int(_threshold(inp, "review_window_days", "30", 3650, low=1))
    since = (inp.today - _dt.timedelta(days=max(1, days))).isoformat()
    estimates = _facts.load().copilot.review_estimates
    lite, balanced = estimates["lite"], estimates["balanced"]
    source = balanced.source.removeprefix("https://")
    quote = (f"Published per review: ${lite.low_usd}-${lite.high_usd} Lite, "
             f"${balanced.low_usd}-${balanced.high_usd} Balanced ({source}, "
             f"{balanced.verified_on}); context only, never projected.")
    when = ("becomes Balanced on" if inp.today < flip else "has been Balanced since")
    out: list[Finding] = []
    for entity, all_cells in _entity_groups(c for c in inp.credit_cells if _is_review(c)).items():
        cells = [c for c in all_cells if c.date_utc is not None and c.date_utc >= since]
        credits = _gross(cells)
        if credits <= 0:
            continue
        items = (_item("review:last-days", days=days, credits_nano=credits,
                       flip=flip.isoformat(), expires=expires.isoformat(),
                       lite_usd=f"{lite.low_usd}-{lite.high_usd}",
                       balanced_usd=f"{balanced.low_usd}-{balanced.high_usd}",
                       source=balanced.source, checked=balanced.verified_on,
                       magnitude=credits),)
        f = _build(_Spec(
            kind="review-default-balanced", category="aggregate", entity=entity,
            title=f"Code review default effort {when} {flip.isoformat()}",
            summary=(f"Review credits, last {days} days: {_credits(credits)} "
                     f"(list-equivalent). Default effort {when} {flip.isoformat()} unless Lite "
                     f"is set; personal defaults still apply to reviews users request. {quote}"),
            cost=exact(credits, Basis.LIST_EQUIVALENT), lever_ids=("copilot.review_effort_lite",),
            n_events=len(cells), n_users=inp.users(entity, _is_review),
            first_seen_ms=_first_seen(c.date_utc for c in cells), evidence=items,
            confidence="medium", needs_eval=True))
        if _gated(inp, f):
            out.append(f)
    return out


def _review_drivers(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    for entity, cells in _entity_groups(c for c in inp.credit_cells if _is_review(c)).items():
        credits = _gross(cells)
        direct = sum(c.gross_nano for c in cells if c.cost_type == _DIRECT)
        items = (_item("review:credits", credits_nano=credits, direct_nano=direct,
                       rows=len(cells), magnitude=credits),)
        f = _build(_Spec(
            kind="review-drivers", category="aggregate", entity=entity,
            title="What drives Copilot code review consumption",
            summary=(f"Code review used {_credits(credits)} AI credits (list-equivalent). "
                     f"Drivers: the repository setting 'Allow Copilot to use MCP tools when "
                     f"reviewing pull requests' is on by default (GitHub and Playwright MCP "
                     f"servers); consumption grows with pull request size and repository custom "
                     f"instructions; personal automatic review of new pushes and drafts."),
            cost=exact(credits, Basis.LIST_EQUIVALENT),
            lever_ids=("copilot.review_triggers", "copilot.review_mcp_off",
                       "copilot.review_instructions_trim"),
            n_events=len(cells), n_users=inp.users(entity, _is_review),
            first_seen_ms=_first_seen(c.date_utc for c in cells), evidence=items))
        if _gated(inp, f):
            out.append(f)
    return out


# ---------------------------------------------------------------------------------------------
# direct-org usage, unattributed spend (entity)
# ---------------------------------------------------------------------------------------------


def _direct_org_usage(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    by_entity: dict[str, list[tuple[_Row, CostLine]]] = defaultdict(list)
    for row, line in inp.lines:
        if row.cost_type == _DIRECT:
            by_entity[row.entity_id].append((row, line))
    cells_by_entity = _entity_groups(inp.credit_cells)
    for entity, pairs in sorted(by_entity.items()):
        lines = [line for _, line in pairs]
        gross = sum(line.list_amount_nano if line.list_amount_nano is not None
                    else line.amount_nano for line in lines)
        net = inp.dollars(entity, lines)
        draw = gross - sum(line.amount_nano for line in lines)
        draws = pool.direct_draws_pool(cells_by_entity.get(entity, []))
        parts: dict[str, int] = defaultdict(int)
        for row, line in pairs:
            part = ("code_review" if _is_review(row) else "cloud_agent" if _is_cloud_agent(row)
                    else "agentic_workflow" if row.workload == "agentic_workflow" else "cli_other")
            parts[part] += line.amount_nano
        items = (_item("direct:pool", gross_nano=gross, pool_draw_nano=draw,
                       direct_draws_pool=draws, magnitude=gross),
                 *(_item(f"direct:{k}", net_nano=v, magnitude=v) for k, v in sorted(parts.items())))
        draw_text = {"yes": "they draw the pool", "no": "they do not draw the pool",
                     "unknown": "whether they draw the pool is unknown"}[draws]
        f = _build(_Spec(
            kind="direct-org-usage", category="aggregate", entity=entity,
            title="Copilot usage billed directly to the organization",
            summary=(f"AI credits without a licensed user (unlicensed or bot code review, "
                     f"GITHUB_TOKEN CLI, agentic workflows) are metered to the organization: "
                     f"net {_dollar_text(net)}; their discounts show a pool draw of "
                     f"{_credits(draw)} credits (list-equivalent; {draw_text})."),
            cost=net, lever_ids=("copilot.review_unlicensed_off", "copilot.session_limits"),
            n_events=len(lines), n_users=0, first_seen_ms=_first_seen(
                line.date_utc for line in lines), evidence=items, confidence="high"))
        if _gated(inp, f, gross):
            out.append(f)
    return out


def _unattributed_spend(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    by_entity: dict[str, list[CostLine]] = defaultdict(list)
    for row, line in inp.lines:
        by_entity[row.entity_id].append(line)
    for entity, lines in sorted(by_entity.items()):
        total = sum(line.amount_nano for line in lines)
        loose = [line for line in lines if line.principal is None or not line.cost_center]
        net = sum(line.amount_nano for line in loose)
        if net <= 0 or total <= 0:
            continue
        no_user = sum(line.amount_nano for line in lines if line.principal is None)
        no_cc = sum(line.amount_nano for line in lines if not line.cost_center)
        share = Fraction(net, total)
        dollars = inp.dollars(entity, loose)
        items = (_item("unattributed", share=_dec(share), no_username_nano=no_user,
                       no_cost_center_nano=no_cc, total_net_nano=total, magnitude=net),)
        f = _build(_Spec(
            kind="unattributed-spend", category="aggregate", entity=entity,
            title=f"{_pct(share)} of Copilot net spend has no user or cost center",
            summary=(f"{_dollar_text(dollars)} of {fmt_usd(total)} AI-credit "
                     f"net spend has no username or no cost center, so showback cannot assign "
                     f"it. Assign users, organizations and repositories to cost centers."),
            cost=dollars, n_events=len(loose),
            n_users=len({ln.principal for ln in loose if ln.principal}),
            first_seen_ms=_first_seen(line.date_utc for line in loose), evidence=items,
            confidence="high"))
        if _gated(inp, f):
            out.append(f)
    return out


# ---------------------------------------------------------------------------------------------
# Actions-based kinds: agentic workflows, larger runners, cloud agent
# ---------------------------------------------------------------------------------------------


def _run_price(inp: _In, agg: UsageAggregate) -> int | None:
    model = dict(agg.dims).get("model")
    if not model or not _has_tokens(agg.usage):
        return None
    date = _date_of_ms(agg.bucket_start_ms)
    if date is None:
        return None
    priced = inp.price(agg.usage, model, date_utc=date.isoformat(), speed="standard",
                       routing="direct", cost_type=_DIRECT)
    return priced[0][1] if priced is not None else None


def _agentic_workflow_cost(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    aw: dict[str, list[CostLine]] = defaultdict(list)
    for entity, line in inp.actions:
        if line.workload == "agentic_workflow":
            aw[entity].append(line)
    repo_entity: dict[str, str] = {}
    for entity, lines in sorted(aw.items()):
        for line in lines:
            if line.repo:
                repo_entity.setdefault(line.repo, entity)
    runs: dict[str, list[UsageAggregate]] = defaultdict(list)
    for agg in inp.ctx.aggregates:
        if agg.source_kind != _GH_AW_RUN:
            continue
        dims = dict(agg.dims)
        org = dims.get("organization")
        entity = repo_entity.get(dims.get("repo") or "") or (
            inp._entity(None, org) if org or inp.mode == "enterprise" else None)
        if entity is not None:
            runs[entity].append(agg)
    cap = _facts.load().copilot.aic_default_cap_per_run
    out: list[Finding] = []
    for entity in sorted(set(aw) | set(runs)):
        actions = aw.get(entity, [])
        repos = {line.repo for line in actions if line.repo}
        direct = [line for row, line in inp.lines
                  if row.cost_type == _DIRECT and row.entity_id == entity and line.repo in repos]
        credits = sum(line.list_amount_nano if line.list_amount_nano is not None
                      else line.amount_nano for line in direct)
        dollars = inp.dollars(entity, actions) if actions else None
        entity_runs = runs.get(entity, [])
        n_runs = len(entity_runs)
        exposure = n_runs * cap * _NANO_PER_CREDIT
        prices = [_run_price(inp, a) for a in entity_runs]
        priced = [p for p in prices if p is not None]
        tokens = [a.usage.total_input + a.usage.output for a in entity_runs]
        stats: dict[str, int | None] = {}
        if priced and len(priced) == len(prices):
            p99 = _rank(priced, Fraction(99, 100))
            stats = {"run_p50_nano": _rank(priced, Fraction(1, 2)),
                     "run_p90_nano": _rank(priced, Fraction(9, 10)), "run_p99_nano": p99,
                     "suggested_max_ai_credits": -(-(p99 * 3) // (2 * _NANO_PER_CREDIT))}
        if dollars is not None:
            cost = dollars
        elif credits > 0:
            cost = Figure(nano=credits, evidence=Evidence.ESTIMATED,
                          basis=Basis.LIST_EQUIVALENT, calibration=Calibration.UNCALIBRATED,
                          note="direct credits joined by repository (heuristic)")
        elif priced and len(priced) == len(prices):
            cost = Figure(nano=sum(priced), evidence=Evidence.ESTIMATED,
                          basis=Basis.LIST_EQUIVALENT, calibration=Calibration.UNCALIBRATED,
                          note="gh-aw run tokens priced at Copilot rates")
        else:
            cost = unpriced("unpriced: gh-aw runs without a model", Basis.LIST_EQUIVALENT)
        items = (
            _item("aw:actions", actions_net_nano=dollars.nano if dollars else None,
                  actions_basis=dollars.basis.value if dollars else None,
                  workflows=len({line.workflow for line in actions if line.workflow}),
                  magnitude=dollars.nano if dollars and dollars.nano else 0),
            _item("aw:credits", ai_credits_nano=credits, direct_rows=len(direct),
                  join="repository (heuristic, estimated)", magnitude=credits),
            _item("aw:runs", runs=n_runs, exposure_nano=exposure if n_runs else None,
                  cap_credits_per_run=cap, run_tokens_p50=_rank(tokens, Fraction(1, 2))
                  if tokens else None, run_tokens_p90=_rank(tokens, Fraction(9, 10))
                  if tokens else None, magnitude=exposure, **stats),
        )
        act = f"Actions {_dollar_text(dollars)}" if dollars is not None else "no Actions lines"
        exp = (f"; {n_runs} runs x {cap} AIC default cap = up to {fmt_usd(exposure)} exposure "
               f"(no cap known; estimated upper bound)" if n_runs else "")
        f = _build(_Spec(
            kind="agentic-workflow-cost", category="aggregate", entity=entity,
            title="Agentic workflow cost: Actions minutes and org-billed AI credits",
            summary=(f"Agentic workflows (.lock.yml): {act}; AI credits billed to the "
                     f"organization in the same repositories {_credits(credits)} "
                     f"(list-equivalent, joined by repository: estimated){exp}. Set "
                     f"max-ai-credits near p99 x 1.5."),
            cost=cost, lever_ids=("copilot.agentic_workflow_caps",),
            n_events=len(actions) + len(direct) + n_runs, n_users=0,
            first_seen_ms=_first_seen([line.date_utc for line in actions + direct]),
            evidence=items, confidence="low" if not actions else "medium"))
        if _gated(inp, f, credits, dollars.nano if dollars else None, exposure):
            out.append(f)
    return out


def _is_larger(line: CostLine) -> bool:
    rate = catalog.runner_rate(line.sku)
    return (rate is not None and rate.runner_class == "larger"
            and line.workload in COPILOT_WORKLOADS)


def _larger_runner(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    groups: dict[str, list[CostLine]] = defaultdict(list)
    for entity, line in inp.actions:
        if _is_larger(line):
            groups[entity].append(line)
    std = catalog.runner_rate(_STANDARD_RUNNER)
    assert std is not None
    out: list[Finding] = []
    for entity, lines in sorted(groups.items()):
        net = inp.dollars(entity, lines)
        minutes = _minutes(lines)
        assert net.nano is not None
        if minutes is None:
            rec: Figure = unpriced("unpriced: Actions lines without minutes", Basis.LIST)
        else:
            standard = decimal_to_nano(EXACT_CTX.multiply(minutes, std.usd_per_minute))
            low = max(0, net.nano - standard)
            high = max(low, net.nano)
            rec = _estimated([low, low, high], Basis.LIST,
                             f"standard runner {_STANDARD_RUNNER} at ${std.usd_per_minute}/min: "
                             "low = net - minutes x rate (no included minutes left), high = net "
                             "(standard minutes covered by included minutes)")
        sku_minutes: dict[str, Decimal] = defaultdict(Decimal)
        sku_net: dict[str, int] = defaultdict(int)
        for line in lines:
            sku = line.sku or ""
            sku_minutes[sku] = EXACT_CTX.add(sku_minutes[sku], Decimal(line.quantity or "0"))
            sku_net[sku] += line.amount_nano
        items = tuple(_item(f"sku:{sku}", minutes=f"{sku_minutes[sku].normalize():f}",
                            net_nano=sku_net[sku], magnitude=sku_net[sku])
                      for sku in sorted(sku_net))
        f = _build(_Spec(
            kind="larger-runner", category="aggregate", entity=entity,
            title="Copilot workloads on larger Actions runners",
            summary=(f"Copilot code review, cloud agent or agentic workflows ran on larger "
                     f"runners: {_dollar_text(net)}. Larger runners never use included minutes; "
                     f"standard runners would cost at most minutes x ${std.usd_per_minute} "
                     f"(estimated range)."),
            cost=net, recoverable=rec, lever_ids=("copilot.agent_runner_standard",),
            n_events=len(lines), n_users=0,
            first_seen_ms=_first_seen(line.date_utc for line in lines), evidence=items,
            confidence="medium"))
        if _gated(inp, f):
            out.append(f)
    return out


def _prs_merged(inp: _In, entity: str) -> int | None:
    team = "(enterprise)" if entity == "enterprise" else f"({entity})"
    total = None
    for o in inp.ctx.outcomes:
        if o.source_kind != _METRICS_OUTCOMES or o.team != team:
            continue
        n = dict(o.extra).get("prs_merged_created_by_copilot")
        if n is not None:
            total = (total or 0) + n
    return total


def _cloud_agent_cost(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    for entity, sp in _spends(inp, _is_cloud_agent, "copilot_cloud_agent").items():
        cells, credits, actions, dollars = sp.cells, sp.credits, sp.actions, sp.dollars
        prs = _prs_merged(inp, entity)
        per_pr = None
        if prs is None:
            skipped["cloud-agent-cost (PR ratio)"] = "no usage-metrics outcomes"
        elif prs > 0:
            per_pr = _round(Fraction(credits, prs))
        ratio = (f"; about {fmt_usd(per_pr)} of credits per merged Copilot-authored pull "
                 f"request (estimated: windows differ)" if per_pr is not None else "")
        act = f" and Actions {_dollar_text(dollars)}" if dollars is not None else ""
        items = (_item("agent:credits", credits_nano=credits, rows=len(cells),
                       magnitude=credits),
                 _item("agent:actions", actions_net_nano=dollars.nano if dollars else None,
                       actions_basis=dollars.basis.value if dollars else None, magnitude=0),
                 _item("agent:prs", prs_merged_created_by_copilot=prs,
                       credits_per_merged_pr_nano=per_pr, magnitude=0))
        f = _build(_Spec(
            kind="cloud-agent-cost", category="aggregate", entity=entity,
            title="Copilot cloud agent cost: credits and Actions minutes",
            summary=(f"The cloud agent used {_credits(credits)} AI credits (list-equivalent)"
                     f"{act}{ratio}. Credits and dollars are separate figures, never added."),
            cost=sp.cost, n_events=len(cells) + len(actions),
            n_users=inp.users(entity, _is_cloud_agent),
            first_seen_ms=_first_seen([c.date_utc for c in cells]
                                      + [line.date_utc for line in actions]),
            evidence=items))
        if _gated(inp, f, credits, dollars.nano if dollars else None):
            out.append(f)
    return out


def _agent_failed_sessions(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    groups: dict[str, list[UsageAggregate]] = defaultdict(list)
    totals: dict[str, int] = defaultdict(int)
    for agg in inp.ctx.aggregates:
        if agg.source_kind != _AGENT_TASKS:
            continue
        dims = dict(agg.dims)
        entity = inp._entity(None, dims.get("organization"))
        totals[entity] += 1
        used = (agg.reported_cost_nano or 0) > 0 or _has_tokens(agg.usage)
        if dims.get("state") in FAILED_STATES and used:
            groups[entity].append(agg)
    out: list[Finding] = []
    for entity, aggs in sorted(groups.items()):
        by_state = {s: sum(1 for a in aggs if dict(a.dims).get("state") == s)
                    for s in FAILED_STATES}
        estimate = sum(a.reported_cost_nano or 0 for a in aggs)
        items = (_item("agent-tasks", provider_estimate_nano=estimate,
                       sessions_total=totals[entity], magnitude=len(aggs),
                       **{f"sessions_{s}": n for s, n in by_state.items()}),)
        out.append(_build(_Spec(
            kind="agent-failed-sessions", category="aggregate", entity=entity,
            title=f"{len(aggs)} cloud agent sessions failed after consuming credits",
            summary=(f"{by_state['failed']} failed, {by_state['timed_out']} timed-out and "
                     f"{by_state['cancelled']} cancelled cloud agent sessions consumed credits. "
                     f"Only GitHub's task API estimate exists (a provider estimate in evidence, "
                     f"never a billed number); scope agent tasks smaller."),
            cost=unpriced("unpriced: provider estimate only", Basis.LIST_EQUIVALENT),
            n_events=len(aggs), n_users=0,
            first_seen_ms=min(a.bucket_start_ms for a in aggs), evidence=items,
            confidence="low")))
    return out


# ---------------------------------------------------------------------------------------------
# activity kinds: mcp-sprawl, context-heavy-cli, editor-mix
# ---------------------------------------------------------------------------------------------


def _activity_by_team(inp: _In) -> dict[str | None, dict[str, dict[str, int]]]:
    """team → principal → Σ counts over the window (people counted, never exported)."""
    out: dict[str | None, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for day in inp.activity:
        user = out[day.team or None][day.principal]
        for key, value in day.counts:
            if key == "mcp_distinct":
                user[key] = max(user.get(key, 0), value)
            else:
                user[key] = user.get(key, 0) + value
    return out


def _count_only() -> Figure:
    return unpriced("unpriced: count-only finding (activity metrics carry no dollars)",
                    Basis.LIST_EQUIVALENT)


def _team_entity(inp: _In, team: str | None) -> str | None:
    """The pool entity of *team*'s activity findings: ``enterprise`` in enterprise mode, else the
    one entity of the team's report rows (None when there are several or none)."""
    if inp.mode == "enterprise":
        return "enterprise"
    ents = {c.entity_id for c in inp.credit_cells if c.team == team}
    return ents.pop() if len(ents) == 1 else None


def _mcp_sprawl(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    if not inp.activity:
        skipped["mcp-sprawl"] = "no per-user activity (aggregate-only bundle or no metrics)"
        return []
    heavy = _threshold(inp, "mcp_heavy_distinct", "5", 10**6)
    share_min = Fraction(_threshold(inp, "mcp_heavy_share", "0.25", 1))
    out: list[Finding] = []
    for team, users in sorted(_activity_by_team(inp).items(), key=lambda kv: kv[0] or ""):
        values = [u.get("mcp_distinct", 0) for u in users.values()]
        if not values or not any(values):
            continue
        median = _rank(values, Fraction(1, 2))
        n_heavy = sum(1 for v in values if v >= heavy)
        share = Fraction(n_heavy, len(values))
        if share < share_min and median < heavy:
            continue
        items = (_item("mcp", users=len(values), median_distinct=median,
                       p90_distinct=_rank(values, Fraction(9, 10)), heavy_share=_dec(share),
                       heavy_threshold=str(heavy), magnitude=n_heavy),)
        out.append(_build(_Spec(
            kind="mcp-sprawl", category="aggregate", entity=_team_entity(inp, team), team=team,
            title=f"MCP server sprawl in {_label(team)}",
            summary=(f"Team {_label(team)}: median {median} distinct MCP servers per user; "
                     f"{_pct(share)} of users use {heavy} or more. Tool definitions are sent "
                     f"with every request; restrict MCP servers and use tool search."),
            cost=_count_only(), lever_ids=("copilot.mcp_trim",), n_events=len(values),
            n_users=len(users), first_seen_ms=_activity_first(inp, team), evidence=items)))
    return out


def _activity_first(inp: _In, team: str | None) -> int:
    return _first_seen(d.date_utc for d in inp.activity if (d.team or None) == team)


def _context_heavy_cli(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    if not inp.activity:
        skipped["context-heavy-cli"] = ("no per-user activity (aggregate-only bundle or no "
                                        "metrics)")
        return []
    limit = int(_threshold(inp, "cli_heavy_tokens", "100000", 2**53))
    out: list[Finding] = []
    for team, users in sorted(_activity_by_team(inp).items(), key=lambda kv: kv[0] or ""):
        ratios = [_round(Fraction(u.get("cli_prompt_tokens", 0), u["cli_requests"]))
                  for u in users.values() if u.get("cli_requests", 0) > 0]
        if not ratios:
            continue
        p50, p90 = _rank(ratios, Fraction(1, 2)), _rank(ratios, Fraction(9, 10))
        if p90 < limit:
            continue
        items = (_item("cli", users=len(ratios), prompt_tokens_per_request_p50=p50,
                       prompt_tokens_per_request_p90=p90, threshold=limit, magnitude=p90),)
        out.append(_build(_Spec(
            kind="context-heavy-cli", category="aggregate", entity=_team_entity(inp, team),
            team=team, title=f"Heavy Copilot CLI context in {_label(team)}",
            summary=(f"Copilot CLI users of team {_label(team)} send p50 {p50} / p90 {p90} "
                     f"prompt tokens per request. Use /compact at task boundaries and the "
                     f"default context tier."),
            cost=_count_only(), lever_ids=("copilot.context_default",), n_events=len(ratios),
            n_users=len(users), first_seen_ms=_activity_first(inp, team), evidence=items)))
    return out


def _seat_surfaces(inp: _In) -> dict[str | None, dict[str, set[str]]]:
    out: dict[str | None, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for lic in inp.ctx.licenses:
        if lic.last_activity_surface:
            out[lic.team or None][lic.last_activity_surface].add(lic.principal)
    return out


def _editor_mix(inp: _In, skipped: dict[str, str]) -> list[Finding]:
    teams: set[str | None] = {d.team or None for d in inp.activity}
    for snap in inp.config:
        if snap.kind == "activity_counts":
            teams.add(dict(snap.attrs).get("team") or None)  # type: ignore[arg-type]
    surfaces = _seat_surfaces(inp)
    out: list[Finding] = []
    for team in sorted(teams | set(surfaces), key=lambda t: t or ""):
        reach = inp.reach(team)
        if reach.shares:
            shares = reach.shares
            basis = "interactions"
            people = len({d.principal for d in inp.activity if (d.team or None) == team})
            if people == 0:
                people = max((v for s in inp.config if s.kind == "activity_counts"
                              and (dict(s.attrs).get("team") or None) == team
                              for k, v in s.attrs if k == "n_people" and type(v) is int),
                             default=0)
        elif surfaces.get(team):
            seats = surfaces[team]
            total = sum(len(p) for p in seats.values())
            shares = tuple(sorted((fam, Fraction(len(p), total)) for fam, p in seats.items()))
            basis = "seat last-activity surfaces"
            people = len(set().union(*seats.values()))
        else:
            continue
        jb = dict(shares).get("jetbrains", Fraction(0))
        heavy = _jetbrains_heavy(inp, shares)
        reach_text = (f"managed-model reach {_dec(reach.point)}" if reach.known and reach.shares
                      else "managed-model reach unknown (no usage metrics)")
        mix = ", ".join(f"{fam} {_pct(s)}" for fam, s in sorted(shares, key=lambda x: -x[1])[:4])
        items = (_item("editors", basis=basis, reach=_dec(reach.point) if reach.shares
                       else "unknown", jetbrains_share=_dec(jb), magnitude=1,
                       **{f"share_{fam}": _dec(s) for fam, s in shares}),)
        fix = _policy_fix(Reach(reach.point, reach.low, reach.high, reach.source, shares)) \
            if heavy else None
        out.append(_build(_Spec(
            kind="editor-mix", category="aggregate", entity=_team_entity(inp, team), team=team,
            title=f"Editor mix of {_label(team)}: JetBrains {_pct(jb)}",
            summary=(f"Team {_label(team)} by {basis}: {mix}; {reach_text}. The managed model "
                     f"key does not reach JetBrains: JetBrains-heavy teams need the server-side "
                     f"model policy first; VS Code teams can use managed \"model\": \"auto\"."),
            cost=_count_only(), n_events=people, n_users=people,
            first_seen_ms=_activity_first(inp, team), evidence=items, fix=fix,
            confidence="medium" if reach.known else "low")))
    return out


# ---------------------------------------------------------------------------------------------
# the detector
# ---------------------------------------------------------------------------------------------

_KIND_FUNCS: tuple[tuple[str, Callable[[_In, dict[str, str]], list[Finding]]], ...] = (
    ("premium-model-share", _premium_model_share),
    ("fast-mode", _fast_mode),
    ("auto-adoption", _auto_adoption),
    ("forced-migration", _forced_migration),
    ("compliance-uplift", _compliance_uplift),
    ("cache-health", _cache_health),
    ("review-cost", _review_cost),
    ("review-default-balanced", _review_default_balanced),
    ("review-drivers", _review_drivers),
    ("direct-org-usage", _direct_org_usage),
    ("agentic-workflow-cost", _agentic_workflow_cost),
    ("larger-runner", _larger_runner),
    ("cloud-agent-cost", _cloud_agent_cost),
    ("agent-failed-sessions", _agent_failed_sessions),
    ("unattributed-spend", _unattributed_spend),
    ("mcp-sprawl", _mcp_sprawl),
    ("context-heavy-cli", _context_heavy_cli),
    ("editor-mix", _editor_mix),
)


def _gate_reason(kind: str, caps: frozenset[str]) -> str | None:
    options = KIND_REQUIRES.get(kind)
    if options is None or any(opt <= caps for opt in options):
        return None
    need = " or ".join("+".join(sorted(opt)) for opt in options)
    return f"needs {need}"


def _skipped_finding(inp: _In, skipped: Mapping[str, str]) -> Finding:
    names = "; ".join(f"{k}: {v}" for k, v in sorted(skipped.items()))
    items = tuple(_item(f"skipped:{k}", reason=v, magnitude=0)
                  for k, v in sorted(skipped.items()))
    return _build(_Spec(
        kind="dq.skipped-kinds", category="data-quality",
        title=f"Copilot org scan: {len(skipped)} kinds not computed (inputs missing)",
        summary=(f"Not computed for missing inputs: {names}. No mechanical fix; add the "
                 f"missing source (usage metrics, the detailed usage report, agent tasks)."),
        cost=unpriced("unpriced: data-quality note", Basis.LIST_EQUIVALENT),
        evidence=items, confidence="high"))


class CopilotOrgScan:
    """``copilot.org-scan`` (addendum §10.2): the causes of Copilot AI-credit and Actions spend
    from GitHub's billing data. Aggregate detector (runs once per run, lanes never read); per-kind
    gates :data:`KIND_REQUIRES`; kinds that cannot run are named in one ``dq.skipped-kinds``
    finding."""

    id = DETECTOR_ID
    version = _VERSION
    kinds = KINDS
    requires = frozenset({"aggregates"})
    aggregate = True
    extension = "copilot"
    families = frozenset({"copilot"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Every §10.2 kind whose inputs are present, sorted by finding id."""
        del lanes  # aggregate detector: the findings never depend on lanes (R-E17)
        inp = _In(ctx)
        caps = frozenset(ctx.capabilities)
        skipped: dict[str, str] = {}
        out: list[Finding] = []
        for kind, fn in _KIND_FUNCS:
            reason = _gate_reason(kind, caps)
            if reason is not None:
                skipped[kind] = reason
                continue
            out.extend(fn(inp, skipped))
        if skipped:
            out.append(_skipped_finding(inp, skipped))
        return sorted(out, key=lambda f: f.finding_id)

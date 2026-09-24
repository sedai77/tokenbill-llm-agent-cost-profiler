"""GitHub Copilot reconciliation — the ledger gate for ``github_copilot``, ``github_actions`` and
``github_sandbox`` (addendum §12, §12.1; CP-RECON; SPEC §12 semantics).

:func:`reconcile_copilot` is the extension's ``ChannelReconciler`` (``ExtensionSpec.reconciler``).
It reads Copilot cost lines, AI usage report aggregates and ledger lanes from the ledger and
licenses / configuration from the record stores, prices report tokens with the given ``Pricer``
and returns **one** ``ReconciliationReport`` with three channel verdicts and the reusable
decisions (``ReconciliationReport.decisions``, CORE-AMENDMENTS C-17)::

    convention:<source_id>            excl | incl | undecidable     (per AI usage report file)
    gross_is_list:<entity>:<YYYY-MM>  true | false | unknown        (DC22, per pool entity-month)
    plan_fit:<entity>:<YYYY-MM>       business | enterprise | unknown  (diagnostic only, R17)

Every pool computation goes through ``core.pool`` (cells, entities, caps, pool months, discount
classes, the direct-draw rule); nothing here re-implements the pool rule. No floats, integer nano,
deterministic output (rows sorted by key; inputs may come in any order).

**Layers.**

* **L0 parity** (client data, when the ledger has Copilot inferences with a provider-reported
  cost): per request, our point price vs ``Inference.provider_reported_cost_nano`` (the runtime
  nano-AIU estimate, R12), a match being within 1 nano per priced line; per conversation (session),
  Σ our prices vs Σ ``COST_STATE`` totals of reporter ``copilot.otel.invoke_agent``. Diagnostics:
  ``copilot_auto_discount`` (does nano-AIU include the Auto 10%?), ``copilot_write_1h_price``
  (``published rate observed`` / ``2 x input rate observed``) and ``copilot_band_hypothesis``
  (``hypothesis A observed`` / ``hypothesis B observed``); anything else is
  ``copilot_rate_mismatch``. L0 compares provider *estimates*, so it never decides a verdict.
* **L1 rate card on report tokens** per (day, entity, model, SKU, routing), from ``core.pool``
  cells (pseudo cells excluded, legacy premium requests never token-priced): Σ tokens × the
  pricer's point rates with modifiers (``Pricer.unit_rates``; base rates, never the long-context
  band, because cells sum many requests) vs Σ gross, under both conventions. **Power rule** per
  file: the convention is decided only when Σ(read + write) ≥ 5% of Σ input over the deciding cells
  (not pseudo, not on a rate boundary, gross > 0), a strict majority of the file's model-days
  (date × model × org) fits the winner within ``tolerance_pct`` and a strict majority of them
  misses the other by more than 2 × ``tolerance_pct`` — a model-day fits when one of its price
  variants (as configured; Auto without its 10%; compliance toggled) is within tolerance, and
  misses when every variant is off by more; else ``undecidable``
  (``dq.copilot_convention_undecidable``: its L1 rows are computed under ``excl`` per R-E46 and are
  excluded from the verdict unless the file has no cache tokens at all, so the channel is at best
  reconciled on totals). Within tolerance means ``|gap| ≤ tolerance_pct × gross`` or ``|gap| ≤``
  :data:`ROW_SLACK_NANO` × report rows (credits carry 6 decimals, VERIFY §19.5 #6).
  Days within ±2 of a K-dated rate change of the cell's model (``core.facts.copilot_rates()``) are
  ``copilot_rate_boundary`` and excluded from the test; Auto rows at list (−10%), compliance
  (+10%), long-context models with a positive gap of at most the band (≤ 2 × base) and utility
  models with gross 0 are explained. ``gross_is_list`` per entity-month is ``true`` when every
  evaluated cell fits or is explained, ``false`` when one does not, else ``unknown``.
* **L2 token coverage** per (day, team, model) when the ledger holds Copilot inferences: ledger >
  report + max(1%, 1,000 tokens) is an over-count row (fails ``github_copilot``); report above
  ledger is ``unobserved_traffic`` (a coverage gap, the report gross share); non-billable utility
  calls are ``copilot_utility_unbilled``.
* **L3 identities** per entity × month: row identity (``quantity × $0.01 == gross`` ±1 nano and
  ``0 ≤ net ≤ gross`` per AI-credit row); DC22 discount classes from ``core.pool.pool_months``
  (``copilot_discount_unclassified`` → ``copilot_pool_included`` / ``copilot_direct_pool_draw``;
  Auto tenths ``copilot_auto_discount``); Σ net vs the REST usage summary **per product**
  (all AI-credit SKUs together; per SKU as a diagnostic) per month and scope (enterprise, or
  ``org:<login>`` for org summaries, where rows without an org are ``copilot_unattributed_org``);
  capped cost centers' pool draw vs their target; seat lines vs seat count × list
  (``copilot_seat_proration``; ``copilot_seat_contract`` for volume / azure billing); Actions lines
  vs runner rates and the summary's Actions SKUs (the Copilot share may not exceed the SKU total) →
  ``github_actions``; sandbox lines vs the summary → ``github_sandbox``; the revision check (the
  latest coverage aggregate of a day vs Σ current rows of that day) →
  ``copilot_revision_stale_rows``.
* **Plan fit** (owner answer 2): for an entity-month whose plan is unknown (two scenario pool
  months), closed and final: pooled discount above the Business pool (beyond tolerance), or pooled
  use above it with overage net 0 → ``enterprise``; overage net > 0 while use is below the
  Enterprise pool and no cost center is capped → ``business``; else ``unknown``. Residual
  ``copilot_plan_inferred`` (nano 0, informational). Never a label, pool or scenario source.

**Sources (decision keys).** The ``<source_id>`` of ``convention:`` is the ``source`` dim of the
file's ``github.ai_usage_report.coverage`` aggregates; a report day belongs to the source whose
coverage aggregate for that day was fetched last (ties: the larger id); days without a coverage
aggregate belong to :data:`FALLBACK_SOURCE_ID` (see
``tests/v2/copilot_recon/CONTRACT-CHANGE-CP-RECON-1.md``).

**Rows and residuals.** ``ReconRow.key`` pairs start with ``layer`` (L0 | L1 | L2 | L3) and
``check``. For L0 rows ``priced_provider_nano`` carries the provider *estimate* (nano-AIU), never an
invoice. For L3 rows ``invoice_nano`` is the invoice-side reference (usage summary net, seat count ×
list, cap, coverage total) and ``priced_provider_nano`` the report-side amount under test. Every
gap is signed "reference − ours" (positive when the provider / invoice side is higher); residual
amounts are signed sums of explained gaps in :data:`RESIDUAL_ORDER` (L0 codes appended), so the
Auto 10% at list shows +10% at L1 and −10% as a discount class; ``unexplained_nano`` is the Σ of
*absolute* unexplained gaps (opposite errors never cancel). Diagnostic rows (per-SKU summary,
revision rows of a month that has a summary, L0 matches) carry a code without adding to the totals.

**Verdicts** (addendum §12): ``github_copilot`` is ``insufficient_data`` without report or seat
lines; ``reconciled`` iff every closed, evaluated L1 row fits or is explained, every row identity
holds, no over-count row, no capped cost center above its cap, and the unexplained gaps of each
closed month are ≤ ``unexplained_pct`` of that month's gross; ``github_actions`` /
``github_sandbox`` need their lines and the usage summary's SKUs (else ``insufficient_data``).
``mapping_verified`` is False for all three until the release fixtures exist (§12.1):
:func:`verdict_label` prints ``reconciled (synthetic; schema unverified)`` (plus ``totals only``
when a file's convention is undecidable) and :func:`report_notes` names
``dq.recon_schema_unverified``.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation

from tokenbill.core import catalog as _catalog
from tokenbill.core import facts as _facts
from tokenbill.core import pool as _pool
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Finality
from tokenbill.core.money import (
    EXACT_CTX,
    NANO_USD_PER_CREDIT,
    credits_str_to_nano,
    decimal_to_nano,
    ratio,
    token_nano,
)
from tokenbill.core.protocols import ExtRecordStore, LedgerStore, Pricer
from tokenbill.core.records import (
    COPILOT_CHANNELS,
    ConfigSnapshot,
    CostLine,
    Inference,
    LaneEventKind,
    LicenseSnapshot,
    PricingContext,
    UsageAggregate,
    UsageBuckets,
)
from tokenbill.core.types import (
    ChannelVerdict,
    DataQualityNote,
    PoolMonth,
    PricedInference,
    ReconciliationReport,
    ReconRow,
    ResolvedRates,
    UnitRates,
)

__all__ = [
    "BOUNDARY_DAYS",
    "CHANNELS",
    "COVERAGE_KIND",
    "DEFAULT_TOLERANCE_PCT",
    "DQ_CONVENTION_UNDECIDABLE",
    "DQ_RECON_SCHEMA_UNVERIFIED",
    "FALLBACK_SOURCE_ID",
    "INVOICE_SOURCES",
    "L0_CODES",
    "POWER_RULE_PCT",
    "REPORT_KIND",
    "RESIDUAL_ORDER",
    "ROW_SLACK_NANO",
    "SCHEMA_VERIFIED",
    "k_dated_boundaries",
    "reconcile_copilot",
    "report_notes",
    "report_sources",
    "verdict_label",
]

#: The channels one Copilot report covers (addendum DC1), in verdict order.
CHANNELS = COPILOT_CHANNELS
#: ``ChannelVerdict.invoice_sources`` per channel (addendum §12).
INVOICE_SOURCES: Mapping[str, tuple[str, ...]] = {
    "github_copilot": ("github.ai_usage_report", "github.billing_api"),
    "github_actions": ("github.metered_usage", "github.billing_api"),
    "github_sandbox": ("github.metered_usage", "github.billing_api"),
}
#: Source kind of AI usage report rows and token aggregates.
REPORT_KIND = "github.ai_usage_report"
#: Source kind of the per (file, day) coverage aggregates (dims ``channel``, ``source``).
COVERAGE_KIND = "github.ai_usage_report.coverage"
#: Source id of report days no coverage aggregate names (builder-made or pre-coverage data).
FALLBACK_SOURCE_ID = "github.ai_usage_report"
#: Default rate-card tolerance (percent; SPEC §12.1) — also the panel's convention decision.
DEFAULT_TOLERANCE_PCT = Decimal("0.5")
#: Power rule: Σ(read + write) must reach this share (percent) of Σ input to decide a convention.
POWER_RULE_PCT = Decimal(5)
#: Days on either side of a K-dated rate change treated as ``copilot_rate_boundary``.
BOUNDARY_DAYS = 2
#: Absolute slack per report row (credits carry 6 decimals: ±0.5e-6 credits = ±5 nano; VERIFY).
ROW_SLACK_NANO = 10
#: False until real, redacted release fixtures exist (addendum §12.1, release gate (1)).
SCHEMA_VERIFIED = False
DQ_RECON_SCHEMA_UNVERIFIED = "dq.recon_schema_unverified"
DQ_CONVENTION_UNDECIDABLE = "dq.copilot_convention_undecidable"
#: Residual codes in the classifier order of the CP-RECON brief (``unexplained`` is separate).
RESIDUAL_ORDER = (
    "revision_window", "copilot_directional_report", "copilot_preview_columns",
    "copilot_rate_boundary", "copilot_discount_unclassified", "copilot_pool_included",
    "copilot_direct_pool_draw", "copilot_model_undisclosed", "copilot_auto_discount",
    "copilot_compliance_uplift", "copilot_long_context_band", "copilot_utility_unbilled",
    "copilot_unattributed_org", "unobserved_traffic", "copilot_seat_proration",
    "copilot_seat_contract", "copilot_revision_stale_rows", "copilot_rounding",
    "copilot_plan_inferred",
)
#: L0 (provider-estimate parity) codes, appended after :data:`RESIDUAL_ORDER`.
L0_CODES = ("copilot_rate_mismatch", "copilot_write_1h_price", "copilot_band_hypothesis")

_COPILOT = "github_copilot"
_ACTIONS = "github_actions"
_SANDBOX = "github_sandbox"
_AI_TYPES = ("ai_credit.user", "ai_credit.direct")
_SUMMARY = "rest.summary"
_INVOKE_AGENT = "copilot.otel.invoke_agent"
_ROUNDING_KEYS = ("github-ai-usage", REPORT_KIND)
_CENT_NANO = NANO_USD_PER_CREDIT            # $0.01 (one credit)
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_MAX_MS = 253_402_300_799_999
_BIG = 10**30
_HUNDRED = Decimal(100)
_PCT_CTX = Context(prec=60, rounding=ROUND_HALF_EVEN)
_PCT_Q = Decimal("0.0001")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FROM_SOURCE_RE = re.compile(r"(?:^|;)\s*date_source=([A-Z])")
_TO_SOURCE_RE = re.compile(r"effective_to date_source=([A-Z])")
_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
            "cache_write_other", "cache_write_unknown", "output")
_OK = ("match", "within_tolerance")
_DIRECTIONAL = ("directional_report_start", "directional_report_end")
_PREVIEW_END = "aic_columns_zeroed"


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _date_of_ms(ms: int) -> str:
    if ms > _MAX_MS:
        ms = _MAX_MS
    return (_EPOCH + _dt.timedelta(days=max(ms, 0) // _DAY_MS)).isoformat()


def _day_ms(date: str) -> int:
    return (_dt.date.fromisoformat(date) - _EPOCH).days * _DAY_MS


def _parse_pct(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise UsageError(f"{name}: expected a decimal string")
    try:
        d = Decimal(str(value).strip()) if not isinstance(value, Decimal) else value
    except InvalidOperation:
        raise UsageError(f"{name}: not a decimal") from None
    if not d.is_finite() or d < 0 or d > 100:
        raise UsageError(f"{name}: expected a percentage in [0, 100]")
    return d


def _within(gap: int, base: int, tol: Decimal, slack: int) -> bool:
    """``|gap| ≤ tol% × |base|`` or ``|gap| ≤ slack``."""
    g = abs(gap)
    if g <= slack:
        return True
    return Decimal(g) * _HUNDRED <= EXACT_CTX.multiply(tol, Decimal(abs(base)))


def _pct(num: int, den: int) -> str | None:
    """``num / den`` in percent, 4 decimals (None when ``den`` is 0)."""
    r = ratio(num, den)
    if r is None:
        return None
    return format(_PCT_CTX.multiply(r, _HUNDRED).quantize(_PCT_Q, context=_PCT_CTX), "f")


def _share(value: Decimal) -> str:
    return format(value.quantize(_PCT_Q, context=_PCT_CTX), "f")


def _clean(text: str | None) -> str:
    return _CONTROL_RE.sub("_", text or "")


def _nearest_rank(values: Sequence[Decimal], num: int, den: int) -> Decimal:
    ordered = sorted(values)
    idx = max(0, (num * len(ordered) + den - 1) // den - 1)
    return ordered[idx]


def _credits_nano(quantity: str | None) -> int | None:
    """Credits (a validated ``CostLine.quantity`` decimal string) → nano-USD, None when absent."""
    return None if quantity is None else credits_str_to_nano(quantity)[0]


def _decimal_or_none(text: str | None) -> Decimal | None:
    """A validated ``CostLine.quantity`` as a ``Decimal`` (None when absent)."""
    return None if text is None else Decimal(text)


# ---------------------------------------------------------------------------------------------
# facts: K-dated boundaries, bands, utility models
# ---------------------------------------------------------------------------------------------


def k_dated_boundaries() -> dict[str, frozenset[str]]:
    """Model → the dates of its K-dated rate changes (``date_source=K`` on a row's
    ``effective_from`` or ``effective_to``) in ``core.facts.copilot_rates()``: docs commit dates,
    not billing effective dates (addendum §19.5 #24)."""
    out: dict[str, set[str]] = defaultdict(set)
    for row in _facts.copilot_rates():
        m = _FROM_SOURCE_RE.search(row.notes)
        if m is not None and m.group(1) == "K":
            out[row.model].add(row.effective_from)
        t = _TO_SOURCE_RE.search(row.notes)
        if t is not None and t.group(1) == "K" and row.effective_to is not None:
            out[row.model].add(row.effective_to)
    return {k: frozenset(v) for k, v in sorted(out.items())}


def _near_boundary(model: str, date: str, bounds: Mapping[str, frozenset[str]]) -> bool:
    dates = bounds.get(model)
    if not dates:
        return False
    day = _dt.date.fromisoformat(date)
    return any(abs((day - _dt.date.fromisoformat(b)).days) <= BOUNDARY_DAYS for b in dates)


def _band_rows(model: str, date: str) -> int | None:
    """The long-context threshold of *model*'s facts row in force on *date* (None: no band)."""
    for row in _facts.copilot_rates():
        if row.model != model or row.long_context_threshold is None:
            continue
        if row.effective_from <= date and (row.effective_to is None or date < row.effective_to):
            return row.long_context_threshold
    return None


def _utility_models() -> frozenset[str]:
    return frozenset(_facts.load().copilot.utility_models)


# ---------------------------------------------------------------------------------------------
# sources (convention decision keys)
# ---------------------------------------------------------------------------------------------


def report_sources(aggregates: Iterable[UsageAggregate]) -> dict[str, str]:
    """Report day (``YYYY-MM-DD``) → AI usage report source id: the ``source`` dim of the
    ``github.ai_usage_report.coverage`` aggregate of that day fetched last (ties: the larger id).
    Days without one are not listed (they belong to :data:`FALLBACK_SOURCE_ID`)."""
    best: dict[str, tuple[int, str]] = {}
    for agg in aggregates:
        if not isinstance(agg, UsageAggregate) or agg.source_kind != COVERAGE_KIND:
            continue
        dims = dict(agg.dims)
        if dims.get("channel", _COPILOT) != _COPILOT:
            continue
        sid = dims.get("source")
        if not sid or _CONTROL_RE.search(sid):
            continue
        day = _date_of_ms(agg.bucket_start_ms)
        cand = (agg.fetched_ms, sid)
        if day not in best or cand > best[day]:
            best[day] = cand
    return {d: s for d, (_, s) in sorted(best.items())}


# ---------------------------------------------------------------------------------------------
# pricing report tokens (point rates; never the long-context band of one request)
# ---------------------------------------------------------------------------------------------


def _fallback_rate(r: ResolvedRates, bucket: str) -> Decimal:
    """SPEC §6.3 point rate of *bucket* from resolved rates: a bucket the row does not price falls
    back (writes → 5m → other → input; reads → input); unknown-TTL writes at the 5m rate."""
    if bucket == "uncached_input":
        return r.input
    if bucket == "output":
        return r.output
    if bucket == "cache_read":
        return r.cache_read if r.cache_read is not None else r.input
    order = {"cache_write_5m": (r.cache_write_5m, r.cache_write_other),
             "cache_write_unknown": (r.cache_write_5m, r.cache_write_other),
             "cache_write_1h": (r.cache_write_1h, r.cache_write_other),
             "cache_write_other": (r.cache_write_other, r.cache_write_5m)}[bucket]
    for rate in order:
        if rate is not None:
            return rate
    return r.input


class _Prices:
    """Point prices of token buckets through one pricer, cached per context and day."""

    def __init__(self, pricer: Pricer) -> None:
        self._pricer = pricer
        self._cache: dict[tuple, UnitRates | ResolvedRates | None] = {}

    def _rates(self, ctx: PricingContext, ts: int) -> UnitRates | ResolvedRates | None:
        key = (ctx, ts)
        if key not in self._cache:
            unit = self._pricer.unit_rates(ctx, ts_ms=ts)
            self._cache[key] = unit if unit is not None else self._pricer.resolve(ctx, ts_ms=ts)
        return self._cache[key]

    def point(self, usage: UsageBuckets, ctx: PricingContext, date: str) -> int | None:
        """Σ tokens × point rate per bucket (rounded once per bucket); None when unpriced."""
        rates = self._rates(ctx, _day_ms(date))
        if rates is None:
            return None
        total = 0
        for bucket in _BUCKETS:
            qty = getattr(usage, bucket)
            if not qty:
                continue
            if isinstance(rates, UnitRates):
                total += rates.bucket_nano(bucket, qty)
            else:
                total += token_nano(qty, _fallback_rate(rates, bucket))
        return total


def _ctx(model: str, routing: str, speed: str, compliance: str | None,
         billing_path: str = "copilot_pool") -> PricingContext:
    return PricingContext(provider="github", channel=_COPILOT, model=model, model_raw=model,
                          speed=speed if speed in ("standard", "fast") else "standard",
                          billing_path=billing_path,
                          routing=routing if routing in ("direct", "auto", "unknown") else
                          "unknown", compliance=compliance)


def _report_tokens(u: UsageBuckets, convention: str) -> int:
    """Report tokens of stored ``excl`` buckets under *convention* (``incl``: input already holds
    reads and writes)."""
    if convention == "incl":
        return u.uncached_input + u.output
    return u.total_input + u.output


# ---------------------------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------------------------


@dataclass
class _In:
    lines: list[CostLine]
    report_aggs: list[UsageAggregate]
    coverage: list[UsageAggregate]
    licenses: list[LicenseSnapshot]
    config: list[ConfigSnapshot]
    capped: dict[str, Decimal]
    flags: dict[str, str | int | bool]
    compliance: str | None


def _copilot_stores(record_stores: Sequence[ExtRecordStore]) -> list[ExtRecordStore]:
    stores = list(record_stores)
    named = [s for s in stores if getattr(s, "name", None) == "copilot"]
    return named or stores


def _read_inputs(ledger: LedgerStore, record_stores: Sequence[ExtRecordStore], since_ms: int,
                 until_ms: int) -> _In:
    lines = [c for c in ledger.cost_lines(since_ms=since_ms, until_ms=until_ms)
             if c.channel in CHANNELS]
    # cost lines are selected by day overlap, aggregates by bucket start: floor the start so a
    # mid-day window start keeps the first day's token aggregates with its rows
    aggs = ledger.aggregates(since_ms=since_ms - since_ms % _DAY_MS, until_ms=until_ms)
    report_aggs = [a for a in aggs if a.source_kind == REPORT_KIND]
    coverage = [a for a in aggs if a.source_kind == COVERAGE_KIND]
    licenses: list[LicenseSnapshot] = []
    config: list[ConfigSnapshot] = []
    for store in _copilot_stores(record_stores):
        licenses.extend(store.licenses(since_ms=since_ms, until_ms=until_ms))
        config.extend(store.config())
    flags = _pool.run_flags(config)
    comp = flags.get("compliance")
    return _In(lines=lines, report_aggs=report_aggs, coverage=coverage, licenses=licenses,
               config=config, capped=_pool.capped_cost_centers(config), flags=flags,
               compliance=comp if comp in ("data_residency", "fedramp") else None)


# ---------------------------------------------------------------------------------------------
# L1: cells, conventions, rows
# ---------------------------------------------------------------------------------------------

_VARIANTS = ("base", "direct", "comp", "both")


@dataclass
class _PCell:
    """A priced ``core.pool`` cell: prices per variant under both conventions."""

    cell: _pool.Cell
    source: str
    prices: dict[tuple[str, str], int | None]     # (variant, convention) → point nano


def _variant_ctx(cell: _pool.Cell, variant: str, compliance: str | None) -> PricingContext:
    routing = cell.routing
    comp = compliance
    if variant in ("direct", "both") and routing == "auto":
        routing = "direct"
    if variant in ("comp", "both"):
        comp = None if compliance is not None else "data_residency"
    path = "copilot_direct" if cell.cost_type == "ai_credit.direct" else "copilot_pool"
    return _ctx(cell.model, routing, cell.speed, comp, path)


def _price_cells(cells_excl: Sequence[_pool.Cell], cells_incl: Sequence[_pool.Cell],
                 prices: _Prices, sources: Mapping[str, str], compliance: str | None
                 ) -> list[_PCell]:
    """Priced AI-credit cells with a model (pseudo and model-less cells are not token-priced)."""
    out: list[_PCell] = []
    for ce, ci in zip(cells_excl, cells_incl, strict=True):
        if ce.pseudo is not None or ce.cost_type not in (*_AI_TYPES, "other") or not ce.model:
            continue
        date = ce.date_utc or f"{ce.month}-01"
        table: dict[tuple[str, str], int | None] = {}
        for variant in _VARIANTS:
            if variant in ("direct", "both") and ce.routing != "auto":
                continue
            ctx = _variant_ctx(ce, variant, compliance)
            table[(variant, "excl")] = prices.point(ce.usage, ctx, date)
            table[(variant, "incl")] = prices.point(ci.usage, ctx, date)
        out.append(_PCell(cell=ce, source=sources.get(date, FALLBACK_SOURCE_ID), prices=table))
    return out


@dataclass
class _Decision:
    source: str
    convention: str          # excl | incl | undecidable
    used: str                # the convention its rows are computed under
    evaluable: bool          # its rows enter the verdict and gross_is_list


def _decide(pcells: Sequence[_PCell], line_counts: Mapping[tuple[str, str, str], int],
            tol: Decimal,
            bounds: Mapping[str, frozenset[str]]) -> dict[str, _Decision]:
    """The power rule per source (module docstring). A model-day fits a convention when one of
    its price variants (as configured; Auto without its discount; compliance toggled) is within
    tolerance, and misses it when every variant is off by more than 2 × tolerance."""
    by_source: dict[str, list[_PCell]] = defaultdict(list)
    for pc in pcells:
        by_source[pc.source].append(pc)
    out: dict[str, _Decision] = {}
    for source in sorted(by_source):
        read_write = input_tokens = 0
        md: dict[tuple[str, str, str], dict[tuple[str, str], int]] = defaultdict(
            lambda: defaultdict(int))
        gross_md: dict[tuple[str, str, str], int] = defaultdict(int)
        cache_free = all(pc.cell.usage.cache_read == 0 and pc.cell.usage.cache_write == 0
                         for pc in by_source[source])
        for pc in by_source[source]:
            c = pc.cell
            date = c.date_utc or f"{c.month}-01"
            if (c.gross_nano <= 0 or None in pc.prices.values()
                    or _near_boundary(c.model, date, bounds)):
                continue
            read_write += c.usage.cache_read + c.usage.cache_write
            input_tokens += c.usage.uncached_input
            gross_md[(date, c.model, c.org or "")] += c.gross_nano
            acc = md[(date, c.model, c.org or "")]
            for variant in _VARIANTS:
                for conv in ("excl", "incl"):
                    price = pc.prices.get((variant, conv), pc.prices[("base", conv)])
                    acc[(variant, conv)] += price or 0
        power = input_tokens > 0 and Decimal(read_write) * _HUNDRED >= \
            EXACT_CTX.multiply(POWER_RULE_PCT, Decimal(input_tokens))
        convention = "undecidable"
        if power and md:
            n = len(md)
            fit = {"excl": 0, "incl": 0}
            miss = {"excl": 0, "incl": 0}
            for key, acc in md.items():
                gross = gross_md[key]
                slack = ROW_SLACK_NANO * max(1, line_counts.get(key, 1))
                for conv in ("excl", "incl"):
                    gaps = [acc[(v, conv)] - gross for v in _VARIANTS]
                    if any(_within(g, gross, tol, slack) for g in gaps):
                        fit[conv] += 1
                    if not any(_within(g, gross, 2 * tol, slack) for g in gaps):
                        miss[conv] += 1
            for conv, other in (("excl", "incl"), ("incl", "excl")):
                if 2 * fit[conv] > n and 2 * miss[other] > n:
                    convention = conv
        used = convention if convention != "undecidable" else "excl"
        out[source] = _Decision(source=source, convention=convention, used=used,
                                evaluable=convention != "undecidable" or cache_free)
    return out


@dataclass
class _L1Acc:
    gross: int = 0
    tokens: int = 0
    lines: int = 0
    final: bool = True
    unpriced: bool = False
    prices: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_VARIANTS, 0))
    variants: set[str] = field(default_factory=set)


@dataclass
class _Gap:
    """One gap of the reconciliation chain (see the module docstring)."""

    code: str | None           # residual code, or None when unexplained / not a residual
    nano: int                  # signed gap attributed to *code* (reference − ours)
    unexplained: int = 0       # |gap| left unexplained
    total: bool = True         # adds to the report's residual totals
    month: str | None = None   # for the unexplained gate (closed months)
    gate: bool = True          # the unexplained part counts toward the github_copilot gate


@dataclass
class _Ctx:
    """Shared state of one reconciliation."""

    tol: Decimal
    today: str
    closed_only: bool
    lag: int
    dates: Mapping[str, str]
    rows: list[ReconRow] = field(default_factory=list)
    gaps: dict[str, list[_Gap]] = field(default_factory=lambda: defaultdict(list))
    fails: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def month_closed(self, month: str) -> bool:
        first = _dt.date.fromisoformat(f"{month}-01")
        nxt = (first.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        last = nxt - _dt.timedelta(days=1)
        return last <= _dt.date.fromisoformat(self.today) - _dt.timedelta(days=self.lag)

    def window_code(self, date: str) -> str | None:
        """``copilot_directional_report`` (April 2026) / ``copilot_preview_columns`` (the preview
        month before the AI-credit columns were zeroed) for *date*, else None."""
        start, end = (self.dates.get(k) for k in _DIRECTIONAL)
        if start and end and start <= date < end:
            return "copilot_directional_report"
        preview_end = self.dates.get(_PREVIEW_END)
        if end and preview_end and end <= date < preview_end:
            return "copilot_preview_columns"
        return None


def _row(key: Sequence[tuple[str, str]], *, status: str, residual: str | None = None,
         ledger_tokens: int | None = None, provider_tokens: int | None = None,
         ledger_nano: int | None = None, priced_nano: int | None = None,
         invoice_nano: int | None = None, error_pct: str | None = None,
         coverage_pct: str | None = None) -> ReconRow:
    return ReconRow(key=tuple((k, _clean(v)) for k, v in key), ledger_tokens=ledger_tokens,
                    provider_tokens=provider_tokens, ledger_nano=ledger_nano,
                    priced_provider_nano=priced_nano, invoice_nano=invoice_nano,
                    rate_card_error_pct=error_pct, coverage_pct=coverage_pct, status=status,
                    residual_code=residual)


def _explain_l1(acc: _L1Acc, model: str, date: str, tol: Decimal, utility: frozenset[str]
                ) -> tuple[str, str | None]:
    """(``fit`` | ``explained`` | ``unfit`` | ``unpriced``, residual code) of one L1 row."""
    if model in utility and acc.gross == 0:
        return "explained", "copilot_utility_unbilled"
    if acc.unpriced:
        return "unpriced", None
    slack = ROW_SLACK_NANO * max(1, acc.lines)
    base = acc.prices["base"]
    if _within(base - acc.gross, acc.gross, tol, slack):
        return "fit", None
    for variant, code in (("direct", "copilot_auto_discount"),
                          ("comp", "copilot_compliance_uplift"),
                          ("both", "copilot_auto_discount")):
        if variant in acc.variants and _within(acc.prices[variant] - acc.gross, acc.gross, tol,
                                               slack):
            return "explained", code
    if _band_rows(model, date) is not None and acc.gross > base and acc.gross <= 2 * base + slack:
        return "explained", "copilot_long_context_band"
    return "unfit", None


def _l1(pcells: Sequence[_PCell], decisions: Mapping[str, _Decision],
        line_counts: Mapping[tuple[str, str, str, str, str, str], int], st: _Ctx,
        bounds: Mapping[str, frozenset[str]], pseudo_cells: Sequence[_pool.Cell]
        ) -> tuple[dict[tuple[str, str], str], list[Decimal]]:
    """L1 rows, gaps and fails; returns gross_is_list per (entity, month) and the |%| errors of
    the gate model-days."""
    utility = _utility_models()
    rows: dict[tuple[str, str, str, str, str, str], _L1Acc] = {}
    for pc in pcells:
        c = pc.cell
        date = c.date_utc or f"{c.month}-01"
        dec = decisions[pc.source]
        key = (date, c.entity_id, c.org or "", c.model, c.sku, c.routing, pc.source)
        acc = rows.setdefault(key, _L1Acc())
        acc.gross += c.gross_nano
        acc.tokens += _report_tokens(c.usage, dec.used)
        acc.final = acc.final and c.final
        for variant in _VARIANTS:
            p = pc.prices.get((variant, dec.used))
            if (variant, dec.used) not in pc.prices:
                p = pc.prices[("base", dec.used)]
            else:
                acc.variants.add(variant)
            if p is None:
                acc.unpriced = True
            else:
                acc.prices[variant] += p
    gil: dict[tuple[str, str], list[bool]] = defaultdict(list)
    model_days: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for (date, entity, org, model, sku, routing, source), acc in sorted(rows.items()):
        month = date[:7]
        dec = decisions[source]
        acc.lines = line_counts.get((date, entity, org, model, sku, routing), 0)
        cls, code = _explain_l1(acc, model, date, st.tol, utility)
        boundary = _near_boundary(model, date, bounds)
        provisional = not acc.final
        window = st.window_code(date)
        if st.closed_only and provisional:
            continue
        base = None if acc.unpriced else acc.prices["base"]
        gap = acc.gross - (base or 0)
        if dec.evaluable and not boundary and window is None and cls != "unpriced":
            gil[(entity, month)].append(cls in ("fit", "explained"))
        if provisional:
            status, residual = "provisional", "revision_window"
        elif window is not None:
            status, residual = "explained", window
        elif boundary:
            status, residual = "explained", "copilot_rate_boundary"
        elif cls == "fit":
            slack = ROW_SLACK_NANO * max(1, acc.lines)
            status, residual = ("match" if abs(gap) <= slack else "within_tolerance"), None
        elif cls == "explained":
            status, residual = "explained", code
        else:
            status, residual = "unexplained", None
        gate = dec.evaluable and status in ("match", "within_tolerance", "explained",
                                            "unexplained") and residual not in (
            "copilot_rate_boundary", "copilot_directional_report", "copilot_preview_columns")
        if status == "unexplained":
            if gate:
                st.fails[_COPILOT] += 1
            st.gaps[_COPILOT].append(_Gap(None, 0, abs(gap), month=month, gate=gate))
        elif residual is not None and status != "match":
            st.gaps[_COPILOT].append(_Gap(residual, gap, month=month))
        if gate and base is not None and acc.gross > 0 and residual is None:
            md = model_days[(date, model)]
            md[0] += acc.gross
            md[1] += base
        conv = dec.used if dec.convention != "undecidable" else "undecidable"
        st.rows.append(_row(
            (("layer", "L1"), ("check", "rate_card"), ("channel", _COPILOT), ("month", month),
             ("date", date), ("entity", entity), ("org", org), ("model", model), ("sku", sku),
             ("routing", routing), ("convention", conv), ("source", source)),
            status=status, residual=residual, provider_tokens=acc.tokens, priced_nano=base,
            invoice_nano=acc.gross,
            error_pct=None if base is None else _pct(base - acc.gross, acc.gross)))
    for c in pseudo_cells:
        date = c.date_utc or f"{c.month}-01"
        if st.closed_only and not c.final:
            continue
        st.gaps[_COPILOT].append(_Gap("copilot_model_undisclosed", c.gross_nano, month=c.month))
        st.rows.append(_row(
            (("layer", "L1"), ("check", "rate_card"), ("channel", _COPILOT), ("month", c.month),
             ("date", date), ("entity", c.entity_id), ("model", ""), ("sku", c.sku),
             ("routing", c.routing), ("pseudo", c.pseudo or ""), ("cost_type", c.cost_type)),
            status="explained", residual="copilot_model_undisclosed",
            provider_tokens=_report_tokens(c.usage, "excl"), invoice_nano=c.gross_nano))
    errors = [abs(ratio(p - g, g) * _HUNDRED) for g, p in model_days.values() if g > 0]  # type: ignore[operator]
    gross_is_list = {k: ("true" if all(v) else "false") for k, v in gil.items() if v}
    return gross_is_list, errors


# ---------------------------------------------------------------------------------------------
# L0: parity with provider estimates
# ---------------------------------------------------------------------------------------------


@dataclass
class _L0Acc:
    n: int = 0
    ours: int = 0
    provider: int = 0


def _l0_class(pricer: Pricer, inf: Inference, ts: int, priced: PricedInference
              ) -> tuple[str, str, str, int | None]:
    """(status, code or "", diagnostic, our point) of one inference with a provider-reported
    cost; code "" is a plain match."""
    provider = inf.provider_reported_cost_nano
    assert provider is not None
    point = priced.figure.nano
    tol = max(1, len(priced.lines))
    if point is None:
        return "unexplained", "copilot_rate_mismatch", "unpriced", None
    ctx = inf.pricing
    date = _date_of_ms(ts)
    threshold = _band_rows(ctx.model, date)
    band_differs = (threshold is not None and ctx.context_tier is not None
                    and (inf.usage.total_input > threshold) != (ctx.context_tier == "long_context"))

    def alt(**kw: object) -> int | None:
        alt_inf = replace(inf, pricing=replace(ctx, **kw))
        return pricer.price_inference(alt_inf, ts_ms=ts).figure.nano

    write_1h = alt(write_ttl_hint="1h") if inf.usage.cache_write_unknown else None
    if abs(provider - point) <= tol:
        if ctx.routing == "auto":
            return "match", "copilot_auto_discount", "nano-AIU includes the Auto discount", point
        if write_1h is not None and write_1h != point:
            return "match", "copilot_write_1h_price", "published rate observed", point
        if band_differs:
            return "match", "copilot_band_hypothesis", "hypothesis A observed", point
        return "match", "", "", point
    if ctx.routing == "auto":
        direct = alt(routing="direct")
        if direct is not None and abs(provider - direct) <= tol:
            return "explained", "copilot_auto_discount", "nano-AIU excludes the Auto discount", \
                point
    if write_1h is not None and write_1h != point and abs(provider - write_1h) <= tol:
        return "explained", "copilot_write_1h_price", "2 x input rate observed", point
    fig = priced.figure
    if band_differs and not inf.usage.cache_write_unknown and fig.low_nano is not None:
        other = fig.high_nano if fig.low_nano == point else fig.low_nano
        if other is not None and abs(provider - other) <= tol:
            return "explained", "copilot_band_hypothesis", "hypothesis B observed", point
    return "unexplained", "copilot_rate_mismatch", "", point


def _l0_l2(ledger: LedgerStore, pricer: Pricer, since_ms: int, until_ms: int, st: _Ctx
           ) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str, str], int], int, bool]:
    """L0 rows and gaps; returns the ledger token sums per (date, team, model) for L2, the utility
    tokens per key, Σ ledger point nano and whether any Copilot inference exists."""
    acc: dict[tuple[str, ...], _L0Acc] = defaultdict(_L0Acc)
    tokens: dict[tuple[str, str, str], int] = defaultdict(int)
    utility: dict[tuple[str, str, str], int] = defaultdict(int)
    session_ours: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])   # ours, lines, unpriced
    session_first: dict[str, tuple[str, str]] = {}
    session_reported: dict[str, int] = defaultdict(int)
    ledger_nano = 0
    seen = False
    for lane in ledger.iter_lanes(since_ms=since_ms, until_ms=until_ms):
        for ev in lane.events:
            if ev.kind is LaneEventKind.COST_STATE:
                attrs = dict(ev.attrs)
                total = attrs.get("reported_total_nano")
                if attrs.get("reporter") == _INVOKE_AGENT and type(total) is int:
                    session_reported[lane.session_key] += total
        for req in lane.requests:
            team = req.attribution.team or ""
            for att in req.attempts:
                for inf in att.inferences:
                    if inf.pricing.channel != _COPILOT:
                        continue
                    seen = True
                    date = _date_of_ms(att.ts_start_ms)
                    key = (date, team, inf.pricing.model)
                    if inf.billable is False:
                        utility[key] += inf.usage.total_input + inf.usage.output
                        continue
                    tokens[key] += inf.usage.total_input + inf.usage.output
                    priced = pricer.price_inference(inf, ts_ms=att.ts_start_ms)
                    s = session_ours[lane.session_key]
                    session_first.setdefault(lane.session_key, (date, inf.pricing.model))
                    s[1] += max(1, len(priced.lines))
                    if priced.figure.nano is None:
                        s[2] += 1
                    else:
                        s[0] += priced.figure.nano
                        ledger_nano += priced.figure.nano
                    if inf.provider_reported_cost_nano is None:
                        continue
                    status, code, detail, point = _l0_class(pricer, inf, att.ts_start_ms,
                                                             priced)
                    a = acc[("request", date, inf.pricing.model, status, code, detail)]
                    a.n += 1
                    a.ours += point or 0
                    a.provider += inf.provider_reported_cost_nano
    for skey, reported in sorted(session_reported.items()):
        if skey not in session_ours:
            continue
        ours, lines, unpriced = session_ours[skey]
        date, model = session_first[skey]
        ok = not unpriced and abs(reported - ours) <= lines
        a = acc[("conversation", date, "", "match" if ok else "unexplained",
                 "" if ok else "copilot_rate_mismatch", "")]
        a.n += 1
        a.ours += ours
        a.provider += reported
    for (check, date, model, status, code, detail), a in sorted(acc.items()):
        gap = a.provider - a.ours
        if code and (status != "match" or code in L0_CODES):
            st.gaps["L0"].append(_Gap(code, gap if status != "match" else 0,
                                      total=code in L0_CODES, gate=False))
        key = [("layer", "L0"), ("check", check), ("channel", _COPILOT), ("month", date[:7]),
               ("date", date), ("model", model), ("requests", str(a.n))]
        if detail:
            key.append(("diagnostic", detail))
        st.rows.append(_row(key, status=status, residual=code or None, ledger_nano=a.ours,
                            priced_nano=a.provider, error_pct=_pct(a.ours - a.provider,
                                                                   a.provider)))
    return tokens, utility, ledger_nano, seen


def _l2(pcells: Sequence[_PCell], decisions: Mapping[str, _Decision],
        ledger_tokens: Mapping[tuple[str, str, str], int],
        utility: Mapping[tuple[str, str, str], int], covered: set[str], st: _Ctx
        ) -> tuple[int, int, int]:
    """L2 rows over the report's *covered* days (days with report rows or a coverage aggregate;
    under ``closed_only`` without provisional rows) — ledger days the report does not cover yet
    (the report lags) are no over-count. Returns (over-count rows, Σ ledger, Σ report tokens)."""
    report: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    days = set(covered)
    for pc in pcells:
        c = pc.cell
        date = c.date_utc or f"{c.month}-01"
        days.add(date)
        key = (date, c.team or "", c.model)
        r = report[key]
        r[0] += _report_tokens(c.usage, decisions[pc.source].used)
        r[1] += c.gross_nano
    if st.closed_only:
        days -= {pc.cell.date_utc or "" for pc in pcells if not pc.cell.final}
    over = led_total = rep_total = 0
    for key in sorted(k for k in set(report) | set(ledger_tokens) if k[0] in days):
        date, team, model = key
        rep, gross = report.get(key, [0, 0])
        led = ledger_tokens.get(key, 0)
        led_total += led
        rep_total += rep
        margin = max(rep // 100, 1000)
        base = [("layer", "L2"), ("check", "token_coverage"), ("channel", _COPILOT),
                ("month", date[:7]), ("date", date), ("team", team), ("model", model)]
        if led > rep + margin:
            over += 1
            st.fails[_COPILOT] += 1
            status, residual = "over", None
        elif rep > led + margin:
            status, residual = "explained", "unobserved_traffic"
            missing = gross if rep == 0 else gross * (rep - led) // rep
            st.gaps[_COPILOT].append(_Gap("unobserved_traffic", missing, gate=False))
        else:
            status, residual = ("match" if led == rep else "within_tolerance"), None
        st.rows.append(_row(base, status=status, residual=residual, ledger_tokens=led,
                            provider_tokens=rep, invoice_nano=gross, coverage_pct=_pct(led, rep)))
    for key, n in sorted(utility.items()):
        date, team, model = key
        if date not in days:
            continue
        st.gaps[_COPILOT].append(_Gap("copilot_utility_unbilled", 0, gate=False))
        st.rows.append(_row([("layer", "L2"), ("check", "utility"), ("channel", _COPILOT),
                             ("month", date[:7]), ("date", date), ("team", team),
                             ("model", model)], status="explained",
                            residual="copilot_utility_unbilled", ledger_tokens=n))
    return over, led_total, rep_total


# ---------------------------------------------------------------------------------------------
# L3: identities, discounts, summary, caps, seats, actions, sandbox, revisions
# ---------------------------------------------------------------------------------------------


def _explain_budget(gap: int, budgets: Sequence[tuple[str, int, int]]
                    ) -> tuple[list[tuple[str, int]], int]:
    """Attribute *gap* to budgets ``(code, magnitude, sign)`` in order (sign 0 = either
    direction); returns the explained parts (signed) and the unexplained magnitude."""
    left = abs(gap)
    sgn = 1 if gap > 0 else -1
    parts: list[tuple[str, int]] = []
    for code, magnitude, sign in budgets:
        if left == 0:
            break
        if magnitude <= 0 or (sign and sign != sgn):
            continue
        take = min(left, magnitude)
        parts.append((code, sgn * take))
        left -= take
    return parts, left


def _identity(lines: Sequence[CostLine], entity_of: Callable[[CostLine], str],
              st: _Ctx) -> None:
    """Row identity per AI-credit report row: ``quantity × $0.01 == gross`` (±1 nano) and
    ``0 ≤ net ≤ gross``; one row per entity-month."""
    fails: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for line in lines:
        if line.source_kind != REPORT_KIND or line.cost_type not in _AI_TYPES:
            continue
        if st.closed_only and line.finality != "final":
            continue
        gross = line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano
        key = (line.date_utc[:7], entity_of(line))
        acc = fails[key]
        acc[1] += gross
        q = _credits_nano(line.quantity)
        if q is not None:
            acc[2] += q
        if (q is not None and abs(q - gross) > 1) or line.amount_nano < 0 or \
                line.amount_nano > gross:
            acc[0] += 1
    for (month, entity), (bad, gross, quantity) in sorted(fails.items()):
        if bad:
            st.fails[_COPILOT] += 1
        st.rows.append(_row((("layer", "L3"), ("check", "row_identity"), ("channel", _COPILOT),
                             ("month", month), ("entity", entity), ("failing_rows", str(bad))),
                            status="match" if not bad else "unexplained", priced_nano=quantity,
                            invoice_nano=gross))


def _discounts(pms: Sequence[PoolMonth], cells: Sequence[_pool.Cell], st: _Ctx) -> None:
    """DC22 discount-class rows per entity-month from ``core.pool.pool_months``."""
    by_em: dict[tuple[str, str], list[PoolMonth]] = defaultdict(list)
    for pm in pms:
        by_em[(pm.entity_id, pm.month)].append(pm)
    cells_em: dict[tuple[str, str], list[_pool.Cell]] = defaultdict(list)
    for c in cells:
        cells_em[(c.entity_id, c.month)].append(c)
    for (entity, month), group in sorted(by_em.items()):
        if st.closed_only and any(p.finality != "closed" for p in group):
            continue
        draws = {p.pool_draw_nano for p in group}
        other = group[0].discount_other_nano
        direct = [c for c in cells_em.get((entity, month), []) if c.cost_type == "ai_credit.direct"]
        direct_part = _pool.classify_discounts(direct, gross_is_list=True, pool_nano=_BIG)[0] or 0
        parts: list[tuple[str, int]] = []
        if None not in draws and len(draws) == 1:
            draw = next(iter(draws))
            assert draw is not None
            parts.append(("copilot_pool_included", draw - direct_part))
            parts.append(("copilot_direct_pool_draw", direct_part))
        else:
            unclassified = max(p.discount_unclassified_nano for p in group)
            parts.append(("copilot_discount_unclassified", unclassified))
        parts.append(("copilot_auto_discount", other))
        for code, nano in parts:
            if nano == 0:
                continue
            st.gaps[_COPILOT].append(_Gap(code, -nano, month=month))
            st.rows.append(_row((("layer", "L3"), ("check", "discount_class"),
                                 ("channel", _COPILOT), ("month", month), ("entity", entity)),
                                status="explained", residual=code, invoice_nano=-nano))


def _scope_lines(lines: Sequence[CostLine], scope: str) -> list[CostLine]:
    if scope == "enterprise":
        return list(lines)
    org = scope[4:]
    return [c for c in lines if c.workspace_id == org]


def _summary_product(line: CostLine) -> str | None:
    sku = line.sku or ""
    if _catalog.copilot_cost_type(sku, username_present=True) == "ai_credit.user":
        return "ai_credits"
    if _catalog.copilot_seat_plan(sku) is not None:
        return "seats"
    if line.channel == _SANDBOX or _catalog.copilot_cost_type(sku, username_present=True) == \
            "sandbox":
        return "sandbox"
    if line.channel == _ACTIONS or _catalog.runner_rate(sku) is not None:
        return "actions"
    return None


def _line_product(line: CostLine) -> str | None:
    if line.cost_type in _AI_TYPES and line.source_kind == REPORT_KIND:
        return "ai_credits"
    if line.cost_type == "seat" and line.channel == _COPILOT:
        return "seats"
    if line.cost_type == "actions" and line.channel == _ACTIONS:
        return "actions"
    if line.cost_type == "sandbox" and line.channel == _SANDBOX:
        return "sandbox"
    return None


_PRODUCT_CHANNEL = {"ai_credits": _COPILOT, "seats": _COPILOT, "actions": _ACTIONS,
                    "sandbox": _SANDBOX}


def _norm_sku(sku: str | None) -> str:
    s = sku or ""
    return s[len("actions_"):] if s.startswith("actions_") else s


def _summaries(inp: _In, stale: Mapping[str, int], rounding: int, st: _Ctx) -> set[str]:
    """Σ net vs the usage summary per (month, scope, product), per-SKU diagnostics, and the
    Actions coverage rule. Returns the months with an AI-credit summary."""
    summary = [c for c in inp.lines if c.cost_type == _SUMMARY]
    lines_by: dict[tuple[str, str], list[CostLine]] = defaultdict(list)
    for c in inp.lines:
        product = _line_product(c)
        if product is not None and not (st.closed_only and c.finality != "final"):
            lines_by[(c.date_utc[:7], product)].append(c)
    sums: dict[tuple[str, str, str], list[CostLine]] = defaultdict(list)
    for s in summary:
        product = _summary_product(s)
        if product is None:
            continue
        scope = f"org:{s.workspace_id}" if s.workspace_id else "enterprise"
        sums[(s.date_utc[:7], scope, product)].append(s)
    ai_months: set[str] = set()
    for (month, scope, product), slines in sorted(sums.items()):
        if st.closed_only and not st.month_closed(month):
            continue
        channel = _PRODUCT_CHANNEL[product]
        ours = _scope_lines(lines_by.get((month, product), []), scope)
        net = sum(c.amount_nano for c in ours)
        ref = sum(c.amount_nano for c in slines)
        gap = ref - net
        base = [("layer", "L3"), ("check", "summary"), ("channel", channel), ("month", month),
                ("scope", scope), ("product", product)]
        if product == "ai_credits":
            ai_months.add(month)
        if product == "actions":
            # the summary SKUs also bill non-Copilot workflows: the Copilot share may not exceed it
            per_sku_ref: dict[str, int] = defaultdict(int)
            for s in slines:
                per_sku_ref[_norm_sku(s.sku)] += s.amount_nano
            per_sku_ours: dict[str, int] = defaultdict(int)
            for c in ours:
                per_sku_ours[_norm_sku(c.sku)] += c.amount_nano
            for sku in sorted(set(per_sku_ref) | set(per_sku_ours)):
                r, o = per_sku_ref.get(sku), per_sku_ours.get(sku, 0)
                key = [*base, ("sku", sku)]
                if r is None:
                    status = "unexplained"
                elif o <= r or _within(r - o, r, st.tol, _CENT_NANO):
                    status = "match" if o <= r else "within_tolerance"
                else:
                    status = "over"
                if status in ("unexplained", "over"):
                    st.fails[_ACTIONS] += 1
                    st.gaps[_ACTIONS].append(_Gap(None, 0, abs((r or 0) - o), month=month,
                                                  gate=False))
                st.rows.append(_row(key, status=status, priced_nano=o, invoice_nano=r,
                                    coverage_pct=None if r is None else _pct(o, r)))
            continue
        # an open month's summary and rows are both still moving: all of its gap is revision
        # window; a closed month only has its provisional rows to revise
        provisional = sum(c.amount_nano for c in ours if c.finality != "final")
        window = abs(gap) if not st.month_closed(month) else abs(provisional)
        budgets = [("revision_window", window, 0),
                   ("copilot_revision_stale_rows", stale.get(month, 0), -1)]
        if scope != "enterprise":
            unattributed = sum(c.amount_nano for c in lines_by.get((month, product), [])
                               if not c.workspace_id)
            budgets.append(("copilot_unattributed_org", unattributed, 1))
        budgets.append(("copilot_rounding", rounding, 0))
        if _within(gap, ref, st.tol, _CENT_NANO):
            status = "match" if gap == 0 else "within_tolerance"
            residual = None
        else:
            parts, left = _explain_budget(gap, budgets)
            for code, nano in parts:
                st.gaps[channel].append(_Gap(code, nano, month=month))
            if left:
                st.gaps[channel].append(_Gap(None, 0, left, month=month))
                if channel != _COPILOT:
                    st.fails[channel] += 1
            status = "unexplained" if left else "explained"
            residual = parts[0][0] if parts and not left else None
        st.rows.append(_row(base, status=status, residual=residual, priced_nano=net,
                            invoice_nano=ref, error_pct=_pct(net - ref, ref)))
        if product == "ai_credits":   # per SKU: a diagnostic only (report SKU grouping unverified)
            ref_sku: dict[str, int] = defaultdict(int)
            ours_sku: dict[str, int] = defaultdict(int)
            for s in slines:
                ref_sku[s.sku or ""] += s.amount_nano
            for c in ours:
                ours_sku[c.sku or ""] += c.amount_nano
            for sku in sorted(set(ref_sku) | set(ours_sku)):
                r, o = ref_sku.get(sku, 0), ours_sku.get(sku, 0)
                st.rows.append(_row([*base, ("sku", sku), ("diagnostic", "per_sku")],
                                    status="match" if _within(r - o, r, st.tol, _CENT_NANO)
                                    else "explained", priced_nano=o, invoice_nano=r))
    return ai_months


def _actions_rates(inp: _In, st: _Ctx) -> None:
    """Actions lines: gross vs minutes × the runner rate (``core.catalog.runner_rate``)."""
    acc: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for c in inp.lines:
        if c.cost_type != "actions" or c.channel != _ACTIONS:
            continue
        if st.closed_only and c.finality != "final":
            continue
        gross = c.list_amount_nano if c.list_amount_nano is not None else c.amount_nano
        a = acc[(c.date_utc[:7], _norm_sku(c.sku))]
        a[0] += gross
        a[2] += 1
        rate = _catalog.runner_rate(c.sku)
        minutes = _decimal_or_none(c.quantity)
        if rate is None or minutes is None:
            a[3] += 1
        else:
            a[1] += decimal_to_nano(EXACT_CTX.multiply(minutes, rate.usd_per_minute))
    for (month, sku), (gross, priced, n, unknown) in sorted(acc.items()):
        key = (("layer", "L3"), ("check", "runner_rate"), ("channel", _ACTIONS), ("month", month),
               ("sku", sku))
        if unknown:
            st.fails[_ACTIONS] += 1
            st.gaps[_ACTIONS].append(_Gap(None, 0, gross, month=month, gate=False))
            st.rows.append(_row(key, status="unexplained", invoice_nano=gross))
            continue
        gap = gross - priced
        ok = _within(gap, gross, st.tol, _CENT_NANO * max(1, n))
        if not ok:
            st.fails[_ACTIONS] += 1
            st.gaps[_ACTIONS].append(_Gap(None, 0, abs(gap), month=month, gate=False))
        st.rows.append(_row(key, status=("match" if gap == 0 else "within_tolerance") if ok
                            else "unexplained", priced_nano=priced, invoice_nano=gross,
                            error_pct=_pct(priced - gross, gross)))


def _caps(pms: Sequence[PoolMonth], inp: _In, cells: Sequence[_pool.Cell], st: _Ctx) -> None:
    """Capped cost centers: their pool draw (non-Auto discount) vs the configured target."""
    by_em: dict[tuple[str, str], list[_pool.Cell]] = defaultdict(list)
    for c in cells:
        by_em[(c.entity_id, c.month)].append(c)
    seen: set[tuple[str, str]] = set()
    for pm in pms:
        if not pm.entity_id.startswith("cc:") or pm.entity_id[3:] not in inp.capped:
            continue
        key = (pm.entity_id, pm.month)
        if key in seen or (st.closed_only and pm.finality != "closed"):
            continue
        seen.add(key)
        draw = _pool.classify_discounts(by_em.get(key, []), gross_is_list=True,
                                        pool_nano=_BIG)[0] or 0
        cap = pm.pool_nano
        gap = cap - draw
        ok = draw <= cap or _within(gap, cap, st.tol, _CENT_NANO)
        if not ok:
            st.fails[_COPILOT] += 1
            st.gaps[_COPILOT].append(_Gap(None, 0, draw - cap, month=pm.month))
        st.rows.append(_row((("layer", "L3"), ("check", "cost_center_cap"), ("channel", _COPILOT),
                             ("month", pm.month), ("entity", pm.entity_id),
                             ("policy", pm.capped_policy or "unknown")),
                            status="match" if ok else "over", priced_nano=draw, invoice_nano=cap,
                            coverage_pct=_pct(draw, cap)))


def _seats(inp: _In, entity_of: Callable[[str | None, str | None], str], st: _Ctx) -> None:
    """Seat lines vs seat count × list (``copilot_seat_proration`` / ``copilot_seat_contract``)."""
    fn = entity_of
    plans = _facts.copilot_plans()
    modes = _pool.billing_modes(inp.lines, inp.config, inp.licenses)
    seat: dict[tuple[str, str, str], list] = defaultdict(lambda: [0, Decimal(0), 0])
    for c in inp.lines:
        if c.cost_type != "seat" or c.channel != _COPILOT:
            continue
        if st.closed_only and c.finality != "final":
            continue
        plan = _catalog.copilot_seat_plan(c.sku)
        if plan is None:
            continue
        a = seat[(c.date_utc[:7], fn(c.cost_center, c.workspace_id), plan)]
        a[0] += c.amount_nano
        q = _decimal_or_none(c.quantity)
        if q is not None:
            a[1] = EXACT_CTX.add(a[1], q)
        a[2] += 1
    counts: dict[tuple[str, str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for lic in inp.licenses:
        if lic.source_kind != "github.copilot_seats" or lic.plan not in plans:
            continue
        key = (lic.snapshot_date[:7], fn(lic.cost_center, lic.org), lic.plan)
        counts[key][lic.snapshot_date].add(lic.principal)
    for (month, entity, plan), (net, qty, n) in sorted(seat.items()):
        price = plans[plan].seat_usd_per_month
        snaps = counts.get((month, entity, plan))
        count = Decimal(max(len(v) for v in snaps.values())) if snaps else qty
        expected = decimal_to_nano(EXACT_CTX.multiply(count, price))
        gap = net - expected
        mode = modes.get(entity, "unknown")
        key = (("layer", "L3"), ("check", "seats"), ("channel", _COPILOT), ("month", month),
               ("entity", entity), ("plan", plan), ("count_source",
                                                    "seats_api" if snaps else "seat_lines"))
        if _within(gap, expected, Decimal(0), _CENT_NANO * max(1, n)):
            status, residual = "match", None
        elif mode in ("volume", "azure"):
            status, residual = "explained", "copilot_seat_contract"
        elif abs(gap) <= expected:
            status, residual = "explained", "copilot_seat_proration"
        else:
            status, residual = "unexplained", None
        if residual is not None:
            st.gaps[_COPILOT].append(_Gap(residual, gap, month=month))
        elif status == "unexplained":
            st.gaps[_COPILOT].append(_Gap(None, 0, abs(gap), month=month))
        st.rows.append(_row(key, status=status, residual=residual, priced_nano=net,
                            invoice_nano=expected, error_pct=_pct(net - expected, expected)))


def _revisions(inp: _In, st: _Ctx) -> dict[str, int]:
    """Latest coverage aggregate per day vs Σ current report rows of that day; returns the stale
    net per month (a budget of the summary explanation)."""
    latest: dict[str, UsageAggregate] = {}
    for agg in inp.coverage:
        dims = dict(agg.dims)
        if dims.get("channel", _COPILOT) != _COPILOT or agg.reported_cost_nano is None:
            continue
        day = _date_of_ms(agg.bucket_start_ms)
        cur = latest.get(day)
        if cur is None or (agg.fetched_ms, dict(agg.dims).get("source", "")) > \
                (cur.fetched_ms, dict(cur.dims).get("source", "")):
            latest[day] = agg
    per_day: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for c in inp.lines:
        if c.source_kind == REPORT_KIND and c.channel == _COPILOT:
            a = per_day[c.date_utc]
            a[0] += c.amount_nano
            a[1] += c.list_amount_nano if c.list_amount_nano is not None else c.amount_nano
            a[2] += 1
    stale: dict[str, int] = defaultdict(int)
    out: list[tuple[str, int, int, int, int, int]] = []
    for day, agg in sorted(latest.items()):
        if st.closed_only and not st.month_closed(day[:7]):
            continue
        net, gross, n = per_day.get(day, [0, 0, 0])
        ref_net = agg.reported_cost_nano or 0
        ref_gross = agg.list_cost_nano if agg.list_cost_nano is not None else gross
        out.append((day, net, gross, n, ref_net, ref_gross))
        if net > ref_net + n or gross > ref_gross + n:
            stale[day[:7]] += max(0, net - ref_net)
    for day, net, gross, n, ref_net, ref_gross in out:
        key = (("layer", "L3"), ("check", "revision"), ("channel", _COPILOT), ("month", day[:7]),
               ("date", day))
        if abs(net - ref_net) <= n and abs(gross - ref_gross) <= n:
            st.rows.append(_row(key, status="match", priced_nano=net, invoice_nano=ref_net))
        elif net >= ref_net and gross >= ref_gross:
            st.gaps[_COPILOT].append(_Gap("copilot_revision_stale_rows", ref_net - net,
                                          total=False, month=day[:7]))
            st.rows.append(_row(key, status="explained", residual="copilot_revision_stale_rows",
                                priced_nano=net, invoice_nano=ref_net))
        else:
            st.gaps[_COPILOT].append(_Gap(None, 0, abs(ref_net - net), month=day[:7]))
            st.rows.append(_row(key, status="under", priced_nano=net, invoice_nano=ref_net))
    return dict(stale)


# ---------------------------------------------------------------------------------------------
# plan fit (diagnostic, R17)
# ---------------------------------------------------------------------------------------------


def _plan_fit(pms: Sequence[PoolMonth], cells: Sequence[_pool.Cell], capped: Mapping[str, Decimal],
              tol: Decimal, st: _Ctx) -> dict[tuple[str, str], str]:
    by_em: dict[tuple[str, str], dict[str | None, PoolMonth]] = defaultdict(dict)
    for pm in pms:
        by_em[(pm.entity_id, pm.month)][pm.plan_scenario] = pm
    cells_em: dict[tuple[str, str], list[_pool.Cell]] = defaultdict(list)
    for c in cells:
        cells_em[(c.entity_id, c.month)].append(c)
    out: dict[tuple[str, str], str] = {}
    for (entity, month), group in sorted(by_em.items()):
        biz, ent = group.get("business"), group.get("enterprise")
        if biz is None or ent is None or entity.startswith("cc:"):
            continue
        fit = "unknown"
        pooled = [c for c in cells_em.get((entity, month), []) if c.cost_type in
                  _pool.POOLED_COST_TYPES]
        final = biz.finality == "closed" and all(c.final for c in pooled)
        # a seat count that is only a lower bound (report users) makes every pool a lower bound
        if final and pooled and biz.seats_source != "report_users":
            discount = _pool.classify_discounts(pooled, gross_is_list=True, pool_nano=_BIG)[0] or 0
            use = biz.consumed_report_nano
            net = sum(c.net_nano for c in pooled)
            margin = EXACT_CTX.divide(tol, _HUNDRED)
            above = EXACT_CTX.multiply(Decimal(biz.pool_nano), EXACT_CTX.add(Decimal(1), margin))
            reached = EXACT_CTX.multiply(Decimal(biz.pool_nano),
                                         EXACT_CTX.subtract(Decimal(1), margin))
            if Decimal(discount) > above or (Decimal(use) > above and net == 0):
                fit = "enterprise"
            elif net > 0 and reached <= Decimal(use) and use < ent.pool_nano and not capped:
                fit = "business"
        out[(entity, month)] = fit
        if fit != "unknown":
            st.gaps[_COPILOT].append(_Gap("copilot_plan_inferred", 0, month=month))
        st.rows.append(_row((("layer", "L3"), ("check", "plan_fit"), ("channel", _COPILOT),
                             ("month", month), ("entity", entity), ("plan_fit", fit)),
                            status="explained" if fit != "unknown" else "match",
                            residual="copilot_plan_inferred" if fit != "unknown" else None,
                            invoice_nano=0))
    return out


# ---------------------------------------------------------------------------------------------
# the reconciler
# ---------------------------------------------------------------------------------------------


def _rounding_nano(remainders: Mapping[str, Decimal] | None) -> int | None:
    if not remainders:
        return None
    total = Decimal(0)
    found = False
    for key in _ROUNDING_KEYS:
        value = remainders.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
            raise UsageError("rounding_remainders: values must be Decimal USD")
        total = EXACT_CTX.add(total, Decimal(value))
        found = True
    return decimal_to_nano(total) if found else None


def _verdicts(inp: _In, st: _Ctx, unexpl_pct: Decimal, gross_by_month: Mapping[str, int],
              summary_channels: set[str]) -> tuple[ChannelVerdict, ...]:
    """The three channel verdicts (module docstring)."""
    has = {ch: False for ch in CHANNELS}
    for c in inp.lines:
        product = _line_product(c)
        if product is not None:
            has[_PRODUCT_CHANNEL[product]] = True
    out: list[ChannelVerdict] = []
    for ch in CHANNELS:
        if not has[ch] or (ch != _COPILOT and ch not in summary_channels):
            verdict = "insufficient_data"
        else:
            failed = st.fails.get(ch, 0) > 0
            if ch == _COPILOT and not failed:
                per_month: dict[str, int] = defaultdict(int)
                for g in st.gaps[_COPILOT]:
                    if g.unexplained and g.gate and g.month is not None and \
                            st.month_closed(g.month):
                        per_month[g.month] += g.unexplained
                for month, left in per_month.items():
                    base = gross_by_month.get(month, 0)
                    if Decimal(left) * _HUNDRED > EXACT_CTX.multiply(unexpl_pct, Decimal(base)):
                        failed = True
            verdict = "not_reconciled" if failed else "reconciled"
        out.append(ChannelVerdict(channel=ch, verdict=verdict, invoice_sources=INVOICE_SOURCES[ch],
                                  mapping_verified=SCHEMA_VERIFIED))
    return tuple(out)


def _overall(channels: Sequence[ChannelVerdict], ledger_seen: bool) -> str:
    """SPEC §12.4 over the three channels: ``insufficient_data`` when none has data; a channel
    ``not_reconciled`` (or ``github_copilot`` without report data while the ledger holds Copilot
    inferences) fails the whole; else ``reconciled``."""
    verdicts = {v.channel: v.verdict for v in channels}
    if all(v == "insufficient_data" for v in verdicts.values()):
        return "insufficient_data"
    if any(v == "not_reconciled" for v in verdicts.values()):
        return "not_reconciled"
    if ledger_seen and verdicts[_COPILOT] == "insufficient_data":
        return "not_reconciled"
    return "reconciled"


def reconcile_copilot(ledger: LedgerStore, record_stores: Sequence[ExtRecordStore],
                      pricer: Pricer, *, since_ms: int, until_ms: int,
                      tolerance_pct: str | Decimal = "0.5", unexplained_pct: str | Decimal = "1.0",
                      closed_only: bool = False, today: str,
                      rounding_remainders: Mapping[str, Decimal] | None = None
                      ) -> ReconciliationReport:
    """Reconcile the Copilot channels over ``[since_ms, until_ms)`` (addendum §12; the
    ``ChannelReconciler`` of ``ExtensionSpec.reconciler``). See the module docstring for the
    layers, residuals, decisions and verdicts. *rounding_remainders* are keyed by adapter name
    (R-E44; ``github-ai-usage``) in USD. Raises ``UsageError`` for malformed arguments."""
    for name, value in (("since_ms", since_ms), ("until_ms", until_ms)):
        if type(value) is not int or value < 0:
            raise UsageError(f"{name}: expected a non-negative int")
    if until_ms <= since_ms:
        raise UsageError("until_ms must be after since_ms")
    if not isinstance(today, str) or not _DATE_RE.match(today):
        raise UsageError("today: expected a YYYY-MM-DD string")
    try:
        _dt.date.fromisoformat(today)
    except ValueError:
        raise UsageError("today: not a calendar date") from None
    tol = _parse_pct(tolerance_pct, "tolerance_pct")
    unexpl = _parse_pct(unexplained_pct, "unexplained_pct")
    if type(closed_only) is not bool:
        raise UsageError("closed_only: expected a bool")
    rounding = _rounding_nano(rounding_remainders)
    inp = _read_inputs(ledger, record_stores, since_ms, until_ms)
    st = _Ctx(tol=tol, today=today, closed_only=closed_only,
              lag=_facts.copilot_report_lag_days(), dates=_facts.copilot_dates())
    mode = "enterprise"
    bounds = k_dated_boundaries()
    sources = report_sources(inp.coverage)
    cells_excl, _ = _pool.build_cells(inp.report_aggs, inp.lines, grain="day", convention="excl",
                                      capped=inp.capped, entity_mode=mode)
    cells_incl, _ = _pool.build_cells(inp.report_aggs, inp.lines, grain="day", convention="incl",
                                      capped=inp.capped, entity_mode=mode)

    def entity_of(cost_center: str | None, org: str | None) -> str:
        return _pool.entity_of(cost_center, org, capped=inp.capped, entity_mode=mode)

    md_lines: dict[tuple[str, str, str], int] = defaultdict(int)
    row_lines: dict[tuple[str, str, str, str, str, str], int] = defaultdict(int)
    for c in inp.lines:
        if c.source_kind != REPORT_KIND or c.cost_type not in _AI_TYPES or c.pseudo is not None:
            continue
        md_lines[(c.date_utc, c.model or "", c.workspace_id or "")] += 1
        row_lines[(c.date_utc, entity_of(c.cost_center, c.workspace_id), c.workspace_id or "",
                   c.model or "", c.sku or "", c.routing or "unknown")] += 1
    pcells = _price_cells(cells_excl, cells_incl, _Prices(pricer), sources, inp.compliance)
    decisions = _decide(pcells, md_lines, tol, bounds)
    for c in cells_excl:        # sources that only hold pseudo / legacy cells are decided too
        date = c.date_utc or f"{c.month}-01"
        sid = sources.get(date, FALLBACK_SOURCE_ID)
        if sid not in decisions and c.cost_type in (*_AI_TYPES, "ai_credit.legacy_pru"):
            decisions[sid] = _Decision(sid, "undecidable", "excl", False)
    pseudo_cells = [c for c in cells_excl
                    if (c.pseudo is not None or not c.model) and c.cost_type in _AI_TYPES]
    gross_is_list, errors = _l1(pcells, decisions, row_lines, st, bounds, pseudo_cells)
    # L0 + L2 (client data)
    ledger_tokens, utility, ledger_nano, ledger_seen = _l0_l2(ledger, pricer, since_ms, until_ms,
                                                              st)
    over = 0
    token_cov = dollar_cov = None
    if ledger_seen:
        covered = {_date_of_ms(a.bucket_start_ms) for a in inp.coverage}
        over, led_total, rep_total = _l2(pcells, decisions, ledger_tokens, utility, covered, st)
        token_cov = _pct(led_total, rep_total)
        report_gross = sum(pc.cell.gross_nano for pc in pcells)
        dollar_cov = _pct(ledger_nano, report_gross)
    # L3
    ai_cells = [c for c in cells_excl if c.cost_type in _AI_TYPES]
    ems = sorted({(c.entity_id, c.month) for c in ai_cells})
    decision_pairs = tuple(
        (f"gross_is_list:{e}:{m}", gross_is_list.get((e, m), "unknown")) for e, m in ems)
    pms = _pool.pool_months(cells_excl, inp.lines, inp.licenses, inp.config, today=today,
                            gross_is_list=decision_pairs, entity_mode=mode)

    def entity_of_line(line: CostLine) -> str:
        return entity_of(line.cost_center, line.workspace_id)

    _identity(inp.lines, entity_of_line, st)
    _discounts(pms, ai_cells, st)
    stale = _revisions(inp, st)
    ai_summary_months = _summaries(inp, stale, abs(rounding or 0), st)
    for g in st.gaps[_COPILOT]:
        if g.code == "copilot_revision_stale_rows" and g.month not in ai_summary_months:
            g.total = True
    summary_channels = {_PRODUCT_CHANNEL[p] for c in inp.lines if c.cost_type == _SUMMARY
                        for p in (_summary_product(c),) if p is not None}
    _actions_rates(inp, st)
    _caps(pms, inp, ai_cells, st)
    _seats(inp, entity_of, st)
    plan_fit = _plan_fit(pms, ai_cells, inp.capped, tol, st)
    if rounding is not None and rounding_remainders:
        st.gaps[_COPILOT].append(_Gap("copilot_rounding", rounding, gate=False))
    # verdicts
    gross_by_month: dict[str, int] = defaultdict(int)
    for c in inp.lines:
        if _line_product(c) in ("ai_credits", "seats"):
            gross_by_month[c.date_utc[:7]] += (c.list_amount_nano if c.list_amount_nano is not None
                                               else c.amount_nano)
    channels = _verdicts(inp, st, unexpl, gross_by_month, summary_channels)
    verdict = _overall(channels, ledger_seen)
    # residual totals (signed), unexplained (absolute)
    order = {code: i for i, code in enumerate((*RESIDUAL_ORDER, *L0_CODES))}
    totals: dict[str, int] = defaultdict(int)
    present: set[str] = set()
    unexplained = 0
    for gaps in st.gaps.values():
        for g in gaps:
            unexplained += g.unexplained
            if g.code is not None and g.total:
                totals[g.code] += g.nano
                present.add(g.code)
    residuals = tuple((code, totals[code]) for code in sorted(present, key=lambda k: order[k]))
    # decisions
    pairs: list[tuple[str, str]] = []
    for sid, dec in sorted(decisions.items()):
        pairs.append((f"convention:{sid}", dec.convention))
    pairs.extend(decision_pairs)
    for (entity, month), fit in sorted(plan_fit.items()):
        pairs.append((f"plan_fit:{entity}:{month}", fit))
    # finality
    months = {c.date_utc[:7] for c in inp.lines}
    if not inp.lines:
        finality = Finality.NA
    elif all(c.finality == "final" for c in inp.lines) and all(st.month_closed(m) for m in months):
        finality = Finality.FINAL
    else:
        finality = Finality.PROVISIONAL
    # effective discount per model (1 − net/gross)
    eff: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for c in ai_cells:
        label = c.model or f"pseudo:{c.pseudo or 'unknown'}"
        e = eff[f"{_COPILOT}:{label}:credits"]
        e[0] += c.gross_nano
        e[1] += c.net_nano
    effective = tuple((k, _share(Decimal(1) - ratio(n, g)))  # type: ignore[operator]
                      for k, (g, n) in sorted(eff.items()) if g > 0)
    rce = None
    if errors:
        rce = tuple(format(v.quantize(_PCT_Q, context=_PCT_CTX), "f") for v in (
            _nearest_rank(errors, 1, 2), _nearest_rank(errors, 19, 20), max(errors)))
    return ReconciliationReport(
        window=(_date_of_ms(since_ms), _date_of_ms(until_ms)), tolerance_pct=format(tol, "f"),
        unexplained_tolerance_pct=format(unexpl, "f"),
        rows=tuple(sorted(st.rows, key=lambda r: (r.key, r.status, r.residual_code or ""))),
        token_coverage_pct=token_cov, dollar_coverage_pct=dollar_cov,
        rate_card_error=rce, over_count_rows=over, effective_discount=effective,
        residuals=residuals, unexplained_nano=unexplained, channels=channels, verdict=verdict,
        finality=finality, suggested_contract=None, rerun_verdict=None,
        decisions=tuple(sorted(pairs)))


# ---------------------------------------------------------------------------------------------
# rendering helpers (CP-OUT / CP-WIRE / CLI-LEDGER call these; RECON's renderer prints verdicts)
# ---------------------------------------------------------------------------------------------


def verdict_label(report: ReconciliationReport, channel: str) -> str:
    """The printed verdict of *channel*: e.g. ``reconciled (synthetic; schema unverified)``,
    ``reconciled (totals only; synthetic; schema unverified)`` when an AI usage report file's
    convention is undecidable (addendum §12, §12.1)."""
    for v in report.channels:
        if v.channel != channel:
            continue
        if v.verdict == "insufficient_data":
            return v.verdict
        quals: list[str] = []
        if channel == _COPILOT and any(k.startswith("convention:") and val == "undecidable"
                                       for k, val in report.decisions):
            quals.append("totals only")
        if not v.mapping_verified:
            quals.extend(("synthetic", "schema unverified"))
        return f"{v.verdict} ({'; '.join(quals)})" if quals else v.verdict
    raise UsageError(f"no verdict for channel {channel!r}")


def report_notes(report: ReconciliationReport) -> tuple[DataQualityNote, ...]:
    """The data-quality notes a renderer prints beside the verdicts: ``dq.recon_schema_unverified``
    (channels with data and ``mapping_verified=False``) and ``dq.copilot_convention_undecidable``
    (count of undecidable report files)."""
    notes: list[DataQualityNote] = []
    unverified = [v.channel for v in report.channels
                  if not v.mapping_verified and v.verdict != "insufficient_data"]
    if unverified:
        detail = "synthetic; schema unverified: " + ", ".join(unverified)
        notes.append(DataQualityNote(code=DQ_RECON_SCHEMA_UNVERIFIED, severity="warn",
                                     count=len(unverified), detail=detail))
    undecidable = sum(1 for k, v in report.decisions
                      if k.startswith("convention:") and v == "undecidable")
    if undecidable:
        notes.append(DataQualityNote(code=DQ_CONVENTION_UNDECIDABLE, severity="warn",
                                     count=undecidable,
                                     detail="report token convention undecidable: totals only"))
    return tuple(notes)

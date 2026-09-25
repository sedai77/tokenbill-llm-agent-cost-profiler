"""FOCUS 1.4 CSV export (SPEC §14.4, D19, D26; package OUT; Copilot addendum §14.3, A-8).

:func:`write_focus` writes one row per (charge period = UTC day, provider, channel, model, token
bucket, basis, allocation group, lane kind, workload class, billing path, workspace, agent product,
rate row, allocation method) from the store's ``LedgerCostRow`` s. FOCUS column names and their
feature levels come from ``core.facts`` (``focus_columns``, FOCUS 1.4 ratified 2026-06-04: the
removed ``ProviderName`` / ``PublisherName`` are replaced by ``ServiceProviderName`` and
``HostProviderName``); the custom columns of :data:`X_COLUMNS` all match
``^x_[A-Z][A-Za-z0-9]{1,48}$``. Money is written as exact decimal strings from int nano (no float).

Honesty rules:

* **BilledCost per channel (D19).** Every channel with billed-basis rows must be in
  ``reconciled_channels``; otherwise the export is refused with ``GateFailed`` (CLI exit 3) naming
  the channels — unless ``allow_unreconciled`` (then ``BilledCost`` is filled from ContractedCost
  or ListCost and ``x_Reconciled = false``) or ``channels`` restricts the export to other channels.
  Channels whose rows are all list-equivalent carry no BilledCost and never block the export.
* **Only billed-eligible amounts in billed columns (R3).** BilledCost / EffectiveCost /
  ContractedCost are built from ``Figure`` s checked with ``Figure.is_billed_eligible``; estimated
  range amounts appear only in ``x_EstimatedCostLow`` / ``x_EstimatedCostHigh``.
* **Seat allowance and Copilot pool usage (D26, R10)**: ``x_PriceBasis = list_equivalent``,
  ``ListCost`` = the list-equivalent amount, ``BilledCost = EffectiveCost = ContractedCost = 0``.
* **Role enrichment**: ``role="enrichment"`` zeroes BilledCost and EffectiveCost (ledger amounts
  stay in ListCost / ContractedCost / ``x_`` columns) for organizations that also load the
  provider's own FOCUS feed.
* **Chargeback**: ``chargeback=True`` refuses (``GateFailed``) below 95% allocation coverage.
* **k-anonymity**: within each charge identity (every grain column except the allocation group
  team / cost center / project), allocation groups below *k* users are merged through
  ``core.kanon.publish`` (with complementary suppression) into one ``"(other: <k users)"`` row that
  carries ``x_SuppressedUsers``. When the whole identity has fewer than *k* users, ``publish``
  withholds every group; the export still emits the identity's cost as that one org-level row
  without any team (so FOCUS totals keep matching the ledger), never a per-team split.
* **Extension channels (A-8)**: ledger rows on ``owned_channels`` are dropped (the channel
  extension's own report rows, ``extra_rows``, replace them); ``extra_rows`` columns — FOCUS 1.4
  columns and ``x_`` columns — join the header union.

Additive keyword arguments beyond SPEC §14.7 (all optional): ``extra_rows`` / ``owned_channels``
(A-8), ``findings`` (fills ``x_FindingIds`` / ``x_TopWasteCause``) and ``reconciliation`` (fills
``x_ReconciliationDeltaPct``). ``contracted`` and ``recoverable_by_scope`` are keyed by
:func:`row_key`; ``recoverable_by_scope`` also accepts ``(team, lane_kind)`` and ``(team,)`` keys
(``""`` for no team).
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from functools import lru_cache
from typing import IO

from tokenbill.core.errors import ContractViolation, GateFailed, UsageError
from tokenbill.core.facts import FocusSpec, load
from tokenbill.core.kanon import other_label, publish
from tokenbill.core.labels import Basis, Figure, exact
from tokenbill.core.money import MTOK, nano_to_usd_str, ratio
from tokenbill.core.records import MAX_TOKENS, UsageBuckets
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import (
    AggRow,
    FocusRow,
    LedgerCostRow,
    PricedTotal,
    RawAggregate,
    ReconciliationReport,
)
from tokenbill.outputs.result_json import require_billed

__all__ = [
    "CHARGEBACK_MIN_COVERAGE",
    "FOCUS_COLUMNS",
    "ROLES",
    "X_COLUMNS",
    "focus_columns",
    "row_key",
    "write_focus",
]

ROLES = ("primary", "enrichment")
#: Allocation coverage below which ``chargeback=True`` is refused (SPEC §14.4).
CHARGEBACK_MIN_COVERAGE = Decimal("0.95")
#: The FOCUS 1.4 columns this exporter fills (names checked against ``core.facts``; every Mandatory
#: column plus the Conditional / Recommended ones SPEC §14.4 lists).
FOCUS_COLUMNS = (
    "BillingAccountId", "BillingAccountName", "BillingCurrency", "BillingPeriodStart",
    "BillingPeriodEnd", "ChargePeriodStart", "ChargePeriodEnd", "ChargeCategory", "ChargeClass",
    "ChargeDescription", "ChargeFrequency", "ServiceCategory", "ServiceName",
    "ServiceProviderName", "HostProviderName", "InvoiceIssuerName", "RegionId", "ResourceId",
    "SkuId", "PricingCategory", "PricingQuantity", "PricingUnit", "ConsumedQuantity",
    "ConsumedUnit", "ListUnitPrice", "ListCost", "ContractedUnitPrice", "ContractedCost",
    "EffectiveCost", "BilledCost", "Tags", "AllocatedMethodId", "AllocatedMethodDetails",
)
#: Custom columns (SPEC §14.4 plus the estimated-range pair of the "ESTIMATED lines only in x_
#: columns" rule).
X_COLUMNS = (
    "x_Source", "x_Role", "x_Channel", "x_BillingPath", "x_TokenBucket", "x_CacheTtl",
    "x_LaneKind", "x_WorkloadClass", "x_ModelId", "x_Evidence", "x_PriceBasis", "x_Reconciled",
    "x_ReconciliationDeltaPct", "x_RateCardSha256", "x_RecoverableCost", "x_EstimatedCostLow",
    "x_EstimatedCostHigh", "x_TopWasteCause", "x_FindingIds", "x_SuppressedUsers",
)
_NUMERIC = frozenset({"PricingQuantity", "ConsumedQuantity", "ListUnitPrice", "ListCost",
                      "ContractedUnitPrice", "ContractedCost", "EffectiveCost", "BilledCost",
                      "x_ReconciliationDeltaPct", "x_RecoverableCost", "x_EstimatedCostLow",
                      "x_EstimatedCostHigh", "x_SuppressedUsers"})
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_TTL = {"cache_write_5m": "5m", "cache_write_1h": "1h", "cache_write_other": "other",
        "cache_write_unknown": "unknown"}
_REQUEST_BUCKETS = frozenset({"web_search", "web_fetch", "web_search_requests",
                              "web_fetch_requests"})
#: Channel → (ServiceName, ServiceProviderName, HostProviderName, InvoiceIssuerName). Display
#: names, not prices (README "unverified" lists them).
_CHANNELS: Mapping[str, tuple[str, str, str, str]] = {
    "anthropic_api": ("Claude API", "Anthropic", "Anthropic", "Anthropic"),
    "claude_platform_aws": ("Claude Platform on AWS", "Anthropic", "AWS", "AWS"),
    "foundry": ("Microsoft Foundry", "Anthropic", "Microsoft", "Microsoft"),
    "bedrock": ("Amazon Bedrock", "Anthropic", "AWS", "AWS"),
    "vertex": ("Vertex AI", "Anthropic", "Google Cloud", "Google"),
    "openai_api": ("OpenAI API", "OpenAI", "OpenAI", "OpenAI"),
    "azure_openai": ("Azure OpenAI", "OpenAI", "Microsoft", "Microsoft"),
    "github_copilot": ("GitHub Copilot", "GitHub", "GitHub", "GitHub"),
}
_EPOCH = _dt.date(1970, 1, 1)
_MAX_IDS = 10
_UNIT_PLACES = Decimal("1e-12")


@lru_cache(maxsize=1)
def _focus_spec() -> FocusSpec:
    return load().focus


@lru_cache(maxsize=1)
def _spec_columns() -> frozenset[str]:
    return frozenset(c.name for c in _focus_spec().columns)


def focus_columns() -> tuple[str, ...]:
    """The FOCUS 1.4 columns of :data:`FOCUS_COLUMNS`, checked against ``core.facts`` (a name that
    FOCUS 1.4 does not define raises ``ContractViolation``)."""
    known = _spec_columns()
    missing = [c for c in FOCUS_COLUMNS if c not in known]
    if missing:
        raise ContractViolation(f"FOCUS 1.4 has no column(s) {missing}")
    return FOCUS_COLUMNS


def row_key(row: LedgerCostRow) -> tuple[str, ...]:
    """The key of *row* in ``contracted`` / ``recoverable_by_scope``: every dimension of the row
    (``""`` for None), without basis, amounts and user count."""
    return (row.date_utc, row.provider, row.channel, row.model, row.bucket, row.team or "",
            row.cost_center or "", row.project or "", row.workspace_id or "", row.lane_kind,
            row.workload_class, row.agent_product or "", row.billing_path)


# ---------------------------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------------------------


def _dec(value: Decimal) -> str:
    if not value:
        return "0"
    text = format(value.normalize(), "f")
    return text


def _money(nano: int) -> str:
    return nano_to_usd_str(nano)


def _unit_price(nano: int, quantity: int, per_request: bool) -> str:
    if quantity <= 0:
        return ""
    den = quantity * (10**9 if per_request else 10**9 // MTOK)
    value = ratio(nano, den)
    assert value is not None
    return _dec(value.quantize(_UNIT_PLACES, rounding=ROUND_HALF_EVEN))


def _iso(day: _dt.date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def _day(date_utc: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(date_utc)
    except (TypeError, ValueError):
        raise UsageError("FOCUS rows need a YYYY-MM-DD charge date (group cost rows by date)") \
            from None


def _month_bounds(day: _dt.date) -> tuple[_dt.date, _dt.date]:
    start = day.replace(day=1)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(
        month=start.month + 1)
    return start, end


def _cell(column: str, value: str) -> str:
    text = sanitize(value)
    if column not in _NUMERIC and text.startswith(_FORMULA_START):
        return "'" + text   # spreadsheet formula-injection guard for free-text cells
    return text


# ---------------------------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------------------------


@dataclass
class _Cell:
    """One export row before k-merging: the summed ledger amounts of an identity × allocation."""

    identity: tuple
    team: str | None
    cost_center: str | None
    project: str | None
    row: LedgerCostRow              # a representative (dimension values)
    method_id: str | None
    method_details: str | None
    quantity: int = 0
    priced: int = 0
    low: int = 0
    high: int = 0
    n_users: int = 0
    contracted: int | None = None
    recoverable: int | None = None
    suppressed_users: int = 0
    keys: tuple[tuple[str, ...], ...] = ()


def _identity(row: LedgerCostRow, method_id: str | None, method_details: str | None) -> tuple:
    return (row.date_utc, row.provider, row.channel, row.model, row.bucket, row.basis.value,
            row.lane_kind, row.workload_class, row.billing_path, row.workspace_id or "",
            row.agent_product or "", row.rate_row_id or "", method_id or "", method_details or "")


def _alloc_key(team: str | None, cost_center: str | None, project: str | None) -> str:
    return json.dumps([team, cost_center, project], ensure_ascii=False)


def _recoverable(row: LedgerCostRow, table: Mapping[tuple[str, ...], int] | None) -> int | None:
    if not table:
        return None
    for key in (row_key(row), (row.team or "", row.lane_kind), (row.team or "",)):
        if key in table:
            value = table[key]
            if type(value) is not int:
                raise ContractViolation("recoverable_by_scope values must be int nano")
            return value
    return None


def _add_opt(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _cells(rows: Iterable[LedgerCostRow], contracted: Mapping[tuple, int] | None,
           recoverable: Mapping[tuple[str, ...], int] | None) -> dict[tuple, dict[str, _Cell]]:
    out: dict[tuple, dict[str, _Cell]] = {}
    for row in rows:
        if not isinstance(row, LedgerCostRow):
            raise ContractViolation("write_focus expects LedgerCostRow items")
        method_id = getattr(row, "method_id", None)
        method_details = getattr(row, "method_details", None)
        ident = _identity(row, method_id, method_details)
        akey = _alloc_key(row.team, row.cost_center, row.project)
        group = out.setdefault(ident, {})
        cell = group.get(akey)
        if cell is None:
            cell = group[akey] = _Cell(identity=ident, team=row.team, cost_center=row.cost_center,
                                       project=row.project, row=row, method_id=method_id,
                                       method_details=method_details)
        cell.quantity += row.quantity
        cell.priced += row.priced_nano
        cell.low += row.estimated_low_nano
        cell.high += row.estimated_high_nano
        cell.n_users = max(cell.n_users, row.n_users)
        key = row_key(row)
        cell.keys += (key,)
        if contracted is not None and key in contracted:
            value = contracted[key]
            if type(value) is not int:
                raise ContractViolation("contracted values must be int nano")
            cell.contracted = _add_opt(cell.contracted, value)
        cell.recoverable = _add_opt(cell.recoverable, _recoverable(row, recoverable))
    return out


def _carrier(cell: _Cell) -> AggRow:
    """An ``AggRow`` carrying the cell's user count and amounts through ``kanon.publish``."""
    return AggRow(dims=(("allocation", _alloc_key(cell.team, cell.cost_center, cell.project)),),
                  n_users=cell.n_users, n_requests=0,
                  usage=UsageBuckets(uncached_input=min(max(cell.quantity, 0), MAX_TOKENS)),
                  priced=PricedTotal(exact=exact(cell.priced, Basis.LIST), estimated=None,
                                     allowance=None, priced_inferences=0, unpriced_inferences=0,
                                     unpriced_tokens=0, coverage="1"))


def _merge(cells: list[_Cell], k: int, *, org_level: bool) -> _Cell:
    first = cells[0]

    def same(attr: str) -> str | None:
        """A dim shared by every merged group survives — unless the row is the org-level
        remainder of groups ``publish`` withheld (then it could name a group below k)."""
        value = getattr(first, attr)
        ok = not org_level and all(getattr(c, attr) == value for c in cells)
        return value if ok else None

    merged = _Cell(identity=first.identity, team=other_label(k), cost_center=same("cost_center"),
                   project=same("project"), row=first.row, method_id=first.method_id,
                   method_details=first.method_details)
    for c in cells:
        merged.quantity += c.quantity
        merged.priced += c.priced
        merged.low += c.low
        merged.high += c.high
        merged.n_users = max(merged.n_users, c.n_users)
        merged.contracted = _add_opt(merged.contracted, c.contracted)
        merged.recoverable = _add_opt(merged.recoverable, c.recoverable)
        merged.suppressed_users += c.n_users
        merged.keys += c.keys
    return merged


def _publish(group: dict[str, _Cell], k: int, window: tuple[int, int]) -> list[_Cell]:
    raw = RawAggregate(group_by=("allocation",), rows=tuple(_carrier(c) for c in group.values()),
                       window=window)
    published = publish(raw, k=k)
    label = other_label(k)
    values = [row.dims[0][1] for row in published.rows]
    shown = {v for v in values if v != label}
    keep = [c for key, c in group.items() if key in shown]
    rest = [c for key, c in group.items() if key not in shown]
    if rest:
        keep.append(_merge(rest, k, org_level=label not in values))
    return keep


# ---------------------------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------------------------


def _findings_index(findings: Sequence[object]) -> dict[str, list[tuple[int, str, str, dict]]]:
    index: dict[str, list[tuple[int, str, str, dict]]] = {}
    for f in findings:
        dims = dict(f.scope.dims)
        team = dims.get("team")
        if not team:
            continue
        fig = getattr(f, "recoverable_shapley", None) or getattr(f, "recoverable", None)
        nano = fig.nano if fig is not None and fig.nano is not None else -1
        index.setdefault(team, []).append((nano, f.finding_id, f.kind,
                                           dims))
    for items in index.values():
        items.sort(key=lambda t: (-t[0], t[1]))
    return index


def _delta_pct(rep: ReconciliationReport | None) -> dict[str, str]:
    if rep is None:
        return {}
    sums: dict[str, list[int]] = {}
    for r in rep.rows:
        ch = dict(r.key).get("channel")
        if ch is None or r.ledger_nano is None or r.invoice_nano is None:
            continue
        acc = sums.setdefault(ch, [0, 0])
        acc[0] += r.ledger_nano
        acc[1] += r.invoice_nano
    out = {}
    for ch, (ledger, invoice) in sums.items():
        value = ratio((ledger - invoice) * 100, invoice)
        if value is not None:
            out[ch] = _dec(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN))
    return out


def _billed(nano: int, basis: Basis) -> Figure:
    return require_billed(exact(nano, basis), "FOCUS billed column")


def _ledger_row(cell: _Cell, *, role: str, reconciled: bool, rate_card_sha: str,
                delta: Mapping[str, str], findings: Mapping[str, list]) -> dict[str, str]:
    row = cell.row
    day = _day(row.date_utc)
    period_start, period_end = _month_bounds(day)
    per_request = row.bucket in _REQUEST_BUCKETS
    names = _CHANNELS.get(row.channel)
    if names is None:
        provider = row.provider.title() if row.provider else "Unknown"
        names = (f"{provider} ({row.channel or 'unknown channel'})", provider, provider, provider)
    service, service_provider, host, issuer = names
    allowance = row.basis is Basis.LIST_EQUIVALENT
    if allowance:
        list_cost = cell.priced
        contracted = billed = effective = 0
    elif row.basis is Basis.CONTRACT:
        # a contract-priced ledger has no list amount; ListCost repeats the contracted amount
        list_cost = cell.priced
        contracted = _billed(cell.priced, Basis.CONTRACT).nano or 0
        billed = effective = contracted
    else:
        list_cost = cell.priced
        contracted_nano = cell.contracted if cell.contracted is not None else cell.priced
        basis = Basis.CONTRACT if cell.contracted is not None else Basis.LIST
        contracted = _billed(contracted_nano, basis).nano or 0
        billed = effective = contracted
    if role == "enrichment":
        billed = effective = 0
    tags = {k: v for k, v in (("team", cell.team), ("cost_center", cell.cost_center),
                              ("project", cell.project), ("workload_class", row.workload_class),
                              ("agent_product", row.agent_product),
                              ("billing_path", row.billing_path)) if v}
    hits = [] if cell.team is None else [
        (n, fid, kind) for n, fid, kind, dims in findings.get(cell.team, ())
        if all(dims.get(d) in (None, getattr(row, d)) for d in ("lane_kind", "model", "channel"))]
    evidence = "exact" if cell.priced or not (cell.low or cell.high) else "estimated"
    quantity = Decimal(cell.quantity) if per_request else Decimal(cell.quantity).scaleb(-6)
    out = {
        "BillingAccountId": f"{row.provider or 'unknown'}/{row.channel or 'unknown'}",
        "BillingAccountName": "",
        "BillingCurrency": "USD",
        "BillingPeriodStart": _iso(period_start),
        "BillingPeriodEnd": _iso(period_end),
        "ChargePeriodStart": _iso(day),
        "ChargePeriodEnd": _iso(day + _dt.timedelta(days=1)),
        "ChargeCategory": "Usage",
        "ChargeClass": "",
        "ChargeDescription": f"{row.model or 'unknown model'} {row.bucket} "
                             f"{'requests' if per_request else 'tokens'}",
        "ChargeFrequency": "Usage-Based",
        "ServiceCategory": _focus_spec().service_category_ai,
        "ServiceName": service,
        "ServiceProviderName": service_provider,
        "HostProviderName": host,
        "InvoiceIssuerName": issuer,
        "RegionId": "",
        "ResourceId": row.workspace_id or "",
        "SkuId": f"{row.rate_row_id}#{row.bucket}" if row.rate_row_id else "",
        "PricingCategory": "Standard",
        "PricingQuantity": _dec(quantity),
        "PricingUnit": "Requests" if per_request else "1M Tokens",
        "ConsumedQuantity": str(cell.quantity),
        "ConsumedUnit": "Requests" if per_request else "Tokens",
        "ListUnitPrice": _unit_price(list_cost, cell.quantity, per_request),
        "ListCost": _money(list_cost),
        "ContractedUnitPrice": "" if allowance else _unit_price(contracted, cell.quantity,
                                                                 per_request),
        "ContractedCost": _money(contracted),
        "EffectiveCost": _money(effective),
        "BilledCost": _money(billed),
        "Tags": json.dumps(tags, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        "AllocatedMethodId": cell.method_id or "",
        "AllocatedMethodDetails": cell.method_details or "",
        "x_Source": "tokenbill",
        "x_Role": role,
        "x_Channel": row.channel,
        "x_BillingPath": row.billing_path,
        "x_TokenBucket": row.bucket,
        "x_CacheTtl": _TTL.get(row.bucket, ""),
        "x_LaneKind": row.lane_kind,
        "x_WorkloadClass": row.workload_class,
        "x_ModelId": row.model,
        "x_Evidence": evidence,
        "x_PriceBasis": row.basis.value,
        "x_Reconciled": "true" if reconciled else "false",
        "x_ReconciliationDeltaPct": delta.get(row.channel, ""),
        "x_RateCardSha256": rate_card_sha,
        "x_RecoverableCost": "" if cell.recoverable is None else _money(cell.recoverable),
        "x_EstimatedCostLow": _money(cell.low) if (cell.low or cell.high) else "",
        "x_EstimatedCostHigh": _money(cell.high) if (cell.low or cell.high) else "",
        "x_TopWasteCause": hits[0][2] if hits else "",
        "x_FindingIds": ",".join(fid for _n, fid, _k in hits[:_MAX_IDS]),
        "x_SuppressedUsers": str(cell.suppressed_users),
    }
    return out


def _sort_key(cell: _Cell, label: str) -> tuple:
    return (cell.identity, cell.team == label, cell.team or "", cell.cost_center or "",
            cell.project or "")


def _coverage_ok(value: str | None) -> bool:
    if value is None:
        return False
    try:
        cov = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise UsageError("allocation coverage must be a decimal string") from None
    if not cov.is_finite():
        raise UsageError("allocation coverage must be a finite decimal string")
    return cov >= CHARGEBACK_MIN_COVERAGE


def write_focus(rows: Iterable[LedgerCostRow], out: IO[str], *,
                reconciled_channels: frozenset[str], allow_unreconciled: bool,
                role: str = "primary", contracted: Mapping[tuple, int] | None = None,
                recoverable_by_scope: Mapping[tuple[str, ...], int] | None = None,
                rate_card_sha: str, k: int = 5, chargeback: bool = False,
                allocation_coverage: str | None = None, channels: frozenset[str] | None = None,
                extra_rows: Iterable[FocusRow] = (), owned_channels: frozenset[str] = frozenset(),
                findings: Sequence[object] = (),
                reconciliation: ReconciliationReport | None = None) -> int:
    """Write the FOCUS 1.4 CSV of *rows* (plus the extensions' *extra_rows*) to *out*; returns the
    number of data rows written. See the module docstring for the honesty rules.

    Raises ``GateFailed`` (CLI exit 3) for unreconciled channels without ``allow_unreconciled`` and
    for ``chargeback`` below 95% allocation coverage, ``UsageError`` for a bad role, ``k`` or
    coverage string, ``ContractViolation`` for malformed rows or a non-FOCUS column in
    *extra_rows*. Nothing is written when a gate refuses.
    """
    if role not in ROLES:
        raise UsageError("FOCUS role must be primary or enrichment")
    if type(k) is not int or k < 1:
        raise UsageError("k must be an int >= 1")
    if not isinstance(rate_card_sha, str):
        raise UsageError("rate_card_sha must be a string")
    if chargeback and not _coverage_ok(allocation_coverage):
        raise GateFailed("chargeback refused: allocation coverage "
                         f"{allocation_coverage or 'unknown'} is below 95%")
    columns = list(focus_columns())
    kept = [r for r in rows if _keep(r, channels, owned_channels)]
    extras = [r for r in extra_rows if _keep_extra(r, channels)]
    blocked = sorted({r.channel for r in kept if r.basis is not Basis.LIST_EQUIVALENT
                      and r.channel not in reconciled_channels}
                     | {r.channel for r in extras if not r.reconciled})
    if blocked and not allow_unreconciled:
        raise GateFailed("FOCUS export refused: channel(s) not reconciled: " + ", ".join(blocked)
                         + " (reconcile them, restrict the export with --channel, or pass "
                         "--allow-unreconciled)")
    groups = _cells(kept, contracted, recoverable_by_scope)
    window = (0, 0)
    label = other_label(k)
    cells = [c for group in groups.values() for c in _publish(group, k, window)]
    cells.sort(key=lambda c: _sort_key(c, label))
    delta = _delta_pct(reconciliation)
    index = _findings_index(findings)
    x_columns = list(X_COLUMNS)
    extra_focus: set[str] = set()
    extra_x: set[str] = set()
    known = _spec_columns()
    for r in extras:
        if not isinstance(r, FocusRow):
            raise ContractViolation("extra_rows must be FocusRows")
        for name, _value in r.columns:
            if name.startswith("x_"):
                if name not in X_COLUMNS:
                    extra_x.add(name)
            elif name not in known:
                raise ContractViolation(f"extension FOCUS row has a non-FOCUS column {name!r}")
            elif name not in FOCUS_COLUMNS:
                extra_focus.add(name)
    order = {c.name: i for i, c in enumerate(_focus_spec().columns)}
    header = columns + sorted(extra_focus, key=lambda c: order[c]) + x_columns + sorted(extra_x)
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    n = 0
    for cell in cells:
        values = _ledger_row(cell, role=role, reconciled=cell.row.channel in reconciled_channels,
                             rate_card_sha=rate_card_sha, delta=delta, findings=index)
        writer.writerow([_cell(c, values.get(c, "")) for c in header])
        n += 1
    for r in extras:
        values = {"x_Source": "tokenbill", "x_Role": role, "x_Channel": r.channel,
                  "x_Reconciled": "true" if r.reconciled else "false", **dict(r.columns)}
        writer.writerow([_cell(c, values.get(c, "")) for c in header])
        n += 1
    return n


def _keep(row: object, channels: frozenset[str] | None, owned: frozenset[str]) -> bool:
    if not isinstance(row, LedgerCostRow):
        raise ContractViolation("write_focus expects LedgerCostRow items")
    if row.channel in owned:
        return False
    return channels is None or row.channel in channels


def _keep_extra(row: object, channels: frozenset[str] | None) -> bool:
    if not isinstance(row, FocusRow):
        raise ContractViolation("extra_rows must be FocusRows")
    return channels is None or row.channel in channels

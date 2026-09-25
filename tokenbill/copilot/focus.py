"""FOCUS 1.4 rows of the Copilot channels (addendum §14.3, CP-OUT).

:func:`focus_rows` is the ``ExtensionSpec.focus_rows`` hook: it turns **GitHub's own lines** —
AI usage report rows, seat SKU lines, Code Quality licence lines, Actions minutes and sandbox meters
of Copilot workloads — into ``FocusRow`` s that OUT's ``write_focus`` appends (it drops its ledger
rows on the owned channels, so collector lanes never add ``BilledCost`` beside the report rows).
Rows never depend on a plan scenario: seat rows come only from seat SKU lines; estimated seat fees
of an unknown plan are not charges and never become rows (the export notes "seat charges unknown:
plan not detected").

* **AI credits** (``x_Channel=github_copilot``): ``ChargeCategory=Usage``,
  ``ChargeFrequency=Usage-Based``, ``ListCost`` = gross, ``BilledCost`` = ``EffectiveCost`` =
  ``ContractedCost`` = net, ``x_DiscountPool`` + ``x_DiscountOther`` + ``x_DiscountUnclassified`` =
  gross − net (Auto rows whose discount is 10% of gross → other; the rest is pool-included only
  when DC22 classifies the entity-month — pass ``gross_is_list`` (CP-RECON's decisions) —
  else unclassified), ``x_CreditsQuantity``, ``PricingUnit="AI Credits"`` (**VERIFY**).
* **Seats and Code Quality licences**: FOCUS's ``Purchase`` / ``Recurring`` values are unverified
  for seat purchases (addendum §19.5 #20): the fallback ``Usage`` / ``Usage-Based`` is used with a
  note in ``x_Notes`` (:data:`SEAT_CHARGE_VERIFIED` switches to ``Purchase`` / ``Recurring``).
* **Actions** (``github_actions``) and **sandbox** (``github_sandbox``) minutes / meters.
* Every row carries ``x_Reconciled`` and ``x_PriceBasis`` (``invoice`` for final rows of a
  reconciled channel, else ``list``). Rows of a channel that is not reconciled are emitted only with
  ``allow_unreconciled`` (then ``x_Reconciled=false``); otherwise the channel is named in the
  result's ``notes`` / ``skipped_channels`` and in a log warning. ``role="enrichment"`` zeroes
  ``BilledCost`` and ``EffectiveCost``. Per charge identity (day, entity, org, channel, cost type,
  SKU, model), allocation groups (team, cost center) below *k* users are merged through
  ``core.kanon.publish`` into one ``(other: <k users)`` row carrying ``x_SuppressedUsers``; an
  identity below *k* altogether becomes one org-level row without a team, so totals stay equal to
  GitHub's lines.

Cross-check sources (metered ``metered.ai_credit``, REST ``rest.*``) and unmapped metered rows
never become FOCUS rows (they duplicate report rows). Money is exact decimal strings (no float).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from tokenbill.core import facts as _facts
from tokenbill.core import pool
from tokenbill.core.errors import UsageError
from tokenbill.core.kanon import other_label, publish
from tokenbill.core.labels import Basis, exact
from tokenbill.core.money import nano_to_usd_str
from tokenbill.core.protocols import ExtRecordStore, LedgerStore
from tokenbill.core.records import (
    COPILOT_CHANNELS,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    UsageBuckets,
)
from tokenbill.core.types import AggRow, FocusRow, PricedTotal, RawAggregate

__all__ = ["AI_SOURCE", "FocusRowList", "ROLES", "SEAT_CHARGE_VERIFIED", "UNKNOWN_SEATS_NOTE",
           "focus_rows"]

_log = logging.getLogger(__name__)

ROLES = ("primary", "enrichment")
#: FOCUS allowed values for seat purchases (``Purchase`` / ``Recurring``) are unverified (addendum
#: §19.5 #20): until verified the fallback ``Usage`` / ``Usage-Based`` is written with a note.
SEAT_CHARGE_VERIFIED = False
#: ``CostLine.source_kind`` of the AI usage report rows (every one is a FOCUS row).
AI_SOURCE = "github.ai_usage_report"
UNKNOWN_SEATS_NOTE = "seat charges unknown: plan not detected"
_SEAT_NOTE = ("seat fee: FOCUS ChargeCategory Purchase / ChargeFrequency Recurring unverified "
              "(VERIFY); fallback Usage")
_ADJACENT = frozenset({"seat", "code_quality.license", "actions", "sandbox"})
_SERVICE = {"github_copilot": "GitHub Copilot", "github_actions": "GitHub Actions",
            "github_sandbox": "GitHub Copilot cloud sandboxes"}
_UNITS = {"seat": "Seats", "code_quality.license": "Licenses", "actions": "Minutes",
          "sandbox": "Units"}
_PATHS = {"ai_credit.user": "copilot_pool", "ai_credit.direct": "copilot_direct"}
_EPOCH = _dt.date(1970, 1, 1)
_DAY_MS = 86_400_000


class FocusRowList(list):  # type: ignore[type-arg]
    """The rows (a list of ``FocusRow``) plus ``notes`` (content-free) and the
    ``skipped_channels`` that had rows but are not reconciled."""

    notes: tuple[str, ...]
    skipped_channels: tuple[str, ...]

    def __init__(self, rows: Iterable[FocusRow] = (), *, notes: Sequence[str] = (),
                 skipped_channels: Sequence[str] = ()) -> None:
        super().__init__(rows)
        self.notes = tuple(notes)
        self.skipped_channels = tuple(skipped_channels)


@dataclass
class _Acc:
    """Summed lines of one charge identity × allocation group."""

    gross: int = 0
    net: int = 0
    pool: int = 0
    other: int = 0
    unclassified: int = 0
    quantity: Decimal = Decimal(0)
    users: set[str] = field(default_factory=set)
    final: bool = True
    notes: set[str] = field(default_factory=set)

    def merge(self, o: _Acc) -> None:
        self.gross += o.gross
        self.net += o.net
        self.pool += o.pool
        self.other += o.other
        self.unclassified += o.unclassified
        self.quantity += o.quantity
        self.users |= o.users
        self.final = self.final and o.final
        self.notes |= o.notes


def _is_auto_tenth(discount: int, gross: int) -> bool:
    return gross > 0 and abs(10 * discount - gross) <= max(10, gross // 1_000_000)


def _day(ms: int) -> str:
    return (_EPOCH + _dt.timedelta(days=max(0, ms) // _DAY_MS)).isoformat()


def _iso(day: _dt.date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def _month_bounds(day: _dt.date) -> tuple[_dt.date, _dt.date]:
    start = day.replace(day=1)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return start, end


def _dec(value: str | None) -> Decimal:
    try:
        d = Decimal(value or "0")
    except ArithmeticError:
        return Decimal(0)
    return d if d.is_finite() else Decimal(0)


def _dec_str(d: Decimal) -> str:
    return "0" if not d else format(d.normalize(), "f")


def _records(record_stores: Sequence[ExtRecordStore], since_ms: int, until_ms: int
             ) -> tuple[list[LicenseSnapshot], list[ConfigSnapshot]]:
    lics: list[LicenseSnapshot] = []
    conf: list[ConfigSnapshot] = []
    for rs in record_stores:
        lics.extend(rs.licenses(since_ms=since_ms, until_ms=until_ms))
        conf.extend(rs.config())
    return lics, conf


def _classified(store: LedgerStore, lines: Sequence[CostLine], lics: Sequence[LicenseSnapshot],
                conf: Sequence[ConfigSnapshot], capped: Mapping[str, Decimal], *, since_ms: int,
                until_ms: int, gross_is_list: object) -> set[tuple[str, str]]:
    """Entity-months whose discounts DC22 classifies as pool-included in **every** scenario."""
    if gross_is_list is None:
        return set()
    aggs = store.aggregates(AI_SOURCE, since_ms=since_ms, until_ms=until_ms)
    cells, _ = pool.build_cells(aggs, lines, capped=capped)
    pms = pool.pool_months(cells, lines, lics, conf, today=_day(until_ms),
                           gross_is_list=gross_is_list)  # type: ignore[arg-type]
    by_key: dict[tuple[str, str], bool] = {}
    for pm in pms:
        key = (pm.entity_id, pm.month)
        by_key[key] = by_key.get(key, True) and pm.pool_draw_nano is not None
    return {k for k, ok in by_key.items() if ok}


def _unknown_seat_months(lines: Sequence[CostLine], lics: Sequence[LicenseSnapshot],
                         conf: Sequence[ConfigSnapshot]) -> set[tuple[str, str]]:
    months = sorted({c.date_utc[:7] for c in lines})
    out: set[tuple[str, str]] = set()
    for month in months:
        for pe in pool.detect_plans(lines, lics, conf, month=month):
            if dict(pe.seats).get("unknown", 0) > 0:
                out.add((pe.entity_id, month))
    return out


def _kind(line: CostLine) -> str | None:
    if line.source_kind == AI_SOURCE and line.channel == "github_copilot":
        return "ai"
    if line.cost_type in _ADJACENT and line.channel in COPILOT_CHANNELS:
        return line.cost_type
    return None


def focus_rows(store: LedgerStore, record_stores: Sequence[ExtRecordStore], *, since_ms: int,
               until_ms: int, reconciled_channels: frozenset[str], k: int,
               allow_unreconciled: bool, role: str,
               gross_is_list: object = None) -> FocusRowList:
    """The Copilot channels' FOCUS rows for ``[since_ms, until_ms)`` (see the module docstring).

    *gross_is_list* (additive, optional): CP-RECON's ``gross_is_list`` decision(s) as
    ``core.pool.pool_months`` accepts them; without it no discount is labelled pool-included."""
    if role not in ROLES:
        raise UsageError("FOCUS role must be primary or enrichment")
    if type(k) is not int or k < 1:
        raise UsageError("k must be an int >= 1")
    lines = [c for c in store.cost_lines(since_ms=since_ms, until_ms=until_ms)
             if _kind(c) is not None]
    lics, conf = _records(record_stores, since_ms, until_ms)
    capped = pool.capped_cost_centers(conf)
    classified = _classified(store, [c for c in lines if _kind(c) == "ai"], lics, conf, capped,
                             since_ms=since_ms, until_ms=until_ms, gross_is_list=gross_is_list)
    unknown = _unknown_seat_months(lines, lics, conf)
    notes: list[str] = []
    skipped = sorted({c.channel for c in lines if c.channel not in reconciled_channels}
                     ) if not allow_unreconciled else []
    for ch in skipped:
        msg = (f"FOCUS: channel {ch} is not reconciled; its Copilot rows are left out (reconcile "
               "it or pass --allow-unreconciled)")
        notes.append(msg)
        _log.warning(msg)
    groups: dict[tuple, dict[tuple[str | None, str | None], _Acc]] = defaultdict(dict)
    seat_months: set[tuple[str, str]] = set()
    for line in lines:
        if line.channel in skipped:
            continue
        kind = _kind(line)
        entity = pool.entity_of(line.cost_center, line.workspace_id, capped=capped)
        month = line.date_utc[:7]
        if kind == "seat":
            seat_months.add((entity, month))
        ident = (line.date_utc, entity, line.workspace_id or "", line.channel,
                 line.cost_type or "", line.sku or "", line.model or line.pseudo or "",
                 line.workload or "", kind)
        acc = groups[ident].setdefault((line.team, line.cost_center), _Acc())
        gross = line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano
        discount = gross - line.amount_nano
        acc.gross += gross
        acc.net += line.amount_nano
        acc.quantity += _dec(line.quantity)
        acc.final = acc.final and line.finality == "final"
        if line.principal is not None:
            acc.users.add(line.principal)
        if kind != "ai":
            acc.other += discount
        elif (line.routing == "auto" and line.amount_nano > 0
              and _is_auto_tenth(discount, gross)):
            acc.other += discount
        elif (entity, month) in classified:
            acc.pool += discount
        else:
            acc.unclassified += discount
        if kind == "ai" and (entity, month) in unknown:
            acc.notes.add(UNKNOWN_SEATS_NOTE)
    missing = sorted(unknown - seat_months)
    if missing:
        notes.append(f"{UNKNOWN_SEATS_NOTE} ({', '.join(f'{e} {m}' for e, m in missing)}); "
                     "estimated seat fees are not FOCUS rows")
    rows: list[FocusRow] = []
    for ident in sorted(groups):
        for (team, cc), acc, suppressed in _publish(groups[ident], k):
            rows.append(_row(ident, team, cc, acc, suppressed, role=role,
                             reconciled=ident[3] in reconciled_channels))
    return FocusRowList(rows, notes=notes, skipped_channels=skipped)


def _publish(group: Mapping[tuple[str | None, str | None], _Acc], k: int
             ) -> list[tuple[tuple[str | None, str | None], _Acc, int]]:
    """Allocation groups of one identity after k-merging (``core.kanon.publish``)."""
    keys = sorted(group, key=lambda t: (t[0] or "", t[1] or ""))
    rows = tuple(AggRow(dims=(("allocation", json.dumps(list(key))),),
                        n_users=len(group[key].users), n_requests=0, usage=UsageBuckets(),
                        priced=PricedTotal(exact=exact(group[key].net, Basis.LIST),
                                           estimated=None, allowance=None, priced_inferences=0,
                                           unpriced_inferences=0, unpriced_tokens=0,
                                           coverage="1"))
                 for key in keys)
    pub = publish(RawAggregate(group_by=("allocation",), rows=rows, window=(0, 0)), k=k)
    label = other_label(k)
    shown = {row.dims[0][1] for row in pub.rows}
    out: list[tuple[tuple[str | None, str | None], _Acc, int]] = []
    rest = _Acc()
    rest_users = 0
    merged = False
    for key in keys:
        if json.dumps(list(key)) in shown:
            out.append((key, group[key], 0))
        else:
            rest.merge(group[key])
            rest_users += len(group[key].users)
            merged = True
    if merged:
        merged_key = (label, None) if label in shown else (None, None)
        out.append((merged_key, rest, rest_users))
    return out


def _row(ident: tuple, team: str | None, cc: str | None, acc: _Acc, suppressed: int, *,
         role: str, reconciled: bool) -> FocusRow:
    date_utc, entity, org, channel, cost_type, sku, model, workload, kind = ident
    day = _dt.date.fromisoformat(date_utc)
    start, end = _month_bounds(day)
    notes = sorted(acc.notes)
    if kind == "ai":
        category, frequency = "Usage", "Usage-Based"
        unit, unit_price = "AI Credits", "0.01"
        quantity = _dec_str(acc.quantity)
        desc = f"{sku or 'unknown sku'} {model or 'unknown model'} AI credits"
    else:
        seatlike = kind in ("seat", "code_quality.license")
        if seatlike and SEAT_CHARGE_VERIFIED:
            category, frequency = "Purchase", "Recurring"
        else:
            category, frequency = "Usage", "Usage-Based"
            if seatlike:
                notes.append(_SEAT_NOTE)
        unit, unit_price = _UNITS.get(cost_type, "Units"), ""
        quantity = _dec_str(acc.quantity)
        desc = f"{sku or cost_type} {workload}".strip()
    billed = acc.net
    if role == "enrichment":
        billed_s = effective_s = "0"
    else:
        billed_s = effective_s = nano_to_usd_str(billed)
    tags = {k: v for k, v in (("team", team), ("cost_center", cc), ("workload", workload or None),
                              ("entity", entity)) if v}
    cols = [
        ("BillingAccountId", f"github/{entity}"), ("BillingAccountName", ""),
        ("BillingCurrency", "USD"), ("BillingPeriodStart", _iso(start)),
        ("BillingPeriodEnd", _iso(end)), ("ChargePeriodStart", _iso(day)),
        ("ChargePeriodEnd", _iso(day + _dt.timedelta(days=1))),
        ("ChargeCategory", category), ("ChargeClass", ""), ("ChargeDescription", desc),
        ("ChargeFrequency", frequency),
        ("ServiceCategory", _facts.load().focus.service_category_ai),
        ("ServiceName", _SERVICE.get(channel, channel)), ("ServiceProviderName", "GitHub"),
        ("HostProviderName", "GitHub"), ("InvoiceIssuerName", "GitHub"), ("RegionId", ""),
        ("ResourceId", org), ("SkuId", sku), ("PricingCategory", "Standard"),
        ("PricingQuantity", quantity), ("PricingUnit", unit), ("ConsumedQuantity", quantity),
        ("ConsumedUnit", unit), ("ListUnitPrice", unit_price),
        ("ListCost", nano_to_usd_str(acc.gross)), ("ContractedUnitPrice", ""),
        ("ContractedCost", nano_to_usd_str(acc.net)), ("EffectiveCost", effective_s),
        ("BilledCost", billed_s),
        ("Tags", json.dumps(tags, sort_keys=True, ensure_ascii=False, separators=(",", ":"))),
        ("AllocatedMethodId", ""), ("AllocatedMethodDetails", ""),
        ("x_BillingPath", _PATHS.get(cost_type, "")), ("x_ModelId", model),
        ("x_Evidence", "exact"),
        ("x_PriceBasis", "invoice" if reconciled and acc.final else "list"),
        ("x_DiscountPool", nano_to_usd_str(acc.pool)),
        ("x_DiscountOther", nano_to_usd_str(acc.other)),
        ("x_DiscountUnclassified", nano_to_usd_str(acc.unclassified)),
        ("x_CreditsQuantity", quantity if kind == "ai" else ""),
        ("x_SuppressedUsers", str(suppressed)),
        ("x_Notes", "; ".join(notes)),
    ]
    return FocusRow(columns=tuple(cols), channel=channel, reconciled=reconciled)

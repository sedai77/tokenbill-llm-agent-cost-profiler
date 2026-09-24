"""Reconciliation, the ledger gate (SPEC §12.1–§12.4, D19, D26, D31; Copilot amendment A-5).

:func:`reconcile` proves (or disproves), **per channel**, that the ledger and the rate card match
the provider's own usage and invoice data:

1. **Rate-card check** (independent of the ledger): provider usage priced with our rate card per
   (date, workspace, model, bucket) joined to invoice lines through ``costmap.COST_TYPE_MAP`` /
   ``core.catalog.map_sku``; per closed model-day the error ``(priced − invoice) / invoice`` must be
   within ``tolerance_pct`` or ``$0.01 × invoice lines``.
2. **Token coverage**: ledger vs provider tokens per (date, workspace, model) — the workspace when
   every ledger record of the channel has one, else per (date, model); ledger > provider +
   max(1%, 1,000 tokens) is an **over-count** and fails the channel.
3. **Dollar coverage**: ledger dollars (exact + estimated points; allowance excluded) ÷ invoice.

The residual classifier (:mod:`tokenbill.recon.residuals`) explains the ``invoice − ledger`` gaps;
the unexplained part must stay within ``unexplained_pct`` per closed workspace-month. A channel
whose token lines cannot be mapped because the rules they need are unverified (every CUR/GCP rule
today, the Enterprise Analytics cost types, OpenAI line items) is reconciled in **channel-total
mode**: coverage on channel-day totals with the ledger priced at list × (1 − the channel's own
effective discount), ``mapping_verified = False`` ("totals only; schema unverified").

The ledger is **streamed** once; memory is bounded by the number of (channel, date, workspace,
model, bucket, tier) keys. ``--suggest-contract`` overlays are derived from the provider side before
the ledger pass, so the **re-run** prices each ledger record with the overlay's pricer in the same
pass. Channels owned by extensions (``core.extensions.delegated_channels()``) are skipped; their
reconcilers' reports are combined with :func:`merge_reports`, the only merge implementation.

Every amount is int nano; ratios are ``Decimal``; no floats (money module, SPEC §2.4).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from tokenbill.core import extensions
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Basis, Finality
from tokenbill.core.money import EXACT_CTX, NANO_PER_USD, RATIO_CTX, decimal_to_nano
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import CostLine, PricingContext, UsageAggregate, UsageRecord, to_json
from tokenbill.core.types import ChannelVerdict, ContractOverlay, ReconciliationReport, ReconRow
from tokenbill.recon import costmap
from tokenbill.recon.costmap import (
    ADAPTER_SOURCE_KINDS,
    GROSS_RATE_SOURCES,
    INVOICE_PRECEDENCE,
    INVOICE_USAGE_PAIRS,
    NO_REPORTING_API_CHANNELS,
    PRIORITY_EXCLUDED_SOURCES,
    PRIORITY_TIERS,
    SOURCE_CHANNELS,
    TOKEN_BUCKETS,
    USAGE_PRECEDENCE,
    LineMapping,
)
from tokenbill.recon.residuals import (
    DEFAULT_JOIN,
    INFO_DIM,
    Classification,
    classify_rows,
    key_value,
    residual_order,
)

__all__ = [
    "DEFAULT_TOLERANCE_PCT",
    "DEFAULT_UNEXPLAINED_PCT",
    "OVER_COUNT_MIN_TOKENS",
    "OVER_COUNT_PCT",
    "REVISION_WINDOW_DAYS",
    "VERDICTS",
    "merge_reports",
    "reconcile",
    "reconciled_channels",
    "suggest_contracts",
]

DEFAULT_TOLERANCE_PCT = Decimal("0.5")
DEFAULT_UNEXPLAINED_PCT = Decimal("1.0")
#: Revisable sources are final this many days after the usage date (Enterprise Analytics: ~30).
REVISION_WINDOW_DAYS = 30
#: Over-count rule: ledger > provider + max(OVER_COUNT_PCT % of provider, OVER_COUNT_MIN_TOKENS).
OVER_COUNT_PCT = Decimal("1")
OVER_COUNT_MIN_TOKENS = 1000
VERDICTS = ("reconciled", "not_reconciled", "insufficient_data")
#: The source's cent rounding: |priced − invoice| ≤ $0.01 per invoice line is within tolerance.
CENT_NANO = NANO_PER_USD // 100
_DAY_MS = 86_400_000
_EPOCH_ORDINAL = _dt.date(1970, 1, 1).toordinal()
_PCT_QUANTUM = Decimal("0.0001")
_RATIO_QUANTUM = Decimal("0.000001")
_MULT_QUANTUM = Decimal("0.0001")
_DEFAULT_WS = "(default)"
_ALL_WS = "*"
_CC_SOURCE = "anthropic.cc_analytics"
_OVERLAY_BUCKETS = (("input", "input"), ("output", "output"), ("cache_read", "cache_read"),
                    ("cache_write_5m", "cache_write_5m"), ("cache_write_1h", "cache_write_1h"))


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _date_of(ms: int) -> str:
    return _dt.date.fromordinal(_EPOCH_ORDINAL + ms // _DAY_MS).isoformat()


def _ordinal(date: str, what: str) -> int:
    if not isinstance(date, str) or len(date) != 10:
        raise UsageError(f"{what}: expected a YYYY-MM-DD date")
    try:
        return _dt.date.fromisoformat(date).toordinal()
    except ValueError:
        raise UsageError(f"{what}: expected a YYYY-MM-DD date") from None


def _pct_arg(value: object, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        raise UsageError(f"{what}: expected a decimal percentage")
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError):
        raise UsageError(f"{what}: expected a decimal percentage") from None
    if not d.is_finite() or d < 0:
        raise UsageError(f"{what}: expected a finite, non-negative percentage")
    return d


def _dec_str(d: Decimal, quantum: Decimal) -> str:
    """*d* rounded half-even to *quantum*, as a plain decimal string without trailing zeros."""
    text = format(d.quantize(quantum, rounding=ROUND_HALF_EVEN, context=RATIO_CTX), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _pct(num: int, den: int) -> str | None:
    """``100 × num / den`` as a percentage string (4 decimals), None when ``den == 0``."""
    if den == 0:
        return None
    return _dec_str(RATIO_CTX.divide(Decimal(num) * 100, Decimal(den)), _PCT_QUANTUM)


def _ratio(num: int, den: int) -> Decimal | None:
    return None if den == 0 else RATIO_CTX.divide(Decimal(num), Decimal(den))


def _scale(amount: int, num: int, den: int) -> int:
    """``amount × num / den`` rounded half-even (``den > 0``)."""
    q, r = divmod(amount * num, den)
    if 2 * r > den or (2 * r == den and q % 2 == 1):
        q += 1
    return q


def _dims(agg: UsageAggregate) -> dict[str, str]:
    return dict(agg.dims)


def _agg_channel(agg: UsageAggregate) -> str | None:
    return _dims(agg).get("channel") or SOURCE_CHANNELS.get(agg.source_kind)


def _ws(value: str | None) -> str:
    return _DEFAULT_WS if value is None else value


def _add(cell: list[int], i: int, value: int) -> None:
    cell[i] += value


# ---------------------------------------------------------------------------------------------
# per-channel state
# ---------------------------------------------------------------------------------------------


@dataclass
class _Channel:
    """Everything the reconciliation knows about one channel."""

    channel: str
    invoice_kind: str | None = None
    usage_kinds: tuple[str, ...] = ()
    total_mode: bool = False
    verified: bool = True
    lines: list[tuple[CostLine, LineMapping]] = field(default_factory=list)
    aggs: list[UsageAggregate] = field(default_factory=list)
    cc_aggs: list[UsageAggregate] = field(default_factory=list)
    remainders_nano: int = 0

    @property
    def excludes_priority(self) -> bool:
        return self.invoice_kind in PRIORITY_EXCLUDED_SOURCES


@dataclass
class _Ledger:
    """The streamed ledger of one channel under one pricer.

    Cells are keyed ``(date, workspace or None, model, bucket, tier marker)``: ``billed`` →
    ``[tokens, nano, estimated nano, list-basis nano, unpriced tokens]``; ``allowance`` → nano."""

    billed: dict[tuple, list[int]] = field(default_factory=dict)
    allowance: dict[tuple, int] = field(default_factory=dict)
    ws_missing: bool = False
    cc_tokens: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def cell(self, key: tuple) -> list[int]:
        found = self.billed.get(key)
        if found is None:
            found = self.billed[key] = [0, 0, 0, 0, 0]
        return found


@dataclass
class _Provider:
    """Provider usage of one channel priced by one pricer: ``(date, ws, model, bucket, tier)`` →
    ``[tokens, nano, unpriced flag]``."""

    cells: dict[tuple, list[int]] = field(default_factory=dict)


@dataclass
class _Invoice:
    """Invoice lines of one channel: token cells ``(date, ws, model, bucket)`` → ``[rate amount,
    list amount, lines, lines without list]``; special rows ``(date, ws, bucket, detail)`` →
    ``[amount, lines]``; channel-total ``date`` → ``[amount, list, lines, lines without list]``."""

    cells: dict[tuple, list[int]] = field(default_factory=dict)
    special: dict[tuple, list[int]] = field(default_factory=dict)
    totals: dict[str, list[int]] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------
# planning: channels, invoice sources, modes
# ---------------------------------------------------------------------------------------------


def _plan(aggregates: Sequence[UsageAggregate], cost_lines: Sequence[CostLine],
          delegated: frozenset[str], keep: Callable[[str], bool]) -> dict[str, _Channel]:
    """Group the provider records by channel, choose each channel's invoice source (the first of
    ``INVOICE_PRECEDENCE`` present, else the first unknown kind in name order) and its paired usage
    kinds, and decide channel-total mode."""
    channels: dict[str, _Channel] = {}
    by_kind: dict[str, dict[str, list[CostLine]]] = {}
    for line in cost_lines:
        if not isinstance(line, CostLine):
            raise UsageError("reconcile: cost_lines must be CostLine records")
        if line.channel in delegated or not keep(line.date_utc):
            continue
        by_kind.setdefault(line.channel, {}).setdefault(line.source_kind, []).append(line)
    for channel, kinds in by_kind.items():
        known = [k for k in INVOICE_PRECEDENCE if k in kinds]
        chosen = known[0] if known else sorted(kinds)[0]
        ch = channels[channel] = _Channel(channel, invoice_kind=chosen,
                                          usage_kinds=INVOICE_USAGE_PAIRS.get(chosen, ()))
        for line in sorted(kinds[chosen], key=lambda c: c.line_id):
            mapping = costmap.map_line(line)
            ch.lines.append((line, mapping))
            if mapping.kind == "unmappable":
                ch.total_mode = True
            if not mapping.verified:
                ch.verified = False
    usage_by_channel: dict[str, dict[str, list[UsageAggregate]]] = {}
    for agg in aggregates:
        if not isinstance(agg, UsageAggregate):
            raise UsageError("reconcile: aggregates must be UsageAggregate records")
        channel = _agg_channel(agg)
        if channel is None or channel in delegated or not keep(_date_of(agg.bucket_start_ms)):
            continue
        usage_by_channel.setdefault(channel, {}).setdefault(agg.source_kind, []).append(agg)
    for channel, kinds in usage_by_channel.items():
        ch = channels.setdefault(channel, _Channel(channel))
        if not ch.usage_kinds:
            present = [k for k in USAGE_PRECEDENCE if k in kinds]
            ch.usage_kinds = tuple(present[:1])
        for kind in ch.usage_kinds:
            ch.aggs.extend(sorted(kinds.get(kind, ()), key=lambda a: a.agg_id))
        ch.cc_aggs = sorted(kinds.get(_CC_SOURCE, ()), key=lambda a: a.agg_id)
    return channels


def _remainders(channels: Mapping[str, _Channel],
                rounding_remainders: Mapping[str, Decimal] | None) -> None:
    """Σ parse remainders (USD, keyed by adapter name, R-E44) of each channel's invoice source."""
    if not rounding_remainders:
        return
    for adapter, usd in sorted(rounding_remainders.items()):
        kind = ADAPTER_SOURCE_KINDS.get(adapter)
        if kind is None:
            continue
        if isinstance(usd, bool) or not isinstance(usd, (Decimal, int, str)):
            raise UsageError("reconcile: rounding_remainders must map adapter names to USD")
        try:
            nano = decimal_to_nano(Decimal(usd))
        except (InvalidOperation, ValueError):
            raise UsageError("reconcile: rounding_remainders must be finite decimals") from None
        for ch in channels.values():
            if ch.invoice_kind == kind:
                ch.remainders_nano += nano


# ---------------------------------------------------------------------------------------------
# provider usage, invoice lines, ledger
# ---------------------------------------------------------------------------------------------


def _provider_ctx(channel: str, dims: Mapping[str, str]) -> PricingContext:
    model = dims.get("model") or ""
    return PricingContext(
        provider=costmap.channel_provider(channel), channel=channel, model=model, model_raw=model,
        service_tier=dims.get("service_tier") or "standard", speed=dims.get("speed") or "standard",
        inference_geo=dims.get("inference_geo") or None,
        endpoint_scope=dims.get("endpoint_scope") or "unknown")


def _tier(ch: _Channel, service_tier: str | None) -> str | None:
    return service_tier if ch.excludes_priority and service_tier in PRIORITY_TIERS else None


def _price_provider(ch: _Channel, pricer: Pricer) -> _Provider:
    out = _Provider()
    for agg in ch.aggs:
        dims = _dims(agg)
        date = _date_of(agg.bucket_start_ms)
        ws = dims.get("workspace_id")
        model = dims.get("model") or ""
        tier = _tier(ch, dims.get("service_tier"))
        try:
            ctx = _provider_ctx(ch.channel, dims)
        except ContractViolation:
            ctx = None
        priced = pricer.price_usage(agg.usage, ctx, ts_ms=agg.bucket_start_ms) \
            if ctx is not None else None
        ok = priced is not None and priced.unpriced_reason is None
        if ok:
            assert priced is not None
            for line in priced.lines:
                bucket = line.bucket
                key = (date, ws, "" if bucket == "web_search" else model, bucket, tier)
                cell = out.cells.setdefault(key, [0, 0, 0])
                if bucket != "web_search":
                    _add(cell, 0, line.quantity)
                _add(cell, 1, line.amount_nano)
        else:
            usage = agg.usage
            for bucket in TOKEN_BUCKETS:
                qty = getattr(usage, bucket)
                if qty:
                    cell = out.cells.setdefault((date, ws, model, bucket, tier), [0, 0, 0])
                    _add(cell, 0, qty)
                    cell[2] = 1
            if usage.web_search_requests:
                out.cells.setdefault((date, ws, "", "web_search", tier), [0, 0, 0])[2] = 1
    return out


def _invoice(ch: _Channel) -> _Invoice:
    out = _Invoice()
    for line, mapping in ch.lines:
        date, ws = line.date_utc, line.workspace_id
        amount = line.amount_nano
        listed = line.list_amount_nano
        if line.source_kind in GROSS_RATE_SOURCES and listed is not None:
            if amount != listed:  # credits netted into amount: a cloud_credits row
                cell = out.special.setdefault((date, ws, "credits", ""), [0, 0])
                _add(cell, 0, amount - listed)
                _add(cell, 1, 1)
            amount = listed
        if mapping.kind in ("report_only", "unmapped", "credit"):
            bucket = {"report_only": mapping.bucket or "code_execution", "unmapped": "unmapped",
                      "credit": "credits"}[mapping.kind]
            detail = "" if mapping.kind != "unmapped" else (
                f"sku={line.sku}" if line.sku else f"cost_type={line.cost_type or ''}")
            cell = out.special.setdefault((date, ws, bucket, detail), [0, 0])
            _add(cell, 0, amount)
            _add(cell, 1, 1)
            continue
        if ch.total_mode:
            cell = out.totals.setdefault(date, [0, 0, 0, 0])
        else:
            key = (date, ws, mapping.model or "", mapping.bucket)
            cell = out.cells.setdefault(key, [0, 0, 0, 0])
        _add(cell, 0, amount)
        _add(cell, 1, listed if listed is not None else 0)
        _add(cell, 2, 1)
        _add(cell, 3, 0 if listed is not None else 1)
    return out


def _stream_ledger(ledger: Iterable[UsageRecord], channels: dict[str, _Channel], pricer: Pricer,
                   rerun: Mapping[str, Pricer], delegated: frozenset[str],
                   keep: Callable[[str], bool]) -> tuple[dict[str, _Ledger], dict[str, _Ledger]]:
    """One pass over the ledger: per channel, its cells under *pricer* and (for the channels of a
    suggested overlay) under the overlay's pricer."""
    main: dict[str, _Ledger] = {}
    second: dict[str, _Ledger] = {}
    for rec in ledger:
        if not isinstance(rec, UsageRecord):
            raise UsageError("reconcile: the ledger must yield UsageRecord records")
        channel = rec.pricing.channel
        if channel in delegated or rec.billable is False or not keep(rec.date_utc):
            continue
        ch = channels.setdefault(channel, _Channel(channel))
        _account(main.setdefault(channel, _Ledger()), ch, rec, pricer)
        if channel in rerun:
            _account(second.setdefault(channel, _Ledger()), ch, rec, rerun[channel])
    return main, second


def _account(side: _Ledger, ch: _Channel, rec: UsageRecord, pricer: Pricer) -> None:
    ws = rec.attribution.workspace_id
    if ws is None:
        side.ws_missing = True
    model = rec.pricing.model
    tier = _tier(ch, rec.pricing.service_tier)
    priced = pricer.price_usage(rec.usage, rec.pricing, ts_ms=rec.ts_ms, billable=rec.billable,
                                usage_source=rec.usage_source)
    date = rec.date_utc
    allowance = priced.figure.basis is Basis.LIST_EQUIVALENT
    if rec.attribution.agent_product == "claude_code" and ch.channel == "anthropic_api":
        team = rec.attribution.team or ""
        cc_key = (date, team, model)
        side.cc_tokens[cc_key] = side.cc_tokens.get(cc_key, 0) + rec.usage.total_input + (
            rec.usage.output)
    if priced.unpriced_reason is not None:
        if allowance:
            return  # unpriced allowance usage: nothing to reconcile against an invoice
        for bucket in TOKEN_BUCKETS:
            qty = getattr(rec.usage, bucket)
            if qty:
                cell = side.cell((date, ws, model, bucket, tier))
                _add(cell, 0, qty)
                _add(cell, 4, qty)
        return
    for line in priced.lines:
        bucket = line.bucket
        key = (date, ws, "" if bucket == "web_search" else model, bucket, tier)
        if allowance:
            side.allowance[key] = side.allowance.get(key, 0) + line.amount_nano
            continue
        cell = side.cell(key)
        if bucket != "web_search":
            _add(cell, 0, line.quantity)
        _add(cell, 1, line.amount_nano)
        if not line.exact:  # the most the true amount can differ from the point
            low = line.low_nano if line.low_nano is not None else line.amount_nano
            high = line.high_nano if line.high_nano is not None else line.amount_nano
            _add(cell, 2, max(line.amount_nano - low, high - line.amount_nano))
        if priced.figure.basis is Basis.LIST:
            _add(cell, 3, line.amount_nano)


# ---------------------------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------------------------


@dataclass
class _Rows:
    rows: list[ReconRow] = field(default_factory=list)
    estimated: dict[tuple, int] = field(default_factory=dict)
    allowance: dict[tuple, int] = field(default_factory=dict)
    lines: dict[tuple, int] = field(default_factory=dict)       # invoice lines per row key
    over_groups: set[tuple] = field(default_factory=set)
    total_rows: int = 0


def _row_key(channel: str, date: str, ws: str | None, model: str | None, bucket: str,
             tier: str | None = None, extra: tuple[tuple[str, str], ...] = ()
             ) -> tuple[tuple[str, str], ...]:
    key: list[tuple[str, str]] = [("channel", channel), ("date", date)]
    if ws is not None:
        key.append(("workspace", ws))
    if model:
        key.append(("model", model))
    key.append(("bucket", bucket))
    if tier:
        key.append(("service_tier", tier))
    key.extend(extra)
    return tuple(key)


def _over(ledger_tokens: int, provider_tokens: int) -> bool:
    slack = max(_scale(provider_tokens, int(OVER_COUNT_PCT * 100), 10_000),
                OVER_COUNT_MIN_TOKENS)
    return ledger_tokens > provider_tokens + slack


def _ws_mapper(ch: _Channel, led: _Ledger, prov: _Provider, inv: _Invoice
               ) -> Callable[[str | None, bool], tuple[str, bool]]:
    """``(ws, is_ledger) → (row workspace, joined through the default workspace)``."""
    if led.ws_missing:
        return lambda ws, is_ledger: (_ALL_WS, False)
    known = {k[1] for k in prov.cells} | {k[1] for k in inv.cells} | {k[1] for k in inv.special}
    has_default = None in known
    known.discard(None)

    def mapper(ws: str | None, is_ledger: bool) -> tuple[str, bool]:
        if is_ledger and ws is not None and ws not in known and has_default:
            return _DEFAULT_WS, True
        return _ws(ws), False

    return mapper


def _mapped_rows(ch: _Channel, led: _Ledger, prov: _Provider, inv: _Invoice, out: _Rows) -> None:
    """Rows of a mapped channel: one per (date, workspace, model, bucket[, priority tier])."""
    ws_of = _ws_mapper(ch, led, prov, inv)
    cells: dict[tuple, dict[str, list[int] | int]] = {}
    joined: set[tuple[str, str]] = set()

    def slot(date: str, ws: str, model: str, bucket: str, tier: str | None) -> dict:
        return cells.setdefault((date, ws, model, bucket, tier or ""), {})

    for (date, ws, model, bucket, tier), value in prov.cells.items():
        s = slot(date, ws_of(ws, False)[0], model, bucket, tier)
        cur = s.setdefault("prov", [0, 0, 0])
        for i in range(3):
            cur[i] = max(cur[i], value[i]) if i == 2 else cur[i] + value[i]
    for (date, ws, model, bucket), value in inv.cells.items():
        s = slot(date, ws_of(ws, False)[0], model, bucket, None)
        cur = s.setdefault("inv", [0, 0, 0, 0])
        for i in range(4):
            cur[i] += value[i]
    for (date, ws, model, bucket, tier), value in led.billed.items():
        row_ws, join = ws_of(ws, True)
        if join:
            joined.add((date, model))
        cur = slot(date, row_ws, model, bucket, tier).setdefault("led", [0, 0, 0, 0, 0])
        for i in range(5):
            cur[i] += value[i]
    for (date, ws, model, bucket, tier), value in led.allowance.items():
        s = slot(date, ws_of(ws, True)[0], model, bucket, tier)
        s["allow"] = int(s.get("allow", 0)) + value  # type: ignore[call-overload]
    groups: dict[tuple, list[int]] = {}
    for (date, ws, model, bucket, tier), s in sorted(cells.items()):
        extra = (DEFAULT_JOIN,) if ws == _DEFAULT_WS and (date, model) in joined else ()
        key = _row_key(ch.channel, date, ws, model, bucket, tier or None, extra)
        prov_c = s.get("prov")
        inv_c = s.get("inv")
        led_c = s.get("led")
        token = bucket != "web_search"
        ledger_tokens = led_c[0] if led_c is not None and token else None
        provider_tokens = prov_c[0] if prov_c is not None and token else None
        ledger_nano = None if led_c is None or led_c[4] else led_c[1]
        priced = None if prov_c is None or prov_c[2] else prov_c[1]
        if inv_c is not None and prov_c is None:
            priced = 0  # invoiced without provider usage: our price of the provider's usage is 0
        invoice = inv_c[0] if inv_c is not None else None
        out.rows.append(_make_row(key, ledger_tokens, provider_tokens, ledger_nano, priced,
                                  invoice))
        if led_c is not None and led_c[2]:
            out.estimated[key] = led_c[2]
        if s.get("allow"):
            out.allowance[key] = int(s["allow"])  # type: ignore[call-overload]
        if inv_c is not None:
            out.lines[key] = inv_c[2]
        if token:
            g = groups.setdefault(key_group(key), [0, 0])
            g[0] += ledger_tokens or 0
            g[1] += provider_tokens or 0
    if ch.aggs:
        out.over_groups.update(g for g, (lt, pt) in groups.items() if _over(lt, pt))


def key_group(key: tuple) -> tuple:
    """The model-day group of a row key (the key without its bucket)."""
    return tuple(pair for pair in key if pair[0] != "bucket")


def _make_row(key: tuple, ledger_tokens: int | None, provider_tokens: int | None,
              ledger_nano: int | None, priced: int | None, invoice: int | None) -> ReconRow:
    return ReconRow(
        key=key, ledger_tokens=ledger_tokens, provider_tokens=provider_tokens,
        ledger_nano=ledger_nano, priced_provider_nano=priced, invoice_nano=invoice,
        rate_card_error_pct=_pct(priced - invoice, invoice)
        if priced is not None and invoice is not None else None,
        coverage_pct=_pct(ledger_nano, invoice)
        if ledger_nano is not None and invoice is not None else None,
        status="match", residual_code=None)


def _channel_discount(ch: _Channel, prov: _Provider, inv: _Invoice) -> tuple[int, int] | None:
    """The channel's own ``(invoice, list)`` over its token lines: the lines' list amounts when
    every line has one (CUR unblended, Enterprise ``list_amount``), else our price of the
    provider's usage; None when neither is known."""
    amount = sum(c[0] for c in inv.totals.values())
    if inv.totals and all(c[3] == 0 for c in inv.totals.values()):
        listed = sum(c[1] for c in inv.totals.values())
        if listed > 0:
            return amount, listed
    if prov.cells and not any(c[2] for c in prov.cells.values()):
        priced = sum(c[1] for c in prov.cells.values())
        if priced > 0:
            return amount, priced
    return None


def _total_rows(ch: _Channel, led: _Ledger, prov: _Provider, inv: _Invoice, out: _Rows) -> None:
    """Channel-total mode (§12.1): one row per channel-day on totals; the ledger's list-basis
    dollars priced at list × (1 − the channel's own effective discount)."""
    discount = _channel_discount(ch, prov, inv)
    days: dict[str, dict[str, list[int]]] = {}
    for (date, *_rest), value in prov.cells.items():
        cur = days.setdefault(date, {}).setdefault("prov", [0, 0])
        cur[0] += value[0]
        cur[1] = max(cur[1], value[2])
    for date, value in inv.totals.items():
        days.setdefault(date, {})["inv"] = list(value)
    for (date, *_rest), value in led.billed.items():
        cur = days.setdefault(date, {}).setdefault("led", [0, 0, 0, 0, 0])
        for i in range(5):
            cur[i] += value[i]
    allow_by_day: dict[str, int] = {}
    for (date, *_rest), value in led.allowance.items():
        allow_by_day[date] = allow_by_day.get(date, 0) + value
        days.setdefault(date, {})
    for date in sorted(days):
        s = days[date]
        key = _row_key(ch.channel, date, _ALL_WS, None, "total")
        led_c, prov_c, inv_c = s.get("led"), s.get("prov"), s.get("inv")
        ledger_nano = None
        if led_c is not None and not led_c[4]:
            list_part = led_c[3]
            ledger_nano = led_c[1] - list_part + (
                _scale(list_part, discount[0], discount[1]) if discount else list_part)
        out.rows.append(_make_row(
            key, led_c[0] if led_c else None, prov_c[0] if prov_c else None, ledger_nano, None,
            inv_c[0] if inv_c else None))
        if led_c is not None and led_c[2]:
            out.estimated[key] = led_c[2]
        if allow_by_day.get(date):
            out.allowance[key] = allow_by_day[date]
        if inv_c is not None:
            out.lines[key] = inv_c[2]
        if ch.aggs and _over(led_c[0] if led_c else 0, prov_c[0] if prov_c else 0):
            out.over_groups.add(key_group(key))


def _special_rows(ch: _Channel, inv: _Invoice, out: _Rows, ws_of: Callable) -> None:
    for (date, ws, bucket, detail), (amount, n) in sorted(
            inv.special.items(), key=lambda kv: tuple("" if v is None else v for v in kv[0])):
        extra: tuple[tuple[str, str], ...] = ()
        if detail:
            name, _, value = detail.partition("=")
            extra = ((name, value),)
        key = _row_key(ch.channel, date, ws_of(ws, False)[0], None, bucket, None, extra)
        out.rows.append(_make_row(key, None, None, None, None, amount))
        out.lines[key] = n


def _cc_rows(ch: _Channel, led: _Ledger, out: _Rows) -> None:
    """Informational team-day token coverage against Claude Code Analytics (§12.4; tokens only,
    team level, never in the verdict)."""
    provider: dict[tuple[str, str, str], int] = {}
    for agg in ch.cc_aggs:
        dims = _dims(agg)
        key = (_date_of(agg.bucket_start_ms), dims.get("team") or "", dims.get("model") or "")
        provider[key] = provider.get(key, 0) + agg.usage.total_input + agg.usage.output
    for date, team, model in sorted(set(provider) | set(led.cc_tokens)):
        if (date, team, model) not in provider:
            continue
        lt = led.cc_tokens.get((date, team, model), 0)
        pt = provider[(date, team, model)]
        key: tuple[tuple[str, str], ...] = (("channel", ch.channel), ("date", date),
                                            ("team", team), ("model", model),
                                            ("bucket", "tokens"), (INFO_DIM, _CC_SOURCE))
        status = "over" if _over(lt, pt) else ("under" if lt < pt else "match")
        out.rows.append(ReconRow(key=key, ledger_tokens=lt, provider_tokens=pt, ledger_nano=None,
                                 priced_provider_nano=None, invoice_nano=None,
                                 rate_card_error_pct=None, coverage_pct=_pct(lt, pt),
                                 status=status, residual_code=None))


# ---------------------------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------------------------


@dataclass
class _Outcome:
    report_rows: list[ReconRow]
    verdicts: list[ChannelVerdict]
    residuals: dict[str, int]
    unexplained: int
    errors: list[Decimal]
    over_count: int
    discounts: dict[str, str]
    dates: set[str]


def _nearest_rank(values: Sequence[Decimal], pct: int) -> Decimal:
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))
    return ordered[rank - 1]


def _assemble(channels: Mapping[str, _Channel], ledgers: Mapping[str, _Ledger],
              providers: Mapping[str, _Provider], invoices: Mapping[str, _Invoice], *,
              tolerance: Decimal, unexplained_pct: Decimal,
              provisional: Callable[[str], bool]) -> _Outcome:
    outcome = _Outcome([], [], {}, 0, [], 0, {}, set())
    for name in sorted(channels):
        ch = channels[name]
        led = ledgers.get(name, _Ledger())
        prov = providers[name]
        inv = invoices[name]
        out = _Rows()
        ws_of = _ws_mapper(ch, led, prov, inv)
        if ch.total_mode:
            _total_rows(ch, led, prov, inv, out)
        else:
            _mapped_rows(ch, led, prov, inv, out)
        _special_rows(ch, inv, out, ws_of)
        dates = {key_value(r.key, "date") or "" for r in out.rows}
        outcome.dates.update(dates)
        prov_dates = frozenset(d for d in dates if provisional(d))
        has_invoice = any(r.invoice_nano is not None for r in out.rows)
        classify_it = has_invoice or name in NO_REPORTING_API_CHANNELS
        cls = classify_rows(out.rows, ledger_estimated_nano=out.estimated,
                            allowance_nano=out.allowance, provisional_dates=prov_dates,
                            remainders_nano=ch.remainders_nano) if classify_it else None
        if cls is not None:
            for code, nano in cls.codes.items():
                outcome.residuals[code] = outcome.residuals.get(code, 0) + nano
            outcome.unexplained += cls.unexplained
        rate_ok = _rate_card(ch, out, prov_dates, tolerance, outcome.errors)
        ws_ok = _ws_months(out, cls, prov_dates, unexplained_pct, abs(ch.remainders_nano))
        over_here = len(out.over_groups)
        outcome.over_count += over_here
        final_rows = _finish_rows(out, cls, rate_ok[1] if has_invoice else set())
        outcome.report_rows.extend(final_rows)
        if not has_invoice:
            verdict = "insufficient_data"
        elif rate_ok[0] and ws_ok and not over_here:
            verdict = "reconciled"
        else:
            verdict = "not_reconciled"
        outcome.verdicts.append(ChannelVerdict(
            channel=name, verdict=verdict,
            invoice_sources=(ch.invoice_kind,) if ch.invoice_kind else (),
            mapping_verified=ch.verified))
        outcome.discounts.update(_discounts(ch, prov, inv, out))
    _cc = channels.get("anthropic_api")
    if _cc is not None and _cc.cc_aggs:
        extra = _Rows()
        _cc_rows(_cc, ledgers.get("anthropic_api", _Ledger()), extra)
        outcome.report_rows.extend(extra.rows)
    outcome.report_rows.sort(key=lambda r: r.key)
    return outcome


def _rate_card(ch: _Channel, out: _Rows, prov_dates: frozenset[str], tolerance: Decimal,
               errors: list[Decimal]) -> tuple[bool, set[tuple[str, str]]]:
    """Layer 1 per closed model-day (web search per day): ``(all within tolerance, failing
    (date, model) keys)``. Priority rows (never invoiced), non-token rows and ledger-only rows are
    not price questions; provider usage without an invoice line, or an invoice line without
    provider usage, fails its model-day (unknown is never within tolerance)."""
    if ch.total_mode:
        return True, set()
    days: dict[tuple[str, str], list] = {}
    for row in out.rows:
        bucket = key_value(row.key, "bucket") or ""
        date = key_value(row.key, "date") or ""
        if bucket not in TOKEN_BUCKETS and bucket != "web_search":
            continue
        if date in prov_dates:
            continue
        if key_value(row.key, "service_tier") in PRIORITY_TIERS and row.invoice_nano is None:
            continue
        has_provider = row.priced_provider_nano is not None or row.provider_tokens is not None
        if not has_provider and row.invoice_nano is None:
            continue  # ledger-only row: a coverage question, not a price question
        model = key_value(row.key, "model") or "web_search"
        d = days.setdefault((date, model), [0, 0, 0, False])
        if row.priced_provider_nano is None:
            d[3] = True
        else:
            d[0] += row.priced_provider_nano
        d[1] += row.invoice_nano or 0
        d[2] += out.lines.get(row.key, 0)
    failing: set[tuple] = set()
    ok = True
    for (date, model), (priced, invoice, n, unknown) in sorted(days.items()):
        if unknown:
            ok = False
            failing.add((date, model))
            continue
        if invoice:
            err = RATIO_CTX.divide(Decimal(priced - invoice) * 100, Decimal(invoice))
            errors.append(abs(err))
            within = abs(err) <= tolerance
        else:
            within = False
        if not within and abs(priced - invoice) > CENT_NANO * n:
            ok = False
            failing.add((date, model))
    return ok, failing


def _ws_months(out: _Rows, cls: Classification | None, prov_dates: frozenset[str], pct: Decimal,
               slack: int) -> bool:
    """Unexplained ≤ *pct* % of the invoice per closed workspace-month (at least the channel's
    parse-remainder magnitude *slack*)."""
    if cls is None:
        return True
    unexplained: dict[tuple[str, str], int] = {}
    invoice: dict[tuple[str, str], int] = {}
    for row in out.rows:
        date = key_value(row.key, "date") or ""
        if date in prov_dates:
            continue
        wm = (key_value(row.key, "workspace") or _ALL_WS, date[:7])
        unexplained[wm] = unexplained.get(wm, 0) + cls.row_unexplained.get(row.key, 0)
        invoice[wm] = invoice.get(wm, 0) + (row.invoice_nano or 0)
    for wm, u in unexplained.items():
        limit = max(_scale(abs(invoice[wm]), int(pct * 10_000), 1_000_000), slack)
        if abs(u) > limit:
            return False
    return True


_NOT_PRICED_BUCKETS = frozenset({"unmapped", "credits", *costmap.REPORT_ONLY_BUCKETS})


def _finish_rows(out: _Rows, cls: Classification | None,
                 failing: set[tuple[str, str]]) -> list[ReconRow]:
    """Final statuses and residual codes: the classifier's, then ``over`` for over-count groups,
    ``unexplained`` for rows of a model-day outside the rate-card tolerance, ``within_tolerance``
    for matching rows with a small rate-card error. Rows of a channel that is not classified (no
    invoice) get ``over`` / ``under`` / ``match`` from their tokens."""
    rows: list[ReconRow] = []
    for row in out.rows:
        key = row.key
        date = key_value(key, "date") or ""
        model_day = (date, key_value(key, "model") or "web_search")
        code: str | None = None
        if cls is not None:
            code = cls.row_codes.get(key)
            status = cls.row_status.get(key, "match")
        elif row.provider_tokens is None and row.ledger_tokens is None:
            status = "unexplained" if row.ledger_nano else "match"
        else:
            lt, pt = row.ledger_tokens or 0, row.provider_tokens or 0
            status = "under" if lt < pt else "match"
        if key_group(key) in out.over_groups:
            status = "over"
        elif status != "provisional" and model_day in failing and (
                key_value(key, "bucket") not in _NOT_PRICED_BUCKETS):
            status = "unexplained"
        elif status == "match" and row.rate_card_error_pct not in (None, "0"):
            status = "within_tolerance"
        rows.append(ReconRow(key=key, ledger_tokens=row.ledger_tokens,
                             provider_tokens=row.provider_tokens, ledger_nano=row.ledger_nano,
                             priced_provider_nano=row.priced_provider_nano,
                             invoice_nano=row.invoice_nano,
                             rate_card_error_pct=row.rate_card_error_pct,
                             coverage_pct=row.coverage_pct, status=status, residual_code=code))
    return rows


def _discounts(ch: _Channel, prov: _Provider, inv: _Invoice, out: _Rows) -> dict[str, str]:
    """Effective discount ``1 − invoice / list`` per ``channel:model:bucket`` (list = the lines'
    list amounts when every line has one, else our price of the provider's usage);
    ``channel:*:*`` in channel-total mode."""
    result: dict[str, str] = {}
    if ch.total_mode:
        pair = _channel_discount(ch, prov, inv)
        if pair is not None:
            result[f"{ch.channel}:*:*"] = _dec_str(1 - RATIO_CTX.divide(
                Decimal(pair[0]), Decimal(pair[1])), _RATIO_QUANTUM)
        return result
    acc: dict[tuple[str, str], list[int]] = {}
    for (_date, _ws, model, bucket), (amount, listed, _n, missing) in inv.cells.items():
        cur = acc.setdefault((model or "*", bucket), [0, 0, 0])
        cur[0] += amount
        cur[1] += listed
        cur[2] += missing
    priced: dict[tuple[str, str], list[int]] = {}
    for row in out.rows:
        if row.invoice_nano is None:
            continue
        model = key_value(row.key, "model") or "*"
        bucket = key_value(row.key, "bucket") or ""
        cur = priced.setdefault((model, bucket), [0, 0])
        if row.priced_provider_nano is None:
            cur[1] = 1
        else:
            cur[0] += row.priced_provider_nano
    for (model, bucket), (amount, listed, missing) in sorted(acc.items()):
        base = listed if not missing else None
        if base is None:
            p = priced.get((model, bucket))
            base = p[0] if p is not None and not p[1] else None
        if base:
            result[f"{ch.channel}:{model}:{bucket}"] = _dec_str(
                1 - RATIO_CTX.divide(Decimal(amount), Decimal(base)), _RATIO_QUANTUM)
    return result


# ---------------------------------------------------------------------------------------------
# verdicts and coverage over rows (shared by reconcile and merge_reports)
# ---------------------------------------------------------------------------------------------


def _is_info(row: ReconRow) -> bool:
    return key_value(row.key, INFO_DIM) is not None


def _coverage(rows: Sequence[ReconRow]) -> tuple[str | None, str | None]:
    """``(token coverage %, dollar coverage %)`` over non-informational rows: tokens over the
    channels with provider tokens, dollars over the channels with invoice rows (Priority Tier rows,
    which no invoice carries, are left out of the dollar ratio)."""
    with_provider = {key_value(r.key, "channel") for r in rows
                     if r.provider_tokens is not None and not _is_info(r)}
    with_invoice = {key_value(r.key, "channel") for r in rows
                    if r.invoice_nano is not None and not _is_info(r)}
    lt = pt = ln = inv = 0
    for r in rows:
        if _is_info(r):
            continue
        channel = key_value(r.key, "channel")
        if channel in with_provider:
            lt += r.ledger_tokens or 0
            pt += r.provider_tokens or 0
        if channel in with_invoice and key_value(r.key, "service_tier") not in PRIORITY_TIERS:
            ln += r.ledger_nano or 0
            inv += r.invoice_nano or 0
    return _pct(lt, pt), _pct(ln, inv)


def _spend_channels(rows: Sequence[ReconRow]) -> set[str]:
    return {key_value(r.key, "channel") or "" for r in rows
            if not _is_info(r) and ((r.ledger_nano or 0) > 0 or (r.ledger_tokens or 0) > 0)}


def _overall(verdicts: Sequence[ChannelVerdict], rows: Sequence[ReconRow]) -> str:
    """SPEC §12.4: ``insufficient_data`` when no channel has invoice data; ``reconciled`` iff every
    channel with ledger spend (or, without any, every channel with invoice data) is reconciled."""
    with_invoice = [v for v in verdicts if v.verdict != "insufficient_data"]
    if not with_invoice:
        return "insufficient_data"
    spend = _spend_channels(rows)
    considered = [v for v in verdicts if v.channel in spend] or with_invoice
    return "reconciled" if all(v.verdict == "reconciled" for v in considered) else "not_reconciled"


def reconciled_channels(report: ReconciliationReport) -> frozenset[str]:
    """The channels whose verdict is ``reconciled``: the channels FOCUS ``BilledCost`` (§14.4) and
    receipts (§13.4) may cover, and ``reconciled_channels`` for ``core.extensions.enrich``."""
    return frozenset(c.channel for c in report.channels if c.verdict == "reconciled")


# ---------------------------------------------------------------------------------------------
# contract suggestion
# ---------------------------------------------------------------------------------------------


def _multipliers(ch: _Channel, prov: _Provider, inv: _Invoice, closed: Callable[[str], bool]
                 ) -> dict[str, tuple[Decimal, int]]:
    """Per-model ``(multiplier invoice / list, invoice nano)`` of one channel over its closed dates
    (all dates when none is closed); ``{"*": …}`` in channel-total mode. List = the lines' list
    amounts when every line of the model has one, else our price of the provider's usage (a model
    with unpriced usage has no multiplier)."""
    if ch.total_mode:
        pair = _channel_discount(ch, prov, inv)
        if pair is None:
            return {}
        return {"*": (_norm(RATIO_CTX.divide(Decimal(pair[0]), Decimal(pair[1]))), pair[0])}
    dates = {k[0] for k in inv.cells}
    use = {d for d in dates if closed(d)} or dates
    priced: dict[str, int] = {}
    unknown: set[str] = set()
    for (date, _ws, model, bucket, tier), value in prov.cells.items():
        if tier or date not in use or bucket == "web_search":
            continue
        if value[2]:
            unknown.add(model)
        priced[model] = priced.get(model, 0) + value[1]
    acc: dict[str, list[int]] = {}
    for (date, _ws, model, bucket), (amount, listed, _n, missing) in inv.cells.items():
        if date not in use or bucket == "web_search" or not model:
            continue
        cur = acc.setdefault(model, [0, 0, 0])
        cur[0] += amount
        cur[1] += listed
        cur[2] += missing
    out: dict[str, tuple[Decimal, int]] = {}
    for model, (amount, listed, missing) in sorted(acc.items()):
        base = listed if not missing else (None if model in unknown else priced.get(model))
        if base:
            out[model] = (_norm(RATIO_CTX.divide(Decimal(amount), Decimal(base))), amount)
    return out


def _norm(d: Decimal) -> Decimal:
    """A multiplier rounded half-even to 4 decimals, without trailing zeros (``0.85``)."""
    return Decimal(_dec_str(d, _MULT_QUANTUM))


def _overlays(channels: Mapping[str, _Channel], providers: Mapping[str, _Provider],
              invoices: Mapping[str, _Invoice], pricer: Pricer, closed: Callable[[str], bool]
              ) -> list[tuple[ContractOverlay, int]]:
    """Suggested overlays with their invoice totals, largest first: channels whose models share one
    multiplier are grouped by that multiplier (one overlay, ``multiplier`` set); a channel whose
    models need different multipliers gets its own overlay with the dollar-weighted most common
    multiplier and per-model ``overrides`` (list rates × the model's multiplier). Channels already
    at list (every multiplier 1) need no overlay."""
    per_channel: dict[str, dict[str, tuple[Decimal, int]]] = {}
    first_date: dict[str, str] = {}
    for name in sorted(channels):
        mults = _multipliers(channels[name], providers[name], invoices[name], closed)
        if not mults or all(m == 1 for m, _ in mults.values()):
            continue
        per_channel[name] = mults
        dates = sorted({k[0] for k in invoices[name].cells} | set(invoices[name].totals))
        first_date[name] = dates[0] if dates else ""
    groups: dict[tuple, list[str]] = {}
    for name, mults in per_channel.items():
        values = {m for m, _ in mults.values()}
        signature = ("uniform", values.pop()) if len(values) == 1 else ("channel", name)
        groups.setdefault(signature, []).append(name)
    result: list[tuple[ContractOverlay, int]] = []
    for names in groups.values():
        weights: dict[Decimal, int] = {}
        for name in names:
            for m, amount in per_channel[name].values():
                weights[m] = weights.get(m, 0) + amount
        common = min(weights, key=lambda m: (-weights[m], m))
        overrides = []
        if len(names) == 1:
            name = names[0]
            for model, (m, _amount) in sorted(per_channel[name].items()):
                if m != common and model != "*":
                    prices = _override_prices(pricer, name, model, m, first_date[name])
                    if prices:
                        overrides.append((model, prices))
        overlay = ContractOverlay(
            name="suggested", multiplier=common, overrides=tuple(overrides),
            effective_from=min(first_date[n] for n in names), effective_to=None, derived=True,
            assumed_fields=(), channels=tuple(sorted(names)))
        digest = hashlib.sha256(json.dumps(to_json(overlay), sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        result.append((ContractOverlay(**{**_fields(overlay), "sha256": digest}),
                       sum(weights.values())))
    result.sort(key=lambda item: (-item[1], item[0].channels))
    return result


def _fields(overlay: ContractOverlay) -> dict[str, object]:
    return {name: getattr(overlay, name) for name in ContractOverlay.__dataclass_fields__}


def _override_prices(pricer: Pricer, channel: str, model: str, m: Decimal, date: str
                     ) -> tuple[tuple[str, Decimal], ...]:
    """Per-bucket contract prices of *model* = list rate × *m* (standard tier, speed and scope)."""
    try:
        ctx = PricingContext(provider=costmap.channel_provider(channel), channel=channel,
                             model=model, model_raw=model, endpoint_scope="global")
    except ContractViolation:  # pragma: no cover - model ids come from validated records
        return ()
    ts = (_ordinal(date, "date") - _EPOCH_ORDINAL) * _DAY_MS if date else 0
    rates = pricer.resolve(ctx, ts_ms=ts)
    if rates is None:
        return ()
    out = []
    for bucket, attr in _OVERLAY_BUCKETS:
        rate = getattr(rates, attr)
        if rate is not None:
            out.append((bucket, EXACT_CTX.multiply(rate, m)))
    return tuple(out)


def suggest_contracts(aggregates: Sequence[UsageAggregate], cost_lines: Sequence[CostLine],
                      pricer: Pricer, *, today: str, closed_only: bool = False
                      ) -> tuple[ContractOverlay, ...]:
    """Every overlay ``--suggest-contract`` derives (SPEC §12.2): one per group of channels whose
    per-model multipliers ``invoice / list`` (rounded to 4 decimals) agree, largest invoice first.
    :func:`reconcile` reports the first as ``suggested_contract``; channels needing different
    multipliers need one overlay each (CONTRACT-CHANGE-RECON-1)."""
    engine = _Engine(aggregates, cost_lines, pricer, today=today, closed_only=closed_only,
                     rounding_remainders=None)
    return tuple(o for o, _total in engine.overlays())


# ---------------------------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------------------------


class _Engine:
    """Provider-side analysis shared by :func:`reconcile` and :func:`suggest_contracts`."""

    def __init__(self, aggregates: Sequence[UsageAggregate], cost_lines: Sequence[CostLine],
                 pricer: Pricer, *, today: str, closed_only: bool,
                 rounding_remainders: Mapping[str, Decimal] | None) -> None:
        if not callable(getattr(pricer, "price_usage", None)):
            raise UsageError("reconcile: pricer must implement Pricer")
        self.today = _ordinal(today, "today")
        self.closed_only = bool(closed_only)
        self.pricer = pricer
        self.delegated = extensions.delegated_channels()
        self.channels = _plan(aggregates, cost_lines, self.delegated, self.keep)
        _remainders(self.channels, rounding_remainders)
        self.invoices = {n: _invoice(ch) for n, ch in self.channels.items()}
        self.providers = {n: _price_provider(ch, pricer) for n, ch in self.channels.items()}

    def provisional(self, date: str) -> bool:
        """Dates within the revision window of ``today`` (or after it) are provisional."""
        try:
            ordinal = _dt.date.fromisoformat(date).toordinal()
        except ValueError:
            return True
        return self.today - ordinal < REVISION_WINDOW_DAYS

    def keep(self, date: str) -> bool:
        return not (self.closed_only and self.provisional(date))

    def closed(self, date: str) -> bool:
        return not self.provisional(date)

    def overlays(self) -> list[tuple[ContractOverlay, int]]:
        return _overlays(self.channels, self.providers, self.invoices, self.pricer, self.closed)

    def ensure(self, names: Iterable[str]) -> None:
        for name in names:
            if name not in self.providers:
                self.invoices[name] = _Invoice()
                self.providers[name] = _Provider()


def reconcile(ledger: Iterable[UsageRecord], aggregates: Sequence[UsageAggregate],
              cost_lines: Sequence[CostLine], pricer: Pricer, *,
              tolerance_pct: Decimal = DEFAULT_TOLERANCE_PCT,
              unexplained_pct: Decimal = DEFAULT_UNEXPLAINED_PCT, closed_only: bool = False,
              today: str, suggest_contract: bool = False,
              rerun_pricer_factory: Callable[[ContractOverlay], Pricer] | None = None,
              rounding_remainders: Mapping[str, Decimal] | None = None) -> ReconciliationReport:
    """Reconcile the streamed *ledger* and *pricer* against the provider's usage *aggregates* and
    invoice *cost_lines*, per channel (SPEC §12; see the module doc).

    ``today`` (``YYYY-MM-DD``) sets the 30-day revision window: rows dated within it are
    provisional (residual ``revision_window``) and, with *closed_only*, excluded. With
    *suggest_contract* the overlay of :func:`suggest_contracts` is reported and the reconciliation
    is re-run with ``rerun_pricer_factory(overlay)`` (else ``pricer.with_contract(overlay)`` when
    the pricer has it): ``rerun_verdict`` is the re-run's verdict (each channel priced with the
    overlay of its own group). *rounding_remainders* maps adapter names to Σ parse remainders in
    USD (``core.extensions.run_reconcilers`` builds it from ``LedgerStats``)."""
    tolerance = _pct_arg(tolerance_pct, "tolerance_pct")
    unexplained = _pct_arg(unexplained_pct, "unexplained_pct")
    engine = _Engine(aggregates, cost_lines, pricer, today=today, closed_only=closed_only,
                     rounding_remainders=rounding_remainders)
    overlays = engine.overlays() if suggest_contract else []
    rerun: dict[str, Pricer] = {}
    factory = rerun_pricer_factory or getattr(pricer, "with_contract", None)
    if overlays and callable(factory):
        for overlay, _total in overlays:
            second = factory(overlay)
            for channel in overlay.channels:
                rerun[channel] = second
    ledgers, rerun_ledgers = _stream_ledger(ledger, engine.channels, pricer, rerun,
                                            engine.delegated, engine.keep)
    engine.ensure(engine.channels)
    outcome = _assemble(engine.channels, ledgers, engine.providers, engine.invoices,
                        tolerance=tolerance, unexplained_pct=unexplained,
                        provisional=engine.provisional)
    rerun_verdict = None
    if rerun:
        providers = {n: (_price_provider(ch, rerun[n]) if n in rerun else engine.providers[n])
                     for n, ch in engine.channels.items()}
        led2 = {n: (rerun_ledgers.get(n, _Ledger()) if n in rerun else ledgers.get(n, _Ledger()))
                for n in engine.channels}
        second_outcome = _assemble(engine.channels, led2, providers, engine.invoices,
                                   tolerance=tolerance, unexplained_pct=unexplained,
                                   provisional=engine.provisional)
        rerun_verdict = _overall(second_outcome.verdicts, second_outcome.report_rows)
    return _report(outcome, engine, tolerance, unexplained,
                   overlays[0][0] if overlays else None, rerun_verdict)


def _report(outcome: _Outcome, engine: _Engine, tolerance: Decimal, unexplained: Decimal,
            suggested: ContractOverlay | None, rerun_verdict: str | None) -> ReconciliationReport:
    rows = outcome.report_rows
    token_cov, dollar_cov = _coverage(rows)
    dates = sorted(d for d in outcome.dates if d)
    today = _dt.date.fromordinal(engine.today).isoformat()
    window = (dates[0], dates[-1]) if dates else (today, today)
    errors = outcome.errors
    rate_card_error = None
    if errors:
        rate_card_error = (_dec_str(_nearest_rank(errors, 50), _PCT_QUANTUM),
                           _dec_str(_nearest_rank(errors, 95), _PCT_QUANTUM),
                           _dec_str(max(errors), _PCT_QUANTUM))
    classified = [r for r in rows if not _is_info(r)]
    if not classified:
        finality = Finality.NA
    elif any(engine.provisional(key_value(r.key, "date") or "") for r in classified):
        finality = Finality.PROVISIONAL
    else:
        finality = Finality.FINAL
    return ReconciliationReport(
        window=window, tolerance_pct=format(tolerance, "f"),
        unexplained_tolerance_pct=format(unexplained, "f"), rows=tuple(rows),
        token_coverage_pct=token_cov, dollar_coverage_pct=dollar_cov,
        rate_card_error=rate_card_error, over_count_rows=outcome.over_count,
        effective_discount=tuple(sorted(outcome.discounts.items())),
        residuals=tuple(sorted(outcome.residuals.items(), key=lambda kv: residual_order(kv[0]))),
        unexplained_nano=outcome.unexplained, channels=tuple(outcome.verdicts),
        verdict=_overall(outcome.verdicts, rows), finality=finality,
        suggested_contract=suggested, rerun_verdict=rerun_verdict)


# ---------------------------------------------------------------------------------------------
# merging reports of disjoint channels (Copilot amendment A-5)
# ---------------------------------------------------------------------------------------------


def _combined_rerun(reports: Sequence[ReconciliationReport], merged_verdict: str) -> str | None:
    if all(r.rerun_verdict is None for r in reports):
        return None
    values = [r.rerun_verdict or r.verdict for r in reports]
    if "not_reconciled" in values:
        return "not_reconciled"
    if all(v == "insufficient_data" for v in values):
        return "insufficient_data"
    if all(v == "reconciled" for v in values):
        return "reconciled"
    return "reconciled" if merged_verdict == "reconciled" else "not_reconciled"


def merge_reports(reports: Sequence[ReconciliationReport]) -> ReconciliationReport:
    """Merge reports over **disjoint** channels (RECON's and the channel extensions' reconcilers)
    into one report — the only merge implementation (addendum §21.4, A-5).

    Rows, channel verdicts, effective discounts and ``decisions`` are unioned (a channel, a
    discount key or a decision key with two different values raises ``ContractViolation``);
    residuals, unexplained nano and over-count rows are summed; the SPEC §12.4 verdict, the
    coverage percentages and the finality are recomputed over the union. The merged
    ``rate_card_error`` is conservative: the maximum of the inputs' p50, p95 and max. Tolerances
    must agree. A single report is returned unchanged."""
    reports = list(reports)
    if not reports:
        raise UsageError("merge_reports: no reports")
    for r in reports:
        if not isinstance(r, ReconciliationReport):
            raise ContractViolation("merge_reports expects ReconciliationReports")
    decisions = extensions.recon_decisions_of(reports)
    if len(reports) == 1:
        return reports[0]
    first = reports[0]
    for r in reports[1:]:
        if Decimal(r.tolerance_pct) != Decimal(first.tolerance_pct) or Decimal(
                r.unexplained_tolerance_pct) != Decimal(first.unexplained_tolerance_pct):
            raise ContractViolation("merge_reports: reports use different tolerances")
    channels: dict[str, ChannelVerdict] = {}
    for r in reports:
        for c in r.channels:
            if c.channel in channels:
                raise ContractViolation("merge_reports: a channel appears in two reports")
            channels[c.channel] = c
    discounts: dict[str, str] = {}
    for r in reports:
        for key, value in r.effective_discount:
            if discounts.setdefault(key, value) != value:
                raise ContractViolation("merge_reports: conflicting effective discounts")
    residuals: dict[str, int] = {}
    for r in reports:
        for code, nano in r.residuals:
            residuals[code] = residuals.get(code, 0) + nano
    rows = sorted((row for r in reports for row in r.rows), key=lambda row: row.key)
    verdicts = [channels[c] for c in sorted(channels)]
    token_cov, dollar_cov = _coverage(rows)
    errors = [r.rate_card_error for r in reports if r.rate_card_error is not None]
    rate_card_error = None
    if errors:
        rate_card_error = tuple(  # type: ignore[assignment]
            max((e[i] for e in errors), key=Decimal) for i in range(3))
    finalities = {r.finality for r in reports}
    if Finality.PROVISIONAL in finalities:
        finality = Finality.PROVISIONAL
    elif Finality.FINAL in finalities:
        finality = Finality.FINAL
    else:
        finality = Finality.NA
    suggested = [r.suggested_contract for r in reports if r.suggested_contract is not None]
    if len({json.dumps(to_json(s), sort_keys=True) for s in suggested}) > 1:
        raise ContractViolation("merge_reports: several different suggested contracts")
    starts = [r.window[0] for r in reports if r.window[0]]
    ends = [r.window[1] for r in reports if r.window[1]]
    verdict = _overall(verdicts, rows)
    return ReconciliationReport(
        window=(min(starts) if starts else "", max(ends) if ends else ""),
        tolerance_pct=first.tolerance_pct,
        unexplained_tolerance_pct=first.unexplained_tolerance_pct, rows=tuple(rows),
        token_coverage_pct=token_cov, dollar_coverage_pct=dollar_cov,
        rate_card_error=rate_card_error,
        over_count_rows=sum(r.over_count_rows for r in reports),
        effective_discount=tuple(sorted(discounts.items())),
        residuals=tuple(sorted(residuals.items(), key=lambda kv: residual_order(kv[0]))),
        unexplained_nano=sum(r.unexplained_nano for r in reports), channels=tuple(verdicts),
        verdict=verdict, finality=finality, suggested_contract=suggested[0] if suggested else None,
        rerun_verdict=_combined_rerun(reports, verdict), decisions=decisions)

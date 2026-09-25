"""Reference fakes and conformance suites (SPEC §3.18, D42).

Every wave-2 package tests against these instead of its siblings (SPEC §21 #4):

* :class:`FakePricer` — a :class:`~tokenbill.core.protocols.Pricer` over the verified rows and
  modifiers of ``core/facts.json`` (the values RATES' registry must equal, D37), implementing SPEC
  §6.2–§6.4 exactly: effective-dated resolution, modifiers, unknown endpoint scope as a range,
  long-context band, contract overlays (:meth:`FakePricer.with_contract`), the subscription path on
  basis LIST_EQUIVALENT, per-line exactness and one rounding per line.
* :class:`MemoryStore` — the executable specification of the store (SPEC §7): every
  :class:`~tokenbill.core.protocols.LedgerStore` method, the §7.3 merge rules, write-time privacy
  (pseudonymization, key-id checks), ``aggregate`` refusing people. The merged ledger is a pure
  function of the *set* of ingested contributions, so ingest is idempotent and order-independent by
  construction.
* :class:`FakeReplayer` — a table (or function) of savings per ``(lane_key, policy spec)``.
* :func:`published_for_tests` — drops rows below *k* (renderer tests; no complementary suppression).
* Conformance suites ``assert_*_conforms`` and :func:`smoke_pipeline_on_fakes` (gate F).

This module never imports F-SEM modules at import time; the helpers that need them
(:func:`smoke_pipeline_on_fakes`, the optional sum-check in :func:`assert_adapter_conforms`) import
them lazily.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import gzip
import hashlib
import inspect
import itertools
import json
import random
import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core import catalog
from tokenbill.core.builders import (
    CANARY_EMAIL,
    CANARY_LOGIN,
    assert_no_canary,
    lane_from_table,
    make_request,
    unit_rates_from,
)
from tokenbill.core.errors import ContractViolation, PricingError, PrivacyError, UsageError
from tokenbill.core.facts import load as load_facts
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.kanon import PERSON_DIMS, _add_usage
from tokenbill.core.labels import (
    Basis,
    Calibration,
    Evidence,
    Figure,
    add,
    estimated,
    exact,
    sub,
    unpriced,
    zero,
)
from tokenbill.core.lanes import group_lanes
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, ratio, token_nano
from tokenbill.core.protocols import (
    Adapter,
    Detector,
    ExtRecordStore,
    LedgerStats,
    LedgerStore,
    Pricer,
    Replayer,
)
from tokenbill.core.records import (
    COPILOT_AGG_SOURCE_KINDS,
    COPILOT_BILLING_PATHS,
    COPILOT_CHANNELS,
    ActivityDay,
    Attempt,
    Attribution,
    ConfigSnapshot,
    ContentTier,
    CostLine,
    Fidelity,
    Inference,
    Lane,
    LaneEvent,
    LaneKind,
    LicenseSnapshot,
    OutcomeAggregate,
    PricingContext,
    Request,
    RequestParams,
    Session,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    UsageRecord,
    UsageSource,
    WorkloadClass,
    billing_class,
    from_json,
    record_key,
    to_json,
)
from tokenbill.core.types import (
    _PUBLISH_TOKEN,
    AggRow,
    AnalysisContext,
    CalibrationReport,
    ClusterDay,
    ContractOverlay,
    Finding,
    IngestOptions,
    IngestResult,
    LaneIndexRow,
    LedgerCostRow,
    Policy,
    PricedInference,
    PricedLine,
    PricedTotal,
    PublishedAggregate,
    RawAggregate,
    ReceiptRow,
    ReplayRequestOutcome,
    ReplayResult,
    ResolvedRates,
    Scope,
    ShardKey,
    SourceInfo,
    UnitRates,
)

__all__ = [
    "ADOPTABLE_ADAPTER",
    "DQ_COPILOT_BAND_HYPOTHESIS",
    "DQ_COPILOT_WRITE_FOLDED",
    "DQ_PRINCIPAL_KEY_MISMATCH",
    "RECORD_STORE_ORG_KEY",
    "RECORD_WHERE_KEYS",
    "SOURCES_MASK_BITS",
    "UNPRICED_DQ",
    "FakePricer",
    "FakeReplayer",
    "MemoryRecordStore",
    "MemoryStore",
    "assert_adapter_conforms",
    "assert_detector_conforms",
    "assert_pricer_conforms",
    "assert_record_store_conforms",
    "assert_replayer_conforms",
    "assert_store_conforms",
    "assert_store_copilot_conforms",
    "fake_price_total",
    "lane_from_table_pool",
    "published_for_tests",
    "smoke_pipeline_on_fakes",
]

_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_FOREVER_MS = 2**53


def _date_of(ts_ms: int) -> str:
    return (_EPOCH + _dt.timedelta(days=ts_ms // _DAY_MS)).isoformat()


def _date_start_ms(date: str) -> int:
    return (_dt.date.fromisoformat(date) - _EPOCH).days * _DAY_MS


def _ts(date: str) -> int:
    """Noon UTC of *date* in ms."""
    return _date_start_ms(date) + 12 * 3_600_000


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _dec_str(d: Decimal) -> str:
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _coverage(priced_tokens: int, all_tokens: int) -> str:
    if all_tokens <= 0:
        return "1"
    value = ratio(max(priced_tokens, 0), all_tokens)
    assert value is not None
    return format(value.normalize(), "f")


# =============================================================================================
# FakePricer (SPEC §6.2–§6.4)
# =============================================================================================

#: Unpriced reasons of :class:`FakePricer` → the data-quality code a caller reports for them.
UNPRICED_DQ: Mapping[str, str] = {
    "not priceable": "dq.unpriced_model",
    "no rate row": "dq.unpriced_model",
    "unverified rate row": "dq.unverified_rate_row",
    "model before effective date": "dq.model_before_effective_date",
    "promotion expired": "dq.promotion_expired",
}
_PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h",
               "cache_write_other")
_BUCKET_TO_KEY = {"uncached_input": "input", "input": "input", "uncached": "input",
                  "output": "output", "cache_read": "cache_read",
                  "cache_write_5m": "cache_write_5m", "cache_write_1h": "cache_write_1h",
                  "cache_write_other": "cache_write_other"}
_PREDICATES = frozenset({"service_tier", "speed", "inference_geo", "endpoint_scope", "channel_in",
                         "model_in", "generation_gte", "routing", "compliance_in"})
#: The Copilot channel (addendum §6): its rows never fall back to provider rows.
_COPILOT_CHANNEL = "github_copilot"
#: Data-quality codes FakePricer names in the notes of Copilot figures (addendum §6.2).
DQ_COPILOT_BAND_HYPOTHESIS = "dq.copilot_band_hypothesis"
DQ_COPILOT_WRITE_FOLDED = "dq.copilot_write_folded_to_input"
_WRITE_BUCKETS = ("cache_write_5m", "cache_write_1h", "cache_write_other")
_FALLBACK_CHANNELS = ("claude_platform_aws", "foundry")


def _generation(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        raise PricingError(f"generation {value!r} is not numeric") from None


def _effective_rate(rates: ResolvedRates, bucket: str) -> Decimal:
    """The rate FakePricer uses for *bucket*: a bucket the row does not price falls back to the
    nearest class (other write ↔ 5m write, then input; reads → input) so no billed token is
    dropped."""
    if bucket in ("uncached_input", "input", "uncached"):
        return rates.input
    if bucket == "output":
        return rates.output
    if bucket == "cache_read":
        return rates.cache_read if rates.cache_read is not None else rates.input
    if bucket in ("cache_write_5m", "cache_write_1h"):
        own = rates.cache_write_5m if bucket == "cache_write_5m" else rates.cache_write_1h
        for rate in (own, rates.cache_write_other):
            if rate is not None:
                return rate
        return rates.input
    if bucket == "cache_write_other":
        for rate in (rates.cache_write_other, rates.cache_write_5m):
            if rate is not None:
                return rate
        return rates.input
    raise ContractViolation(f"unknown bucket {bucket!r}")


class FakePricer:
    """The reference :class:`~tokenbill.core.protocols.Pricer` over ``core/facts.json``.

    Rows: claude-opus-5-5, claude-opus-5, claude-opus-4-8, claude-fable-5, claude-fable-5-1,
    claude-mythos-5-1, claude-sonnet-5, claude-sonnet-4-6, claude-haiku-4-5 on ``anthropic_api``
    (``claude_platform_aws`` and ``foundry`` fall back to them); claude-opus-5 on ``bedrock``;
    gpt-5.6-sol on ``openai_api`` (disabled launch row, promotional row). Modifiers: batch, US geo,
    fast-mode bases, Bedrock regional. Unpriced reasons are the keys of :data:`UNPRICED_DQ`.
    Multiply modifiers scale token buckets; per-request server-tool prices are not scaled by them
    (a contract multiplier scales every bucket, per-request prices included; per-model contract
    overrides are final prices).

    GitHub Copilot (K-5, addendum §6.2): every ``facts.copilot.rates`` row (channel
    ``github_copilot``, current and history, promotional rows and their expiry) and the Copilot
    modifiers (``github.auto`` ×0.9 on ``routing="auto"``, ``github.compliance`` ×1.1, the Opus 4.8
    fast base) with the predicate keys ``routing`` and ``compliance_in``. Both Copilot billing paths
    price on basis LIST_EQUIVALENT and contract overlays never apply to them. Claude-model
    unknown-TTL writes are the range [published write, 2 × input] (the row's 1h multiplier), point
    at ``write_ttl_hint``; rows without a write price fold every write bucket into input as a
    zero-width ESTIMATED line (:data:`DQ_COPILOT_WRITE_FOLDED`). Long-context bands: hypothesis A
    (request input above the threshold) prices; when ``ctx.context_tier`` is known and hypothesis B
    (``context_tier == "long_context"``) disagrees, every line is the range of both with point A,
    ESTIMATED (:data:`DQ_COPILOT_BAND_HYPOTHESIS`). A non-billable Copilot call (a utility call with
    nano-AIU 0) is EXACT $0 even for a model without a row. The Anthropic / OpenAI behaviour is
    unchanged.
    """

    def __init__(self, contract: ContractOverlay | None = None) -> None:
        facts = load_facts()
        copilot = getattr(facts, "copilot", None)  # absent on reduced facts stand-ins
        self._rows = tuple(facts.rate_rows) + (tuple(copilot.rate_rows) if copilot else ())
        self._mods = tuple(facts.modifiers) + (tuple(copilot.modifiers) if copilot else ())
        for m in self._mods:
            unknown = {k for k, _ in m.when} - _PREDICATES
            if unknown:
                raise PricingError(f"modifier {m.modifier_id}: unknown predicate")
        self._contract = contract
        self.basis: Basis = Basis.CONTRACT if contract is not None else Basis.LIST
        payload = {
            "rates": facts.rate_rows_json(),
            "modifiers": facts.modifiers_json(),
            "contract": to_json(contract) if contract is not None else None,
        }
        if copilot is not None:
            payload["copilot_rates"] = facts.copilot_rate_rows_json()
            payload["copilot_modifiers"] = [to_json(m) for m in copilot.modifiers]
        self.rate_card_sha256: str = hashlib.sha256(_canonical(payload).encode()).hexdigest()

    def with_contract(self, overlay: ContractOverlay) -> FakePricer:
        """A FakePricer with *overlay* applied (basis CONTRACT on the overlay's channels)."""
        if not isinstance(overlay, ContractOverlay):
            raise UsageError("with_contract expects a ContractOverlay")
        return FakePricer(contract=overlay)

    # ---------- resolution ----------

    def _rows_for(self, channel: str, model: str) -> list:
        rows = [r for r in self._rows
                if r.channel == channel and (r.model == model or model in r.aliases)]
        if not rows and channel in _FALLBACK_CHANNELS:
            rows = [r for r in self._rows
                    if r.channel == "anthropic_api" and (r.model == model or model in r.aliases)]
        return sorted(rows, key=lambda r: r.effective_from)

    def _row(self, ctx: PricingContext, date: str) -> tuple[Any, str | None]:
        if not ctx.model:
            return None, "not priceable"
        rows = self._rows_for(ctx.channel, ctx.model)
        if not rows:
            return None, "no rate row"
        for row in rows:
            if row.effective_from <= date and (row.effective_to is None or date < row.effective_to):
                return (row, None) if row.enabled else (None, "unverified rate row")
        if date < rows[0].effective_from:
            return None, "model before effective date"
        last = max(rows, key=lambda r: r.effective_to or "9999-12-31")
        if last.promotion is not None and last.effective_to is not None \
                and date >= last.effective_to:
            return None, "promotion expired"
        return None, "no rate row"

    def _scope_priced(self, channel: str) -> bool:
        for m in self._mods:
            when = dict(m.when)
            if "endpoint_scope" in when:
                chans = when.get("channel_in")
                if chans is None or channel in chans.split(","):
                    return True
        return False

    @staticmethod
    def _matches(m: Any, row: Any, ctx: PricingContext, scope: str) -> bool:
        for key, value in m.when:
            if key == "service_tier":
                ok = ctx.service_tier == value
            elif key == "speed":
                ok = ctx.speed == value
            elif key == "inference_geo":
                ok = ctx.inference_geo == value
            elif key == "endpoint_scope":
                ok = scope == value or (value == "regional" and scope == "multi_region")
            elif key == "channel_in":
                ok = ctx.channel in value.split(",")
            elif key == "model_in":
                ok = row.model in value.split(",")
            elif key == "routing":
                ok = ctx.routing == value
            elif key == "compliance_in":
                ok = ctx.compliance is not None and ctx.compliance in value.split(",")
            else:  # generation_gte (predicates validated in __init__)
                ok = _generation(row.generation) >= _generation(value)
            if not ok:
                return False
        return True

    def _contract_applies(self, ctx: PricingContext, date: str) -> bool:
        c = self._contract
        if c is None or ctx.billing_path == "subscription":
            return False
        if ctx.billing_path in COPILOT_BILLING_PATHS or ctx.channel == _COPILOT_CHANNEL:
            return False  # contract overlays never apply to Copilot credits (addendum §6.2 #3)
        if c.channels and ctx.channel not in c.channels:
            return False
        return c.effective_from <= date and (c.effective_to is None or date < c.effective_to)

    def _build(self, row: Any, ctx: PricingContext, date: str, *, scope: str,
               band: bool) -> ResolvedRates:
        applicable = [m for m in self._mods if self._matches(m, row, ctx, scope)]
        inp, out = row.input_usd_per_mtok, row.output_usd_per_mtok
        mod_ids: list[str] = []
        assumed = False
        for m in applicable:
            if m.kind == "replace_base":
                base = dict(m.base_usd_per_mtok)
                inp, out = base.get("input", inp), base.get("output", out)
                mod_ids.append(m.modifier_id)
                assumed = assumed or m.stacking == "assumed"

        def times(mult: Decimal | None) -> Decimal | None:
            return None if mult is None else EXACT_CTX.multiply(inp, mult)

        rates: dict[str, Decimal | None] = {
            "input": inp,
            "output": out,
            "cache_read": times(row.cache_read_mult),
            "cache_write_5m": times(row.cache_write_5m_mult),
            "cache_write_1h": times(row.cache_write_1h_mult),
            "cache_write_other": times(row.cache_write_other_mult),
        }
        if band:
            for bucket, value in row.long_context_usd_per_mtok:
                rates[_BUCKET_TO_KEY.get(bucket, bucket)] = value
        per_request = dict(row.per_request_usd)
        for m in applicable:
            if m.kind != "multiply" or m.factor is None:
                continue
            targets = set(_PRICE_KEYS) if "*" in m.applies_to else {
                _BUCKET_TO_KEY.get(b, b) for b in m.applies_to}
            for key in _PRICE_KEYS:
                if key in targets and rates[key] is not None:
                    rates[key] = EXACT_CTX.multiply(rates[key], m.factor)
            mod_ids.append(m.modifier_id)
            assumed = assumed or m.stacking == "assumed"
        layer = "builtin"
        if self._contract_applies(ctx, date):
            c = self._contract
            assert c is not None
            overrides = dict(dict(c.overrides).get(row.model, ()))
            overridden: set[str] = set()
            for bucket, value in overrides.items():
                if bucket == "web_search":
                    per_request["web_search"] = value
                    overridden.add(bucket)
                    continue
                key = _BUCKET_TO_KEY.get(bucket)
                if key is None:
                    raise PricingError(f"contract {c.name}: unknown override bucket {bucket!r}")
                rates[key] = value
                overridden.add(key)
            if c.multiplier is not None:
                for key in _PRICE_KEYS:
                    if key not in overridden and rates[key] is not None:
                        rates[key] = EXACT_CTX.multiply(rates[key], c.multiplier)
                for name, value in list(per_request.items()):
                    if name not in overridden:
                        per_request[name] = EXACT_CTX.multiply(value, c.multiplier)
            layer = "contract"
            mod_ids.append(f"contract:{c.name}")
        return ResolvedRates(
            row_id=row.row_id,
            channel=ctx.channel,
            model=row.model,
            input=rates["input"],  # type: ignore[arg-type]
            output=rates["output"],  # type: ignore[arg-type]
            cache_read=rates["cache_read"],
            cache_write_5m=rates["cache_write_5m"],
            cache_write_1h=rates["cache_write_1h"],
            cache_write_other=rates["cache_write_other"],
            per_request=tuple(sorted(per_request.items())),
            modifier_ids=tuple(mod_ids),
            stacking_assumed=assumed,
            layer=layer,
            min_cacheable_tokens=row.min_cacheable_tokens,
            tokenizer_family=row.tokenizer_family,
            long_context_band=band,
        )

    def _resolve(self, ctx: PricingContext, ts_ms: int, total_input: int = 0, *,
                 band: bool | None = None) -> tuple[ResolvedRates | None, str | None]:
        date = _date_of(ts_ms)
        row, reason = self._row(ctx, date)
        if row is None:
            return None, reason
        if band is None:
            band = (row.long_context_threshold is not None
                    and total_input > row.long_context_threshold)
        else:
            band = band and row.long_context_threshold is not None
        if self._scope_priced(ctx.channel) and ctx.endpoint_scope == "unknown":
            low = self._build(row, ctx, date, scope="global", band=band)
            high = self._build(row, ctx, date, scope="regional", band=band)
            return dataclasses.replace(low, scope_range=high), None
        return self._build(row, ctx, date, scope=ctx.endpoint_scope, band=band), None

    def resolve(self, ctx: PricingContext, *, ts_ms: int) -> ResolvedRates | None:
        """Resolved rates at the UTC date of *ts_ms* (None when unpriced; SPEC §6.2). With an
        unknown endpoint scope on a scope-priced channel the result is the global (low) rates with
        the regional rates in ``scope_range``."""
        return self._resolve(ctx, ts_ms)[0]

    def unit_rates(self, ctx: PricingContext, *, ts_ms: int) -> UnitRates | None:
        """Exact integer unit rates of the point (low) rates (SPEC §6.4); None when unpriced or the
        scale would exceed 24."""
        rates, _ = self._resolve(ctx, ts_ms)
        if rates is None:
            return None
        effective = dataclasses.replace(
            rates,
            cache_read=_effective_rate(rates, "cache_read"),
            cache_write_5m=_effective_rate(rates, "cache_write_5m"),
            cache_write_1h=_effective_rate(rates, "cache_write_1h"),
            cache_write_other=_effective_rate(rates, "cache_write_other"),
            scope_range=None,
        )
        web = dict(rates.per_request).get("web_search", Decimal(0))
        return unit_rates_from(effective, web_search_usd=web)

    def min_cacheable_tokens(self, ctx: PricingContext, *, ts_ms: int) -> int | None:
        """The row's minimum cacheable prefix (None when unpriced)."""
        rates, _ = self._resolve(ctx, ts_ms)
        return rates.min_cacheable_tokens if rates is not None else None

    def supports(self, ctx: PricingContext, feature: str, *, ts_ms: int) -> bool:
        """Whether the effective row lists *feature* in ``supports``."""
        row, _ = self._row(ctx, _date_of(ts_ms))
        return row is not None and feature in row.supports

    def tokenizer_family(self, ctx: PricingContext, *, ts_ms: int) -> str | None:
        """The effective row's tokenizer family (None when unpriced)."""
        row, _ = self._row(ctx, _date_of(ts_ms))
        return row.tokenizer_family if row is not None else None

    # ---------- pricing ----------

    def _basis(self, ctx: PricingContext, rates: ResolvedRates | None) -> Basis:
        if ctx.billing_path == "subscription" or ctx.billing_path in COPILOT_BILLING_PATHS:
            return Basis.LIST_EQUIVALENT
        if rates is not None and rates.layer == "contract":
            return Basis.CONTRACT
        return Basis.LIST

    def price_inference(self, inf: Inference, *, ts_ms: int) -> PricedInference:
        """Price one inference (:meth:`price_usage` of its usage, context and flags)."""
        priced = self.price_usage(inf.usage, inf.pricing, ts_ms=ts_ms, billable=inf.billable,
                                  usage_source=inf.usage_source, output_upper=inf.output_upper)
        return dataclasses.replace(priced, inference_id=inf.inference_id)

    def _band_hypothesis_b(self, ctx: PricingContext, ts_ms: int, total_input: int) -> bool | None:
        """Band hypothesis B when it disagrees with A on a Copilot row (addendum §6.2 #4), else
        None."""
        if ctx.channel != _COPILOT_CHANNEL or ctx.context_tier is None:
            return None
        row, _ = self._row(ctx, _date_of(ts_ms))
        if row is None or row.long_context_threshold is None:
            return None
        a = total_input > row.long_context_threshold
        b = ctx.context_tier == "long_context"
        return b if a != b else None

    def price_usage(self, usage: UsageBuckets, ctx: PricingContext, *, ts_ms: int,
                    billable: bool | None = True, usage_source: UsageSource = UsageSource.FINAL,
                    output_upper: int | None = None) -> PricedInference:
        """One PricedLine per non-zero bucket and server-tool counter, each rounded once; exactness
        per line (SPEC §6.3 table, R5, R9); Copilot band hypotheses and folded writes per the
        class docstring."""
        source = UsageSource(usage_source)
        rates, reason = self._resolve(ctx, ts_ms, usage.total_input)
        basis = self._basis(ctx, rates)
        if rates is None:
            if billable is False and ctx.channel == _COPILOT_CHANNEL:
                # a non-billable Copilot call (utility model, nano-AIU 0): EXACT $0, not a gap
                return PricedInference(inference_id=None, lines=(), figure=exact(0, basis),
                                       exact_nano=0, estimated=None, unpriced_reason=None)
            why = reason or "no rate row"
            return PricedInference(inference_id=None, lines=(), figure=unpriced(why, basis),
                                   exact_nano=0, estimated=None, unpriced_reason=why)
        lines, notes = self._lines(usage, ctx, rates, billable=billable, source=source,
                                   output_upper=output_upper)
        band_b = self._band_hypothesis_b(ctx, ts_ms, usage.total_input)
        if band_b is not None:
            alt, _ = self._resolve(ctx, ts_ms, usage.total_input, band=band_b)
            assert alt is not None
            alt_lines, _ = self._lines(usage, ctx, alt, billable=billable, source=source,
                                       output_upper=output_upper)
            lines = [_hypothesis_range(a, b) for a, b in zip(lines, alt_lines, strict=True)]
            notes.append("long-context band hypotheses A/B differ: range, point A "
                         f"({DQ_COPILOT_BAND_HYPOTHESIS})")
        return _assemble(lines, basis, (rates.row_id,), "; ".join(dict.fromkeys(notes)))

    def _lines(self, usage: UsageBuckets, ctx: PricingContext, rates: ResolvedRates, *,
               billable: bool | None, source: UsageSource,
               output_upper: int | None) -> tuple[list[PricedLine], list[str]]:
        """The priced lines of *usage* at *rates* and their notes (one rounding per line)."""
        high_rates = rates.scope_range
        folded = (ctx.channel == _COPILOT_CHANNEL and rates.cache_write_5m is None
                  and rates.cache_write_1h is None and rates.cache_write_other is None)
        notes: list[str] = []
        lines: list[PricedLine] = []

        def emit(bucket: str, qty: int, rate: Decimal, *, low: tuple[int, Decimal] | None = None,
                 high: tuple[int, Decimal] | None = None, per_request: bool = False) -> None:
            def amount(q: int, r: Decimal) -> int:
                if per_request:
                    return decimal_to_nano(EXACT_CTX.multiply(Decimal(q), r))
                return token_nano(q, r)

            point = amount(qty, rate)
            lo = hi = None
            if low is not None and high is not None:
                lo, hi = amount(*low), amount(*high)
            if billable is False:
                point, lo, hi = 0, None, None
            elif billable is None or source is UsageSource.PARTIAL_STREAM:
                lo, hi = 0, max(point, hi if hi is not None else point)
            elif source is UsageSource.ESTIMATED and lo is None:
                lo, hi = point, point
            lines.append(PricedLine(
                bucket=bucket, quantity=qty, unit_usd_per_mtok=_dec_str(rate), amount_nano=point,
                low_nano=lo, high_nano=hi, exact=lo is None, rate_row_id=rates.row_id,
                modifier_ids=rates.modifier_ids, layer=rates.layer))

        def scoped(bucket: str) -> dict[str, Any]:
            if high_rates is None:
                return {}
            return {"low": (qty_of[bucket], _effective_rate(rates, bucket)),
                    "high": (qty_of[bucket], _effective_rate(high_rates, bucket))}

        qty_of = {
            "uncached_input": usage.uncached_input, "cache_read": usage.cache_read,
            "cache_write_5m": usage.cache_write_5m, "cache_write_1h": usage.cache_write_1h,
            "cache_write_other": usage.cache_write_other,
        }
        if high_rates is not None:
            notes.append("endpoint scope unknown: priced [global, regional]")
        for bucket, qty in qty_of.items():
            if qty and folded and bucket in _WRITE_BUCKETS:
                rate = _effective_rate(rates, bucket)  # = input: the row prices no writes
                emit(bucket, qty, rate, low=(qty, rate), high=(qty, rate))
                notes.append(f"writes folded to input ({DQ_COPILOT_WRITE_FOLDED})")
            elif qty:
                emit(bucket, qty, _effective_rate(rates, bucket), **scoped(bucket))
        if usage.cache_write_unknown:
            notes.append(f"writes folded to input ({DQ_COPILOT_WRITE_FOLDED})" if folded
                         else "unknown-TTL cache writes priced [5m, 1h]")
            q = usage.cache_write_unknown
            low_rate = _effective_rate(rates, "cache_write_5m")
            top = high_rates if high_rates is not None else rates
            high_rate = _effective_rate(top, "cache_write_1h")
            hint = {"5m": low_rate, "1h": _effective_rate(rates, "cache_write_1h")}.get(
                ctx.write_ttl_hint or "", low_rate)
            emit("cache_write_unknown", q, hint, low=(q, low_rate), high=(q, high_rate))
        out_high = high_rates.output if high_rates is not None else rates.output
        if source is UsageSource.MESSAGE_START_ONLY and (usage.output or output_upper):
            notes.append("placeholder output priced [logged, upper]")
            upper = max(usage.output, output_upper if output_upper is not None else usage.output)
            emit("output", usage.output, rates.output, low=(usage.output, rates.output),
                 high=(upper, out_high))
        elif usage.output:
            emit("output", usage.output, rates.output,
                 **({"low": (usage.output, rates.output), "high": (usage.output, out_high)}
                    if high_rates is not None else {}))
        web = dict(rates.per_request).get("web_search")
        if usage.web_search_requests and web is not None:
            q = usage.web_search_requests
            web_high = dict(high_rates.per_request).get("web_search", web) \
                if high_rates is not None else web
            emit("web_search", q, web, per_request=True,
                 **({"low": (q, web), "high": (q, web_high)} if high_rates is not None else {}))
        if billable is None:
            notes.append("billing uncertain: [0, full]")
        elif source is UsageSource.ESTIMATED:
            notes.append("reconstructed usage (estimated)")
        elif source is UsageSource.PARTIAL_STREAM:
            notes.append("partial stream: billing unknown [0, full]")
        return lines, notes


def _hypothesis_range(a: PricedLine, b: PricedLine) -> PricedLine:
    """Line *a* (hypothesis A) widened to cover line *b* (hypothesis B): point A, range over both,
    never exact."""
    lows = [x.low_nano if x.low_nano is not None else x.amount_nano for x in (a, b)]
    highs = [x.high_nano if x.high_nano is not None else x.amount_nano for x in (a, b)]
    return dataclasses.replace(a, low_nano=min(lows), high_nano=max(highs), exact=False)


def _assemble(lines: list[PricedLine], basis: Basis, provenance: tuple[str, ...],
              note: str) -> PricedInference:
    exact_nano = sum(ln.amount_nano for ln in lines if ln.exact)
    ranged = [ln for ln in lines if not ln.exact]
    if not ranged:
        figure = Figure(nano=exact_nano, evidence=Evidence.EXACT, basis=basis,
                        provenance=provenance)
        return PricedInference(inference_id=None, lines=tuple(lines), figure=figure,
                               exact_nano=exact_nano, estimated=None, unpriced_reason=None)
    point = sum(ln.amount_nano for ln in ranged)
    low = sum(ln.low_nano or 0 for ln in ranged)
    high = sum(ln.high_nano or 0 for ln in ranged)
    text = note or "range lines"
    est = Figure(nano=point, evidence=Evidence.ESTIMATED, basis=basis, low_nano=low,
                 high_nano=high, provenance=provenance, note=text)
    figure = Figure(nano=exact_nano + point, evidence=Evidence.ESTIMATED, basis=basis,
                    low_nano=exact_nano + low, high_nano=exact_nano + high, provenance=provenance,
                    note=text)
    return PricedInference(inference_id=None, lines=tuple(lines), figure=figure,
                           exact_nano=exact_nano, estimated=est, unpriced_reason=None)


def fake_price_total(pricer: Pricer, items: Iterable[tuple[Inference, int]]) -> PricedTotal:
    """A :class:`~tokenbill.core.types.PricedTotal` over ``(inference, ts_ms)`` pairs (the fake of
    ``rates.engine.price_total``, SPEC §6.3): exact lines on billed bases into ``exact``, range
    lines on billed bases into ``estimated``, LIST_EQUIVALENT lines on the Copilot billing paths
    into ``pool`` and every other LIST_EQUIVALENT line (the ``subscription`` path) into
    ``allowance`` (C-15); unpriced inferences counted with their tokens; coverage = priced billable
    tokens / all billable tokens. Inferences with ``billable False`` are not billable and are
    skipped."""
    billed_basis = pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST
    return _price_total(((inf, pricer.price_inference(inf, ts_ms=ts_ms))
                         for inf, ts_ms in items if inf.billable is not False), billed_basis)


class _ListEquivalentSum:
    """Σ of LIST_EQUIVALENT figures (allowance or pool): EXACT iff every figure is."""

    def __init__(self) -> None:
        self.n = self.point = self.low = self.high = 0
        self.exact = True

    def add(self, fig: Figure) -> None:
        assert fig.nano is not None
        self.n += 1
        self.point += fig.nano
        self.low += fig.low_nano if fig.low_nano is not None else fig.nano
        self.high += fig.high_nano if fig.high_nano is not None else fig.nano
        self.exact = self.exact and fig.evidence is Evidence.EXACT

    def figure(self) -> Figure | None:
        if self.n == 0:
            return None
        if self.exact:
            return exact(self.point, Basis.LIST_EQUIVALENT)
        return Figure(nano=self.point, evidence=Evidence.ESTIMATED, basis=Basis.LIST_EQUIVALENT,
                      low_nano=self.low, high_nano=self.high, note="range lines")


def _price_total(priced_items: Iterable[tuple[Inference, PricedInference]],
                 billed_basis: Basis) -> PricedTotal:
    """:func:`fake_price_total` over already priced ``(inference, priced)`` pairs of billable
    inferences; the exact and estimated totals carry *billed_basis*."""
    exact_nano = est_point = est_low = est_high = 0
    has_est = False
    allowance = _ListEquivalentSum()
    pool = _ListEquivalentSum()
    priced = unpriced_n = unpriced_tokens = all_tokens = 0
    for inf, p in priced_items:
        tokens = inf.usage.total_input + inf.usage.output
        all_tokens += tokens
        if p.figure.nano is None:
            unpriced_n += 1
            unpriced_tokens += tokens
            continue
        priced += 1
        if p.figure.basis is Basis.LIST_EQUIVALENT:
            target = pool if inf.pricing.billing_path in COPILOT_BILLING_PATHS else allowance
            target.add(p.figure)
            continue
        exact_nano += p.exact_nano
        if p.estimated is not None and p.estimated.nano is not None:
            has_est = True
            est_point += p.estimated.nano
            est_low += p.estimated.low_nano if p.estimated.low_nano is not None else 0
            est_high += p.estimated.high_nano if p.estimated.high_nano is not None else 0
    est_fig = (Figure(nano=est_point, evidence=Evidence.ESTIMATED, basis=billed_basis,
                      low_nano=est_low, high_nano=est_high, note="range lines")
               if has_est else None)
    return PricedTotal(
        exact=exact(exact_nano, billed_basis),
        estimated=est_fig,
        allowance=allowance.figure(),
        priced_inferences=priced,
        unpriced_inferences=unpriced_n,
        unpriced_tokens=unpriced_tokens,
        coverage=_coverage(all_tokens - unpriced_tokens, all_tokens),
        pool=pool.figure(),
    )


# =============================================================================================
# published_for_tests
# =============================================================================================


def published_for_tests(raw: RawAggregate, k: int = 5) -> PublishedAggregate:
    """Test-only publisher: drops rows with ``n_users < k`` (no complementary suppression), so
    renderer tests do not depend on ``core.kanon``."""
    kept = tuple(r for r in raw.rows if r.n_users >= k)
    dropped = [r for r in raw.rows if r.n_users < k]
    return PublishedAggregate(group_by=tuple(raw.group_by), rows=kept, window=raw.window, k=k,
                              suppressed_rows=len(dropped),
                              suppressed_users=sum(r.n_users for r in dropped),
                              token=_PUBLISH_TOKEN)


# =============================================================================================
# MemoryStore (SPEC §7)
# =============================================================================================

#: ``sources_mask`` bit per adapter (SPEC §7.3; Copilot bits per C-30 / R-E37); other adapters
#: contribute no bit.
SOURCES_MASK_BITS: Mapping[str, int] = {
    "claude-code": 1, "trace@1": 2, "trace@2": 4, "otlp": 8, "openai": 16, "bedrock": 32,
    "anthropic-responses": 64, "claude-code-headless": 128,
    "copilot-cli": 256, "copilot-otel": 512, "copilot-vscode-traces": 1024,
    "gh-aw-token-usage": 2048, "copilot-export": 4096,
}
_AGG_DIMS = ("date", "team", "cost_center", "workspace_id", "workload_class", "lane_kind", "model",
             "agent_type", "agent_product", "repo", "arm", "wave", "skill", "mcp_server",
             "billing_path", "channel")
_COST_DIMS = ("date", "provider", "channel", "model", "team", "cost_center", "project",
              "workspace_id", "lane_kind", "workload_class", "agent_product", "billing_path")
_WHERE_KEYS = frozenset(_AGG_DIMS) | {"billing_class", "provider", "project"}
_HASHED_ATTR = ("repo", "workspace_id", "api_key_id", "skill", "mcp_server", "plugin", "cwd_key")
#: ``CostLine`` fields that may carry ``h_`` values (name key): the wave-1 workspace / account and
#: the GitHub repository and agentic-workflow names (C-4).
_COST_LINE_HASHED = ("workspace_id", "repo", "workflow")
_HASH_RE = re.compile(r"h_[0-9a-f]{20}\Z")
_STORE_P_RE = re.compile(r"p_[0-9a-f]{20}\Z")
_CLUSTER_KINDS = ("team", "workspace", "mdm_group", "gateway")  # "gateway": R-E28
#: ``count_users(source="cost_lines")`` filter keys (C-27).
_COST_LINE_WHERE_KEYS = frozenset({"team", "cost_center", "channel", "model", "sku",
                                   "workspace_id", "cost_type"})
#: The only adapter whose key ids a store opened with ``adopt_key_ids=True`` adopts (R-E21).
ADOPTABLE_ADAPTER = "copilot-export"
DQ_PRINCIPAL_KEY_MISMATCH = "dq.principal_key_mismatch"


def _latest_fetch_wins(rec: object) -> bool:
    """Copilot provider records: the version with the latest ``fetched_ms`` wins (addendum §7.1);
    every other record keeps the SPEC rule (final, then latest)."""
    if isinstance(rec, CostLine):
        return rec.channel in COPILOT_CHANNELS
    if isinstance(rec, UsageAggregate):
        return rec.source_kind in COPILOT_AGG_SOURCE_KINDS or dict(rec.dims).get(
            "channel") in COPILOT_CHANNELS
    return isinstance(rec, OutcomeAggregate) and rec.source_kind == "github.copilot_metrics"


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and value.startswith("h_")


@dataclasses.dataclass(frozen=True)
class _Contribution:
    request: Request
    source_id: str
    adapter: str
    fidelity: Fidelity
    priority: int
    canon: str
    key: tuple[str, str]          # (adapter, sha256(canon)): identity of the contribution


@dataclasses.dataclass(frozen=True)
class _Merged:
    request: Request
    fidelity: Fidelity
    priority: int
    adapter: str
    sources_mask: int
    lane_kind: LaneKind
    pricer: int | None                    # index into MemoryStore._pricers (None: unpriced)
    members: tuple[tuple[str, str], ...]  # keys of every contribution merged into the request


@dataclasses.dataclass
class _State:
    requests: dict[str, _Merged]
    shells: dict[str, Lane]
    collisions: int
    mismatches: int


def _serving_output(req: Request) -> int:
    si = req.serving_inference
    return si.usage.output if si is not None else 0


def _message_ids(req: Request) -> set[str]:
    return {a.provider_message_id for a in req.attempts if a.provider_message_id}


def _request_hints(req: Request) -> set[str]:
    return {a.provider_request_id for a in req.attempts if a.provider_request_id}


def _shell_rank(shell: Lane) -> tuple[bool, bool, str]:
    """Preferred lane shell: a known kind, a known cache scope, then the canonical smallest."""
    return (shell.kind is LaneKind.UNKNOWN, shell.cache_scope_key == "unknown",
            _canonical(to_json(shell)))


def _day_overlaps(date: str, lo: int, hi: int) -> bool:
    start = _date_start_ms(date)
    return lo < start + _DAY_MS and start < hi


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _is_null(value: object) -> bool:
    return value is None or value == () or value is WorkloadClass.UNKNOWN


def _pick(candidates: list[tuple[int, Any]]) -> Any:
    """Highest priority wins; equal priority → lexicographically smallest value."""
    best = max(p for p, _ in candidates)
    values = [v for p, v in candidates if p == best]
    return min(values, key=lambda v: _canonical(to_json(v)) if dataclasses.is_dataclass(v)
               else str(v))


def _merge_attribution(group: list[_Contribution]) -> Attribution:
    fields: dict[str, Any] = {}
    for f in dataclasses.fields(Attribution):
        if f.name == "extra":
            continue
        cands = [(c.priority, getattr(c.request.attribution, f.name)) for c in group
                 if not _is_null(getattr(c.request.attribution, f.name))]
        if cands:
            fields[f.name] = _pick(cands)
    extra: dict[str, list[tuple[int, str]]] = {}
    for c in group:
        for key, value in c.request.attribution.extra:
            extra.setdefault(key, []).append((c.priority, value))
    fields["extra"] = tuple(sorted((key, _pick(v)) for key, v in extra.items()))
    return Attribution(**fields)


def _merge_params(winner: _Contribution, group: list[_Contribution]) -> RequestParams:
    base = winner.request.params
    fills: dict[str, Any] = {}
    for f in dataclasses.fields(RequestParams):
        if f.name == "model_requested" or not _is_null(getattr(base, f.name)):
            continue
        cands = [(c.priority, getattr(c.request.params, f.name)) for c in group
                 if not _is_null(getattr(c.request.params, f.name))]
        if cands:
            fills[f.name] = _pick(cands)
    return dataclasses.replace(base, **fills) if fills else base


def _fill_diagnostics(winner: _Contribution, group: list[_Contribution]) -> tuple[Attempt, ...]:
    attempts = winner.request.attempts
    final = attempts[-1]
    if final.diagnostics is not None:
        return attempts
    cands = [(c.priority, c.request.attempts[-1].diagnostics) for c in group
             if c.request.attempts[-1].diagnostics is not None]
    if not cands:
        return attempts
    return (*attempts[:-1], dataclasses.replace(final, diagnostics=_pick(cands)))


def _first_by_priority(group: list[_Contribution], getter: Callable[[Request], Any]) -> Any:
    for c in sorted(group, key=lambda c: (-c.priority, c.canon)):
        value = getter(c.request)
        if not _is_null(value):
            return value
    return None


class MemoryStore:
    """Dict-backed reference :class:`~tokenbill.core.protocols.LedgerStore` (SPEC §7), also a
    :class:`~tokenbill.core.protocols.LedgerStats`.

    ``MemoryStore(*, org_key=None, name_key_id=None, pricer=None, now_ms=0, adopt_key_ids=False)``
    mirrors ``SqliteStore``'s keyword arguments. Semantics:

    * **Privacy at write time** (§7.2, §7.6): ``r_<ref>`` principals become
      ``pseudonym(org_key, "p", ref)`` and ``c_<hex>`` become ``pseudonym(org_key, "p", "c:"+hex)``;
      either without an org key raises ``PrivacyError`` before anything is stored. ``p_`` values are
      kept only when ``SourceInfo.principal_key_id`` equals the store's org key id, ``h_`` values
      only when ``SourceInfo.name_key_id`` equals the store's name key id (set by the first ingest
      that carries one); otherwise those fields are nulled and counted as ``dq.name_key_mismatch``.
    * **Merge** (§7.3): contributions join through the request id, provider message ids and — unless
      a provider request id was seen with two message ids (``dq.request_id_collision``) — provider
      request ids. The usage set (attempts, inferences, timing) comes from the highest (fidelity,
      source priority, serving output) contribution, exact ties to the canonically smallest (the
      lane, session and sequence number travel with it);
      attribution is merged per field (and per ``extra`` key) by priority, equal priority →
      lexicographically smallest; ``sources_mask`` is the OR of adapter bits; diagnostics fill if
      null; parameters fill if null. The surviving request id is the smallest id among contributions
      carrying a message id (else the smallest id). The merged ledger is recomputed from the *set*
      of contributions, so ingest is idempotent and order-independent.
    * **Pricing** (§7.2): each ingest prices its requests with the pricer it is given (else the
      constructor's); a merged request is priced with the pricer of the ingest that brought its
      winning usage set; ``reprice(pricer, since_ms=…, until_ms=…)`` re-prices exactly the requests
      in its window. Without any pricer an inference is unpriced (``"no pricer"``).
    * ``where`` filters accept the empty string to match a missing (NULL) value. ``cluster_days``
      windows are half-open ``[since, until)`` dates. ``cost_rows`` fills dimensions that are not
      grouped with ``""`` (string fields) or None.

    GitHub Copilot (K-5):

    * **Key-id adoption** (ruling R-E21) with ``adopt_key_ids=True``: the first ingested source of
      adapter ``copilot-export`` whose ``principal_key_id`` is not the store's own is adopted —
      ``meta()`` gains ``adopted_key_id`` / ``adopted_name_key_id`` (a store without an org key also
      takes them as ``org_key_id`` / ``name_key_id`` — never the name key id of an earlier source —
      and ``org_key_mode == "adopted"``) and an ``adopt_key_id`` audit row; ``p_`` / ``h_`` values
      (cost-line ``repo`` / ``workflow`` included) under the own or the adopted key id are kept,
      others nulled (``dq.principal_key_mismatch`` / ``dq.name_key_mismatch``); a second
      bundle under another key id → ``UsageError`` (nothing stored); another adapter's key id is
      never adopted; ``r_`` / ``c_`` principals still need the store's own org key. Without the
      flag the wave-1 behaviour is unchanged. ``meta()`` always reports ``org_key_mode`` (``own`` |
      ``adopted`` | ``none``).
    * ``count_users(…, source="cost_lines")`` counts distinct ``CostLine.principal``;
      :meth:`source_stats` sums the integer ``IngestResult.stats`` of every stored source.
    * LIST_EQUIVALENT lines on the Copilot billing paths go to ``PricedTotal.pool`` and
      ``ClusterDay.pool_nano`` (``allowance`` keeps the subscription path); cluster kind
      ``gateway`` (R-E28) groups by ``Attribution.extra["gateway"]``.
    * Copilot cost lines and aggregates (channels ``COPILOT_CHANNELS``, Copilot source kinds) keep
      the version with the latest ``fetched_ms`` per id (ties: canonical order).
    """

    def __init__(self, *, org_key: bytes | None = None, name_key_id: str | None = None,
                 pricer: Pricer | None = None, now_ms: int = 0,
                 adopt_key_ids: bool = False) -> None:
        self._org_key = org_key
        self._now_ms = now_ms
        self._adopt = bool(adopt_key_ids)
        # pricers in use; strong references keep id() stable for the index
        self._pricers: list[Pricer] = []
        self._pricer_ids: dict[int, int] = {}
        self._default: int | None = self._pidx(pricer) if pricer is not None else None
        self._contrib_pricer: dict[tuple[str, str], int | None] = {}
        self._meta: dict[str, str] = {
            "schema_version": "memory@1",
            "org_key_id": key_id(org_key) if org_key else "",
            "name_key_id": name_key_id or "",
            "content_tier": "none",
            "created_ms": str(now_ms),
            "org_key_mode": "own" if org_key else "none",
            "adopted_key_id": "",
            "adopted_name_key_id": "",
        }
        self._contribs: dict[tuple[str, str], _Contribution] = {}
        self._shells: dict[str, dict[str, Lane]] = {}
        self._events: dict[str, LaneEvent] = {}
        self._aggregates: dict[str, dict[str, UsageAggregate]] = {}
        self._cost_lines: dict[str, dict[str, CostLine]] = {}
        self._outcomes: dict[tuple[str, str, str], dict[str, OutcomeAggregate]] = {}
        self._sources: dict[str, SourceInfo] = {}
        self._ingested: set[tuple[str, str]] = set()
        self._cursors: dict[tuple[str, str], tuple[int, str, int, int]] = {}
        self._findings: dict[str, dict[str, str]] = {}
        self._receipts: dict[str, ReceiptRow] = {}
        self._audit: list[tuple[int, str, str, str]] = []
        self._name_mismatch = 0
        self._principal_mismatch = 0
        self._stats: dict[tuple[str, str], tuple[str, dict[str, int]]] = {}
        self._state: _State | None = None
        # keyed by (pricer index, the hashable frozen inference itself, ts): two inferences may
        # share an id
        self._price_cache: dict[tuple[int, Inference, int], PricedInference] = {}

    def _pidx(self, pricer: Pricer) -> int:
        idx = self._pricer_ids.get(id(pricer))
        if idx is None:
            idx = self._pricer_ids[id(pricer)] = len(self._pricers)
            self._pricers.append(pricer)
        return idx

    # ---------- ingest ----------

    def _pseudonymize(self, principal: str | None, principals_ok: bool,
                      counts: dict[str, int]) -> str | None:
        if principal is None:
            return None
        if principal.startswith(("r_", "c_")):
            if not self._org_key:
                raise PrivacyError("collector principals (r_/c_) need the org key to be stored")
            counts["principals_pseudonymized"] += 1
            if principal.startswith("r_"):
                return pseudonym(self._org_key, "p", principal[2:])
            return pseudonym(self._org_key, "p", "c:" + principal[2:])
        if principals_ok:
            return principal
        counts["principals_nulled"] += 1
        return None

    def _clean_request(self, req: Request, names_ok: bool, principals_ok: bool,
                       counts: dict[str, int]) -> Request:
        attr = req.attribution
        changes: dict[str, Any] = {}
        principal = self._pseudonymize(attr.principal, principals_ok, counts)
        if principal != attr.principal:
            changes["principal"] = principal
        if not names_ok:
            for name in _HASHED_ATTR:
                if _is_hash(getattr(attr, name)):
                    changes[name] = None
                    counts["names_nulled"] += 1
            hashed_extra = [k for k, v in attr.extra if _is_hash(v)]
            if hashed_extra:
                changes["extra"] = tuple((k, v) for k, v in attr.extra if not _is_hash(v))
                counts["names_nulled"] += len(hashed_extra)
        appended = req.appended
        if not names_ok and any(_is_hash(a.name) for a in appended):
            counts["names_nulled"] += sum(1 for a in appended if _is_hash(a.name))
            appended = tuple(dataclasses.replace(a, name=None) if _is_hash(a.name) else a
                             for a in appended)
        if not changes and appended is req.appended:
            return req
        return dataclasses.replace(req, attribution=dataclasses.replace(attr, **changes),
                                   appended=appended)

    def ingest(self, result: IngestResult, *, pricer: Pricer | None = None) -> dict[str, int]:
        """Store *result* (SPEC §7.2). Re-ingesting a source with the same ``(source_id, sha256)``
        is a no-op (``skipped = 1``). Returns counts: ``requests``, ``events``, ``sessions``,
        ``aggregates``, ``cost_lines``, ``outcomes``, ``skipped``, ``principals_pseudonymized``,
        ``principals_nulled``, ``names_nulled`` and ``dq.name_key_mismatch``; the store-wide merge
        codes are in :meth:`dq_counts`."""
        if not isinstance(result, IngestResult):
            raise UsageError("ingest expects an IngestResult")
        src = result.source
        counts = {k: 0 for k in ("requests", "events", "sessions", "aggregates", "cost_lines",
                                 "outcomes", "skipped", "principals_pseudonymized",
                                 "principals_nulled", "names_nulled")}
        mark = (src.source_id, src.sha256)
        if mark in self._ingested:
            counts["skipped"] = 1
            counts["dq.name_key_mismatch"] = 0
            counts[DQ_PRINCIPAL_KEY_MISMATCH] = 0
            return counts
        adopting = self._adoption(src)
        # a keyless store opened for adoption takes its name key id from the bundle only (R-E21),
        # never from whichever source comes first (the SPEC §7.2 rule of every other store)
        keyless_adopt = self._adopt and self._org_key is None
        store_name = (self._meta["name_key_id"] if keyless_adopt
                      else self._meta["name_key_id"] or src.name_key_id or "")
        name_ids = {store_name, self._meta["adopted_name_key_id"]} - {""}
        principal_ids = {self._meta["org_key_id"], self._meta["adopted_key_id"]} - {""}
        if adopting:
            name_ids.add(src.name_key_id or "")
            principal_ids.add(src.principal_key_id or "")
        names_ok = src.name_key_id is not None and src.name_key_id in name_ids
        principals_ok = (src.principal_key_id is not None
                         and src.principal_key_id in principal_ids)
        loose = list(result.requests)
        events = list(result.events)
        shells: list[Lane] = []
        for session in result.sessions:
            for lane in session.lanes:
                loose.extend(lane.requests)
                events.extend(lane.events)
                shells.append(dataclasses.replace(lane, requests=(), events=()))
        cleaned = [self._clean_request(r, names_ok, principals_ok, counts) for r in loose]
        cost_lines = []
        for line in result.cost_lines:
            changes: dict[str, Any] = {}
            principal = self._pseudonymize(line.principal, principals_ok, counts)
            if principal != line.principal:
                changes["principal"] = principal
            if not names_ok:
                # h_ values under a name key id that is neither the store's nor the adopted one
                # (R-E21); repo / workflow are the GitHub cost-line names (C-4)
                for name in _COST_LINE_HASHED:
                    if _is_hash(getattr(line, name)):
                        changes[name] = None
                        counts["names_nulled"] += 1
            cost_lines.append(dataclasses.replace(line, **changes) if changes else line)
        aggregates = []
        for agg in result.aggregates:
            if not names_ok and any(_is_hash(v) for _, v in agg.dims):
                counts["names_nulled"] += sum(1 for _, v in agg.dims if _is_hash(v))
                agg = dataclasses.replace(agg, dims=tuple((k, v) for k, v in agg.dims
                                                          if not _is_hash(v)))
            aggregates.append(agg)
        # --- commit (nothing above has mutated the store) ---
        if adopting:
            self._adopt_from(src)
        if not self._meta["name_key_id"] and src.name_key_id and not keyless_adopt:
            self._meta["name_key_id"] = src.name_key_id
        ingest_pricer = self._pidx(pricer) if pricer is not None else self._default
        self._ingested.add(mark)
        self._sources[src.source_id] = src
        for req in cleaned:
            ref = req.source
            adapter = ref.adapter if ref is not None else src.adapter
            canon = _canonical(to_json(req))
            key = (adapter, hashlib.sha256(canon.encode()).hexdigest())
            if key in self._contribs:
                continue  # an identical contribution is already stored (and priced)
            self._contribs[key] = _Contribution(
                request=req, source_id=src.source_id, adapter=adapter,
                fidelity=ref.fidelity if ref is not None else Fidelity.FULL,
                priority=ref.priority if ref is not None else 10, canon=canon, key=key)
            self._contrib_pricer[key] = ingest_pricer
        for shell in shells:
            self._shells.setdefault(shell.lane_key, {})[_canonical(to_json(shell))] = shell
        for ev in events:
            self._events.setdefault(_canonical(to_json(ev)), ev)
        for agg in aggregates:
            self._aggregates.setdefault(agg.agg_id, {})[_canonical(to_json(agg))] = agg
        for line in cost_lines:
            self._cost_lines.setdefault(line.line_id, {})[_canonical(to_json(line))] = line
        for out in result.outcomes:
            key3 = (out.date_utc, out.team, out.source_kind)
            self._outcomes.setdefault(key3, {})[_canonical(to_json(out))] = out
        counts.update(requests=len(cleaned), events=len(events), sessions=len(result.sessions),
                      aggregates=len(aggregates), cost_lines=len(cost_lines),
                      outcomes=len(result.outcomes))
        counts["dq.name_key_mismatch"] = counts["names_nulled"] + counts["principals_nulled"]
        counts[DQ_PRINCIPAL_KEY_MISMATCH] = counts["principals_nulled"]
        self._name_mismatch += counts["dq.name_key_mismatch"]
        self._principal_mismatch += counts["principals_nulled"]
        self._stats[mark] = (src.adapter, {k: v for k, v in result.stats.items()
                                           if type(v) is int})
        self._state = None
        return counts

    def _adoption(self, src: SourceInfo) -> bool:
        """Whether ingesting *src* adopts its key ids (R-E21); a second bundle key id raises."""
        if not self._adopt or src.adapter != ADOPTABLE_ADAPTER or not src.principal_key_id:
            return False
        if src.principal_key_id in (self._meta["org_key_id"], self._meta["adopted_key_id"]):
            return False
        if self._meta["adopted_key_id"]:
            raise UsageError("this store already adopted another export key id; use the same "
                             "export key every month")
        return True

    def _adopt_from(self, src: SourceInfo) -> None:
        key = src.principal_key_id or ""
        name = src.name_key_id or ""
        self._meta["adopted_key_id"] = key
        self._meta["adopted_name_key_id"] = name
        if not self._meta["org_key_id"]:
            self._meta["org_key_id"] = key
            self._meta["org_key_mode"] = "adopted"
            if not self._meta["name_key_id"] and name:
                self._meta["name_key_id"] = name
        self.audit("store", "adopt_key_id", {"key_id": key, "name_key_id": name,
                                             "adapter": src.adapter})

    def dq_counts(self) -> dict[str, int]:
        """Store-wide data-quality counts (MemoryStore extension, not part of the protocol)."""
        state = self._merged()
        return {"dq.name_key_mismatch": self._name_mismatch,
                "dq.request_id_collision": state.collisions,
                "dq.cross_source_usage_mismatch": state.mismatches,
                DQ_PRINCIPAL_KEY_MISMATCH: self._principal_mismatch}

    def source_stats(self, *, adapter: str | None = None) -> dict[str, int]:
        """Σ of the integer ``IngestResult.stats`` values by key over every stored source, or over
        one adapter's (``LedgerStats``, C-27); sorted by key."""
        totals: dict[str, int] = {}
        for src_adapter, stats in self._stats.values():
            if adapter is not None and src_adapter != adapter:
                continue
            for key, value in stats.items():
                totals[key] = totals.get(key, 0) + value
        return dict(sorted(totals.items()))

    # ---------- the merged ledger ----------

    def _merged(self) -> _State:
        if self._state is not None:
            return self._state
        contribs = sorted(self._contribs.values(),
                          key=lambda c: (c.request.request_id, c.adapter, c.canon))
        n = len(contribs)
        uf = _UnionFind(n)
        rq_msgs: dict[str, set[str]] = {}
        for c in contribs:
            for a in c.request.attempts:
                if a.provider_request_id and a.provider_message_id:
                    rq_msgs.setdefault(a.provider_request_id, set()).add(a.provider_message_id)
        collisions = {rq for rq, msgs in rq_msgs.items() if len(msgs) > 1}
        first_by: dict[tuple[str, str], int] = {}
        for i, c in enumerate(contribs):
            keys = [("id", c.request.request_id)]
            keys += [("msg", m) for m in sorted(_message_ids(c.request))]
            keys += [("rq", q) for q in sorted(_request_hints(c.request)) if q not in collisions]
            for key in keys:
                if key in first_by:
                    uf.union(first_by[key], i)
                else:
                    first_by[key] = i
        groups: dict[int, list[_Contribution]] = {}
        for i, c in enumerate(contribs):
            groups.setdefault(uf.find(i), []).append(c)
        shells = {key: min(cands.values(), key=_shell_rank) for key, cands in self._shells.items()}
        requests: dict[str, _Merged] = {}
        mismatches = 0
        for group in groups.values():
            winner = sorted(group, key=lambda c: (-int(c.fidelity), -c.priority,
                                                  -_serving_output(c.request), c.canon))[0]
            with_msg = [c.request.request_id for c in group if _message_ids(c.request)]
            rid = min(with_msg) if with_msg else min(c.request.request_id for c in group)
            usages = {_canonical(to_json(c.request.serving_inference.usage)) for c in group
                      if c.request.serving_inference is not None}
            if len(usages) > 1:
                mismatches += 1
            mask = 0
            for c in group:
                mask |= SOURCES_MASK_BITS.get(c.adapter, 0)
            w = winner.request
            merged = Request(
                request_id=rid, session_key=w.session_key, lane_key=w.lane_key, seq=w.seq,
                attribution=_merge_attribution(group), params=_merge_params(winner, group),
                attempts=_fill_diagnostics(winner, group),
                fingerprint=w.fingerprint if w.fingerprint is not None else _first_by_priority(
                    group, lambda r: r.fingerprint),
                appended=w.appended or (_first_by_priority(group, lambda r: r.appended) or ()),
                source=w.source)
            shell = shells.get(merged.lane_key)
            requests[rid] = _Merged(request=merged, fidelity=winner.fidelity,
                                    priority=winner.priority, adapter=winner.adapter,
                                    sources_mask=mask,
                                    lane_kind=shell.kind if shell is not None else LaneKind.UNKNOWN,
                                    pricer=self._contrib_pricer.get(winner.key, self._default),
                                    members=tuple(sorted(c.key for c in group)))
        self._state = _State(requests=requests, shells=shells,
                             collisions=len(collisions), mismatches=mismatches)
        return self._state

    def _price(self, inf: Inference, ts_ms: int, pricer: int | None) -> PricedInference:
        """*inf* priced with the store's pricer number *pricer* (None: unpriced)."""
        if pricer is None:
            return PricedInference(inference_id=inf.inference_id, lines=(),
                                   figure=unpriced("no pricer"), exact_nano=0, estimated=None,
                                   unpriced_reason="no pricer")
        key = (pricer, inf, ts_ms)
        cached = self._price_cache.get(key)
        if cached is None:
            cached = self._price_cache[key] = self._pricers[pricer].price_inference(
                inf, ts_ms=ts_ms)
        return cached

    def _billed_basis(self, used: Iterable[int | None]) -> Basis:
        """The basis of billed totals over requests priced by the pricers *used*: CONTRACT when
        every one of them is a contract card, else LIST."""
        bases = {self._pricers[i].basis for i in used if i is not None}
        return Basis.CONTRACT if bases == {Basis.CONTRACT} else Basis.LIST

    # ---------- dimensions and filters ----------

    @staticmethod
    def _request_dims(m: _Merged) -> dict[str, str | None]:
        r = m.request
        a = r.attribution
        si = r.serving_inference
        ctx = si.pricing if si is not None else None
        extra = dict(a.extra)
        return {
            "date": _date_of(r.ts_start_ms), "team": a.team, "cost_center": a.cost_center,
            "project": a.project, "workspace_id": a.workspace_id,
            "workload_class": a.workload_class.value, "lane_kind": m.lane_kind.value,
            "model": r.model, "agent_type": a.agent_type, "agent_product": a.agent_product,
            "repo": a.repo, "arm": a.arm, "wave": a.wave, "skill": a.skill,
            "mcp_server": a.mcp_server,
            "billing_path": a.billing_path or (ctx.billing_path if ctx is not None else None),
            "channel": ctx.channel if ctx is not None else None,
            "provider": ctx.provider if ctx is not None else None,
            "billing_class": billing_class(a.billing_path or (
                ctx.billing_path if ctx is not None else None)),
            "principal": a.principal, "mdm_group": extra.get("mdm_group"),
            "gateway": extra.get("gateway"),
        }

    @staticmethod
    def _inference_dims(req_dims: Mapping[str, str | None], inf: Inference,
                        ts_ms: int) -> dict[str, str | None]:
        dims = dict(req_dims)
        dims.update(date=_date_of(ts_ms), model=inf.pricing.model or None,
                    channel=inf.pricing.channel, provider=inf.pricing.provider,
                    billing_path=inf.pricing.billing_path,
                    billing_class=billing_class(inf.pricing.billing_path))
        return dims

    @staticmethod
    def _check_where(where: Mapping[str, str] | None) -> dict[str, str]:
        clause = dict(where or {})
        person = sorted(set(clause) & PERSON_DIMS)
        if person:
            raise PrivacyError(f"filtering by {', '.join(person)} is not allowed")
        unknown = sorted(set(clause) - _WHERE_KEYS)
        if unknown:
            raise UsageError(f"unknown filter key(s): {', '.join(unknown)}")
        return clause

    @staticmethod
    def _match(dims: Mapping[str, str | None], clause: Mapping[str, str]) -> bool:
        for key, value in clause.items():
            actual = dims.get(key)
            if value == "":
                if actual is not None:
                    return False
            elif actual != value:
                return False
        return True

    def _in_window(self, since_ms: int | None, until_ms: int | None) -> list[_Merged]:
        lo = since_ms if since_ms is not None else 0
        hi = until_ms if until_ms is not None else _FOREVER_MS
        rows = [m for m in self._merged().requests.values() if lo <= m.request.ts_start_ms < hi]
        rows.sort(key=lambda m: (m.request.lane_key, m.request.ts_start_ms, m.request.seq,
                                 m.request.request_id))
        return rows

    def _billable(self, m: _Merged) -> Iterator[tuple[Attempt, Inference]]:
        for att in m.request.attempts:
            for inf in att.inferences:
                if inf.billable is not False:
                    yield att, inf

    # ---------- protocol: reading lanes and requests ----------

    def reprice(self, pricer: Pricer, *, since_ms: int | None = None,
                until_ms: int | None = None) -> int:
        """Re-price the requests starting in ``[since_ms, until_ms)`` (default: all) with *pricer*
        (later ingests keep using their own pricer); returns the number of billable inferences
        re-priced."""
        idx = self._pidx(pricer)
        rows = self._in_window(since_ms, until_ms)
        for m in rows:
            for key in m.members:
                self._contrib_pricer[key] = idx
        self._state = None
        return sum(1 for m in rows for _ in self._billable(m))

    _LANE_KEYS = frozenset({"team", "lane_kind", "billing_class"})

    def iter_lanes(self, *, since_ms: int | None = None, until_ms: int | None = None,
                   where: Mapping[str, str] | None = None,
                   lane_keys: Collection[str] | None = None) -> Iterator[Lane]:
        """Lanes (ordered by lane key) assembled from the merged requests in ``[since, until)``,
        their events in the window and the lane shells. ``team``, ``lane_kind`` and
        ``billing_class`` filter lanes (by their first request); other keys filter requests."""
        clause = self._check_where(where)
        lane_clause = {k: v for k, v in clause.items() if k in self._LANE_KEYS}
        req_clause = {k: v for k, v in clause.items() if k not in self._LANE_KEYS}
        wanted = set(lane_keys) if lane_keys is not None else None
        state = self._merged()
        by_lane: dict[str, list[_Merged]] = {}
        for m in self._in_window(since_ms, until_ms):
            if wanted is not None and m.request.lane_key not in wanted:
                continue
            if req_clause and not self._match(self._request_dims(m), req_clause):
                continue
            by_lane.setdefault(m.request.lane_key, []).append(m)
        lo = since_ms if since_ms is not None else 0
        hi = until_ms if until_ms is not None else _FOREVER_MS
        events = [e for e in self._events.values() if e.lane_key in by_lane and lo <= e.ts_ms < hi]
        sessions: dict[str, list[Lane]] = {}
        for key in by_lane:
            shell = state.shells.get(key)
            if shell is not None:
                sessions.setdefault(shell.session_key, []).append(shell)
        wrapped = [Session(session_key=skey, source_kind="memory", attribution=Attribution(),
                           lanes=tuple(sorted(lanes, key=lambda s: s.lane_key)), started_ms=0,
                           ended_ms=0)
                   for skey, lanes in sorted(sessions.items())]
        requests = [m.request for ms in by_lane.values() for m in ms]
        lanes = [lane for lane in group_lanes(requests, events, wrapped) if lane.requests]
        lanes.sort(key=lambda lane: lane.lane_key)
        for lane in lanes:
            dims = {"team": lane.team, "lane_kind": lane.kind.value,
                    "billing_class": lane.billing_class}
            if self._match(dims, lane_clause):
                yield lane

    def iter_requests(self, *, since_ms: int | None = None, until_ms: int | None = None,
                      where: Mapping[str, str] | None = None) -> Iterator[Request]:
        """Merged requests in ``[since, until)`` matching *where*, ordered by (ts, lane, seq,
        id)."""
        clause = self._check_where(where)
        rows = [m for m in self._in_window(since_ms, until_ms)
                if self._match(self._request_dims(m), clause)]
        rows.sort(key=lambda m: (m.request.ts_start_ms, m.request.lane_key, m.request.seq,
                                 m.request.request_id))
        for m in rows:
            yield m.request

    def iter_usage_records(self, *, since_ms: int | None = None,
                           until_ms: int | None = None) -> Iterator[UsageRecord]:
        """One UsageRecord per billable inference (``billable`` not False), by attempt start."""
        lo = since_ms if since_ms is not None else 0
        hi = until_ms if until_ms is not None else _FOREVER_MS
        rows = sorted(self._merged().requests.values(),
                      key=lambda m: (m.request.ts_start_ms, m.request.lane_key, m.request.seq,
                                     m.request.request_id))
        for m in rows:
            r = m.request
            for att, inf in self._billable(m):
                if not lo <= att.ts_start_ms < hi:
                    continue
                yield UsageRecord(
                    inference_id=inf.inference_id, request_id=r.request_id,
                    attempt_id=att.attempt_id, session_key=r.session_key, lane_key=r.lane_key,
                    lane_kind=m.lane_kind, ts_ms=att.ts_start_ms,
                    date_utc=_date_of(att.ts_start_ms),
                    kind=inf.kind, usage_source=inf.usage_source, billable=inf.billable,
                    billing_rule_id=inf.billing_rule_id, pricing=inf.pricing, usage=inf.usage,
                    attribution=r.attribution, fidelity=m.fidelity)

    def lane_index(self, *, since_ms: int, until_ms: int) -> Iterator[LaneIndexRow]:
        """One row per lane with requests in the window: team, kind, billing class, request count
        and the point priced nano of its billable inferences (unpriced counts 0)."""
        state = self._merged()
        for lane in self.iter_lanes(since_ms=since_ms, until_ms=until_ms):
            point = 0
            for req in lane.requests:
                pricer = state.requests[req.request_id].pricer
                for att in req.attempts:
                    for inf in att.inferences:
                        if inf.billable is False:
                            continue
                        nano = self._price(inf, att.ts_start_ms, pricer).figure.nano
                        point += nano or 0
            yield LaneIndexRow(lane_key=lane.lane_key, team=lane.team, lane_kind=lane.kind.value,
                               billing_class=lane.billing_class, requests=len(lane.requests),
                               point_nano=point)

    def lane_first_reads(self, *, since_ms: int,
                         until_ms: int) -> Iterator[tuple[str, str, int]]:
        """``(cache_scope_key, model, R)`` of each lane's first request with a serving inference."""
        for lane in self.iter_lanes(since_ms=since_ms, until_ms=until_ms):
            for req in lane.requests:
                si = req.serving_inference
                if si is not None:
                    yield (lane.cache_scope_key, req.model, si.usage.cache_read)
                    break

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str],
                    source: str = "requests") -> int:
        """Distinct principals (never ids) over requests matching *where*, or with
        ``source="cost_lines"`` over the stored cost lines dated in the window (``where`` keys
        team, cost_center, channel, model, sku, workspace_id, cost_type; ``""`` matches None)."""
        if source == "cost_lines":
            clause = dict(where or {})
            person = sorted(set(clause) & PERSON_DIMS)
            if person:
                raise PrivacyError(f"filtering by {', '.join(person)} is not allowed")
            unknown = sorted(set(clause) - _COST_LINE_WHERE_KEYS)
            if unknown:
                raise UsageError(f"unknown cost-line filter key(s): {', '.join(unknown)}")
            lines = self.cost_lines(since_ms=since_ms, until_ms=until_ms)
            return len({c.principal for c in lines if c.principal is not None and self._match(
                {k: getattr(c, k) for k in _COST_LINE_WHERE_KEYS}, clause)})
        if source != "requests":
            raise UsageError(f"unknown count source {source!r} (requests | cost_lines)")
        clause = self._check_where(where)
        users = {m.request.attribution.principal for m in self._in_window(since_ms, until_ms)
                 if m.request.attribution.principal is not None
                 and self._match(self._request_dims(m), clause)}
        return len(users)

    # ---------- protocol: provider-side records ----------

    @staticmethod
    def _window(window: Mapping[str, int]) -> tuple[int, int]:
        unknown = set(window) - {"since_ms", "until_ms"}
        if unknown:
            raise UsageError(f"unknown window key(s): {', '.join(sorted(unknown))}")
        lo = window.get("since_ms")
        hi = window.get("until_ms")
        return (lo if lo is not None else 0, hi if hi is not None else _FOREVER_MS)

    @staticmethod
    def _choose(cands: Mapping[str, Any]) -> Any:
        """One stored version per provider id: a ``final`` version over a provisional one, then the
        most recently fetched, then the canonically largest (deterministic, order-independent).
        Copilot records: the most recently fetched, then the canonically largest (addendum §7.1)."""
        if any(_latest_fetch_wins(c) for c in cands.values()):
            best = max(cands, key=lambda canon: (getattr(cands[canon], "fetched_ms", 0), canon))
        else:
            best = max(cands, key=lambda canon: (getattr(cands[canon], "finality", "") == "final",
                                                 getattr(cands[canon], "fetched_ms", 0), canon))
        return cands[best]

    def aggregates(self, source_kind: str | None = None, **window: int) -> list[UsageAggregate]:
        """Stored aggregates (one per ``agg_id``) whose bucket starts in the window."""
        lo, hi = self._window(window)
        out = [self._choose(c) for c in self._aggregates.values()]
        out = [a for a in out if (source_kind is None or a.source_kind == source_kind)
               and lo <= a.bucket_start_ms < hi]
        return sorted(out, key=lambda a: (a.bucket_start_ms, a.agg_id))

    def cost_lines(self, source_kind: str | None = None, **window: int) -> list[CostLine]:
        """Stored cost lines (one per ``line_id``) dated inside the window."""
        lo, hi = self._window(window)
        out = [self._choose(c) for c in self._cost_lines.values()]
        out = [c for c in out if (source_kind is None or c.source_kind == source_kind)
               and _day_overlaps(c.date_utc, lo, hi)]
        return sorted(out, key=lambda c: (c.date_utc, c.line_id))

    def outcomes(self, **window: int) -> list[OutcomeAggregate]:
        """Stored outcome aggregates dated inside the window."""
        lo, hi = self._window(window)
        out = [self._choose(c) for c in self._outcomes.values()]
        out = [o for o in out
               if _day_overlaps(o.date_utc, lo, hi)]
        return sorted(out, key=lambda o: (o.date_utc, o.team, o.source_kind))

    # ---------- protocol: aggregates for publication ----------

    def aggregate(self, *, since_ms: int, until_ms: int, group_by: Sequence[str],
                  where: Mapping[str, str] | None = None,
                  pricer: Pricer | None = None) -> RawAggregate:
        """Group billable inferences by whitelisted dimensions (``n_users`` = distinct principals).
        ``principal``/``session`` in *group_by* → ``PrivacyError``; unknown dims →
        ``UsageError``."""
        dims_by = tuple(group_by)
        person = sorted(set(dims_by) & PERSON_DIMS)
        if person:
            raise PrivacyError(f"group by {', '.join(person)} is not allowed")
        unknown = [d for d in dims_by if d not in _AGG_DIMS]
        if unknown:
            raise UsageError(f"unknown group-by dimension(s): {', '.join(unknown)}")
        clause = self._check_where(where)
        groups: dict[tuple, dict[str, Any]] = {}
        for m in self._in_window(since_ms, until_ms):
            req_dims = self._request_dims(m)
            for att, inf in self._billable(m):
                dims = self._inference_dims(req_dims, inf, att.ts_start_ms)
                if not self._match(dims, clause):
                    continue
                key = tuple((d, dims.get(d)) for d in dims_by)
                g = groups.setdefault(key, {"users": set(), "requests": set(), "items": [],
                                            "usage": UsageBuckets(), "pricers": set()})
                if m.request.attribution.principal is not None:
                    g["users"].add(m.request.attribution.principal)
                g["requests"].add(m.request.request_id)
                if pricer is not None:
                    p = pricer.price_inference(inf, ts_ms=att.ts_start_ms)
                else:
                    p = self._price(inf, att.ts_start_ms, m.pricer)
                    g["pricers"].add(m.pricer)
                g["items"].append((inf, p))
                g["usage"] = _add_usage(g["usage"], inf.usage)
        rows = []
        for key in sorted(groups, key=lambda k: tuple((v is None, v or "") for _, v in k)):
            g = groups[key]
            if pricer is not None:
                basis = pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST
            else:
                basis = self._billed_basis(g["pricers"])
            priced = _price_total(g["items"], basis)
            rows.append(AggRow(dims=key, n_users=len(g["users"]), n_requests=len(g["requests"]),
                               usage=g["usage"], priced=priced))
        return RawAggregate(group_by=dims_by, rows=tuple(rows), window=(since_ms, until_ms))

    def cluster_days(self, *, cluster_kind: str, since: str, until: str) -> list[ClusterDay]:
        """Per (date, cluster, arm, wave) over ``[since, until)``: active users (distinct principals
        of requests with a billable inference), requests, billed-basis exact nano, list-equivalent
        point nano of the subscription path (``allowance_nano``) and of the Copilot paths
        (``pool_nano``). Cluster kinds: ``team``, ``workspace``, ``mdm_group``, ``gateway``."""
        if cluster_kind not in _CLUSTER_KINDS:
            raise UsageError(f"unknown cluster kind {cluster_kind!r}")
        field = {"team": "team", "workspace": "workspace_id", "mdm_group": "mdm_group",
                 "gateway": "gateway"}[cluster_kind]
        cells: dict[tuple[str, str, str | None, str | None], dict[str, Any]] = {}
        for m in self._in_window(_date_start_ms(since), _date_start_ms(until)):
            billable = list(self._billable(m))
            if not billable:
                continue
            dims = self._request_dims(m)
            cid = dims.get(field)
            if cid is None:
                continue
            a = m.request.attribution
            cell = cells.setdefault((_date_of(m.request.ts_start_ms), cid, a.arm, a.wave),
                                    {"users": set(), "requests": 0, "exact": 0, "allow": 0,
                                     "pool": 0})
            if a.principal is not None:
                cell["users"].add(a.principal)
            cell["requests"] += 1
            for att, inf in billable:
                p = self._price(inf, att.ts_start_ms, m.pricer)
                if p.figure.nano is None:
                    continue
                if p.figure.basis is Basis.LIST_EQUIVALENT:
                    slot = "pool" if inf.pricing.billing_path in COPILOT_BILLING_PATHS else "allow"
                    cell[slot] += p.figure.nano
                else:
                    cell["exact"] += p.exact_nano
        return [ClusterDay(date_utc=d, cluster_kind=cluster_kind, cluster_id=cid, arm=arm,
                           wave=wave, active_users=len(c["users"]), requests=c["requests"],
                           exact_nano=c["exact"], allowance_nano=c["allow"], pool_nano=c["pool"])
                for (d, cid, arm, wave), c in sorted(
                    cells.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "",
                                                   kv[0][3] or ""))]

    def cost_rows(self, *, since_ms: int, until_ms: int,
                  group_by: Sequence[str]) -> list[LedgerCostRow]:
        """One row per group × bucket × basis of the priced lines of billable inferences (exact per
        bucket sums). Dimensions not grouped are ``""`` (str fields) or None."""
        dims_by = tuple(group_by)
        person = sorted(set(dims_by) & PERSON_DIMS)
        if person:
            raise PrivacyError(f"group by {', '.join(person)} is not allowed")
        unknown = [d for d in dims_by if d not in _COST_DIMS]
        if unknown:
            raise UsageError(f"unknown cost-row dimension(s): {', '.join(unknown)}")
        cells: dict[tuple, dict[str, Any]] = {}
        for m in self._in_window(since_ms, until_ms):
            req_dims = self._request_dims(m)
            for att, inf in self._billable(m):
                p = self._price(inf, att.ts_start_ms, m.pricer)
                if p.figure.nano is None:
                    continue
                dims = self._inference_dims(req_dims, inf, att.ts_start_ms)
                gkey = tuple(dims.get(d) for d in dims_by)
                for line in p.lines:
                    key = (gkey, line.bucket, p.figure.basis.value)
                    c = cells.setdefault(key, {"qty": 0, "exact": 0, "lo": 0, "hi": 0,
                                               "users": set(), "rows": set()})
                    c["qty"] += line.quantity
                    if line.exact:
                        c["exact"] += line.amount_nano
                    else:
                        c["lo"] += line.low_nano or 0
                        c["hi"] += line.high_nano or 0
                    if m.request.attribution.principal is not None:
                        c["users"].add(m.request.attribution.principal)
                    c["rows"].add(line.rate_row_id)
        out = []
        for (gkey, bucket, basis), c in sorted(
                cells.items(), key=lambda kv: (tuple(v or "" for v in kv[0][0]), kv[0][1],
                                               kv[0][2])):
            vals = dict(zip(dims_by, gkey, strict=True))
            out.append(LedgerCostRow(
                date_utc=vals.get("date") or "", provider=vals.get("provider") or "",
                channel=vals.get("channel") or "", model=vals.get("model") or "", bucket=bucket,
                team=vals.get("team"), cost_center=vals.get("cost_center"),
                project=vals.get("project"), workspace_id=vals.get("workspace_id"),
                lane_kind=vals.get("lane_kind") or "",
                workload_class=vals.get("workload_class") or "",
                agent_product=vals.get("agent_product"),
                billing_path=vals.get("billing_path") or "", quantity=c["qty"],
                priced_nano=c["exact"], estimated_low_nano=c["lo"], estimated_high_nano=c["hi"],
                basis=Basis(basis), rate_row_id=min(c["rows"]) if len(c["rows"]) == 1 else None,
                n_users=len(c["users"])))
        return out

    # ---------- protocol: cursors, findings, receipts, purge, audit, meta ----------

    def get_cursor(self, source_id: str, unit_hmac: str) -> tuple[int, str, int, int] | None:
        """``(byte_offset, head_sha, size, mtime_ns)`` or None."""
        return self._cursors.get((source_id, unit_hmac))

    def set_cursor(self, source_id: str, unit_hmac: str, *, byte_offset: int, head_sha: str,
                   size: int, mtime_ns: int) -> None:
        """Record the incremental-collection cursor of one unit of a source."""
        self._cursors[(source_id, unit_hmac)] = (byte_offset, head_sha, size, mtime_ns)

    def put_findings(self, run_id: str, findings: Sequence[Finding]) -> None:
        """Persist *findings* under *run_id* (JSON round trip; a later put of an id replaces it)."""
        run = self._findings.pop(run_id, {})
        for f in findings:
            run[f.finding_id] = _canonical(to_json(f))
        self._findings[run_id] = run  # re-inserted: the most recent run is last

    def findings(self, run_id: str | None = None) -> list[Finding]:
        """Findings of *run_id*, or of the most recently written run."""
        if run_id is None:
            if not self._findings:
                return []
            run_id = next(reversed(self._findings))
        return [from_json(Finding, json.loads(text))
                for text in self._findings.get(run_id, {}).values()]

    def put_receipt(self, row: ReceiptRow) -> None:
        """Store a receipt row (by ``receipt_id``)."""
        self._receipts[row.receipt_id] = row

    def receipts(self, *, lever_class: str | None = None) -> list[ReceiptRow]:
        """Receipts (optionally of one lever class) ordered by (created_ms, receipt_id)."""
        rows = [r for r in self._receipts.values()
                if lever_class is None or r.lever_class == lever_class]
        return sorted(rows, key=lambda r: (r.created_ms, r.receipt_id))

    def purge(self, *, principal: str | None = None, before_ms: int | None = None,
              actor: str) -> int:
        """Delete a person's requests (every contribution of a merged request whose principal is
        *principal*, and cost lines carrying it) or everything before *before_ms*; returns the
        number of merged requests deleted and writes an audit row without the identity."""
        if principal is None and before_ms is None:
            raise UsageError("purge needs principal or before_ms")
        state = self._merged()
        doomed: set[str] = set()
        members: set[tuple[str, str]] = set()
        for rid, m in state.requests.items():
            r = m.request
            if (principal is not None and r.attribution.principal == principal) or (
                    before_ms is not None and r.ts_start_ms < before_ms):
                doomed.add(rid)
                members.update(m.members)  # every source merged into it, however it joined
        for key, c in list(self._contribs.items()):
            r = c.request
            if (key in members
                    or (principal is not None and r.attribution.principal == principal)
                    or (before_ms is not None and r.ts_start_ms < before_ms)):
                del self._contribs[key]
                self._contrib_pricer.pop(key, None)
        if before_ms is not None:
            self._events = {k: e for k, e in self._events.items() if e.ts_ms >= before_ms}
            self._aggregates = {k: v for k, v in self._aggregates.items()
                                if self._choose(v).bucket_end_ms > before_ms}
            cut = _date_of(before_ms)
            self._cost_lines = {k: v for k, v in self._cost_lines.items()
                                if self._choose(v).date_utc >= cut}
            self._outcomes = {k: v for k, v in self._outcomes.items() if k[0] >= cut}
        if principal is not None:
            for line_id, cands in list(self._cost_lines.items()):
                kept = {k: c for k, c in cands.items() if c.principal != principal}
                if kept:
                    self._cost_lines[line_id] = kept
                else:
                    del self._cost_lines[line_id]
        self._state = None
        self._price_cache.clear()
        detail = {"by": "principal" if principal is not None else "before_ms",
                  "rows": len(doomed)}
        if before_ms is not None:
            detail["before_ms"] = before_ms
        self.audit(actor, "purge", detail)
        return len(doomed)

    def audit(self, actor: str, action: str, detail: Mapping[str, object]) -> None:
        """Append an audit row (``detail`` stored as canonical JSON)."""
        self._audit.append((self._now_ms, actor, action, _canonical(dict(detail))))

    def audit_log(self) -> list[tuple[int, str, str, str]]:
        """The audit rows ``(ts_ms, actor, action, detail_json)`` (MemoryStore extension)."""
        return list(self._audit)

    def meta(self) -> dict[str, str]:
        """``schema_version``, ``org_key_id``, ``name_key_id``, ``content_tier``, ``created_ms``,
        ``org_key_mode``, ``adopted_key_id`` and ``adopted_name_key_id`` (``""`` when none)."""
        return dict(self._meta)


# =============================================================================================
# FakeReplayer
# =============================================================================================


def _policy_key(policy: Policy) -> str:
    try:
        return policy.spec()
    except (ImportError, AttributeError):
        return policy.name


class FakeReplayer:
    """A :class:`~tokenbill.core.protocols.Replayer` whose saving per lane is a table keyed by
    ``(lane_key, policy.spec())`` (missing entries save 0), or a function (:meth:`from_function`).

    ``replay`` prices the ``baseline`` with the given pricer (Σ ``PricedInference.figure`` over the
    billable inferences of the given lanes), sums the savings of exactly the given lanes (so cohort
    subsets and shards add up), and returns ``cost = baseline − saving``. The observed policy saves
    nothing and returns ``cost == baseline``. Mixed billing classes raise ``UsageError``. Before
    F-SEM's ``core.policy`` exists the key falls back to ``policy.name``; build tables with
    :meth:`policy_key` to be independent of that.
    """

    def __init__(self, table: Mapping[tuple[str, str], int] | None = None, *,
                 fn: Callable[[Lane, Policy], int] | None = None) -> None:
        self._table = dict(table or {})
        self._fn = fn
        for (lane_key, spec), value in self._table.items():
            if not isinstance(lane_key, str) or not isinstance(spec, str) or type(value) is not int:
                raise UsageError("FakeReplayer table: (lane_key, spec) → int nano")

    @classmethod
    def from_function(cls, fn: Callable[[Lane, Policy], int]) -> FakeReplayer:
        """A FakeReplayer whose per-lane saving is ``fn(lane, policy)`` (int nano)."""
        return cls(fn=fn)

    @staticmethod
    def policy_key(policy: Policy) -> str:
        """The table key of *policy*: ``policy.spec()``, or ``policy.name`` before F-SEM lands."""
        return _policy_key(policy)

    def _saving(self, lane: Lane, policy: Policy) -> int:
        if self._fn is not None:
            value = self._fn(lane, policy)
            if type(value) is not int:
                raise ContractViolation("FakeReplayer function must return int nano")
            return value
        return self._table.get((lane.lane_key, _policy_key(policy)), 0)

    def replay(self, lanes: Sequence[Lane], policy: Policy, *, mode: str, pricer: Pricer,
               rules: Any, calibration: CalibrationReport | None,
               static_prefix_floor: Mapping[tuple[str, str], int] | None = None,
               keep_outcomes: bool = False) -> ReplayResult:
        """See the class docstring."""
        classes = {lane.billing_class for lane in lanes if lane.requests}
        if len(classes) > 1:
            raise UsageError("replay: lanes of one billing class only (billed | allowance | pool)")
        if mode not in ("documented", "calibrated"):
            raise UsageError(f"unknown replay mode {mode!r}")
        calibrated = (mode == "calibrated" and calibration is not None
                      and calibration.status == "pass")
        cal = Calibration.CALIBRATED if calibrated else Calibration.UNCALIBRATED
        basis = Basis.LIST_EQUIVALENT if classes in ({"allowance"}, {"pool"}) else (
            pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST)
        observed = policy.is_observed()
        baseline = zero(basis)
        total_saving = 0
        per_lane: list[tuple[str, int]] = []
        outcomes: list[ReplayRequestOutcome] = []
        n_requests = 0
        for lane in sorted(lanes, key=lambda lane: lane.lane_key):
            lane_base = zero(basis)
            req_points: list[tuple[Request, PricedInference | None, int | None, int | None,
                                   int | None]] = []
            for req in lane.requests:
                n_requests += 1
                req_fig = zero(basis)
                for att in req.attempts:
                    for inf in att.inferences:
                        if inf.billable is False:
                            continue
                        req_fig = add(req_fig, pricer.price_inference(inf, ts_ms=att.ts_start_ms)
                                      .figure)
                lane_base = add(lane_base, req_fig)
                req_points.append((req, None, req_fig.nano, req_fig.low_nano, req_fig.high_nano))
            baseline = add(baseline, lane_base)
            saving = 0 if observed else self._saving(lane, policy)
            total_saving += saving
            base_nano = lane_base.nano if lane_base.nano is not None else 0
            per_lane.append((lane.lane_key, base_nano - saving))
            if keep_outcomes:
                for i, (req, _, point, lo, hi) in enumerate(req_points):
                    last = i == len(req_points) - 1
                    delta = saving if last else 0
                    si = req.serving_inference
                    outcomes.append(ReplayRequestOutcome(
                        request_id=req.request_id,
                        usage=si.usage if si is not None else UsageBuckets(), extra=(),
                        cost_nano=None if point is None else point - delta,
                        low_nano=None if lo is None else lo - delta,
                        high_nano=None if hi is None else hi - delta,
                        changed=delta != 0))
        if observed:
            cost = baseline
            saving_fig = zero(basis) if baseline.nano is not None else unpriced(
                "baseline unpriced", basis)
        else:
            saving_fig = estimated(total_saving, basis, calibration=cal,
                                   note="FakeReplayer table saving")
            cost = sub(baseline, saving_fig)
        return ReplayResult(
            policy=policy, mode="calibrated" if calibrated else "documented", baseline=baseline,
            cost=cost, saving=saving_fig, per_lane=tuple(per_lane),
            outcomes=tuple(outcomes) if keep_outcomes else None,
            assumptions=("FakeReplayer: savings from a fixed table",), calibration=cal,
            added_calls=0, keepalive_pings=0, lanes_skipped=(), n_lanes=len(lanes),
            n_requests=n_requests)


# =============================================================================================
# conformance: pricer
# =============================================================================================

_TS_2026_09_10 = _ts("2026-09-10")
_TS_2026_09_23 = _date_start_ms("2026-09-23")


def _row_matches(pricer: Pricer, model: str, channel: str, date: str) -> bool:
    """Whether *pricer* resolves (*model*, *channel*) on *date* to the verified facts rates."""
    facts = load_facts()
    rows = [r for r in facts.rows_for(model, channel) if r.enabled
            and r.effective_from <= date and (r.effective_to is None or date < r.effective_to)]
    if not rows:
        return False
    provider = "openai" if channel.startswith("openai") else "anthropic"
    ctx = PricingContext(provider=provider, channel=channel, model=model, model_raw=model,
                         endpoint_scope="global" if channel in ("bedrock", "vertex") else "unknown")
    resolved = pricer.resolve(ctx, ts_ms=_ts(date))
    return (resolved is not None and resolved.input == rows[0].input_usd_per_mtok
            and resolved.output == rows[0].output_usd_per_mtok)


def _ctx(model: str, **kw: Any) -> PricingContext:
    provider = kw.pop("provider", "openai" if model.startswith("gpt-") else "anthropic")
    channel = kw.pop("channel", "openai_api" if provider == "openai" else "anthropic_api")
    return PricingContext(provider=provider, channel=channel, model=model, model_raw=model, **kw)


_CASE1 = UsageBuckets(uncached_input=1000, cache_read=100_000, cache_write_5m=2000,
                      cache_write_1h=3000, output=500)


def _check(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)


def _golden_cases(pricer: Pricer) -> list[str]:
    """SPEC §6.9 cases for the rows *pricer* carries; returns the case ids checked."""
    checked: list[str] = []
    day = "2026-09-23"
    ts = _ts(day)

    def price(usage: UsageBuckets, ctx: PricingContext, when: int = ts,
              **kw: Any) -> PricedInference:
        return pricer.price_usage(usage, ctx, ts_ms=when, **kw)

    if _row_matches(pricer, "claude-opus-5-5", "anthropic_api", day):
        opus = _ctx("claude-opus-5-5")
        p = price(_CASE1, opus)
        _check(p.figure.nano == 68_000_000 and p.figure.evidence is Evidence.EXACT, "§6.9 case 1")
        p = price(_CASE1, dataclasses.replace(opus, inference_geo="us"))
        _check(p.figure.nano == 74_800_000, "§6.9 case 2")
        p = price(_CASE1, dataclasses.replace(opus, service_tier="batch"))
        _check(p.figure.nano == 34_000_000, "§6.9 case 3 (batch)")
        p = price(_CASE1, dataclasses.replace(opus, service_tier="batch", inference_geo="us"))
        _check(p.figure.nano == 37_400_000, "§6.9 case 3 (batch + US geo)")
        p = price(_CASE1, dataclasses.replace(opus, speed="fast"))
        _check(p.figure.nano == 136_000_000, "§6.9 case 4")
        web = price(dataclasses.replace(_CASE1, web_search_requests=3), opus)
        _check(web.figure.nano == 68_000_000 + 30_000_000, "§6.9 case 6")
        p = price(UsageBuckets(uncached_input=1000, cache_write_unknown=1_000_000, output=500),
                  opus)
        _check(p.exact_nano == 14_000_000 and p.estimated is not None
               and (p.estimated.low_nano, p.estimated.high_nano) == (5_000_000_000, 8_000_000_000)
               and p.figure.evidence is Evidence.ESTIMATED, "§6.9 case 9")
        p = price(_CASE1, dataclasses.replace(opus, billing_path="subscription"))
        _check(p.figure.nano == 68_000_000 and p.figure.basis is Basis.LIST_EQUIVALENT
               and not p.figure.is_billed_eligible, "§6.9 case 23")
        p = pricer.price_usage(UsageBuckets(cache_read=100_000, output=3), opus, ts_ms=ts,
                               usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=403)
        out = [ln for ln in p.lines if ln.bucket == "output"]
        _check(p.exact_nano == 20_000_000 and len(out) == 1 and not out[0].exact
               and (out[0].amount_nano, out[0].low_nano, out[0].high_nano)
               == (60_000, 60_000, 8_060_000), "§6.9 case 24")
        p = price(UsageBuckets(cache_write_1h=1_000_000),
                  dataclasses.replace(opus, service_tier="batch", inference_geo="us"))
        _check(p.figure.nano == 4_400_000_000, "§6.9 case 18")
        p = price(UsageBuckets(output=1), opus, _ts("2026-09-21"))
        _check(p.figure.nano is None and p.unpriced_reason is not None, "§6.9 case 17")
        p = price(UsageBuckets(output=1), _ctx("claude-foo-9"))
        _check(p.figure.nano is None and p.unpriced_reason is not None
               and p.exact_nano == 0 and p.estimated is None, "§6.9 case 15")
        checked += ["1", "2", "3", "4", "6", "9", "15", "17", "18", "23", "24"]
    if _row_matches(pricer, "claude-fable-5-1", "anthropic_api", "2026-09-10") and _row_matches(
            pricer, "claude-fable-5", "anthropic_api", "2026-09-10"):
        reads = UsageBuckets(cache_read=1_000_000)
        a = price(reads, _ctx("claude-fable-5-1"), _TS_2026_09_10).figure.nano
        b = price(reads, _ctx("claude-fable-5"), _TS_2026_09_10).figure.nano
        _check((a, b) == (250_000_000, 1_000_000_000), "§6.9 case 5")
        p = price(reads, _ctx("claude-fable-5-1"), _ts("2026-08-20"))
        _check(p.figure.nano is None, "§6.9 case 22")
        checked += ["5", "22"]
    if _row_matches(pricer, "claude-opus-5", "bedrock", day):
        br = _ctx("claude-opus-5", channel="bedrock")
        inp = UsageBuckets(uncached_input=1_000_000)
        g = price(inp, dataclasses.replace(br, endpoint_scope="global"))
        r = price(inp, dataclasses.replace(br, endpoint_scope="regional"))
        u = price(inp, dataclasses.replace(br, endpoint_scope="unknown"))
        _check(g.figure.nano == 5_000_000_000 and g.figure.evidence is Evidence.EXACT,
               "§6.9 case 7 (global)")
        _check(r.figure.nano == 5_500_000_000, "§6.9 case 7 (regional)")
        _check(u.figure.evidence is Evidence.ESTIMATED
               and (u.figure.low_nano, u.figure.high_nano) == (5_000_000_000, 5_500_000_000),
               "§6.9 case 7 (scope unknown)")
        checked.append("7")
    if _row_matches(pricer, "gpt-5.6-sol", "openai_api", "2026-09-10"):
        sol = _ctx("gpt-5.6-sol")
        usage = UsageBuckets(uncached_input=2000, cache_read=6000, cache_write_other=2000,
                             cache_write_other_ttl_s=1800, output=1000)
        _check(price(usage, sol, _TS_2026_09_10).figure.nano == 40_400_000, "§6.9 case 10")
        big = UsageBuckets(uncached_input=300_000, output=1000)
        _check(price(big, sol, _TS_2026_09_10).figure.nano == 2_430_000_000, "§6.9 case 11")
        _check(price(big, sol, _ts("2026-08-01")).figure.nano is None, "§6.9 case 21 (launch)")
        _check(price(big, sol, _ts("2026-11-22")).figure.nano is None,
               "§6.9 case 21 (promotion expired)")
        checked += ["10", "11", "21"]
    checked += _copilot_golden_cases(pricer)
    return checked


def _copilot_row_matches(pricer: Pricer, model: str, date: str) -> bool:
    """Whether *pricer* resolves the Copilot row of *model* on *date* at the facts rates."""
    rows = [r for r in load_facts().copilot.rate_rows if r.model == model and r.enabled
            and r.effective_from <= date and (r.effective_to is None or date < r.effective_to)]
    if not rows:  # pragma: no cover - facts carry every model the goldens use
        return False
    resolved = pricer.resolve(_copilot_ctx(model), ts_ms=_ts(date))
    return (resolved is not None and resolved.input == rows[0].input_usd_per_mtok
            and resolved.output == rows[0].output_usd_per_mtok)


def _copilot_ctx(model: str, **kw: Any) -> PricingContext:
    return PricingContext(provider="github", channel=_COPILOT_CHANNEL, model=model,
                          model_raw=model, billing_path=kw.pop("billing_path", "copilot_pool"),
                          **kw)


def _copilot_golden_cases(pricer: Pricer) -> list[str]:
    """Copilot addendum Appendix C.G1–G10, G14–G16 for a pricer that carries the Copilot rows at
    the facts rates (every figure LIST_EQUIVALENT); returns the case ids checked. A contract card
    (basis CONTRACT) skips them like the SPEC cases."""
    if pricer.basis is not Basis.LIST or not _copilot_row_matches(pricer, "claude-opus-5-5",
                                                                     "2026-09-23"):
        return []

    def price(usage: UsageBuckets, model: str, date: str, **kw: Any) -> Figure:
        p = pricer.price_usage(usage, _copilot_ctx(model, **kw), ts_ms=_ts(date))
        _check(p.figure.basis is Basis.LIST_EQUIVALENT, "Copilot figures are LIST_EQUIVALENT")
        return p.figure

    def rng(f: Figure) -> tuple[int | None, int | None, int | None]:
        return (f.nano, f.low_nano, f.high_nano)

    g1 = UsageBuckets(uncached_input=12_000, cache_read=180_000, cache_write_unknown=6000,
                      output=3000)
    f = price(g1, "claude-opus-5-5", "2026-09-23")
    _check(rng(f) == (174_000_000, 174_000_000, 192_000_000)
           and f.evidence is Evidence.ESTIMATED, "C.G1")
    _check(price(g1, "claude-opus-5-5", "2026-09-23", write_ttl_hint="1h").nano == 192_000_000,
           "C.G1 (1h hint)")
    _check(rng(price(g1, "claude-opus-5-5", "2026-09-23", routing="auto"))
           == (156_600_000, 156_600_000, 172_800_000), "C.G2")
    g3 = price(g1, "claude-opus-5-5", "2026-09-23", compliance="data_residency")
    _check((g3.nano, g3.high_nano) == (191_400_000, 211_200_000), "C.G3")
    g4 = price(g1, "claude-opus-5-5", "2026-09-23", routing="auto", compliance="data_residency")
    _check((g4.nano, g4.high_nano) == (172_260_000, 190_080_000), "C.G4")
    big = UsageBuckets(uncached_input=20_000, cache_read=280_000, output=4000)
    small = UsageBuckets(uncached_input=20_000, cache_read=250_000, output=4000)
    g5 = price(big, "gpt-5.5", "2026-09-10")
    g5s = price(small, "gpt-5.5", "2026-09-10")
    _check(g5.nano == 660_000_000 and g5.evidence is Evidence.EXACT
           and g5s.nano == 345_000_000 and g5s.evidence is Evidence.EXACT, "C.G5")
    g5b = price(small, "gpt-5.5", "2026-09-10", context_tier="long_context")
    _check(rng(g5b) == (345_000_000, 345_000_000, 630_000_000)
           and g5b.evidence is Evidence.ESTIMATED, "C.G5b")
    g6 = UsageBuckets(uncached_input=2000, cache_read=6000, cache_write_unknown=2000, output=1000)
    _check(price(g6, "gpt-5.6-sol", "2026-09-10").nano == 40_400_000
           and price(g6, "gpt-5.6-sol", "2026-08-25").nano == 20_200_000, "C.G6")
    _check(price(g6, "gpt-5.6-sol", "2026-08-20").nano == 27_750_000, "C.G7")
    g8 = UsageBuckets(uncached_input=10_000, cache_read=90_000, output=2000)
    fast = price(g8, "claude-opus-4-8", "2026-09-23", speed="fast")
    std = price(g8, "claude-opus-4-8", "2026-09-23")
    _check((fast.nano, std.nano) == (290_000_000, 145_000_000)
           and fast.evidence is Evidence.EXACT, "C.G8")
    g8b = dataclasses.replace(g8, cache_write_unknown=5000)
    # the same unknown TTL on both sides: the premium range is taken bound by bound
    fb = rng(price(g8b, "claude-opus-4-8", "2026-09-23", speed="fast"))
    sb = rng(price(g8b, "claude-opus-4-8", "2026-09-23"))
    _check(tuple((x or 0) - (y or 0) for x, y in zip(fb, sb, strict=True))
           == (176_250_000, 176_250_000, 195_000_000), "C.G8b")
    g9 = UsageBuckets(uncached_input=100_000, cache_read=400_000, output=10_000)
    _check(price(g9, "gemini-3.8-flash", "2026-09-23").nano == 142_500_000
           and price(g9, "gemini-3.8-flash", "2027-01-01").nano is None, "C.G9")
    g10 = price(UsageBuckets(uncached_input=50_000, cache_read=160_000, output=5000), "grok-4.7",
                "2026-09-23")
    _check(g10.nano == 420_000_000 and g10.evidence is Evidence.EXACT, "C.G10")
    _check(price(UsageBuckets(cache_read=1_000_000), "kimi-k2.7-code", "2026-09-23").nano
           == 190_000_000, "C.G14")
    g15 = price(UsageBuckets(cache_write_unknown=1000), "gpt-5.3-codex", "2026-09-23")
    _check(g15.nano == 1_750_000 and g15.evidence is Evidence.ESTIMATED, "C.G15")
    _check(price(g1, "claude-opus-5-5", "2026-09-21").nano is None, "C.G16")
    return ["C.G1", "C.G2", "C.G3", "C.G4", "C.G5", "C.G5b", "C.G6", "C.G7", "C.G8", "C.G8b",
            "C.G9", "C.G10", "C.G14", "C.G15", "C.G16"]


def _rule_checks(pricer: Pricer, ctx: PricingContext, ts: int) -> None:
    """Per-line exactness rules (SPEC §6.3) expressed through the pricer's own point prices."""
    def pt(usage: UsageBuckets, c: PricingContext = ctx) -> int:
        nano = pricer.price_usage(usage, c, ts_ms=ts).figure.nano
        assert nano is not None
        return nano

    full = pricer.price_usage(_CASE1, ctx, ts_ms=ts)
    _check(full.figure.evidence is Evidence.EXACT and all(ln.exact for ln in full.lines)
           and full.estimated is None and full.figure.nano == full.exact_nano
           == sum(ln.amount_nano for ln in full.lines), "exact usage prices exactly")
    for ln in full.lines:
        if ln.bucket != "web_search":
            _check(ln.amount_nano == token_nano(ln.quantity, Decimal(ln.unit_usd_per_mtok)),
                   f"line {ln.bucket} is quantity × unit rate rounded once")
    unk = UsageBuckets(uncached_input=1000, cache_write_unknown=40_000, output=100)
    p = pricer.price_usage(unk, ctx, ts_ms=ts)
    line = [ln for ln in p.lines if ln.bucket == "cache_write_unknown"]
    _check(len(line) == 1 and not line[0].exact
           and line[0].low_nano == pt(UsageBuckets(cache_write_5m=40_000))
           and line[0].high_nano == pt(UsageBuckets(cache_write_1h=40_000)),
           "unknown-TTL writes are a [5m, 1h] range")
    _check(all(ln.exact for ln in p.lines if ln.bucket != "cache_write_unknown")
           and p.figure.evidence is Evidence.ESTIMATED, "only the unknown-TTL line is a range")
    ph = pricer.price_usage(UsageBuckets(uncached_input=500, output=7), ctx, ts_ms=ts,
                            usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=900)
    out = [ln for ln in ph.lines if ln.bucket == "output"]
    _check(len(out) == 1 and not out[0].exact and out[0].amount_nano == out[0].low_nano
           == pt(UsageBuckets(output=7)) and out[0].high_nano == pt(UsageBuckets(output=900)),
           "placeholder output is a [logged, upper] range with point logged")
    _check(all(ln.exact for ln in ph.lines if ln.bucket != "output"),
           "placeholder input lines stay exact")
    unc = pricer.price_usage(_CASE1, ctx, ts_ms=ts, billable=None)
    _check(all(not ln.exact and ln.low_nano == 0 and ln.high_nano == ln.amount_nano
               for ln in unc.lines) and unc.figure.nano == full.figure.nano
           and unc.figure.low_nano == 0, "uncertain billing is a [0, full] range")
    no = pricer.price_usage(_CASE1, ctx, ts_ms=ts, billable=False)
    _check(no.figure.nano == 0 and no.figure.evidence is Evidence.EXACT
           and all(ln.amount_nano == 0 for ln in no.lines), "billable False is EXACT 0")
    est = pricer.price_usage(_CASE1, ctx, ts_ms=ts, usage_source=UsageSource.ESTIMATED)
    _check(est.figure.evidence is Evidence.ESTIMATED and est.figure.nano == full.figure.nano
           and all(not ln.exact for ln in est.lines), "reconstructed usage is ESTIMATED")
    part = pricer.price_usage(_CASE1, ctx, ts_ms=ts, usage_source=UsageSource.PARTIAL_STREAM)
    _check(part.figure.low_nano == 0 and part.figure.nano == full.figure.nano,
           "partial streams are a [0, full] range")
    sub_ctx = dataclasses.replace(ctx, billing_path="subscription")
    allow = pricer.price_usage(_CASE1, sub_ctx, ts_ms=ts)
    _check(allow.figure.basis is Basis.LIST_EQUIVALENT and not allow.figure.is_billed_eligible
           and (pricer.basis is not Basis.LIST or allow.figure.nano == full.figure.nano),
           "subscription path is list-equivalent at list rates")
    none = pricer.price_usage(_CASE1, dataclasses.replace(ctx, model=""), ts_ms=ts)
    _check(none.figure.nano is None and none.unpriced_reason and none.exact_nano == 0
           and none.estimated is None and none.lines == (), "empty model is unpriced")
    inf = Inference(inference_id="inf_conformance", kind="message", usage=_CASE1, pricing=ctx)
    pi = pricer.price_inference(inf, ts_ms=ts)
    _check(pi.inference_id == "inf_conformance" and pi.figure == full.figure
           and pi.lines == full.lines, "price_inference equals price_usage")
    for priced in (p, ph, unc, est, part):
        f, e = priced.figure, priced.estimated
        _check(e is not None and f.nano == priced.exact_nano + (e.nano or 0)
               and f.low_nano == priced.exact_nano + (e.low_nano or 0)
               and f.high_nano == priced.exact_nano + (e.high_nano or 0),
               "figure = exact lines + estimated lines")


def _random_usage(rnd: random.Random, rates: ResolvedRates) -> UsageBuckets:
    def maybe(limit: int) -> int:
        return rnd.randrange(limit) if rnd.random() < 0.7 else 0

    has5, has1, hasother = (rates.cache_write_5m is not None, rates.cache_write_1h is not None,
                            rates.cache_write_other is not None)
    other = maybe(20_000) if hasother else 0
    return UsageBuckets(
        uncached_input=maybe(60_000),
        cache_read=maybe(120_000) if rates.cache_read is not None else 0,
        cache_write_5m=maybe(30_000) if has5 else 0,
        cache_write_1h=maybe(30_000) if has1 else 0,
        cache_write_other=other,
        cache_write_other_ttl_s=1800 if other else None,
        cache_write_unknown=maybe(10_000) if has5 and has1 else 0,
        output=maybe(20_000),
        web_search_requests=rnd.randrange(4) if dict(rates.per_request).get("web_search")
        else 0,
    )


def _unit_rate_agreement(pricer: Pricer, n: int, seed: int) -> int:
    rnd = random.Random(seed)
    facts = load_facts()
    candidates = [(r.model, r.channel, r.effective_from) for r in facts.rate_rows if r.enabled]
    variants = [{}, {"inference_geo": "us"}, {"service_tier": "batch"}, {"speed": "fast"},
                {"billing_path": "subscription"}]
    checked = 0
    attempts = 0
    while checked < n and attempts < 20 * n:
        attempts += 1
        model, channel, start = rnd.choice(candidates)
        extra = dict(rnd.choice(variants))
        if channel == "bedrock":
            extra["endpoint_scope"] = rnd.choice(["global", "regional"])
        ctx = _ctx(model, channel=channel, **extra)
        ts = max(_ts(start), _TS_2026_09_10) + rnd.randrange(10) * _DAY_MS
        rates = pricer.resolve(ctx, ts_ms=ts)
        unit = pricer.unit_rates(ctx, ts_ms=ts)
        if rates is None or unit is None:
            continue
        usage = _random_usage(rnd, rates)
        priced = pricer.price_usage(usage, ctx, ts_ms=ts)
        _check(priced.figure.nano is not None, "resolvable usage prices")
        total = 0
        for line in priced.lines:
            got = unit.bucket_nano(line.bucket, line.quantity)
            _check(got == line.amount_nano,
                   f"unit_rates disagree with price_usage on {line.bucket} ({ctx.model})")
            total += got
        _check(total == priced.figure.nano, "unit-rate sum equals the point price")
        checked += 1
    _check(checked == n, "not enough resolvable contexts for the unit-rate check")
    return checked


def assert_pricer_conforms(pricer: Pricer, *, samples: int = 200, seed: int = 0) -> dict[str, Any]:
    """Conformance of a :class:`~tokenbill.core.protocols.Pricer` (SPEC §3.18).

    Checks (1) the SPEC §6.9 golden cases for every verified facts row the pricer carries at the
    facts rates (a pricer with other rates, e.g. ``FlatRates`` or a contract card, skips them);
    (2) the §6.3 per-line exactness table (unknown-TTL range, placeholder output range, uncertain
    billing, reconstructed usage, partial streams, billable False, subscription basis, unpriced
    model, one rounding per line) expressed through the pricer's own point prices; (3)
    ``unit_rates`` agrees with ``price_usage`` to the nano on *samples* seeded random usages.
    Returns a summary.
    """
    _check(isinstance(pricer, Pricer), "not a Pricer (protocol surface)")
    _check(isinstance(pricer.rate_card_sha256, str) and pricer.rate_card_sha256 != "",
           "rate_card_sha256 must be a non-empty str")
    _check(pricer.basis in (Basis.LIST, Basis.CONTRACT), "card basis is LIST or CONTRACT")
    golden = _golden_cases(pricer)
    opus = _ctx("claude-opus-5-5")
    ts = _TS_2026_09_23 + 3_600_000
    if pricer.resolve(opus, ts_ms=ts) is not None:
        _rule_checks(pricer, opus, ts)
        _check(isinstance(pricer.min_cacheable_tokens(opus, ts_ms=ts), int),
               "min_cacheable_tokens of a priced model is an int")
        _check(isinstance(pricer.supports(opus, "batch", ts_ms=ts), bool), "supports → bool")
        _check(isinstance(pricer.tokenizer_family(opus, ts_ms=ts), str),
               "tokenizer_family of a priced model is a str")
    agreed = _unit_rate_agreement(pricer, samples, seed) if samples else 0
    return {"golden_cases": golden, "unit_rate_samples": agreed}


# =============================================================================================
# conformance: adapter
# =============================================================================================

#: Deterministic keys used by the adapter conformance default options.
CONFORMANCE_NAME_KEY = bytes(range(32))
CONFORMANCE_PRINCIPAL_KEY = bytes(range(32, 64))


def conformance_ingest_options(**kw: Any) -> IngestOptions:
    """The default :class:`~tokenbill.core.types.IngestOptions` of :func:`assert_adapter_conforms`:
    content tier none, install identity mode with fixed name/principal keys, clock 2026-09-23."""
    base: dict[str, Any] = {
        "name_key": CONFORMANCE_NAME_KEY, "name_key_id": key_id(CONFORMANCE_NAME_KEY),
        "principal_key": CONFORMANCE_PRINCIPAL_KEY,
        "principal_key_id": key_id(CONFORMANCE_PRINCIPAL_KEY), "now_ms": _TS_2026_09_23,
    }
    base.update(kw)
    return IngestOptions(**base)


def _source_bytes(path: Path) -> bytes:
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    blobs = []
    for f in files:
        data = f.read_bytes()
        if f.suffix.lower() == ".gz":
            try:
                data = gzip.decompress(data)
            except (OSError, EOFError):
                pass
        blobs.append(data)
    return b"\n".join(blobs)


def _walk_strings(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v, path)


def _walk_usage(obj: Any) -> Iterator[UsageBuckets]:
    if isinstance(obj, UsageBuckets):
        yield obj
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            yield from _walk_usage(getattr(obj, f.name))
    elif isinstance(obj, (list, tuple, frozenset, set)):
        for v in obj:
            yield from _walk_usage(v)
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from _walk_usage(v)


def _leaks_source(text: str, source: bytes, window: int = 65) -> bool:
    data = text.encode("utf-8", "surrogatepass")
    if len(data) < window:
        return False
    return any(data[start:start + window] in source for start in range(len(data) - window + 1))


#: Provider-authored labels that may exceed 64 bytes verbatim: cost-report / CUR descriptions and
#: SKU codes, aggregate dimension values. They are never user content.
_PROVIDER_LABEL_FIELDS = (".cost_lines.description", ".cost_lines.sku", ".aggregates.dims")


def _raw_usage(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        raise AssertionError("raw_usage_json is not valid JSON") from None


def _sum_check_attempts(result: IngestResult) -> int:
    try:
        from tokenbill.core import conventions  # F-SEM; optional at wave 1
    except ImportError:
        return 0
    checked = 0
    for req in result.requests:
        for att in req.attempts:
            if not att.raw_usage_json or not att.convention_id or len(att.inferences) != 1:
                continue
            try:
                conv = conventions.get_convention(att.convention_id)
            except Exception:  # unknown convention: nothing to compare against
                continue
            if not conv.enabled:
                continue
            raw = _raw_usage(att.raw_usage_json)
            if isinstance(raw, Mapping) and raw.get("iterations"):
                continue
            buckets, _ = conventions.normalize(att.convention_id, raw)
            got = att.inferences[0].usage
            _check(buckets.total_input == got.total_input and buckets.output == got.output,
                   f"sum check: attempt {att.attempt_id} disagrees with its raw usage")
            checked += 1
    return checked


def assert_adapter_conforms(adapter: Adapter, fixture_path: Path, *,
                            expect_capabilities: Collection[str],
                            opts: IngestOptions | None = None) -> IngestResult:
    """Conformance of an :class:`~tokenbill.core.protocols.Adapter` on one fixture (SPEC §3.18).

    Reads the fixture twice (identical ``to_json``); ``result.capabilities`` equals
    *expect_capabilities* and is within the declared maximum; no canary in ``repr`` or JSON; every
    token count in range; where a single-inference attempt carries its raw usage and an enabled
    convention (F-SEM's ``core.conventions``, when importable) the inference matches the normalized
    totals; in content tier ``none`` no string field carries more than 64 bytes of source text
    (``raw_usage_json`` excepted: numbers and allowlisted enums only); every ``h_`` value appears
    only with ``SourceInfo.name_key_id`` equal to the options' name key id and every ``p_``/``c_``
    value only with the matching ``principal_key_id``; every ``LicenseSnapshot`` / ``ActivityDay``
    / ``CostLine`` principal is a ``p_`` pseudonym and neither ``CANARY_LOGIN`` nor
    ``CANARY_EMAIL`` appears anywhere (GitHub sources, K-5). Returns the first result.
    """
    fixture_path = Path(fixture_path)
    _check(isinstance(adapter, Adapter), "not an Adapter (protocol surface)")
    _check(isinstance(adapter.name, str) and adapter.name, "adapter.name must be a non-empty str")
    _check(isinstance(adapter.capabilities, frozenset), "adapter.capabilities must be a frozenset")
    options = opts if opts is not None else conformance_ingest_options()
    if fixture_path.is_file():
        head = _source_bytes(fixture_path)[:64 * 1024]
        _check(isinstance(adapter.sniff(fixture_path, head), bool), "sniff must return a bool")
    first = adapter.read(fixture_path, options)
    second = adapter.read(fixture_path, options)
    _check(isinstance(first, IngestResult), "read must return an IngestResult")
    one, two = _canonical(to_json(first)), _canonical(to_json(second))
    _check(one == two, "reading the same fixture twice gives different results")
    _check(first.capabilities == frozenset(expect_capabilities),
           f"capabilities {sorted(first.capabilities)} != expected {sorted(expect_capabilities)}")
    _check(first.capabilities <= adapter.capabilities,
           "result capabilities exceed the adapter's declared capabilities")
    assert_no_canary(repr(first), one)
    for blob in (repr(first), one):
        _check(CANARY_LOGIN.lower() not in blob.lower() and CANARY_EMAIL.lower() not in
               blob.lower(), "a canary login or e-mail leaked into the result")
    for rec in (*first.licenses, *first.activity, *first.cost_lines):
        principal = rec.principal
        _check(principal is None or bool(_STORE_P_RE.match(principal)),
               f"{type(rec).__name__}.principal must be a p_ pseudonym")
    for usage in _walk_usage(first):
        for f in dataclasses.fields(UsageBuckets):
            v = getattr(usage, f.name)
            _check(v is None or (type(v) is int and 0 <= v <= 2**53),
                   f"token count {f.name} out of range")
    _sum_check_attempts(first)
    encoded = json.loads(one)
    if str(options.content_tier) == "none":
        source = _source_bytes(fixture_path)
        for path, text in _walk_strings(encoded):
            if path.endswith(_PROVIDER_LABEL_FIELDS):
                continue  # provider cost-type labels and codes, never user content
            if path.endswith(".raw_usage_json"):
                _check(all(len(s.encode()) <= 64 for _, s in _walk_strings(_raw_usage(text))),
                       "raw_usage_json carries long strings")
                continue
            _check(not _leaks_source(text, source),
                   f"field {path} carries more than 64 bytes of source text")
    for path, text in _walk_strings(encoded):
        if _HASH_RE.match(text):
            _check(first.source.name_key_id is not None
                   and first.source.name_key_id == options.name_key_id,
                   f"h_ value at {path} without the matching SourceInfo.name_key_id")
        if re.match(r"[pc]_[0-9a-f]{20}\Z", text):
            _check(first.source.principal_key_id is not None
                   and first.source.principal_key_id == options.principal_key_id,
                   f"{text[:2]} value at {path} without the matching principal_key_id")
    return first


# =============================================================================================
# conformance: store
# =============================================================================================

STORE_ORG_KEY = bytes(range(64, 96))
STORE_NAME_KEY = bytes(range(96, 128))
_OTHER_NAME_KEY = bytes(range(128, 160))


def _h(value: str, key: bytes = STORE_NAME_KEY) -> str:
    return pseudonym(key, "h", value)


def _src(source_id: str, adapter: str, *, name_key: bytes | None = STORE_NAME_KEY) -> SourceInfo:
    return SourceInfo(source_id=source_id, adapter=adapter, name_hmac=_h(source_id),
                      sha256=hashlib.sha256(source_id.encode()).hexdigest(), bytes=100,
                      name_key_id=key_id(name_key) if name_key is not None else None,
                      principal_key_id=key_id(STORE_ORG_KEY))


def _ref(adapter: str, source_id: str, fidelity: Fidelity, priority: int, n: int) -> SourceRef:
    return SourceRef(adapter=adapter, source_id=source_id, locator=f"line:{n}", fidelity=fidelity,
                     priority=priority)


def _result(source: SourceInfo, requests: list[Request], *, sessions: Sequence[Session] = (),
            events: Sequence[LaneEvent] = (), aggregates: Sequence[UsageAggregate] = (),
            cost_lines: Sequence[CostLine] = (),
            outcomes: Sequence[OutcomeAggregate] = ()) -> IngestResult:
    return IngestResult(source=source, requests=list(requests), sessions=list(sessions),
                        events=list(events), aggregates=list(aggregates),
                        cost_lines=list(cost_lines), outcomes=list(outcomes), quarantined=[],
                        notes=[], stats={"records": len(requests)},
                        capabilities=frozenset({"usage_sequence", "timing"}))


def _shell(lane_key: str, session_key: str, kind: LaneKind, scope: str = "ws:w1") -> Session:
    lane = Lane(lane_key=lane_key, session_key=session_key, kind=kind, parent_lane_key=None,
                cache_scope_key=scope, requests=())
    return Session(session_key=session_key, source_kind="test", attribution=Attribution(),
                   lanes=(lane,), started_ms=0, ended_ms=0)


_CONFORMANCE_DAY = "2026-09-23"   # Opus 5.5 is priced from 2026-09-22
_T0 = _date_start_ms(_CONFORMANCE_DAY) + 9 * 3_600_000


def _conformance_sources() -> list[IngestResult]:
    """Five small sources (transcript-, OTel-, trace@2-, trace@1- and responses-shaped) exercising
    every §7.3 merge rule. Collision and split-entry pairs sit inside one source."""
    alice, bob, carol = "r_alice", "r_bob", "r_carol"
    cc_attr = {"principal": alice, "team": "payments", "agent_product": "claude_code",
               "cwd_key": _h("/repo/a"), "workload_class": "interactive",
               "billing_path": "api_key"}
    s1 = _src("s_transcript", "claude-code")

    def cc(seq: int, ts: int, usage: dict[str, int], msg: str, rq: str, **kw: Any) -> Request:
        return make_request("L-main", seq, ts, usage, "claude-opus-5-5", session_key="S-cc",
                            request_id=stable_id("rq", "anthropic", msg), message_id=msg,
                            attribution=kw.pop("attribution", cc_attr),
                            source=_ref("claude-code", "s_transcript", Fidelity.FULL, 40, seq),
                            billing_path="api_key", **kw)

    def with_rq(req: Request, rq: str) -> Request:
        att = dataclasses.replace(req.attempts[0], provider_request_id=rq)
        return dataclasses.replace(req, attempts=(att, *req.attempts[1:]))

    transcript = [
        with_rq(cc(0, _T0, {"cache_write_5m": 100_000, "output": 500}, "msg_m0", "req_q0"),
                "req_q0"),
        with_rq(cc(1, _T0 + 420_000, {"cache_write_5m": 102_000, "output": 500}, "msg_m1",
                   "req_q1"), "req_q1"),
        with_rq(cc(2, _T0 + 450_000, {"cache_read": 102_000, "cache_write_1h": 2000,
                                      "output": 10}, "msg_m2", "req_q2"), "req_q2"),
        with_rq(cc(2, _T0 + 450_000, {"cache_read": 102_000, "cache_write_1h": 2000,
                                      "output": 500}, "msg_m2", "req_q2"), "req_q2"),
        with_rq(cc(3, _T0 + 480_000, {"cache_read": 104_000, "output": 300}, "msg_m5",
                   "req_dup"), "req_dup"),
        with_rq(cc(4, _T0 + 510_000, {"cache_read": 104_000, "output": 200}, "msg_m6",
                   "req_dup"), "req_dup"),
    ]
    sub_attr = {**cc_attr, "principal": bob}
    transcript.append(make_request(
        "L-sub", 0, _T0 + 60_000, {"cache_write_5m": 20_000, "output": 100}, "claude-sonnet-5",
        session_key="S-cc", request_id=stable_id("rq", "anthropic", "msg_sub0"),
        message_id="msg_sub0", attribution=sub_attr, billing_path="api_key",
        source=_ref("claude-code", "s_transcript", Fidelity.FULL, 40, 90)))
    r1 = _result(s1, transcript, sessions=[_shell("L-main", "S-cc", LaneKind.MAIN),
                                           _shell("L-sub", "S-cc", LaneKind.SUBAGENT)],
                 events=[LaneEvent(lane_key="L-main", ts_ms=_T0 + 400_000, kind="human_prompt")])
    s2 = _src("s_otel", "otlp")
    otel_attr = {"principal": alice, "team": "payments", "cost_center": "cc-1",
                 "extra": (("mdm_group", "g1"),)}

    def otel(n: int, ts: int, rq: str, lane: str = "L-otel") -> Request:
        req = make_request(lane, n, ts, {"cache_read": 50_000, "cache_write_unknown": 52_000,
                                         "output": 500}, "claude-opus-5-5",
                           session_key="S-otel", request_id=stable_id("rq", "s_otel", n),
                           attribution=otel_attr,
                           source=_ref("otlp", "s_otel", Fidelity.NO_TTL_SPLIT, 20, n))
        att = dataclasses.replace(req.attempts[0], provider_request_id=rq)
        return dataclasses.replace(req, attempts=(att,))

    r2 = _result(s2, [otel(1, _T0 + 420_000, "req_q1"), otel(7, _T0 + 480_000, "req_dup"),
                      otel(9, _T0 + 900_000, "req_q9")],
                 sessions=[_shell("L-otel", "S-otel", LaneKind.UNKNOWN, "unknown")])
    s3 = _src("s_trace2", "trace@2")
    sdk_attr = {"principal": carol, "team": "platform", "agent_product": "agent_sdk",
                "workload_class": "ci", "repo": _h("repo-x"), "extra": (("mdm_group", "g2"),)}

    def sdk(n: int, ts: int, usage: dict[str, int], adapter: str, source_id: str,
            priority: int, attr: Mapping[str, Any] = sdk_attr) -> Request:
        return make_request("L-sdk", n, ts, usage, "claude-sonnet-5", session_key="S-sdk",
                            request_id=stable_id("rq", "anthropic", f"msg_s{n}"),
                            message_id=f"msg_s{n}", attribution=dict(attr),
                            source=_ref(adapter, source_id, Fidelity.FULL, priority, n))

    r3 = _result(s3, [sdk(0, _T0 + 1000, {"cache_write_5m": 30_000, "output": 800}, "trace@2",
                          "s_trace2", 50),
                      sdk(1, _T0 + 31_000, {"cache_read": 30_000, "cache_write_5m": 1000,
                                            "output": 800}, "trace@2", "s_trace2", 50)],
                 sessions=[_shell("L-sdk", "S-sdk", LaneKind.API_RUN)],
                 aggregates=[_aggregate()], cost_lines=[_cost_line()],
                 outcomes=[OutcomeAggregate(date_utc=_CONFORMANCE_DAY, team="platform", n_users=6,
                                            sessions=10, commits=3, pull_requests=2,
                                            lines_added=100, lines_removed=20, edits_accepted=5,
                                            edits_rejected=1)])
    s4 = _src("s_trace1", "trace@1", name_key=_OTHER_NAME_KEY)
    t1_attr = {"principal": "p_" + "0" * 20, "team": "data", "repo": _h("repo-y", _OTHER_NAME_KEY),
               "workload_class": "batch"}
    r4 = _result(s4, [make_request(
        "L-t1", n, _T0 + 5000 * n, {"uncached_input": 3000, "output": 100}, "claude-haiku-4-5",
        session_key="S-t1", request_id=stable_id("rq", "s_trace1", n), attribution=t1_attr,
        source=_ref("trace@1", "s_trace1", Fidelity.FULL, 10, n)) for n in range(2)])
    s5 = _src("s_responses", "anthropic-responses")
    resp_attr = {"principal": carol, "team": "platform", "agent_product": "agent_sdk",
                 "project": "proj-1"}
    r5 = _result(s5, [
        sdk(0, _T0 + 1000, {"cache_write_5m": 30_000, "output": 700}, "anthropic-responses",
            "s_responses", 35, resp_attr),
        make_request("L-allow", 0, _T0 + 7000, {"uncached_input": 1000, "output": 100},
                     "claude-opus-5-5", session_key="S-allow",
                     request_id=stable_id("rq", "anthropic", "msg_a0"), message_id="msg_a0",
                     attribution={"principal": alice, "team": "payments",
                                  "billing_path": "subscription"},
                     billing_path="subscription",
                     source=_ref("anthropic-responses", "s_responses", Fidelity.FULL, 35, 9)),
    ], sessions=[_shell("L-allow", "S-allow", LaneKind.MAIN)])
    return [r1, r2, r3, r4, r5]


def _aggregate() -> UsageAggregate:
    return UsageAggregate(agg_id="ag_conformance", source_kind="anthropic.usage_report",
                          bucket_start_ms=_date_start_ms(_CONFORMANCE_DAY),
                          bucket_end_ms=_date_start_ms(_CONFORMANCE_DAY) + _DAY_MS,
                          dims=(("api_key_id", _h("key-1")), ("channel", "anthropic_api"),
                                ("model", "claude-opus-5-5")),
                          usage=UsageBuckets(uncached_input=10, output=5))


def _cost_line() -> CostLine:
    return CostLine(line_id="cl_conformance", source_kind="anthropic.cost_report",
                    date_utc=_CONFORMANCE_DAY, channel="anthropic_api", workspace_id=None,
                    description="Claude Opus 5.5 output", model="claude-opus-5-5",
                    cost_type="tokens", token_type="output", sku=None, service_tier=None,
                    inference_geo=None, endpoint_scope=None, amount_nano=1_000_000)


_ALL_COST_DIMS = list(_COST_DIMS)


def _store_dump(store: LedgerStore) -> str:
    w = {"since_ms": 0, "until_ms": _FOREVER_MS}

    def enc(items: Iterable[Any]) -> list[str]:
        return sorted(_canonical(to_json(x)) if dataclasses.is_dataclass(x) else _canonical(x)
                      for x in items)

    dump = {
        "requests": enc(store.iter_requests(**w)),
        "records": enc(store.iter_usage_records(**w)),
        "lanes": enc(store.iter_lanes(**w)),
        "lane_index": enc(store.lane_index(**w)),
        "first_reads": enc(list(t) for t in store.lane_first_reads(**w)),
        "aggregates": enc(store.aggregates(**w)),
        "cost_lines": enc(store.cost_lines(**w)),
        "outcomes": enc(store.outcomes(**w)),
        "cost_rows": enc(store.cost_rows(group_by=_ALL_COST_DIMS, **w)),
        "agg": _canonical(to_json(store.aggregate(group_by=["team", "model"], **w))),
        "clusters": enc(store.cluster_days(cluster_kind="team", since="2026-09-01",
                                           until="2026-10-01")),
    }
    return _canonical(dump)


def _permutations(n: int, count: int, seed: int) -> list[tuple[int, ...]]:
    everything = list(itertools.permutations(range(n)))
    if count >= len(everything):
        return everything
    rnd = random.Random(seed)
    picked = [tuple(range(n)), tuple(reversed(range(n)))]
    rest = [p for p in everything if p not in picked]
    picked += rnd.sample(rest, count - 2)
    return picked


def assert_store_conforms(factory: Callable[..., LedgerStore], *, permutations: int = 12,
                          seed: int = 0) -> dict[str, Any]:
    """Conformance of a :class:`~tokenbill.core.protocols.LedgerStore` (SPEC §3.18, §7).

    *factory* is called with keyword arguments ``org_key`` (bytes or None), ``name_key_id`` (str)
    and ``pricer`` and must return a new, empty store each call (``lambda **kw: SqliteStore(path,
    **kw)`` with a fresh path; the name key id is fixed up front so sources under another name key
    are nulled whatever the ingest order). Checks: the §7.3 merge rules on five small sources
    (transcript + OTel join through the provider request id: transcript usage, OTel attribution
    incl. an ``extra`` key, two ``sources_mask`` bits; a request id seen with two message ids is
    never a join key; the split-entry rule; source priority at equal fidelity); write-time privacy
    (``r_`` principals stored only as ``p_``; ``r_`` without an org key → ``PrivacyError``; a source
    under another name key has its ``h_`` values nulled); idempotence (same sources twice → same
    dump); order independence over *permutations* orders of the five sources; every protocol method
    (lanes, index, first reads, ``count_users`` vs a brute force, aggregates, cost lines, outcomes,
    ``aggregate`` and ``cost_rows`` totals equal to the pricer's exact lines, principal group-by →
    ``PrivacyError``, clusters, cursors, findings, receipts, purge, reprice, meta). Returns counts.
    """
    pricer = FakePricer()
    sources = _conformance_sources()
    kw = {"name_key_id": key_id(STORE_NAME_KEY), "pricer": pricer}
    store = factory(org_key=STORE_ORG_KEY, **kw)
    _check(isinstance(store, LedgerStore), "not a LedgerStore (protocol surface)")
    for result in sources:
        store.ingest(result, pricer=pricer)
    reference = _store_dump(store)
    w = {"since_ms": 0, "until_ms": _FOREVER_MS}
    reqs = {r.request_id: r for r in store.iter_requests(**w)}
    alice = pseudonym(STORE_ORG_KEY, "p", "alice")
    # --- merge rules ---
    m1 = reqs.get(stable_id("rq", "anthropic", "msg_m1"))
    _check(m1 is not None, "transcript request m1 missing")
    assert m1 is not None
    si = m1.serving_inference
    _check(si is not None and si.usage.cache_write_5m == 102_000 and si.usage.cache_write_unknown
           == 0, "FULL transcript usage must win over NO_TTL_SPLIT OTel usage")
    _check(m1.attribution.cost_center == "cc-1" and ("mdm_group", "g1") in m1.attribution.extra,
           "OTel-only attribution (incl. an extra key) must fill the transcript request")
    _check(m1.attribution.principal == alice, "r_ principals are stored as p_ (org key)")
    otel_only = [r for r in reqs.values() if r.lane_key == "L-otel"]
    _check(len(otel_only) == 2, "OTel requests joined through a colliding request id")
    m2 = reqs.get(stable_id("rq", "anthropic", "msg_m2"))
    _check(m2 is not None and m2.serving_inference is not None
           and m2.serving_inference.usage.output == 500, "split-entry rule: larger output wins")
    s0 = reqs.get(stable_id("rq", "anthropic", "msg_s0"))
    _check(s0 is not None and s0.serving_inference is not None
           and s0.serving_inference.usage.output == 800,
           "equal fidelity, different adapters: higher source priority wins")
    _check(s0 is not None and s0.attribution.project == "proj-1",
           "attribution from a lower-priority source fills null fields")
    t1 = [r for r in reqs.values() if r.lane_key == "L-t1"]
    _check(len(t1) == 2 and all(r.attribution.repo is None and r.attribution.principal == (
        "p_" + "0" * 20) for r in t1), "h_ values under another name key are nulled")
    _check(all(not (r.attribution.principal or "p_").startswith(("r_", "c_"))
               for r in reqs.values()), "no r_/c_ principal is stored")
    _check(len(reqs) == 13, f"expected 13 merged requests, got {len(reqs)}")
    # --- idempotence and order independence ---
    for result in sources:
        store.ingest(result, pricer=pricer)
    _check(_store_dump(store) == reference, "ingesting the same sources twice changed the store")
    orders = _permutations(len(sources), permutations, seed)
    for order in orders:
        other = factory(org_key=STORE_ORG_KEY, **kw)
        for i in order:
            other.ingest(sources[i], pricer=pricer)
        _check(_store_dump(other) == reference, f"ingest order {order} changed the store")
    # --- privacy ---
    bare = factory(org_key=None, **kw)
    try:
        bare.ingest(sources[0], pricer=pricer)
    except PrivacyError:
        pass
    else:
        raise AssertionError("r_ principals without an org key must raise PrivacyError")
    for bad in (["principal"], ["team", "principal"], ["session_key"]):
        try:
            store.aggregate(group_by=bad, **w)
        except PrivacyError:
            pass
        else:
            raise AssertionError(f"aggregate(group_by={bad}) must raise PrivacyError")
    try:
        store.cost_rows(group_by=["principal"], **w)
    except (PrivacyError, UsageError):  # §3.6 "principal never allowed" (error type open)
        pass
    else:
        raise AssertionError("cost_rows(group_by=['principal']) must be refused")
    # --- lanes, index, first reads, users ---
    lanes = list(store.iter_lanes(**w))
    index = {row.lane_key: row for row in store.lane_index(**w)}
    _check({lane.lane_key for lane in lanes} == set(index), "lane_index and iter_lanes disagree")
    for lane in lanes:
        row = index[lane.lane_key]
        point = sum(pricer.price_inference(inf, ts_ms=att.ts_start_ms).figure.nano or 0
                    for req in lane.requests for att in req.attempts for inf in att.inferences
                    if inf.billable is not False)
        _check((row.requests, row.team, row.lane_kind, row.billing_class, row.point_nano)
               == (len(lane.requests), lane.team, lane.kind.value, lane.billing_class, point),
               f"lane_index row of {lane.lane_key} is inconsistent")
    kinds = {lane.lane_key: lane.kind for lane in lanes}
    _check(kinds.get("L-main") is LaneKind.MAIN and kinds.get("L-sdk") is LaneKind.API_RUN,
           "lane shells give lane kinds")
    sub_lanes = list(store.iter_lanes(where={"team": "payments", "lane_kind": "main"}, **w))
    _check({lane.lane_key for lane in sub_lanes} == {"L-main", "L-allow"},
           "iter_lanes(where=team, lane_kind) filters lanes")
    sample = list(store.iter_lanes(lane_keys=["L-sdk"], **w))
    _check([lane.lane_key for lane in sample] == ["L-sdk"], "iter_lanes(lane_keys) restricts")
    first = {t[0:2]: t[2] for t in store.lane_first_reads(**w)}
    _check(first.get(("ws:w1", "claude-opus-5-5")) is not None, "lane_first_reads")
    for where in ({"team": "payments"}, {"team": "platform"}, {"lane_kind": "main"},
                  {"model": "claude-opus-5-5"}, {"billing_class": "allowance"},
                  {"team": "payments", "lane_kind": "subagent"}):
        brute = {r.attribution.principal for lane in lanes for r in lane.requests
                 if r.attribution.principal is not None
                 and _request_matches(r, lane, where)}
        _check(store.count_users(where=where, **w) == len(brute),
               f"count_users({where}) differs from a brute-force distinct count")
    # --- provider-side records ---
    _check([a.agg_id for a in store.aggregates(**w)] == ["ag_conformance"], "aggregates")
    _check([c.line_id for c in store.cost_lines(**w)] == ["cl_conformance"], "cost_lines")
    _check(len(store.outcomes(**w)) == 1, "outcomes")
    _check(store.aggregates("anthropic.cost_report", **w) == [], "aggregates(source_kind)")
    # --- money invariants ---
    records = list(store.iter_usage_records(**w))
    _check(len({r.inference_id for r in records}) == len(records), "one record per inference")
    exact_billed = allow = 0
    for rec in records:
        inf = Inference(inference_id=rec.inference_id, kind=rec.kind, usage=rec.usage,
                        pricing=rec.pricing, usage_source=rec.usage_source,
                        billable=rec.billable, billing_rule_id=rec.billing_rule_id)
        p = pricer.price_inference(inf, ts_ms=rec.ts_ms)
        if p.figure.basis is Basis.LIST_EQUIVALENT:
            allow += p.figure.nano or 0
        else:
            exact_billed += p.exact_nano
    rows = store.cost_rows(group_by=_ALL_COST_DIMS, **w)
    billed_rows = sum(r.priced_nano for r in rows if r.basis is not Basis.LIST_EQUIVALENT)
    _check(billed_rows == exact_billed, "cost_rows exact sums differ from the ledger's exact lines")
    _check(any(r.basis is Basis.LIST_EQUIVALENT for r in rows), "allowance rows kept apart")
    total = store.aggregate(group_by=[], **w)
    _check(len(total.rows) == 1 and total.rows[0].priced.exact.nano == exact_billed,
           "aggregate exact total differs from the ledger's exact lines")
    allowance = total.rows[0].priced.allowance
    _check(allowance is not None and allowance.nano == allow, "aggregate allowance total")
    teams = store.aggregate(group_by=["team"], **w)
    for row in teams.rows:
        team = dict(row.dims)["team"]
        if team is not None:
            _check(row.n_users == store.count_users(where={"team": team}, **w),
                   "aggregate n_users equals count_users")
    clusters = store.cluster_days(cluster_kind="team", since=_CONFORMANCE_DAY, until="2026-09-24")
    pay = [c for c in clusters if c.cluster_id == "payments"]
    _check(pay and sum(c.active_users for c in pay) >= 1, "cluster_days(team)")
    # --- cursors, findings, receipts ---
    _check(store.get_cursor("s_x", "u") is None, "missing cursor is None")
    store.set_cursor("s_x", "u", byte_offset=10, head_sha="abc", size=20, mtime_ns=30)
    _check(store.get_cursor("s_x", "u") == (10, "abc", 20, 30), "cursor round trip")
    fnd = _conformance_finding()
    store.put_findings("run-1", [fnd])
    store.put_findings("run-2", [dataclasses.replace(fnd, n_events=9)])
    _check(store.findings("run-1") == [fnd], "findings(run_id) round trip")
    _check([f.n_events for f in store.findings()] == [9], "findings() returns the latest run")
    rec_a = ReceiptRow(receipt_id="rc_a", lever_id="cc.prompt_cache_ttl.main",
                       lever_class="cache_transform", label="measured", realization_rate="0.9",
                       created_ms=1, json="{}", dsse=None)
    rec_b = dataclasses.replace(rec_a, receipt_id="rc_b", lever_class="rate", created_ms=2)
    store.put_receipt(rec_b)
    store.put_receipt(rec_a)
    _check([r.receipt_id for r in store.receipts()] == ["rc_a", "rc_b"], "receipts ordered")
    _check([r.receipt_id for r in store.receipts(lever_class="rate")] == ["rc_b"],
           "receipts(lever_class)")
    # --- meta, reprice, purge ---
    meta = store.meta()
    _check(isinstance(meta, dict) and all(isinstance(v, str) for v in meta.values())
           and meta.get("org_key_id") == key_id(STORE_ORG_KEY), "meta() names the org key id")
    _check(isinstance(store.reprice(pricer), int), "reprice returns a count")
    # reprice(pricer, since_ms, until_ms) re-prices exactly the requests of its window (§7.2)
    half = FakePricer().with_contract(ContractOverlay(
        name="conformance-half", multiplier=Decimal("0.5"), overrides=(),
        effective_from="2026-01-01", effective_to=None, derived=False, assumed_fields=()))
    cut = _T0 + 450_000
    _check(isinstance(store.reprice(half, since_ms=cut, until_ms=_FOREVER_MS), int),
           "reprice(since_ms, until_ms) returns a count")
    expected_billed = 0
    for rec in store.iter_usage_records(**w):
        inf = Inference(inference_id=rec.inference_id, kind=rec.kind, usage=rec.usage,
                        pricing=rec.pricing, usage_source=rec.usage_source,
                        billable=rec.billable, billing_rule_id=rec.billing_rule_id)
        p = (half if rec.ts_ms >= cut else pricer).price_inference(inf, ts_ms=rec.ts_ms)
        if p.figure.basis is not Basis.LIST_EQUIVALENT:
            expected_billed += p.exact_nano
    repriced = store.cost_rows(group_by=_ALL_COST_DIMS, **w)
    _check(sum(r.priced_nano for r in repriced if r.basis is not Basis.LIST_EQUIVALENT)
           == expected_billed and any(r.basis is Basis.CONTRACT for r in repriced),
           "reprice(since_ms, until_ms) must re-price exactly the requests of its window")
    store.reprice(pricer)
    _check(sum(r.priced_nano for r in store.cost_rows(group_by=_ALL_COST_DIMS, **w)
               if r.basis is not Basis.LIST_EQUIVALENT) == exact_billed,
           "reprice with the original pricer restores the ledger")
    before = store.count_users(where={}, **w)
    removed = store.purge(principal=alice, actor="conformance")
    _check(removed > 0 and store.count_users(where={}, **w) == before - 1,
           "purge(principal) removes that person's requests")
    _check(all(r.attribution.principal != alice for r in store.iter_requests(**w)),
           "no request of the purged principal remains")
    store.audit("conformance", "check", {"n": 1})
    return {"requests": len(reqs), "orders": len(orders), "lanes": len(lanes)}


def _request_matches(req: Request, lane: Lane, where: Mapping[str, str]) -> bool:
    """Brute-force evaluation of a ``count_users`` filter on one request of *lane*."""
    si = req.serving_inference
    path = req.attribution.billing_path or (si.pricing.billing_path if si is not None else None)
    values = {"team": req.attribution.team, "lane_kind": lane.kind.value, "model": req.model,
              "billing_class": billing_class(path), "cost_center": req.attribution.cost_center,
              "workspace_id": req.attribution.workspace_id}
    return all(values.get(k) == v for k, v in where.items())


def _conformance_finding() -> Finding:
    scope = Scope(dims=(("lane_kind", "main"), ("team", "payments")))
    from tokenbill.core.registry import _finding_id

    return Finding(
        finding_id=_finding_id("test.conformance", "k", scope), detector_id="test.conformance",
        kind="k", detector_version="1", category="lever", lever_class="cache_transform",
        audience="org", title="t", summary="No mechanical fix.", scope=scope, n_events=1,
        n_lanes=1, n_users=5, first_seen_ms=0, cost_observed=exact(100, Basis.LIST),
        recoverable=estimated(50, Basis.LIST, note="x"), references=("r",))


# =============================================================================================
# conformance: replayer
# =============================================================================================


_REPLAY_T0_S = _ts("2026-09-23") // 1000


def _shifted(rows: Iterable[tuple[int, int, int, int, int, int]]) -> list[tuple[int, ...]]:
    return [(_REPLAY_T0_S + ts, *rest) for ts, *rest in rows]


def _replayer_lanes() -> list[Lane]:
    cc = {"attribution": {"agent_product": "claude_code", "team": "t1", "principal": "r_u1"}}
    main = lane_from_table(_shifted([(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500),
                                     (840, 0, 104_000, 0, 0, 500),
                                     (1260, 0, 106_000, 0, 0, 500)]), lane_key="R-main", **cc)
    bursty = lane_from_table(_shifted([(0, 0, 100_000, 0, 0, 500), (30, 100_000, 2000, 0, 0, 500),
                                       (60, 102_000, 2000, 0, 0, 500)]), lane_key="R-bursty",
                             **cc)
    sub = lane_from_table(_shifted([(0, 0, 20_000, 0, 0, 100), (400, 0, 21_000, 0, 0, 100)]),
                          lane_key="R-sub", kind=LaneKind.SUBAGENT, model="claude-sonnet-5", **cc)
    return [main, bursty, sub]


def _priced_request(pricer: Pricer, req: Request, basis: Basis) -> Figure:
    fig = zero(basis)
    for att in req.attempts:
        for inf in att.inferences:
            if inf.billable is not False:
                fig = add(fig, pricer.price_inference(inf, ts_ms=att.ts_start_ms).figure)
    return fig


def assert_replayer_conforms(replayer: Replayer, pricer: Pricer, *,
                             rules: Any = None, pool: bool = False) -> dict[str, Any]:
    """Conformance of a :class:`~tokenbill.core.protocols.Replayer` (SPEC §3.18, §9.1).

    The observed policy returns ``cost == baseline`` to the nano (point and range) with every
    outcome ``changed=False`` and zero saving; ``baseline`` equals Σ ``PricedInference.figure`` over
    the billable inferences; under ``ttl=1h`` scoped to main lanes, requests of the other lanes are
    unchanged (cost equal to their priced point); replay is deterministic; mixed billing classes
    raise ``UsageError``. *rules* defaults to F-SEM's ``RulesTable`` when importable. With
    *pool* (GitHub Copilot, CORE-AMENDMENTS A-6) also: Copilot ``pool`` lanes replay on
    LIST_EQUIVALENT (observed cost == baseline), and ``pool`` mixed with billed lanes raises."""
    _check(isinstance(replayer, Replayer), "not a Replayer (protocol surface)")
    if rules is None:
        try:
            from tokenbill.core.cache_rules import RulesTable  # F-SEM; optional at wave 1

            rules = RulesTable()
        except ImportError:
            rules = None
    lanes = _replayer_lanes()
    basis = pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST
    kw = {"mode": "documented", "pricer": pricer, "rules": rules, "calibration": None}
    res = replayer.replay(lanes, Policy.observed(), keep_outcomes=True, **kw)
    expected = zero(basis)
    for lane in lanes:
        for req in lane.requests:
            expected = add(expected, _priced_request(pricer, req, basis))
    for attr in ("nano", "low_nano", "high_nano"):
        _check(getattr(res.baseline, attr) == getattr(expected, attr),
               f"baseline.{attr} differs from the priced ledger")
        _check(getattr(res.cost, attr) == getattr(res.baseline, attr),
               f"observed policy: cost.{attr} != baseline.{attr}")
    _check(res.saving.nano == 0, "observed policy saves nothing")
    _check(res.outcomes is not None and len(res.outcomes) == sum(len(x.requests) for x in lanes)
           and all(not o.changed for o in res.outcomes), "observed outcomes are unchanged")
    _check(isinstance(res.calibration, Calibration), "calibration label")
    ttl = Policy(name="conformance-ttl-1h-main", ttl=(("lane_kind:main", "1h"),))
    a = replayer.replay(lanes, ttl, keep_outcomes=True, **kw)
    b = replayer.replay(lanes, ttl, keep_outcomes=True, **kw)
    _check(_canonical(to_json(a)) == _canonical(to_json(b)), "replay is not deterministic")
    _check(a.saving.evidence is Evidence.ESTIMATED or a.saving.nano == 0,
           "a counterfactual saving is ESTIMATED")
    sub_ids = {req.request_id: req for lane in lanes if lane.kind is not LaneKind.MAIN
               for req in lane.requests}
    assert a.outcomes is not None
    for o in a.outcomes:
        if o.request_id in sub_ids:
            point = _priced_request(pricer, sub_ids[o.request_id], basis).nano
            _check(not o.changed and o.cost_nano == point,
                   "a request outside the policy's selector changed")
    allowance = lane_from_table_allowance()
    try:
        replayer.replay([lanes[0], allowance], ttl, **kw)
    except UsageError:
        pass
    else:
        raise AssertionError("mixed billing classes must raise UsageError")
    out = {"baseline_nano": res.baseline.nano, "ttl_saving_nano": a.saving.nano}
    if pool:
        pooled = lane_from_table_pool()
        pres = replayer.replay([pooled], Policy.observed(), **kw)
        expected_pool = zero(Basis.LIST_EQUIVALENT)
        for req in pooled.requests:
            expected_pool = add(expected_pool, _priced_request(pricer, req,
                                                               Basis.LIST_EQUIVALENT))
        _check(pres.baseline.basis is Basis.LIST_EQUIVALENT
               and pres.cost.nano == pres.baseline.nano == expected_pool.nano,
               "a pool (Copilot) lane replays on LIST_EQUIVALENT")
        try:
            replayer.replay([lanes[0], pooled], ttl, **kw)
        except UsageError:
            pass
        else:
            raise AssertionError("pool lanes mixed with billed lanes must raise UsageError")
        out["pool_baseline_nano"] = pres.baseline.nano
    return out


def lane_from_table_allowance() -> Lane:
    """A one-request subscription (allowance) lane, for mixed-billing-class checks."""
    return lane_from_table(_shifted([(0, 0, 10_000, 0, 0, 100)]), lane_key="R-allow",
                           billing_path="subscription")


def lane_from_table_pool() -> Lane:
    """A two-request GitHub Copilot lane (channel ``github_copilot``, billing path
    ``copilot_pool``: billing class ``pool``), for Copilot replay checks."""
    return lane_from_table(_shifted([(0, 0, 10_000, 0, 0, 100), (30, 10_000, 500, 0, 0, 100)]),
                           lane_key="R-pool", provider="github", channel="github_copilot",
                           billing_path="copilot_pool")


# =============================================================================================
# conformance: detector
# =============================================================================================

_CATEGORIES = frozenset({"breaker", "lever", "premium", "failure", "attribution", "data-quality",
                         "aggregate"})
_LEVER_CLASSES = frozenset({"rate", "cache_transform", "trajectory", "behavioral", "hygiene",
                            "none"})


def _figures(f: Finding) -> list[tuple[str, Figure | None]]:
    return [("cost_observed", f.cost_observed), ("recoverable", f.recoverable),
            ("recoverable_shapley", f.recoverable_shapley),
            ("projected_monthly", f.projected_monthly)]


def _check_finding(detector: Detector, f: Finding, ctx: AnalysisContext) -> None:
    from tokenbill.core.registry import _finding_id

    tag = f"finding {f.finding_id} ({f.kind})"
    _check(isinstance(f, Finding), "detect must return Findings")
    _check(f.kind in detector.kinds, f"{tag}: kind not declared by {detector.id}")
    _check(f.detector_id == detector.id and f.detector_version == detector.version,
           f"{tag}: detector id/version mismatch")
    _check(bool(f.references), f"{tag}: no references")
    _check(f.fix is not None or "no mechanical fix" in f.summary.lower(),
           f"{tag}: neither a fix nor an explicit 'no mechanical fix' summary")
    _check(f.finding_id == _finding_id(f.detector_id, f.kind, f.scope),
           f"{tag}: finding_id is not finding_id(detector_id, kind, scope)")
    _check(list(f.scope.dims) == sorted(f.scope.dims), f"{tag}: scope dims not sorted")
    _check(f.category in _CATEGORIES and f.lever_class in _LEVER_CLASSES,
           f"{tag}: unknown category or lever class")
    _check(f.confidence in ("high", "medium", "low"), f"{tag}: unknown confidence")
    _check(len(f.title) <= 120 and len(f.summary) <= 400 and len(f.evidence) <= 20,
           f"{tag}: title/summary/evidence too long")
    _check(all(isinstance(t, int) and t >= 0 for t in (f.n_events, f.n_lanes, f.n_users)),
           f"{tag}: counts must be non-negative ints")
    for name, fig in _figures(f):
        _check(fig is None or isinstance(fig, Figure), f"{tag}: {name} is not a Figure")
    _check(isinstance(f.cost_observed, Figure), f"{tag}: cost_observed is required")
    names = {n for n, _ in f.scope.dims}
    if f.audience == "self":
        _check(ctx.self_principal is not None, f"{tag}: self finding without a self principal")
    else:
        _check(f.audience == "org", f"{tag}: audience must be org or self")
        _check("principal" not in names, f"{tag}: org finding scoped by principal")
        _check(not names & {"session", "session_key"} or ctx.break_glass is not None,
               f"{tag}: org finding names a session without break-glass")
    if dict(f.scope.dims).get("billing_class") == "allowance":
        _check(f.title.startswith("Allowance headroom:"), f"{tag}: allowance title")
        _check(all(fig is None or fig.basis is Basis.LIST_EQUIVALENT for _, fig in _figures(f)),
               f"{tag}: allowance figures must be list-equivalent")
    dims = dict(f.scope.dims)
    copilot_scope = dims.get("product") == "copilot" or dims.get("billing_class") == "pool"
    if f.headroom is not None:  # R-E20: pool headroom only on Copilot scopes, list-equivalent
        _check(copilot_scope and isinstance(f.headroom, Figure)
               and f.headroom.basis is Basis.LIST_EQUIVALENT,
               f"{tag}: headroom must be LIST_EQUIVALENT on a product=copilot / pool scope")
    if f.fix is not None and f.fix.config_patch:
        table = catalog.COPILOT_ALLOWLIST if f.fix.target == "github-copilot" else catalog.ALLOWLIST
        for key, _ in f.fix.config_patch:
            _check(key in table, f"{tag}: config key {key!r} not allowlisted")
    for lever_id in f.lever_ids:
        catalog.lever(lever_id)


def _by_id(findings: Iterable[Finding]) -> list[str]:
    return sorted(_canonical(to_json(f)) for f in findings)


def assert_detector_conforms(detector: Detector, lanes: Sequence[Lane],
                             ctx: AnalysisContext) -> list[Finding]:
    """Conformance of a :class:`~tokenbill.core.protocols.Detector` (SPEC §3.18, §10.1).

    Every finding: declared kind; matching detector id/version; references; a fix or an explicit
    "no mechanical fix" summary; labeled Figures; ``finding_id == finding_id(detector_id, kind,
    scope)``; sorted scope; audience rules (no principal in org scopes, sessions only with
    break-glass, self findings only with a self principal); allowance findings list-equivalent with
    the "Allowance headroom:" title; allowlisted config keys (``COPILOT_ALLOWLIST`` for fixes
    targeting ``github-copilot``) and catalog lever ids (both lever tables); ``headroom`` only on
    ``product=copilot`` / ``pool`` scopes and LIST_EQUIVALENT (R-E20). Runs are deterministic, and
    **shard invariance** holds: running separately on each ``(team, lane_kind)`` group of *lanes*
    (with ``ctx.shard`` set) and concatenating equals one run over all lanes (skipped for
    detectors requiring ``aggregates``, which ignore lanes). Detectors with ``aggregate=True``
    (run once per run, ruling R-E17) are checked for **lane independence** instead: the findings
    for ``lanes=[]`` equal those for *lanes*. Returns the findings of the full run.
    """
    _check(isinstance(detector, Detector), "not a Detector (protocol surface)")
    _check(isinstance(detector.id, str) and isinstance(detector.version, str),
           "id and version must be str")
    _check(isinstance(detector.kinds, tuple) and all(isinstance(k, str) for k in detector.kinds),
           "kinds must be a tuple of str")
    _check(isinstance(detector.requires, frozenset), "requires must be a frozenset")
    _check(detector.requires <= ctx.capabilities, "ctx lacks the detector's required capabilities")
    findings = detector.detect(lanes, ctx)
    _check(isinstance(findings, list), "detect must return a list")
    ids = [f.finding_id for f in findings]
    _check(len(ids) == len(set(ids)), "duplicate finding ids")
    for f in findings:
        _check_finding(detector, f, ctx)
    again = detector.detect(lanes, ctx)
    _check(_by_id(again) == _by_id(findings), "detect is not deterministic")
    if getattr(detector, "aggregate", False):
        alone = detector.detect([], ctx)
        _check(_by_id(alone) == _by_id(findings),
               "lane independence: an aggregate=True detector's findings depend on the lanes")
    elif "aggregates" not in detector.requires:
        groups: dict[tuple[str | None, str], list[Lane]] = {}
        for lane in lanes:
            groups.setdefault((lane.team, lane.kind.value), []).append(lane)
        sharded: list[Finding] = []
        for (team, kind), members in sorted(groups.items(), key=lambda kv: (kv[0][0] or "",
                                                                            kv[0][1])):
            shard_ctx = dataclasses.replace(ctx, shard=ShardKey(team=team, lane_kind=kind))
            sharded.extend(detector.detect(members, shard_ctx))
        _check(_by_id(sharded) == _by_id(findings),
               "shard invariance: per-(team, lane_kind) runs differ from one run over all lanes")
    return findings


# =============================================================================================
# smoke pipeline on fakes (gate F)
# =============================================================================================

_SMOKE_DAY = "2026-09-23"
SMOKE_ORG_KEY = bytes(range(160, 192))
SMOKE_NAME_KEY = bytes(range(192, 224))
_SMOKE_MAIN = "agent_product:claude_code,lane_kind:main"


def _smoke_result() -> IngestResult:
    """Two teams: ``payments`` (six developers, 5m main lanes idling past the TTL, one fast-mode
    lane) and ``mobile`` (two developers, bursty lanes)."""
    requests: list[Request] = []
    sessions: list[Session] = []
    day = _date_start_ms(_SMOKE_DAY) + 9 * 3_600_000
    for team, users, gap, fast_user in (("payments", 6, 420, 0), ("mobile", 2, 30, None)):
        for u in range(users):
            lane_key = f"lane-{team}-{u}"
            skey = f"S-{team}-{u}"
            attr = {"principal": f"r_{team}{u}", "team": team, "cost_center": "cc-eng",
                    "agent_product": "claude_code", "workload_class": "interactive",
                    "billing_path": "api_key"}
            speed = "fast" if u == fast_user else "standard"
            ts = day + u * 60_000
            prev = 0
            for seq in range(4):
                total = 100_000 + 2000 * seq
                if gap > 300 or seq == 0:
                    usage = {"cache_write_5m": total, "output": 500}
                else:
                    usage = {"cache_read": prev, "cache_write_5m": total - prev, "output": 500}
                msg = f"msg-{team}-{u}-{seq}"
                requests.append(make_request(
                    lane_key, seq, ts + seq * gap * 1000, usage, "claude-opus-5-5",
                    session_key=skey, request_id=stable_id("rq", "anthropic", msg),
                    message_id=msg, attribution=attr, billing_path="api_key", speed=speed,
                    source=SourceRef(adapter="claude-code", source_id="s_smoke",
                                     locator=f"line:{len(requests)}", fidelity=Fidelity.FULL,
                                     priority=40)))
                prev = total
            sessions.append(_shell(lane_key, skey, LaneKind.MAIN, f"ws:{team}"))
    src = SourceInfo(source_id="s_smoke", adapter="claude-code", name_hmac=_h("smoke"),
                     sha256=hashlib.sha256(b"smoke").hexdigest(), bytes=1,
                     name_key_id=key_id(SMOKE_NAME_KEY), principal_key_id=None)
    result = _result(src, requests, sessions=sessions)
    result.capabilities = frozenset({"usage_sequence", "timing", "ttl_split", "lanes_exact"})
    return result


class SmokeTtlDetector:
    """The trivial registered test detector of :func:`smoke_pipeline_on_fakes`: per cohort
    ``(team, lane_kind, billing_class)``, the TTL-expiry miss events of ``core.transitions`` with
    their billed rewrite cost (EXACT) and the ``ttl=1h`` replay saving (ESTIMATED)."""

    id = "test.smoke-ttl"
    version = "1"
    kinds = ("ttl-expiry",)
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """One ``ttl-expiry`` finding per cohort with at least one TTL-expiry miss event."""
        from tokenbill.core import findings as fh
        from tokenbill.core.transitions import classify_transitions

        cohorts: dict[tuple, list[Lane]] = {}
        for lane in lanes:
            cohorts.setdefault(fh.cohort_key(lane), []).append(lane)
        out: list[Finding] = []
        for (team, kind, bclass), members in sorted(cohorts.items(),
                                                    key=lambda kv: tuple(x or "" for x in kv[0])):
            basis = Basis.LIST_EQUIVALENT if bclass == "allowance" else ctx.pricer.basis
            rewrite = 0
            events = 0
            users: set[str] = set()
            first = None
            hit_lanes = []
            for lane in members:
                reqs = {r.request_id: r for r in lane.requests}
                lane_hit = False
                for t in classify_transitions(lane, pricer=ctx.pricer, rules=ctx.rules):
                    if not t.is_miss_event or t.cause != "ttl-expiry":
                        continue
                    req = reqs[t.request_id]
                    mw, mu = fh.miss_waste(t, req)
                    si = req.serving_inference
                    assert si is not None
                    ts = req.ts_start_ms
                    rewrite += fh.rate_nano(ctx.pricer, si.pricing, ts, "cache_write_5m", mw)
                    rewrite += fh.rate_nano(ctx.pricer, si.pricing, ts, "uncached_input", mu)
                    events += 1
                    lane_hit = True
                    first = ts if first is None else min(first, ts)
                    if req.attribution.principal:
                        users.add(req.attribution.principal)
                if lane_hit:
                    hit_lanes.append(lane)
            if not events:
                continue
            recoverable = None
            if ctx.replayer is not None:
                policy = Policy(name="ttl-1h", ttl=((f"lane_kind:{kind}", "1h"),))
                res = ctx.replayer.replay(hit_lanes, policy, mode="documented",
                                          pricer=ctx.pricer, rules=ctx.rules,
                                          calibration=ctx.calibration)
                recoverable = res.saving
            scope = fh.make_scope(team=team, lane_kind=kind,
                                  billing_class="allowance" if bclass == "allowance" else None)
            out.append(fh.build_finding(
                finding_id=fh.finding_id(self.id, "ttl-expiry", scope), detector_id=self.id,
                kind="ttl-expiry", detector_version=self.version, category="lever",
                lever_class="cache_transform", audience="org",
                title=f"Cache TTL expiries in {team or 'unattributed'} {kind} lanes",
                summary="Requests after idle gaps longer than the cache TTL rewrote the prefix.",
                scope=scope, n_events=events, n_lanes=len(hit_lanes), n_users=len(users),
                first_seen_ms=first or 0, cost_observed=exact(rewrite, basis),
                recoverable=recoverable, lever_ids=("cc.prompt_cache_ttl.main",),
                fix=None if recoverable is None else _smoke_fix(),
                references=("cc-miss-taxonomy-ground-truth",)))
        return out


def _smoke_fix() -> Any:
    from tokenbill.core.types import Fix

    return Fix(text="Set the main-conversation prompt cache TTL to 1h.",
               config_patch=(("promptCacheTtl", '"1h"'),),
               target="claude-code-managed-settings", doc_url=None, gates=("claude-code>=2.1.242",))


def _smoke_saving(pricer: Pricer) -> Callable[[Lane, Policy], int]:
    def fn(lane: Lane, policy: Policy) -> int:
        from tokenbill.core.policy import lane_matches

        saving = 0
        if any(lane_matches(sel, lane) for sel, ttl in policy.ttl if ttl == "1h"):
            for prev, req in itertools.pairwise(lane.requests):
                si = req.serving_inference
                prev_si = prev.serving_inference
                if si is None or prev_si is None:
                    continue
                if req.ts_start_ms - prev.ts_start_ms > 300_000:
                    reuse = min(prev_si.usage.cache_read + prev_si.usage.cache_write,
                                si.usage.total_input)
                    rates = pricer.unit_rates(si.pricing, ts_ms=req.ts_start_ms)
                    if rates is not None:
                        saving += (rates.bucket_nano("cache_write_5m", reuse)
                                   - rates.bucket_nano("cache_read", reuse))
            first = lane.requests[0].serving_inference if lane.requests else None
            if first is not None:
                rates = pricer.unit_rates(first.pricing, ts_ms=lane.requests[0].ts_start_ms)
                if rates is not None:
                    w = first.usage.cache_write
                    saving -= rates.bucket_nano("cache_write_1h", w) - rates.bucket_nano(
                        "cache_write_5m", w)
        if policy.fast_off:
            for req in lane.requests:
                si = req.serving_inference
                if si is None or si.pricing.speed != "fast":
                    continue
                fast = pricer.price_inference(si, ts_ms=req.ts_start_ms).figure.nano or 0
                std = dataclasses.replace(si, pricing=dataclasses.replace(si.pricing,
                                                                          speed="standard"))
                saving += fast - (pricer.price_inference(std, ts_ms=req.ts_start_ms).figure.nano
                                  or 0)
        return saving

    return fn


def smoke_pipeline_on_fakes(*, seed: int = 0) -> Any:
    """Compose the foundation contracts end to end on fakes (SPEC §3.18; merge gate F): builders →
    :class:`MemoryStore` → :class:`FakePricer` → ``core.transitions`` → a registered test detector
    (:class:`SmokeTtlDetector`, through ``core.registry.run_detectors``) → :class:`FakeReplayer` →
    ``core.kanon`` → ``core.shapley`` → a :class:`~tokenbill.core.types.RunResult`. Needs F-SEM
    (``core.transitions``, ``core.findings``, ``core.policy``, ``core.shapley``,
    ``core.cache_rules``), imported here lazily."""
    from tokenbill.core import kanon, registry
    from tokenbill.core.cache_rules import RulesTable
    from tokenbill.core.shapley import shapley_exact
    from tokenbill.core.transitions import classify_transitions
    from tokenbill.core.types import (
        ActionPlan,
        BillSummary,
        LeverResult,
        PrivacyInfo,
        RateCardInfo,
        RunResult,
    )

    pricer = FakePricer()
    rules = RulesTable()
    store = MemoryStore(org_key=SMOKE_ORG_KEY, pricer=pricer)
    result = _smoke_result()
    stats = store.ingest(result)
    window = (_date_start_ms(_SMOKE_DAY), _date_start_ms(_SMOKE_DAY) + _DAY_MS)
    w = {"since_ms": window[0], "until_ms": window[1]}
    lanes = list(store.iter_lanes(**w))
    misses = sum(1 for lane in lanes for t in classify_transitions(lane, pricer=pricer,
                                                                   rules=rules)
                 if t.is_miss_event)
    replayer = FakeReplayer.from_function(_smoke_saving(pricer))
    ctx = AnalysisContext(pricer=pricer, rules=rules, replayer=replayer, calibration=None,
                          window=window, capabilities=result.capabilities, now_ms=window[1])
    previous = dict(registry._PLUGIN_DETECTORS)
    registry._PLUGIN_DETECTORS[SmokeTtlDetector.id] = SmokeTtlDetector
    try:
        raw_findings = registry.run_detectors(lanes, ctx, only=[SmokeTtlDetector.id])
    finally:
        registry._PLUGIN_DETECTORS.clear()
        registry._PLUGIN_DETECTORS.update(previous)

    def users_of(scope: Scope) -> int:
        where = {k: v for k, v in scope.dims if k in ("team", "cost_center", "lane_kind",
                                                       "billing_class")}
        return store.count_users(where=where, **w)

    findings = kanon.rescope_findings(raw_findings, k=5, count_users=users_of)
    ttl = Policy(name="cc.prompt_cache_ttl.main", ttl=((_SMOKE_MAIN, "1h"),))
    fast = Policy(name="cc.fast_mode_opt_in", fast_off=True)
    levers = {"cc.prompt_cache_ttl.main": ttl, "cc.fast_mode_opt_in": fast}

    def joined(names: Iterable[str]) -> Policy:
        chosen = [levers[n] for n in sorted(names)]
        if not chosen:
            return Policy.observed()
        return Policy(name="+".join(p.name for p in chosen),
                      ttl=tuple(t for p in chosen for t in p.ttl),
                      fast_off=any(p.fast_off for p in chosen))

    def value(players: frozenset[str]) -> int:
        saving = replayer.replay(lanes, joined(players), mode="documented", pricer=pricer,
                                 rules=rules, calibration=None).saving.nano
        return saving or 0

    credits = shapley_exact(sorted(levers), value)
    joint = replayer.replay(lanes, joined(levers), mode="documented", pricer=pricer,
                            rules=rules, calibration=None)
    lever_results = []
    for lever_id in sorted(levers):
        spec_class = catalog.lever(lever_id)
        standalone = replayer.replay(lanes, levers[lever_id], mode="documented", pricer=pricer,
                                     rules=rules, calibration=None).saving
        shap = estimated(credits[lever_id], Basis.LIST, note="shapley credit (fake replay)")
        lever_results.append(LeverResult(
            lever_id=lever_id, lever_class=spec_class.lever_class, params=levers[lever_id].spec(),
            basis=Basis.LIST, standalone=standalone, shapley=shap,
            projected_monthly=estimated(credits[lever_id] * 30, Basis.LIST,
                                        note="30 days of the window saving (smoke)"),
            needs_eval=spec_class.needs_eval, upper_bound=spec_class.upper_bound, group="g1",
            finding_ids=tuple(f.finding_id for f in findings if lever_id in f.lever_ids)))
    total_credit = sum(credits.values())
    plan = ActionPlan(
        joint_saving=joint.saving,
        headline_monthly=estimated(total_credit * 30, Basis.LIST, note="smoke headline"),
        allowance_headroom_monthly=None, levers=tuple(lever_results),
        groups=(("g1", tuple(sorted(levers))),), method="shapley-exact", shapley_se=(),
        sample=f"shapley on {len(lanes)}/{len(lanes)} lanes (seed {seed})")
    items = [(inf, att.ts_start_ms) for lane in lanes for req in lane.requests
             for att in req.attempts for inf in att.inferences]
    breakdown = kanon.publish(store.aggregate(group_by=["team"], **w), k=5)
    bill = BillSummary(total=fake_price_total(pricer, items), esr=None,
                       breakdowns=(("team", breakdown),))
    return RunResult(
        command="smoke", window=window,
        inputs=((result.source, stats["requests"], 0),),
        privacy=PrivacyInfo(content_tier=ContentTier.NONE, key_id=key_id(SMOKE_ORG_KEY),
                            identity_mode="central-ingest", k=5,
                            suppressed_groups=breakdown.suppressed_rows),
        rate_card=RateCardInfo(sha256=pricer.rate_card_sha256, layers=("facts",), stale_rows=(),
                               contract=None, basis=pricer.basis),
        bill=bill, findings=tuple(findings), action_plan=plan, replays=(joint,),
        notes=(f"miss events: {misses}", f"lanes: {len(lanes)}"))



# =============================================================================================
# MemoryRecordStore (GitHub Copilot record store fake, K-5; addendum §7.2)
# =============================================================================================

#: Where keys of ``ExtRecordStore.count_users`` for licenses / activity (CP-STORE; ``surface`` and
#: ``editor_family`` both name the seat's last activity surface).
RECORD_WHERE_KEYS = frozenset({"team", "cost_center", "org", "plan", "bucket", "product",
                               "editor_family", "surface", "date_from", "date_to"})
_RECORD_SOURCES = ("licenses", "activity")


def _day_in(date: str, lo: int, hi: int) -> bool:
    return _day_overlaps(date, lo, hi)


def _dates_ok(date: str, clause: Mapping[str, str]) -> bool:
    lo, hi = clause.get("date_from"), clause.get("date_to")
    return (lo is None or date >= lo) and (hi is None or date <= hi)


def _eq(actual: object, wanted: str) -> bool:
    """``where`` equality: ``""`` matches a missing (None) value."""
    return actual is None if wanted == "" else actual == wanted


def _rec_window(window: Mapping[str, int]) -> tuple[int, int]:
    unknown = set(window) - {"since_ms", "until_ms"}
    if unknown:
        raise UsageError(f"unknown window key(s): {', '.join(sorted(unknown))}")
    lo, hi = window.get("since_ms"), window.get("until_ms")
    return (lo if lo is not None else 0, hi if hi is not None else _FOREVER_MS)


def _source_rank(rec: Any) -> int:
    """0 for a 28-day rollup row (``source_kind`` ending ``.28day``), else 1: a daily row of the
    same natural key always wins over a 28-day row, whatever the fetch order (addendum §5.5)."""
    return 0 if str(getattr(rec, "source_kind", "")).endswith(".28day") else 1


def _newer(new: Any, old: Any) -> bool:
    """Daily-over-28-day, then latest-fetch-wins upsert; ties go to the canonically larger version
    (order independent)."""
    return ((_source_rank(new), new.fetched_ms, _canonical(to_json(new)))
            > (_source_rank(old), old.fetched_ms, _canonical(to_json(old))))


class MemoryRecordStore:
    """Dict-backed :class:`~tokenbill.core.protocols.ExtRecordStore` for ``LicenseSnapshot`` /
    ``ActivityDay`` / ``ConfigSnapshot`` (the fake of CP-STORE's ``CopilotRecordStore``).

    ``MemoryRecordStore(ledger=None, *, name="copilot", org_key_id=None, now_ms=0)``:

    * **Key-id check** (R-E21): ``put(result, *, principal_key_id)`` stores licenses and activity
      only when *principal_key_id* is one of the ledger's accepted key ids — ``meta()``
      ``org_key_id`` or ``adopted_key_id`` (read at every ``put``, so a key id the ledger adopted
      is accepted afterwards) — or the constructor's *org_key_id*; otherwise they are skipped and
      counted as ``dq.principal_key_mismatch``. Configuration rows carry no person and are always
      stored. Every stored row keeps its key id (:meth:`principal_key_id_of`), so per-person joins
      stay inside one key id.
    * Natural keys are ``core.records.record_key``; an upsert keeps the version with the latest
      ``fetched_ms`` (ties: canonical order), so re-ingest is idempotent — also with ``org=None`` —
      and order independent.
    * ``count_users(source="licenses" | "activity")``: distinct principals (never ids) over the
      rows in the window matching *where* (:data:`RECORD_WHERE_KEYS`; ``""`` matches None;
      ``date_from`` / ``date_to`` inclusive). Only when no license (activity) row exists in the
      window does it fall back to the ``n_people`` of the ``seat_counts`` (``activity_counts``)
      configuration rows of aggregate-only bundles: seat-count rows are summed per entity and
      snapshot day (they partition that entity's seats) and the largest such sum is returned;
      activity-count rows give their largest ``n_people`` — both lower bounds.
    * ``retain`` deletes license and activity rows dated before the cut (configuration rows are
      team-level and kept); ``purge`` deletes a principal's rows and / or everything before a time,
      with an audit row that names no person.
    """

    def __init__(self, ledger: LedgerStore | None = None, *, name: str = "copilot",
                 org_key_id: str | None = None, now_ms: int = 0) -> None:
        self.name = name
        self._ledger = ledger
        self._org_key_id = org_key_id
        self._now_ms = now_ms
        self._licenses: dict[str, tuple[LicenseSnapshot, str]] = {}
        self._activity: dict[str, tuple[ActivityDay, str]] = {}
        self._config: dict[str, ConfigSnapshot] = {}
        self._audit: list[tuple[int, str, str, str]] = []

    # ---------- writes ----------

    def accepted_key_ids(self) -> frozenset[str]:
        """The principal key ids ``put`` accepts now: the ledger's own and adopted key ids and the
        constructor's *org_key_id*."""
        ids: set[str] = set()
        if self._ledger is not None:
            meta = self._ledger.meta()
            ids.update((meta.get("org_key_id", ""), meta.get("adopted_key_id", "")))
        if self._org_key_id:
            ids.add(self._org_key_id)
        return frozenset(ids - {""})

    @staticmethod
    def _upsert(table: dict[str, Any], rec: Any, value: Any) -> None:
        key = record_key(rec)
        old = table.get(key)
        if old is None or _newer(rec, old[0] if isinstance(old, tuple) else old):
            table[key] = value

    def put(self, result: IngestResult, *, principal_key_id: str | None) -> dict[str, int]:
        """Store the licenses, activity days and configuration rows of *result*; returns counts
        ``licenses``, ``activity``, ``config``, ``skipped`` and ``dq.principal_key_mismatch``."""
        if not isinstance(result, IngestResult):
            raise UsageError("put expects an IngestResult")
        counts = {"licenses": 0, "activity": 0, "config": 0, "skipped": 0,
                  DQ_PRINCIPAL_KEY_MISMATCH: 0}
        people = [*result.licenses, *result.activity]
        if people and (principal_key_id is None
                       or principal_key_id not in self.accepted_key_ids()):
            counts["skipped"] = counts[DQ_PRINCIPAL_KEY_MISMATCH] = len(people)
        else:
            kid = principal_key_id or ""
            for lic in result.licenses:
                self._upsert(self._licenses, lic, (lic, kid))
            for day in result.activity:
                self._upsert(self._activity, day, (day, kid))
            counts["licenses"], counts["activity"] = len(result.licenses), len(result.activity)
        for cfg in result.config:
            self._upsert(self._config, cfg, cfg)
        counts["config"] = len(result.config)
        return counts

    # ---------- reads ----------

    def licenses(self, **window: int) -> list[LicenseSnapshot]:
        """Stored seat snapshots dated in the window, by (snapshot date, natural key)."""
        lo, hi = _rec_window(window)
        return [rec for key, (rec, _) in sorted(self._licenses.items(),
                                                key=lambda kv: (kv[1][0].snapshot_date, kv[0]))
                if _day_in(rec.snapshot_date, lo, hi)]

    def activity(self, **window: int) -> list[ActivityDay]:
        """Stored activity days dated in the window, by (date, natural key)."""
        lo, hi = _rec_window(window)
        return [rec for key, (rec, _) in sorted(self._activity.items(),
                                                key=lambda kv: (kv[1][0].date_utc, kv[0]))
                if _day_in(rec.date_utc, lo, hi)]

    def config(self, **window: int) -> list[ConfigSnapshot]:
        """Stored configuration rows whose ``snapshot_ms`` is in the window, by (time, key)."""
        lo, hi = _rec_window(window)
        return [rec for key, rec in sorted(self._config.items(),
                                           key=lambda kv: (kv[1].snapshot_ms, kv[0]))
                if lo <= rec.snapshot_ms < hi]

    def principal_key_id_of(self, rec: LicenseSnapshot | ActivityDay) -> str | None:
        """The principal key id a stored license / activity row was accepted under (None when the
        row is not stored)."""
        table = self._licenses if isinstance(rec, LicenseSnapshot) else self._activity
        stored = table.get(record_key(rec))
        return stored[1] if stored is not None and stored[0] == rec else None

    # ---------- counting people ----------

    @staticmethod
    def _check_record_where(where: Mapping[str, str]) -> dict[str, str]:
        clause = dict(where or {})
        person = sorted(set(clause) & PERSON_DIMS)
        if person:
            raise PrivacyError(f"filtering by {', '.join(person)} is not allowed")
        unknown = sorted(set(clause) - RECORD_WHERE_KEYS)
        if unknown:
            raise UsageError(f"unknown record filter key(s): {', '.join(unknown)}")
        return clause

    @staticmethod
    def _license_match(rec: LicenseSnapshot, clause: Mapping[str, str]) -> bool:
        values = {"team": rec.team, "cost_center": rec.cost_center, "org": rec.org,
                  "plan": rec.plan, "bucket": rec.last_activity_bucket, "product": rec.product,
                  "editor_family": rec.last_activity_surface,
                  "surface": rec.last_activity_surface}
        return _dates_ok(rec.snapshot_date, clause) and all(
            _eq(values[k], v) for k, v in clause.items() if k in values)

    @staticmethod
    def _activity_match(rec: ActivityDay, clause: Mapping[str, str]) -> bool:
        values: dict[str, object] = {"team": rec.team, "cost_center": rec.cost_center,
                                     "product": rec.product}
        for k, v in clause.items():
            if k in ("date_from", "date_to"):
                continue
            if k not in values:
                if v != "":
                    return False  # activity days carry no org / plan / bucket / surface
                continue
            if not _eq(values[k], v):
                return False
        return _dates_ok(rec.date_utc, clause)

    @staticmethod
    def _config_match(cfg: ConfigSnapshot, clause: Mapping[str, str]) -> bool:
        attrs = dict(cfg.attrs)
        for k, v in clause.items():
            if k in ("date_from", "date_to"):
                continue
            if k == "org":
                ok = cfg.entity_id == f"org:{v}"
            elif k == "cost_center":
                ok = cfg.entity_id == f"cc:{v}"
            elif k == "product":
                ok = v == "github_copilot"
            elif k in ("editor_family", "surface"):
                ok = _eq(attrs.get("surface"), v)
            else:
                ok = _eq(attrs.get(k), v)
            if not ok:
                return False
        return _dates_ok(_date_of(cfg.snapshot_ms), clause)

    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str],
                    source: str) -> int:
        """Distinct people (never ids) of *source* (``licenses`` | ``activity``) matching *where*;
        see the class docstring for the count-row fallback."""
        if source not in _RECORD_SOURCES:
            raise UsageError(f"unknown record count source {source!r} (licenses | activity)")
        clause = self._check_record_where(where)
        w = {"since_ms": since_ms, "until_ms": until_ms}
        if source == "licenses":
            rows: list[Any] = self.licenses(**w)
            match = self._license_match
            kind = "seat_counts"
        else:
            rows = self.activity(**w)
            match = self._activity_match
            kind = "activity_counts"
        if rows:
            return len({r.principal for r in rows if match(r, clause)})
        summary = [c for c in self.config(**w) if c.kind == kind and self._config_match(c, clause)]
        if kind == "activity_counts":
            return max((int(dict(c.attrs).get("n_people") or 0) for c in summary), default=0)
        per_snapshot: dict[tuple[str, str], int] = {}
        for c in summary:
            key = (c.entity_id, _date_of(c.snapshot_ms))
            per_snapshot[key] = per_snapshot.get(key, 0) + int(dict(c.attrs).get("n_people") or 0)
        return max(per_snapshot.values(), default=0)

    # ---------- retention, purge, audit ----------

    def retain(self, *, identity_before_ms: int) -> int:
        """Delete license and activity rows dated before the UTC day of *identity_before_ms*;
        returns the number of rows deleted."""
        cut = _date_of(identity_before_ms)
        doomed_l = [k for k, (rec, _) in self._licenses.items() if rec.snapshot_date < cut]
        doomed_a = [k for k, (rec, _) in self._activity.items() if rec.date_utc < cut]
        for k in doomed_l:
            del self._licenses[k]
        for k in doomed_a:
            del self._activity[k]
        return len(doomed_l) + len(doomed_a)

    def purge(self, *, principal: str | None, before_ms: int | None, actor: str) -> int:
        """Delete a principal's license and activity rows and / or every row before *before_ms*;
        returns the number of rows deleted and writes an audit row without the identity."""
        if principal is None and before_ms is None:
            raise UsageError("purge needs principal or before_ms")
        cut = _date_of(before_ms) if before_ms is not None else None
        removed = 0
        for table, date_of in ((self._licenses, lambda r: r.snapshot_date),
                               (self._activity, lambda r: r.date_utc)):
            for key, (rec, _) in list(table.items()):
                if rec.principal == principal or (cut is not None and date_of(rec) < cut):
                    del table[key]
                    removed += 1
        if before_ms is not None:
            for key, cfg in list(self._config.items()):
                if cfg.snapshot_ms < before_ms:
                    del self._config[key]
                    removed += 1
        detail: dict[str, object] = {"by": "principal" if principal is not None else "before_ms",
                                     "rows": removed}
        if before_ms is not None:
            detail["before_ms"] = before_ms
        self._audit.append((self._now_ms, actor, "purge", _canonical(detail)))
        return removed

    def audit_log(self) -> list[tuple[int, str, str, str]]:
        """The audit rows ``(ts_ms, actor, action, detail_json)`` (extension, not protocol)."""
        return list(self._audit)


# =============================================================================================
# conformance: record store (K-5)
# =============================================================================================

#: The org key the record-store conformance suite pseudonymizes its people with; the store under
#: test must accept ``p_`` values under ``key_id(RECORD_STORE_ORG_KEY)``.
RECORD_STORE_ORG_KEY = bytes(range(224, 256))
_RS_DAY = "2026-09-20"


def _rs_p(name: str) -> str:
    return pseudonym(RECORD_STORE_ORG_KEY, "p", name)


def _rs_result(licenses: Sequence[LicenseSnapshot] = (), activity: Sequence[ActivityDay] = (),
               config: Sequence[ConfigSnapshot] = (), *, source_id: str = "rs") -> IngestResult:
    src = SourceInfo(source_id=source_id, adapter="github-copilot-seats",
                     name_hmac=_h(source_id), sha256=hashlib.sha256(source_id.encode()).hexdigest(),
                     bytes=1, name_key_id=None, principal_key_id=key_id(RECORD_STORE_ORG_KEY))
    result = _result(src, [])
    result.licenses, result.activity, result.config = list(licenses), list(activity), list(config)
    return result


def _record_batches() -> list[IngestResult]:
    """Five batches: two orgs (one person seated in both), a revised and a stale license version,
    an activity-report seat without an org, activity days, count and budget rows, old rows."""
    from tokenbill.core.builders import make_activity, make_config, make_license

    t0 = _date_start_ms(_RS_DAY)
    lic = make_license
    b1 = _rs_result([lic(_rs_p("u1"), snapshot_date=_RS_DAY, team="a", org="org-a", fetched_ms=1),
                     lic(_rs_p("u2"), snapshot_date=_RS_DAY, team="a", org="org-a",
                         last_activity_bucket="none_90d", fetched_ms=1),
                     lic(_rs_p("u3"), snapshot_date=_RS_DAY, team="b", org="org-a",
                         plan="enterprise", fetched_ms=1),
                     lic(_rs_p("u1"), snapshot_date=_RS_DAY, team="a", org="org-b",
                         fetched_ms=1)],
                    [make_activity(_rs_p("u1"), date_utc=_RS_DAY, team="a"),
                     make_activity(_rs_p("u2"), date_utc=_RS_DAY, team="a")], source_id="rs1")
    b2 = _rs_result([lic(_rs_p("u2"), snapshot_date=_RS_DAY, team="a", org="org-a",
                         last_activity_bucket="31-90", fetched_ms=5),
                     lic(_rs_p("u1"), snapshot_date=_RS_DAY, team="a", org="org-a",
                         last_activity_bucket="8-30", fetched_ms=0)], source_id="rs2")
    b3 = _rs_result([lic(_rs_p("u4"), snapshot_date=_RS_DAY, team="b", org=None, plan="unknown",
                         assigned_via_team=None, source_kind="github.copilot_activity_report",
                         fetched_ms=2)],
                    config=[make_config("seat_counts", {"team": "c", "plan": "business",
                                                        "bucket": "0-7", "n": 7, "n_people": 7},
                                        entity_id="org:org-c", snapshot_ms=t0),
                            make_config("run_flags", {"promo_eligible": True}, snapshot_ms=t0)],
                    source_id="rs3")
    b4 = _rs_result(activity=[make_activity(_rs_p("u3"), date_utc=_RS_DAY, team="b",
                                            counts={"interactions": 4, "ide:intellij": 2}),
                              make_activity(_rs_p("u4"), date_utc=_RS_DAY, team="b")],
                    config=[make_config("budget", {"scope": "user", "amount_nano": 0,
                                                   "team": "a"},
                                        entity_id="budget:b1", snapshot_ms=t0)],
                    source_id="rs4")
    b5 = _rs_result([lic(_rs_p("u5"), snapshot_date="2026-06-01", team="a", org="org-a")],
                    [make_activity(_rs_p("u5"), date_utc="2026-06-01", team="a")],
                    source_id="rs5")
    return [b1, b2, b3, b4, b5]


def _record_dump(store: ExtRecordStore) -> str:
    w = {"since_ms": 0, "until_ms": _FOREVER_MS}
    return _canonical({name: sorted(_canonical(to_json(r)) for r in getattr(store, name)(**w))
                       for name in ("licenses", "activity", "config")})


def _call_record_factory(factory: Callable[..., ExtRecordStore], path: Path) -> ExtRecordStore:
    try:
        params = [p for p in inspect.signature(factory).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):  # pragma: no cover - builtins
        params = []
    return factory(path, RECORD_STORE_ORG_KEY) if len(params) >= 2 else factory(path)


def assert_record_store_conforms(factory: Callable[..., ExtRecordStore], *,
                                 permutations: int = 12, seed: int = 0) -> dict[str, Any]:
    """Conformance of an :class:`~tokenbill.core.protocols.ExtRecordStore` (K-5, addendum §7.2).

    *factory* is called with a fresh database path (``factory(path)``, or ``factory(path,
    org_key)`` when it takes two positional parameters) and must return an empty record store
    that accepts ``p_`` values under ``key_id(RECORD_STORE_ORG_KEY)`` (e.g. through its ledger's
    ``meta``). Checks: the five batches of :func:`_record_batches` round-trip; re-ingest is
    idempotent (incl. a license with ``org=None``); latest-fetch-wins per natural key; order
    independence over *permutations* orders; a batch under another key id stores no person
    (``dq.principal_key_mismatch``); windows; ``count_users`` for licenses and activity equals a
    brute-force distinct count (a person seated through two orgs counted once) and falls back to
    ``seat_counts`` / ``activity_counts`` rows only without person rows; person filters raise
    ``PrivacyError``; ``retain`` deletes old license / activity rows and keeps configuration;
    ``purge(principal=…)`` deletes that person's rows only. Returns a summary."""
    import tempfile

    batches = _record_batches()
    w = {"since_ms": 0, "until_ms": _FOREVER_MS}
    kid = key_id(RECORD_STORE_ORG_KEY)
    with tempfile.TemporaryDirectory(prefix="tb-record-store-") as tmp:
        paths = (Path(tmp) / f"rs{i}.db" for i in itertools.count())
        store = _call_record_factory(factory, next(paths))
        _check(isinstance(store, ExtRecordStore), "not an ExtRecordStore (protocol surface)")
        _check(isinstance(store.name, str) and bool(store.name), "name must be a non-empty str")
        for b in batches:
            counts = store.put(b, principal_key_id=kid)
            _check(isinstance(counts, dict), "put returns counts")
            _check(not counts.get(DQ_PRINCIPAL_KEY_MISMATCH),
                   "the store refused p_ values under key_id(RECORD_STORE_ORG_KEY): the factory "
                   "must return a store whose ledger accepts that key id (e.g. take "
                   "(path, org_key) and open the ledger with org_key first)")
        reference = _record_dump(store)
        lics = store.licenses(**w)
        _check(len(lics) == 6, f"expected 6 stored licenses, got {len(lics)}")
        u2 = [x for x in lics if x.principal == _rs_p("u2")]
        _check(len(u2) == 1 and u2[0].last_activity_bucket == "31-90",
               "latest-fetch-wins: the revised license replaces the older version")
        u1a = [x for x in lics if x.principal == _rs_p("u1") and x.org == "org-a"]
        _check(len(u1a) == 1 and u1a[0].last_activity_bucket == "0-7",
               "an older fetch never replaces a newer one")
        _check(any(x.org is None and x.assigned_via_team is None for x in lics),
               "an activity-report seat (org None, assignment unknown) round-trips")
        _check(len(store.activity(**w)) == 5 and len(store.config(**w)) == 3,
               "activity days and configuration rows stored")
        for b in batches:
            store.put(b, principal_key_id=kid)
        _check(_record_dump(store) == reference, "re-ingesting the same batches changed the store")
        orders = _permutations(len(batches), permutations, seed)
        for order in orders:
            other = _call_record_factory(factory, next(paths))
            for i in order:
                other.put(batches[i], principal_key_id=kid)
            _check(_record_dump(other) == reference, f"put order {order} changed the store")
        # --- key-id check ---
        foreign = _rs_result([_rs_license("stranger")], source_id="rs-x")
        counts = store.put(foreign, principal_key_id="k_not_accepted")
        _check(counts.get(DQ_PRINCIPAL_KEY_MISMATCH, 0) >= 1 and _record_dump(store) == reference,
               "a batch under another principal key id stores no person (dq code)")
        # --- windows ---
        recent = {"since_ms": _date_start_ms("2026-09-01"), "until_ms": _FOREVER_MS}
        _check(len(store.licenses(**recent)) == 5 and len(store.activity(**recent)) == 4,
               "license / activity windows filter by date")
        # --- counting people ---
        lic_now = store.licenses(**recent)
        for where in ({}, {"team": "a"}, {"org": "org-a"}, {"plan": "business"},
                      {"bucket": "none_90d"}, {"team": "b", "plan": "enterprise"}, {"org": ""},
                      {"product": "github_copilot"}, {"date_from": _RS_DAY, "date_to": _RS_DAY}):
            brute = {x.principal for x in lic_now if all(
                (getattr(x, {"bucket": "last_activity_bucket"}.get(k, k)) == (v or None))
                for k, v in where.items() if k not in ("date_from", "date_to"))}
            got = store.count_users(where=where, source="licenses", **recent)
            _check(got == len(brute), f"count_users(licenses, {where}) = {got} != {len(brute)}")
        _check(store.count_users(where={}, source="licenses", **recent) == 4,
               "a person seated through two organizations is counted once")
        act = store.activity(**recent)
        for where in ({}, {"team": "b"}, {"team": "a"}):
            brute = {x.principal for x in act if all(getattr(x, k) == v for k, v in where.items())}
            _check(store.count_users(where=where, source="activity", **recent) == len(brute),
                   f"count_users(activity, {where}) differs from a brute-force count")
        for bad in ({"principal": _rs_p("u1")}, {"session_key": "s"}):
            try:
                store.count_users(where=bad, source="licenses", **recent)
            except PrivacyError:
                pass
            else:
                raise AssertionError(f"count_users(where={bad}) must raise PrivacyError")
        summary_only = _call_record_factory(factory, next(paths))
        summary_only.put(dataclasses.replace(batches[2], licenses=[], activity=[]),
                         principal_key_id=kid)
        _check(summary_only.count_users(where={"team": "c"}, source="licenses", **recent) == 7,
               "without licenses, seat_counts n_people rows count the team's people")
        # --- retention and purge ---
        removed = store.retain(identity_before_ms=_date_start_ms("2026-08-01"))
        _check(removed == 2 and len(store.config(**w)) == 3 and len(store.licenses(**w)) == 5,
               "retain deletes old license / activity rows only")
        gone = store.purge(principal=_rs_p("u1"), before_ms=None, actor="conformance")
        _check(gone == 3 and all(x.principal != _rs_p("u1") for x in store.licenses(**w))
               and all(x.principal != _rs_p("u1") for x in store.activity(**w))
               and len(store.licenses(**w)) == 3,
               "purge(principal) deletes that person's rows only")
    return {"batches": len(batches), "orders": len(orders)}


def _rs_license(name: str) -> LicenseSnapshot:
    """A seat snapshot of *name* (pseudonymized under ``RECORD_STORE_ORG_KEY``) on the suite's
    day, for record-store checks."""
    from tokenbill.core.builders import make_license

    return make_license(_rs_p(name), snapshot_date=_RS_DAY, team="z", org="org-z")


# =============================================================================================
# conformance: Copilot additions of a LedgerStore (K-5; STORE amendment A-2)
# =============================================================================================

ADOPTED_EXPORT_KEY = bytes(range(0, 64, 2))
_OTHER_EXPORT_KEY = bytes(range(1, 64, 2))


def _copilot_source(source_id: str, adapter: str, principal_key: bytes | None,
                    name_key: bytes | None = None) -> SourceInfo:
    return SourceInfo(source_id=source_id, adapter=adapter, name_hmac=_h(source_id),
                      sha256=hashlib.sha256(source_id.encode()).hexdigest(), bytes=1,
                      name_key_id=key_id(name_key) if name_key is not None else None,
                      principal_key_id=key_id(principal_key) if principal_key else None)


def _copilot_ledger_batch(source: SourceInfo, principal_key: bytes | None, *,
                          users: Sequence[str] = ("u1", "u2"), fetched_ms: int = 0,
                          finality: str = "final", day: str = "2026-09-10",
                          name_key: bytes | None = None) -> IngestResult:
    """Copilot requests and AI-usage cost lines of *users* (``p_`` under *principal_key*, or ``r_``
    refs when it is None); with *name_key* the cost lines carry an ``h_`` repository name."""
    from tokenbill.core.builders import make_ai_usage_row

    requests, lines, aggs = [], [], []
    for i, user in enumerate(users):
        who = pseudonym(principal_key, "p", user) if principal_key else f"r_{user}"
        req = make_request(f"CP-{source.source_id}-{i}", 0, _ts(day) + i * 60_000,
                           {"uncached_input": 1000, "output": 100}, "claude-sonnet-5",
                           provider="github", channel="github_copilot",
                           billing_path="copilot_pool",
                           request_id=stable_id("rq", source.source_id, i),
                           attribution={"principal": who, "team": "t1",
                                        "billing_path": "copilot_pool",
                                        "extra": (("gateway", "gw-1"),)})
        requests.append(req)
        if principal_key:
            repo = pseudonym(name_key, "h", f"repo-{i}") if name_key else None
            line, agg = make_ai_usage_row(date_utc=day, principal=who, credits=str(10 + i),
                                          team="t1", fetched_ms=fetched_ms, finality=finality,
                                          repo=repo)
            lines.append(line)
            aggs.append(agg)
    result = _result(source, requests, aggregates=aggs, cost_lines=lines)
    result.stats = {"records": len(requests), "rounding_remainders": len(users)}
    return result


def assert_store_copilot_conforms(factory: Callable[..., LedgerStore]) -> dict[str, Any]:
    """The Copilot additions of a :class:`~tokenbill.core.protocols.LedgerStore` (K-5, STORE A-2).

    *factory* is called like :func:`assert_store_conforms`'s, plus the keyword ``adopt_key_ids``.
    Checks: ``source_stats`` (``LedgerStats``) sums the ingested stats, per adapter too;
    ``count_users(source="cost_lines")`` equals a brute-force distinct count; LIST_EQUIVALENT
    Copilot lines land in ``PricedTotal.pool`` (``allowance`` stays empty) and
    ``ClusterDay.pool_nano``; cluster kind ``gateway``; a Copilot cost line re-fetched later
    replaces the earlier version even when that one was final; key-id adoption (R-E21) — a keyless
    store adopts the first ``copilot-export`` key id (``meta`` ``adopted_key_id``, ``org_key_mode
    == "adopted"``), keeps the bundle's ``h_`` cost-line names (adopted name key id), nulls
    another adapter's ``p_`` values (``dq.principal_key_mismatch``) and ``h_`` names, refuses a
    second bundle key id (``UsageError``) and ``r_`` principals (``PrivacyError``); an
    org-keyed store keeps its own pseudonyms and the adopted ones side by side. Returns a
    summary."""
    pricer = FakePricer()
    w = {"since_ms": 0, "until_ms": _FOREVER_MS}
    kw = {"name_key_id": key_id(STORE_NAME_KEY), "pricer": pricer}
    # --- org-keyed store: stats, cost-line users, pool, clusters, latest fetch ---
    store = factory(org_key=STORE_ORG_KEY, adopt_key_ids=False, **kw)
    _check(isinstance(store, LedgerStats), "the store implements LedgerStats (source_stats)")
    own = _copilot_source("cp-own", "github-ai-usage", STORE_ORG_KEY)
    store.ingest(_copilot_ledger_batch(own, STORE_ORG_KEY, users=("u1", "u2", "u3")),
                 pricer=pricer)
    stats = store.source_stats()
    _check(stats.get("records") == 3 and stats.get("rounding_remainders") == 3,
           "source_stats sums the ingested stats")
    _check(store.source_stats(adapter="otlp") == {}, "source_stats(adapter=…) filters")
    lines = store.cost_lines(**w)
    _check(len(lines) == 3, "Copilot cost lines stored")
    for where in ({}, {"team": "t1"}, {"channel": "github_copilot"}, {"team": "other"}):
        brute = {c.principal for c in lines if c.principal
                 and all(getattr(c, k) == v for k, v in where.items())}
        _check(store.count_users(where=where, source="cost_lines", **w) == len(brute),
               f"count_users(cost_lines, {where}) differs from a brute-force count")
    total = store.aggregate(group_by=[], **w)
    priced = total.rows[0].priced
    _check(priced.pool is not None and priced.pool.nano and priced.allowance is None
           and priced.exact.nano == 0, "Copilot LIST_EQUIVALENT lines go to PricedTotal.pool")
    days = store.cluster_days(cluster_kind="team", since="2026-09-01", until="2026-10-01")
    _check(sum(d.pool_nano for d in days) == priced.pool.nano
           and sum(d.allowance_nano for d in days) == 0, "ClusterDay.pool_nano")
    gw = store.cluster_days(cluster_kind="gateway", since="2026-09-01", until="2026-10-01")
    _check([d.cluster_id for d in gw] == ["gw-1"], "cluster kind gateway (R-E28)")
    newer = _copilot_ledger_batch(_copilot_source("cp-own-2", "github-ai-usage", STORE_ORG_KEY),
                                  STORE_ORG_KEY, users=("u1",), fetched_ms=9,
                                  finality="provisional")
    newer.cost_lines = [dataclasses.replace(newer.cost_lines[0], amount_nano=1)]
    store.ingest(newer, pricer=pricer)
    ids = {c.line_id: c for c in store.cost_lines(**w)}
    _check(ids[newer.cost_lines[0].line_id].amount_nano == 1,
           "latest-fetch-wins for Copilot cost lines")
    # --- keyless adoption ---
    keyless = factory(org_key=None, adopt_key_ids=True, **kw)
    bundle = _copilot_source("bundle-1", ADOPTABLE_ADAPTER, ADOPTED_EXPORT_KEY, ADOPTED_EXPORT_KEY)
    keyless.ingest(_copilot_ledger_batch(bundle, ADOPTED_EXPORT_KEY, name_key=ADOPTED_EXPORT_KEY),
                   pricer=pricer)
    meta = keyless.meta()
    adopted = key_id(ADOPTED_EXPORT_KEY)
    _check(meta.get("adopted_key_id") == adopted and meta.get("org_key_mode") == "adopted"
           and meta.get("org_key_id") == adopted, "a keyless store adopts the bundle key id")
    kept = {r.attribution.principal for r in keyless.iter_requests(**w)}
    _check(kept == {pseudonym(ADOPTED_EXPORT_KEY, "p", u) for u in ("u1", "u2")},
           "the adopted bundle's p_ values are kept")
    other = _copilot_source("vendor-x", "github-ai-usage", _OTHER_EXPORT_KEY, _OTHER_EXPORT_KEY)
    counts = keyless.ingest(_copilot_ledger_batch(other, _OTHER_EXPORT_KEY, users=("u9",),
                                                  name_key=_OTHER_EXPORT_KEY), pricer=pricer)
    _check(counts.get(DQ_PRINCIPAL_KEY_MISMATCH, 0) >= 1
           and keyless.meta().get("adopted_key_id") == adopted,
           "another adapter's key id is never adopted; its p_ values are nulled")
    repos = {c.principal: c.repo for c in keyless.cost_lines(**w)}
    _check(repos.get(None, "") is None
           and all(r is not None for p, r in repos.items() if p is not None),
           "h_ cost-line names are kept under the adopted name key id, nulled under another")
    second = _copilot_source("bundle-2", ADOPTABLE_ADAPTER, _OTHER_EXPORT_KEY, _OTHER_EXPORT_KEY)
    try:
        keyless.ingest(_copilot_ledger_batch(second, _OTHER_EXPORT_KEY), pricer=pricer)
    except UsageError:
        pass
    else:
        raise AssertionError("a second bundle key id must raise UsageError")
    collector = _copilot_source("collector", "copilot-cli", None)
    try:
        keyless.ingest(_copilot_ledger_batch(collector, None), pricer=pricer)
    except PrivacyError:
        pass
    else:
        raise AssertionError("r_ principals in a keyless store must raise PrivacyError")
    # --- org-keyed store with adoption: both key spaces side by side ---
    both = factory(org_key=STORE_ORG_KEY, adopt_key_ids=True, **kw)
    both.ingest(_copilot_ledger_batch(bundle, ADOPTED_EXPORT_KEY), pricer=pricer)
    both.ingest(_copilot_ledger_batch(_copilot_source("collector", "copilot-cli", None), None,
                                      users=("u7",)), pricer=pricer)
    principals = {r.attribution.principal for r in both.iter_requests(**w)}
    _check(pseudonym(STORE_ORG_KEY, "p", "u7") in principals
           and pseudonym(ADOPTED_EXPORT_KEY, "p", "u1") in principals
           and both.meta().get("org_key_id") == key_id(STORE_ORG_KEY)
           and both.meta().get("adopted_key_id") == adopted,
           "an org-keyed store keeps its own and the adopted pseudonyms side by side")
    return {"cost_line_users": len({c.principal for c in lines}), "adopted_key_id": adopted}

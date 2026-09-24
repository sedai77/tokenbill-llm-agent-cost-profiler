"""The rate card: resolution and pricing over layers (SPEC §6.2–§6.4, D26, D27, D36).

:class:`RateCard` implements :class:`~tokenbill.core.protocols.Pricer` over an ordered set of
:class:`~tokenbill.core.types.RateLayer` s and an optional contract overlay.

Resolution (§6.2): the row for ``(ctx.channel, ctx.model)`` (or an alias; channel ``"*"`` rows match
every channel) whose ``[effective_from, effective_to)`` contains the UTC date of ``ts_ms`` is looked
up layer by layer, highest precedence first — ``--model-price`` above ``--rates`` files above the
built-in registry; among layers of one kind, later ones win — and the first containing row decides
(a disabled row: ``"unverified rate row"``). ``claude_platform_aws`` and ``foundry`` fall back to
the ``anthropic_api`` rows when no layer has a row of their own; Copilot rows never fall back.
Then: ``replace_base`` modifiers set the base input/output rates; cache rates are the base input
rate × the row's multipliers; a long-context band replaces the banded buckets for the whole
request; every matching ``multiply`` modifier multiplies its buckets (exactly, so order does not
matter; per-request prices are not scaled by modifiers); the contract overlay comes last
(per-model overrides are final
prices, the multiplier scales every other bucket and the per-request prices; not on the
``subscription`` path nor on Copilot). Modifiers of all layers apply; one id defined by several
layers resolves to the highest layer's definition.

Unknown endpoint scope (§6.2 #4): when a modifier predicated on ``endpoint_scope`` would apply to
the row but for the scope, ``ctx.endpoint_scope == "unknown"`` resolves the global rates as the
point and attaches the regional rates as ``ResolvedRates.scope_range``; every line is then a range.
(A pre-4.5 Bedrock model, which has no regional premium, therefore stays exact.)

Pricing (§6.3) is per line with one rounding per line; see :meth:`RateCard.price_usage`. Buckets a
row does not price fall back to the nearest priced class (other write ↔ 5m write, then input;
reads → input) exactly like ``core.testing.FakePricer``; a Copilot row without any write price
folds writes into input as zero-width ESTIMATED lines (``dq.copilot_write_folded_to_input``).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from tokenbill.core.errors import PricingError, UsageError
from tokenbill.core.labels import Basis, Evidence, Figure, exact, unpriced
from tokenbill.core.models import normalize_model
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, ratio, token_nano
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    COPILOT_BILLING_PATHS,
    Inference,
    PricingContext,
    UsageBuckets,
    UsageSource,
    to_json,
)
from tokenbill.core.types import (
    ContractOverlay,
    DataQualityNote,
    Modifier,
    PricedInference,
    PricedLine,
    PricedTotal,
    RateCardInfo,
    RateLayer,
    RateRow,
    ResolvedRates,
    UnitRates,
)
from tokenbill.rates import billing_rules
from tokenbill.rates.schema import WILDCARD_CHANNEL, layer_kind

__all__ = [
    "COPILOT_CHANNEL",
    "DQ_COPILOT_BAND_HYPOTHESIS",
    "DQ_COPILOT_WRITE_FOLDED",
    "DQ_SCOPE_UNKNOWN",
    "DQ_STALE_RATE",
    "FALLBACK_CHANNELS",
    "STALE_AFTER_DAYS",
    "UNPRICED_REASONS",
    "RateCard",
    "no_cache_equivalent_nano",
    "price_total",
]

_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_MAX_SCALE = 24
_CACHE_LIMIT = 65_536
#: Unpriced reasons → the data-quality code reported for them (same strings as FakePricer).
UNPRICED_REASONS: Mapping[str, str] = MappingProxyType({
    "not priceable": "dq.unpriced_model",
    "no rate row": "dq.unpriced_model",
    "unverified rate row": "dq.unverified_rate_row",
    "model before effective date": "dq.model_before_effective_date",
    "promotion expired": "dq.promotion_expired",
})
DQ_SCOPE_UNKNOWN = "dq.scope_unknown"
DQ_STALE_RATE = "dq.stale_rate"
DQ_COPILOT_BAND_HYPOTHESIS = "dq.copilot_band_hypothesis"
DQ_COPILOT_WRITE_FOLDED = "dq.copilot_write_folded_to_input"
#: A used row verified more than this many days before the run date is stale (§6.7).
STALE_AFTER_DAYS = 45
FALLBACK_CHANNELS = ("claude_platform_aws", "foundry")
COPILOT_CHANNEL = "github_copilot"
_PRICE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h",
               "cache_write_other")
_BUCKET_TO_KEY = MappingProxyType({
    "uncached_input": "input", "input": "input", "uncached": "input", "output": "output",
    "cache_read": "cache_read", "cache_write_5m": "cache_write_5m",
    "cache_write_1h": "cache_write_1h", "cache_write_other": "cache_write_other"})
_WRITE_BUCKETS = ("cache_write_5m", "cache_write_1h", "cache_write_other")
_KIND_RANK = {"builtin": 0, "user": 1, "model-price": 2}
_RESOLVED_LAYER = {"builtin": "builtin", "user": "user", "model-price": "user"}


def _date_of(ts_ms: int) -> str:
    if type(ts_ms) is not int:
        raise UsageError("ts_ms must be an int (milliseconds since the Unix epoch)")
    try:
        return (_EPOCH + _dt.timedelta(days=ts_ms // _DAY_MS)).isoformat()
    except OverflowError:
        raise UsageError("ts_ms out of range") from None


def _dec_str(d: Decimal) -> str:
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _generation(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _effective_rate(rates: ResolvedRates, bucket: str) -> Decimal:
    """The rate for *bucket*: a bucket the row does not price falls back to the nearest class
    (other write ↔ 5m write, then input; reads → input), as ``core.testing.FakePricer``."""
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
    raise UsageError(f"unknown bucket {bucket!r}")


def _min_exponent(d: Decimal) -> int:
    """The exponent of *d*'s last non-zero digit (0 for zero), without rounding."""
    _, digits, exp = d.as_tuple()
    assert isinstance(exp, int)
    stripped = len(digits)
    while stripped and digits[stripped - 1] == 0:
        stripped -= 1
    return 0 if stripped == 0 else exp + (len(digits) - stripped)


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class RateCard:
    """A :class:`~tokenbill.core.protocols.Pricer` over rate layers and an optional contract.

    *layers* is a sequence of :class:`RateLayer` (a single layer is accepted too); *contract* a
    :class:`ContractOverlay` (basis CONTRACT on its channels, LIST elsewhere). The card records the
    ids of the rows it resolves (:meth:`stale_rows`, :meth:`data_quality`) and is picklable.
    """

    def __init__(self, layers: Sequence[RateLayer] | RateLayer,
                 contract: ContractOverlay | None = None) -> None:
        if isinstance(layers, RateLayer):
            layers = (layers,)
        layers = tuple(layers)
        if not layers or not all(isinstance(layer, RateLayer) for layer in layers):
            raise UsageError("RateCard needs at least one RateLayer")
        if contract is not None and not isinstance(contract, ContractOverlay):
            raise UsageError("contract must be a ContractOverlay")
        self.layers: tuple[RateLayer, ...] = layers
        self.contract: ContractOverlay | None = contract
        self.basis: Basis = Basis.CONTRACT if contract is not None else Basis.LIST
        ranked = sorted(enumerate(layers), key=lambda p: (_KIND_RANK[layer_kind(p[1])], p[0]),
                        reverse=True)
        #: layers highest precedence first: (kind, {(channel, name): rows sorted by date})
        self._ranked: list[tuple[str, dict[tuple[str, str], tuple[RateRow, ...]]]] = []
        self._rows_by_id: dict[str, tuple[RateRow, str]] = {}
        mods: dict[str, Modifier] = {}
        for _, layer in ranked:
            kind = layer_kind(layer)
            index: dict[tuple[str, str], list[RateRow]] = {}
            for row in layer.rows:
                self._rows_by_id.setdefault(row.row_id, (row, kind))
                for name in dict.fromkeys((row.model, *row.aliases)):
                    index.setdefault((row.channel, name), []).append(row)
            self._ranked.append((kind, {k: tuple(sorted(v, key=lambda r: r.effective_from))
                                        for k, v in index.items()}))
            for m in layer.modifiers:
                mods.setdefault(m.modifier_id, m)
        self._modifiers: tuple[Modifier, ...] = tuple(mods[k] for k in sorted(mods))
        self._scope_mods = tuple(m for m in self._modifiers if "endpoint_scope" in dict(m.when))
        self._overrides: dict[str, dict[str, Decimal]] = {}
        if contract is not None:
            for model, buckets in contract.overrides:
                key = normalize_model(model).model or model
                target = self._overrides.setdefault(key, {})
                for bucket, value in buckets:
                    if bucket != "web_search" and bucket not in _BUCKET_TO_KEY:
                        raise PricingError(f"contract {contract.name}: unknown override bucket "
                                           f"{bucket!r}")
                    target[bucket] = value
        payload = {"layers": [[layer.name, layer.sha256] for layer in layers],
                   "contract": to_json(contract) if contract is not None else None}
        self.rate_card_sha256: str = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        self._find_cache: dict[tuple[str, str, str], tuple[RateRow | None, str, str | None]] = {}
        self._rates_cache: dict[tuple[Any, ...], ResolvedRates] = {}
        self._used: set[str] = set()

    # ---------- introspection ----------

    @property
    def sha256(self) -> str:
        """SHA-256 of the canonical JSON of the layers in use and the contract (§6.7)."""
        return self.rate_card_sha256

    def modifiers(self) -> tuple[Modifier, ...]:
        """The effective modifiers (one per id, highest layer's definition)."""
        return self._modifiers

    def rows(self) -> tuple[RateRow, ...]:
        """Every row of every layer, highest precedence first."""
        return tuple(dict.fromkeys(row for _, index in self._ranked for rows in index.values()
                                   for row in rows))

    def list_card(self) -> RateCard:
        """The same layers without the contract overlay (list prices)."""
        return self if self.contract is None else RateCard(self.layers)

    def used_rows(self) -> tuple[str, ...]:
        """Ids of the rows this card has resolved so far, sorted."""
        return tuple(sorted(self._used))

    def stale_rows(self, *, today: str, row_ids: Iterable[str] | None = None,
                   max_age_days: int = STALE_AFTER_DAYS) -> tuple[str, ...]:
        """Ids of the used rows (or of *row_ids*) whose ``verified_on`` is more than
        *max_age_days* before *today*; ``--model-price`` rows are exempt (they are the run's own
        assertion)."""
        cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=max_age_days)).isoformat()
        out = []
        for row_id in sorted(set(self._used if row_ids is None else row_ids)):
            found = self._rows_by_id.get(row_id)
            if found is not None and found[1] != "model-price" and found[0].verified_on < cutoff:
                out.append(row_id)
        return tuple(out)

    def data_quality(self, *, today: str) -> list[DataQualityNote]:
        """``dq.stale_rate`` for the stale used rows (empty when none)."""
        stale = self.stale_rows(today=today)
        if not stale:
            return []
        detail = f"verified more than {STALE_AFTER_DAYS} days before {today}: " + ", ".join(stale)
        return [DataQualityNote(code=DQ_STALE_RATE, severity="warn", count=len(stale),
                                detail=detail[:256])]

    def info(self, *, today: str) -> RateCardInfo:
        """The ``rate_card`` block of run results (§14.1)."""
        return RateCardInfo(sha256=self.rate_card_sha256,
                            layers=tuple(layer.name for layer in self.layers),
                            stale_rows=self.stale_rows(today=today),
                            contract=self.contract.name if self.contract is not None else None,
                            basis=self.basis)

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state["_find_cache"] = {}
        state["_rates_cache"] = {}
        return state

    # ---------- resolution ----------

    def _candidates(self, channel: str, model: str) -> list[tuple[str, tuple[RateRow, ...]]]:
        out = []
        for kind, index in self._ranked:
            for key in ((channel, model), (WILDCARD_CHANNEL, model)):
                rows = index.get(key)
                if rows:
                    out.append((kind, rows))
        return out

    def _find(self, ctx: PricingContext, date: str) -> tuple[RateRow | None, str, str | None]:
        """(row, layer kind, unpriced reason) for *ctx* on *date*."""
        key = (ctx.channel, ctx.model, date)
        hit = self._find_cache.get(key)
        if hit is None:
            hit = self._find_uncached(ctx.channel, ctx.model, date)
            if len(self._find_cache) >= _CACHE_LIMIT:
                self._find_cache.clear()
            self._find_cache[key] = hit
        if hit[0] is not None:
            self._used.add(hit[0].row_id)
        return hit

    def _find_uncached(self, channel: str, model: str,
                       date: str) -> tuple[RateRow | None, str, str | None]:
        if not model:
            return None, "", "not priceable"
        cands = self._candidates(channel, model)
        if not cands and channel in FALLBACK_CHANNELS:
            cands = self._candidates("anthropic_api", model)
        if not cands:
            return None, "", "no rate row"
        for kind, rows in cands:
            for row in rows:
                if row.effective_from <= date and (row.effective_to is None
                                                   or date < row.effective_to):
                    return (row, kind, None) if row.enabled else (None, kind,
                                                                  "unverified rate row")
        every = [row for _, rows in cands for row in rows]
        if date < min(row.effective_from for row in every):
            return None, "", "model before effective date"
        last = max(every, key=lambda r: r.effective_to or "9999-12-31")
        if last.promotion is not None and last.effective_to is not None \
                and date >= last.effective_to:
            return None, "", "promotion expired"
        return None, "", "no rate row"

    @staticmethod
    def _matches(m: Modifier, row: RateRow, ctx: PricingContext, scope: str | None) -> bool:
        """Whether modifier *m* applies; ``scope=None`` ignores the endpoint_scope predicate."""
        for key, value in m.when:
            if key == "service_tier":
                ok = ctx.service_tier == value
            elif key == "speed":
                ok = ctx.speed == value
            elif key == "inference_geo":
                ok = ctx.inference_geo == value
            elif key == "endpoint_scope":
                ok = scope is None or scope == value or (value == "regional"
                                                         and scope == "multi_region")
            elif key == "channel_in":
                ok = ctx.channel in value.split(",")
            elif key == "model_in":
                ok = row.model in value.split(",")
            elif key == "routing":
                ok = ctx.routing == value
            elif key == "compliance_in":
                ok = ctx.compliance is not None and ctx.compliance in value.split(",")
            else:  # generation_gte (validated at load)
                ok = _generation(row.generation) >= _generation(value)
            if not ok:
                return False
        return True

    def _contract_applies(self, ctx: PricingContext, date: str) -> bool:
        c = self.contract
        if c is None or ctx.billing_path == "subscription":
            return False
        if ctx.billing_path in COPILOT_BILLING_PATHS or ctx.channel == COPILOT_CHANNEL:
            return False  # contract overlays never apply to Copilot credits (addendum §6.2 #3)
        if c.channels and ctx.channel not in c.channels:
            return False
        return c.effective_from <= date and (c.effective_to is None or date < c.effective_to)

    def _build(self, row: RateRow, kind: str, ctx: PricingContext, date: str, scope: str,
               band: bool) -> ResolvedRates:
        applicable = [m for m in self._modifiers if self._matches(m, row, ctx, scope)]
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
            "input": inp, "output": out, "cache_read": times(row.cache_read_mult),
            "cache_write_5m": times(row.cache_write_5m_mult),
            "cache_write_1h": times(row.cache_write_1h_mult),
            "cache_write_other": times(row.cache_write_other_mult),
        }
        if band:
            for bucket, value in row.long_context_usd_per_mtok:
                rates[_BUCKET_TO_KEY[bucket]] = value
        per_request = dict(row.per_request_usd)
        for m in applicable:
            if m.kind != "multiply" or m.factor is None:
                continue
            targets = set(_PRICE_KEYS) if "*" in m.applies_to else {
                _BUCKET_TO_KEY[b] for b in m.applies_to}
            for key in _PRICE_KEYS:
                value = rates[key]
                if key in targets and value is not None:
                    rates[key] = EXACT_CTX.multiply(value, m.factor)
            mod_ids.append(m.modifier_id)
            assumed = assumed or m.stacking == "assumed"
        layer = _RESOLVED_LAYER[kind]
        if self._contract_applies(ctx, date):
            c = self.contract
            assert c is not None
            overrides = self._overrides.get(row.model, {})
            overridden: set[str] = set()
            for bucket, value in overrides.items():
                if bucket == "web_search":
                    per_request["web_search"] = value
                    overridden.add(bucket)
                    continue
                key = _BUCKET_TO_KEY[bucket]
                rates[key] = value
                overridden.add(key)
            if c.multiplier is not None:
                for key in _PRICE_KEYS:
                    value = rates[key]
                    if key not in overridden and value is not None:
                        rates[key] = EXACT_CTX.multiply(value, c.multiplier)
                for name, value in list(per_request.items()):
                    if name not in overridden:
                        per_request[name] = EXACT_CTX.multiply(value, c.multiplier)
            layer = "contract"
            mod_ids.append(f"contract:{c.name}")
        return ResolvedRates(
            row_id=row.row_id, channel=ctx.channel, model=row.model,
            input=rates["input"],  # type: ignore[arg-type]
            output=rates["output"],  # type: ignore[arg-type]
            cache_read=rates["cache_read"], cache_write_5m=rates["cache_write_5m"],
            cache_write_1h=rates["cache_write_1h"], cache_write_other=rates["cache_write_other"],
            per_request=tuple(sorted(per_request.items())), modifier_ids=tuple(mod_ids),
            stacking_assumed=assumed, layer=layer, min_cacheable_tokens=row.min_cacheable_tokens,
            tokenizer_family=row.tokenizer_family, long_context_band=band)

    def _scope_priced(self, row: RateRow, ctx: PricingContext) -> bool:
        return any(self._matches(m, row, ctx, None) for m in self._scope_mods)

    def _rates(self, row: RateRow, kind: str, ctx: PricingContext, date: str,
               band: bool) -> ResolvedRates:
        key = (row.row_id, kind, ctx, date, band)
        hit = self._rates_cache.get(key)
        if hit is not None:
            return hit
        if ctx.endpoint_scope == "unknown" and self._scope_priced(row, ctx):
            low = self._build(row, kind, ctx, date, "global", band)
            high = self._build(row, kind, ctx, date, "regional", band)
            hit = dataclasses.replace(low, scope_range=high)
        else:
            hit = self._build(row, kind, ctx, date, ctx.endpoint_scope, band)
        if len(self._rates_cache) >= _CACHE_LIMIT:
            self._rates_cache.clear()
        self._rates_cache[key] = hit
        return hit

    def _resolve(self, ctx: PricingContext, ts_ms: int, total_input: int = 0, *,
                 band: bool | None = None) -> tuple[ResolvedRates | None, str | None,
                                                    RateRow | None]:
        if not isinstance(ctx, PricingContext):
            raise UsageError("ctx must be a PricingContext")
        date = _date_of(ts_ms)
        row, kind, reason = self._find(ctx, date)
        if row is None:
            return None, reason, None
        if band is None:
            band = row.long_context_threshold is not None and total_input > \
                row.long_context_threshold
        else:
            band = band and row.long_context_threshold is not None
        return self._rates(row, kind, ctx, date, band), None, row

    def resolve(self, ctx: PricingContext, *, ts_ms: int) -> ResolvedRates | None:
        """Resolved rates at the UTC date of *ts_ms* (None when unpriced; base, not band, rates)."""
        return self._resolve(ctx, ts_ms)[0]

    def unpriced_reason(self, ctx: PricingContext, *, ts_ms: int) -> str | None:
        """Why *ctx* does not resolve on that date (a key of :data:`UNPRICED_REASONS`), or None."""
        return self._resolve(ctx, ts_ms)[1]

    def unit_rates(self, ctx: PricingContext, *, ts_ms: int) -> UnitRates | None:
        """Exact integer unit rates of the point (low) rates (§6.4): the smallest scale ``s ≥ 6``
        making every bucket rate × 10^(s−6) integral; None when unpriced, when ``s`` would exceed
        24, or when the per-request web-search price is not a whole number of nano-USD."""
        rates = self._resolve(ctx, ts_ms)[0]
        if rates is None:
            return None
        values = {
            "uncached": rates.input, "cache_read": _effective_rate(rates, "cache_read"),
            "cache_write_5m": _effective_rate(rates, "cache_write_5m"),
            "cache_write_1h": _effective_rate(rates, "cache_write_1h"),
            "cache_write_other": _effective_rate(rates, "cache_write_other"),
            "output": rates.output,
        }
        web = dict(rates.per_request).get("web_search", Decimal(0))
        if _min_exponent(web) < -9:
            return None
        scale = max(6, 6 - min(_min_exponent(v) for v in values.values()))
        if scale > _MAX_SCALE:
            return None
        return UnitRates(scale_exp=scale, web_search_nano=int(web.scaleb(9, EXACT_CTX)),
                         row_id=rates.row_id,
                         **{k: int(v.scaleb(scale - 6, EXACT_CTX)) for k, v in values.items()})

    def min_cacheable_tokens(self, ctx: PricingContext, *, ts_ms: int) -> int | None:
        """The effective row's minimum cacheable prefix (None when unpriced or unknown)."""
        row = self._resolve(ctx, ts_ms)[2]
        return row.min_cacheable_tokens if row is not None else None

    def supports(self, ctx: PricingContext, feature: str, *, ts_ms: int) -> bool:
        """Whether the effective (enabled) row lists *feature* in ``supports``."""
        row = self._resolve(ctx, ts_ms)[2]
        return row is not None and feature in row.supports

    def tokenizer_family(self, ctx: PricingContext, *, ts_ms: int) -> str | None:
        """The effective row's tokenizer family (None when unpriced)."""
        row = self._resolve(ctx, ts_ms)[2]
        return row.tokenizer_family if row is not None else None

    # ---------- pricing ----------

    def _basis(self, ctx: PricingContext, rates: ResolvedRates | None) -> Basis:
        if ctx.billing_path == "subscription" or ctx.billing_path in COPILOT_BILLING_PATHS:
            return Basis.LIST_EQUIVALENT
        if rates is not None and rates.layer == "contract":
            return Basis.CONTRACT
        return Basis.LIST

    def price_inference(self, inf: Inference, *, ts_ms: int) -> PricedInference:
        """Price one inference (:meth:`price_usage` of its usage, context and flags); provider
        reported costs are never used."""
        if not isinstance(inf, Inference):
            raise UsageError("price_inference expects an Inference")
        priced = self.price_usage(inf.usage, inf.pricing, ts_ms=ts_ms, billable=inf.billable,
                                  usage_source=inf.usage_source, output_upper=inf.output_upper)
        return dataclasses.replace(priced, inference_id=inf.inference_id)

    def _band_b(self, row: RateRow, ctx: PricingContext, total_input: int) -> bool | None:
        """Copilot band hypothesis B when it disagrees with A (addendum §6.2 #4), else None."""
        if ctx.channel != COPILOT_CHANNEL or ctx.context_tier is None \
                or row.long_context_threshold is None:
            return None
        a = total_input > row.long_context_threshold
        b = ctx.context_tier == "long_context"
        return b if a != b else None

    def price_usage(self, usage: UsageBuckets, ctx: PricingContext, *, ts_ms: int,
                    billable: bool | None = True, usage_source: UsageSource = UsageSource.FINAL,
                    output_upper: int | None = None) -> PricedInference:
        """One :class:`PricedLine` per non-zero bucket and per server-tool counter, each rounded
        once; exactness per line (§6.3): ``billable False`` → every line EXACT 0 (kept); ``None``
        → every line ``[0, full]``; reconstructed usage → ``[point, point]`` ESTIMATED; a partial
        stream → per billing rule ``<provider>.abort.client`` (unknown: ``[0, full]``);
        unknown-TTL writes → ``[5m, 1h]`` with the point at ``write_ttl_hint`` (else 5m); unknown
        endpoint scope on a scope-priced row → every line ``[global, regional]``;
        ``MESSAGE_START_ONLY`` → the output line ``[logged, max(logged, output_upper)]`` at the
        output rate with the point at logged. Copilot: both billing paths on LIST_EQUIVALENT, band
        hypotheses A/B as a range with point A when ``ctx.context_tier`` disagrees with the
        request size, writes folded into input on rows without a write price, and a non-billable
        call EXACT $0 even without a row. Basis LIST_EQUIVALENT on the ``subscription`` path
        (D26), CONTRACT where the contract applies, else LIST."""
        if not isinstance(usage, UsageBuckets):
            raise UsageError("price_usage expects UsageBuckets")
        try:
            source = UsageSource(usage_source)
        except ValueError:
            raise UsageError("unknown usage_source") from None
        if billable is not None and type(billable) is not bool:
            raise UsageError("billable must be True, False or None")
        if output_upper is not None and (type(output_upper) is not int or output_upper < 0):
            raise UsageError("output_upper must be a non-negative int")
        rates, reason, row = self._resolve(ctx, ts_ms, usage.total_input)
        basis = self._basis(ctx, rates)
        if rates is None or row is None:
            if billable is False and ctx.channel == COPILOT_CHANNEL:
                # a non-billable Copilot call (utility model, nano-AIU 0): EXACT $0, not a gap
                return PricedInference(inference_id=None, lines=(), figure=exact(0, basis),
                                       exact_nano=0, estimated=None, unpriced_reason=None)
            why = reason or "no rate row"
            return PricedInference(inference_id=None, lines=(), figure=unpriced(why, basis),
                                   exact_nano=0, estimated=None, unpriced_reason=why)
        lines, notes = self._lines(usage, ctx, rates, billable=billable, source=source,
                                   output_upper=output_upper)
        band_b = self._band_b(row, ctx, usage.total_input)
        if band_b is not None and lines:
            alt = self._resolve(ctx, ts_ms, usage.total_input, band=band_b)[0]
            assert alt is not None
            alt_lines, _ = self._lines(usage, ctx, alt, billable=billable, source=source,
                                       output_upper=output_upper)
            lines = [_hypothesis_range(a, b) for a, b in zip(lines, alt_lines, strict=True)]
            notes.append("long-context band hypotheses A/B differ: range, point A "
                         f"({DQ_COPILOT_BAND_HYPOTHESIS})")
        if usage.web_search_requests and not any(ln.bucket == "web_search" for ln in lines):
            notes.append("web search requests unpriced (no per-request rate on the row)")
        provenance = (rates.row_id, *rates.modifier_ids)
        return _assemble(lines, basis, provenance, "; ".join(dict.fromkeys(notes)))

    def _lines(self, usage: UsageBuckets, ctx: PricingContext, rates: ResolvedRates, *,
               billable: bool | None, source: UsageSource,
               output_upper: int | None) -> tuple[list[PricedLine], list[str]]:
        high_rates = rates.scope_range
        folded = (ctx.channel == COPILOT_CHANNEL and rates.cache_write_5m is None
                  and rates.cache_write_1h is None and rates.cache_write_other is None)
        partial_exact: bool | None = None
        notes: list[str] = []
        if source is UsageSource.PARTIAL_STREAM:
            rule = billing_rules.rule_for(ctx.provider, "abort.client")
            partial_exact = rule.billable
            notes.append(f"partial stream: billing {rule.confidence} ({rule.rule_id})"
                         + ("" if partial_exact is not None else ": range [0, full]"))
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
                lo, hi = sorted((amount(*low), amount(*high)))
                point = min(max(point, lo), hi)
            if billable is False or partial_exact is False:
                point, lo, hi = 0, None, None
            elif billable is None or (source is UsageSource.PARTIAL_STREAM
                                      and partial_exact is None):
                lo, hi = 0, max(point, hi if hi is not None else point)
            elif source is UsageSource.ESTIMATED and lo is None:
                lo, hi = point, point
            lines.append(PricedLine(
                bucket=bucket, quantity=qty, unit_usd_per_mtok=_dec_str(rate), amount_nano=point,
                low_nano=lo, high_nano=hi, exact=lo is None, rate_row_id=rates.row_id,
                modifier_ids=rates.modifier_ids, layer=rates.layer))

        qty_of = {"uncached_input": usage.uncached_input, "cache_read": usage.cache_read,
                  "cache_write_5m": usage.cache_write_5m, "cache_write_1h": usage.cache_write_1h,
                  "cache_write_other": usage.cache_write_other}
        if high_rates is not None:
            notes.append(f"endpoint scope unknown: priced [global, regional] ({DQ_SCOPE_UNKNOWN})")
        for bucket, qty in qty_of.items():
            if not qty:
                continue
            rate = _effective_rate(rates, bucket)
            if folded and bucket in _WRITE_BUCKETS:
                emit(bucket, qty, rate, low=(qty, rate), high=(qty, rate))
                notes.append(f"writes folded to input ({DQ_COPILOT_WRITE_FOLDED})")
            elif high_rates is not None:
                emit(bucket, qty, rate, low=(qty, rate),
                     high=(qty, _effective_rate(high_rates, bucket)))
            else:
                emit(bucket, qty, rate)
        if usage.cache_write_unknown:
            notes.append(f"writes folded to input ({DQ_COPILOT_WRITE_FOLDED})" if folded
                         else "unknown-TTL cache writes priced [5m, 1h]")
            q = usage.cache_write_unknown
            low_rate = _effective_rate(rates, "cache_write_5m")
            top = high_rates if high_rates is not None else rates
            # a row without a 1h write price has no 1h writes: the range collapses to the 5m rate
            one_hour = rates.cache_write_1h is not None
            high_rate = _effective_rate(top, "cache_write_1h" if one_hour else "cache_write_5m")
            hint = _effective_rate(rates, "cache_write_1h") \
                if ctx.write_ttl_hint == "1h" and one_hour else low_rate
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
        return lines, notes


def _hypothesis_range(a: PricedLine, b: PricedLine) -> PricedLine:
    """Line *a* (hypothesis A) widened to cover line *b* (hypothesis B): point A, never exact."""
    lows = [x.low_nano if x.low_nano is not None else x.amount_nano for x in (a, b)]
    highs = [x.high_nano if x.high_nano is not None else x.amount_nano for x in (a, b)]
    return dataclasses.replace(a, low_nano=min(lows), high_nano=max(highs), exact=False)


def _assemble(lines: list[PricedLine], basis: Basis, provenance: tuple[str, ...],
              note: str) -> PricedInference:
    exact_nano = sum(ln.amount_nano for ln in lines if ln.exact)
    ranged = [ln for ln in lines if not ln.exact]
    if not ranged:
        figure = Figure(nano=exact_nano, evidence=Evidence.EXACT, basis=basis,
                        provenance=provenance, note=note)
        return PricedInference(inference_id=None, lines=tuple(lines), figure=figure,
                               exact_nano=exact_nano, estimated=None, unpriced_reason=None)
    point = sum(ln.amount_nano for ln in ranged)
    low = sum(ln.low_nano or 0 for ln in ranged)
    high = sum(ln.high_nano or 0 for ln in ranged)
    text = note or "range lines"
    est = Figure(nano=point, evidence=Evidence.ESTIMATED, basis=basis, low_nano=low,
                 high_nano=high, provenance=provenance, note=text)
    figure = Figure(nano=exact_nano + point, evidence=Evidence.ESTIMATED, basis=basis,
                    low_nano=exact_nano + low, high_nano=exact_nano + high,
                    provenance=provenance, note=text)
    return PricedInference(inference_id=None, lines=tuple(lines), figure=figure,
                           exact_nano=exact_nano, estimated=est, unpriced_reason=None)


# ---------------------------------------------------------------------------------------------
# totals
# ---------------------------------------------------------------------------------------------


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


def _coverage(priced_tokens: int, all_tokens: int) -> str:
    if all_tokens <= 0:
        return "1"
    value = ratio(max(priced_tokens, 0), all_tokens)
    assert value is not None
    return format(value.normalize(), "f")


def price_total(pricer: Pricer, items: Iterable[tuple[Inference, int]]) -> PricedTotal:
    """Price ``(inference, ts_ms)`` pairs into a :class:`PricedTotal` (§6.3).

    Exact lines on billed bases (LIST / CONTRACT) sum into ``exact``, their range lines into
    ``estimated``; LIST_EQUIVALENT figures go to ``allowance`` (billing path ``subscription``) or
    ``pool`` (the Copilot billing paths) and never into ``exact``. Inferences with ``billable
    False`` are not billable and are skipped; unpriced inferences are counted with their tokens;
    ``coverage`` = priced billable tokens / all billable tokens. Provider-reported costs are never
    added. The billed totals carry the common basis of their inferences (a mix of LIST and CONTRACT
    is labelled with the card's basis and a note).
    """
    exact_nano = est_point = est_low = est_high = 0
    has_est = False
    allowance, pool = _ListEquivalentSum(), _ListEquivalentSum()
    priced = unpriced_n = unpriced_tokens = all_tokens = 0
    bases: set[Basis] = set()
    for inf, ts_ms in items:
        if not isinstance(inf, Inference):
            raise UsageError("price_total expects (Inference, ts_ms) pairs")
        if inf.billable is False:
            continue
        p = pricer.price_inference(inf, ts_ms=ts_ms)
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
        bases.add(p.figure.basis)
        exact_nano += p.exact_nano
        if p.estimated is not None and p.estimated.nano is not None:
            has_est = True
            est_point += p.estimated.nano
            est_low += p.estimated.low_nano if p.estimated.low_nano is not None else 0
            est_high += p.estimated.high_nano if p.estimated.high_nano is not None else 0
    card_basis = pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST
    basis = next(iter(bases)) if len(bases) == 1 else card_basis
    note = "mixed bases: list and contract" if len(bases) > 1 else ""
    est_fig = (Figure(nano=est_point, evidence=Evidence.ESTIMATED, basis=basis, low_nano=est_low,
                      high_nano=est_high, note="range lines") if has_est else None)
    return PricedTotal(
        exact=Figure(nano=exact_nano, evidence=Evidence.EXACT, basis=basis, note=note),
        estimated=est_fig, allowance=allowance.figure(), priced_inferences=priced,
        unpriced_inferences=unpriced_n, unpriced_tokens=unpriced_tokens,
        coverage=_coverage(all_tokens - unpriced_tokens, all_tokens), pool=pool.figure())


def no_cache_equivalent_nano(pricer: Pricer, inference: Inference, ts_ms: int) -> int:
    """The ESR denominator (§6.3, §14.1): the same tokens with every input token at the uncached
    list rate and no discounts — standard service tier, direct routing, no contract (when the
    pricer offers ``list_card()``). 0 for non-billable or unpriced inferences."""
    if not isinstance(inference, Inference):
        raise UsageError("no_cache_equivalent_nano expects an Inference")
    if inference.billable is False:
        return 0
    list_card = getattr(pricer, "list_card", None)
    base: Pricer = list_card() if callable(list_card) else pricer
    usage = inference.usage
    flat = UsageBuckets(uncached_input=usage.total_input, output=usage.output,
                        web_search_requests=usage.web_search_requests)
    ctx = dataclasses.replace(inference.pricing, service_tier="standard", routing="direct")
    priced = base.price_usage(flat, ctx, ts_ms=ts_ms)
    return priced.figure.nano if priced.figure.nano is not None else 0

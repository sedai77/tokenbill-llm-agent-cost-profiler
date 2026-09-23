"""Test and fixture builders, the content canary, and ``FlatRates`` (SPEC §3.25).

Every package builds its fixtures with these helpers instead of sibling packages (SPEC §21 #4).
Builders are deterministic: default ids derive from their arguments with ``core.ids.stable_id``.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, unpriced
from tokenbill.core.lanes import _ttl_observed
from tokenbill.core.money import EXACT_CTX, decimal_to_nano, token_nano
from tokenbill.core.records import (
    AppendedItem,
    Attempt,
    Attribution,
    BlockRef,
    CacheDiagnostic,
    ContentFingerprint,
    CostLine,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneKind,
    Outcome,
    PricingContext,
    Request,
    RequestParams,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    UsageSource,
)
from tokenbill.core.types import PricedInference, PricedLine, ResolvedRates, UnitRates

__all__ = [
    "CANARY",
    "CANARY_EMAIL",
    "CANARY_KEYS",
    "FlatRates",
    "assert_no_canary",
    "lane_from_table",
    "make_aggregate",
    "make_attempt",
    "make_block",
    "make_cost_line",
    "make_ctx",
    "make_fingerprint",
    "make_inference",
    "make_lane",
    "make_request",
    "make_usage",
    "plant_canary",
    "unit_rates_from",
]

# ---------------------------------------------------------------------------------------------
# content canary (SPEC §8.8)
# ---------------------------------------------------------------------------------------------

CANARY = "TB-CANARY-7f3a91"
CANARY_EMAIL = f"canary.{CANARY}@example.com"
#: Keys whose string values carry user content in the sources Token Bill reads.
CANARY_KEYS = frozenset(
    {
        "text",
        "content",
        "thinking",
        "input",
        "prompt",
        "system",
        "systemPrompt",
        "cwd",
        "gitBranch",
        "file_path",
        "filePath",
        "path",
        "command",
        "description",
        "query",
        "url",
        "email",
        "user.email",
        "stdout",
        "stderr",
        "result",
        "summary",
        "title",
        "message",
    }
)


def plant_canary(obj: Any, *, keys: Iterable[str] | None = None) -> Any:
    """A deep copy of a JSON-like fixture with :data:`CANARY` appended to every string stored under
    a
    content key (:data:`CANARY_KEYS`, or *keys*), at any depth below it. A bare string gets the
    canary
    appended. Structural strings (``type``, ids, model names) outside content keys are left
    untouched so
    the fixture still parses."""
    wanted = frozenset(keys) if keys is not None else CANARY_KEYS

    def walk(value: Any, planting: bool) -> Any:
        if isinstance(value, str):
            return f"{value} {CANARY}" if planting else value
        if isinstance(value, Mapping):
            return {k: walk(v, planting or k in wanted) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, planting) for v in value]
        return copy.deepcopy(value)

    return walk(obj, isinstance(obj, str))


def assert_no_canary(*blobs: bytes | str) -> None:
    """Raise ``AssertionError`` when any blob contains the canary (as text or UTF-8 bytes)."""
    needle_b = CANARY.encode("utf-8")
    for i, blob in enumerate(blobs):
        hit = needle_b in blob if isinstance(blob, (bytes, bytearray)) else CANARY in str(blob)
        if hit:
            raise AssertionError(f"content canary found in blob #{i}")


# ---------------------------------------------------------------------------------------------
# record builders
# ---------------------------------------------------------------------------------------------


def make_usage(**buckets: int | None) -> UsageBuckets:
    """``UsageBuckets(**buckets)``."""
    return UsageBuckets(**buckets)  # type: ignore[arg-type]


def _as_usage(usage: UsageBuckets | Mapping[str, int] | None) -> UsageBuckets:
    if usage is None:
        return UsageBuckets()
    if isinstance(usage, UsageBuckets):
        return usage
    return UsageBuckets(**usage)


def make_ctx(model: str = "claude-opus-5-5", **kw: Any) -> PricingContext:
    """A PricingContext; provider defaults to ``openai`` for ``gpt-*`` models else ``anthropic``,
    channel
    to the provider's first-party channel, ``model_raw`` to *model*."""
    provider = kw.pop("provider", "openai" if model.startswith("gpt-") else "anthropic")
    channel = kw.pop("channel", "openai_api" if provider == "openai" else "anthropic_api")
    model_raw = kw.pop("model_raw", model)
    return PricingContext(
        provider=provider, channel=channel, model=model, model_raw=model_raw, **kw
    )


def make_inference(
    usage: UsageBuckets | Mapping[str, int] | None = None,
    *,
    model: str = "claude-opus-5-5",
    kind: InferenceKind | str = InferenceKind.MESSAGE,
    inference_id: str | None = None,
    ctx: PricingContext | None = None,
    usage_source: UsageSource | str = UsageSource.FINAL,
    billable: bool | None = True,
    billing_rule_id: str | None = None,
    output_upper: int | None = None,
    provider_reported_cost_nano: int | None = None,
    provider_reported_cost_basis: str | None = None,
    **ctx_kw: Any,
) -> Inference:
    """An Inference; ``ctx_kw`` go to :func:`make_ctx` when *ctx* is not given."""
    u = _as_usage(usage)
    context = ctx if ctx is not None else make_ctx(model, **ctx_kw)
    iid = inference_id or stable_id("inf", context.model, str(kind), repr(u))
    return Inference(
        inference_id=iid,
        kind=kind,
        usage=u,
        pricing=context,  # type: ignore[arg-type]
        usage_source=usage_source,
        billable=billable,  # type: ignore[arg-type]
        billing_rule_id=billing_rule_id,
        output_upper=output_upper,
        provider_reported_cost_nano=provider_reported_cost_nano,
        provider_reported_cost_basis=provider_reported_cost_basis,
    )


def make_attempt(
    inferences: Sequence[Inference] = (),
    *,
    ts_ms: int = 0,
    attempt_no: int = 0,
    attempt_id: str | None = None,
    outcome: Outcome | str = Outcome.OK,
    http_status: int | None = None,
    error_type: str | None = None,
    message_id: str | None = None,
    request_id_hint: str | None = None,
    model_served: str | None = None,
    stop_reason: str | None = None,
    ttft_ms: int | None = None,
    duration_ms: int | None = None,
    **kw: Any,
) -> Attempt:
    """An Attempt; the remaining Attempt fields (``retry_layer``, ``diagnostics``,
    ``applied_edits``,
    ``thinking_dropped``, …) pass through *kw*."""
    aid = attempt_id or stable_id("at", ts_ms, attempt_no, *(i.inference_id for i in inferences))
    return Attempt(
        attempt_id=aid,
        attempt_no=attempt_no,
        ts_start_ms=ts_ms,
        ttft_ms=ttft_ms,
        duration_ms=duration_ms,
        outcome=outcome,
        http_status=http_status,
        # type: ignore[arg-type]
        error_type=error_type,
        retry_layer=kw.pop("retry_layer", None),
        retry_after_ms=kw.pop("retry_after_ms", None),
        should_retry=kw.pop("should_retry", None),
        provider_request_id=request_id_hint,
        provider_message_id=message_id,
        model_served=model_served,
        stop_reason=stop_reason,
        inferences=tuple(inferences),
        **kw,
    )


def make_request(
    lane_key: str,
    seq: int,
    ts_ms: int,
    usage: UsageBuckets | Mapping[str, int] | None = None,
    model: str = "claude-opus-5-5",
    *,
    request_id: str | None = None,
    session_key: str = "s_test",
    attribution: Attribution | Mapping[str, Any] | None = None,
    params: RequestParams | None = None,
    kind: InferenceKind | str = InferenceKind.MESSAGE,
    usage_source: UsageSource | str = UsageSource.FINAL,
    billable: bool | None = True,
    billing_rule_id: str | None = None,
    output_upper: int | None = None,
    extra_inferences: Sequence[Inference] = (),
    attempts: Sequence[Attempt] | None = None,
    message_id: str | None = None,
    stop_reason: str | None = None,
    outcome: Outcome | str = Outcome.OK,
    diagnostics: CacheDiagnostic | None = None,
    applied_edits: Sequence[tuple[str, int]] = (),
    thinking_dropped: int = 0,
    fingerprint: ContentFingerprint | None = None,
    appended: Sequence[AppendedItem] = (),
    source: SourceRef | None = None,
    **ctx_kw: Any,
) -> Request:
    """One logical request with a single attempt whose serving inference carries *usage* on *model*.

    ``ctx_kw`` (``channel``, ``billing_path``, ``write_ttl_hint``, ``speed``, …) go to
    :func:`make_ctx`; a
    ``billing_path`` is mirrored into the attribution. *extra_inferences* precede the serving
    inference
    (e.g. a compaction iteration); *attempts* replaces the generated attempt entirely.
    """
    rid = request_id or stable_id("rq", lane_key, seq)
    if attribution is None:
        bp = ctx_kw.get("billing_path")
        attr = Attribution(billing_path=bp if bp not in (None, "unknown") else None)
    elif isinstance(attribution, Attribution):
        attr = attribution
    else:
        attr = Attribution(**attribution)
    if attempts is None:
        serving = make_inference(
            usage,
            model=model,
            kind=kind,
            inference_id=stable_id("inf", rid, 0),
            usage_source=usage_source,
            billable=billable,
            billing_rule_id=billing_rule_id,
            output_upper=output_upper,
            **ctx_kw,
        )
        infs = (*extra_inferences, serving)
        attempts = (
            make_attempt(
                infs,
                ts_ms=ts_ms,
                attempt_id=stable_id("at", rid, 0),
                outcome=outcome,
                message_id=message_id,
                stop_reason=stop_reason,
                model_served=model,
                diagnostics=diagnostics,
                applied_edits=tuple(applied_edits),
                thinking_dropped=thinking_dropped,
            ),
        )
    return Request(
        request_id=rid,
        session_key=session_key,
        lane_key=lane_key,
        seq=seq,
        attribution=attr,
        params=params if params is not None else RequestParams(model_requested=model),
        attempts=tuple(attempts),
        fingerprint=fingerprint,
        appended=tuple(appended),
        source=source,
    )


def make_lane(
    requests: Sequence[Request],
    kind: LaneKind | str = LaneKind.MAIN,
    events: Sequence[LaneEvent] = (),
    scope: str = "ws:test",
    *,
    lane_key: str | None = None,
    session_key: str | None = None,
    parent_lane_key: str | None = None,
    lane_exact: bool = True,
) -> Lane:
    """A Lane over *requests* (their lane/session keys unless given); ``ttl_observed`` is
    computed."""
    reqs = tuple(requests)
    key = lane_key or (reqs[0].lane_key if reqs else "lane-0")
    skey = session_key or (reqs[0].session_key if reqs else "s_test")
    return Lane(
        lane_key=key,
        session_key=skey,
        kind=kind,
        parent_lane_key=parent_lane_key,  # type: ignore[arg-type]
        cache_scope_key=scope,
        requests=reqs,
        events=tuple(events),
        ttl_observed=_ttl_observed(reqs),
        lane_exact=lane_exact,
    )


def lane_from_table(
    rows: Iterable[Sequence[int]],
    *,
    model: str = "claude-opus-5-5",
    lane_key: str = "lane-0",
    kind: LaneKind | str = LaneKind.MAIN,
    scope: str = "ws:test",
    events: Sequence[LaneEvent] = (),
    session_key: str = "s_test",
    **request_kw: Any,
) -> Lane:
    """A lane from rows of ``(ts_s, R, W5, W1, U, O)``: seconds since 0, cache reads, 5m writes, 1h
    writes,
    uncached input, output. ``request_kw`` go to :func:`make_request` for every row."""
    requests = []
    for seq, row in enumerate(rows):
        ts_s, r, w5, w1, u, o = row
        usage = UsageBuckets(
            uncached_input=u, cache_read=r, cache_write_5m=w5, cache_write_1h=w1, output=o
        )
        requests.append(
            make_request(
                lane_key,
                seq,
                int(round(ts_s * 1000)),
                usage,
                model,
                session_key=session_key,
                **request_kw,
            )
        )
    return make_lane(
        requests, kind=kind, events=events, scope=scope, lane_key=lane_key, session_key=session_key
    )


def make_block(
    h: str,
    *,
    tier: str = "messages",
    kind: str = "text",
    n_bytes: int = 100,
    role: str | None = "user",
    est_tokens: int | None = None,
    h_sorted: str | None = None,
    h_norm: str | None = None,
    **kw: Any,
) -> BlockRef:
    """A BlockRef; ``h_sorted`` / ``h_norm`` default to *h*; other fields pass through *kw*."""
    return BlockRef(
        h=h,
        h_sorted=h_sorted if h_sorted is not None else h,
        h_norm=h_norm if h_norm is not None else h,
        tier=tier,
        kind=kind,
        role=role,
        n_bytes=n_bytes,
        est_tokens=est_tokens,
        **kw,
    )


def make_fingerprint(blocks: Sequence[BlockRef], key_id: str = "k_test") -> ContentFingerprint:
    """A ContentFingerprint whose ``tier_end`` is computed from the blocks' tiers (wire order)."""
    blocks = tuple(blocks)
    ends = []
    for tier in ("tools", "system", "messages"):
        last = max((i + 1 for i, b in enumerate(blocks) if b.tier == tier), default=0)
        ends.append(max(last, ends[-1] if ends else 0))
    return ContentFingerprint(key_id=key_id, blocks=blocks, tier_end=(ends[0], ends[1], ends[2]))


def make_aggregate(
    usage: UsageBuckets | Mapping[str, int] | None = None,
    *,
    source_kind: str = "anthropic.usage_report",
    bucket_start_ms: int = 0,
    bucket_end_ms: int = 86_400_000,
    dims: Mapping[str, str] | None = None,
    agg_id: str | None = None,
    reported_cost_nano: int | None = None,
    reported_cost_basis: str | None = None,
    list_cost_nano: int | None = None,
    finality: str = "provisional",
    fetched_ms: int = 0,
) -> UsageAggregate:
    """A UsageAggregate; *dims* default to channel ``anthropic_api`` and model
    ``claude-opus-5-5``."""
    d = dict(dims) if dims is not None else {"channel": "anthropic_api", "model": "claude-opus-5-5"}
    pairs = tuple(sorted(d.items()))
    u = _as_usage(usage)
    aid = agg_id or stable_id(
        "ag", source_kind, bucket_start_ms, bucket_end_ms, *(f"{k}={v}" for k, v in pairs)
    )
    return UsageAggregate(
        agg_id=aid,
        source_kind=source_kind,
        bucket_start_ms=bucket_start_ms,
        bucket_end_ms=bucket_end_ms,
        dims=pairs,
        usage=u,
        reported_cost_nano=reported_cost_nano,
        reported_cost_basis=reported_cost_basis,
        list_cost_nano=list_cost_nano,
        finality=finality,
        fetched_ms=fetched_ms,
    )


def make_cost_line(
    amount_nano: int = 0,
    *,
    date_utc: str = "2026-09-10",
    channel: str = "anthropic_api",
    model: str | None = "claude-opus-5-5",
    source_kind: str = "anthropic.cost_report",
    line_id: str | None = None,
    description: str | None = None,
    workspace_id: str | None = None,
    cost_type: str | None = "tokens",
    token_type: str | None = None,
    sku: str | None = None,
    service_tier: str | None = None,
    inference_geo: str | None = None,
    endpoint_scope: str | None = None,
    **kw: Any,
) -> CostLine:
    """A CostLine; other CostLine fields (``list_amount_nano``, ``finality``, …) pass through
    *kw*."""
    desc = (
        description
        if description is not None
        else f"{model or 'unknown'} {token_type or cost_type}"
    )
    lid = line_id or stable_id(
        "cl",
        source_kind,
        date_utc,
        channel,
        workspace_id or "",
        model or "",
        cost_type or "",
        token_type or "",
        sku or "",
    )
    return CostLine(
        line_id=lid,
        source_kind=source_kind,
        date_utc=date_utc,
        channel=channel,
        workspace_id=workspace_id,
        description=desc,
        model=model,
        cost_type=cost_type,
        token_type=token_type,
        sku=sku,
        service_tier=service_tier,
        inference_geo=inference_geo,
        endpoint_scope=endpoint_scope,
        amount_nano=amount_nano,
        **kw,
    )


# ---------------------------------------------------------------------------------------------
# FlatRates: a trivially simple, exact Pricer
# ---------------------------------------------------------------------------------------------

_MAX_SCALE = 24


def _dec_str(d: Decimal) -> str:
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def unit_rates_from(
    resolved: ResolvedRates, *, web_search_usd: Decimal = Decimal("0")
) -> UnitRates | None:
    """Exact integer unit rates for *resolved*: the smallest ``s ≥ 6`` making every bucket rate
    (USD/MTok × 10^(s−6)) integral; None when ``s`` would exceed 24 (SPEC §6.4)."""
    rates = {
        "uncached": resolved.input,
        "cache_read": resolved.cache_read or Decimal(0),
        "cache_write_5m": resolved.cache_write_5m or Decimal(0),
        "cache_write_1h": resolved.cache_write_1h or Decimal(0),
        "cache_write_other": resolved.cache_write_other or Decimal(0),
        "output": resolved.output,
    }
    for s in range(6, _MAX_SCALE + 1):
        scaled = {k: EXACT_CTX.multiply(v, Decimal(10) ** (s - 6)) for k, v in rates.items()}
        if all(v == v.to_integral_value() for v in scaled.values()):
            return UnitRates(
                scale_exp=s,
                web_search_nano=decimal_to_nano(web_search_usd),
                row_id=resolved.row_id,
                **{k: int(v) for k, v in scaled.items()},
            )
    return None


class FlatRates:
    """A ``Pricer`` with one flat rate card for every model: $1 input / $5 output per MTok, reads
    ×0.1,
    5m writes ×1.25, 1h writes ×2, other-TTL writes ×1.25, web search $0.01 per request, minimum
    cacheable 1,024 tokens. Exact unit rates at scale 8. Basis LIST; the ``subscription`` billing
    path
    prices on LIST_EQUIVALENT (D26). Implements the per-line exactness table of SPEC §6.3.
    """

    rate_card_sha256 = "flat"
    basis = Basis.LIST

    INPUT = Decimal("1")
    OUTPUT = Decimal("5")
    READ_MULT = Decimal("0.1")
    WRITE_5M_MULT = Decimal("1.25")
    WRITE_1H_MULT = Decimal("2")
    WRITE_OTHER_MULT = Decimal("1.25")
    WEB_SEARCH_USD = Decimal("0.01")
    MIN_CACHEABLE = 1024
    SUPPORTS = frozenset({"batch", "keepalive", "1m_context"})
    TOKENIZER = "claude-4.7+"

    def resolve(self, ctx: PricingContext, *, ts_ms: int) -> ResolvedRates | None:
        """Flat rates for any priceable model (None for an empty model id)."""
        if not ctx.model:
            return None
        inp = self.INPUT
        return ResolvedRates(
            row_id="flat",
            channel=ctx.channel,
            model=ctx.model,
            input=inp,
            output=self.OUTPUT,
            cache_read=EXACT_CTX.multiply(inp, self.READ_MULT),
            cache_write_5m=EXACT_CTX.multiply(inp, self.WRITE_5M_MULT),
            cache_write_1h=EXACT_CTX.multiply(inp, self.WRITE_1H_MULT),
            cache_write_other=EXACT_CTX.multiply(inp, self.WRITE_OTHER_MULT),
            per_request=(("web_search", self.WEB_SEARCH_USD),),
            modifier_ids=(),
            stacking_assumed=False,
            layer="builtin",
            min_cacheable_tokens=self.MIN_CACHEABLE,
            tokenizer_family=self.TOKENIZER,
            long_context_band=False,
        )

    def unit_rates(self, ctx: PricingContext, *, ts_ms: int) -> UnitRates | None:
        """Exact unit rates (scale 8: the smallest s ≥ 6 making $0.10 and $1.25 per MTok
        integral)."""
        resolved = self.resolve(ctx, ts_ms=ts_ms)
        if resolved is None:
            return None
        return unit_rates_from(resolved, web_search_usd=self.WEB_SEARCH_USD)

    def min_cacheable_tokens(self, ctx: PricingContext, *, ts_ms: int) -> int | None:
        return self.MIN_CACHEABLE if ctx.model else None

    def supports(self, ctx: PricingContext, feature: str, *, ts_ms: int) -> bool:
        return bool(ctx.model) and feature in self.SUPPORTS

    def tokenizer_family(self, ctx: PricingContext, *, ts_ms: int) -> str | None:
        return self.TOKENIZER if ctx.model else None

    def price_inference(self, inf: Inference, *, ts_ms: int) -> PricedInference:
        """Price one inference (see :meth:`price_usage`)."""
        priced = self.price_usage(
            inf.usage,
            inf.pricing,
            ts_ms=ts_ms,
            billable=inf.billable,
            usage_source=inf.usage_source,
            output_upper=inf.output_upper,
        )
        return PricedInference(
            inference_id=inf.inference_id,
            lines=priced.lines,
            figure=priced.figure,
            exact_nano=priced.exact_nano,
            estimated=priced.estimated,
            unpriced_reason=priced.unpriced_reason,
        )

    def price_usage(
        self,
        usage: UsageBuckets,
        ctx: PricingContext,
        *,
        ts_ms: int,
        billable: bool | None = True,
        usage_source: UsageSource = UsageSource.FINAL,
        output_upper: int | None = None,
    ) -> PricedInference:
        """One PricedLine per non-zero bucket, each rounded once; exactness per line (SPEC §6.3,
        R9)."""
        basis = Basis.LIST_EQUIVALENT if ctx.billing_path == "subscription" else self.basis
        rates = self.resolve(ctx, ts_ms=ts_ms)
        if rates is None:
            return PricedInference(
                inference_id=None,
                lines=(),
                figure=unpriced("not priceable", basis),
                exact_nano=0,
                estimated=None,
                unpriced_reason="not priceable",
            )
        source = UsageSource(usage_source)
        notes: list[str] = []
        lines: list[PricedLine] = []

        def line(
            bucket: str,
            qty: int,
            rate: Decimal,
            *,
            per_request: bool = False,
            low_rate: Decimal | None = None,
            high_rate: Decimal | None = None,
            low_qty: int | None = None,
            high_qty: int | None = None,
        ) -> None:
            def amount(q: int, r: Decimal) -> int:
                if per_request:
                    return decimal_to_nano(EXACT_CTX.multiply(Decimal(q), r))
                return token_nano(q, r)

            point = amount(qty, rate)
            low = high = None
            if low_rate is not None or low_qty is not None:
                low = amount(low_qty if low_qty is not None else qty, low_rate or rate)
                high = amount(high_qty if high_qty is not None else qty, high_rate or rate)
            if billable is False:
                point, low, high = 0, None, None
            elif billable is None or source is UsageSource.PARTIAL_STREAM:
                low, high = 0, max(point, high if high is not None else point)
            elif source is UsageSource.ESTIMATED and low is None:
                low, high = point, point
            lines.append(
                PricedLine(
                    bucket=bucket,
                    quantity=qty,
                    unit_usd_per_mtok=_dec_str(rate),
                    amount_nano=point,
                    low_nano=low,
                    high_nano=high,
                    exact=low is None,
                    rate_row_id=rates.row_id,
                    modifier_ids=(),
                    layer=rates.layer,
                )
            )

        hint = {"5m": rates.cache_write_5m, "1h": rates.cache_write_1h}.get(
            ctx.write_ttl_hint or ""
        )
        for bucket, qty, rate in (
            ("uncached_input", usage.uncached_input, rates.input),
            ("cache_read", usage.cache_read, rates.cache_read),
            ("cache_write_5m", usage.cache_write_5m, rates.cache_write_5m),
            ("cache_write_1h", usage.cache_write_1h, rates.cache_write_1h),
            ("cache_write_other", usage.cache_write_other, rates.cache_write_other),
        ):
            if qty:
                line(bucket, qty, rate or Decimal(0))
        if usage.cache_write_unknown:
            notes.append("unknown-TTL cache writes priced [5m, 1h]")
            line(
                "cache_write_unknown",
                usage.cache_write_unknown,
                hint or rates.cache_write_5m or Decimal(0),
                low_rate=rates.cache_write_5m,
                high_rate=rates.cache_write_1h,
            )
        if usage.output:
            if source is UsageSource.MESSAGE_START_ONLY:
                notes.append("placeholder output priced [logged, upper]")
                upper = max(
                    usage.output, output_upper if output_upper is not None else usage.output
                )
                line(
                    "output",
                    usage.output,
                    rates.output,
                    low_qty=usage.output,
                    high_qty=upper,
                    low_rate=rates.output,
                    high_rate=rates.output,
                )
            else:
                line("output", usage.output, rates.output)
        elif source is UsageSource.MESSAGE_START_ONLY and output_upper:
            notes.append("placeholder output priced [logged, upper]")
            line(
                "output",
                0,
                rates.output,
                low_qty=0,
                high_qty=output_upper,
                low_rate=rates.output,
                high_rate=rates.output,
            )
        if usage.web_search_requests:
            per_request = dict(rates.per_request).get("web_search", Decimal(0))
            line("web_search", usage.web_search_requests, per_request, per_request=True)
        if billable is None:
            notes.append("billing uncertain: [0, full]")
        elif source is UsageSource.ESTIMATED:
            notes.append("reconstructed usage")
        elif source is UsageSource.PARTIAL_STREAM:
            notes.append("partial stream: billing unknown [0, full]")

        exact_nano = sum(ln.amount_nano for ln in lines if ln.exact)
        range_lines = [ln for ln in lines if not ln.exact]
        provenance = (rates.row_id,)
        note = "; ".join(dict.fromkeys(notes))
        if not range_lines:
            figure = Figure(
                nano=exact_nano, evidence=Evidence.EXACT, basis=basis, provenance=provenance
            )
            return PricedInference(
                inference_id=None,
                lines=tuple(lines),
                figure=figure,
                exact_nano=exact_nano,
                estimated=None,
                unpriced_reason=None,
            )
        est_point = sum(ln.amount_nano for ln in range_lines)
        est_low = sum(ln.low_nano or 0 for ln in range_lines)
        est_high = sum(ln.high_nano or 0 for ln in range_lines)
        estimated = Figure(
            nano=est_point,
            evidence=Evidence.ESTIMATED,
            basis=basis,
            low_nano=est_low,
            high_nano=est_high,
            calibration=Calibration.NA,
            provenance=provenance,
            note=note or "range lines",
        )
        figure = Figure(
            nano=exact_nano + est_point,
            evidence=Evidence.ESTIMATED,
            basis=basis,
            low_nano=exact_nano + est_low,
            high_nano=exact_nano + est_high,
            calibration=Calibration.NA,
            provenance=provenance,
            note=note or "range lines",
        )
        return PricedInference(
            inference_id=None,
            lines=tuple(lines),
            figure=figure,
            exact_nano=exact_nano,
            estimated=estimated,
            unpriced_reason=None,
        )

"""Hypothesis strategies producing valid instances of every SPEC §3.2 record (F-CORE test helpers).

Imported only by tests in this area (SPEC §21 #4).
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import strategies as st

from tokenbill.core import records as r
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, Finality
from tokenbill.core.types import (
    DataQualityNote,
    EvidenceItem,
    Finding,
    Fix,
    Policy,
    RateRow,
    Scope,
    SourceCitation,
)

MAX = r.MAX_TOKENS
# small ints keep examples readable; edges included explicitly
counts = st.one_of(st.integers(0, 10_000), st.sampled_from([0, 1, MAX - 1, MAX]))
small = st.integers(0, 5000)
ms = st.integers(0, 4_102_444_800_000)
text = st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=12)
ident = st.text("abcdefghijklmnopqrstuvwxyz0123456789_-.", min_size=1, max_size=16)
opt_text = st.none() | text
hex20 = st.text("0123456789abcdef", min_size=20, max_size=20)
dates = st.dates().map(lambda d: d.isoformat())


@st.composite
def usage_buckets(draw: st.DrawFn) -> r.UsageBuckets:
    output = draw(counts)
    reasoning = draw(st.none() | st.integers(0, output))
    other = draw(small)
    ttl = draw(st.sampled_from([300, 1800, 3600])) if other else draw(st.none() | st.just(1800))
    return r.UsageBuckets(
        uncached_input=draw(counts),
        cache_read=draw(counts),
        cache_write_5m=draw(small),
        cache_write_1h=draw(small),
        cache_write_other=other,
        cache_write_other_ttl_s=ttl,
        cache_write_unknown=draw(small),
        output=output,
        output_reasoning=reasoning,
        web_search_requests=draw(st.integers(0, 20)),
        web_fetch_requests=draw(st.integers(0, 20)),
    )


pricing_contexts = st.builds(
    r.PricingContext,
    provider=st.sampled_from(["anthropic", "openai"]),
    channel=st.sampled_from(["anthropic_api", "bedrock", "vertex", "openai_api", "unknown"]),
    model=st.sampled_from(["claude-opus-5-5", "claude-sonnet-5", "gpt-5.6-sol", ""]),
    model_raw=text,
    service_tier=st.sampled_from(["standard", "batch", "fast"]),
    speed=st.sampled_from(["standard", "fast"]),
    inference_geo=st.sampled_from([None, "us", "global"]),
    endpoint_scope=st.sampled_from(["global", "regional", "multi_region", "unknown"]),
    write_ttl_hint=st.sampled_from([None, "5m", "1h"]),
    billing_path=st.sampled_from(r.BILLING_PATHS),
)


@st.composite
def inferences(draw: st.DrawFn) -> r.Inference:
    usage = draw(usage_buckets())
    source = draw(st.sampled_from(list(r.UsageSource)))
    upper = None
    if source is r.UsageSource.MESSAGE_START_ONLY:
        upper = draw(st.none() | st.integers(usage.output, MAX))
    return r.Inference(
        inference_id=draw(ident),
        kind=draw(st.sampled_from(list(r.InferenceKind))),
        usage=usage,
        pricing=draw(pricing_contexts),
        usage_source=source,
        billable=draw(st.sampled_from([True, False, None])),
        billing_rule_id=draw(st.none() | ident),
        output_upper=upper,
        provider_reported_cost_nano=draw(st.none() | st.integers(-(10**12), 10**12)),
        provider_reported_cost_basis=draw(st.sampled_from([None, "provider_estimate", "list"])),
    )


cache_diagnostics = st.builds(
    r.CacheDiagnostic,
    reason=st.sampled_from(sorted(r.DIAG_REASONS)),
    provider_reason=text,
    missed_input_tokens_estimate=st.none() | counts,
    source=st.sampled_from(["anthropic.cache_diagnostics", "openai.prompt_cache_diagnostics"]),
)


@st.composite
def attempts(draw: st.DrawFn) -> r.Attempt:
    return r.Attempt(
        attempt_id=draw(ident),
        attempt_no=draw(st.integers(0, 5)),
        ts_start_ms=draw(ms),
        ttft_ms=draw(st.none() | small),
        duration_ms=draw(st.none() | small),
        outcome=draw(st.sampled_from(list(r.Outcome))),
        http_status=draw(st.sampled_from([None, 200, 529])),
        error_type=draw(st.none() | ident),
        retry_layer=draw(st.sampled_from([None, "sdk", "agent"])),
        retry_after_ms=draw(st.none() | small),
        should_retry=draw(st.sampled_from([None, True, False])),
        provider_request_id=draw(st.none() | ident),
        provider_message_id=draw(st.none() | ident),
        model_served=draw(st.none() | ident),
        stop_reason=draw(st.none() | ident),
        inferences=tuple(draw(st.lists(inferences(), max_size=3))),
        diagnostics=draw(st.none() | cache_diagnostics),
        applied_edits=tuple(draw(st.lists(st.tuples(ident, small), max_size=2))),
        thinking_dropped=draw(st.integers(0, 3)),
        sdk_retry_count=draw(st.none() | st.integers(0, 3)),
        raw_usage_json=draw(st.none() | st.just('{"input_tokens":1}')),
        convention_id=draw(st.sampled_from([None, "anthropic.messages"])),
    )


breakpoints = st.builds(
    r.Breakpoint, block_index=small, ttl=st.sampled_from(["5m", "1h", "30m"]), assumed=st.booleans()
)

request_params = st.builds(
    r.RequestParams,
    model_requested=ident,
    max_tokens=st.none() | small,
    stream=st.sampled_from([None, True, False]),
    thinking=st.sampled_from([None, "off", "adaptive"]),
    effort=st.sampled_from([None, "low", "max"]),
    session_effort=st.none() | ident,
    tool_choice=st.sampled_from([None, "auto"]),
    output_format=st.none() | ident,
    speed=st.none() | ident,
    service_tier_requested=st.none() | ident,
    inference_geo_requested=st.none() | ident,
    betas=st.lists(ident, max_size=3).map(lambda b: tuple(sorted(b))),
    breakpoints=st.lists(breakpoints, max_size=3).map(tuple),
    automatic_caching=st.sampled_from([None, True]),
    context_management=st.none() | ident,
    task_budget=st.none() | small,
    web_search_enabled=st.sampled_from([None, False]),
    citations_enabled=st.sampled_from([None, True]),
    has_images=st.sampled_from([None, True]),
    advisor_model=st.none() | ident,
    disable_parallel_tool_use=st.sampled_from([None, True]),
)

block_refs = st.builds(
    r.BlockRef,
    h=hex20,
    h_sorted=st.none() | hex20,
    h_norm=st.none() | hex20,
    tier=st.sampled_from(["tools", "system", "messages"]),
    kind=st.sampled_from(sorted(r.BLOCK_KINDS)),
    role=st.sampled_from([None, "user", "assistant"]),
    n_bytes=small,
    est_tokens=st.none() | small,
    image_px=st.none() | st.tuples(small, small),
    volatile_classes=st.lists(ident, max_size=2).map(tuple),
    lookback_pos=small,
    deferred=st.booleans(),
)


@st.composite
def fingerprints(draw: st.DrawFn) -> r.ContentFingerprint:
    blocks = tuple(draw(st.lists(block_refs, max_size=4)))
    ends = sorted(draw(st.lists(st.integers(0, len(blocks)), min_size=3, max_size=3)))
    return r.ContentFingerprint(
        key_id=draw(ident), blocks=blocks, tier_end=(ends[0], ends[1], ends[2])
    )


appended_items = st.builds(
    r.AppendedItem,
    kind=st.sampled_from(["tool_result", "user_text", "attachment", "image", "assistant"]),
    name=st.none() | ident,
    n_bytes=small,
    is_error=st.booleans(),
    images=st.integers(0, 3),
)

principals = st.one_of(
    st.none(),
    hex20.map(lambda h: "p_" + h),
    hex20.map(lambda h: "c_" + h),
    ident.map(lambda s: "r_" + s[:60]),
)
hashes = st.none() | hex20.map(lambda h: "h_" + h)


@st.composite
def attributions(draw: st.DrawFn) -> r.Attribution:
    keys = draw(st.lists(st.sampled_from(r.EXTRA_KEYS), unique=True, max_size=3))
    extra = tuple(sorted((k, draw(text)) for k in keys))
    return r.Attribution(
        principal=draw(principals),
        team=draw(opt_text),
        cost_center=draw(opt_text),
        project=draw(opt_text),
        repo=draw(opt_text),
        workspace_id=draw(opt_text),
        api_key_id=draw(hashes),
        agent_product=draw(st.sampled_from([None, "claude_code", "agent_sdk"])),
        agent_type=draw(opt_text),
        query_source=draw(opt_text),
        skill=draw(opt_text),
        mcp_server=draw(opt_text),
        plugin=draw(opt_text),
        workload_class=draw(st.sampled_from(list(r.WorkloadClass))),
        entrypoint=draw(opt_text),
        client_version=draw(opt_text),
        billing_path=draw(st.sampled_from([None, *r.BILLING_PATHS])),
        cwd_key=draw(hashes),
        arm=draw(opt_text),
        wave=draw(opt_text),
        extra=extra,
    )


source_refs = st.builds(
    r.SourceRef,
    adapter=ident,
    source_id=ident,
    locator=ident,
    fidelity=st.sampled_from(list(r.Fidelity)),
    priority=st.integers(0, 50),
)


@st.composite
def requests(draw: st.DrawFn, lane_key: str | None = None) -> r.Request:
    return r.Request(
        request_id=draw(ident),
        session_key=draw(ident),
        lane_key=lane_key or draw(ident),
        seq=draw(st.integers(0, 50)),
        attribution=draw(attributions()),
        params=draw(request_params),
        attempts=tuple(draw(st.lists(attempts(), min_size=1, max_size=2))),
        fingerprint=draw(st.none() | fingerprints()),
        appended=tuple(draw(st.lists(appended_items, max_size=2))),
        source=draw(st.none() | source_refs),
    )


_ATTR_VALUE = {int: st.integers(0, 10**6), str: ident, bool: st.booleans(), type(None): st.none()}


@st.composite
def lane_events(draw: st.DrawFn, lane_key: str | None = None) -> r.LaneEvent:
    kind = draw(st.sampled_from(list(r.LaneEventKind)))
    schema = r.EVENT_ATTRS[kind]
    keys = draw(st.lists(st.sampled_from(sorted(schema)), unique=True)) if schema else []
    attrs = []
    for key in sorted(keys):
        if key == "trigger":
            domain = (
                ["auto", "manual"] if kind is r.LaneEventKind.COMPACTION else ["refusal", "unknown"]
            )
            attrs.append((key, draw(st.sampled_from(domain))))
        else:
            attrs.append((key, draw(st.one_of(*(_ATTR_VALUE[t] for t in schema[key])))))
    return r.LaneEvent(
        lane_key=lane_key or draw(ident), ts_ms=draw(ms), kind=kind, attrs=tuple(attrs)
    )


@st.composite
def lanes(draw: st.DrawFn, session_key: str | None = None) -> r.Lane:
    key = draw(ident)
    return r.Lane(
        lane_key=key,
        session_key=session_key or draw(ident),
        kind=draw(st.sampled_from(list(r.LaneKind))),
        parent_lane_key=draw(st.none() | ident),
        cache_scope_key=draw(st.sampled_from(["ws:a", "unknown"])),
        requests=tuple(draw(st.lists(requests(lane_key=key), max_size=3))),
        events=tuple(draw(st.lists(lane_events(lane_key=key), max_size=3))),
        ttl_observed=draw(st.sampled_from(["5m", "1h", "mixed", "unknown"])),
        lane_exact=draw(st.booleans()),
    )


@st.composite
def sessions(draw: st.DrawFn) -> r.Session:
    key = draw(ident)
    start = draw(ms)
    return r.Session(
        session_key=key,
        source_kind=draw(ident),
        attribution=draw(attributions()),
        lanes=tuple(draw(st.lists(lanes(session_key=key), max_size=2))),
        started_ms=start,
        ended_ms=start + draw(small),
    )


@st.composite
def usage_aggregates(draw: st.DrawFn) -> r.UsageAggregate:
    start = draw(ms)
    dims = draw(st.dictionaries(ident, ident, max_size=3))
    return r.UsageAggregate(
        agg_id=draw(ident),
        source_kind=draw(ident),
        bucket_start_ms=start,
        bucket_end_ms=start + draw(small),
        dims=tuple(sorted(dims.items())),
        usage=draw(usage_buckets()),
        reported_cost_nano=draw(st.none() | st.integers(-(10**12), 10**12)),
        reported_cost_basis=draw(st.sampled_from([None, "invoice", "provider_estimate"])),
        list_cost_nano=draw(st.none() | st.integers(0, 10**12)),
        finality=draw(st.sampled_from(["provisional", "final"])),
        fetched_ms=draw(ms),
    )


cost_lines = st.builds(
    r.CostLine,
    line_id=ident,
    source_kind=ident,
    date_utc=dates,
    channel=ident,
    workspace_id=st.none() | ident,
    description=text,
    model=st.none() | ident,
    cost_type=st.none() | ident,
    token_type=st.none() | ident,
    sku=st.none() | ident,
    service_tier=st.none() | ident,
    inference_geo=st.none() | ident,
    endpoint_scope=st.sampled_from([None, "global", "regional"]),
    amount_nano=st.integers(-(10**15), 10**15),
    list_amount_nano=st.none() | st.integers(0, 10**15),
    currency=st.just("USD"),
    finality=st.sampled_from(["provisional", "final"]),
    principal=st.none() | hex20.map(lambda h: "p_" + h),
    fetched_ms=ms,
)

outcome_aggregates = st.builds(
    r.OutcomeAggregate,
    date_utc=dates,
    team=ident,
    n_users=small,
    sessions=small,
    commits=small,
    pull_requests=small,
    lines_added=small,
    lines_removed=small,
    edits_accepted=small,
    edits_rejected=small,
)

usage_records = st.builds(
    r.UsageRecord,
    inference_id=ident,
    request_id=ident,
    attempt_id=ident,
    session_key=ident,
    lane_key=ident,
    lane_kind=st.sampled_from(list(r.LaneKind)),
    ts_ms=ms,
    date_utc=dates,
    kind=st.sampled_from(list(r.InferenceKind)),
    usage_source=st.sampled_from(list(r.UsageSource)),
    billable=st.sampled_from([True, False, None]),
    billing_rule_id=st.none() | ident,
    pricing=pricing_contexts,
    usage=usage_buckets(),
    attribution=attributions(),
    fidelity=st.sampled_from(list(r.Fidelity)),
)


@st.composite
def figures(draw: st.DrawFn, basis: Basis | None = None) -> Figure:
    b = basis or draw(st.sampled_from([Basis.LIST, Basis.CONTRACT, Basis.LIST_EQUIVALENT]))
    ev = draw(st.sampled_from(list(Evidence)))
    nano = draw(st.integers(-(10**12), 10**12))
    if ev is Evidence.EXACT:
        return Figure(
            nano=nano, evidence=ev, basis=b, provenance=tuple(draw(st.lists(ident, max_size=2)))
        )
    lo = nano - draw(st.integers(0, 10**9))
    hi = nano + draw(st.integers(0, 10**9))
    if ev is Evidence.ESTIMATED:
        has_range = draw(st.booleans())
        return Figure(
            nano=nano,
            evidence=ev,
            basis=b,
            low_nano=lo if has_range else None,
            high_nano=hi if has_range else None,
            calibration=draw(st.sampled_from([Calibration.CALIBRATED, Calibration.UNCALIBRATED])),
            note=draw(st.sampled_from(["", "assumption"])),
        )
    return Figure(
        nano=nano,
        evidence=ev,
        basis=b,
        low_nano=lo,
        high_nano=hi,
        ci_level_pct=95,
        calibration=Calibration.CALIBRATED,
        finality=draw(st.sampled_from(list(Finality))),
    )


RECORD_STRATEGIES = {
    r.UsageBuckets: usage_buckets(),
    r.PricingContext: pricing_contexts,
    r.Inference: inferences(),
    r.CacheDiagnostic: cache_diagnostics,
    r.Attempt: attempts(),
    r.Breakpoint: breakpoints,
    r.RequestParams: request_params,
    r.BlockRef: block_refs,
    r.ContentFingerprint: fingerprints(),
    r.AppendedItem: appended_items,
    r.Attribution: attributions(),
    r.SourceRef: source_refs,
    r.Request: requests(),
    r.LaneEvent: lane_events(),
    r.Lane: lanes(),
    r.Session: sessions(),
    r.UsageAggregate: usage_aggregates(),
    r.CostLine: cost_lines,
    r.OutcomeAggregate: outcome_aggregates,
    r.UsageRecord: usage_records,
}

findings = st.builds(
    Finding,
    finding_id=ident,
    detector_id=ident,
    kind=ident,
    detector_version=ident,
    category=st.just("breaker"),
    lever_class=st.just("none"),
    audience=st.just("org"),
    title=text,
    summary=text,
    scope=st.builds(Scope, dims=st.just((("team", "a"),))),
    n_events=small,
    n_lanes=small,
    n_users=small,
    first_seen_ms=ms,
    cost_observed=figures(Basis.LIST),
    recoverable=st.none() | figures(Basis.LIST),
    evidence=st.lists(
        st.builds(
            EvidenceItem, kind=ident, ref=ident, attrs=st.just((("gap_ms", 5), ("model", "m")))
        ),
        max_size=2,
    ).map(tuple),
    fix=st.none()
    | st.builds(
        Fix,
        text=text,
        config_patch=st.none() | st.just((("promptCacheTtl", '"1h"'),)),
        target=st.none() | ident,
        doc_url=st.none() | ident,
    ),
    references=st.lists(ident, min_size=1, max_size=2).map(tuple),
)

rate_rows = st.builds(
    RateRow,
    row_id=ident,
    provider=ident,
    channel=ident,
    model=ident,
    aliases=st.just(()),
    generation=st.just("5.5"),
    effective_from=dates,
    effective_to=st.none() | dates,
    input_usd_per_mtok=st.decimals("0", "100", places=4),
    output_usd_per_mtok=st.decimals("0", "100", places=4),
    cache_read_mult=st.none() | st.just(Decimal("0.1")),
    cache_write_5m_mult=st.just(Decimal("1.25")),
    cache_write_1h_mult=st.just(Decimal("2")),
    cache_write_other_mult=st.none(),
    cache_write_other_ttl_s=st.none(),
    published_absolute=st.just((("cache_read", Decimal("0.20")),)),
    min_cacheable_tokens=st.none() | small,
    tokenizer_family=ident,
    per_request_usd=st.just((("web_search", Decimal("0.01")),)),
    long_context_threshold=st.none() | small,
    long_context_usd_per_mtok=st.just(()),
    supports=st.just(("batch",)),
    enabled=st.booleans(),
    verified_on=dates,
    sources=st.just((SourceCitation(url="u", retrieved="2026-09-23", finding=None),)),
)

policies = st.builds(
    Policy,
    name=ident,
    ttl=st.lists(st.tuples(ident, st.sampled_from(["5m", "1h"])), max_size=2).map(tuple),
    keepalive=st.none() | st.tuples(ident, st.just(240), st.just(3600)),
    compaction_window=st.none() | st.tuples(small, st.none() | small),
    fast_off=st.booleans(),
)

dq_notes = st.builds(
    DataQualityNote,
    code=ident,
    severity=st.sampled_from(["info", "warn"]),
    count=small,
    detail=text,
    tokens=st.none() | small,
    figure=st.none() | figures(Basis.LIST),
)

TYPE_STRATEGIES = {
    Figure: figures(),
    Finding: findings,
    RateRow: rate_rows,
    Policy: policies,
    DataQualityNote: dq_notes,
}

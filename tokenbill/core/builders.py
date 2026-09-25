"""Test and fixture builders, the content canary, and ``FlatRates`` (SPEC §3.25).

Every package builds its fixtures with these helpers instead of sibling packages (SPEC §21 #4).
Builders are deterministic: default ids derive from their arguments with ``core.ids.stable_id``.

GitHub Copilot builders (CORE-AMENDMENTS C-29): ``make_copilot_ctx``, ``make_license``,
``make_activity``, ``make_config``, ``make_ai_usage_row``, ``make_seat_line``,
``make_actions_line``, ``make_pool_month``, ``make_plan_evidence``, the login canary
``CANARY_LOGIN`` and ``make_principal``; ``FlatRates`` prices both Copilot billing paths on
LIST_EQUIVALENT like ``subscription``.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from tokenbill.core.ids import natural_id, stable_id
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, unpriced
from tokenbill.core.lanes import _ttl_observed
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.money import (
    EXACT_CTX,
    NANO_USD_PER_CREDIT,
    credits_str_to_nano,
    decimal_to_nano,
    token_nano,
    usd,
)
from tokenbill.core.records import (
    ActivityDay,
    AppendedItem,
    Attempt,
    Attribution,
    BlockRef,
    CacheDiagnostic,
    ConfigSnapshot,
    ContentFingerprint,
    CostLine,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneKind,
    LicenseSnapshot,
    Outcome,
    PricingContext,
    Request,
    RequestParams,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    UsageSource,
    billing_class,
)
from tokenbill.core.types import (
    PlanEvidence,
    PoolMonth,
    PricedInference,
    PricedLine,
    ResolvedRates,
    UnitRates,
)

__all__ = [
    "CANARY",
    "CANARY_EMAIL",
    "CANARY_KEYS",
    "CANARY_LOGIN",
    "STRUCTURAL_KEYS",
    "FlatRates",
    "assert_no_canary",
    "lane_from_table",
    "make_activity",
    "make_actions_line",
    "make_aggregate",
    "make_ai_usage_row",
    "make_attempt",
    "make_block",
    "make_config",
    "make_copilot_ctx",
    "make_cost_line",
    "make_ctx",
    "make_fingerprint",
    "make_inference",
    "make_lane",
    "make_license",
    "make_plan_evidence",
    "make_pool_month",
    "make_principal",
    "make_request",
    "make_seat_line",
    "make_usage",
    "plant_canary",
    "unit_rates_from",
]

# ---------------------------------------------------------------------------------------------
# content canary (SPEC §8.8)
# ---------------------------------------------------------------------------------------------

CANARY = "TB-CANARY-7f3a91"
CANARY_EMAIL = f"canary.{CANARY}@example.com"
#: A GitHub login planted in Copilot fixtures (seats, AI usage report, metrics, activity report); it
#: must never appear in any output (logins are mapped to team / cost center and pseudonymized).
CANARY_LOGIN = "tb-canary-login-7f3a91"
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
    }
)
#: Structural keys whose values are never planted, even inside a content subtree.
STRUCTURAL_KEYS = frozenset(
    {
        "type",
        "id",
        "model",
        "role",
        "name",
        "tool_use_id",
        "uuid",
        "parentUuid",
        "sessionId",
        "requestId",
        "timestamp",
        "stop_reason",
        "subtype",
    }
)


def plant_canary(obj: Any, *, keys: Iterable[str] | None = None) -> Any:
    """A deep copy of a JSON-like fixture with :data:`CANARY` appended to every string stored under
    a content key (:data:`CANARY_KEYS`, or *keys*), at any depth below it. A bare string gets the
    canary appended. Structural keys (:data:`STRUCTURAL_KEYS`: ``type``, ids, model names) are never
    planted, so the fixture still parses."""
    wanted = frozenset(keys) if keys is not None else CANARY_KEYS

    def walk(value: Any, planting: bool) -> Any:
        if isinstance(value, str):
            return f"{value} {CANARY}" if planting else value
        if isinstance(value, Mapping):
            return {
                k: copy.deepcopy(v) if k in STRUCTURAL_KEYS else walk(v, planting or k in wanted)
                for k, v in value.items()
            }
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
    channel to the provider's first-party channel, ``model_raw`` to *model*."""
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
    ``applied_edits``, ``thinking_dropped``, …) pass through *kw*."""
    aid = attempt_id or stable_id("at", ts_ms, attempt_no, *(i.inference_id for i in inferences))
    return Attempt(
        attempt_id=aid,
        attempt_no=attempt_no,
        ts_start_ms=ts_ms,
        ttft_ms=ttft_ms,
        duration_ms=duration_ms,
        outcome=outcome,
        http_status=http_status,
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
    :func:`make_ctx`; a ``billing_path`` is mirrored into the attribution. *extra_inferences*
    precede the serving inference (e.g. a compaction iteration); *attempts* replaces the generated
    attempt entirely.
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
    """A lane from rows of ``(ts_s, R, W5, W1, U, O)``: seconds since 0, cache reads, 5m writes,
    1h writes, uncached input, output. ``request_kw`` go to :func:`make_request` for every row."""
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
# GitHub Copilot builders (C-29)
# ---------------------------------------------------------------------------------------------

_DAY_MS = 86_400_000
_USD_PER_CREDIT = Decimal(NANO_USD_PER_CREDIT) / Decimal(10**9)   # 0.01
_SEAT_SKUS = {"business": "copilot_for_business", "enterprise": "copilot_enterprise"}
_SEAT_USD = {"business": Decimal("19"), "enterprise": Decimal("39")}
_INCLUDED_CREDITS = {"business": Decimal("1900"), "enterprise": Decimal("3900")}
_CONFIG_SOURCES = {
    "budget": "github.budgets",
    "budget_users": "github.budgets",
    "cost_center": "github.cost_centers",
    "org_settings": "github.org_copilot_settings",
    "run_flags": "tokenbill.cli",
    "seat_counts": "tokenbill.copilot_export",
    "activity_counts": "tokenbill.copilot_export",
    "plan_quota": "github.ai_usage_report",
}


def make_principal(seed: str | int = 0) -> str:
    """A deterministic ``p_<20 hex>`` fixture pseudonym (``sha256`` of *seed*, not a key HMAC)."""
    return "p_" + hashlib.sha256(f"tokenbill-fixture-principal:{seed}".encode()).hexdigest()[:20]


def _day_start_ms(date_utc: str) -> int:
    d = _dt.date.fromisoformat(date_utc)
    return (d - _dt.date(1970, 1, 1)).days * _DAY_MS


def make_copilot_ctx(model: str = "claude-opus-5-5", **kw: Any) -> PricingContext:
    """A PricingContext on GitHub Copilot: provider ``github``, channel ``github_copilot``,
    billing path ``copilot_pool`` (each overridable through *kw*, like every other field)."""
    kw.setdefault("provider", "github")
    kw.setdefault("channel", "github_copilot")
    kw.setdefault("billing_path", "copilot_pool")
    return make_ctx(model, **kw)


def make_license(
    principal: str | None = None,
    *,
    snapshot_date: str = "2026-09-01",
    plan: str = "business",
    team: str | None = None,
    cost_center: str | None = None,
    org: str | None = "org-a",
    seat_created: str | None = None,
    pending_cancellation: str | None = None,
    last_activity_bucket: str = "0-7",
    last_activity_surface: str | None = "vscode",
    last_authenticated_bucket: str = "0-7",
    assigned_via_team: bool | None = False,
    fetched_ms: int = 0,
    source_kind: str = "github.copilot_seats",
    product: str = "github_copilot",
) -> LicenseSnapshot:
    """A Copilot seat snapshot (seats API by default; ``source_kind=
    "github.copilot_activity_report"`` with ``plan="unknown"`` and ``assigned_via_team=None`` for
    the UI activity report). *principal* defaults to ``make_principal(0)``."""
    return LicenseSnapshot(
        snapshot_date=snapshot_date,
        product=product,
        plan=plan,
        principal=principal if principal is not None else make_principal(0),
        team=team,
        cost_center=cost_center,
        org=org,
        seat_created=seat_created,
        pending_cancellation=pending_cancellation,
        last_activity_bucket=last_activity_bucket,
        last_activity_surface=last_activity_surface,
        last_authenticated_bucket=last_authenticated_bucket,
        assigned_via_team=assigned_via_team,
        fetched_ms=fetched_ms,
        source_kind=source_kind,
    )


def make_activity(
    principal: str | None = None,
    *,
    date_utc: str = "2026-09-01",
    team: str | None = None,
    cost_center: str | None = None,
    reported_cost_nano: int | None = None,
    counts: Mapping[str, int] | None = None,
    flags: Iterable[str] = (),
    fetched_ms: int = 0,
    product: str = "github_copilot",
    source_kind: str = "github.copilot_metrics",
) -> ActivityDay:
    """One usage-metrics user-day; *counts* default to ``{"interactions": 1}``."""
    c = dict(counts) if counts is not None else {"interactions": 1}
    return ActivityDay(
        date_utc=date_utc,
        product=product,
        principal=principal if principal is not None else make_principal(0),
        team=team,
        cost_center=cost_center,
        reported_cost_nano=reported_cost_nano,
        counts=tuple(sorted(c.items())),
        flags=tuple(sorted(flags)),
        fetched_ms=fetched_ms,
        source_kind=source_kind,
    )


def make_config(
    kind: str = "run_flags",
    attrs: Mapping[str, str | int | bool | None] | None = None,
    *,
    entity_id: str = "run",
    source_kind: str | None = None,
    snapshot_ms: int = 0,
    fetched_ms: int = 0,
) -> ConfigSnapshot:
    """A configuration snapshot; *source_kind* defaults by kind (run flags: ``tokenbill.cli``;
    budgets: ``github.budgets``; count kinds: ``tokenbill.copilot_export``; …)."""
    return ConfigSnapshot(
        snapshot_ms=snapshot_ms,
        source_kind=source_kind if source_kind is not None else _CONFIG_SOURCES.get(
            kind, "tokenbill.cli"),
        kind=kind,
        entity_id=entity_id,
        attrs=tuple(sorted((attrs or {}).items())),
        fetched_ms=fetched_ms,
    )


def _workload(pseudo: str | None, sku: str) -> str | None:
    if pseudo == "code_review":
        return "copilot_code_review"
    if pseudo == "cloud_agent" or sku == "coding_agent_ai_credit":
        return "copilot_cloud_agent"
    if sku == "code_quality_ai_credit":
        return "code_quality"
    return None


def make_ai_usage_row(
    *,
    date_utc: str = "2026-09-10",
    model: str = "Claude Opus 5.5",
    credits: str = "100",
    discount_credits: str = "0",
    principal: str | None = None,
    unattributed: bool = False,
    organization: str | None = "org-a",
    cost_center: str | None = None,
    team: str | None = None,
    repo: str | None = None,
    sku: str = "copilot_ai_credit",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    finality: str = "final",
    fetched_ms: int = 0,
) -> tuple[CostLine, UsageAggregate]:
    """One AI usage report row as the ``github-ai-usage`` adapter maps it (addendum §5.1): a
    ``CostLine`` (gross = *credits* × $0.01, net = gross − *discount_credits* × $0.01, natural
    ``line_id``) and its ``UsageAggregate`` (tokens under the ``excl`` convention: report input →
    uncached, cache writes → unknown TTL). *model* is the report label, normalized with
    ``normalize_copilot_model``; *unattributed* rows have no principal (``ai_credit.direct``)."""
    cm = normalize_copilot_model(model)
    who = None if unattributed else (principal if principal is not None else make_principal(0))
    gross, _ = credits_str_to_nano(credits)
    discount, _ = credits_str_to_nano(discount_credits)
    net = gross - discount
    cost_type = "ai_credit.user" if who is not None else "ai_credit.direct"
    if sku.endswith("_premium_request"):
        cost_type = "ai_credit.legacy_pru"
    line = CostLine(
        line_id=natural_id("cl", "github.ai_usage_report", date_utc, who, cm.model or model, sku,
                           organization, cost_center, repo),
        source_kind="github.ai_usage_report",
        date_utc=date_utc,
        channel="github_copilot",
        workspace_id=organization,
        description=f"{sku} {model}"[:128],
        model=cm.model or None,
        cost_type=cost_type,
        token_type=None,
        sku=sku,
        service_tier=None,
        inference_geo=None,
        endpoint_scope=None,
        amount_nano=net,
        list_amount_nano=gross,
        finality=finality,
        principal=who,
        fetched_ms=fetched_ms,
        quantity=credits,
        unit="ai-credits",
        cost_center=cost_center,
        team=team,
        repo=repo,
        workload=_workload(cm.pseudo, sku),
        routing=cm.routing,
        speed=cm.speed,
        pseudo=cm.pseudo,
    )
    dims = {"channel": "github_copilot", "organization": organization, "team": team,
            "cost_center": cost_center, "model": cm.model or None, "sku": sku,
            "routing": cm.routing, "speed": cm.speed, "pseudo": cm.pseudo}
    pairs = tuple(sorted((k, v) for k, v in dims.items() if v is not None))
    start = _day_start_ms(date_utc)
    agg = UsageAggregate(
        agg_id=natural_id("ag", "github.ai_usage_report", date_utc,
                          *(f"{k}={v}" for k, v in pairs)),
        source_kind="github.ai_usage_report",
        bucket_start_ms=start,
        bucket_end_ms=start + _DAY_MS,
        dims=pairs,
        usage=UsageBuckets(uncached_input=input_tokens, cache_read=cache_read_tokens,
                           cache_write_unknown=cache_write_tokens, output=output_tokens),
        reported_cost_nano=net,
        reported_cost_basis="invoice",
        list_cost_nano=gross,
        finality=finality,
        fetched_ms=fetched_ms,
    )
    return line, agg


def make_seat_line(
    plan: str = "business",
    seats: str = "1",
    *,
    date_utc: str = "2026-09-01",
    organization: str | None = "org-a",
    cost_center: str | None = None,
    team: str | None = None,
    principal: str | None = None,
    sku: str | None = None,
    discount_nano: int = 0,
    finality: str = "final",
    fetched_ms: int = 0,
) -> CostLine:
    """A detailed-usage seat line (cost type ``seat``): *seats* seat-months × the plan's list price
    ($19 Business, $39 Enterprise); the SKU defaults to the plan's seat SKU."""
    s = sku if sku is not None else _SEAT_SKUS[plan]
    gross = decimal_to_nano(EXACT_CTX.multiply(usd(seats), _SEAT_USD[plan]))
    return CostLine(
        line_id=natural_id("cl", "github.metered_usage", date_utc, principal, s, organization,
                           cost_center, "seat"),
        source_kind="github.metered_usage",
        date_utc=date_utc,
        channel="github_copilot",
        workspace_id=organization,
        description=f"{s} seats",
        model=None,
        cost_type="seat",
        token_type=None,
        sku=s,
        service_tier=None,
        inference_geo=None,
        endpoint_scope=None,
        amount_nano=gross - discount_nano,
        list_amount_nano=gross,
        finality=finality,
        principal=principal,
        fetched_ms=fetched_ms,
        quantity=seats,
        unit="seat-months",
        cost_center=cost_center,
        team=team,
    )


def make_actions_line(
    minutes: str = "10",
    *,
    sku: str = "actions_linux",
    usd_per_minute: str = "0.006",
    workload: str | None = "copilot_code_review",
    date_utc: str = "2026-09-10",
    organization: str | None = "org-a",
    cost_center: str | None = None,
    team: str | None = None,
    repo: str | None = None,
    workflow: str | None = None,
    discount_nano: int = 0,
    finality: str = "final",
    fetched_ms: int = 0,
) -> CostLine:
    """A detailed-usage Actions line of a Copilot workload (channel ``github_actions``, cost type
    ``actions``): gross = *minutes* × *usd_per_minute*, net = gross − *discount_nano*."""
    gross = decimal_to_nano(EXACT_CTX.multiply(usd(minutes), usd(usd_per_minute)))
    return CostLine(
        line_id=natural_id("cl", "github.metered_usage", date_utc, sku, organization, repo,
                           workflow, workload, cost_center),
        source_kind="github.metered_usage",
        date_utc=date_utc,
        channel="github_actions",
        workspace_id=organization,
        description=f"{sku} minutes",
        model=None,
        cost_type="actions",
        token_type=None,
        sku=sku,
        service_tier=None,
        inference_geo=None,
        endpoint_scope=None,
        amount_nano=gross - discount_nano,
        list_amount_nano=gross,
        finality=finality,
        fetched_ms=fetched_ms,
        quantity=minutes,
        unit="minutes",
        cost_center=cost_center,
        team=team,
        repo=repo,
        workload=workload,
        workflow=workflow,
    )


def make_pool_month(
    *,
    entity_id: str = "enterprise",
    month: str = "2026-09",
    seats: Mapping[str, str] | None = None,
    consumed_report_nano: int = 0,
    **kw: Any,
) -> PoolMonth:
    """A closed, metered ``PoolMonth``; the pool is Σ seats × included credits of the known plans
    (*seats* default ``{"business": "100"}``; unknown seats need a ``plan_scenario``, whose plan
    they are counted under). The regime follows consumption vs pool unless given; any other field
    passes through *kw*."""
    seat_map = dict(seats) if seats is not None else {"business": "100"}
    scenario = kw.get("plan_scenario")
    credits = Decimal(0)
    for plan, n in seat_map.items():
        per_seat = _INCLUDED_CREDITS.get(plan if plan != "unknown" else (scenario or ""))
        if per_seat is None:
            raise ValueError("make_pool_month: unknown seats need a plan_scenario")
        credits = EXACT_CTX.add(credits, EXACT_CTX.multiply(usd(n), per_seat))
    pool_nano = decimal_to_nano(EXACT_CTX.multiply(credits, _USD_PER_CREDIT))
    fields: dict[str, Any] = {
        "billing_mode": "metered",
        "seats_source": "seat_lines",
        "pool_credits": format(credits.normalize(), "f"),
        "pool_nano": pool_nano,
        "promo": None,
        "consumed_report_nano": consumed_report_nano,
        "consumed_estimate_nano": 0,
        "pool_draw_nano": None,
        "discount_other_nano": 0,
        "discount_unclassified_nano": 0,
        "overage_observed_nano": max(0, consumed_report_nano - pool_nano),
        "direct_net_nano": 0,
        "direct_draws_pool": "unknown",
        "capped_policy": None,
        "days_final": 30,
        "days_provisional": 0,
        "days_in_month": 30,
        "finality": "closed",
        "forecast": None,
        "overage_forecast": None,
        "regime": "overage" if consumed_report_nano > pool_nano else "slack",
    }
    fields.update(kw)
    return PoolMonth(entity_id=entity_id, month=month,
                     seats=tuple(sorted(seat_map.items())), **fields)


def make_plan_evidence(
    *,
    entity_id: str = "enterprise",
    month: str = "2026-09",
    plan: str = "unknown",
    source: str = "none",
    seats: Mapping[str, int] | None = None,
    conflict: bool = False,
    evidence: Iterable[str] = (),
) -> PlanEvidence:
    """A ``PlanEvidence``; *seats* default to ``{"unknown": 100}`` (the addendum C.P13 entity)."""
    seat_map = dict(seats) if seats is not None else {"unknown": 100}
    return PlanEvidence(entity_id=entity_id, month=month, plan=plan, source=source,
                        seats=tuple(sorted(seat_map.items())), conflict=conflict,
                        evidence=tuple(evidence))


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
    ×0.1, 5m writes ×1.25, 1h writes ×2, other-TTL writes ×1.25, web search $0.01 per request,
    minimum cacheable 1,024 tokens. Exact unit rates at scale 8. Basis LIST; the ``subscription``
    billing path (D26) and both Copilot billing paths ``copilot_pool`` / ``copilot_direct``
    (billing class ``pool``, addendum DC2) price on LIST_EQUIVALENT. Implements the per-line
    exactness table of SPEC §6.3.
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
        basis = (Basis.LIST_EQUIVALENT if billing_class(ctx.billing_path) in ("allowance", "pool")
                 else self.basis)
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

"""Area-local builders for the RECON tests (imported only by tests in ``tests/v2/recon``).

Provider aggregates and cost lines are built with ``core.builders``; invoice lines are the provider
usage priced by ``FakePricer`` (the ``facts.json`` rates) line by line, optionally scaled, so every
expectation is closed-form arithmetic on the published rates.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from tokenbill.core.builders import make_aggregate, make_cost_line, make_ctx, make_usage
from tokenbill.core.records import (
    Attribution,
    CostLine,
    Fidelity,
    InferenceKind,
    LaneKind,
    UsageAggregate,
    UsageBuckets,
    UsageRecord,
    UsageSource,
)
from tokenbill.core.testing import FakePricer

PRICER = FakePricer()
DAY = "2026-08-10"
DAY2 = "2026-08-11"
TODAY = "2026-09-23"          # DAY and DAY2 are closed (44 / 43 days earlier)
RECENT = "2026-09-10"         # inside the 30-day revision window of TODAY
DAY_MS = 86_400_000
OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
WS = "wrkspc_payments"
WS2 = "wrkspc_search"
#: COST_TYPE_MAP inverse: bucket → (cost_type, token_type) of the Admin cost report.
TOKEN_TYPES = {
    "uncached_input": ("tokens", "uncached_input_tokens"),
    "cache_read": ("tokens", "cache_read_input_tokens"),
    "cache_write_5m": ("tokens", "cache_creation.ephemeral_5m_input_tokens"),
    "cache_write_1h": ("tokens", "cache_creation.ephemeral_1h_input_tokens"),
    "output": ("tokens", "output_tokens"),
    "web_search": ("web_search", None),
}
USAGE = {"uncached_input": 2_000_000, "cache_read": 30_000_000, "cache_write_5m": 1_500_000,
         "cache_write_1h": 100_000, "output": 700_000}


def ms(date: str) -> int:
    """UTC midnight of *date* in ms."""
    return (dt.date.fromisoformat(date).toordinal() - dt.date(1970, 1, 1).toordinal()) * DAY_MS


def agg(usage: Mapping[str, int] | UsageBuckets | None = None, *, date: str = DAY,
        model: str | None = OPUS, ws: str | None = WS, channel: str = "anthropic_api",
        source_kind: str = "anthropic.usage_report", tier: str | None = "standard",
        **dims: str) -> UsageAggregate:
    """A one-day provider usage aggregate."""
    d: dict[str, str] = {"channel": channel}
    for k, v in (("model", model), ("workspace_id", ws), ("service_tier", tier)):
        if v is not None:
            d[k] = v
    d.update(dims)
    u = usage if isinstance(usage, UsageBuckets) else make_usage(**(usage or USAGE))
    return make_aggregate(u, source_kind=source_kind, bucket_start_ms=ms(date),
                          bucket_end_ms=ms(date) + DAY_MS, dims=d)


def ctx_of(a: UsageAggregate) -> Any:
    d = dict(a.dims)
    kw: dict[str, Any] = {"channel": d["channel"],
                          "service_tier": d.get("service_tier") or "standard",
                          "speed": d.get("speed") or "standard"}
    if d.get("inference_geo"):
        kw["inference_geo"] = d["inference_geo"]
    if d.get("endpoint_scope"):
        kw["endpoint_scope"] = d["endpoint_scope"]
    return make_ctx(d.get("model") or "", **kw)


def priced_buckets(a: UsageAggregate, pricer: Any = PRICER) -> dict[str, int]:
    """bucket → nano of *a* priced by *pricer*."""
    priced = pricer.price_usage(a.usage, ctx_of(a), ts_ms=a.bucket_start_ms)
    assert priced.unpriced_reason is None
    out: dict[str, int] = {}
    for line in priced.lines:
        out[line.bucket] = out.get(line.bucket, 0) + line.amount_nano
    return out


def scale(nano: int, factor: Decimal) -> int:
    return int((Decimal(nano) * factor).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def cost_lines(a: UsageAggregate, *, factor: Decimal | Mapping[str, Decimal] = Decimal(1),
               source_kind: str = "anthropic.cost_report", with_list: bool = False
               ) -> list[CostLine]:
    """Admin cost-report lines for aggregate *a*: its usage at list × *factor* (per bucket when a
    mapping), one line per bucket, rounded half-even once."""
    d = dict(a.dims)
    date = dt.date.fromordinal(dt.date(1970, 1, 1).toordinal() + a.bucket_start_ms // DAY_MS)
    out = []
    for bucket, nano in sorted(priced_buckets(a).items()):
        f = factor.get(bucket, Decimal(1)) if isinstance(factor, Mapping) else factor
        cost_type, token_type = TOKEN_TYPES[bucket]
        model = None if bucket == "web_search" else d.get("model")
        out.append(make_cost_line(
            scale(nano, f), date_utc=date.isoformat(), channel=d["channel"], model=model,
            source_kind=source_kind, workspace_id=d.get("workspace_id"), cost_type=cost_type,
            token_type=token_type, service_tier=d.get("service_tier"),
            list_amount_nano=nano if with_list else None,
            line_id=f"cl-{source_kind}-{date}-{d.get('workspace_id')}-{model}-{bucket}"))
    return out


def record(usage: Mapping[str, int] | None = None, *, date: str = DAY, model: str = OPUS,
           ws: str | None = WS, channel: str = "anthropic_api", billing_path: str = "api_key",
           tier: str = "standard", n: int = 0, agent_product: str | None = None,
           team: str | None = None, usage_source: UsageSource = UsageSource.FINAL,
           billable: bool | None = True, **ctx_kw: Any) -> UsageRecord:
    """One ledger record (a billable inference) at noon UTC of *date*."""
    pricing = make_ctx(model, channel=channel, service_tier=tier, billing_path=billing_path,
                       **ctx_kw)
    rid = f"r-{channel}-{date}-{model}-{ws}-{billing_path}-{tier}-{n}"
    return UsageRecord(
        inference_id=f"inf-{rid}", request_id=f"rq-{rid}", attempt_id=f"at-{rid}",
        session_key="s1", lane_key="l1", lane_kind=LaneKind.API_RUN, ts_ms=ms(date) + 43_200_000,
        date_utc=date, kind=InferenceKind.MESSAGE, usage_source=usage_source, billable=billable,
        billing_rule_id=None, pricing=pricing, usage=make_usage(**(usage or USAGE)),
        attribution=Attribution(workspace_id=ws, billing_path=billing_path,
                                agent_product=agent_product, team=team),
        fidelity=Fidelity.FULL)


def split_records(usage: Mapping[str, int], parts: int, **kw: Any) -> list[UsageRecord]:
    """*usage* split into *parts* ledger records (the remainder on the first)."""
    out = []
    for i in range(parts):
        piece = {k: v // parts + (v % parts if i == 0 else 0) for k, v in usage.items()}
        out.append(record(piece, n=i, **kw))
    return out


def verdicts(report: Any) -> dict[str, str]:
    return {c.channel: c.verdict for c in report.channels}


def residuals(report: Any) -> dict[str, int]:
    return dict(report.residuals)


def rows_where(report: Any, **dims: str) -> list[Any]:
    return [r for r in report.rows if all((k, v) in r.key for k, v in dims.items())]


def with_dims(a: UsageAggregate, **dims: str) -> UsageAggregate:
    d = dict(a.dims)
    d.update(dims)
    return dataclasses.replace(a, dims=tuple(sorted(d.items())))

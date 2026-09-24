"""OpenAI organization usage and costs page adapters (SPEC §5.11; package ADMIN).

* :class:`OpenAIUsageBucketsAdapter` — ``GET /v1/organization/usage/completions`` pages (``object:
  "page"`` of ``bucket``\\ s with ``organization.usage.completions.result`` rows) →
  ``UsageAggregate(source_kind="openai.usage")`` per bucket and group (project → ``workspace_id``
  and API key → ``api_key_id``, both ``h_`` under the name key; ``model``; ``service_tier``, with
  ``batch: true`` → ``batch``). Disjoint buckets (convention ``openai.usage_buckets``):
  ``input_uncached_tokens`` → uncached input, ``input_cached_tokens`` → cache read,
  ``input_cache_write_tokens`` → ``cache_write_other`` with the 30-minute TTL (1800 s, the only
  GPT-5.6+ write TTL), ``output_tokens`` → output. Pages without ``input_uncached_tokens`` derive
  it as ``input_tokens − cached − cache_write`` (negative → quarantined with
  ``dq.sum_check_failed``); when every field is present a disagreement with ``input_tokens`` is
  flagged ``dq.sum_check_failed``. ``user_id`` grouping (a person) is dropped and its rows summed.
* :class:`OpenAICostsAdapter` — ``GET /v1/organization/costs`` pages
  (``organization.costs.result``) → ``CostLine(source_kind="openai.costs", channel="openai_api")``
  per day, project and ``line_item`` (the description); ``amount.value`` is USD (a JSON number
  parsed as an exact ``Decimal``, never a float); only ``usd`` is accepted.

Every aggregate and cost line carries channel ``openai_api``. Result schemas follow the OpenAI
OpenAPI description (``UsageCompletionsResult``, ``CostsResult``, ``UsageTimeBucket``); endpoint
paths are **VERIFY** (SPEC §19.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tokenbill.adapters.anthropic_admin import (
    K_OAI_COSTS,
    K_OAI_USAGE,
    BadRecord,
    Page,
    PageAdapter,
    ReadContext,
    date_of,
    description,
    guarded,
    iter_bucket_rows,
    label,
    model_id,
    money_scaled,
    opt_tokens,
    req_tokens,
)
from tokenbill.core.records import UsageBuckets

__all__ = ["CHANNEL", "OPENAI_WRITE_TTL_S", "OpenAICostsAdapter", "OpenAIUsageBucketsAdapter"]

CHANNEL = "openai_api"
#: GPT-5.6+ cache writes have one TTL class, 30 minutes (SPEC §19.6, convention openai.responses).
OPENAI_WRITE_TTL_S = 1800
_COMPLETIONS = "organization.usage.completions.result"
#: Served-tier names → canonical ``PricingContext.service_tier`` values (others kept verbatim).
SERVICE_TIERS: Mapping[str, str] = {"default": "standard", "auto": "standard",
                                    "standard": "standard"}


def openai_usage(result: Mapping[str, Any], ctx: ReadContext) -> UsageBuckets:
    """Completions usage result → disjoint buckets (see the module docstring)."""
    total_in = req_tokens(result, "input_tokens")
    output = req_tokens(result, "output_tokens")
    cached = opt_tokens(result, "input_cached_tokens")
    write = opt_tokens(result, "input_cache_write_tokens")
    if result.get("input_uncached_tokens") is not None:
        uncached = req_tokens(result, "input_uncached_tokens")
        if uncached + cached + write != total_in:
            ctx.note("dq.sum_check_failed", "warn", "OpenAI usage bucket: uncached + cached + "
                     "cache_write != input_tokens (disjoint fields kept)", 1,
                     abs(total_in - uncached - cached - write))
    else:
        uncached = total_in - cached - write
        if uncached < 0:
            ctx.note("dq.sum_check_failed", "warn", "OpenAI usage bucket: cached + cache_write "
                     "exceed input_tokens (row quarantined)", 1, -uncached)
            raise BadRecord("bad_usage")
    return UsageBuckets(uncached_input=uncached, cache_read=cached, cache_write_other=write,
                        cache_write_other_ttl_s=OPENAI_WRITE_TTL_S if write else None,
                        output=output)


def service_tier(result: Mapping[str, Any]) -> str | None:
    """``batch: true`` → ``batch``; else the served tier (``default`` → ``standard``)."""
    batch = result.get("batch")
    if batch is not None and not isinstance(batch, bool):
        raise BadRecord("bad_type:batch")
    if batch:
        return "batch"
    tier = label(result.get("service_tier"), "service_tier")
    if tier is None:
        return None
    return SERVICE_TIERS.get(tier.lower(), tier.lower())


class OpenAIUsageBucketsAdapter(PageAdapter):
    """``openai-usage-buckets``: organization completions usage pages → token aggregates."""

    name = "openai-usage-buckets"
    capabilities = frozenset({"aggregates"})
    kinds = frozenset({K_OAI_USAGE})
    source_kind = "openai.usage"

    def handle_page(self, ctx: ReadContext, kind: str, page: Page, loc: str) -> None:
        for start, end, rloc, result in iter_bucket_rows(ctx, page, loc, unix_seconds=True):
            if result.get("object", _COMPLETIONS) != _COMPLETIONS:
                ctx.stat("results_skipped_other_kinds")  # embeddings, images, audio, …
                continue
            guarded(ctx, rloc, lambda r=result, s=start, e=end: self._row(ctx, page, s, e, r))

    def _row(self, ctx: ReadContext, page: Page, start: int, end: int,
             result: Mapping[str, Any]) -> None:
        dims = {
            "channel": CHANNEL,
            "workspace_id": ctx.name(result.get("project_id"), "project_id"),
            "api_key_id": ctx.name(result.get("api_key_id"), "api_key_id"),
            "model": model_id(result.get("model")),
            "service_tier": service_tier(result),
        }
        usage = openai_usage(result, ctx)  # last: its sum-check note is for this row only
        if result.get("user_id") is not None:
            ctx.stat("person_dims_dropped")
        ctx.add_aggregate(self.source_kind, start, end, dims, usage, fetched_ms=page.fetched_ms)


class OpenAICostsAdapter(PageAdapter):
    """``openai-costs``: organization costs pages → cost lines (exact USD, remainders in
    ``stats["rounding_remainder_e18"]``); rows differing only by API key are summed (a cost line
    has no API-key field)."""

    name = "openai-costs"
    capabilities = frozenset({"cost"})
    kinds = frozenset({K_OAI_COSTS})
    source_kind = "openai.costs"

    def handle_page(self, ctx: ReadContext, kind: str, page: Page, loc: str) -> None:
        for start, _end, rloc, result in iter_bucket_rows(ctx, page, loc, unix_seconds=True):
            guarded(ctx, rloc, lambda r=result, s=start: self._row(ctx, page, date_of(s), r))

    def _row(self, ctx: ReadContext, page: Page, day: str, result: Mapping[str, Any]) -> None:
        amount = result.get("amount")
        if not isinstance(amount, dict) or amount.get("value") is None:
            raise BadRecord("missing:amount")
        cur = amount.get("currency", "usd")
        if not isinstance(cur, str) or cur.strip().lower() != "usd":
            raise BadRecord("bad_type:currency")
        value = money_scaled(amount["value"], "amount", cents=False)
        ctx.add_cost(source_kind=self.source_kind, date_utc=day, channel=CHANNEL, amount=value,
                     workspace_id=ctx.name(result.get("project_id"), "project_id"),
                     description=description(result.get("line_item")),
                     fetched_ms=page.fetched_ms)

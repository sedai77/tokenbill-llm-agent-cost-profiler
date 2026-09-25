"""Invoice-line → (model, bucket) maps for the reconciliation ledger gate (SPEC §12, §19.8 #6, #17).

:data:`COST_TYPE_MAP` is **data**: the documented ``(cost_type, token_type)`` pairs of the Anthropic
Admin cost report, each mapped to a canonical bucket (a ``UsageBuckets`` field name, or one of the
non-token buckets ``web_search`` / ``code_execution`` / ``session_usage``). The enumerations were
checked against the primary source (the *Get Cost Report* API reference,
https://platform.claude.com/docs/en/api/admin-api/usage-cost/get-cost-report, on 2026-09-24):
``cost_type`` ∈ {``tokens``, ``web_search``, ``code_execution``, ``session_usage``} and
``token_type`` ∈ the five token fields. They are verified for the Admin cost report only
(:data:`COST_TYPE_VERIFIED_SOURCES`); the Enterprise Analytics cost endpoint groups by the same
names but documents no enumeration, so its lines map through the same table **unverified** and the
channel is reconciled in channel-total mode (§12.1).

CUR usage types and GCP SKUs map **only** through :func:`tokenbill.core.catalog.map_sku` (verified
rules of ``core.catalog.SKU_RULES``, shared with ADMIN). Every rule shipped today is
``verified: false`` (§19.8 #17), so Bedrock / Vertex invoices reconcile on channel totals.

:func:`map_line` classifies one cost line; the other tables name the source kinds, channels and
tiers the reconciliation treats specially. No floats (money module, SPEC §2.4).
"""

from __future__ import annotations

import re
import types
from collections.abc import Mapping
from dataclasses import dataclass

from tokenbill.core import catalog
from tokenbill.core.records import CostLine

__all__ = [
    "ADAPTER_SOURCE_KINDS",
    "CCU_CHANNELS",
    "COST_TYPE_MAP",
    "COST_TYPE_VERIFIED_SOURCES",
    "CREDIT_LINE_TYPES",
    "GROSS_RATE_SOURCES",
    "INVOICE_PRECEDENCE",
    "INVOICE_USAGE_PAIRS",
    "NO_REPORTING_API_CHANNELS",
    "NON_TOKEN_BUCKETS",
    "PRIORITY_EXCLUDED_SOURCES",
    "PRIORITY_TIERS",
    "REPORT_ONLY_BUCKETS",
    "SKU_SOURCES",
    "SOURCE_CHANNELS",
    "TOKEN_BUCKETS",
    "USAGE_PRECEDENCE",
    "LineMapping",
    "channel_provider",
    "map_line",
    "sku_rule_status",
]

#: Canonical token buckets (``UsageBuckets`` fields that count tokens).
TOKEN_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
                 "cache_write_other", "cache_write_unknown", "output")
#: Cost-report-only buckets: charges that no usage report carries (§12.3
#: ``code_execution_cost_report_only``; ``session_usage`` is the Managed Agents session runtime,
#: another cost-report-only runtime charge).
REPORT_ONLY_BUCKETS = ("code_execution", "session_usage")
#: Buckets that are not token counts (per-request server tools and report-only runtime charges).
NON_TOKEN_BUCKETS = ("web_search", *REPORT_ONLY_BUCKETS)

#: ``(cost_type, token_type)`` → bucket (SPEC §12 ``recon/costmap.py``). Verified 2026-09-24 against
#: the Get Cost Report reference for source kind ``anthropic.cost_report``.
COST_TYPE_MAP: Mapping[tuple[str | None, str | None], str] = types.MappingProxyType({
    ("tokens", "uncached_input_tokens"): "uncached_input",
    ("tokens", "cache_read_input_tokens"): "cache_read",
    ("tokens", "cache_creation.ephemeral_5m_input_tokens"): "cache_write_5m",
    ("tokens", "cache_creation.ephemeral_1h_input_tokens"): "cache_write_1h",
    ("tokens", "output_tokens"): "output",
    ("web_search", None): "web_search",
    ("code_execution", None): "code_execution",
    ("session_usage", None): "session_usage",
})
#: Invoice source kinds whose ``(cost_type, token_type)`` enumerations are verified against a
#: primary source (a ``COST_TYPE_MAP`` hit on any other kind is an unverified mapping).
COST_TYPE_VERIFIED_SOURCES = frozenset({"anthropic.cost_report"})
#: Invoice source kinds whose lines map through SKU rules (``core.catalog.map_sku``).
SKU_SOURCES = frozenset({"aws.cur2", "gcp.billing_export"})

#: Invoice source kinds in order of precedence on one channel: the first present is the channel's
#: invoice (the Admin cost report and the Enterprise cost endpoint cover the same spend, so they
#: are never added).
INVOICE_PRECEDENCE = ("anthropic.cost_report", "anthropic.enterprise_cost", "aws.cur2",
                      "gcp.billing_export", "openai.costs")
#: Invoice source kind → the provider usage source kinds priced against it (layer 1).
INVOICE_USAGE_PAIRS: Mapping[str, tuple[str, ...]] = types.MappingProxyType({
    "anthropic.cost_report": ("anthropic.usage_report",),
    "anthropic.enterprise_cost": ("anthropic.enterprise_usage",),
    "aws.cur2": ("aws.cur2",),
    "gcp.billing_export": ("gcp.billing_export",),
    "openai.costs": ("openai.usage",),
})
#: Provider usage kinds in order of precedence on a channel without invoice lines (token coverage
#: only). Team-level kinds (``anthropic.cc_analytics``, ``anthropic.enterprise_team_*``) are never
#: summed with organization-level ones (CONTRACT-CHANGE-ADMIN-1 (a)).
USAGE_PRECEDENCE = ("anthropic.usage_report", "anthropic.enterprise_usage", "aws.cur2",
                    "gcp.billing_export", "openai.usage")
#: Default channel of a source kind whose record carries no channel dim.
SOURCE_CHANNELS: Mapping[str, str] = types.MappingProxyType({
    "anthropic.usage_report": "anthropic_api", "anthropic.cost_report": "anthropic_api",
    "anthropic.enterprise_usage": "anthropic_api", "anthropic.enterprise_cost": "anthropic_api",
    "anthropic.cc_analytics": "anthropic_api", "aws.cur2": "bedrock",
    "gcp.billing_export": "vertex", "openai.usage": "openai_api", "openai.costs": "openai_api",
})
#: Registry adapter name → the invoice source kind whose parse remainders it reports
#: (``rounding_remainders`` are keyed by adapter name, ruling R-E44).
ADAPTER_SOURCE_KINDS: Mapping[str, str] = types.MappingProxyType({
    "anthropic-cost-report": "anthropic.cost_report",
    "anthropic-enterprise-analytics": "anthropic.enterprise_cost",
    "aws-cur": "aws.cur2",
    "gcp-billing": "gcp.billing_export",
    "openai-costs": "openai.costs",
})
#: Usage-report service tiers of Priority Tier (excluded from the Admin cost report, §19.4).
PRIORITY_TIERS = frozenset({"priority", "priority_on_demand"})
#: Invoice kinds that exclude Priority Tier usage.
PRIORITY_EXCLUDED_SOURCES = frozenset({"anthropic.cost_report"})
#: Invoice kinds whose ``amount`` nets credits reported beside a gross ``list_amount`` (GCP:
#: ``amount = cost + Σ credits``, ``list_amount = cost``): the rate card is checked against the
#: gross amount and the difference is residual ``cloud_credits``.
GROSS_RATE_SOURCES = frozenset({"gcp.billing_export"})
#: CUR ``line_item_line_item_type`` values (CostLine.cost_type) that are credits.
CREDIT_LINE_TYPES = frozenset({"Credit", "Refund"})
#: Channels billed as one capacity line (Microsoft Foundry, Claude Platform on AWS): residual
#: ``ccu_single_line`` (§12.3).
CCU_CHANNELS = frozenset({"foundry", "claude_platform_aws"})
#: Channels without a usage/cost reporting API (Claude Platform on AWS, §19.4).
NO_REPORTING_API_CHANNELS = frozenset({"claude_platform_aws"})

_OPENAI_CHANNELS = frozenset({"openai_api", "azure_openai"})
_GCP_TOKEN_COST_TYPES = frozenset({"", "regular"})
_CUR_USAGE_TYPES = frozenset({"", "Usage", "DiscountedUsage", "SavingsPlanCoveredUsage"})


def channel_provider(channel: str) -> str:
    """The pricing provider of *channel* (``openai`` for the OpenAI channels, else
    ``anthropic``)."""
    return "openai" if channel in _OPENAI_CHANNELS else "anthropic"


@dataclass(frozen=True, slots=True)
class LineMapping:
    """How one invoice line joins the reconciliation.

    ``kind``: ``"token"`` (a token or web-search bucket, joined to provider usage),
    ``"report_only"`` (code execution / session runtime), ``"credit"`` (credits and refunds),
    ``"unmapped"`` (a known non-token line with no rule: residual ``unmapped_cost_type``) or
    ``"unmappable"`` (a token line whose rule is missing or unverified, or whose model is unknown:
    the channel is reconciled on channel totals). ``verified`` is False when the rule used (or
    needed) is unverified.
    """

    kind: str
    bucket: str | None
    model: str | None
    verified: bool
    service_tier: str | None = None
    endpoint_scope: str | None = None


def sku_rule_status(source_kind: str, sku: str | None) -> tuple[catalog.SkuRule | None, bool]:
    """``(verified rule, matched_unverified)`` for *sku*: the rule ``core.catalog.map_sku``
    returns, and whether a **disabled** (unverified) rule of ``core.catalog.SKU_RULES`` matches it
    (a VERIFY rule: the line falls to channel totals and ``mapping_verified`` is False)."""
    if not isinstance(sku, str) or not sku:
        return None, False
    rule = catalog.map_sku(source_kind, sku)
    if rule is not None:
        return rule, False
    for candidate in catalog.SKU_RULES:
        if candidate.verified or candidate.source_kind != source_kind:
            continue
        try:
            if re.fullmatch(candidate.pattern, sku):
                return None, True
        except re.error:  # pragma: no cover - facts.json patterns are validated by F-CORE
            continue
    return None, False


def _sku_line(line: CostLine) -> LineMapping:
    cost_type = line.cost_type or ""
    if line.source_kind == "aws.cur2":
        if cost_type in CREDIT_LINE_TYPES:
            return LineMapping("credit", None, None, True)
        if cost_type not in _CUR_USAGE_TYPES:
            return LineMapping("unmapped", None, None, True)
    elif cost_type.lower() not in _GCP_TOKEN_COST_TYPES:
        return LineMapping("unmapped", None, None, True)
    rule, disabled = sku_rule_status(line.source_kind, line.sku)
    if rule is None:
        return LineMapping("unmappable", None, line.model, False)
    model = line.model or rule.model
    if not model:  # the model identity of a CUR row is not derivable (CONTRACT-CHANGE-ADMIN-1 (d))
        return LineMapping("unmappable", rule.bucket, None, False)
    return LineMapping("token", rule.bucket, model, True, rule.service_tier, rule.endpoint_scope)


def map_line(line: CostLine) -> LineMapping:
    """Classify one invoice line (see :class:`LineMapping`)."""
    if line.source_kind in SKU_SOURCES:
        return _sku_line(line)
    bucket = COST_TYPE_MAP.get((line.cost_type, line.token_type))
    verified = line.source_kind in COST_TYPE_VERIFIED_SOURCES
    if bucket is None:
        if line.cost_type == "tokens" or (line.cost_type is None and line.token_type is None):
            # a token line with an unknown token type, or a line with no cost type at all
            # (OpenAI line items): nothing maps it to (model, bucket)
            return LineMapping("unmappable", None, line.model, False)
        return LineMapping("unmapped", None, line.model, verified)
    if bucket in REPORT_ONLY_BUCKETS:
        return LineMapping("report_only", bucket, None, verified)
    if not verified:
        return LineMapping("unmappable", bucket, line.model, False)
    if bucket == "web_search":  # per workspace, never per model
        return LineMapping("token", bucket, None, True)
    if not line.model:
        return LineMapping("unmappable", bucket, None, False)
    return LineMapping("token", bucket, line.model, True, line.service_tier, line.endpoint_scope)

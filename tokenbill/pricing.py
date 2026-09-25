"""Pricing and prompt-cache rule tables as versioned, sourced data.

Every number in this module is transcribed from the provider's public
documentation and carries a source comment. Dollar math built on these tables
is **exact** with respect to billed usage: Token Bill multiplies real billed
token counts by these published rates. What is approximate elsewhere in the
pipeline (char-based attribution) is labeled there — never here.

Model ids resolve through :func:`pricing_for`: an exact table entry wins, and
otherwise a dated snapshot id (``claude-haiku-4-5-20251001``, or Vertex-style
``claude-sonnet-4-6@20260101``) is priced as its base model.

Verify the tables against the source before each release:
https://platform.claude.com/docs/en/about-claude/pricing.md
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pricing duck-types usage; no runtime dependency on trace
    from tokenbill.trace import Usage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    """Published per-model rates (USD per million tokens) and cache limits.

    ``cache_write_multiplier`` applies to ``cache_creation_input_tokens``
    (5-minute-TTL writes); ``cache_read_multiplier`` applies to
    ``cache_read_input_tokens``. Both multiply the base input rate.
    ``cache_write_1h_multiplier`` documents the 1-hour-TTL write rate; the
    v0.1 engine does not use it (the v0.2 registry in ``tokenbill.rates``
    prices 1h writes).
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_write_multiplier: float = 1.25  # 5-minute TTL writes
    cache_read_multiplier: float = 0.10
    min_cacheable_prefix_tokens: int = 1024
    cache_write_1h_multiplier: float = 2.0  # 1-hour TTL writes (documentation only in v0.1)


# Source: https://platform.claude.com/docs/en/about-claude/pricing.md — verified 2026-09.
# VERIFY BEFORE EACH RELEASE: rates and minimum cacheable prefix lengths change
# between model generations; re-check every row against the doc above.
PRICING: dict[str, ModelPricing] = {
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-09-24).
    # Cache hits are $0.20/MTok: 0.05x base input. Effective 2026-09-22 (launch).
    "claude-opus-5-5": ModelPricing(
        4.00, 20.00, cache_read_multiplier=0.05, min_cacheable_prefix_tokens=512
    ),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-09-24).
    # Cache hits are $0.25/MTok: 0.025x base input (like Fable 5.1). Limited availability.
    "claude-mythos-5-1": ModelPricing(
        10.00, 50.00, cache_read_multiplier=0.025, min_cacheable_prefix_tokens=512
    ),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-09-24)
    "claude-mythos-5": ModelPricing(10.00, 50.00, min_cacheable_prefix_tokens=512),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-09).
    # Cache hits are $0.25/MTok: 0.025x base input, not the standard 0.10x.
    "claude-fable-5-1": ModelPricing(
        10.00, 50.00, cache_read_multiplier=0.025, min_cacheable_prefix_tokens=512
    ),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-07)
    "claude-opus-5": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=512),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-07)
    "claude-fable-5": ModelPricing(10.00, 50.00, min_cacheable_prefix_tokens=512),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-07)
    "claude-opus-4-8": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=1024),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-07)
    "claude-opus-4-7": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=2048),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-07)
    "claude-opus-4-6": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=4096),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-09-24);
    # minimum prefix from .../build-with-claude/prompt-caching (verified 2026-09-24).
    "claude-opus-4-5": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=4096),
    # Same sources (verified 2026-09-24). Retired on the Claude API 2026-08-05 (still billed on
    # Bedrock and Google Cloud); minimum prefix verified, so the row is kept (SPEC §6.8).
    "claude-opus-4-1": ModelPricing(15.00, 75.00, min_cacheable_prefix_tokens=1024),
    # Same sources (verified 2026-09-24). Retired on the Claude API 2026-06-15.
    "claude-opus-4": ModelPricing(15.00, 75.00, min_cacheable_prefix_tokens=1024),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-09).
    # The $2.00/$10.00 launch rate is now the standard price; the scheduled
    # 2026-09-01 increase to $3.00/$15.00 was cancelled.
    "claude-sonnet-5": ModelPricing(2.00, 10.00, min_cacheable_prefix_tokens=1024),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-07)
    "claude-sonnet-4-6": ModelPricing(3.00, 15.00, min_cacheable_prefix_tokens=1024),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-09-24)
    "claude-sonnet-4-5": ModelPricing(3.00, 15.00, min_cacheable_prefix_tokens=1024),
    # https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-07)
    "claude-haiku-4-5": ModelPricing(1.00, 5.00, min_cacheable_prefix_tokens=4096),
}


# Prompt-cache rules, same source and verification cadence as PRICING:
# https://platform.claude.com/docs/en/about-claude/pricing.md (verified 2026-07).
CACHE_TTL_SECONDS = 300  # 5-minute cache entry lifetime.
# Documented assumption: a cache read refreshes the entry's TTL (sliding
# expiry). The simulator models this; DESIGN.md lists it under threats to
# validity in case server behavior diverges from the docs.
TTL_REFRESH_ON_READ = True
MAX_BREAKPOINTS = 4  # maximum cache_control markers per request.
# Provider's documented request render order — the byte-comparison substrate
# (trace.render_segments) mirrors it.
RENDER_ORDER = ("tools", "system", "messages")

_MTOK = 1_000_000

#: Models already warned about — a few-hundred-call trace must produce ONE
#: unknown-model warning per model id, not one per lookup.
_warned_models: set[str] = set()

#: Dated snapshot suffix: ``-YYYYMMDD`` (first-party) or ``@YYYYMMDD`` (Vertex).
_SNAPSHOT_SUFFIX = re.compile(r"[-@]\d{8}$")


def pricing_for(model: str) -> ModelPricing | None:
    """The :data:`PRICING` row for *model*, or ``None`` when unknown.

    An exact entry wins (so a ``--model-price`` for a dated id is honored);
    otherwise a dated snapshot id resolves to its base model's row — a
    snapshot is billed and cache-gated exactly like the model it pins.
    """
    entry = PRICING.get(model)
    if entry is None:
        base = _SNAPSHOT_SUFFIX.sub("", model)
        if base != model:
            entry = PRICING.get(base)
    return entry


def cost_breakdown(model: str, usage: Usage) -> dict[str, float] | None:
    """Billed dollars by category for one call: exact, from real usage.

    Returns ``{"uncached": ..., "write": ..., "read": ..., "output": ...}``
    in USD, or ``None`` (with a warning logged once per unknown model) when
    :func:`pricing_for` finds no row — never crash; token counts are still
    reported upstream.
    """
    pricing = pricing_for(model)
    if pricing is None:
        if model not in _warned_models:
            _warned_models.add(model)
            logger.warning(
                "unknown model %r: no pricing entry, reporting tokens without dollars "
                "(known models: %s)",
                model,
                ", ".join(sorted(PRICING)),
            )
        return None
    return {
        "uncached": usage.input_tokens / _MTOK * pricing.input_per_mtok,
        "write": (
            usage.cache_creation_input_tokens
            / _MTOK
            * pricing.input_per_mtok
            * pricing.cache_write_multiplier
        ),
        "read": (
            usage.cache_read_input_tokens
            / _MTOK
            * pricing.input_per_mtok
            * pricing.cache_read_multiplier
        ),
        "output": usage.output_tokens / _MTOK * pricing.output_per_mtok,
    }


def price_usd(model: str, usage: Usage) -> float | None:
    """Total billed dollars for one call, or ``None`` for unknown models.

    Exact: real billed token counts times published rates. Unknown models
    warn once per model id and return ``None`` — never crash; tokens are
    still reported without dollars.
    """
    breakdown = cost_breakdown(model, usage)
    if breakdown is None:
        return None
    return sum(breakdown.values())

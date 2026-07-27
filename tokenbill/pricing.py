"""Pricing and prompt-cache rule tables as versioned, sourced data.

Every number in this module is transcribed from the provider's public
documentation and carries a source comment. Dollar math built on these tables
is **exact** with respect to billed usage: Token Bill multiplies real billed
token counts by these published rates. What is approximate elsewhere in the
pipeline (char-based attribution) is labeled there — never here.

Known deliberate deviation: ``claude-sonnet-5`` has *introductory* billing
($2.00 in / $10.00 out per MTok) in effect through 2026-08-31; this table
carries the standard $3.00/$15.00 rates the SPEC pins, so sonnet-5 dollar
figures can overstate real bills during the introductory window. The report's
pricing footnote states this.

Verify the tables against the source before each release:
https://platform.claude.com/docs/en/pricing.md
"""

from __future__ import annotations

import logging
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
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_write_multiplier: float = 1.25  # 5-minute TTL writes
    cache_read_multiplier: float = 0.10
    min_cacheable_prefix_tokens: int = 1024


# Source: https://platform.claude.com/docs/en/pricing.md — verified 2026-07.
# VERIFY BEFORE EACH RELEASE: rates and minimum cacheable prefix lengths change
# between model generations; re-check every row against the doc above.
PRICING: dict[str, ModelPricing] = {
    # https://platform.claude.com/docs/en/pricing.md (verified 2026-07)
    "claude-opus-5": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=512),
    # https://platform.claude.com/docs/en/pricing.md (verified 2026-07)
    "claude-fable-5": ModelPricing(10.00, 50.00, min_cacheable_prefix_tokens=512),
    # https://platform.claude.com/docs/en/pricing.md (verified 2026-07)
    "claude-opus-4-8": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=1024),
    # https://platform.claude.com/docs/en/pricing.md (verified 2026-07)
    "claude-opus-4-7": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=2048),
    # https://platform.claude.com/docs/en/pricing.md (verified 2026-07)
    "claude-opus-4-6": ModelPricing(5.00, 25.00, min_cacheable_prefix_tokens=4096),
    # https://platform.claude.com/docs/en/pricing.md (verified 2026-07).
    # Introductory pricing of $2.00/$10.00 per MTok applies through
    # 2026-08-31; the standard rates below are used deliberately (see module
    # docstring) — sonnet-5 dollars can overstate bills until then.
    "claude-sonnet-5": ModelPricing(3.00, 15.00, min_cacheable_prefix_tokens=1024),
    # https://platform.claude.com/docs/en/pricing.md (verified 2026-07)
    "claude-sonnet-4-6": ModelPricing(3.00, 15.00, min_cacheable_prefix_tokens=1024),
    # https://platform.claude.com/docs/en/pricing.md (verified 2026-07)
    "claude-haiku-4-5": ModelPricing(1.00, 5.00, min_cacheable_prefix_tokens=4096),
}


# Prompt-cache rules, same source and verification cadence as PRICING:
# https://platform.claude.com/docs/en/pricing.md (verified 2026-07).
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


def cost_breakdown(model: str, usage: Usage) -> dict[str, float] | None:
    """Billed dollars by category for one call: exact, from real usage.

    Returns ``{"uncached": ..., "write": ..., "read": ..., "output": ...}``
    in USD, or ``None`` (with a warning logged once per unknown model) when
    *model* is not in :data:`PRICING` — never crash; token counts are still
    reported upstream.
    """
    pricing = PRICING.get(model)
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

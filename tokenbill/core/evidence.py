"""Published constants with sources (SPEC §3.17), read from ``core/facts.json``.

Each constant is an :class:`EvidenceConstant` ``(value, source_url, finding_id, checked_on)``. These
are published benchmarks and documented defaults, never fleet predictions; outputs that show one
show its source and date (SPEC §1.2 rule 6).
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from decimal import Decimal
from typing import NamedTuple

from tokenbill.core.errors import ContractViolation
from tokenbill.core.facts import load
from tokenbill.core.money import RATIO_CTX

__all__ = [
    "BATCH_CACHE_HIT_BAND",
    "CACHE_READ_SHARE_INVESTIGATE_BELOW",
    "CACHE_READ_SHARE_MEDIAN",
    "CACHE_READ_SHARE_TOP_DECILE",
    "CC_AUTOCOMPACT_DEFAULT_TOKENS",
    "CC_FLEET_USD_PER_ACTIVE_DAY",
    "CC_FLEET_USD_PER_ACTIVE_DAY_P90_UNDER",
    "CODE_REVIEW_USD_PER_REVIEW",
    "COLD_RESUME_MIN_CONTEXT",
    "COMPACTION_SUMMARY_TOKENS_DEFAULT",
    "CPT_DEFAULT_47PLUS_TOOL_OUTPUT",
    "CPT_DEFAULT_LEGACY",
    "EDIT_PAYBACK_FORMULA",
    "FEMP_HOURLY_DAILY",
    "FEMP_MONTHLY",
    "KEEPALIVE_BREAK_EVEN",
    "KEEPALIVE_INTERVAL_S",
    "KEEPALIVE_MAX_IDLE_S",
    "MAX_TOKENS_AGENTIC_RECOMMENDED",
    "MAX_TOKENS_AGENTIC_RECOMMENDED_XHIGH",
    "MIN_CALIBRATION_PERIODS",
    "MISS_MIN_FRACTION",
    "MISS_MIN_TOKENS",
    "REFUSAL_AMBIGUOUS_MAX_OUTPUT",
    "STALE_PROMPT_OUTPUT_DELTA",
    "TABLE",
    "THINKING_SHARE_PRIOR",
    "TOKENIZER_BAND",
    "TOOL_DEFS_DEFER_THRESHOLD_TOKENS",
    "TOOL_SEARCH_REDUCTION_BAND",
    "TTL_RULE_GAP_SHARE_5_60MIN",
    "EvidenceConstant",
    "get",
    "keepalive_break_even_s",
]


class EvidenceConstant(NamedTuple):
    """A published constant: ``(value, source_url, finding_id, checked_on)``."""

    value: Decimal | int | str | tuple[Decimal, Decimal]
    source_url: str
    finding_id: str
    checked_on: str


TABLE: Mapping[str, EvidenceConstant] = types.MappingProxyType(
    {
        name: EvidenceConstant(fact.value, fact.source, fact.finding, fact.verified_on)
        for name, fact in load().evidence.items()
    }
)


def get(name: str) -> EvidenceConstant:
    """The constant *name* (ContractViolation when facts.json does not define it)."""
    try:
        return TABLE[name]
    except KeyError:
        raise ContractViolation(f"evidence constant {name} is not defined in facts.json") from None


MISS_MIN_TOKENS = get("MISS_MIN_TOKENS")
MISS_MIN_FRACTION = get("MISS_MIN_FRACTION")
CACHE_READ_SHARE_MEDIAN = get("CACHE_READ_SHARE_MEDIAN")
CACHE_READ_SHARE_TOP_DECILE = get("CACHE_READ_SHARE_TOP_DECILE")
CACHE_READ_SHARE_INVESTIGATE_BELOW = get("CACHE_READ_SHARE_INVESTIGATE_BELOW")
TTL_RULE_GAP_SHARE_5_60MIN = get("TTL_RULE_GAP_SHARE_5_60MIN")
KEEPALIVE_INTERVAL_S = get("KEEPALIVE_INTERVAL_S")
KEEPALIVE_MAX_IDLE_S = get("KEEPALIVE_MAX_IDLE_S")
KEEPALIVE_BREAK_EVEN = get("KEEPALIVE_BREAK_EVEN")
CC_AUTOCOMPACT_DEFAULT_TOKENS = get("CC_AUTOCOMPACT_DEFAULT_TOKENS")
COMPACTION_SUMMARY_TOKENS_DEFAULT = get("COMPACTION_SUMMARY_TOKENS_DEFAULT")
THINKING_SHARE_PRIOR = get("THINKING_SHARE_PRIOR")
TOKENIZER_BAND = get("TOKENIZER_BAND")
BATCH_CACHE_HIT_BAND = get("BATCH_CACHE_HIT_BAND")
CC_FLEET_USD_PER_ACTIVE_DAY = get("CC_FLEET_USD_PER_ACTIVE_DAY")
CC_FLEET_USD_PER_ACTIVE_DAY_P90_UNDER = get("CC_FLEET_USD_PER_ACTIVE_DAY_P90_UNDER")
REFUSAL_AMBIGUOUS_MAX_OUTPUT = get("REFUSAL_AMBIGUOUS_MAX_OUTPUT")
CPT_DEFAULT_47PLUS_TOOL_OUTPUT = get("CPT_DEFAULT_47PLUS_TOOL_OUTPUT")
CPT_DEFAULT_LEGACY = get("CPT_DEFAULT_LEGACY")
COLD_RESUME_MIN_CONTEXT = get("COLD_RESUME_MIN_CONTEXT")
FEMP_MONTHLY = get("FEMP_MONTHLY")
FEMP_HOURLY_DAILY = get("FEMP_HOURLY_DAILY")
MIN_CALIBRATION_PERIODS = get("MIN_CALIBRATION_PERIODS")
MAX_TOKENS_AGENTIC_RECOMMENDED = get("MAX_TOKENS_AGENTIC_RECOMMENDED")
MAX_TOKENS_AGENTIC_RECOMMENDED_XHIGH = get("MAX_TOKENS_AGENTIC_RECOMMENDED_XHIGH")
TOOL_DEFS_DEFER_THRESHOLD_TOKENS = get("TOOL_DEFS_DEFER_THRESHOLD_TOKENS")
TOOL_SEARCH_REDUCTION_BAND = get("TOOL_SEARCH_REDUCTION_BAND")
EDIT_PAYBACK_FORMULA = get("EDIT_PAYBACK_FORMULA")
CODE_REVIEW_USD_PER_REVIEW = get("CODE_REVIEW_USD_PER_REVIEW")
STALE_PROMPT_OUTPUT_DELTA = get("STALE_PROMPT_OUTPUT_DELTA")


def keepalive_break_even_s(
    write_mult: Decimal, read_mult: Decimal, interval_s: int | None = None
) -> Decimal:
    """``κ(w/r − 1)`` in seconds (KEEPALIVE_BREAK_EVEN; κ defaults to KEEPALIVE_INTERVAL_S = 240 s):
    ≈ 46 min at r = 0.1×, ≈ 96 min on Opus 5.5 (r = 0.05×), ≈ 196 min on Fable 5.1 (r = 0.025×)."""
    kappa = interval_s if interval_s is not None else KEEPALIVE_INTERVAL_S.value
    if not isinstance(kappa, int) or read_mult <= 0:
        raise ContractViolation(
            "keepalive_break_even_s: needs an int interval and a positive read multiplier"
        )
    return RATIO_CTX.multiply(Decimal(kappa), RATIO_CTX.divide(write_mult, read_mult) - 1)

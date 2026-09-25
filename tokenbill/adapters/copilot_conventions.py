"""GitHub Copilot CLI usage conventions and billing rules (addendum §5.11, CP-LOCAL).

Importing this module registers three conventions (``core.registry.CONVENTION_MODULES`` lists it, so
``core.conventions.normalize`` imports it lazily the first time one of these ids is requested):

* ``github_copilot.shutdown_rollup`` — ``session.shutdown`` ``modelMetrics[m].usage``:
  ``inputTokens`` **includes** ``cacheReadTokens`` and ``cacheWriteTokens`` (CLI 1.0.51 changelog;
  rollup arithmetic 23,399 = 6 + 10,069 + 13,324); uncached = input − read − write; writes have no
  TTL split (``cache_write_unknown``); read + write > input → the counts are taken as exclusive
  with ``dq.convention_mismatch``.
* ``github_copilot.session_store`` — ``session-store.db`` ``assistant_usage_events`` rows (columns
  ``input_tokens``, ``cache_read_tokens``, ``cache_write_tokens``, ``output_tokens``,
  ``reasoning_tokens``); ``input_tokens`` assumed cache-inclusive (third-party evidence only,
  **VERIFY**); a negative uncached remainder raises :class:`NegativeUncachedError` (the adapter
  quarantines the row, ``dq.convention_mismatch``).
* ``github_copilot.token_details`` — ``copilotUsage`` ``{tokenDetails: [{tokenType, tokenCount,
  batchSize, costPerBatch}], totalNanoAiu}``; ``input`` is uncached (payload arithmetic reproduces
  the published Opus 4.7 rates); ``cache_write`` → ``cache_write_unknown``; sum-check
  Σ tokenCount × costPerBatch / batchSize == totalNanoAiu (a difference below one nano-AIU is
  rounding), else ``dq.copilot_nano_aiu_mismatch``.

:func:`billing_rule` is the §5.11 utility / BYOK rule: a call is unbilled only when its model is a
``facts.copilot.utility_models`` model **and** its nano-AIU is 0, or its ``interactionType`` is
``conversation-background`` with nano-AIU 0 (never by name alone: GPT-5.4 nano is also billed); BYOK
calls are never billed by GitHub.

Money here is exact: nano-AIU sums use :class:`fractions.Fraction` over ints and ``Decimal`` values;
no float is ever constructed.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from tokenbill.core.conventions import BadUsageError, Convention, register_convention
from tokenbill.core.records import MAX_TOKENS, UsageBuckets

__all__ = [
    "BYOK_RULE",
    "CONVENTION_SESSION_STORE",
    "CONVENTION_SHUTDOWN_ROLLUP",
    "CONVENTION_TOKEN_DETAILS",
    "DQ_CONVENTION_MISMATCH",
    "DQ_NANO_AIU_MISMATCH",
    "TOKEN_TYPES",
    "UTILITY_RULE",
    "NegativeUncachedError",
    "billing_rule",
    "exact_number",
    "normalize_session_store",
    "normalize_shutdown_rollup",
    "normalize_token_details",
    "token_details_nano_aiu",
    "utility_models",
]

CONVENTION_SHUTDOWN_ROLLUP = "github_copilot.shutdown_rollup"
CONVENTION_SESSION_STORE = "github_copilot.session_store"
CONVENTION_TOKEN_DETAILS = "github_copilot.token_details"

DQ_CONVENTION_MISMATCH = "dq.convention_mismatch"
DQ_NANO_AIU_MISMATCH = "dq.copilot_nano_aiu_mismatch"

#: Billing-rule ids of addendum §5.11.
UTILITY_RULE = "github.copilot.utility_unbilled"
BYOK_RULE = "github.copilot.byok_not_billed_by_github"

#: ``tokenDetails[].tokenType`` values (``core.records.RAW_USAGE_ENUMS["tokenType"]``).
TOKEN_TYPES = ("input", "cache_read", "cache_write", "output")
_BACKGROUND = "conversation-background"
_MAX_DETAILS = 256


class NegativeUncachedError(BadUsageError):
    """A cache-inclusive input count smaller than its cache read + write (``bad_usage``)."""


def exact_number(value: object) -> Fraction | None:
    """An exact non-negative finite number (``int``, ``Decimal`` or a JSON float re-read through its
    shortest ``repr``) as a :class:`Fraction`; None for anything else (bools, strings, NaN, …)."""
    if type(value) is int:
        return Fraction(value) if value >= 0 else None
    if isinstance(value, Decimal):
        dec = value
    elif type(value) is float:  # a JSON number re-parsed without exact_numbers (never money here)
        try:
            dec = Decimal(repr(value))
        except InvalidOperation:  # pragma: no cover - repr of a float is always a valid literal
            return None
    else:
        return None
    if not dec.is_finite() or dec < 0:
        return None
    return Fraction(dec)


def _count(raw: Mapping[str, Any], key: str, where: str = "") -> int | None:
    """A token count; None when absent or null; anything but an int in [0, 2**53] raises."""
    value = raw.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > MAX_TOKENS:
        raise BadUsageError(f"bad_usage: {where}{key}")
    return value


def _reasoning(reasoning: int | None, output: int, notes: list[str]) -> int | None:
    if reasoning is not None and reasoning > output:
        notes.append("dq.sum_check_failed")
        return None
    return reasoning


def _inclusive(input_total: int, read: int, write: int, output: int, reasoning: int | None,
               notes: list[str], *, strict: bool) -> UsageBuckets:
    """Split a cache-inclusive input count; ``strict`` raises on a negative remainder, otherwise the
    counts are treated as exclusive with ``dq.convention_mismatch``."""
    uncached = input_total - read - write
    if uncached < 0:
        if strict:
            raise NegativeUncachedError("bad_usage: negative uncached input")
        notes.append(DQ_CONVENTION_MISMATCH)
        uncached = input_total
    return UsageBuckets(uncached_input=uncached, cache_read=read, cache_write_unknown=write,
                        output=output, output_reasoning=_reasoning(reasoning, output, notes))


def normalize_shutdown_rollup(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``github_copilot.shutdown_rollup`` of one ``modelMetrics[m].usage`` object (camelCase keys
    ``inputTokens``, ``cacheReadTokens``, ``cacheWriteTokens``, ``outputTokens``,
    ``reasoningTokens``; missing counts are 0)."""
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    notes: list[str] = []
    buckets = _inclusive(_count(raw, "inputTokens") or 0, _count(raw, "cacheReadTokens") or 0,
                         _count(raw, "cacheWriteTokens") or 0, _count(raw, "outputTokens") or 0,
                         _count(raw, "reasoningTokens"), notes, strict=False)
    return buckets, notes


def normalize_session_store(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``github_copilot.session_store`` of one ``assistant_usage_events`` row (snake_case columns;
    missing counts are 0). A negative uncached remainder raises :class:`NegativeUncachedError`."""
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    notes: list[str] = []
    buckets = _inclusive(_count(raw, "input_tokens") or 0, _count(raw, "cache_read_tokens") or 0,
                         _count(raw, "cache_write_tokens") or 0, _count(raw, "output_tokens") or 0,
                         _count(raw, "reasoning_tokens"), notes, strict=True)
    return buckets, notes


def _details(raw: Mapping[str, object]) -> list[Mapping[str, Any]]:
    details = raw.get("tokenDetails")
    if not isinstance(details, list) or len(details) > _MAX_DETAILS:
        raise BadUsageError("bad_usage: tokenDetails")
    for item in details:
        if not isinstance(item, Mapping):
            raise BadUsageError("bad_usage: tokenDetails")
    return details  # type: ignore[return-value]


def token_details_nano_aiu(raw: Mapping[str, object]) -> Fraction:
    """Σ ``tokenCount × costPerBatch / batchSize`` over ``raw["tokenDetails"]``, exactly.

    A non-positive or non-numeric ``batchSize`` or a non-numeric ``costPerBatch`` raises
    :class:`~tokenbill.core.conventions.BadUsageError`."""
    total = Fraction(0)
    for item in _details(raw):
        count = _count(item, "tokenCount", "tokenDetails.") or 0
        batch = exact_number(item.get("batchSize"))
        cost = exact_number(item.get("costPerBatch"))
        if batch is None or batch == 0:
            raise BadUsageError("bad_usage: tokenDetails.batchSize")
        if cost is None:
            raise BadUsageError("bad_usage: tokenDetails.costPerBatch")
        total += count * cost / batch
    return total


def normalize_token_details(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``github_copilot.token_details`` of one ``copilotUsage`` object (``tokenDetails`` list plus
    ``totalNanoAiu``). Entries of one token type are summed; an unknown ``tokenType`` raises
    :class:`~tokenbill.core.conventions.BadUsageError`. The sum-check note
    ``dq.copilot_nano_aiu_mismatch`` is returned when Σ tokenCount × costPerBatch / batchSize
    differs from ``totalNanoAiu`` by one nano-AIU or more (skipped when ``totalNanoAiu`` is
    absent)."""
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    sums = dict.fromkeys(TOKEN_TYPES, 0)
    for item in _details(raw):
        kind = item.get("tokenType")
        if not isinstance(kind, str) or kind not in sums:
            raise BadUsageError("bad_usage: tokenDetails.tokenType")
        sums[kind] += _count(item, "tokenCount", "tokenDetails.") or 0
    for kind, value in sums.items():
        if value > MAX_TOKENS:
            raise BadUsageError(f"bad_usage: tokenDetails.{kind}")
    notes: list[str] = []
    total_raw = raw.get("totalNanoAiu")
    if total_raw is not None:
        total = exact_number(total_raw)
        if total is None:
            raise BadUsageError("bad_usage: totalNanoAiu")
        if abs(token_details_nano_aiu(raw) - total) >= 1:
            notes.append(DQ_NANO_AIU_MISMATCH)
    buckets = UsageBuckets(uncached_input=sums["input"], cache_read=sums["cache_read"],
                           cache_write_unknown=sums["cache_write"], output=sums["output"])
    return buckets, notes


@functools.lru_cache(maxsize=1)
def utility_models() -> frozenset[str]:
    """``facts.copilot.utility_models`` (canonical Copilot model ids), empty when the facts carry
    no Copilot section."""
    from tokenbill.core.facts import load as load_facts

    copilot = getattr(load_facts(), "copilot", None)
    return frozenset(copilot.utility_models) if copilot is not None else frozenset()


def billing_rule(model: str, *, nano_aiu: int | Fraction | None,
                 interaction_type: str | None = None,
                 is_byok: bool = False) -> tuple[bool, str | None]:
    """``(billable, billing_rule_id)`` of one Copilot call (addendum §5.11).

    BYOK → ``(False, "github.copilot.byok_not_billed_by_github")``; nano-AIU exactly 0 **and**
    (*model* ∈ ``facts.copilot.utility_models`` or *interaction_type* ==
    ``"conversation-background"``) → ``(False, "github.copilot.utility_unbilled")``; anything else,
    including an unknown nano-AIU, → ``(True, None)``."""
    if is_byok:
        return False, BYOK_RULE
    if nano_aiu is not None and nano_aiu == 0 and (
            model in utility_models() or interaction_type == _BACKGROUND):
        return False, UTILITY_RULE
    return True, None


register_convention(
    Convention(CONVENTION_SHUTDOWN_ROLLUP, "github", True, True,
               "Copilot CLI session.shutdown modelMetrics usage: inputTokens includes cache read "
               "and write (CLI 1.0.51 changelog); uncached = input - read - write; writes have no "
               "TTL split; cumulative across resume legs (addendum §5.11)"),
    normalize_shutdown_rollup)
register_convention(
    Convention(CONVENTION_SESSION_STORE, "github", True, True,
               "Copilot CLI session-store.db assistant_usage_events: input_tokens assumed "
               "cache-inclusive (third-party evidence only, VERIFY); negative uncached -> "
               "quarantine (experimental flag copilot-store)"),
    normalize_session_store)
register_convention(
    Convention(CONVENTION_TOKEN_DETAILS, "github", False, True,
               "Copilot copilotUsage.tokenDetails: input is uncached; cache_write has no TTL "
               "split; sum-check tokenCount x costPerBatch / batchSize == totalNanoAiu "
               "(addendum §5.11)"),
    normalize_token_details)

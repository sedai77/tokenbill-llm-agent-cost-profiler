"""Usage conventions of the non-Anthropic sources, and the plumbing shared by the TELEM adapters.

Importing this module registers (SPEC §5.2, ``core.registry.CONVENTION_MODULES``):

========================  ================================================================
``bedrock.converse``      ``inputTokens`` excludes cache; ``cacheReadInputTokens``;
                          ``cacheWriteInputTokens`` split by ``cacheDetails[{ttl, inputTokens}]``
                          (``5m``/``1h``), the remainder is ``cache_write_unknown``
``openai.responses``      ``input_tokens`` includes ``input_tokens_details.{cached_tokens,
                          cache_write_tokens}``; a negative uncached residual raises
                          :class:`SumCheckError` (``dq.sum_check_failed``, quarantine)
``openai.chat``           ``prompt_tokens`` includes ``prompt_tokens_details.{cached_tokens,
                          cache_write_tokens}``; same rule
``otel.genai``            ``gen_ai.usage.input_tokens`` includes ``cache_read`` / ``cache_write``;
                          read + write > input → exclusive with ``dq.convention_mismatch``
``otel.genai.legacy``     the same with ``gen_ai.usage.cache_creation.input_tokens`` (semconv
                          1.40–1.41) and the deprecated ``prompt_tokens`` / ``completion_tokens``
``openinference``         ``llm.token_count.prompt`` includes ``prompt_details.{cache_read,
                          cache_write}``; ``llm.token_count.total`` is sum-checked
``claude_code.otel``      exclusive counts, no TTL split: writes → ``cache_write_unknown``
                          (``dq.no_ttl_split``)
========================  ================================================================

Every normalizer takes the provider's usage mapping (for the span/event conventions: a flat
mapping of the attribute names above to ints) and returns ``(UsageBuckets, dq codes)``; malformed
counts raise :class:`~tokenbill.core.conventions.BadUsageError` (quarantine reason ``bad_usage``).

The second half of the module is shared adapter plumbing (TELEM-internal, used by
``adapters.otel``, ``adapters.openai``, ``adapters.bedrock`` and ``adapters.anthropic_responses``):
per-file bookkeeping (:class:`SourceScan`), identity and name pseudonymization, channel tables,
draft requests and lane assembly. Nothing here reads or keeps content: only numbers, provider enum
strings, HMACs and allowlisted names leave it.
"""

from __future__ import annotations

import calendar
import dataclasses
import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tokenbill.core.conventions import (
    BadUsageError,
    Convention,
    register_convention,
    sum_check,
)
from tokenbill.core.errors import ContractViolation, SourceError, TokenbillError, UsageError
from tokenbill.core.ids import is_opaque_ref, pseudonym, stable_id
from tokenbill.core.jsonl import ZSTD_MESSAGE, iter_lines, parse_json_line
from tokenbill.core.money import decimal_to_nano
from tokenbill.core.records import (
    BILLING_PATHS,
    EXTRA_KEYS,
    MAX_TOKENS,
    AppendedItem,
    Attempt,
    Attribution,
    Fidelity,
    Lane,
    LaneEvent,
    LaneKind,
    Request,
    RequestParams,
    Session,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    WorkloadClass,
)
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    SourceInfo,
)

__all__ = [
    "BEDROCK_CONVERSE",
    "BILLING_PATH_BY_CHANNEL",
    "CLAUDE_CODE_OTEL",
    "CLAUDE_CODE_OTEL_KEYS",
    "GENAI_KEYS",
    "GENAI_LEGACY_KEYS",
    "OPENAI_CACHE_WRITE_TTL_S",
    "OPENAI_CHAT",
    "OPENAI_RESPONSES",
    "OPENINFERENCE",
    "OPENINFERENCE_KEYS",
    "OTEL_GENAI",
    "OTEL_GENAI_LEGACY",
    "Draft",
    "LaneShell",
    "SourceScan",
    "SumCheckError",
    "assemble",
    "cache_scope",
    "canonical_usage_json",
    "clean_label",
    "member",
    "name_or_hash",
    "normalize_bedrock_converse",
    "normalize_identity",
    "normalize_claude_code_otel",
    "normalize_openai_chat",
    "normalize_openai_responses",
    "normalize_openinference",
    "normalize_otel_genai",
    "normalize_otel_genai_legacy",
    "parse_iso_ms",
    "principal_for",
    "team_for",
    "to_int",
    "usd_to_nano",
]

BEDROCK_CONVERSE = "bedrock.converse"
OPENAI_RESPONSES = "openai.responses"
OPENAI_CHAT = "openai.chat"
OTEL_GENAI = "otel.genai"
OTEL_GENAI_LEGACY = "otel.genai.legacy"
OPENINFERENCE = "openinference"
CLAUDE_CODE_OTEL = "claude_code.otel"

#: OpenAI GPT-5.6+ cache writes have a single class: ``prompt_cache_options.ttl = "30m"`` (§19.6).
OPENAI_CACHE_WRITE_TTL_S = 1800


class SumCheckError(BadUsageError):
    """An inclusive usage object whose cached + written tokens exceed its input total.

    The record is quarantined (reason ``bad_usage``) and ``dq.sum_check_failed`` is recorded
    (§5.2, ``openai.responses`` / ``openai.chat``).
    """


# =============================================================================================
# conventions
# =============================================================================================

_MISSING = object()


def _tok(obj: Mapping[str, Any], key: str, where: str = "") -> int | None:
    """An int token count; None when absent or null; anything else malformed raises."""
    value = obj.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    if type(value) is not int or value < 0 or value > MAX_TOKENS:
        raise BadUsageError(f"bad_usage: {where}{key}")
    return value


def _sub(obj: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BadUsageError(f"bad_usage: {key}")
    return value


def _reasoning(reasoning: int | None, output: int, notes: list[str]) -> int | None:
    """The informational reasoning subset; dropped (``dq.sum_check_failed``) above output."""
    if reasoning is not None and reasoning > output:
        notes.append("dq.sum_check_failed")
        return None
    return reasoning


def _buckets(**kw: Any) -> UsageBuckets:
    try:
        return UsageBuckets(**kw)
    except ContractViolation:  # pragma: no cover - every count was range-checked
        raise BadUsageError("bad_usage: usage") from None


def _openai_inclusive(total_in: int, cached: int, write: int, output: int,
                      reasoning: int | None, total: int | None,
                      input_key: str) -> tuple[UsageBuckets, list[str]]:
    notes: list[str] = []
    uncached = total_in - cached - write
    if uncached < 0:
        raise SumCheckError(f"bad_usage: sum_check {input_key}")
    buckets = _buckets(
        uncached_input=uncached, cache_read=cached, cache_write_other=write,
        cache_write_other_ttl_s=OPENAI_CACHE_WRITE_TTL_S if write else None,
        output=output, output_reasoning=_reasoning(reasoning, output, notes))
    if total is not None:
        notes.extend(sum_check(buckets, max(total - output, -1), None))
    return buckets, list(dict.fromkeys(notes))


def normalize_openai_responses(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``openai.responses``: ``usage`` of a Responses object (§5.2).

    uncached = ``input_tokens − cached_tokens − cache_write_tokens`` (negative →
    :class:`SumCheckError`); read = ``cached_tokens``; ``cache_write_other`` =
    ``cache_write_tokens`` with TTL 1,800 s; output = ``output_tokens`` (includes reasoning);
    ``output_reasoning`` = ``output_tokens_details.reasoning_tokens``; ``total_tokens`` (when
    present) is sum-checked.
    """
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    details = _sub(raw, "input_tokens_details") or {}
    odetails = _sub(raw, "output_tokens_details") or {}
    output = _tok(raw, "output_tokens") or 0
    return _openai_inclusive(
        _tok(raw, "input_tokens") or 0,
        _tok(details, "cached_tokens", "input_tokens_details.") or 0,
        _tok(details, "cache_write_tokens", "input_tokens_details.") or 0,
        output,
        _tok(odetails, "reasoning_tokens", "output_tokens_details."),
        _tok(raw, "total_tokens"),
        "input_tokens")


def normalize_openai_chat(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``openai.chat``: ``usage`` of a Chat Completions object (§5.2).

    ``prompt_tokens`` includes ``prompt_tokens_details.cached_tokens`` (and ``cache_write_tokens``
    on GPT-5.6+; 0 before); ``completion_tokens`` includes reasoning and rejected prediction tokens
    (both billed as output); ``completion_tokens_details.reasoning_tokens`` is the subset.
    """
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    details = _sub(raw, "prompt_tokens_details") or {}
    cdetails = _sub(raw, "completion_tokens_details") or {}
    for key in ("accepted_prediction_tokens", "rejected_prediction_tokens"):
        _tok(cdetails, key, "completion_tokens_details.")  # validated; already inside output
    output = _tok(raw, "completion_tokens") or 0
    return _openai_inclusive(
        _tok(raw, "prompt_tokens") or 0,
        _tok(details, "cached_tokens", "prompt_tokens_details.") or 0,
        _tok(details, "cache_write_tokens", "prompt_tokens_details.") or 0,
        output,
        _tok(cdetails, "reasoning_tokens", "completion_tokens_details."),
        _tok(raw, "total_tokens"),
        "prompt_tokens")


def normalize_bedrock_converse(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``bedrock.converse``: a Converse ``TokenUsage`` object (§5.2).

    uncached = ``inputTokens`` (excludes cache); read = ``cacheReadInputTokens``; writes split by
    ``cacheDetails[].ttl`` (``5m`` / ``1h``); ``cacheWriteInputTokens − Σ cacheDetails`` → unknown
    (``dq.ttl_split_residual``; no details at all: ``dq.no_ttl_split``). Σ cacheDetails above
    ``cacheWriteInputTokens`` fails the sum check (``dq.sum_check_failed``) and the split is
    trusted; a detail with another TTL counts as unknown (``dq.unknown_fields``).
    """
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    notes: list[str] = []
    uncached = _tok(raw, "inputTokens") or 0
    output = _tok(raw, "outputTokens") or 0
    read = _tok(raw, "cacheReadInputTokens") or 0
    write = _tok(raw, "cacheWriteInputTokens")
    details = raw.get("cacheDetails")
    w5 = w1 = odd = 0
    if details is not None:
        if not isinstance(details, list):
            raise BadUsageError("bad_usage: cacheDetails")
        for item in details:
            if not isinstance(item, Mapping):
                raise BadUsageError("bad_usage: cacheDetails")
            n = _tok(item, "inputTokens", "cacheDetails.") or 0
            ttl = item.get("ttl")
            if ttl == "5m":
                w5 += n
            elif ttl == "1h":
                w1 += n
            else:
                odd += n
                notes.append("dq.unknown_fields")
    unknown = odd
    if write is not None:
        residual = write - w5 - w1 - odd
        if residual > 0:
            unknown += residual
            notes.append("dq.ttl_split_residual" if details else "dq.no_ttl_split")
        elif residual < 0:
            notes.append("dq.sum_check_failed")
    if w5 + w1 + unknown > MAX_TOKENS:
        raise BadUsageError("bad_usage: cacheDetails")
    return _buckets(uncached_input=uncached, cache_read=read, cache_write_5m=w5,
                    cache_write_1h=w1, cache_write_unknown=unknown,
                    output=output), list(dict.fromkeys(notes))


def _inclusive_attrs(raw: Mapping[str, object], *, input_keys: Sequence[str],
                     output_keys: Sequence[str], read_key: str, write_keys: Sequence[str],
                     reasoning_key: str, total_key: str | None = None,
                     ) -> tuple[UsageBuckets, list[str]]:
    """Inclusive span conventions: input includes read and write; read + write > input means the
    instrumentation reported exclusive counts (treated exclusive, ``dq.convention_mismatch``)."""
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    notes: list[str] = []

    def first(keys: Sequence[str]) -> int | None:
        for key in keys:
            value = _tok(raw, key)
            if value is not None:
                return value
        return None

    total_in = first(input_keys) or 0
    output = first(output_keys) or 0
    read = _tok(raw, read_key) or 0
    write = first(write_keys) or 0
    if read + write > total_in:
        uncached = total_in
        notes.append("dq.convention_mismatch")
    else:
        uncached = total_in - read - write
    if uncached + read + write > MAX_TOKENS:
        raise BadUsageError("bad_usage: usage")
    buckets = _buckets(uncached_input=uncached, cache_read=read, cache_write_unknown=write,
                       output=output,
                       output_reasoning=_reasoning(_tok(raw, reasoning_key), output, notes))
    if total_key is not None:
        total = _tok(raw, total_key)
        if total is not None:
            notes.extend(sum_check(buckets, max(total - output, -1), None))
    return buckets, list(dict.fromkeys(notes))


#: Attribute names of the span/event conventions (§19.4); the adapters build the raw usage mapping
#: from exactly these keys.
GENAI_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
              "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_write.input_tokens",
              "gen_ai.usage.reasoning.output_tokens")
GENAI_LEGACY_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens",
                     "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens",
                     "gen_ai.usage.cache_read.input_tokens",
                     "gen_ai.usage.cache_creation.input_tokens",
                     "gen_ai.usage.reasoning.output_tokens")
OPENINFERENCE_KEYS = ("llm.token_count.prompt", "llm.token_count.completion",
                      "llm.token_count.total", "llm.token_count.prompt_details.cache_read",
                      "llm.token_count.prompt_details.cache_write",
                      "llm.token_count.completion_details.reasoning")
CLAUDE_CODE_OTEL_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens",
                         "cache_creation_tokens")


def normalize_otel_genai(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``otel.genai`` (semconv ≥ 1.42): writes have no TTL split (``cache_write_unknown``)."""
    return _inclusive_attrs(raw, input_keys=("gen_ai.usage.input_tokens",),
                            output_keys=("gen_ai.usage.output_tokens",),
                            read_key="gen_ai.usage.cache_read.input_tokens",
                            write_keys=("gen_ai.usage.cache_write.input_tokens",),
                            reasoning_key="gen_ai.usage.reasoning.output_tokens")


def normalize_otel_genai_legacy(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``otel.genai.legacy``: ``gen_ai.usage.cache_creation.input_tokens`` (semconv 1.40–1.41);
    the deprecated ``prompt_tokens`` / ``completion_tokens`` names are accepted as fallbacks."""
    return _inclusive_attrs(
        raw, input_keys=("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"),
        output_keys=("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"),
        read_key="gen_ai.usage.cache_read.input_tokens",
        write_keys=("gen_ai.usage.cache_creation.input_tokens",
                    "gen_ai.usage.cache_write.input_tokens"),
        reasoning_key="gen_ai.usage.reasoning.output_tokens")


def normalize_openinference(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``openinference`` (LLM-kind spans): like ``otel.genai``; ``llm.token_count.total −
    completion`` must equal the buckets' total input (``dq.sum_check_failed`` otherwise: the
    CrewAI-style defect, 17 prompt tokens reported for a call whose counts imply 17,119)."""
    return _inclusive_attrs(raw, input_keys=("llm.token_count.prompt",),
                            output_keys=("llm.token_count.completion",),
                            read_key="llm.token_count.prompt_details.cache_read",
                            write_keys=("llm.token_count.prompt_details.cache_write",),
                            reasoning_key="llm.token_count.completion_details.reasoning",
                            total_key="llm.token_count.total")


def normalize_claude_code_otel(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``claude_code.otel``: exclusive ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    ``cache_creation_tokens`` (no TTL split: writes → ``cache_write_unknown``, ``dq.no_ttl_split``).
    ``cost_usd`` is a client-side list estimate and never part of the usage."""
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    write = _tok(raw, "cache_creation_tokens") or 0
    notes = ["dq.no_ttl_split"] if write else []
    return _buckets(uncached_input=_tok(raw, "input_tokens") or 0,
                    cache_read=_tok(raw, "cache_read_tokens") or 0,
                    cache_write_unknown=write,
                    output=_tok(raw, "output_tokens") or 0), notes


_CONVENTIONS: tuple[tuple[Convention, Callable[[Mapping[str, object]],
                                                tuple[UsageBuckets, list[str]]]], ...] = (
    (Convention(BEDROCK_CONVERSE, "aws", False, True,
                "inputTokens excludes cache reads and writes; cacheDetails splits writes by TTL "
                "(5m/1h), the remainder is cache_write_unknown (SPEC §5.2)"),
     normalize_bedrock_converse),
    (Convention(OPENAI_RESPONSES, "openai", True, True,
                "input_tokens includes cached_tokens and cache_write_tokens; writes are one class "
                "with a 30m TTL; output_tokens includes reasoning (SPEC §5.2)"),
     normalize_openai_responses),
    (Convention(OPENAI_CHAT, "openai", True, True,
                "prompt_tokens includes cached_tokens (and cache_write_tokens on 5.6+); "
                "completion_tokens includes reasoning and rejected predictions (SPEC §5.2)"),
     normalize_openai_chat),
    (Convention(OTEL_GENAI, "otel", True, True,
                "gen_ai.usage.input_tokens includes cache_read and cache_write; read + write > "
                "input is treated as exclusive with dq.convention_mismatch (SPEC §5.2)"),
     normalize_otel_genai),
    (Convention(OTEL_GENAI_LEGACY, "otel", True, True,
                "otel.genai with gen_ai.usage.cache_creation.input_tokens (semconv 1.40-1.41)"),
     normalize_otel_genai_legacy),
    (Convention(OPENINFERENCE, "openinference", True, True,
                "llm.token_count.prompt includes prompt_details.cache_read/cache_write; LLM-kind "
                "spans only; llm.token_count.total is sum-checked (SPEC §5.2)"),
     normalize_openinference),
    (Convention(CLAUDE_CODE_OTEL, "anthropic", False, True,
                "claude_code.api_request: exclusive counts without a TTL split; cost_usd is a "
                "provider estimate, never billed (SPEC §5.2)"),
     normalize_claude_code_otel),
)

for _conv, _fn in _CONVENTIONS:
    register_convention(_conv, _fn)


# =============================================================================================
# small parsers shared by the adapters
# =============================================================================================

_ISO_RE = re.compile(
    r"\s*(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,12}))?)?)?"
    r"\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?\s*\Z")
_LABEL_RE = re.compile(r"[A-Za-z0-9_.:/@+=-]{1,64}\Z")
_ENUM_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_DIGITS_RE = re.compile(r"\s*[+-]?\d{1,30}\s*\Z")


def parse_iso_ms(value: object) -> int | None:
    """An ISO 8601 timestamp (``Z`` or offset; any fraction digits) → epoch ms, else None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    m = _ISO_RE.match(value)
    if m is None:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59
            and second <= 60 and year >= 1970):
        return None
    frac = (m.group(7) or "0")[:3].ljust(3, "0")
    ms = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) * 1000 + int(frac)
    tz = m.group(8)
    if tz and tz not in ("Z", "z"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        offset_min = int(digits[:2]) * 60 + (int(digits[2:4]) if len(digits) >= 4 else 0)
        ms -= sign * offset_min * 60_000
    return ms if 0 <= ms <= MAX_TOKENS else None


def to_int(value: object) -> int | None:
    """A non-negative integer from an int, a decimal digit string (OTLP int64) or an integral finite
    float; None for anything else (bools, fractions, negatives, NaN)."""
    if type(value) is int:
        return value if 0 <= value <= MAX_TOKENS else None
    if isinstance(value, str) and _DIGITS_RE.match(value):
        n = int(value)
        return n if 0 <= n <= MAX_TOKENS else None
    if type(value) is float and math.isfinite(value) and value.is_integer():
        n = int(value)
        return n if 0 <= n <= MAX_TOKENS else None
    return None


def usd_to_nano(value: object) -> int | None:
    """A provider-reported USD estimate (decimal string, int, or a JSON number already parsed as a
    float, converted through its shortest ``repr``) → int nano-USD, half-even; None when missing,
    negative, non-finite or out of range. Used for provider estimates only (never billed)."""
    if type(value) is bool or value is None:
        return None
    try:
        if isinstance(value, str):
            if len(value) > 64:
                return None
            amount = Decimal(value.strip())
        elif type(value) is int:
            amount = Decimal(value)
        elif type(value) is float:
            if not math.isfinite(value):
                return None
            amount = Decimal(repr(value))
        else:
            return None
        if not amount.is_finite() or amount < 0:
            return None
        return decimal_to_nano(amount)
    except (InvalidOperation, ValueError):
        return None


def clean_label(value: object, *, enum: bool = False) -> str | None:
    """A short provider label (ids, enum values, model names) or None when it is not one: at most
    64 characters from a safe alphabet. Never used for free text."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if (_ENUM_RE if enum else _LABEL_RE).match(value) else None


def canonical_usage_json(raw: Mapping[str, object]) -> str | None:
    """Canonical JSON (sorted keys, compact, ASCII) of a provider usage object keeping only
    numbers, booleans, nulls and short enum-like strings (≤ 64 chars); None beyond 8 KiB."""

    def clean(value: Any, depth: int) -> Any:
        if depth > 8:
            return _MISSING
        if value is None or type(value) is bool or type(value) is int:
            return value
        if isinstance(value, str):
            return value if _ENUM_RE.match(value) else _MISSING
        if isinstance(value, Mapping):
            out = {}
            for k, v in value.items():
                if isinstance(k, str) and len(k) <= 96:
                    cv = clean(v, depth + 1)
                    if cv is not _MISSING:
                        out[k] = cv
            return out
        if isinstance(value, list):
            return [cv for cv in (clean(v, depth + 1) for v in value) if cv is not _MISSING]
        return _MISSING

    cleaned = clean(raw, 0)
    if not isinstance(cleaned, dict):
        return None
    text = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text if len(text) <= 8 * 1024 else None


# =============================================================================================
# identity, names, channels
# =============================================================================================

#: Channel implied by a Claude Code billing path (§5.3 step 5); anything else is the Claude API.
CHANNEL_BY_BILLING_PATH: Mapping[str, str] = {
    "bedrock": "bedrock", "vertex": "vertex", "foundry": "foundry",
    "claude_platform_aws": "claude_platform_aws", "openai": "openai_api",
    "azure_openai": "azure_openai",
}
#: Billing path implied by a channel (the channel alone does not tell API key from subscription).
BILLING_PATH_BY_CHANNEL: Mapping[str, str] = {
    "bedrock": "bedrock", "vertex": "vertex", "foundry": "foundry",
    "claude_platform_aws": "claude_platform_aws", "openai_api": "openai",
    "azure_openai": "azure_openai",
}
#: Cache isolation domain per channel (records.Lane.cache_scope_key; D43 for Azure).
SCOPE_PREFIX_BY_CHANNEL: Mapping[str, str] = {
    "anthropic_api": "ws", "claude_platform_aws": "ws", "foundry": "ws", "bedrock": "org",
    "vertex": "org", "openai_api": "org", "azure_openai": "sub",
}
#: Claude Code cache-write TTL defaults by billing path (§19.5): (main lanes, other lanes).
TTL_HINT_BY_BILLING_PATH: Mapping[str, tuple[str, str]] = {
    "api_key": ("5m", "5m"), "usage_credits": ("5m", "5m"), "bedrock": ("5m", "5m"),
    "vertex": ("5m", "5m"), "foundry": ("5m", "5m"), "claude_platform_aws": ("5m", "5m"),
    "subscription": ("1h", "5m"),
}


def cache_scope(channel: str, account: str | None) -> str:
    """``ws:<id>`` (1P, Claude Platform on AWS, Foundry), ``org:<channel>:<account>`` (Bedrock,
    Vertex, OpenAI), ``sub:<id>`` (Azure OpenAI), ``unknown`` for unknown channels."""
    prefix = SCOPE_PREFIX_BY_CHANNEL.get(channel)
    if prefix is None:
        return "unknown"
    acct = account or "unknown"
    return f"org:{channel}:{acct}" if prefix == "org" else f"{prefix}:{acct}"


def team_for(opts: IngestOptions, candidates: Iterable[str | None]) -> str | None:
    """The team of the first raw actor found in ``opts.team_map`` (the raw value is never kept)."""
    if not opts.team_map:
        return None
    mapping = dict(opts.team_map)
    for raw in candidates:
        if raw and raw in mapping:
            return mapping[raw]
    return None


def check_identity_mode(opts: IngestOptions) -> None:
    """Refuse unknown identity modes and invalid collector refs up front (``UsageError``)."""
    mode = opts.identity_mode
    if mode not in ("install", "central", "two-stage", "central-ingest"):
        raise UsageError("identity_mode must be install, central, two-stage or central-ingest")
    if mode == "central" and opts.principal_ref is not None \
            and not is_opaque_ref(opts.principal_ref):
        raise UsageError("principal_ref must be an opaque ref ([A-Za-z0-9._-]{1,64}, no '@')")


def principal_for(opts: IngestOptions, raw: str | None) -> str | None:
    """The principal of a record (§5.1 identity): ``central`` → ``r_<principal_ref>``;
    ``two-stage`` → ``c_`` HMAC of the ref under the principal key; ``install`` /
    ``central-ingest`` → ``p_`` HMAC of the raw central identity (email, account uuid, IAM ARN)
    under the principal key. The raw value is dropped by the caller immediately."""
    mode = opts.identity_mode
    if mode == "central":
        return f"r_{opts.principal_ref}" if opts.principal_ref else None
    if mode == "two-stage":
        if opts.principal_ref and opts.principal_key is not None:
            return pseudonym(opts.principal_key, "c", opts.principal_ref)
        return None
    if not raw or opts.principal_key is None:
        return None
    return pseudonym(opts.principal_key, "p", raw)


def member(value: object, allowed: Collection[str]) -> bool:
    """``value in allowed`` for string values only (source values may be unhashable lists)."""
    return isinstance(value, str) and value in allowed


def normalize_identity(value: object) -> str | None:
    """A raw central identity prepared for team lookup and pseudonymization: stripped, emails
    lower-cased (so one person has one ``p_`` across sources); None when empty or over-long."""
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        return None
    value = value.strip()
    return value.lower() if "@" in value else value


def name_or_hash(opts: IngestOptions, value: object,
                 builtin: frozenset[str] = frozenset()) -> str | None:
    """A tool / skill / MCP / plugin / repo name in clear when built in or allowlisted, else its
    ``h_`` HMAC under the name key; None for non-strings and empty values."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if (value in builtin or value in opts.name_allowlist) and len(value) <= 64:
        return value
    return pseudonym(opts.name_key, "h", value)


# =============================================================================================
# per-file bookkeeping
# =============================================================================================

_SEVERITY = {
    "dq.sum_check_failed": "warn", "dq.convention_mismatch": "warn", "dq.quarantined": "warn",
    "dq.ttl_split_exceeds_total": "warn", "dq.iterations_mismatch": "warn",
    "dq.unpriced_model": "warn", "dq.request_id_collision": "warn",
}
_DETAILS = {
    "dq.sum_check_failed": "usage counts disagree with a provider-reported total",
    "dq.convention_mismatch": "cache read + write exceeded input; counts treated as exclusive",
    "dq.no_ttl_split": "cache writes without a 5m/1h split priced as cache_write_unknown",
    "dq.raw_bodies_ignored": "raw API body events ignored (never parsed)",
    "dq.lanes_inferred": "requests placed on heuristic lanes (no agent ids)",
    "dq.quarantined": "records quarantined (see quarantine reasons)",
    "dq.ttl_split_residual": "cache writes beyond the reported TTL split priced as unknown TTL",
    "dq.ttl_split_exceeds_total": "reported TTL split exceeded the write total; split trusted",
    "dq.unknown_fields": "fields outside the allowlist dropped",
    "dq.unpriced_model": "model unknown; usage left unpriced",
    "dq.iterations_mismatch": "usage.iterations disagree with the top-level usage",
    "dq.request_id_collision": "one provider request id seen with two message ids",
}


class SourceScan:
    """Per-file bookkeeping: source identity, quarantine, data-quality counters and statistics."""

    def __init__(self, adapter: str, path: Path, opts: IngestOptions) -> None:
        check_identity_mode(opts)
        self.adapter = adapter
        self.path = Path(path)
        self.opts = opts
        try:
            where = os.fspath(self.path.resolve())
        except (OSError, RuntimeError):  # pragma: no cover - resolve() of odd paths
            where = os.fspath(self.path)
        self.source_id = pseudonym(opts.name_key, "s", where)
        self.quarantined: list[QuarantineItem] = []
        self.stats: dict[str, int] = {}
        self._notes: dict[str, list[int]] = {}
        self.principal_used = False

    # ---------- counters ----------
    def count(self, key: str, n: int = 1) -> None:
        """Add *n* to statistic *key*."""
        self.stats[key] = self.stats.get(key, 0) + n

    def note(self, code: str, count: int = 1, tokens: int | None = None) -> None:
        """Record a data-quality code (aggregated per code: count and token magnitude)."""
        entry = self._notes.setdefault(code, [0, 0, 0])
        entry[0] += count
        if tokens is not None:
            entry[1] += tokens
            entry[2] = 1

    def notes(self, codes: Iterable[str], tokens: int | None = None) -> None:
        """Record every code of *codes* once."""
        for code in codes:
            self.note(code, tokens=tokens)

    def quarantine(self, locator: str, reason: str) -> None:
        """Quarantine one record (lenient) or raise ``SourceError`` naming file and locator."""
        if not self.opts.lenient:
            raise SourceError(f"{self.path.name}: {locator}: {reason}")
        self.quarantined.append(QuarantineItem(source_id=self.source_id, locator=locator,
                                               reason=reason))
        self.count("quarantined")

    def principal(self, raw: str | None) -> str | None:
        """:func:`principal_for` that remembers whether a key-bound pseudonym was emitted."""
        value = principal_for(self.opts, raw)
        if value is not None and value[:2] in ("p_", "c_"):
            self.principal_used = True
        return value

    def in_window(self, ts_ms: int) -> bool:
        """``opts.since_ms <= ts_ms < opts.until_ms`` (each bound optional)."""
        since, until = self.opts.since_ms, self.opts.until_ms
        return (since is None or ts_ms >= since) and (until is None or ts_ms < until)

    # ---------- reading ----------
    def records(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """``(line_no, object)`` for every JSON-object line; bad lines are quarantined
        (``bad_json``, ``not_object``, ``oversize_line``). A ``.zst`` file without
        ``compression.zstd`` raises ``SourceError`` naming the fix (``compression: none``)."""
        try:
            for line_no, _offset, raw in iter_lines(self.path):
                self.count("lines")
                if not raw:
                    self.quarantine(f"line:{line_no}", "oversize_line")
                    continue
                obj = parse_json_line(raw)
                if obj is None:
                    reason = "not_object" if _is_json(raw) else "bad_json"
                    self.quarantine(f"line:{line_no}", reason)
                    continue
                yield line_no, obj
        except SourceError as exc:
            if ZSTD_MESSAGE in str(exc):
                raise SourceError(
                    f"{self.path.name}: {ZSTD_MESSAGE} (collector fileexporter "
                    "`compression: none`); dq.zstd_unavailable") from None
            raise

    def _file_digest(self) -> tuple[str, int]:
        h = hashlib.sha256()
        size = 0
        try:
            with open(self.path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise SourceError(f"{self.path.name}: unreadable ({type(exc).__name__})") from None
        return h.hexdigest(), size

    # ---------- result ----------
    def finish(self, *, requests: list[Request], sessions: list[Session],
               events: list[LaneEvent], aggregates: Sequence[UsageAggregate] = (),
               capabilities: Iterable[str] = ()) -> IngestResult:
        """The :class:`IngestResult` (notes sorted by code; ``dq.quarantined`` added)."""
        if self.quarantined:
            self.note("dq.quarantined", len(self.quarantined) - self._notes.get(
                "dq.quarantined", [0])[0])
        sha, size = self._file_digest()
        source = SourceInfo(
            source_id=self.source_id, adapter=self.adapter,
            name_hmac=pseudonym(self.opts.name_key, "h", self.path.name), sha256=sha,
            bytes=size, name_key_id=self.opts.name_key_id,
            principal_key_id=self.opts.principal_key_id if self.principal_used else None)
        notes = [DataQualityNote(code=code, severity=_SEVERITY.get(code, "info"), count=c,
                                 detail=_DETAILS.get(code, code), tokens=t if has_t else None)
                 for code, (c, t, has_t) in sorted(self._notes.items()) if c > 0]
        self.count("requests", 0)
        self.count("records", 0)
        stats = dict(sorted(self.stats.items()))
        stats["requests"] = len(requests)
        stats["events"] = len(events)
        stats["aggregates"] = len(aggregates)
        return IngestResult(source=source, requests=requests, sessions=sessions, events=events,
                            aggregates=list(aggregates), cost_lines=[], outcomes=[],
                            quarantined=list(self.quarantined), notes=notes, stats=stats,
                            capabilities=frozenset(capabilities))


def _is_json(raw: bytes) -> bool:
    try:
        json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return True


# =============================================================================================
# draft requests and lane assembly
# =============================================================================================

@dataclass
class LaneShell:
    """A lane the adapter knows about (kind, parent, cache scope, exactness)."""

    lane_key: str
    session_key: str
    kind: LaneKind
    parent_lane_key: str | None
    cache_scope_key: str
    lane_exact: bool
    source_kind: str


@dataclass
class Draft:
    """A request under construction (``seq`` is assigned per lane by :func:`assemble`)."""

    request_id: str
    session_key: str
    lane_key: str
    ts_ms: int
    order: tuple[Any, ...]
    attribution: Attribution
    params: RequestParams
    attempts: list[Attempt]
    source: SourceRef
    appended: list[AppendedItem] = field(default_factory=list)
    end_ms: int = 0


def assemble(drafts: Iterable[Draft], shells: Mapping[str, LaneShell],
             opts: IngestOptions) -> tuple[list[Request], list[Session]]:
    """Sort drafts per lane by ``(ts, order, request id)``, assign ``seq`` and build the frozen
    requests plus one :class:`Session` (with request-less lane shells) per session key."""
    by_lane: dict[str, list[Draft]] = {}
    for d in drafts:
        by_lane.setdefault(d.lane_key, []).append(d)
    requests: list[Request] = []
    first_attr: dict[str, tuple[tuple[Any, ...], Attribution]] = {}
    span: dict[str, list[int]] = {}
    for lane_key in sorted(by_lane):
        lane_drafts = sorted(by_lane[lane_key], key=lambda d: (d.ts_ms, d.order, d.request_id))
        for seq, d in enumerate(lane_drafts):
            requests.append(Request(
                request_id=d.request_id, session_key=d.session_key, lane_key=d.lane_key, seq=seq,
                attribution=d.attribution, params=d.params, attempts=tuple(d.attempts),
                appended=tuple(d.appended), source=d.source))
            key = (d.ts_ms, d.order, d.request_id)
            if d.session_key not in first_attr or key < first_attr[d.session_key][0]:
                first_attr[d.session_key] = (key, d.attribution)
            window = span.setdefault(d.session_key, [d.ts_ms, d.ts_ms])
            window[0] = min(window[0], d.ts_ms)
            window[1] = max(window[1], d.ts_ms, d.end_ms)
    requests.sort(key=lambda r: (r.session_key, r.lane_key, r.seq))
    lanes_by_session: dict[str, list[LaneShell]] = {}
    for shell in shells.values():
        lanes_by_session.setdefault(shell.session_key, []).append(shell)
    sessions: list[Session] = []
    for session_key in sorted(lanes_by_session):
        lane_shells = sorted(lanes_by_session[session_key], key=lambda s: s.lane_key)
        lanes = tuple(Lane(lane_key=s.lane_key, session_key=s.session_key, kind=s.kind,
                           parent_lane_key=s.parent_lane_key, cache_scope_key=s.cache_scope_key,
                           requests=(), lane_exact=s.lane_exact) for s in lane_shells)
        started, ended = span.get(session_key, [0, 0])
        attribution = first_attr[session_key][1] if session_key in first_attr \
            else opts.attribution
        sessions.append(Session(session_key=session_key, source_kind=lane_shells[0].source_kind,
                                attribution=attribution, lanes=lanes, started_ms=started,
                                ended_ms=ended))
    return requests, sessions


def lane_capabilities(requests: Sequence[Request], shells: Mapping[str, LaneShell],
                      extra: Iterable[str] = ()) -> set[str]:
    """Capabilities every request-producing TELEM adapter derives the same way."""
    caps: set[str] = set(extra)
    if not requests:
        return caps
    caps.update({"usage_sequence", "timing"})
    lanes = {r.lane_key for r in requests}
    if all(shells[k].lane_exact for k in lanes if k in shells):
        caps.add("lanes_exact")
    if any(r.attribution.team for r in requests):
        caps.add("attribution.team")
    if any(r.attribution.workload_class is not WorkloadClass.UNKNOWN for r in requests):
        caps.add("workload")
    if any(len(r.attempts) > 1 or any(a.outcome.value != "ok" for a in r.attempts)
           for r in requests):
        caps.add("attempts")
    if any(r.params.effort or r.params.max_tokens is not None or r.params.speed
           for r in requests):
        caps.add("params")
    if any(a.diagnostics is not None for r in requests for a in r.attempts):
        caps.add("diagnostics")
    if any(a.ttft_ms is not None for r in requests for a in r.attempts):
        caps.add("ttft")
    if any(r.appended for r in requests):
        caps.add("appended")
    return caps


# =============================================================================================
# request_meta (gateway pairs) and attribution overrides
# =============================================================================================

_CLEAR_ATTR = ("team", "cost_center", "project", "workspace_id", "agent_product", "agent_type",
               "entrypoint", "client_version", "arm", "wave")
_NAMED_ATTR = ("skill", "mcp_server", "plugin")
_HASHED_ATTR = ("repo", "api_key_id")


def attribution_from(opts: IngestOptions, scan: SourceScan, meta: Mapping[str, Any] | None,
                     *, base: Attribution | None = None, raw_principal: str | None = None,
                     team: str | None = None) -> Attribution:
    """``opts.attribution`` overridden by an allowlisted attribution mapping (request_meta /
    requestMetadata), then by *team* (from ``opts.team_map``): clear short labels, names hashed
    unless allowlisted, repos and API keys hashed, ``principal`` pseudonymized; unknown keys and
    over-long values are dropped (``dq.unknown_fields``)."""
    attr = base if base is not None else opts.attribution
    updates: dict[str, Any] = {}
    extra = dict(attr.extra)
    principal_raw = raw_principal
    if meta:
        for key, value in meta.items():
            if key == "principal" and normalize_identity(value) is not None:
                principal_raw = normalize_identity(value)
            elif key in _CLEAR_ATTR and clean_label(value) is not None:
                updates[key] = clean_label(value)
            elif key in _NAMED_ATTR and isinstance(value, str) and value.strip():
                updates[key] = name_or_hash(opts, value)
            elif key in _HASHED_ATTR and isinstance(value, str) and value.strip():
                updates[key] = pseudonym(opts.name_key, "h", value.strip())
            elif key == "workload_class" and member(value, WorkloadClass._value2member_map_):
                updates[key] = WorkloadClass(value)
            elif key == "billing_path" and value in BILLING_PATHS:
                updates[key] = value
            elif key in EXTRA_KEYS and clean_label(value) is not None:
                extra[key] = clean_label(value)
            elif key == "extra" and isinstance(value, Mapping):
                for k, v in value.items():
                    if k in EXTRA_KEYS and clean_label(v) is not None:
                        extra[k] = clean_label(v)
                    else:
                        scan.note("dq.unknown_fields")
            else:
                scan.note("dq.unknown_fields")
    if team:  # the admin's team map outranks caller-supplied metadata
        updates["team"] = team
    principal = scan.principal(principal_raw)
    if principal is not None:
        updates["principal"] = principal
    if extra != dict(attr.extra):
        updates["extra"] = tuple(sorted(extra.items()))
    return dataclasses.replace(attr, **updates) if updates else attr


def key_part(value: object) -> str | None:
    """A caller-supplied lane/session name reduced to a hashable key part (never stored raw)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip() and len(value) <= 512:
        return value.strip()
    return None


def meta_ts(meta: Mapping[str, Any] | None) -> int | None:
    """``request_meta.ts_ms`` as int epoch ms (also accepts an ISO 8601 ``ts``)."""
    if not meta:
        return None
    ts = to_int(meta.get("ts_ms"))
    if ts is None:
        ts = parse_iso_ms(meta.get("ts"))
    return ts


def guarded(scan: SourceScan, locator: str, fn: Callable[[], None]) -> None:
    """Run one record's processing; any malformed-input failure quarantines that record.

    ``bad_usage`` (and ``dq.sum_check_failed`` for :class:`SumCheckError`) for usage errors,
    ``bad_type:record`` for structurally unexpected values. A strict-mode ``SourceError``
    propagates.
    """
    try:
        fn()
    except SourceError:
        raise
    except SumCheckError:
        scan.note("dq.sum_check_failed")
        scan.quarantine(locator, "bad_usage")
    except BadUsageError:
        scan.quarantine(locator, "bad_usage")
    except (TokenbillError, ValueError, TypeError, KeyError, AttributeError, OverflowError,
            RecursionError, IndexError):
        scan.quarantine(locator, "bad_type:record")


def source_ref(scan: SourceScan, locator: str, fidelity: Fidelity, priority: int) -> SourceRef:
    """The :class:`SourceRef` of one record."""
    return SourceRef(adapter=scan.adapter, source_id=scan.source_id, locator=locator,
                     fidelity=fidelity, priority=priority)


def attempt_id(request_id: str, attempt_no: int) -> str:
    """Stable attempt id within a request."""
    return stable_id("att", request_id, attempt_no)


#: ``Attempt.error_type`` from an HTTP status (§3.2 vocabulary; anything else is ``other``).
ERROR_TYPE_BY_STATUS: Mapping[int, str] = {
    400: "invalid_request", 401: "auth", 403: "auth", 404: "invalid_request", 408: "timeout",
    413: "prompt_too_long", 429: "rate_limit", 503: "overloaded", 504: "timeout",
    529: "overloaded",
}


def error_type_for(status: int | None) -> str:
    """``connection`` without a status, else :data:`ERROR_TYPE_BY_STATUS` or ``other``."""
    if status is None:
        return "connection"
    return ERROR_TYPE_BY_STATUS.get(status, "other")


_HEAD_RE = re.compile(rb"\A\s*\{")


def head_record(head: bytes) -> tuple[dict[str, Any] | None, bytes]:
    """The first non-blank line of a sniffed head parsed as a JSON object (None when it is not one
    or is truncated) and the raw bytes of that line (for key-prefix checks of huge lines)."""
    for line in head.split(b"\n"):
        if line.strip():
            return parse_json_line(line.rstrip(b"\r")), line if _HEAD_RE.match(line) else b""
    return None, b""

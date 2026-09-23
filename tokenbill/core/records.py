"""Canonical ledger records (SPEC §3.2).

Every record is a frozen, slotted dataclass validated in ``__post_init__``: token counts are ``int``
in ``[0, 2**53]``, enum-typed fields are coerced to their enum (a plain value string is accepted),
list-typed inputs are converted to tuples, and pair tuples that the SPEC declares "sorted" are
sorted. Any violation raises :class:`~tokenbill.core.errors.ContractViolation` with a content-free
message.

``to_json`` / ``from_json`` give a lossless JSON round trip for every record (and every dataclass of
``core.types``): enums by value, tuples as lists, ``Decimal`` as a decimal string, frozensets as
sorted lists.
"""

from __future__ import annotations

import dataclasses
import functools
import re
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum, IntEnum
from typing import Any

from tokenbill.core.errors import ContractViolation

__all__ = [
    "BILLING_PATHS",
    "BLOCK_KINDS",
    "DIAG_REASONS",
    "EVENT_ATTRS",
    "EXTRA_KEYS",
    "MAX_TOKENS",
    "AppendedItem",
    "Attempt",
    "Attribution",
    "BlockRef",
    "Breakpoint",
    "CacheDiagnostic",
    "ContentFingerprint",
    "ContentTier",
    "CostLine",
    "Fidelity",
    "Inference",
    "InferenceKind",
    "Lane",
    "LaneEvent",
    "LaneEventKind",
    "LaneKind",
    "Outcome",
    "OutcomeAggregate",
    "PricingContext",
    "Request",
    "RequestParams",
    "Session",
    "SourceRef",
    "TBEnum",
    "UsageAggregate",
    "UsageBuckets",
    "UsageRecord",
    "UsageSource",
    "WorkloadClass",
    "billing_class",
    "from_json",
    "to_json",
]

MAX_TOKENS = 2**53


class TBEnum(str, Enum):
    """``str`` enum whose ``str()`` and ``format()`` are its value on every supported Python.

    Python 3.12 changed ``format()`` of mixed-in ``str`` enums; contract enums must render the same
    on 3.10-3.13 (SPEC §2.4).
    """

    def __str__(self) -> str:
        return self.value

    def __format__(self, spec: str) -> str:
        return format(self.value, spec)


class ContentTier(TBEnum):
    NONE = "none"                # numbers, enums, allowlisted names, HMAC ids only
    FINGERPRINT = "fingerprint"  # + per-block HMAC hashes and byte lengths
    FULL = "full"                # + raw content; local only; export refuses it


class LaneKind(TBEnum):
    MAIN = "main"
    SUBAGENT = "subagent"
    WORKFLOW_AGENT = "workflow_agent"
    HELPER = "helper"
    COMPACTION = "compaction"
    API_RUN = "api_run"
    UNKNOWN = "unknown"


class InferenceKind(TBEnum):
    MESSAGE = "message"                     # ordinary billed inference (iterations type message)
    COMPACTION = "compaction"               # server/client compaction pass
    ADVISOR = "advisor"                     # advisor sub-inference (priced at the advisor model)
    FALLBACK_DECLINED = "fallback_declined" # refused attempt preceding a fallback_message
    FALLBACK = "fallback"                   # iterations[].type "fallback_message"
    KEEPALIVE = "keepalive"                 # counterfactual only (max_tokens 0 cache refresh)
    OUTPUT_RESIDUAL = "output_residual"     # headless/SDK session output not attributable per step
    OTHER = "other"


class UsageSource(TBEnum):
    FINAL = "final"                            # final usage frame / non-streaming response
    MESSAGE_START_ONLY = "message_start_only"  # output is the streaming placeholder (a lower bound)
    PARTIAL_STREAM = "partial_stream"          # aborted: message_start input + streamed output
    ESTIMATED = "estimated"                    # reconstructed (e.g. hidden compaction call)
    PROVIDER_ROLLUP = "provider_rollup"        # aggregate source (never per request)


class Outcome(TBEnum):
    OK = "ok"
    HTTP_ERROR = "http_error"
    ABORTED = "aborted"
    TIMEOUT = "timeout"
    REFUSED = "refused"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


class WorkloadClass(TBEnum):
    INTERACTIVE = "interactive"
    CI = "ci"
    SCHEDULED = "scheduled"
    EVAL = "eval"
    BATCH = "batch"
    SERVICE = "service"
    UNKNOWN = "unknown"


class Fidelity(IntEnum):
    AGGREGATE = 0      # bucket-derived
    ESTIMATED = 1      # placeholder or synthesized usage
    NO_TTL_SPLIT = 2   # final usage without 5m/1h split (Claude Code OTel)
    FULL = 3           # final usage with TTL split and iterations (transcripts, recorder, API)


BILLING_PATHS = ("api_key", "subscription", "usage_credits", "bedrock", "vertex", "foundry",
                 "claude_platform_aws", "openai", "azure_openai", "unknown")


def billing_class(billing_path: str | None) -> str:
    """``"allowance"`` iff *billing_path* is ``"subscription"`` (seat allowance, D26), else
    ``"billed"``."""
    return "allowance" if billing_path == "subscription" else "billed"


#: Allowlisted ``Attribution.extra`` keys; anything else is dropped at the adapter
#: (dq.unknown_fields). ``endpoint_scope``: "global" | "regional" (--attr; Claude Code on Vertex).
EXTRA_KEYS = ("mdm_group", "gateway", "task_id", "workflow", "run_attempt", "department",
              "environment", "endpoint_scope")

#: Canonical ``CacheDiagnostic.reason`` values.
DIAG_REASONS = frozenset({"model_changed", "system_changed", "tools_changed", "messages_changed",
                          "param_changed", "key_changed", "compacted", "previous_message_not_found",
                          "unavailable"})

#: ``BlockRef.kind`` values.
BLOCK_KINDS = frozenset({"tool_def", "system_text", "text", "tool_use", "tool_result", "image",
                         "document", "thinking", "redacted_thinking", "compaction", "other"})

# Basis values (core.labels.Basis) — duplicated here to avoid an import cycle; a test pins equality.
_BASIS_VALUES = frozenset({"list", "contract", "invoice", "provider_estimate", "list_equivalent"})
_TIERS = frozenset({"tools", "system", "messages"})
_APPENDED_KINDS = frozenset({"tool_result", "user_text", "attachment", "image", "assistant"})
_RETRY_LAYERS = frozenset({"sdk", "agent", "gateway"})
_BREAKPOINT_TTLS = frozenset({"5m", "1h", "30m"})
_TTL_HINTS = frozenset({"5m", "1h"})
_ENDPOINT_SCOPES = frozenset({"global", "regional", "multi_region", "unknown"})
_TTL_OBSERVED = frozenset({"5m", "1h", "mixed", "unknown"})
_FINALITIES = frozenset({"provisional", "final"})
_PRINCIPAL_RE = re.compile(r"(?:[pc]_[0-9a-f]{20}|r_[A-Za-z0-9._-]{1,64})\Z")
_STORE_PRINCIPAL_RE = re.compile(r"p_[0-9a-f]{20}\Z")
_HASH_RE = re.compile(r"h_[0-9a-f]{20}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_RAW_USAGE_MAX_BYTES = 8 * 1024
_EXTRA_VALUE_MAX = 128


# ---------------------------------------------------------------------------------------------
# validation helpers (messages are content-free: field names and integers only)
# ---------------------------------------------------------------------------------------------

def _fail(obj: object, name: str, why: str) -> ContractViolation:
    return ContractViolation(f"{type(obj).__name__}.{name}: {why}")


def _count(obj: object, name: str, *, optional: bool = False) -> None:
    v = getattr(obj, name)
    if v is None:
        if optional:
            return
        raise _fail(obj, name, "required")
    if type(v) is not int or v < 0 or v > MAX_TOKENS:
        raise _fail(obj, name, "must be an int in [0, 2**53]")


def _int(obj: object, name: str, *, optional: bool = False) -> None:
    v = getattr(obj, name)
    if v is None and optional:
        return
    if type(v) is not int:
        raise _fail(obj, name, "must be an int")


def _str(obj: object, name: str, *, optional: bool = False) -> None:
    v = getattr(obj, name)
    if v is None and optional:
        return
    if not isinstance(v, str):
        raise _fail(obj, name, "must be a str")


def _bool(obj: object, name: str, *, optional: bool = False) -> None:
    v = getattr(obj, name)
    if v is None and optional:
        return
    if type(v) is not bool:
        raise _fail(obj, name, "must be a bool")


def _one_of(obj: object, name: str, allowed: frozenset[str] | tuple[str, ...], *,
            optional: bool = False) -> None:
    v = getattr(obj, name)
    if v is None and optional:
        return
    if not isinstance(v, str) or v not in allowed:
        raise _fail(obj, name, "not an allowed value")


def _enum(obj: object, name: str, enum_cls: type[Enum]) -> None:
    v = getattr(obj, name)
    if type(v) is enum_cls:
        return
    try:
        coerced = enum_cls(v)
    except (ValueError, TypeError):
        raise _fail(obj, name, f"not a {enum_cls.__name__} value") from None
    object.__setattr__(obj, name, coerced)


def _tuple(obj: object, name: str, item_type: type | None = None) -> tuple:
    v = getattr(obj, name)
    if type(v) is not tuple:
        if isinstance(v, (list, tuple)):
            v = tuple(v)
            object.__setattr__(obj, name, v)
        else:
            raise _fail(obj, name, "must be a tuple")
    if item_type is not None:
        for item in v:
            if not isinstance(item, item_type):
                raise _fail(obj, name, f"items must be {item_type.__name__}")
    return v


def _pairs(obj: object, name: str, *, sort: bool, value_types: tuple[type, ...] = (str,),
           unique: bool = True) -> tuple[tuple[str, Any], ...]:
    raw = getattr(obj, name)
    if not isinstance(raw, (list, tuple)):
        raise _fail(obj, name, "must be a tuple of pairs")
    out = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise _fail(obj, name, "items must be (key, value) pairs")
        k, v = pair
        if not isinstance(k, str):
            raise _fail(obj, name, "keys must be str")
        if not isinstance(v, value_types) or (type(v) is bool and bool not in value_types):
            raise _fail(obj, name, "value of an unsupported type")
        out.append((k, v))
    if sort:
        out.sort(key=lambda kv: kv[0])
    if unique:
        keys = [k for k, _ in out]
        if len(set(keys)) != len(keys):
            raise _fail(obj, name, "duplicate keys")
    result = tuple(out)
    if result != raw or type(raw) is not tuple:
        object.__setattr__(obj, name, result)
    return result


def _date(obj: object, name: str) -> None:
    v = getattr(obj, name)
    if not isinstance(v, str) or not _DATE_RE.match(v):
        raise _fail(obj, name, "must be a YYYY-MM-DD string")


def _instance(obj: object, name: str, cls: type, *, optional: bool = False) -> None:
    v = getattr(obj, name)
    if v is None and optional:
        return
    if not isinstance(v, cls):
        raise _fail(obj, name, f"must be a {cls.__name__}")


# ---------------------------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------------------------

_BUCKET_COUNTS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
                  "cache_write_other", "cache_write_unknown", "output", "web_search_requests",
                  "web_fetch_requests")


@dataclass(frozen=True, slots=True)
class UsageBuckets:
    """Disjoint billed token buckets. total_input = uncached_input + cache_read + all writes."""

    uncached_input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_other: int = 0              # single-class write, known TTL (OpenAI 5.6+: 30m)
    cache_write_other_ttl_s: int | None = None   # required iff cache_write_other > 0 (e.g. 1800)
    cache_write_unknown: int = 0            # writes whose TTL split the source did not report
    output: int = 0                         # billed output INCLUDING thinking/reasoning
    output_reasoning: int | None = None     # informational subset of output; never added
    web_search_requests: int = 0
    web_fetch_requests: int = 0

    def __post_init__(self) -> None:
        for name in _BUCKET_COUNTS:
            v = getattr(self, name)
            if type(v) is not int or v < 0 or v > MAX_TOKENS:
                raise _fail(self, name, "must be an int in [0, 2**53]")
        r = self.output_reasoning
        if r is not None:
            if type(r) is not int or r < 0 or r > MAX_TOKENS:
                raise _fail(self, "output_reasoning", "must be an int in [0, 2**53]")
            if r > self.output:
                raise _fail(self, "output_reasoning", "exceeds output")
        ttl = self.cache_write_other_ttl_s
        if ttl is not None and (type(ttl) is not int or ttl <= 0 or ttl > MAX_TOKENS):
            raise _fail(self, "cache_write_other_ttl_s", "must be a positive int")
        if self.cache_write_other > 0 and ttl is None:
            raise _fail(self, "cache_write_other_ttl_s", "required when cache_write_other > 0")

    @property
    def cache_write(self) -> int:
        """All write buckets: 5m + 1h + other + unknown."""
        return (self.cache_write_5m + self.cache_write_1h + self.cache_write_other
                + self.cache_write_unknown)

    @property
    def total_input(self) -> int:
        """uncached_input + cache_read + cache_write."""
        return self.uncached_input + self.cache_read + self.cache_write

    def __add__(self, other: object) -> UsageBuckets:
        if not isinstance(other, UsageBuckets):
            return NotImplemented
        a_ttl, b_ttl = self.cache_write_other_ttl_s, other.cache_write_other_ttl_s
        if a_ttl is not None and b_ttl is not None and a_ttl != b_ttl:
            raise ContractViolation("UsageBuckets.__add__: different cache_write_other_ttl_s")
        reasoning = (self.output_reasoning + other.output_reasoning
                     if self.output_reasoning is not None and other.output_reasoning is not None
                     else None)
        return UsageBuckets(
            uncached_input=self.uncached_input + other.uncached_input,
            cache_read=self.cache_read + other.cache_read,
            cache_write_5m=self.cache_write_5m + other.cache_write_5m,
            cache_write_1h=self.cache_write_1h + other.cache_write_1h,
            cache_write_other=self.cache_write_other + other.cache_write_other,
            cache_write_other_ttl_s=a_ttl if a_ttl is not None else b_ttl,
            cache_write_unknown=self.cache_write_unknown + other.cache_write_unknown,
            output=self.output + other.output,
            output_reasoning=reasoning,
            web_search_requests=self.web_search_requests + other.web_search_requests,
            web_fetch_requests=self.web_fetch_requests + other.web_fetch_requests,
        )

    def __radd__(self, other: object) -> UsageBuckets:
        if other == 0:  # sum() starts from 0
            return self
        return NotImplemented


@dataclass(frozen=True, slots=True)
class PricingContext:
    provider: str            # "anthropic" | "openai"
    # "anthropic_api" | "claude_platform_aws" | "foundry" | "bedrock" | "vertex" | "openai_api" |
    # "azure_openai" | "unknown"
    channel: str
    model: str               # normalized id (core/models.py), e.g. "claude-opus-5-5"; "" if unknown
    model_raw: str           # exactly as reported
    service_tier: str = "standard"   # "standard"|"batch"|"flex"|"priority"|"fast"|"unknown"
    speed: str = "standard"          # Anthropic "standard" | "fast"
    inference_geo: str | None = None # "us" | "global" | None ("not_available" → None)
    endpoint_scope: str = "unknown"  # "global" | "regional" | "multi_region" | "unknown"
    write_ttl_hint: str | None = None  # "5m" | "1h" | None: point estimate for cache_write_unknown
    billing_path: str = "unknown"    # BILLING_PATHS; "subscription" ⇒ basis list_equivalent (D26)

    def __post_init__(self) -> None:
        for name in ("provider", "channel", "model", "model_raw", "service_tier", "speed"):
            _str(self, name)
        _str(self, "inference_geo", optional=True)
        _one_of(self, "endpoint_scope", _ENDPOINT_SCOPES)
        _one_of(self, "write_ttl_hint", _TTL_HINTS, optional=True)
        _one_of(self, "billing_path", BILLING_PATHS)


@dataclass(frozen=True, slots=True)
class Inference:
    """Atomic priced unit: one element of Anthropic usage.iterations, or the whole usage when
    absent."""

    inference_id: str
    kind: InferenceKind
    usage: UsageBuckets
    pricing: PricingContext
    usage_source: UsageSource = UsageSource.FINAL
    billable: bool | None = True            # None = billing rule uncertain → range [0, full]
    billing_rule_id: str | None = None      # e.g. "anthropic.refusal.pre_output"
    # MESSAGE_START_ONLY only: upper estimate of the true output tokens (≥ usage.output); tokens,
    # never dollars
    output_upper: int | None = None
    provider_reported_cost_nano: int | None = None
    provider_reported_cost_basis: str | None = None   # a Basis value; CC OTel: provider_estimate

    def __post_init__(self) -> None:
        _str(self, "inference_id")
        _enum(self, "kind", InferenceKind)
        _instance(self, "usage", UsageBuckets)
        _instance(self, "pricing", PricingContext)
        _enum(self, "usage_source", UsageSource)
        _bool(self, "billable", optional=True)
        _str(self, "billing_rule_id", optional=True)
        if self.output_upper is not None:
            _count(self, "output_upper")
            if self.usage_source is not UsageSource.MESSAGE_START_ONLY:
                raise _fail(self, "output_upper", "only for usage_source message_start_only")
            if self.output_upper < self.usage.output:
                raise _fail(self, "output_upper", "below usage.output")
        _int(self, "provider_reported_cost_nano", optional=True)
        _one_of(self, "provider_reported_cost_basis", _BASIS_VALUES, optional=True)


@dataclass(frozen=True, slots=True)
class CacheDiagnostic:
    # canonical (DIAG_REASONS): "model_changed" | "system_changed" | "tools_changed" |
    # "messages_changed" | "param_changed" | "key_changed" | "compacted" |
    # "previous_message_not_found" | "unavailable"
    reason: str
    provider_reason: str             # verbatim provider label (e.g. OpenAI "reasoning_effort")
    missed_input_tokens_estimate: int | None   # magnitude only; NEVER priced
    source: str      # "anthropic.cache_diagnostics" | "openai.prompt_cache_diagnostics"

    def __post_init__(self) -> None:
        _one_of(self, "reason", DIAG_REASONS)
        _str(self, "provider_reason")
        _count(self, "missed_input_tokens_estimate", optional=True)
        _str(self, "source")


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    attempt_no: int                   # 0-based within the logical request
    ts_start_ms: int                  # request start: the cache TTL is measured from here
    ttft_ms: int | None
    duration_ms: int | None
    outcome: Outcome
    http_status: int | None
    # "overloaded" | "rate_limit" | "timeout" | "connection" | "prompt_too_long" | "spend_cap" |
    # "thinking_binding" | "invalid_request" | "auth" | other
    error_type: str | None
    retry_layer: str | None           # "sdk" | "agent" | "gateway" | None
    retry_after_ms: int | None
    should_retry: bool | None         # x-should-retry
    provider_request_id: str | None   # "req_…": join hint only (D4)
    provider_message_id: str | None   # "msg_…": the de-duplication key for Anthropic responses
    model_served: str | None
    stop_reason: str | None           # provider stop reason
    inferences: tuple[Inference, ...]
    diagnostics: CacheDiagnostic | None = None
    applied_edits: tuple[tuple[str, int], ...] = ()   # (edit type, cleared_input_tokens)
    thinking_dropped: int = 0         # count of input_transformations of type "thinking_dropped"
    sdk_retry_count: int | None = None  # x-stainless-retry-count of this attempt (recorder hooks)
    raw_usage_json: str | None = None # canonical JSON of the provider usage object (≤ 8 KiB)
    convention_id: str | None = None  # convention used to derive `inferences` (§5.2)

    def __post_init__(self) -> None:
        _str(self, "attempt_id")
        _count(self, "attempt_no")
        _count(self, "ts_start_ms")
        _count(self, "ttft_ms", optional=True)
        _count(self, "duration_ms", optional=True)
        _enum(self, "outcome", Outcome)
        _count(self, "http_status", optional=True)
        _str(self, "error_type", optional=True)
        _one_of(self, "retry_layer", _RETRY_LAYERS, optional=True)
        _count(self, "retry_after_ms", optional=True)
        _bool(self, "should_retry", optional=True)
        for name in ("provider_request_id", "provider_message_id", "model_served", "stop_reason",
                     "convention_id"):
            _str(self, name, optional=True)
        _tuple(self, "inferences", Inference)
        _instance(self, "diagnostics", CacheDiagnostic, optional=True)
        edits = _tuple(self, "applied_edits")
        norm = []
        for edit in edits:
            if (not isinstance(edit, (list, tuple)) or len(edit) != 2
                    or not isinstance(edit[0], str) or type(edit[1]) is not int
                    or not 0 <= edit[1] <= MAX_TOKENS):
                raise _fail(self, "applied_edits", "items must be (edit type, token count)")
            norm.append((edit[0], edit[1]))
        if any(type(e) is not tuple for e in edits):
            object.__setattr__(self, "applied_edits", tuple(norm))
        _count(self, "thinking_dropped")
        _count(self, "sdk_retry_count", optional=True)
        raw = self.raw_usage_json
        if raw is not None:
            if not isinstance(raw, str):
                raise _fail(self, "raw_usage_json", "must be a str")
            if len(raw) > _RAW_USAGE_MAX_BYTES // 4 and len(raw.encode("utf-8", "surrogatepass")) \
                    > _RAW_USAGE_MAX_BYTES:
                raise _fail(self, "raw_usage_json", "exceeds 8 KiB")


@dataclass(frozen=True, slots=True)
class Breakpoint:
    block_index: int                  # index into ContentFingerprint.blocks
    ttl: str                          # "5m" | "1h" | "30m"
    assumed: bool = False             # True when inferred (trace@1 count without positions, §5.5)

    def __post_init__(self) -> None:
        _count(self, "block_index")
        _one_of(self, "ttl", _BREAKPOINT_TTLS)
        _bool(self, "assumed")


@dataclass(frozen=True, slots=True)
class RequestParams:
    """Prompt-affecting parameters (the invalidation hierarchy lives outside rendered bytes)."""

    model_requested: str
    max_tokens: int | None = None
    stream: bool | None = None
    thinking: str | None = None        # "off" | "adaptive" | "enabled:<budget>"
    effort: str | None = None          # effective effort of THIS request
    session_effort: str | None = None  # Claude Code session-level `effort` (sticky default)
    tool_choice: str | None = None     # "auto"|"any"|"none"|"tool:<hmac>"
    output_format: str | None = None   # HMAC of output_config.format (fingerprint) or "set"/None
    speed: str | None = None
    service_tier_requested: str | None = None
    inference_geo_requested: str | None = None
    betas: tuple[str, ...] = ()        # sorted anthropic-beta values
    breakpoints: tuple[Breakpoint, ...] = ()
    automatic_caching: bool | None = None   # top-level cache_control present
    context_management: str | None = None   # "set" or HMAC (fingerprint tier)
    task_budget: int | None = None
    web_search_enabled: bool | None = None
    citations_enabled: bool | None = None
    has_images: bool | None = None
    advisor_model: str | None = None
    disable_parallel_tool_use: bool | None = None

    def __post_init__(self) -> None:
        _str(self, "model_requested")
        _count(self, "max_tokens", optional=True)
        for name in ("thinking", "effort", "session_effort", "tool_choice", "output_format",
                     "speed", "service_tier_requested", "inference_geo_requested",
                     "context_management", "advisor_model"):
            _str(self, name, optional=True)
        for name in ("stream", "automatic_caching", "web_search_enabled", "citations_enabled",
                     "has_images", "disable_parallel_tool_use"):
            _bool(self, name, optional=True)
        betas = _tuple(self, "betas", str)
        if list(betas) != sorted(betas):
            object.__setattr__(self, "betas", tuple(sorted(betas)))
        _tuple(self, "breakpoints", Breakpoint)
        _count(self, "task_budget", optional=True)


@dataclass(frozen=True, slots=True)
class BlockRef:
    """One rendered block in wire order, content-free. Hashes: HMAC-SHA256 hex[:32] under key_id."""

    h: str               # wire bytes minus every cache_control key (marker moves never alter h)
    h_sorted: str | None # key-sorted canonical rendering (serialization-churn detection)
    h_norm: str | None   # h after replacing volatile spans by class placeholders
    tier: str            # "tools" | "system" | "messages"
    kind: str            # see BLOCK_KINDS
    role: str | None
    n_bytes: int
    est_tokens: int | None           # estimate; never billed
    image_px: tuple[int, int] | None = None
    # computed BEFORE hashing: "iso_datetime","uuid","unix_ts",…
    volatile_classes: tuple[str, ...] = ()
    lookback_pos: int = 0            # position index with tool_use / tool_result runs collapsed
    deferred: bool = False           # tool definition sent with defer_loading (tool search)

    def __post_init__(self) -> None:
        _str(self, "h")
        _str(self, "h_sorted", optional=True)
        _str(self, "h_norm", optional=True)
        _one_of(self, "tier", _TIERS)
        _one_of(self, "kind", BLOCK_KINDS)
        _str(self, "role", optional=True)
        _count(self, "n_bytes")
        _count(self, "est_tokens", optional=True)
        px = self.image_px
        if px is not None:
            if (not isinstance(px, (list, tuple)) or len(px) != 2
                    or any(type(p) is not int or p < 0 for p in px)):
                raise _fail(self, "image_px", "must be (width, height) ints")
            if type(px) is not tuple:
                object.__setattr__(self, "image_px", tuple(px))
        _tuple(self, "volatile_classes", str)
        _count(self, "lookback_pos")
        _bool(self, "deferred")


@dataclass(frozen=True, slots=True)
class ContentFingerprint:
    key_id: str                      # hashes are comparable only within one key
    blocks: tuple[BlockRef, ...]
    tier_end: tuple[int, int, int]   # index after the last tools / system / messages block

    def __post_init__(self) -> None:
        _str(self, "key_id")
        blocks = _tuple(self, "blocks", BlockRef)
        te = _tuple(self, "tier_end")
        if (len(te) != 3 or any(type(x) is not int for x in te)
                or not 0 <= te[0] <= te[1] <= te[2] <= len(blocks)):
            raise _fail(self, "tier_end", "must be three non-decreasing indexes within blocks")


@dataclass(frozen=True, slots=True)
class AppendedItem:
    """Content-free summary of what entered the context since the previous request of the lane."""

    kind: str            # "tool_result" | "user_text" | "attachment" | "image" | "assistant"
    # allowlisted tool/attachment type name, else "h_"+HMAC (MCP, skill, plugin)
    name: str | None
    n_bytes: int
    is_error: bool = False
    images: int = 0

    def __post_init__(self) -> None:
        _one_of(self, "kind", _APPENDED_KINDS)
        _str(self, "name", optional=True)
        _count(self, "n_bytes")
        _bool(self, "is_error")
        _count(self, "images")


@dataclass(frozen=True, slots=True)
class Attribution:
    # "p_<20hex>" pseudonym (store); in transit also "r_<opaque>" | "c_<20hex>"
    principal: str | None = None
    team: str | None = None
    cost_center: str | None = None
    project: str | None = None
    repo: str | None = None          # "h_…" HMAC (name key) unless policy allowlists plain names
    workspace_id: str | None = None  # provider workspace id (not personal); "h_…" when hashed
    api_key_id: str | None = None    # "h_…" HMAC (name key)
    agent_product: str | None = None # "claude_code"|"agent_sdk"|"api"|framework name
    agent_type: str | None = None    # "general-purpose"|"Explore"|"workflow-subagent"|custom
    query_source: str | None = None  # "main"|"subagent"|"auxiliary"|"compaction"
    skill: str | None = None         # allowlisted or "h_…"
    mcp_server: str | None = None    # allowlisted or "h_…"
    plugin: str | None = None        # allowlisted or "h_…"
    workload_class: WorkloadClass = WorkloadClass.UNKNOWN
    entrypoint: str | None = None
    client_version: str | None = None
    # one of BILLING_PATHS (mirrored into PricingContext.billing_path)
    billing_path: str | None = None
    cwd_key: str | None = None       # "h_…" HMAC(cwd) with the name key
    # experiment tags (OTEL_RESOURCE_ATTRIBUTES tokenbill.arm/.wave)
    arm: str | None = None
    wave: str | None = None
    # allowlisted keys only (EXTRA_KEYS), values ≤ 128 chars, sorted
    extra: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        p = self.principal
        if p is not None and (not isinstance(p, str) or not _PRINCIPAL_RE.match(p)):
            raise _fail(self, "principal", "must be p_/c_<20 hex> or r_<opaque ref>")
        for name in ("team", "cost_center", "project", "repo", "workspace_id", "agent_product",
                     "agent_type", "query_source", "skill", "mcp_server", "plugin", "entrypoint",
                     "client_version", "arm", "wave"):
            _str(self, name, optional=True)
        for name in ("api_key_id", "cwd_key"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, str) or not _HASH_RE.match(v)):
                raise _fail(self, name, "must be h_<20 hex>")
        _enum(self, "workload_class", WorkloadClass)
        _one_of(self, "billing_path", BILLING_PATHS, optional=True)
        extra = self.extra
        if extra:
            extra = _pairs(self, "extra", sort=True)
            for k, v in extra:
                if k not in EXTRA_KEYS:
                    raise _fail(self, "extra", "key not in EXTRA_KEYS")
                if len(v) > _EXTRA_VALUE_MAX:
                    raise _fail(self, "extra", "value longer than 128 chars")
        elif type(extra) is not tuple:
            _tuple(self, "extra")


@dataclass(frozen=True, slots=True)
class SourceRef:
    # "claude-code" | "claude-code-headless" | "trace@1" | "trace@2" | "otlp" | …
    adapter: str
    source_id: str        # "s_" + HMAC of the source path/name (never the raw path)
    locator: str          # "line:1234" etc.; content-free
    fidelity: Fidelity
    # recorder 50, claude-code 40, claude-code-headless 38, anthropic-responses 35, otlp 20,
    # other 10
    priority: int

    def __post_init__(self) -> None:
        _str(self, "adapter")
        _str(self, "source_id")
        _str(self, "locator")
        _enum(self, "fidelity", Fidelity)
        _int(self, "priority")


_SERVING_KINDS = frozenset({InferenceKind.MESSAGE, InferenceKind.FALLBACK})


@dataclass(frozen=True, slots=True)
class Request:
    """One logical request (what the client meant to send once); ≥ 1 attempts."""

    request_id: str
    session_key: str
    lane_key: str
    seq: int                                  # order within lane (ties in ts broken by seq)
    attribution: Attribution
    params: RequestParams
    attempts: tuple[Attempt, ...]
    fingerprint: ContentFingerprint | None = None
    appended: tuple[AppendedItem, ...] = ()
    source: SourceRef | None = None

    def __post_init__(self) -> None:
        _str(self, "request_id")
        _str(self, "session_key")
        _str(self, "lane_key")
        _count(self, "seq")
        _instance(self, "attribution", Attribution)
        _instance(self, "params", RequestParams)
        if not _tuple(self, "attempts", Attempt):
            raise _fail(self, "attempts", "at least one attempt is required")
        _instance(self, "fingerprint", ContentFingerprint, optional=True)
        _tuple(self, "appended", AppendedItem)
        _instance(self, "source", SourceRef, optional=True)

    @property
    def ts_start_ms(self) -> int:
        """Start of the first attempt."""
        return self.attempts[0].ts_start_ms

    @property
    def final_attempt(self) -> Attempt:
        """The last attempt."""
        return self.attempts[-1]

    @property
    def serving_inference(self) -> Inference | None:
        """Last MESSAGE or FALLBACK inference of the final attempt, or None."""
        for inf in reversed(self.attempts[-1].inferences):
            if inf.kind in _SERVING_KINDS:
                return inf
        return None

    @property
    def billable_inferences(self) -> tuple[Inference, ...]:
        """Every inference of every attempt whose ``billable`` is not False."""
        return tuple(inf for att in self.attempts for inf in att.inferences
                     if inf.billable is not False)

    @property
    def model(self) -> str:
        """Serving inference model, else the requested model."""
        si = self.serving_inference
        if si is not None and si.pricing.model:
            return si.pricing.model
        return self.params.model_requested


class LaneEventKind(TBEnum):
    COMPACTION = "compaction"
    CLEAR = "clear"
    MODEL_FALLBACK = "model_fallback"
    MODEL_SWITCH_USER = "model_switch_user"
    API_ERROR = "api_error"
    CONTEXT_EDIT = "context_edit"
    UPGRADE = "upgrade"
    CONTEXT_INJECTION = "context_injection"
    HUMAN_PROMPT = "human_prompt"
    SESSION_META = "session_meta"
    COST_STATE = "cost_state"
    IMAGE_EVICTION = "image_eviction"
    QUOTA_STATE = "quota_state"


_N = type(None)
#: Fixed attrs schema per event kind (SPEC §3.2): key → allowed value types. Keys and types are
#: part of the contract; a key may be omitted when the source does not report it, never invented.
EVENT_ATTRS: Mapping[LaneEventKind, Mapping[str, tuple[type, ...]]] = types.MappingProxyType({
    LaneEventKind.COMPACTION: {"trigger": (str,), "pre_tokens": (int,), "post_tokens": (int,),
                               "duration_ms": (int,), "dropped_tokens": (int, _N)},
    LaneEventKind.CLEAR: {},
    LaneEventKind.MODEL_FALLBACK: {"from_model": (str,), "to_model": (str,), "trigger": (str,),
                                   "credited": (bool, _N)},
    LaneEventKind.MODEL_SWITCH_USER: {"from_model": (str,), "to_model": (str,)},
    LaneEventKind.API_ERROR: {"status": (int, _N), "error_type": (str,), "retry_attempt": (int, _N),
                              "max_retries": (int, _N), "retry_in_ms": (int, _N)},
    LaneEventKind.CONTEXT_EDIT: {"edit_type": (str,), "cleared_input_tokens": (int,)},
    LaneEventKind.UPGRADE: {"from_version": (str,), "to_version": (str,)},
    LaneEventKind.CONTEXT_INJECTION: {"att_type": (str,), "n_bytes": (int,)},
    LaneEventKind.HUMAN_PROMPT: {},
    LaneEventKind.SESSION_META: {"agent_type": (str, _N), "spawn_depth": (int, _N),
                                 "model_alias": (str, _N)},
    LaneEventKind.COST_STATE: {"reported_total_nano": (int,), "reporter": (str,)},
    LaneEventKind.IMAGE_EVICTION: {"n_images": (int,)},
    LaneEventKind.QUOTA_STATE: {"status": (str, _N), "rate_limit_type": (str, _N),
                                "using_overage": (bool, _N), "overage_status": (str, _N),
                                "resets_at_ms": (int, _N)},
})
_EVENT_VALUE_DOMAINS: Mapping[tuple[LaneEventKind, str], frozenset[str]] = {
    (LaneEventKind.COMPACTION, "trigger"): frozenset({"auto", "manual"}),
    (LaneEventKind.MODEL_FALLBACK, "trigger"): frozenset({"refusal", "availability", "unknown"}),
}


def _attr_type_ok(value: object, allowed: tuple[type, ...]) -> bool:
    t = type(value)
    if t is bool:
        return bool in allowed
    if t is int:
        return int in allowed
    if value is None:
        return _N in allowed
    return isinstance(value, str) and str in allowed


def _event_sort_key(ev: LaneEvent) -> tuple:
    return (ev.ts_ms, ev.kind.value, tuple((k, repr(v)) for k, v in ev.attrs))


@dataclass(frozen=True, slots=True)
class LaneEvent:
    lane_key: str
    ts_ms: int
    kind: LaneEventKind
    attrs: tuple[tuple[str, str | int | bool | None], ...] = ()   # sorted by key

    def __post_init__(self) -> None:
        _str(self, "lane_key")
        _count(self, "ts_ms")
        _enum(self, "kind", LaneEventKind)
        attrs = _pairs(self, "attrs", sort=True, value_types=(str, int, bool, _N))
        schema = EVENT_ATTRS[self.kind]
        for key, value in attrs:
            allowed = schema.get(key)
            if allowed is None:
                raise _fail(self, "attrs", f"key not in the {self.kind.value} schema")
            if not _attr_type_ok(value, allowed):
                raise _fail(self, "attrs", "value type does not match the schema")
            if type(value) is int and not -MAX_TOKENS <= value <= MAX_TOKENS:
                raise _fail(self, "attrs", "int out of range")
            domain = _EVENT_VALUE_DOMAINS.get((self.kind, key))
            if domain is not None and value not in domain:
                raise _fail(self, "attrs", "value not in the allowed domain")


def _request_sort_key(r: Request) -> tuple[int, int, str]:
    return (r.attempts[0].ts_start_ms, r.seq, r.request_id)


def _request_billing_path(r: Request) -> str | None:
    if r.attribution.billing_path is not None:
        return r.attribution.billing_path
    si = r.serving_inference
    return si.pricing.billing_path if si is not None else None


@dataclass(frozen=True, slots=True)
class Lane:
    lane_key: str
    session_key: str
    kind: LaneKind
    parent_lane_key: str | None
    # "ws:<id>" | "org:<channel>:<account>" | "sub:<id>" (Azure) | "unknown"
    cache_scope_key: str
    requests: tuple[Request, ...] # sorted by (ts_start_ms, seq)
    events: tuple[LaneEvent, ...] = ()   # sorted by ts_ms
    ttl_observed: str = "unknown" # "5m" | "1h" | "mixed" | "unknown"
    lane_exact: bool = True       # False when reconstructed heuristically

    def __post_init__(self) -> None:
        _str(self, "lane_key")
        _str(self, "session_key")
        _enum(self, "kind", LaneKind)
        _str(self, "parent_lane_key", optional=True)
        _str(self, "cache_scope_key")
        reqs = _tuple(self, "requests", Request)
        for r in reqs:
            if r.lane_key != self.lane_key:
                raise _fail(self, "requests", "request lane_key differs from the lane")
        keys = [_request_sort_key(r) for r in reqs]
        if any(keys[i] > keys[i + 1] for i in range(len(keys) - 1)):
            object.__setattr__(self, "requests", tuple(sorted(reqs, key=_request_sort_key)))
        evs = _tuple(self, "events", LaneEvent)
        for ev in evs:
            if ev.lane_key != self.lane_key:
                raise _fail(self, "events", "event lane_key differs from the lane")
        ekeys = [_event_sort_key(e) for e in evs]
        if any(ekeys[i] > ekeys[i + 1] for i in range(len(ekeys) - 1)):
            object.__setattr__(self, "events", tuple(sorted(evs, key=_event_sort_key)))
        _one_of(self, "ttl_observed", _TTL_OBSERVED)
        _bool(self, "lane_exact")

    @property
    def team(self) -> str | None:
        """``attribution.team`` of the first request, or None."""
        return self.requests[0].attribution.team if self.requests else None

    @property
    def billing_class(self) -> str:
        """Billing class (``billed`` | ``allowance``) of the first request."""
        return billing_class(_request_billing_path(self.requests[0]) if self.requests else None)


@dataclass(frozen=True, slots=True)
class Session:
    session_key: str              # globally unique, source-qualified (fixes the reused-run_id bug)
    source_kind: str
    attribution: Attribution
    lanes: tuple[Lane, ...]
    started_ms: int
    ended_ms: int

    def __post_init__(self) -> None:
        _str(self, "session_key")
        _str(self, "source_kind")
        _instance(self, "attribution", Attribution)
        for lane in _tuple(self, "lanes", Lane):
            if lane.session_key != self.session_key:
                raise _fail(self, "lanes", "lane session_key differs from the session")
        _count(self, "started_ms")
        _count(self, "ended_ms")
        if self.ended_ms < self.started_ms:
            raise _fail(self, "ended_ms", "before started_ms")


@dataclass(frozen=True, slots=True)
class UsageAggregate:
    """Provider-side aggregate: Admin usage_report bucket, Analytics user-day rolled to team, CUR
    usage amount, OTel metric, headless result totals."""

    agg_id: str
    # "anthropic.usage_report" | "anthropic.cc_analytics" | … | "otel.metric" |
    # "claude_code.headless_result"
    source_kind: str
    bucket_start_ms: int
    bucket_end_ms: int
    dims: tuple[tuple[str, str], ...]   # sorted: channel, workspace_id, api_key_id, model, …
    usage: UsageBuckets
    reported_cost_nano: int | None = None
    reported_cost_basis: str | None = None   # "invoice"|"contract"|"provider_estimate"|"list"
    list_cost_nano: int | None = None        # Enterprise Analytics list_amount
    finality: str = "provisional"            # "provisional" | "final"
    fetched_ms: int = 0

    def __post_init__(self) -> None:
        _str(self, "agg_id")
        _str(self, "source_kind")
        _count(self, "bucket_start_ms")
        _count(self, "bucket_end_ms")
        if self.bucket_end_ms < self.bucket_start_ms:
            raise _fail(self, "bucket_end_ms", "before bucket_start_ms")
        _pairs(self, "dims", sort=True)
        _instance(self, "usage", UsageBuckets)
        _int(self, "reported_cost_nano", optional=True)
        _one_of(self, "reported_cost_basis", _BASIS_VALUES, optional=True)
        _int(self, "list_cost_nano", optional=True)
        _one_of(self, "finality", _FINALITIES)
        _count(self, "fetched_ms")


@dataclass(frozen=True, slots=True)
class CostLine:
    """One row of a provider invoice-side report, aggregated to (date, channel, workspace/account,
    model, bucket)."""

    line_id: str
    source_kind: str               # "anthropic.cost_report" | … | "aws.cur2" | "gcp.billing_export"
    date_utc: str
    channel: str                   # "anthropic_api" | "bedrock" | "vertex" | "openai_api" | …
    workspace_id: str | None       # workspace (Anthropic), account (AWS, h_), project (GCP, h_)
    description: str               # provider model/cost-type label, never user content
    model: str | None
    cost_type: str | None
    token_type: str | None
    sku: str | None                # CUR line_item_usage_type / GCP sku.id (provider codes)
    service_tier: str | None
    inference_geo: str | None
    endpoint_scope: str | None
    amount_nano: int               # half-even from the source decimal string (§3.3)
    list_amount_nano: int | None = None
    currency: str = "USD"
    finality: str = "provisional"
    principal: str | None = None   # CUR line_item_iam_principal → p_ (principal key); not exported
    fetched_ms: int = 0

    def __post_init__(self) -> None:
        for name in ("line_id", "source_kind", "channel", "description", "currency"):
            _str(self, name)
        _date(self, "date_utc")
        for name in ("workspace_id", "model", "cost_type", "token_type", "sku", "service_tier",
                     "inference_geo"):
            _str(self, name, optional=True)
        _one_of(self, "endpoint_scope", _ENDPOINT_SCOPES, optional=True)
        _int(self, "amount_nano")
        _int(self, "list_amount_nano", optional=True)
        _one_of(self, "finality", _FINALITIES)
        p = self.principal
        if p is not None and (not isinstance(p, str) or not _STORE_PRINCIPAL_RE.match(p)):
            raise _fail(self, "principal", "must be p_<20 hex>")
        _count(self, "fetched_ms")


@dataclass(frozen=True, slots=True)
class OutcomeAggregate:
    """Team-level outcome/productivity counts (Claude Code Analytics), aggregated at ingest with k
    ≥ 5. Never stored per principal. Used only as a quality guardrail and for active-developer-day
    counts."""

    date_utc: str
    team: str
    n_users: int                   # distinct users contributing (≥ k or the row is merged/dropped)
    sessions: int
    commits: int
    pull_requests: int
    lines_added: int
    lines_removed: int
    edits_accepted: int
    edits_rejected: int
    source_kind: str = "anthropic.cc_analytics"

    def __post_init__(self) -> None:
        _date(self, "date_utc")
        _str(self, "team")
        for name in ("n_users", "sessions", "commits", "pull_requests", "lines_added",
                     "lines_removed", "edits_accepted", "edits_rejected"):
            _count(self, name)
        _str(self, "source_kind")


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Denormalized row, one per billable Inference: storage, group-by, reconcile, export."""

    inference_id: str
    request_id: str
    attempt_id: str
    session_key: str
    lane_key: str
    lane_kind: LaneKind
    ts_ms: int
    date_utc: str
    kind: InferenceKind
    usage_source: UsageSource
    billable: bool | None
    billing_rule_id: str | None
    pricing: PricingContext
    usage: UsageBuckets
    attribution: Attribution
    fidelity: Fidelity

    def __post_init__(self) -> None:
        for name in ("inference_id", "request_id", "attempt_id", "session_key", "lane_key"):
            _str(self, name)
        _enum(self, "lane_kind", LaneKind)
        _count(self, "ts_ms")
        _date(self, "date_utc")
        _enum(self, "kind", InferenceKind)
        _enum(self, "usage_source", UsageSource)
        _bool(self, "billable", optional=True)
        _str(self, "billing_rule_id", optional=True)
        _instance(self, "pricing", PricingContext)
        _instance(self, "usage", UsageBuckets)
        _instance(self, "attribution", Attribution)
        _enum(self, "fidelity", Fidelity)


# ---------------------------------------------------------------------------------------------
# lossless JSON round trip
# ---------------------------------------------------------------------------------------------

_FIELD_NAMES: dict[type, tuple[str, ...]] = {}


def _enc(value: Any) -> Any:
    t = type(value)
    if t is str or t is int or t is bool or value is None:
        return value
    if t is tuple or t is list:
        return [_enc(v) for v in value]
    names = _FIELD_NAMES.get(t)
    if names is not None:
        return {name: _enc(getattr(value, name)) for name in names}
    if isinstance(value, Enum):
        return value.value
    if t is Decimal:
        if not value.is_finite():
            raise TypeError("to_json: non-finite Decimal")
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        names = _FIELD_NAMES[t] = tuple(
            f.name for f in dataclasses.fields(value)
            if f.metadata.get("tokenbill.json", True)  # e.g. PublishedAggregate.token is not data
        )
        return {name: _enc(getattr(value, name)) for name in names}
    if t is frozenset or t is set:
        return sorted(_enc(v) for v in value)
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            key = k.value if isinstance(k, Enum) else k
            if not isinstance(key, str):
                raise TypeError("to_json: mapping keys must be str")
            out[key] = _enc(v)
        return out
    if isinstance(value, str):
        return str(value)
    raise TypeError(f"to_json: unsupported type {t.__name__}")


def to_json(obj: Any) -> dict[str, Any]:
    """Encode a record (any dataclass instance) as a JSON-compatible dict.

    Enums become their values, tuples lists, ``Decimal`` decimal strings, frozensets sorted lists.
    Floats are refused (money is never float, SPEC §2.4). Fields marked
    ``metadata={"tokenbill.json": False}`` are left out: ``PublishedAggregate.token`` is a
    construction guard, so a ``RunResult`` with published breakdowns encodes, while ``from_json``
    still refuses to rebuild a ``PublishedAggregate`` (only ``core.kanon.publish`` makes one).
    """
    if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
        raise TypeError("to_json expects a dataclass instance")
    return _enc(obj)


class _DecodeError(Exception):
    pass


_UNION_TYPES: tuple[Any, ...] = (typing.Union, types.UnionType)


def _json_matches(hint: Any, value: Any) -> bool:
    """Whether a JSON value can be decoded as *hint* (used to pick a union arm)."""
    if hint is Any or hint is object:
        return True
    if hint is _N:
        return value is None
    origin = typing.get_origin(hint)
    if origin in (tuple, list, frozenset, set):
        return type(value) is list or type(value) is tuple
    if origin in (dict, Mapping):
        return isinstance(value, dict)
    if hint is bool:
        return type(value) is bool
    if hint is int:
        return type(value) is int
    if hint is str:
        return isinstance(value, str)
    if hint is Decimal:
        return isinstance(value, str) or type(value) is int
    if isinstance(hint, type) and issubclass(hint, Enum):
        try:
            hint(value)
        except (ValueError, TypeError):
            return False
        return True
    if dataclasses.is_dataclass(hint):
        return isinstance(value, dict)
    return False


@functools.cache
def _decoder(hint: Any) -> Callable[[Any], Any]:
    if hint is Any or hint is object:
        return lambda v: v
    if hint is _N:
        def dec_none(v: Any) -> None:
            if v is not None:
                raise _DecodeError("expected null")
            return None
        return dec_none
    if hint in (tuple, list, frozenset, set, dict):  # bare builtins: no element type to check
        hint = {tuple: tuple[Any, ...], list: list[Any], frozenset: frozenset[Any],
                set: frozenset[Any], dict: dict[str, Any]}[hint]
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin in _UNION_TYPES:
        arms = [a for a in args if a is not _N]
        nullable = len(arms) != len(args)
        if len(arms) == 1:
            inner = _decoder(arms[0])

            def dec_opt(v: Any) -> Any:
                if v is None:
                    if nullable:
                        return None
                    raise _DecodeError("unexpected null")
                return inner(v)
            return dec_opt

        primitive = all(a in (str, int, bool) for a in arms)

        def dec_union(v: Any) -> Any:
            if v is None:
                if nullable:
                    return None
                raise _DecodeError("unexpected null")
            if primitive:  # e.g. str | int | bool: JSON already carries the right type
                t = type(v)
                if (t is bool and bool in arms) or (t is int and int in arms) or (
                        t is str and str in arms):
                    return v
                raise _DecodeError("value matches no union arm")
            for arm in arms:
                if _json_matches(arm, v):
                    return _decoder(arm)(v)
            raise _DecodeError("value matches no union arm")
        return dec_union
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            item = _decoder(args[0])
            return lambda v: tuple(item(x) for x in _as_list(v))
        items = [_decoder(a) for a in args]

        def dec_fixed(v: Any) -> tuple:
            seq = _as_list(v)
            if len(seq) != len(items):
                raise _DecodeError("tuple length mismatch")
            return tuple(d(x) for d, x in zip(items, seq, strict=True))
        return dec_fixed
    if origin is list:
        item = _decoder(args[0]) if args else (lambda x: x)
        return lambda v: [item(x) for x in _as_list(v)]
    if origin in (frozenset, set):
        item = _decoder(args[0]) if args else (lambda x: x)
        return lambda v: frozenset(item(x) for x in _as_list(v))
    if origin in (dict, Mapping):
        kdec = _decoder(args[0]) if args else (lambda x: x)
        vdec = _decoder(args[1]) if args else (lambda x: x)

        def dec_map(v: Any) -> dict:
            if not isinstance(v, dict):
                raise _DecodeError("expected an object")
            return {kdec(k): vdec(x) for k, x in v.items()}
        return dec_map
    if hint is bool:
        def dec_bool(v: Any) -> bool:
            if type(v) is not bool:
                raise _DecodeError("expected a bool")
            return v
        return dec_bool
    if hint is int:
        def dec_int(v: Any) -> int:
            if type(v) is not int:
                raise _DecodeError("expected an int")
            return v
        return dec_int
    if hint is str:
        def dec_str(v: Any) -> str:
            if not isinstance(v, str):
                raise _DecodeError("expected a string")
            return v
        return dec_str
    if hint is Decimal:
        def dec_decimal(v: Any) -> Decimal:
            if type(v) is int:
                return Decimal(v)
            if not isinstance(v, str):
                raise _DecodeError("expected a decimal string")
            try:
                d = Decimal(v)
            except InvalidOperation:
                raise _DecodeError("bad decimal string") from None
            if not d.is_finite():
                raise _DecodeError("non-finite decimal")
            return d
        return dec_decimal
    if isinstance(hint, type) and issubclass(hint, Enum):
        def dec_enum(v: Any) -> Enum:
            try:
                return hint(v)
            except (ValueError, TypeError):
                raise _DecodeError(f"not a {hint.__name__} value") from None
        return dec_enum
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return _dataclass_decoder(hint)
    raise TypeError(f"from_json: unsupported annotation {hint!r}")


def _as_list(v: Any) -> list | tuple:
    if type(v) is not list and type(v) is not tuple:
        raise _DecodeError("expected an array")
    return v


def _dataclass_decoder(cls: type) -> Callable[[Any], Any]:
    try:
        hints = typing.get_type_hints(cls)
    except Exception as exc:  # unresolvable forward references (protocol-typed fields)
        raise TypeError(f"from_json: {cls.__name__} is not JSON-decodable") from exc
    specs = []
    for f in dataclasses.fields(cls):
        if not f.init:
            continue
        has_default = (f.default is not dataclasses.MISSING
                       or f.default_factory is not dataclasses.MISSING)
        specs.append((f.name, hints[f.name], has_default))
    names = frozenset(name for name, _, _ in specs)
    # Field decoders are resolved on first use (not here) so self-referential types such as
    # ResolvedRates.scope_range do not recurse while the decoder is being built.
    field_decoders: list[Callable[[Any], Any] | None] = [None] * len(specs)

    def dec(v: Any) -> Any:
        if not isinstance(v, dict):
            raise _DecodeError(f"{cls.__name__}: expected an object")
        if not names.issuperset(v):
            raise _DecodeError(f"{cls.__name__}: unknown key")
        kwargs = {}
        for i, (name, hint, has_default) in enumerate(specs):
            if name in v:
                d = field_decoders[i]
                if d is None:
                    d = field_decoders[i] = _decoder(hint)
                kwargs[name] = d(v[name])
            elif not has_default:
                raise _DecodeError(f"{cls.__name__}: missing key {name}")
        return cls(**kwargs)
    return dec


def from_json(cls: type, d: Mapping[str, Any]) -> Any:
    """Decode *d* (as produced by :func:`to_json`) into an instance of the dataclass *cls*.

    Unknown keys, missing required keys and mistyped values raise ``ContractViolation``
    (content-free), as do the record's own ``__post_init__`` invariants.
    """
    if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
        raise TypeError("from_json expects a dataclass type")
    try:
        return _decoder(cls)(d)
    except _DecodeError as exc:
        raise ContractViolation(f"from_json({cls.__name__}): {exc}") from None
    except (ValueError, OverflowError) as exc:
        raise ContractViolation(f"from_json({cls.__name__}): {type(exc).__name__}") from None

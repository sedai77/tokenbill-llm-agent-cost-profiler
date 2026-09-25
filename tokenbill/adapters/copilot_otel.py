"""GitHub Copilot OpenTelemetry spans (addendum §5.10, §5.11; package CP-OTEL).

Two things live here:

* :func:`map_chat_spans` — the **single** implementation that turns Copilot client spans into
  canonical records, shared by this module's :class:`CopilotOtelAdapter` (OTel files) and
  :class:`~tokenbill.adapters.copilot_vscode.VsCodeAgentTracesAdapter` (VS Code
  ``agent-traces.db`` and CP-VSCODE's extracts). Each ``chat`` span becomes one request: model =
  response model else request model through ``core.models.normalize_copilot_model`` (request model
  ``auto`` → routing ``auto``); ``gen_ai.response.id`` → ``provider_message_id``; session key
  ``core.ids.copilot_session_key(gen_ai.conversation.id)`` and lane key
  ``core.ids.copilot_lane_key(session_key, lane_kind, agent_id)`` (the formulas CP-LOCAL uses, so
  one conversation seen by two sources merges in STORE); tokens under the convention
  ``github_copilot.otel`` registered here (input inclusive of cache read and creation; creation →
  ``cache_write_unknown``); ``copilot_chat.copilot_usage_nano_aiu`` / ``github.copilot.nano_aiu`` →
  provider estimate (``core.money.nano_aiu_to_nano``, R12); ``github.copilot.cost`` (a multiplier)
  is ignored; ``gen_ai.request.reasoning.level`` → effort; a stated context tier or
  ``copilot_chat.request.max_prompt_tokens`` above the model's band threshold →
  ``PricingContext.context_tier``; initiator / interaction-type attributes and nested
  ``invoke_agent`` ancestry → lane kind. Root ``invoke_agent`` spans are never requests: their
  nano-AIU total becomes one ``COST_STATE`` event (reporter ``copilot.otel.invoke_agent``) per
  conversation on its main lane (summing every span would double count). Span events
  ``github.copilot.session.compaction_complete`` / ``…truncation`` / ``…shutdown`` become
  COMPACTION / CONTEXT_EDIT (``copilot_truncation``) / SESSION_META lane events.

  **Dedupe:** inside one trace, native ``github-copilot`` spans win over chat spans VS Code
  synthesizes for the in-editor CLI agent (service ``copilot-chat``, no response id:
  ``dq.copilot_synthesized_span_skipped``; kept without a native span:
  ``dq.copilot_synthesized_span_kept``); requests are keyed by ``gen_ai.response.id``, else by
  (conversation id, turn index, span id), so a span read twice counts once. Utility calls (a
  ``facts.copilot.utility_models`` model **and** nano-AIU 0, or a background interaction with
  nano-AIU 0) and BYOK calls are not billable (§5.11). Every Copilot inference carries billing
  path ``copilot_pool`` or ``copilot_direct`` (R-E43; attribution, else ``copilot_pool`` with
  ``dq.copilot_billing_path_assumed``). Fidelity ``NO_TTL_SPLIT``.

* :class:`CopilotOtelAdapter` (registry name ``copilot-otel``) — OTel files in three dialects:
  (a) VS Code / Copilot OTel-JS JSON-lines dumps (``readableSpanToJson``: ``traceId, spanId,
  parentSpanContext, name, startTime/endTime`` HrTime, ``attributes{}``, ``events[]``,
  ``resource{attributes}``; primary); (b) OTLP/JSON collector files (``resourceSpans``, int64 as
  strings); (c) Copilot CLI envelope variants (``type: "span"``, ``spanContext``,
  ``hrTime``/``_hrTime``, ``timeUnixNano``) only with ``"copilot-cli-otel-file" in
  opts.experimental``, else quarantined (``experimental:copilot-cli-otel-file``). A resource is
  read only when ``core.models.is_copilot_resource`` accepts it (with ``opts.otel_service_names``);
  other resources are counted in ``stats["foreign_resources"]`` (mixed files are claimed by TELEM's
  ``otlp``, which defers Copilot resources here, §5.10). ``sniff`` is True only when every resource
  in the head is Copilot. JetBrains resources — only through ``--otel-service-name
  NAME=copilot_jetbrains`` (or a configured name containing ``jetbrains`` / ``intellij``) — are read
  only with ``"copilot-jetbrains-otel" in opts.experimental`` (``agent_product=
  "copilot_jetbrains"``); without it they are counted (``stats["jetbrains_resources_skipped"]``)
  and skipped with ``dq.copilot_jetbrains_otel_experimental``. ``agent_product``: service
  ``github-copilot`` → ``copilot_cli``, ``copilot-chat`` → ``copilot_vscode``, anything else →
  ``copilot_other``.

**Privacy.** Only allowlisted attribute keys are decoded. Content attributes (messages, system
instructions, tool definitions / arguments / results, ``github.copilot.tool.parameters.*``, hook
input / output) are counted, never parsed (``dq.raw_bodies_ignored``); span names are never
emitted. Identity attributes (``enduser.pseudo.id``, ``user.name``, ``process.user.name``,
``host.name``) map to a team through ``opts.team_map`` and to a ``p_`` pseudonym (SPEC §5.1
identity modes), then are dropped; repository attributes become ``h_`` values. A resource (or
CP-VSCODE ``tokenbill_meta`` table) marked ``tokenbill.collector = "copilot-vscode-collect@1"`` is
a collector extract: its ``tokenbill.principal`` (``r_…`` / ``c_<20 hex>``, else dropped with
``dq.copilot_collector_principal_invalid``) becomes ``Attribution.principal``, its
``principal_key_id`` ``SourceInfo.principal_key_id`` and its ``team`` ``Attribution.team``;
without the marker those keys are ignored.

Attribute names beyond the addendum's allowlist (initiator, interaction type, context tier, BYOK,
TTFT, compaction / truncation event attributes, pre-1.0.64 underscore cache names) are **VERIFY**
(tests/v2/copilot_otel/README.md); unknown layouts are counted, never guessed.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core.conventions import BadUsageError, Convention, register_convention
from tokenbill.core.errors import ContractViolation, SourceError, UsageError
from tokenbill.core.facts import load as load_facts
from tokenbill.core.ids import (
    copilot_lane_key,
    copilot_session_key,
    is_opaque_ref,
    key_id,
    pseudonym,
    request_id_for,
    stable_id,
)
from tokenbill.core.jsonl import iter_lines, parse_json_line
from tokenbill.core.models import is_copilot_resource, normalize_copilot_model
from tokenbill.core.money import nano_aiu_to_nano
from tokenbill.core.records import (
    COPILOT_BILLING_PATHS,
    MAX_TOKENS,
    Attempt,
    Attribution,
    Fidelity,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Outcome,
    PricingContext,
    Request,
    RequestParams,
    Session,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    UsageSource,
)
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    SourceInfo,
)

__all__ = [
    "COLLECTOR_MARKER",
    "CONVENTION",
    "DIALECTS",
    "EXTRACT_SCHEMA",
    "REPORTER_INVOKE_AGENT",
    "CopilotOtelAdapter",
    "SpanEvent",
    "SpanView",
    "map_chat_spans",
    "normalize_copilot_otel",
    "service_products",
    "sniff_copilot_otel",
]

#: Convention of Copilot OTel / VS Code span usage (addendum §5.11).
CONVENTION = "github_copilot.otel"
#: ``tokenbill.collector`` resource value of a CP-VSCODE JSON-lines extract.
COLLECTOR_MARKER = "copilot-vscode-collect@1"
#: ``tokenbill_meta.schema`` value of a CP-VSCODE SQLite extract.
EXTRACT_SCHEMA = "tokenbill/vscode-extract@1"
#: COST_STATE reporter of the root ``invoke_agent`` nano-AIU total (the L0 parity input).
REPORTER_INVOKE_AGENT = "copilot.otel.invoke_agent"
#: ``map_chat_spans`` dialects: OTel-JS dumps, OTLP/JSON collector files, CLI envelopes, VS Code DB.
DIALECTS = ("otel-js", "otlp-json", "otel-cli", "vscode-db")

ADAPTER_NAME = "copilot-otel"
PRIORITY_OTEL = 21
PRIORITY_VSCODE_DB = 22
FLAG_CLI = "copilot-cli-otel-file"
FLAG_JETBRAINS = "copilot-jetbrains-otel"
QUARANTINE_CLI = f"experimental:{FLAG_CLI}"

DQ_RAW_BODIES = "dq.raw_bodies_ignored"
DQ_SYNTH_SKIPPED = "dq.copilot_synthesized_span_skipped"
DQ_SYNTH_KEPT = "dq.copilot_synthesized_span_kept"
DQ_PRINCIPAL_INVALID = "dq.copilot_collector_principal_invalid"
DQ_JETBRAINS_EXPERIMENTAL = "dq.copilot_jetbrains_otel_experimental"
DQ_JETBRAINS_UNMAPPED = "dq.copilot_jetbrains_otel_unmapped"
DQ_BILLING_PATH_ASSUMED = "dq.copilot_billing_path_assumed"
DQ_LEGACY_NAMES = "dq.copilot_legacy_otel_names"
DQ_CONVENTION_MISMATCH = "dq.convention_mismatch"
DQ_SUM_CHECK = "dq.sum_check_failed"
DQ_QUARANTINED = "dq.quarantined"
DQ_NO_TTL_SPLIT = "dq.no_ttl_split"

UTILITY_RULE = "github.copilot.utility_unbilled"
BYOK_RULE = "github.copilot.byok_not_billed_by_github"

_SEVERITY = {
    DQ_PRINCIPAL_INVALID: "warn", DQ_JETBRAINS_EXPERIMENTAL: "warn",
    DQ_JETBRAINS_UNMAPPED: "warn", DQ_LEGACY_NAMES: "warn", DQ_CONVENTION_MISMATCH: "warn",
    DQ_SUM_CHECK: "warn", DQ_QUARANTINED: "warn",
}
_DETAILS = {
    DQ_RAW_BODIES: "content attributes counted, never parsed",
    DQ_SYNTH_SKIPPED: "VS Code synthesized chat spans skipped (a native span of the trace exists)",
    DQ_SYNTH_KEPT: "VS Code synthesized chat spans kept (no native span in the trace)",
    DQ_PRINCIPAL_INVALID: "collector principal invalid or under another key id; dropped",
    DQ_JETBRAINS_EXPERIMENTAL: "JetBrains OTel resources skipped (flag copilot-jetbrains-otel)",
    DQ_JETBRAINS_UNMAPPED: "JetBrains spans with an unknown attribute layout counted, not mapped",
    DQ_BILLING_PATH_ASSUMED: "billing path not stated; copilot_pool assumed",
    DQ_LEGACY_NAMES: "pre-1.0.64 CLI cache attribute names; spans counted, not mapped",
    DQ_CONVENTION_MISMATCH: "cache read + creation exceeded input; counts treated as exclusive",
    DQ_SUM_CHECK: "reasoning tokens exceeded output; reasoning subset dropped",
    DQ_QUARANTINED: "records quarantined (see quarantine reasons)",
    DQ_NO_TTL_SPLIT: "cache writes without a 5m/1h split priced as cache_write_unknown",
    "dq.copilot_vscode_no_spans": "no spans in the VS Code traces database",
    "dq.copilot_vscode_schema": "VS Code traces database without the required columns",
    "dq.copilot_nano_aiu_invalid": "nano-AIU values that are not non-negative integers ignored",
}

_OPERATIONS = ("chat", "invoke_agent", "execute_tool")
_PRINCIPAL_RE = re.compile(r"(?:r_[A-Za-z0-9._-]{1,64}|c_[0-9a-f]{20})\Z")
_KEY_ID_RE = re.compile(r"k_[0-9a-f]{6,64}\Z")
_HEX_RE = re.compile(r"[0-9a-fA-F]{1,64}\Z")
_ID_RE = re.compile(r"[\x21-\x7e]{1,256}\Z")
_INT_STR_RE = re.compile(r"\s*\d{1,19}\s*\Z")
_EFFORT_RE = re.compile(r"[a-z][a-z_-]{0,15}\Z")
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?"
                     r"(Z|z|[+-]\d{2}:?\d{2})?\Z")
_MAX_ATTRS = 4096
_MAX_ANCESTRY = 64

# ---- attribute allowlists (the only keys ever decoded) ----------------------------------------
_TRACES = load_facts().copilot.vscode_traces
#: The addendum §5.13 allowlist (= facts ``vscode_traces.attribute_allowlist``).
VSCODE_ATTRIBUTE_ALLOWLIST: tuple[str, ...] = _TRACES.attribute_allowlist
_K_OP = "gen_ai.operation.name"
_K_REQ_MODEL = "gen_ai.request.model"
_K_RESP_MODEL = "gen_ai.response.model"
_K_RESP_ID = "gen_ai.response.id"
_K_CONV = "gen_ai.conversation.id"
_K_IN = "gen_ai.usage.input_tokens"
_K_OUT = "gen_ai.usage.output_tokens"
_K_READ = "gen_ai.usage.cache_read.input_tokens"
_K_CREATE = "gen_ai.usage.cache_creation.input_tokens"
_K_REASON = "gen_ai.usage.reasoning.output_tokens"
_K_REASON_ALT = "gen_ai.usage.reasoning_tokens"
_K_NANO_VSCODE = "copilot_chat.copilot_usage_nano_aiu"
_K_NANO_CLI = "github.copilot.nano_aiu"
_K_MAX_PROMPT = "copilot_chat.request.max_prompt_tokens"
_K_TURN = "copilot_chat.turn.index"
_K_EFFORT = "gen_ai.request.reasoning.level"
_K_MAX_TOKENS = "gen_ai.request.max_tokens"
_K_AGENT_ID = "gen_ai.agent.id"
_K_AGENT_NAME = "gen_ai.agent.name"
_K_ERROR_TYPE = "error.type"
#: Private keys the VS Code DB reader fills from typed columns.
K_CHAT_SESSION = "tokenbill.chat_session_id"
K_TTFT = "tokenbill.ttft_ms"
_USAGE_KEYS = (_K_IN, _K_OUT, _K_READ, _K_CREATE, _K_REASON, _K_REASON_ALT)
#: Pre-1.0.64 CLI underscore cache names (VERIFY): such spans are counted, never mapped.
_LEGACY_KEYS = ("gen_ai.usage.cache_read_input_tokens", "gen_ai.usage.cache_creation_input_tokens")
#: Lane-kind attributes (VERIFY): initiator and interaction type.
_INITIATOR_KEYS = ("github.copilot.initiator", "copilot_chat.initiator",
                   "github.copilot.interaction_type", "copilot_chat.interaction_type")
_SUBAGENT_VALUES = frozenset({"sub-agent", "subagent", "sub_agent", "conversation-subagent"})
_BACKGROUND_VALUES = frozenset({"conversation-background", "background", "utility"})
_COMPACTION_VALUES = frozenset({"conversation-compaction", "compaction"})
_TIER_KEYS = ("github.copilot.context_tier", "copilot_chat.context_tier")   # VERIFY
_BYOK_KEYS = ("github.copilot.byok", "copilot_chat.byok")                    # VERIFY
_ENDPOINT_TYPE = "copilot_chat.endpoint_type"
_TTFT_KEYS = (K_TTFT, "copilot_chat.time_to_first_token")                  # VERIFY (ms)
IDENTITY_KEYS: tuple[str, ...] = _TRACES.identity_keys
_REPO_KEYS = ("vcs.repository.url.full", "vcs.repository.name", "github.copilot.repository")
_EXTRA_SPAN_KEYS = (
    _K_NANO_CLI, _K_EFFORT, _K_MAX_TOKENS, _K_AGENT_ID, _K_AGENT_NAME, _K_ERROR_TYPE,
    *_LEGACY_KEYS, *_INITIATOR_KEYS, *_TIER_KEYS, *_BYOK_KEYS, *_TTFT_KEYS,
)
#: Every span attribute key the OTel readers decode (identity and repository keys included).
SPAN_KEYS = frozenset((*VSCODE_ATTRIBUTE_ALLOWLIST, *_EXTRA_SPAN_KEYS, *IDENTITY_KEYS,
                       *_REPO_KEYS))
#: Resource attribute keys the OTel readers decode.
RESOURCE_KEYS = frozenset(("service.name", "service.version", "tokenbill.collector",
                           "tokenbill.principal", "tokenbill.principal_key_id", "tokenbill.team",
                           *IDENTITY_KEYS, *_REPO_KEYS))
#: Content attribute keys and prefixes: counted, never decoded.
CONTENT_KEYS = frozenset(_TRACES.content_keys)
CONTENT_PREFIXES = ("gen_ai.input.", "gen_ai.output.", "gen_ai.system_instructions",
                    "gen_ai.tool.definitions", "gen_ai.tool.call.", "gen_ai.prompt",
                    "gen_ai.completion", "github.copilot.tool.parameters.",
                    "github.copilot.tool.result", "copilot_chat.hook_")
#: Span-event attributes (VERIFY: compaction / truncation event payloads).
_EV_TRIGGER = ("github.copilot.trigger", "trigger")
_EV_PRE = ("github.copilot.pre_compaction_tokens", "pre_compaction_tokens",
           "preCompactionTokens", "pre_tokens")
_EV_POST = ("github.copilot.post_compaction_tokens", "post_compaction_tokens",
            "postCompactionTokens", "post_tokens")
_EV_SYSTEM = ("github.copilot.system_tokens", "system_tokens", "systemTokens")
_EV_TOOLS = ("github.copilot.tool_definitions_tokens", "tool_definitions_tokens",
             "toolDefinitionsTokens")
_EV_DURATION = ("github.copilot.duration_ms", "duration_ms", "durationMs")
_EV_REMOVED = ("github.copilot.tokens_removed", "tokens_removed", "tokensRemoved")
EVENT_KEYS = frozenset((*_EV_TRIGGER, *_EV_PRE, *_EV_POST, *_EV_SYSTEM, *_EV_TOOLS,
                        *_EV_DURATION, *_EV_REMOVED))
_COPILOT_TRIGGERS = frozenset({"threshold", "manual", "context_limit_retry", "memory_pressure",
                               "model_switch"})
_EVENT_KINDS = {"session.compaction_complete": LaneEventKind.COMPACTION,
                "session.truncation": LaneEventKind.CONTEXT_EDIT,
                "session.shutdown": LaneEventKind.SESSION_META}

_PRODUCTS = ("copilot_jetbrains", "copilot_other")
_JETBRAINS_HINTS = ("jetbrains", "intellij")


# =============================================================================================
# convention github_copilot.otel
# =============================================================================================

def _usage_count(raw: Mapping[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_TOKENS:
        raise BadUsageError(f"bad_usage: {key}")
    return value


def normalize_copilot_otel(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """The ``github_copilot.otel`` mapping of one span's usage attributes (addendum §5.11).

    ``gen_ai.usage.input_tokens`` includes ``…cache_read.input_tokens`` and
    ``…cache_creation.input_tokens`` (VS Code source; CLI **VERIFY**): uncached = input − read −
    creation, creation → ``cache_write_unknown``; read + creation > input → the counts are treated
    as exclusive (``dq.convention_mismatch``). Output includes reasoning (a subset, dropped with
    ``dq.sum_check_failed`` when it exceeds output). Missing counts are 0; malformed ones raise
    :class:`BadUsageError`."""
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    notes: list[str] = []
    inp = _usage_count(raw, _K_IN) or 0
    read = _usage_count(raw, _K_READ) or 0
    create = _usage_count(raw, _K_CREATE) or 0
    out = _usage_count(raw, _K_OUT) or 0
    reasoning = _usage_count(raw, _K_REASON)
    if reasoning is None:
        reasoning = _usage_count(raw, _K_REASON_ALT)
    if read + create <= inp:
        uncached = inp - read - create
    else:
        uncached = inp
        notes.append(DQ_CONVENTION_MISMATCH)
    if reasoning is not None and reasoning > out:
        reasoning = None
        notes.append(DQ_SUM_CHECK)
    try:
        buckets = UsageBuckets(uncached_input=uncached, cache_read=read, cache_write_unknown=create,
                               output=out, output_reasoning=reasoning)
    except ContractViolation:  # sums beyond 2**53
        raise BadUsageError("bad_usage: total") from None
    return buckets, notes


register_convention(
    Convention(convention_id=CONVENTION, provider="github", inclusive_input=True, enabled=True,
               notes="gen_ai.usage.input_tokens includes cache read and cache creation (VS Code "
                     "chatMLFetcher.ts; CLI VERIFY); creation -> cache_write_unknown"),
    normalize_copilot_otel)


# =============================================================================================
# span views
# =============================================================================================

@dataclass(frozen=True, slots=True)
class SpanEvent:
    """One span event: name, time and its allowlisted attributes."""

    name: str
    ts_ms: int | None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SpanView:
    """A reader-neutral, allowlisted view of one span (OTel file line or VS Code DB row).

    ``attributes`` / ``resource`` hold only allowlisted keys (readers never decode content);
    ``name`` is used only to recognize the operation and is never emitted; ``agent_product`` is
    the reader's decision from the resource (``copilot_cli`` | ``copilot_vscode`` |
    ``copilot_jetbrains`` | ``copilot_other``); ``locator`` is content-free."""

    name: str
    trace_id: str | None
    span_id: str | None
    parent_span_id: str | None
    start_ms: int | None
    end_ms: int | None
    attributes: Mapping[str, Any]
    resource: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[SpanEvent, ...] = ()
    locator: str = ""
    error: bool = False
    agent_product: str | None = None


# =============================================================================================
# small value helpers
# =============================================================================================

def _int(value: object) -> int | None:
    """A non-negative int from an int, a digit string or an integral Decimal; else None."""
    if type(value) is int:
        return value if 0 <= value <= MAX_TOKENS else None
    if isinstance(value, str) and _INT_STR_RE.match(value):
        n = int(value)
        return n if n <= MAX_TOKENS else None
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        if 0 <= value <= MAX_TOKENS:
            return int(value)
    return None


def _nonneg_int(value: object) -> int | None:
    """Like :func:`_int` but without the 2**53 cap (nano-AIU may be large)."""
    if type(value) is int:
        return value if 0 <= value < 2**63 else None
    if isinstance(value, str) and _INT_STR_RE.match(value):
        return int(value)
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        if 0 <= value < 2**63:
            return int(value)
    return None


def _label(value: object, max_len: int = 128) -> str | None:
    """A printable, stripped label (model id, response id, team) of at most *max_len* chars."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_len or any(ord(c) < 32 or ord(c) == 127 for c in text):
        return None
    return text


def _ident(value: object) -> str | None:
    """A span / trace / conversation identifier (used only inside hashes)."""
    if type(value) is int:
        return str(value)
    if isinstance(value, str) and _ID_RE.match(value.strip() or "-"):
        return value.strip()
    return None


def _hex_id(value: object) -> str | None:
    if isinstance(value, str) and _HEX_RE.match(value):
        return value.lower()
    return None


def _truthy(value: object) -> bool:
    if type(value) is bool:
        return value
    return isinstance(value, str) and value.strip().lower() in ("true", "1", "yes")


def _first(attrs: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = attrs.get(key)
        if value is not None:
            return value
    return None


def parse_iso_ms(value: object) -> int | None:
    """An ISO 8601 timestamp (``Z`` or an offset; any fraction digits) → epoch ms, else None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    m = _ISO_RE.match(value.strip())
    if m is None:
        return None
    try:
        base = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
                            tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    ms = (base - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)) // _dt.timedelta(
        milliseconds=1)
    ms += int((m.group(7) or "0")[:3].ljust(3, "0"))
    tz = m.group(8)
    if tz and tz not in ("Z", "z"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        ms -= sign * (int(digits[:2]) * 60 + int(digits[2:4])) * 60_000
    return ms if 0 <= ms <= MAX_TOKENS else None


def time_ms(value: object) -> int | None:
    """An OTel time → epoch ms: HrTime ``[seconds, nanos]``, an epoch int or decimal string in
    ns (OTLP ``*UnixNano``), µs or ms (by magnitude), or an ISO 8601 string; None when absent or
    invalid."""
    if isinstance(value, list) and len(value) == 2:
        sec, nanos = _nonneg_int(value[0]), _nonneg_int(value[1])
        if sec is None or nanos is None or nanos >= 10**9:
            return None
        ms = sec * 1000 + nanos // 1_000_000
        return ms if 0 < ms <= MAX_TOKENS else None
    if isinstance(value, str) and not _INT_STR_RE.match(value):
        return parse_iso_ms(value)
    n = _nonneg_int(value)
    if n is None or n == 0:
        return None
    # epoch magnitude decides the unit: ns (OTLP ``*UnixNano``) ≥ 1e17, µs ≥ 1e14, else ms
    ms = n // 1_000_000 if n >= 10**17 else n // 1000 if n >= 10**14 else n
    return ms if 0 < ms <= MAX_TOKENS else None


def is_content_key(key: str) -> bool:
    """Whether *key* is a content attribute (counted, never decoded)."""
    return key in CONTENT_KEYS or key.startswith(CONTENT_PREFIXES)


def service_products(entries: Iterable[str]) -> dict[str, str | None]:
    """``opts.otel_service_names`` → ``{service name: agent product or None}``: ``NAME`` (product
    None, i.e. ``copilot_other`` unless the name looks like JetBrains) or
    ``NAME=copilot_jetbrains`` / ``NAME=copilot_other`` (another product → ``copilot_other``)."""
    out: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, str):
            continue
        name, sep, product = entry.partition("=")
        name, product = name.strip(), product.strip()
        if not name:
            continue
        mapped: str | None = None
        if sep:
            mapped = product if product in _PRODUCTS else "copilot_other"
        out.setdefault(name, mapped)
    return out


def product_for(service: str | None, configured: Mapping[str, str | None]) -> str:
    """The ``agent_product`` of a Copilot resource (addendum §5.10)."""
    if service == "github-copilot":
        return "copilot_cli"
    if service == "copilot-chat":
        return "copilot_vscode"
    if service is not None and service in configured:
        mapped = configured[service]
        if mapped is not None:
            return mapped
        low = service.lower()
        if any(hint in low for hint in _JETBRAINS_HINTS):
            return "copilot_jetbrains"
    return "copilot_other"


def _principal_for(opts: IngestOptions, raw: str | None) -> str | None:
    """SPEC §5.1 identity: ``central`` → ``r_<ref>``; ``two-stage`` → ``c_`` of the ref;
    ``install`` → ``p_`` of the raw identity (else of the ref); ``central-ingest`` → ``p_`` of
    the raw identity."""
    mode = opts.identity_mode
    if mode == "central":
        return f"r_{opts.principal_ref}" if opts.principal_ref else None
    if opts.principal_key is None:
        return None
    if mode == "two-stage":
        return pseudonym(opts.principal_key, "c", opts.principal_ref) \
            if opts.principal_ref else None
    source = raw if raw else (opts.principal_ref if mode == "install" else None)
    return pseudonym(opts.principal_key, "p", source) if source else None


def _team_for(opts: IngestOptions, candidates: Sequence[str]) -> str | None:
    if not opts.team_map:
        return None
    mapping = dict(opts.team_map)
    folded: dict[str, str] = {}
    for ref, team in opts.team_map:
        if isinstance(ref, str):
            folded.setdefault(ref.casefold(), team)
    for raw in candidates:
        if raw in mapping:
            return mapping[raw]
        team = folded.get(raw.casefold())
        if team is not None:
            return team
    return None


def check_options(opts: IngestOptions) -> None:
    """Refuse unknown identity modes and invalid collector refs (``UsageError``)."""
    if not isinstance(opts, IngestOptions):
        raise UsageError("read expects IngestOptions")
    if opts.identity_mode not in ("install", "central", "two-stage", "central-ingest"):
        raise UsageError("identity_mode must be install, central, two-stage or central-ingest")
    if opts.identity_mode == "central" and opts.principal_ref is not None \
            and not is_opaque_ref(opts.principal_ref):
        raise UsageError("principal_ref must be an opaque ref ([A-Za-z0-9._-]{1,64}, no '@')")


# =============================================================================================
# per-file bookkeeping (shared by the three CP-OTEL adapters)
# =============================================================================================

class Scan:
    """Per-file bookkeeping: source identity, quarantine, data-quality counters, statistics."""

    def __init__(self, adapter: str, path: Path, opts: IngestOptions) -> None:
        check_options(opts)
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

    def quarantine(self, locator: str, reason: str) -> None:
        """Quarantine one record (lenient) or raise ``SourceError`` naming file and locator."""
        if not self.opts.lenient:
            raise SourceError(f"{self.path.name}: {locator}: {reason}")
        self.quarantined.append(QuarantineItem(source_id=self.source_id, locator=locator,
                                               reason=reason))
        self.count("quarantined")

    def in_window(self, ts_ms: int) -> bool:
        """``opts.since_ms <= ts_ms < opts.until_ms`` (each bound optional)."""
        since, until = self.opts.since_ms, self.opts.until_ms
        return (since is None or ts_ms >= since) and (until is None or ts_ms < until)

    def records(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """``(line_no, object)`` for every JSON-object line (numbers exact); bad lines are
        quarantined (``bad_json``, ``not_object``, ``oversize_line``)."""
        for line_no, _offset, raw in iter_lines(self.path):
            self.count("lines")
            if not raw:
                self.quarantine(f"line:{line_no}", "oversize_line")
                continue
            obj = parse_json_line(raw, exact_numbers=True)
            if obj is None:
                self.quarantine(f"line:{line_no}", "not_object" if _is_json(raw) else "bad_json")
                continue
            yield line_no, obj

    def digest(self) -> tuple[str, int]:
        """SHA-256 and size of the source file (read only)."""
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

    def source_info(self, principal_key_id: str | None = None) -> SourceInfo:
        """The ``SourceInfo`` of this file (name key id set whenever a name key exists)."""
        sha, size = self.digest()
        name_key = self.opts.name_key
        name_key_id = self.opts.name_key_id or (key_id(name_key) if name_key else None)
        return SourceInfo(
            source_id=self.source_id, adapter=self.adapter,
            name_hmac=pseudonym(name_key, "h", self.path.name) if name_key else "",
            sha256=sha, bytes=size, name_key_id=name_key_id, principal_key_id=principal_key_id)

    def name_hash(self, value: str) -> str | None:
        """``h_`` of *value* under the name key (None without a name key)."""
        if not self.opts.name_key:
            self.count("names_dropped_no_key")
            return None
        return pseudonym(self.opts.name_key, "h", value)

    def finish(self, source: SourceInfo, *, requests: list[Request], sessions: list[Session],
               events: list[LaneEvent], aggregates: Sequence[UsageAggregate] = (),
               capabilities: Iterable[str] = ()) -> IngestResult:
        """The :class:`IngestResult` (notes sorted by code; ``dq.quarantined`` added)."""
        if self.quarantined:
            already = self._notes.get(DQ_QUARANTINED, [0])[0]
            self.note(DQ_QUARANTINED, len(self.quarantined) - already)
        notes = [DataQualityNote(code=code, severity=_SEVERITY.get(code, "info"), count=c,
                                 detail=_DETAILS.get(code, code), tokens=t if has_t else None)
                 for code, (c, t, has_t) in sorted(self._notes.items()) if c > 0]
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


def billing_path_of(opts: IngestOptions, scan: Scan, default: str) -> str:
    """``opts.attribution.billing_path`` when it is a Copilot path, else *default* with
    ``dq.copilot_billing_path_assumed`` (R-E43: never the unknown default)."""
    stated = opts.attribution.billing_path
    if stated in COPILOT_BILLING_PATHS:
        return stated
    scan.note(DQ_BILLING_PATH_ASSUMED)
    return default


def compliance_of(opts: IngestOptions) -> str | None:
    """``copilot_compliance`` from ``--attr`` when it names a documented restriction."""
    value = dict(opts.attribution.extra).get("copilot_compliance")
    return value if value in ("data_residency", "fedramp") else None


# =============================================================================================
# the shared chat-span mapping
# =============================================================================================

@dataclass
class _Draft:
    sort_key: tuple[Any, ...]
    request_id: str
    session_key: str
    lane_key: str
    ts_start: int
    ts_end: int
    build: Any            # callable(seq) -> Request


class _Mapper:
    """One :func:`map_chat_spans` run over the spans of one source."""

    def __init__(self, opts: IngestOptions, scan: Scan, *, source_id: str, adapter: str,
                 dialect: str, allowed_caps: frozenset[str]) -> None:
        if dialect not in DIALECTS:
            raise UsageError(f"unknown span dialect {dialect[:32]!r}")
        self.opts = opts
        self.scan = scan
        self.source_id = source_id
        self.adapter = adapter
        self.dialect = dialect
        self.priority = PRIORITY_VSCODE_DB if dialect == "vscode-db" else PRIORITY_OTEL
        self.source_kind = ("github_copilot.vscode_traces" if dialect == "vscode-db"
                            else "github_copilot.otel")
        self.allowed_caps = allowed_caps
        facts = load_facts().copilot
        self.utility_models = facts.utility_models
        self.bands = {m: rule.threshold for m, rule in facts.band_rules.items()}
        self.index: dict[tuple[str, str], SpanView] = {}
        self.conv_memo: dict[int, str] = {}
        self.key_id: str | None = None
        self.key_id_from_opts = False
        # lane key → (session key, kind, parent lane key)
        self.shells: dict[str, tuple[str, LaneKind, str | None]] = {}
        self.session_attr: dict[str, Attribution] = {}
        self.session_span: dict[str, list[int]] = {}
        self.events: list[LaneEvent] = []
        self.billing_path: str | None = None
        self.compliance = compliance_of(opts)

    # ---------- structure ----------
    def _op(self, span: SpanView) -> str | None:
        op = span.attributes.get(_K_OP)
        if isinstance(op, str) and op.strip():
            return op.strip()
        first = span.name.split(" ", 1)[0] if isinstance(span.name, str) else ""
        return first if first in _OPERATIONS else None

    def _parent(self, span: SpanView) -> SpanView | None:
        if span.trace_id is None or span.parent_span_id is None:
            return None
        return self.index.get((span.trace_id, span.parent_span_id))

    def _agents(self, span: SpanView, *, include_self: bool = False) -> list[SpanView]:
        """``invoke_agent`` ancestors, nearest first (the span itself first when asked)."""
        out: list[SpanView] = []
        if include_self and self._op(span) == "invoke_agent":
            out.append(span)
        node = self._parent(span)
        seen = 0
        while node is not None and seen < _MAX_ANCESTRY:
            if self._op(node) == "invoke_agent":
                out.append(node)
            node = self._parent(node)
            seen += 1
        return out

    def _conversation(self, span: SpanView) -> str:
        memo = self.conv_memo.get(id(span))
        if memo is not None:
            return memo
        conv = _ident(span.attributes.get(_K_CONV)) or _ident(span.attributes.get(K_CHAT_SESSION))
        if conv is None:
            node = self._parent(span)
            seen = 0
            while node is not None and seen < _MAX_ANCESTRY and conv is None:
                conv = _ident(node.attributes.get(_K_CONV)) \
                    or _ident(node.attributes.get(K_CHAT_SESSION))
                node = self._parent(node)
                seen += 1
        if conv is None:
            self.scan.count("conversation_from_trace")
            conv = f"trace:{span.trace_id}" if span.trace_id else f"source:{self.source_id}"
        self.conv_memo[id(span)] = conv
        return conv

    def _lane(self, span: SpanView, *, include_self: bool = False
              ) -> tuple[LaneKind, str | None, str | None]:
        """(lane kind, agent id, query source) of a span."""
        explicit = _first(span.attributes, _INITIATOR_KEYS)
        value = explicit.strip().lower() if isinstance(explicit, str) else ""
        agents = self._agents(span, include_self=include_self)
        if value in _SUBAGENT_VALUES or len(agents) >= 2:
            agent = agents[0] if agents else span
            agent_id = _ident(agent.attributes.get(_K_AGENT_ID)) \
                or _ident(agent.attributes.get(_K_AGENT_NAME)) or agent.span_id or "subagent"
            return LaneKind.SUBAGENT, agent_id, "subagent"
        if value in _BACKGROUND_VALUES:
            return LaneKind.HELPER, None, "auxiliary"
        if value in _COMPACTION_VALUES:
            return LaneKind.COMPACTION, None, "compaction"
        return LaneKind.MAIN, None, "main"

    def _lane_keys(self, span: SpanView, *, include_self: bool = False
                   ) -> tuple[str, str, LaneKind, str | None]:
        """(session key, lane key, kind, query source); registers the lane shells."""
        session_key = copilot_session_key(self._conversation(span))
        kind, agent_id, query_source = self._lane(span, include_self=include_self)
        main_key = copilot_lane_key(session_key, LaneKind.MAIN.value, None)
        self.shells.setdefault(main_key, (session_key, LaneKind.MAIN, None))
        lane_key = main_key
        if kind is not LaneKind.MAIN:
            lane_key = copilot_lane_key(session_key, kind.value, agent_id)
            self.shells.setdefault(lane_key, (session_key, kind, main_key))
        return session_key, lane_key, kind, query_source

    # ---------- identity ----------
    def _is_extract(self, span: SpanView) -> bool:
        return span.resource.get("tokenbill.collector") == COLLECTOR_MARKER

    def _claim_key(self, key: str | None) -> bool:
        """Whether a principal under key id *key* may be emitted (the first key id wins)."""
        if key is None:
            return False
        if self.key_id is None:
            self.key_id = key
            return True
        return self.key_id == key

    def _identity(self, span: SpanView) -> tuple[str | None, str | None]:
        """(principal, team) of a span."""
        opts = self.opts
        if self._is_extract(span):
            res = span.resource
            team = _label(res.get("tokenbill.team"))
            raw = res.get("tokenbill.principal")
            if raw is None:
                return None, team or opts.attribution.team
            principal = raw if isinstance(raw, str) and _PRINCIPAL_RE.match(raw) else None
            if principal is not None and principal.startswith("c_"):
                kid = res.get("tokenbill.principal_key_id")
                kid = kid if isinstance(kid, str) and _KEY_ID_RE.match(kid) else None
                if not self._claim_key(kid):
                    principal = None
            if principal is None:
                self.scan.note(DQ_PRINCIPAL_INVALID)
            return principal or opts.attribution.principal, team or opts.attribution.team
        merged = {**span.resource, **span.attributes}
        candidates = [v.strip() for k in IDENTITY_KEYS
                      if isinstance((v := merged.get(k)), str) and v.strip() and len(v) <= 512]
        team = _team_for(opts, candidates) or opts.attribution.team
        principal = _principal_for(opts, candidates[0] if candidates else None)
        if principal is not None and principal[:2] in ("p_", "c_"):
            if not self._claim_key(opts.principal_key_id or key_id(opts.principal_key or b"")):
                self.scan.note(DQ_PRINCIPAL_INVALID)
                principal = None
            else:
                self.key_id_from_opts = True
        return principal or opts.attribution.principal, team

    def _attribution(self, span: SpanView, query_source: str | None, product: str,
                     billing_path: str) -> Attribution:
        principal, team = self._identity(span)
        updates: dict[str, Any] = {"principal": principal, "team": team,
                                   "agent_product": product, "billing_path": billing_path}
        merged = {**span.resource, **span.attributes}
        repo = _first(merged, _REPO_KEYS)
        if isinstance(repo, str) and repo.strip() and len(repo) <= 2048:
            hashed = self.scan.name_hash(repo.strip())
            if hashed is not None:
                updates["repo"] = hashed
        version = _label(span.resource.get("service.version"), 32)
        if version is not None and opts_version_ok(version):
            updates["client_version"] = version
        agent_name = span.attributes.get(_K_AGENT_NAME) if query_source == "subagent" else None
        if isinstance(agent_name, str) and agent_name.strip() and len(agent_name) <= 256:
            name = agent_name.strip()
            if name in self.opts.name_allowlist and len(name) <= 64:
                updates["agent_type"] = name
            else:
                hashed = self.scan.name_hash(name)
                if hashed is not None:
                    updates["agent_type"] = hashed
        if query_source is not None:
            updates["query_source"] = query_source
        return dataclasses.replace(self.opts.attribution, **updates)

    # ---------- requests ----------
    def _nano(self, attrs: Mapping[str, Any]) -> int | None:
        raw = attrs.get(_K_NANO_VSCODE)
        if raw is None:
            raw = attrs.get(_K_NANO_CLI)
        if raw is None:
            return None
        n = _nonneg_int(raw)
        if n is None:
            self.scan.note("dq.copilot_nano_aiu_invalid")
        return n

    def _context_tier(self, attrs: Mapping[str, Any], model: str) -> str | None:
        stated = _first(attrs, _TIER_KEYS)
        if isinstance(stated, str) and stated.strip().lower() in ("default", "long_context"):
            return stated.strip().lower()
        max_prompt = _int(attrs.get(_K_MAX_PROMPT))
        threshold = self.bands.get(model)
        if max_prompt is None or threshold is None:
            return None
        return "long_context" if max_prompt > threshold else "default"

    def _usage_raw(self, attrs: Mapping[str, Any]) -> dict[str, int]:
        raw: dict[str, int] = {}
        for key in _USAGE_KEYS:
            value = attrs.get(key)
            if value is None:
                continue
            n = _int(value)
            if n is None:
                raise BadUsageError(f"bad_usage: {key}")
            raw[key] = n
        return raw

    def _request(self, span: SpanView, product: str) -> _Draft | None:
        attrs = span.attributes
        opts = self.opts
        if span.start_ms is None:
            self.scan.quarantine(span.locator, "missing:startTime")
            return None
        raw = self._usage_raw(attrs)
        buckets, codes = normalize_copilot_otel(raw)
        for code in codes:
            self.scan.note(code, tokens=buckets.total_input)
        if buckets.cache_write_unknown:
            self.scan.note(DQ_NO_TTL_SPLIT, tokens=buckets.cache_write_unknown)
        req_raw = _label(attrs.get(_K_REQ_MODEL)) or ""
        resp_raw = _label(attrs.get(_K_RESP_MODEL)) or ""
        requested = normalize_copilot_model(req_raw) if req_raw else None
        served = normalize_copilot_model(resp_raw) if resp_raw else None
        model_raw = resp_raw or req_raw
        cm = served if served is not None and served.model else (requested or served)
        routing = "direct"
        if requested is not None and requested.routing != "direct":
            routing = requested.routing
        if cm is not None and cm.routing == "auto":
            routing = "auto"
        model = cm.model if cm is not None else ""
        speed = cm.speed if cm is not None else "standard"
        nano = self._nano(attrs)
        billable: bool | None = True
        rule: str | None = None
        interaction = _first(attrs, _INITIATOR_KEYS)
        background = isinstance(interaction, str) and \
            interaction.strip().lower() in _BACKGROUND_VALUES
        endpoint = attrs.get(_ENDPOINT_TYPE)
        byok = any(_truthy(attrs.get(k)) for k in _BYOK_KEYS) or (
            isinstance(endpoint, str) and endpoint.strip().lower() == "byok")
        if byok:
            billable, rule = False, BYOK_RULE
        elif nano == 0 and (model in self.utility_models or background):
            billable, rule = False, UTILITY_RULE
        if self.billing_path is None:
            self.billing_path = billing_path_of(opts, self.scan, "copilot_pool")
        elif opts.attribution.billing_path not in COPILOT_BILLING_PATHS:
            self.scan.note(DQ_BILLING_PATH_ASSUMED)
        session_key, lane_key, _kind, query_source = self._lane_keys(span)
        attribution = self._attribution(span, query_source, product, self.billing_path)
        conv = self._conversation(span)
        response_id = _label(attrs.get(_K_RESP_ID), 256)
        turn = _int(attrs.get(_K_TURN))
        if response_id is not None:
            request_id = request_id_for("github", response_id, self.source_id, span.locator)
        else:
            request_id = stable_id("rq", "github_copilot", conv, "" if turn is None else turn,
                                   span.span_id or span.locator)
        pricing = PricingContext(
            provider="github", channel="github_copilot", model=model, model_raw=model_raw,
            speed=speed, billing_path=self.billing_path, routing=routing,
            compliance=self.compliance, context_tier=self._context_tier(attrs, model))
        cost_nano = nano_aiu_to_nano(nano)[0] if nano is not None else None
        inference = Inference(
            inference_id=stable_id("inf", request_id, 0, 0), kind=InferenceKind.MESSAGE,
            usage=buckets, pricing=pricing, usage_source=UsageSource.FINAL, billable=billable,
            billing_rule_id=rule, provider_reported_cost_nano=cost_nano,
            provider_reported_cost_basis="provider_estimate" if cost_nano is not None else None)
        end = span.end_ms if span.end_ms is not None and span.end_ms >= span.start_ms else None
        ttft = _int(_first(attrs, _TTFT_KEYS))
        error_type = _label(attrs.get(_K_ERROR_TYPE), 64) if span.error else None
        attempt = Attempt(
            attempt_id=stable_id("at", request_id, 0), attempt_no=0, ts_start_ms=span.start_ms,
            ttft_ms=ttft, duration_ms=end - span.start_ms if end is not None else None,
            outcome=Outcome.HTTP_ERROR if span.error else Outcome.OK, http_status=None,
            error_type=("error" if span.error and error_type is None else error_type),
            retry_layer=None, retry_after_ms=None, should_retry=None, provider_request_id=None,
            provider_message_id=response_id, model_served=model or None, stop_reason=None,
            inferences=(inference,),
            raw_usage_json=json.dumps(raw, sort_keys=True, separators=(",", ":")),
            convention_id=CONVENTION)
        effort = attrs.get(_K_EFFORT)
        effort = effort.strip().lower() if isinstance(effort, str) and \
            _EFFORT_RE.match(effort.strip().lower()) else None
        max_tokens = _int(attrs.get(_K_MAX_TOKENS))
        if requested is not None and requested.pseudo == "auto_unattributed":
            requested_id = "auto"
        elif requested is not None and requested.model:
            requested_id = requested.model
        else:
            requested_id = model
        params = RequestParams(model_requested=requested_id, max_tokens=max_tokens, effort=effort)
        ref = SourceRef(adapter=self.adapter, source_id=self.source_id, locator=span.locator,
                        fidelity=Fidelity.NO_TTL_SPLIT, priority=self.priority)
        self.session_attr.setdefault(session_key, attribution)
        window = self.session_span.setdefault(session_key, [span.start_ms, end or span.start_ms])
        window[0] = min(window[0], span.start_ms)
        window[1] = max(window[1], end or span.start_ms)

        def build(seq: int) -> Request:
            return Request(request_id=request_id, session_key=session_key, lane_key=lane_key,
                           seq=seq, attribution=attribution, params=params, attempts=(attempt,),
                           source=ref)

        return _Draft(sort_key=(span.start_ms, request_id), request_id=request_id,
                      session_key=session_key, lane_key=lane_key, ts_start=span.start_ms,
                      ts_end=end or span.start_ms, build=build)

    # ---------- events ----------
    def _span_events(self, span: SpanView) -> None:
        for ev in span.events:
            name = ev.name.strip() if isinstance(ev.name, str) else ""
            short = name[len("github.copilot."):] if name.startswith("github.copilot.") else name
            if short.startswith("session.compaction_") and short != "session.compaction_complete":
                self.scan.count("compaction_other_events")
                continue
            kind = _EVENT_KINDS.get(short)
            if kind is None:
                continue
            ts = ev.ts_ms if ev.ts_ms is not None else (span.end_ms or span.start_ms)
            if ts is None or not self.scan.in_window(ts):
                continue
            _session, lane_key, _k, _q = self._lane_keys(span, include_self=True)
            self._session_seen(span)
            attrs = self._event_attrs(kind, ev.attributes)
            self.events.append(LaneEvent(lane_key=lane_key, ts_ms=ts, kind=kind, attrs=attrs))

    @staticmethod
    def _event_attrs(kind: LaneEventKind, raw: Mapping[str, Any]
                     ) -> tuple[tuple[str, str | int | bool | None], ...]:
        out: dict[str, str | int | bool | None] = {}
        if kind is LaneEventKind.COMPACTION:
            trig = _first(raw, _EV_TRIGGER)
            trig = trig.strip().lower() if isinstance(trig, str) else None
            out["trigger"] = "manual" if trig == "manual" else "auto"
            if trig in _COPILOT_TRIGGERS:
                out["copilot_trigger"] = trig
            for name, keys in (("pre_tokens", _EV_PRE), ("post_tokens", _EV_POST),
                               ("system_tokens", _EV_SYSTEM),
                               ("tool_definitions_tokens", _EV_TOOLS),
                               ("duration_ms", _EV_DURATION)):
                n = _int(_first(raw, keys))
                if n is not None:
                    out[name] = n
        elif kind is LaneEventKind.CONTEXT_EDIT:
            out["edit_type"] = "copilot_truncation"
            n = _int(_first(raw, _EV_REMOVED))
            if n is not None:
                out["cleared_input_tokens"] = n
        return tuple(sorted(out.items()))

    def _session_seen(self, span: SpanView) -> None:
        session_key = copilot_session_key(self._conversation(span))
        ts = span.start_ms if span.start_ms is not None else span.end_ms
        if ts is None:
            return
        window = self.session_span.setdefault(session_key, [ts, ts])
        window[0] = min(window[0], ts)
        window[1] = max(window[1], span.end_ms if span.end_ms is not None else ts)

    def _cost_states(self, agents: list[SpanView]) -> None:
        totals: dict[str, list[int]] = {}
        lanes: dict[str, str] = {}
        for span in agents:
            if self._agents(span):     # nested invoke_agent: never summed (double count)
                continue
            nano = self._nano(span.attributes)
            ts = span.end_ms if span.end_ms is not None else span.start_ms
            if nano is None or ts is None or not self.scan.in_window(ts):
                continue
            session_key, _lane, _k, _q = self._lane_keys(span)
            self._session_seen(span)
            lanes[session_key] = copilot_lane_key(session_key, LaneKind.MAIN.value, None)
            entry = totals.setdefault(session_key, [0, ts])
            entry[0] += nano
            entry[1] = max(entry[1], ts)
        for session_key in sorted(totals):
            nano, ts = totals[session_key]
            self.events.append(LaneEvent(
                lane_key=lanes[session_key], ts_ms=ts, kind=LaneEventKind.COST_STATE,
                attrs=(("reported_total_nano", nano_aiu_to_nano(nano)[0]),
                       ("reporter", REPORTER_INVOKE_AGENT))))

    # ---------- run ----------
    def run(self, spans: Iterable[SpanView]) -> tuple[list[Request], list[Session],
                                                      list[LaneEvent], frozenset[str]]:
        ordered = sorted(spans, key=lambda s: (s.start_ms or 0, s.span_id or "", s.locator))
        for span in ordered:
            if span.trace_id is not None and span.span_id is not None:
                self.index.setdefault((span.trace_id, span.span_id), span)
        native_traces = {s.trace_id for s in ordered
                         if self._op(s) == "chat" and s.trace_id is not None and self._native(s)}
        drafts: dict[str, _Draft] = {}
        agents: list[SpanView] = []
        for span in ordered:
            op = self._op(span)
            product = span.agent_product or "copilot_other"
            if op == "invoke_agent":
                agents.append(span)
            if op != "chat":
                if product == "copilot_jetbrains" and op not in _OPERATIONS:
                    self.scan.note(DQ_JETBRAINS_UNMAPPED)
                self._span_events(span)
                continue
            if product == "copilot_jetbrains" and not any(k in span.attributes
                                                          for k in _USAGE_KEYS):
                self.scan.note(DQ_JETBRAINS_UNMAPPED)
                continue
            if any(k in span.attributes for k in _LEGACY_KEYS) and not any(
                    k in span.attributes for k in (_K_READ, _K_CREATE)):
                tokens = sum(n for k in (_K_IN, _K_OUT)
                             if (n := _int(span.attributes.get(k))) is not None)
                self.scan.note(DQ_LEGACY_NAMES, tokens=tokens)
                continue
            if self._synthesized(span):
                if span.trace_id in native_traces:
                    self.scan.note(DQ_SYNTH_SKIPPED)
                    continue
                self.scan.note(DQ_SYNTH_KEPT)
            if span.start_ms is not None and not self.scan.in_window(span.start_ms):
                self.scan.count("out_of_window")
                continue
            try:
                draft = self._request(span, product)
            except BadUsageError:
                self.scan.quarantine(span.locator, "bad_usage")
                continue
            if draft is None:
                continue
            if draft.request_id in drafts:
                self.scan.count("duplicate_spans")
                continue
            drafts[draft.request_id] = draft
            self._span_events(span)
        self._cost_states(agents)
        requests = self._sequence(list(drafts.values()))
        sessions = self._sessions()
        events = sorted(self.events, key=lambda e: (
            e.lane_key, e.ts_ms, e.kind.value, tuple((k, repr(v)) for k, v in e.attrs)))
        caps: set[str] = set()
        if requests:
            caps |= {"usage_sequence", "timing", "params", "credits"}
        if events:
            caps.add("events")
            if any(e.kind is LaneEventKind.COST_STATE for e in events):
                caps.add("credits")
        return requests, sessions, events, frozenset(caps) & self.allowed_caps

    def _native(self, span: SpanView) -> bool:
        service = span.resource.get("service.name")
        if service is not None:
            return service == "github-copilot"
        return self.dialect == "vscode-db" and _label(span.attributes.get(_K_RESP_ID), 256) \
            is not None

    def _synthesized(self, span: SpanView) -> bool:
        """A chat span VS Code synthesized for the in-editor CLI agent (no response id, usage
        present, not an error; service ``copilot-chat`` or a VS Code DB row)."""
        service = span.resource.get("service.name")
        if service is not None and service != "copilot-chat":
            return False
        if service is None and self.dialect != "vscode-db":
            return False
        if span.error or _label(span.attributes.get(_K_RESP_ID), 256) is not None:
            return False
        return any(span.attributes.get(k) is not None for k in (_K_IN, _K_OUT))

    def _sequence(self, drafts: list[_Draft]) -> list[Request]:
        by_lane: dict[str, list[_Draft]] = {}
        for d in drafts:
            by_lane.setdefault(d.lane_key, []).append(d)
        out: list[Request] = []
        for lane_key in sorted(by_lane):
            for seq, d in enumerate(sorted(by_lane[lane_key], key=lambda x: x.sort_key)):
                out.append(d.build(seq))
        out.sort(key=lambda r: (r.session_key, r.lane_key, r.ts_start_ms, r.seq, r.request_id))
        return out

    def _sessions(self) -> list[Session]:
        by_session: dict[str, list[Lane]] = {}
        for lane_key in sorted(self.shells):
            session_key, kind, parent = self.shells[lane_key]
            by_session.setdefault(session_key, []).append(Lane(
                lane_key=lane_key, session_key=session_key, kind=kind, parent_lane_key=parent,
                cache_scope_key="unknown", requests=()))
        sessions: list[Session] = []
        for session_key in sorted(by_session):
            start, end = self.session_span.get(session_key, [0, 0])
            attribution = self.session_attr.get(session_key, self.opts.attribution)
            sessions.append(Session(session_key=session_key, source_kind=self.source_kind,
                                    attribution=attribution, lanes=tuple(by_session[session_key]),
                                    started_ms=start, ended_ms=max(start, end)))
        return sessions


def opts_version_ok(version: str) -> bool:
    """A client version string made of version characters only (``1.0.64``, ``0.35.3-insiders``)."""
    return re.fullmatch(r"[0-9A-Za-z.+_-]{1,32}", version) is not None


def run_mapping(spans: Iterable[SpanView], scan: Scan, *, dialect: str,
                allowed_caps: frozenset[str], principal_key_id: str | None = None,
                aggregates: Sequence[UsageAggregate] = ()) -> IngestResult:
    """Map *spans* with *scan*'s options and bookkeeping and finish the result (readers' entry
    point; *principal_key_id* is the key id a reader already knows, e.g. from an extract's
    ``tokenbill_meta``)."""
    mapper = _Mapper(scan.opts, scan, source_id=scan.source_id, adapter=scan.adapter,
                     dialect=dialect, allowed_caps=allowed_caps)
    if principal_key_id is not None:
        mapper.key_id = principal_key_id
    requests, sessions, events, caps = mapper.run(spans)
    used = any(r.attribution.principal is not None and r.attribution.principal[:2] in ("p_", "c_")
               for r in requests)
    kid = mapper.key_id if used or principal_key_id is not None else None
    return scan.finish(scan.source_info(kid), requests=requests, sessions=sessions, events=events,
                       aggregates=aggregates, capabilities=caps)


def map_chat_spans(spans: Iterable[SpanView], opts: IngestOptions, *, source: SourceInfo,
                   dialect: str) -> IngestResult:
    """Map Copilot client spans to canonical records (module-internal API of CP-OTEL's three
    span readers; see the module docstring for the rules).

    *source* names the source (``source_id``, adapter, digest); its ``principal_key_id`` is
    replaced by the key id of the principals actually emitted (an extract's, else
    ``opts.principal_key_id``). *dialect* ∈ :data:`DIALECTS` sets the source priority (21 for
    OTel files, 22 for the VS Code DB) and the session ``source_kind``."""
    check_options(opts)
    scan = _SpanScan(source, opts)
    allowed = frozenset({"usage_sequence", "timing", "params", "credits", "events"})
    mapper = _Mapper(opts, scan, source_id=source.source_id, adapter=source.adapter,
                     dialect=dialect, allowed_caps=allowed)
    if source.principal_key_id is not None:
        mapper.key_id = source.principal_key_id
    requests, sessions, events, caps = mapper.run(spans)
    used = any(r.attribution.principal is not None and r.attribution.principal[:2] in ("p_", "c_")
               for r in requests)
    info = dataclasses.replace(source, principal_key_id=mapper.key_id if used
                               else source.principal_key_id)
    return scan.finish(info, requests=requests, sessions=sessions, events=events,
                       capabilities=caps)


class _SpanScan(Scan):
    """Bookkeeping for :func:`map_chat_spans` called without a file."""

    def __init__(self, source: SourceInfo, opts: IngestOptions) -> None:
        self.adapter = source.adapter
        self.path = Path(source.adapter)
        self.opts = opts
        self.source_id = source.source_id
        self.quarantined = []
        self.stats = {}
        self._notes = {}


# =============================================================================================
# the OTel file reader (copilot-otel)
# =============================================================================================

def _otlp_value(value: object) -> Any:
    """One OTLP ``AnyValue`` → a scalar (str | int | Decimal | bool), else None."""
    if not isinstance(value, dict):
        return None
    if "stringValue" in value:
        s = value["stringValue"]
        return s if isinstance(s, str) else None
    if "intValue" in value:
        x = value["intValue"]
        if type(x) is int:
            return x
        if isinstance(x, str) and re.fullmatch(r"\s*[+-]?\d{1,20}\s*", x):
            return int(x)
        return None
    if "doubleValue" in value:
        x = value["doubleValue"]
        return x if isinstance(x, (int, Decimal)) and type(x) is not bool else None
    if "boolValue" in value:
        x = value["boolValue"]
        return x if type(x) is bool else None
    return None


def _scalar(value: object) -> Any:
    if isinstance(value, (str, Decimal)) or type(value) in (int, bool):
        return value
    return None


class _AttrFilter:
    """Decode only allowlisted keys of an attribute container; count content keys."""

    def __init__(self, scan: Scan) -> None:
        self.scan = scan

    def otlp(self, items: object, allowed: frozenset[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if not isinstance(items, list):
            return out
        for kv in items[:_MAX_ATTRS]:
            if not isinstance(kv, dict) or not isinstance(kv.get("key"), str):
                continue
            key = kv["key"]
            if key in allowed:
                value = _otlp_value(kv.get("value"))
                if value is not None:
                    out[key] = value
            elif is_content_key(key):
                self.scan.note(DQ_RAW_BODIES)
        return out

    def flat(self, items: object, allowed: frozenset[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if not isinstance(items, dict):
            return out
        for n, (key, raw) in enumerate(items.items()):
            if n >= _MAX_ATTRS:
                break
            if not isinstance(key, str):
                continue
            if key in allowed:
                value = _scalar(raw)
                if value is not None:
                    out[key] = value
            elif is_content_key(key):
                self.scan.note(DQ_RAW_BODIES)
        return out


def _otlp_keys(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    return [kv["key"] for kv in items[:_MAX_ATTRS]
            if isinstance(kv, dict) and isinstance(kv.get("key"), str)]


def _dict(obj: object, key: str) -> dict[str, Any]:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, dict) else {}


def _list(obj: object, key: str) -> list[Any]:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, list) else []


def _error_status(status: object) -> bool:
    if not isinstance(status, dict):
        return False
    return status.get("code") in (2, "2", "ERROR", "STATUS_CODE_ERROR")


_OTLP_KEYS = ("resourceSpans", "resourceLogs", "resourceMetrics")
_OTLP_BLOCKS = (("resourceSpans", "scopeSpans", "spans"), ("resourceLogs", "scopeLogs",
                                                            "logRecords"),
                ("resourceMetrics", "scopeMetrics", "metrics"))


def _js_span_kind(obj: dict[str, Any]) -> str | None:
    """``"otel-js"`` / ``"otel-cli"`` for a span line of an OTel-JS dump, else None."""
    kind = obj.get("type")
    if isinstance(kind, str):
        return "otel-cli" if kind.lower() == "span" else None
    if isinstance(obj.get("spanContext"), dict):
        return "otel-cli"
    if "traceId" in obj and "name" in obj and ("spanId" in obj or "id" in obj):
        if any(k in obj for k in ("hrTime", "_hrTime", "timeUnixNano")):
            return "otel-cli"
        return "otel-js"
    return None


def _js_resource_verdict(obj: dict[str, Any], extra: Iterable[str]) -> bool | None:
    res = _dict(obj, "resource")
    attrs = _dict(res, "attributes")
    if not attrs and not res:
        return None
    service = attrs.get("service.name") if isinstance(attrs.get("service.name"), str) else None
    scope = _dict(obj, "instrumentationScope") or _dict(obj, "instrumentationLibrary")
    scopes = [scope["name"]] if isinstance(scope.get("name"), str) else []
    keys = [k for k in _dict(obj, "attributes") if isinstance(k, str)]
    return is_copilot_resource(service, scopes, keys, extra_service_names=extra)


def _otlp_verdicts(obj: dict[str, Any], extra: Iterable[str]) -> list[bool]:
    verdicts: list[bool] = []
    for top, scope_key, item_key in _OTLP_BLOCKS:
        for block in _list(obj, top):
            res = _dict(block, "resource")
            attrs = _list(res, "attributes")
            service = None
            for kv in attrs:
                if isinstance(kv, dict) and kv.get("key") == "service.name":
                    value = _otlp_value(kv.get("value"))
                    service = value if isinstance(value, str) else None
            scopes: list[str] = []
            keys: list[str] = []
            for sb in _list(block, scope_key):
                scope = _dict(sb, "scope")
                if isinstance(scope.get("name"), str):
                    scopes.append(scope["name"])
                for item in _list(sb, item_key):
                    keys.extend(_otlp_keys(_list(item, "attributes") if isinstance(item, dict)
                                           else None))
            verdicts.append(is_copilot_resource(service, scopes, keys,
                                                extra_service_names=extra))
    return verdicts


def sniff_copilot_otel(head: bytes, *, extra_service_names: Iterable[str] = ()) -> bool:
    """Whether every resource in the complete lines of *head* is a Copilot resource (and there is
    at least one): OTLP/JSON lines and OTel-JS lines alike. A truncated last line is ignored; any
    other non-JSON or non-OTel line makes the file not ours."""
    lines = head.split(b"\n")
    tail_complete = head.endswith(b"\n")
    resources = 0
    for i, raw in enumerate(lines):
        raw = raw.strip()
        if not raw:
            continue
        obj = parse_json_line(raw)
        if obj is None:
            if i == len(lines) - 1 and not tail_complete:
                break
            return False
        if any(k in obj for k in _OTLP_KEYS):
            verdicts = _otlp_verdicts(obj, extra_service_names)
        else:
            verdict = _js_resource_verdict(obj, extra_service_names)
            if verdict is None:
                return False
            verdicts = [verdict]
        if not all(verdicts):
            return False
        resources += len(verdicts)
    return resources > 0


class _OtelReader:
    """One ``read()`` of a Copilot OTel file."""

    def __init__(self, adapter: CopilotOtelAdapter, path: Path, opts: IngestOptions) -> None:
        self.scan = Scan(adapter.name, path, opts)
        self.opts = opts
        self.filter = _AttrFilter(self.scan)
        self.configured = service_products(opts.otel_service_names)
        self.spans: list[SpanView] = []
        self.dialects: list[str] = []
        self.jetbrains_ok = FLAG_JETBRAINS in opts.experimental
        self.cli_ok = FLAG_CLI in opts.experimental

    def run(self) -> IngestResult:
        for line_no, obj in self.scan.records():
            self.scan.count("records")
            if any(k in obj for k in _OTLP_KEYS):
                self._otlp(obj, line_no)
                continue
            kind = _js_span_kind(obj)
            if kind is None:
                self.scan.count("ignored_records")
                continue
            if kind == "otel-cli" and not self.cli_ok:
                self.scan.quarantine(f"line:{line_no}", QUARANTINE_CLI)
                continue
            self._js(obj, line_no, kind)
        dialect = self.dialects[0] if self.dialects else "otel-js"
        return run_mapping(self.spans, self.scan, dialect=dialect,
                           allowed_caps=CopilotOtelAdapter.capabilities)

    # ---------- resources ----------
    def _classify(self, service: str | None, scopes: list[str], keys: list[str]) -> str | None:
        """The agent product of a resource, or None when it is not read (foreign / JetBrains
        without the flag)."""
        if not is_copilot_resource(service, scopes, keys,
                                   extra_service_names=self.opts.otel_service_names):
            self.scan.count("foreign_resources")
            return None
        product = product_for(service, self.configured)
        if product == "copilot_jetbrains" and not self.jetbrains_ok:
            self.scan.count("jetbrains_resources_skipped")
            self.scan.note(DQ_JETBRAINS_EXPERIMENTAL)
            return None
        self.scan.count("copilot_resources")
        return product

    def _dialect(self, name: str) -> None:
        if name not in self.dialects:
            self.dialects.append(name)

    # ---------- OTLP/JSON ----------
    def _otlp(self, obj: dict[str, Any], line_no: int) -> None:
        k = 0
        for top, scope_key, item_key in _OTLP_BLOCKS:
            for block in _list(obj, top):
                res_items = _list(_dict(block, "resource"), "attributes")
                resource = self.filter.otlp(res_items, RESOURCE_KEYS)
                service = resource.get("service.name")
                service = service if isinstance(service, str) else None
                scopes = [sc["name"] for sb in _list(block, scope_key)
                          if isinstance((sc := _dict(sb, "scope")).get("name"), str)]
                items = [item for sb in _list(block, scope_key) for item in _list(sb, item_key)]
                keys = [key for item in items if isinstance(item, dict)
                        for key in _otlp_keys(_list(item, "attributes"))]
                product = self._classify(service, scopes, keys)
                if product is None:
                    continue
                if top != "resourceSpans":
                    self.scan.count("ignored_records", len(items))
                    continue
                self._dialect("otlp-json")
                for item in items:
                    k += 1
                    if isinstance(item, dict):
                        self._otlp_span(item, resource, product, f"line:{line_no}#{k}")

    def _otlp_span(self, span: dict[str, Any], resource: dict[str, Any], product: str,
                   locator: str) -> None:
        attrs = self.filter.otlp(span.get("attributes"), SPAN_KEYS)
        trace, span_id = _hex_id(span.get("traceId")), _hex_id(span.get("spanId"))
        if trace is None or span_id is None:
            self.scan.quarantine(locator, "missing:spanId")
            return
        events = tuple(
            SpanEvent(name=ev["name"], ts_ms=time_ms(ev.get("timeUnixNano")),
                      attributes=self.filter.otlp(ev.get("attributes"), EVENT_KEYS))
            for ev in _list(span, "events")[:_MAX_ATTRS]
            if isinstance(ev, dict) and isinstance(ev.get("name"), str))
        start = time_ms(span.get("startTimeUnixNano"))
        self.spans.append(SpanView(
            name=_op_name(span.get("name")), trace_id=trace, span_id=span_id,
            parent_span_id=_hex_id(span.get("parentSpanId")), start_ms=start,
            end_ms=time_ms(span.get("endTimeUnixNano")) or start, attributes=attrs,
            resource=resource, events=events, locator=locator,
            error=_error_status(span.get("status")), agent_product=product))

    # ---------- OTel-JS dumps and CLI envelopes ----------
    def _js(self, obj: dict[str, Any], line_no: int, kind: str) -> None:
        locator = f"line:{line_no}"
        res_attrs = _dict(_dict(obj, "resource"), "attributes")
        resource = self.filter.flat(res_attrs, RESOURCE_KEYS)
        service = resource.get("service.name")
        service = service if isinstance(service, str) else None
        scope = _dict(obj, "instrumentationScope") or _dict(obj, "instrumentationLibrary")
        scopes = [scope["name"]] if isinstance(scope.get("name"), str) else []
        raw_attrs = _dict(obj, "attributes")
        product = self._classify(service, scopes, [k for k in raw_attrs if isinstance(k, str)])
        if product is None:
            return
        self._dialect(kind)
        ctx = _dict(obj, "spanContext")
        trace = _hex_id(obj.get("traceId")) or _hex_id(ctx.get("traceId"))
        span_id = _hex_id(obj.get("spanId")) or _hex_id(obj.get("id")) \
            or _hex_id(ctx.get("spanId"))
        if trace is None or span_id is None:
            self.scan.quarantine(locator, "missing:spanId")
            return
        parent = _hex_id(obj.get("parentSpanId")) or _hex_id(obj.get("parentId")) \
            or _hex_id(_dict(obj, "parentSpanContext").get("spanId"))
        start = time_ms(obj.get("startTime"))
        if start is None:
            start = time_ms(obj.get("startTimeUnixNano")) or time_ms(obj.get("hrTime")) \
                or time_ms(obj.get("_hrTime")) or time_ms(obj.get("timeUnixNano"))
        end = time_ms(obj.get("endTime")) or time_ms(obj.get("endTimeUnixNano")) or start
        events = []
        for ev in _list(obj, "events")[:_MAX_ATTRS]:
            if not isinstance(ev, dict) or not isinstance(ev.get("name"), str):
                continue
            ts = time_ms(ev.get("time"))
            if ts is None:
                ts = time_ms(ev.get("timeUnixNano")) or time_ms(ev.get("hrTime"))
            events.append(SpanEvent(name=ev["name"], ts_ms=ts,
                                    attributes=self.filter.flat(ev.get("attributes"),
                                                                EVENT_KEYS)))
        self.spans.append(SpanView(
            name=_op_name(obj.get("name")), trace_id=trace, span_id=span_id,
            parent_span_id=parent, start_ms=start, end_ms=end,
            attributes=self.filter.flat(raw_attrs, SPAN_KEYS), resource=resource,
            events=tuple(events), locator=locator, error=_error_status(obj.get("status")),
            agent_product=product))


def _op_name(name: object) -> str:
    """The operation word of a span name (``chat``, ``invoke_agent``, ``execute_tool``) or
    ``"other"``: span names may carry agent / tool names and are never kept."""
    if isinstance(name, str):
        first = name.strip().split(" ", 1)[0]
        if first in _OPERATIONS:
            return first
    return "other"


class CopilotOtelAdapter:
    """GitHub Copilot OTel files (VS Code OTel-JS dumps, OTLP/JSON, experimental CLI envelopes)
    → requests from ``chat`` spans, ``invoke_agent`` COST_STATE totals and lane events."""

    name = ADAPTER_NAME
    capabilities = frozenset({"usage_sequence", "timing", "params", "credits", "events"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """True only when every resource in the head is a Copilot resource (§5.10 claim rule)."""
        return sniff_copilot_otel(head)

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Read the Copilot resources of *path*; foreign resources are counted, not read."""
        return _OtelReader(self, Path(path), opts).run()

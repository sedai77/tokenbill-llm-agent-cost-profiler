"""Claude Code transcript importer (SPEC §5.3, §5.1, D4, D26, D27, D40).

Reads ``~/.claude/projects/<slug>/<session>.jsonl`` (MAIN lanes), ``…/subagents/agent-*.jsonl``
(SUBAGENT lanes, with ``agent-*.meta.json``) and ``…/workflows/…`` agent files (WORKFLOW_AGENT
lanes) into the canonical, content-free ledger. The transcript format is internal to Claude Code
and version dependent: the importer degrades by quarantine and data-quality counters, never by
crashing.

The empirical traps it gets right:

* one billed API response is written as ~2.4 lines sharing ``message.id``; lines are grouped by id
  and the line with the **maximum** ``usage.output_tokens`` is kept (naive summing overstates spend
  ~2.33×; :attr:`IngestResult.naive_usage` keeps the naive sum per model for that self-check);
* duplicate ``(file, uuid)`` lines are dropped; ``<synthetic>`` messages are skipped;
* a call whose every line has a null ``stop_reason``, ≤ 20 output tokens and is answered by a
  ``tool_result`` is ``MESSAGE_START_ONLY``: its logged output is a lower bound and
  ``Inference.output_upper`` carries an upper estimate **in tokens**;
* the 5m/1h cache-write split, ``usage.iterations`` (refusal rule), diagnostics, per-turn effort,
  quota state and the lane structure are preserved.

Only numbers, enums, allowlisted names and HMAC ids leave this module (content tier ``none``
only): text, tool inputs and results, paths and branch names are reduced to byte lengths or
``h_`` pseudonyms under the name key before any record is built.

The parser is resumable: :mod:`tokenbill.adapters.cc_collect` feeds it a byte offset and a
content-free context (the state at that offset), so incremental collection and a one-shot import
derive every record identically.
"""

from __future__ import annotations

import dataclasses
import gc
import hashlib
import json
import logging
import math
import operator
import os
import re
from collections import Counter, OrderedDict, deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core import jsonl
from tokenbill.core.conventions import (
    ANTHROPIC_MESSAGES,
    BadUsageError,
    anthropic_inferences,
    normalize_anthropic_messages,
)
from tokenbill.core.errors import ContractViolation, SourceError, UsageError
from tokenbill.core.evidence import CPT_DEFAULT_47PLUS_TOOL_OUTPUT, CPT_DEFAULT_LEGACY
from tokenbill.core.facts import load as load_facts
from tokenbill.core.ids import is_opaque_ref, key_id, pseudonym, request_id_for, stable_id
from tokenbill.core.models import normalize_model
from tokenbill.core.money import decimal_to_nano
from tokenbill.core.records import (
    DIAG_REASONS,
    MAX_TOKENS,
    AppendedItem,
    Attempt,
    Attribution,
    CacheDiagnostic,
    ContentTier,
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
    UsageBuckets,
    UsageSource,
    from_json,
    to_json,
)
from tokenbill.core.secrets import find_secrets
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    SourceInfo,
)

__all__ = [
    "ADAPTER_NAME",
    "BUILTIN_TOOLS",
    "CAPABILITIES",
    "ClaudeCodeAdapter",
    "ContextError",
    "Identity",
    "NameHasher",
    "iter_claude_files",
    "parse_ts_ms",
    "resolve_identity",
    "token",
]

logger = logging.getLogger("tokenbill.adapters.claude_code")

ADAPTER_NAME = "claude-code"
SOURCE_KIND = "claude-code"
SOURCE_PRIORITY = 40
#: Capabilities the importer can provide (SPEC §5.3); a result carries the subset its data shows.
CAPABILITIES = frozenset({"usage_sequence", "timing", "ttl_split", "iterations", "diagnostics",
                          "appended", "events", "human_prompts", "lanes_exact", "params",
                          "quota_state"})
#: Present whenever a result holds at least one request.
_BASE_CAPABILITIES = frozenset({"usage_sequence", "timing", "iterations", "appended", "events",
                                "human_prompts", "lanes_exact", "params"})

#: MESSAGE_START_ONLY: the largest logged output that can be a message_start placeholder (§5.3.6).
MSO_MAX_OUTPUT = 20
#: Complete tool_use outputs remembered per (model, lane kind) for the MSO median.
STATS_WINDOW = 256
#: tool_use id → tool name entries remembered per file (tool_result names). Results answer the
#: tool uses of the last few calls; the bound keeps the collector state file small.
TOOL_NAME_WINDOW = 128
#: Sliding window of uuids for duplicate-line detection (the collector cursor's bound, §5.3).
UUID_WINDOW = 2000
#: requestIds remembered per read for the collision check (collisions are between nearby calls;
#: the store detects the rest, §7.3).
REQUEST_ID_WINDOW = 4096
#: Closed message groups remembered per file (a later line of the same id re-opens it).
CLOSED_WINDOW = 16
#: Latest requests scanned for a re-opened message before an id → index map is built.
_RECENT_SCAN = 256
#: ``cleanupPeriodDays`` default (CC-SETTINGS) and the warning margin (§5.3.12).
RETENTION_DEFAULT_DAYS = 30
RETENTION_MARGIN_DAYS = 3
#: The fallback block and the model_refusal_fallback entry of one fallback are this close.
FALLBACK_DEDUPE_MS = 60_000
#: Largest ``*.meta.json`` read (bytes); meta files are a few hundred bytes.
_META_MAX_BYTES = 64 * 1024
#: Directory levels above a transcript that decide its lane kind (subagents / workflows).
_LAYOUT_DEPTH = 3
#: Longest source string kept as a cache key (hostile input never pins large strings).
_CACHE_MAX_LEN = 1024
_DAY_MS = 86_400_000
_DETAIL_MAX = 256

#: Built-in Claude Code tool names, emitted in clear (SPEC §5.1). Every other tool name (MCP
#: ``mcp__server__tool``, plugin tools) is an ``h_`` pseudonym unless allowlisted.
BUILTIN_TOOLS = frozenset({
    "Agent", "AskUserQuestion", "Bash", "BashOutput", "Edit", "EnterPlanMode", "ExitPlanMode",
    "Glob", "Grep", "KillBash", "KillShell", "LS", "ListMcpResourcesTool", "LSP", "MultiEdit",
    "NotebookEdit", "NotebookRead", "Read", "ReadMcpResourceTool", "SlashCommand", "Skill", "Task",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate", "TodoRead",
    "TodoWrite", "ToolSearch", "WebFetch", "WebSearch", "Write",
})

_BUILTIN_TOOL_NAMES = {name: name for name in BUILTIN_TOOLS}
#: Billing path → pricing channel (table-driven so additive billing paths need no code change).
_CHANNEL_BY_BILLING_PATH = {"bedrock": "bedrock", "vertex": "vertex", "foundry": "foundry",
                            "claude_platform_aws": "claude_platform_aws"}
#: Billing path while the session reports ``quotaLimits.isUsingOverage = true`` (D26).
_OVERAGE_BILLING_PATH = {"subscription": "usage_credits"}
_ENDPOINT_SCOPES = frozenset({"global", "regional", "multi_region"})
#: HTTP status → API_ERROR ``error_type`` (SPEC §5.3.8); ``error.connection`` → ``connection``.
_ERROR_TYPE_BY_STATUS = {429: "rate_limit", 529: "overloaded", 408: "timeout", 401: "auth",
                         403: "auth", 400: "invalid_request", 413: "prompt_too_long"}
#: Entry types that are recognized but carry nothing to import (never counted as unknown; they
#: do not close message groups either — only user, attachment and system entries do).
_BOOKKEEPING_TYPES = frozenset({"summary", "file-history-snapshot", "queue-operation", "progress",
                                "custom-title", "tag", "agent-name", "ai-title", "last-prompt",
                                "mode", "worktree-state", "pr-link"})
_COST_STATE_KEYS = ("totalCostUSD", "totalCostUsd", "total_cost_usd", "costUSD")
_ATTACHMENT_SIZE_KEYS = ("content", "addedLines", "addedBlocks", "addedNames", "entries")
_QUERY_SOURCE = {LaneKind.MAIN: "main", LaneKind.SUBAGENT: "subagent",
                 LaneKind.WORKFLOW_AGENT: "subagent", LaneKind.COMPACTION: "compaction"}
_SEVERITY = {
    "dq.duplicate_uuid_lines": "info", "dq.synthetic_skipped": "info",
    "dq.split_usage_mismatch": "warn", "dq.message_start_only": "info",
    "dq.hidden_compaction_estimated": "info", "dq.rollup_not_spend": "info",
    "dq.iterations_mismatch": "warn", "dq.ttl_split_residual": "info",
    "dq.ttl_split_exceeds_total": "warn", "dq.sum_check_failed": "warn",
    "dq.request_id_collision": "warn", "dq.quarantined": "warn", "dq.secrets_observed": "warn",
    "dq.subscription_allowance": "info", "dq.unpriced_model": "warn",
    "dq.version_histogram": "info", "dq.unknown_entry_type": "info",
    "dq.retention_warning": "warn", "dq.unknown_fields": "info",
    "dq.headless_output_residual": "warn", "dq.headless_resumed_totals": "warn",
    "dq.headless_zeroed_result": "warn",
}
_DETAILS = {
    "dq.duplicate_uuid_lines": "duplicate (file, uuid) lines dropped",
    "dq.synthetic_skipped": "<synthetic> assistant messages skipped (never billed)",
    "dq.split_usage_mismatch": "split entries of one message disagree outside output; the "
                               "max-output entry was kept",
    "dq.message_start_only": "calls whose logged output is the message_start placeholder; "
                             "tokens = sum of (output_upper - logged)",
    "dq.hidden_compaction_estimated": "compaction calls reconstructed from compact_boundary "
                                      "(estimated, never in the exact bill); tokens = pre + post",
    "dq.rollup_not_spend": "toolUseResult.totalTokens roll-ups ignored (not spend)",
    "dq.iterations_mismatch": "usage.iterations disagree with the top-level usage",
    "dq.ttl_split_residual": "cache writes without a TTL split (cache_write_unknown)",
    "dq.ttl_split_exceeds_total": "TTL split exceeds cache_creation_input_tokens",
    "dq.sum_check_failed": "usage sub-counts exceed their totals",
    "dq.request_id_collision": "one requestId maps to several message ids; not used as a join key",
    "dq.quarantined": "records quarantined (see quarantined; reasons are content-free)",
    "dq.subscription_allowance": "requests on the seat allowance (list-equivalent, never billed)",
    "dq.unpriced_model": "inferences without a priceable model",
    "dq.unknown_fields": "unrecognized values dropped",
    "dq.headless_output_residual": "negative per-model output residuals dropped (result output "
                                   "below the logged step outputs); tokens = dropped magnitude",
    "dq.headless_resumed_totals": "result totals include earlier (resumed) spend; the output "
                                  "residual was skipped",
    "dq.headless_zeroed_result": "zeroed result (crash) with non-zero steps; the output residual "
                                 "was skipped",
}


# ---------------------------------------------------------------------------------------------
# small shared helpers (also used by cc_headless and cc_collect)
# ---------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+/\[\]-]{0,63}\Z")


#: token value → its canonical (shared) string, or "" when it is not a token.
_TOKENS: dict[str, str] = {}


def token(value: object, limit: int = 64) -> str | None:
    """*value* when it is a short identifier-like string (≤ *limit* chars of ``[A-Za-z0-9_.:+/-]``
    and brackets, no spaces or ``@``), else None. Every provider string copied into a record goes
    through this guard, so no record carries free text (SPEC §8.1). Repeated values (models,
    versions, stop reasons) come back as one shared string object."""
    if type(value) is not str or len(value) > 64:
        return None
    canon = _TOKENS.get(value)
    if canon is None:
        canon = value if _TOKEN_RE.match(value) is not None else ""
        if len(_TOKENS) < 8192:
            _TOKENS[value] = canon
    return canon if canon and len(canon) <= limit else None


_TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:[.,]([0-9]{1,9}))?"
    r"(Z|z|[+-][0-9]{2}(?::?[0-9]{2})?)?\Z")


_DAYS: dict[str, int] = {}
_TS_FAST = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z\Z")


_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _valid_date(y: int, m: int, d: int) -> bool:
    """A real calendar date (``2026-02-31`` is not)."""
    if not (1 <= m <= 12 and d >= 1):
        return False
    leap = m == 2 and y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    return d <= _DAYS_IN_MONTH[m - 1] + leap


def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_ts_ms(value: object) -> int | None:
    """ISO-8601 timestamp (``2026-09-22T10:00:00.123Z``, offsets allowed) or integer epoch
    milliseconds → int ms since the epoch (UTC); None when absent, malformed or out of range."""
    if type(value) is int:
        return value if 0 <= value <= MAX_TOKENS else None
    if not isinstance(value, str) or not 19 <= len(value) <= 40:
        return None
    fast = _TS_FAST.match(value)
    if fast is not None:  # the common ``YYYY-MM-DDTHH:MM:SS.mmmZ`` form
        days = _DAYS.get(value[:10])
        if days is None:
            y, mo, d = int(value[0:4]), int(value[5:7]), int(value[8:10])
            if not _valid_date(y, mo, d):
                return None
            days = _days_from_civil(y, mo, d)
            if len(_DAYS) < 100_000:
                _DAYS[value[:10]] = days
        hh, mm, ss, ms = (int(value[11:13]), int(value[14:16]), int(value[17:19]),
                          int(value[20:23]))
        if hh >= 24 or mm >= 60 or ss >= 61:
            return None
        total = ((days * 24 + hh) * 60 + mm) * 60_000 + ss * 1000 + ms
        return total if 0 <= total <= MAX_TOKENS else None
    m = _TS_RE.match(value)
    if m is None:
        return None
    y, mo, d, hh, mm, ss = (int(m.group(i)) for i in range(1, 7))
    if not (_valid_date(y, mo, d) and hh < 24 and mm < 60 and ss < 61):
        return None
    frac = m.group(7)
    ms = int((frac + "00")[:3]) if frac else 0
    tz = m.group(8)
    offset_min = 0
    if tz and tz not in ("Z", "z"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        offset_min = sign * (int(digits[:2]) * 60 + (int(digits[2:4]) if len(digits) > 2 else 0))
    days = _days_from_civil(y, mo, d)
    total = ((days * 24 + hh) * 60 + mm - offset_min) * 60_000 + ss * 1000 + ms
    return total if 0 <= total <= MAX_TOKENS else None


def _int(value: object) -> int | None:
    """A non-negative JSON integer (bools excluded) within the token range, else None."""
    return value if type(value) is int and 0 <= value <= MAX_TOKENS else None


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _json_nbytes(value: Any) -> int:
    try:
        return _nbytes(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError, RecursionError):
        return 0


@dataclass(frozen=True)
class Identity:
    """The principal attached to every request of a read (SPEC §5.1, §5.4, D5)."""

    principal: str | None
    principal_key_id: str | None


def resolve_identity(opts: IngestOptions) -> Identity:
    """``central`` → ``r_<ref>``; ``two-stage`` → ``c_`` HMAC with the principal (collection) key;
    ``install`` / ``central-ingest`` → ``p_`` HMAC with the principal key. A ref must be opaque
    (``[A-Za-z0-9._-]{1,64}``, never an email): anything else raises :class:`UsageError`. Without a
    ref the caller's ``opts.attribution.principal`` is kept."""
    mode = opts.identity_mode
    ref = opts.principal_ref
    if mode not in ("central", "two-stage", "install", "central-ingest"):
        raise UsageError(f"unknown identity mode {str(mode)[:32]!r}")
    if ref is None:
        return Identity(opts.attribution.principal, None)
    if not is_opaque_ref(ref):
        raise UsageError("principal ref must match [A-Za-z0-9._-]{1,64} (never an email)")
    if mode == "central":
        return Identity("r_" + ref, None)
    key = opts.principal_key
    if not key:
        raise UsageError(f"identity mode {mode} needs a principal key")
    prefix = "c" if mode == "two-stage" else "p"
    return Identity(pseudonym(key, prefix, ref), opts.principal_key_id or key_id(key))


class NameHasher:
    """Clear-or-``h_`` naming with the name key (SPEC §5.1): allowlisted names stay in clear,
    everything else becomes ``pseudonym(name_key, "h", value)``. Without a name key hashed values
    are dropped (None)."""

    def __init__(self, opts: IngestOptions) -> None:
        self.key = opts.name_key or b""
        self.key_id = (opts.name_key_id or key_id(self.key)) if self.key else None
        self.allow = frozenset(opts.name_allowlist)
        self.used = False
        self._cache: dict[str, str | None] = {}
        self._hashed: dict[str, str] = {}

    def hashed(self, value: str) -> str | None:
        """``h_`` pseudonym of *value* (never clear)."""
        if not self.key:
            return None
        self.used = True
        hit = self._hashed.get(value)
        if hit is None:
            hit = pseudonym(self.key, "h", value)
            if len(self._hashed) < 4096 and len(value) <= _CACHE_MAX_LEN:
                self._hashed[value] = hit
        return hit

    def name(self, value: object) -> str | None:
        """A skill/MCP/plugin/tool name: clear when allowlisted (and short), else ``h_``."""
        if not isinstance(value, str) or not value:
            return None
        hit = self._cache.get(value, "")
        if hit != "":
            if hit is not None and hit.startswith("h_"):
                self.used = True
            return hit
        if value in self.allow and token(value) is not None:
            out: str | None = value
        else:
            out = self.hashed(value)
        if len(self._cache) < 4096 and len(value) <= _CACHE_MAX_LEN:
            self._cache[value] = out
        return out

    def tool(self, value: object) -> str | None:
        """A tool name: built-in Claude Code tools in clear, others through :meth:`name`."""
        if isinstance(value, str):
            builtin = _BUILTIN_TOOL_NAMES.get(value)
            if builtin is not None:
                return builtin
        return self.name(value)


def mcp_server_of(tool_name: object) -> str | None:
    """The server part of an MCP tool name ``mcp__<server>__<tool>`` (None otherwise)."""
    if isinstance(tool_name, str) and tool_name.startswith("mcp__"):
        server, sep, _ = tool_name[5:].partition("__")
        if sep and server:
            return server
    return None


def safe_usage_json(usage: Mapping[str, Any]) -> str | None:
    """Canonical JSON of a provider usage object as the trace@2 ``raw_usage`` allows it (§4.2):
    integers in ``[0, 2**53]``, bools and nulls; strings only for ``service_tier``, ``speed``,
    ``inference_geo`` and ``iterations[].{type, model}`` (short tokens, else null); any other
    string, float or out-of-range number becomes null. None when larger than 8 KiB."""
    if _usage_is_clean(usage):
        text = json.dumps(usage, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return text if len(text) <= 8 * 1024 else None
    return _safe_usage_json_slow(usage)


_USAGE_KEYS = frozenset({"input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                         "output_tokens", "service_tier", "inference_geo", "speed",
                         "cache_creation", "server_tool_use", "output_tokens_details"})
_USAGE_INT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                   "output_tokens")
_USAGE_STRING_KEYS = frozenset({"service_tier", "inference_geo", "speed"})
_ITERATION_STRING_KEYS = frozenset({"type", "model"})
_USAGE_NESTED_KEYS = frozenset({"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
                                "web_search_requests", "web_fetch_requests", "thinking_tokens"})


def _raw_int(v: Any) -> bool:
    return type(v) is int and 0 <= v <= MAX_TOKENS


def _usage_is_clean(usage: Mapping[str, Any]) -> bool:
    """True for the documented usage shape with valid values (serialized as is, the hot path);
    anything else goes through :func:`_safe_usage_json_slow`."""
    if type(usage) is not dict or not (usage.keys() <= _USAGE_KEYS):
        return False
    for key in _USAGE_INT_KEYS:
        v = usage.get(key)
        if v is not None and not _raw_int(v):
            return False
    for key in _USAGE_STRING_KEYS:
        v = usage.get(key)
        if v is not None and token(v) is None:
            return False
    for key in ("cache_creation", "server_tool_use", "output_tokens_details"):
        v = usage.get(key)
        if v is None:
            continue
        if type(v) is not dict or not (v.keys() <= _USAGE_NESTED_KEYS):
            return False
        for x in v.values():
            if x is not None and not _raw_int(x):
                return False
    return True


def _safe_usage_json_slow(usage: Mapping[str, Any]) -> str | None:
    def scalar(v: Any) -> Any:
        return v if v is None or type(v) is bool or _raw_int(v) else None

    def obj(o: Mapping[str, Any], depth: int, strings: frozenset[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in list(o.items())[:64]:
            if not isinstance(k, str) or token(k) is None:
                continue
            if isinstance(v, str):
                out[k] = token(v) if k in strings else None
            elif isinstance(v, Mapping):
                out[k] = obj(v, depth + 1, frozenset()) if depth < 4 else None
            elif isinstance(v, list):
                if k == "iterations" and depth == 0:
                    out[k] = [obj(x, depth + 1, _ITERATION_STRING_KEYS)
                              if isinstance(x, Mapping) else None for x in v[:64]]
                else:
                    out[k] = [scalar(x) for x in v[:64]]
            else:
                out[k] = scalar(v)
        return out

    if not isinstance(usage, Mapping):
        return None
    try:
        text = json.dumps(obj(usage, 0, _USAGE_STRING_KEYS), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return text if len(text) <= 8 * 1024 else None


def decimal_usd_to_nano(value: object) -> int | None:
    """A USD amount parsed as ``Decimal`` (or int) → int nano-USD (half-even); None otherwise.
    Money is never a float: callers parse JSON with ``parse_float=Decimal``."""
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return None
    try:
        nano = decimal_to_nano(value)
    except (ValueError, ArithmeticError):
        return None
    return nano if nano <= MAX_TOKENS else None


def loads_decimal(text: str | bytes) -> Any:
    """``json.loads`` with floats as ``Decimal`` and NaN/Infinity refused (money-safe)."""

    def refuse(tok: str) -> Any:
        raise ValueError("non-finite number")

    if isinstance(text, bytes):
        text = text.decode("utf-8")
    return json.loads(text, parse_float=Decimal, parse_constant=refuse)


#: Every ``core.secrets`` pattern contains one of these literals or (the entropy detector) a run of
#: 32 base64/hex characters, so text matching neither cannot contain a reported secret.
_SECRET_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32}")


def _may_hold_secret(text: str) -> bool:
    return ("sk-" in text or "AKIA" in text or "ghp_" in text or "github_pat_" in text
            or "xox" in text or "eyJ" in text or "PRIVATE KEY" in text
            or _SECRET_RUN.search(text) is not None)
_CPT_CACHE: dict[str, tuple[int, int]] = {}


def _cpt_ratio(model: str) -> tuple[int, int]:
    """Bytes per token of the model's tokenizer family as an integer ratio (num, den)."""
    hit = _CPT_CACHE.get(model)
    if hit is not None:
        return hit
    families = {row.tokenizer_family for row in load_facts().rows_for(model)} if model else set()
    const = CPT_DEFAULT_LEGACY if families == {"claude-legacy"} else CPT_DEFAULT_47PLUS_TOOL_OUTPUT
    value = const.value
    ratio = Decimal(str(value)).as_integer_ratio() if not isinstance(value, tuple) else (5, 2)
    _CPT_CACHE[model] = ratio
    return ratio


def ceil_tokens(n_bytes: int, model: str) -> int:
    """``ceil(n_bytes / CPT_DEFAULT_*)`` for the model's tokenizer family (integer arithmetic)."""
    num, den = _cpt_ratio(model)
    return -(-n_bytes * den // num)


# ---------------------------------------------------------------------------------------------
# file discovery and layout
# ---------------------------------------------------------------------------------------------

def iter_claude_files(root: Path) -> Iterator[Path]:
    """Every ``*.jsonl`` transcript under *root* (or *root* itself when it is a file), in sorted
    order, skipping every ``journal.jsonl``. ``*.meta.json`` side files are read by the adapter
    next to their transcript; ``workflows/*.json`` roll-ups are never read."""
    root = Path(root)
    if root.is_file():
        if root.name != "journal.jsonl":
            yield root
        return
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.jsonl")):
        if path.name == "journal.jsonl" or not path.is_file():
            continue
        yield path


@dataclass(frozen=True)
class FileLayout:
    """What a transcript's path says about its lanes (SPEC §5.3 Layout)."""

    kind: LaneKind
    agent_id: str | None          # from agent-<id>.jsonl
    session_hint: str | None      # file stem (main) or parent session directory (subagents)
    meta: Mapping[str, Any]


def _read_meta(path: Path) -> dict[str, Any]:
    meta_path = path.with_name(path.name[: -len(".jsonl")] + ".meta.json") \
        if path.name.endswith(".jsonl") else None
    if meta_path is None or not meta_path.is_file():
        return {}
    try:
        with open(meta_path, "rb") as f:
            raw = f.read(_META_MAX_BYTES + 1)
        if len(raw) > _META_MAX_BYTES:
            return {}
        obj = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, Any] = {}
    for k in ("agentType", "model", "parentAgentId", "toolUseId", "workflowPhase"):
        v = token(obj.get(k))
        if v is not None:
            out[k] = v
    depth = _int(obj.get("spawnDepth"))
    if depth is not None:
        out["spawnDepth"] = depth
    return out


def file_layout(path: Path) -> FileLayout:
    """Lane kind from the path: ``/workflows/`` → WORKFLOW_AGENT, ``/subagents/`` → SUBAGENT,
    else MAIN; plus the ``agent-*.meta.json`` fields SPEC §5.3 allows."""
    parts = Path(path).parts
    name = Path(path).name
    stem = name[: -len(".jsonl")] if name.endswith(".jsonl") else Path(path).stem
    # only the directories nearest the file count (``<session>/subagents/agent-*.jsonl``,
    # ``<session>/workflows/<run>/…``), so an ancestor that happens to be named "workflows"
    # (a home directory, a checkout) never changes the lane kind
    near = len(parts) - 1 - _LAYOUT_DEPTH
    dirs = [(i, p) for i, p in enumerate(parts[:-1]) if i >= near]
    if any(p == "workflows" for _, p in dirs):
        kind, container = LaneKind.WORKFLOW_AGENT, "workflows"
    elif any(p == "subagents" for _, p in dirs):
        kind, container = LaneKind.SUBAGENT, "subagents"
    else:
        kind, container = LaneKind.MAIN, ""
    agent_id = None
    session_hint: str | None = stem
    if kind is not LaneKind.MAIN:
        agent_id = stem[len("agent-"):] if stem.startswith("agent-") else stem
        idx = max(i for i, p in dirs if p == container)
        session_hint = parts[idx - 1] if idx >= 1 else None
    meta = _read_meta(Path(path)) if kind is not LaneKind.MAIN else {}
    return FileLayout(kind=kind, agent_id=agent_id, session_hint=session_hint, meta=meta)


# ---------------------------------------------------------------------------------------------
# per-read accumulator
# ---------------------------------------------------------------------------------------------

class _Run:
    """Everything one ``read`` (one file, a directory, or one collector chunk) accumulates."""

    def __init__(self, opts: IngestOptions, source_id: str, locator_prefix: str = "") -> None:
        self.opts = opts
        self.source_id = source_id
        self.locator_prefix = locator_prefix
        self.identity = resolve_identity(opts)
        self.names = NameHasher(opts)
        base = opts.attribution
        self.base_attr = base
        billing = base.billing_path
        self.billing_path = billing
        extra = dict(base.extra)
        scope = extra.get("endpoint_scope")
        self.endpoint_scope = scope if scope in _ENDPOINT_SCOPES else None
        self.cache_scope_key = "ws:" + (base.workspace_id or "unknown")
        #: the since/until window, or None (the naive self-check sums only lines inside it)
        self.window = None if opts.since_ms is None and opts.until_ms is None else \
            (opts.since_ms, opts.until_ms)
        self.requests: list[Request] = []
        self.events: list[LaneEvent] = []
        self.quarantined: list[QuarantineItem] = []
        self.dq: Counter[str] = Counter()
        self.dq_tokens: Counter[str] = Counter()
        self.stats: Counter[str] = Counter()
        self.naive: dict[str, list[int]] = {}
        self.versions: Counter[str] = Counter()
        self.unknown_types: Counter[str] = Counter()
        self.secrets: Counter[str] = Counter()
        self.rq_first: dict[str, str] = {}
        self.rq_order: deque[str] = deque()
        self.rq_bad: set[str] = set()
        self.shells: dict[str, tuple[str, LaneKind, str | None, bool]] = {}
        self.session_ts: dict[str, list[int]] = {}
        self.saw_split = False
        self.saw_diag = False
        self.saw_quota = False
        self._attr_cache: dict[tuple, Attribution] = {}
        self.attr_by_key: dict[tuple, Attribution] = {}
        self._ctx_cache: dict[tuple, PricingContext] = {}
        self._params_cache: dict[tuple, RequestParams] = {}
        self._model_cache: dict[tuple[str, str | None], Any] = {}

    # --- quarantine / notes ---
    def quarantine(self, offset: int, reason: str, path: Path) -> None:
        locator = f"{self.locator_prefix}offset:{offset}"
        if not self.opts.lenient:
            raise SourceError(f"{path.name}: {locator}: {reason}")
        self.quarantined.append(QuarantineItem(source_id=self.source_id, locator=locator,
                                               reason=reason))
        self.dq["dq.quarantined"] += 1

    # --- interning ---
    def model_id(self, model_raw: str, hint: str | None = None) -> Any:
        key = (model_raw, hint)
        mid = self._model_cache.get(key)
        if mid is None:
            mid = self._model_cache[key] = normalize_model(model_raw, hint)
        return mid

    def pricing(self, model_raw: str, tier: str, speed: str, geo: str | None,
                billing_path: str) -> PricingContext:
        key = (model_raw, tier, speed, geo, billing_path)
        ctx = self._ctx_cache.get(key)
        if ctx is not None:
            return ctx
        channel = _CHANNEL_BY_BILLING_PATH.get(self.billing_path or "")
        mid = self.model_id(model_raw, channel if channel in ("bedrock", "vertex") else None)
        if channel is None:
            channel = mid.channel_hint or "anthropic_api"
        scope = self.endpoint_scope
        if scope is None:
            scope = mid.endpoint_scope if mid.endpoint_scope in _ENDPOINT_SCOPES else "unknown"
        ctx = PricingContext(provider="anthropic", channel=channel, model=mid.model,
                             model_raw=model_raw, service_tier=tier, speed=speed,
                             inference_geo=geo, endpoint_scope=scope, write_ttl_hint=None,
                             billing_path=billing_path)
        self._ctx_cache[key] = ctx
        return ctx

    def attribution(self, **kw: Any) -> Attribution:
        """``opts.attribution`` (every field, so fields added to the contract later carry over)
        with the resolved principal, ``agent_product = "claude_code"`` and the per-request
        values in *kw* (None leaves the default, except for ``billing_path``)."""
        key = tuple(kw.items())
        attr = self._attr_cache.get(key)
        if attr is None:
            fields: dict[str, Any] = {"principal": self.identity.principal,
                                      "agent_product": "claude_code"}
            fields.update({k: v for k, v in kw.items() if v is not None or k == "billing_path"})
            attr = self._attr_cache[key] = dataclasses.replace(self.base_attr, **fields)
        return attr

    def params(self, model_raw: str, effort: str | None, session_effort: str | None,
               advisor: str | None) -> RequestParams:
        key = (model_raw, effort, session_effort, advisor)
        p = self._params_cache.get(key)
        if p is None:
            p = self._params_cache[key] = RequestParams(
                model_requested=model_raw, effort=effort, session_effort=session_effort,
                advisor_model=advisor)
        return p

    def shell(self, lane_key: str, session_key: str, kind: LaneKind, parent: str | None,
              exact: bool = True) -> None:
        self.shells.setdefault(lane_key, (session_key, kind, parent, exact))

    def touch_session(self, session_key: str, ts: int | None) -> None:
        if ts is None:
            return
        span = self.session_ts.get(session_key)
        if span is None:
            self.session_ts[session_key] = [ts, ts]
        else:
            if ts < span[0]:
                span[0] = ts
            if ts > span[1]:
                span[1] = ts

    def add_naive(self, model: str, c: tuple[int, ...], ts: int | None) -> None:
        """Add ``usage_counts`` to the naive (every-line) sum of *model*: lines outside the
        since/until window are left out, like the requests (a line without a timestamp counts)."""
        window = self.window
        if window is not None and ts is not None and (
                (window[0] is not None and ts < window[0])
                or (window[1] is not None and ts >= window[1])):
            return
        acc = self.naive.get(model)
        if acc is None:
            self.naive[model] = list(c)
        else:
            acc[:] = map(operator.add, acc, c)

    def emit_request(self, req: Request, *, at: int | None = None) -> int:
        """Append *req* and return its index; with *at* (a re-opened message whose request this
        run already holds) replace the request at that index instead."""
        requests = self.requests
        if at is not None:
            requests[at] = req
            return at
        requests.append(req)
        return len(requests) - 1

    def count_secrets(self, text: str) -> None:
        """Count likely secrets by type (``dq.secrets_observed``; values never kept). A cheap
        pre-filter that every ``core.secrets`` pattern must match skips clean text."""
        if len(text) < 20 or not _may_hold_secret(text):
            return
        for kind, _s, _e in find_secrets(text):
            self.secrets[kind] += 1


# ---------------------------------------------------------------------------------------------
# parser state
# ---------------------------------------------------------------------------------------------

class _LaneState:
    """Per-lane state carried across lines (and across collector runs, via :meth:`to_ctx`)."""

    __slots__ = ("last_fallback", "last_model", "last_req_ts", "last_version", "last_write_ttl",
                 "pending", "trigger")

    def __init__(self) -> None:
        self.trigger: int | None = None
        self.pending: list[AppendedItem] = []
        self.last_req_ts: int | None = None
        self.last_write_ttl: str | None = None
        self.last_version: str | None = None
        self.last_model: str | None = None
        self.last_fallback: list[Any] | None = None   # [from, to, ts] of the last MODEL_FALLBACK

    def to_ctx(self) -> dict[str, Any]:
        return {"trigger": self.trigger,
                "pending": [[a.kind, a.name, a.n_bytes, a.is_error, a.images]
                            for a in self.pending],
                "last_req_ts": self.last_req_ts, "last_write_ttl": self.last_write_ttl,
                "last_version": self.last_version, "last_model": self.last_model,
                "last_fallback": self.last_fallback}

    @classmethod
    def from_ctx(cls, d: Mapping[str, Any]) -> _LaneState:
        st = cls()
        st.trigger = _int(d.get("trigger"))
        st.pending = _appended_from(d.get("pending"))
        st.last_req_ts = _int(d.get("last_req_ts"))
        st.last_write_ttl = d.get("last_write_ttl") if d.get("last_write_ttl") in ("5m", "1h") \
            else None
        st.last_version = token(d.get("last_version"))
        st.last_model = token(d.get("last_model"))
        fb = d.get("last_fallback")
        if isinstance(fb, list) and len(fb) == 3 and _int(fb[2]) is not None:
            st.last_fallback = fb
        return st


def _record_from(cls: type, d: object) -> Any:
    """A record from its ``to_json`` form (collector context), None when absent or invalid."""
    if not isinstance(d, dict):
        return None
    try:
        return from_json(cls, d)
    except (ContractViolation, TypeError, ValueError):
        return None


def _appended_from(items: object) -> list[AppendedItem]:
    out: list[AppendedItem] = []
    if not isinstance(items, list):
        return out
    for it in items:
        try:
            kind, name, n_bytes, is_error, images = it
            out.append(AppendedItem(kind=kind, name=name, n_bytes=n_bytes, is_error=is_error,
                                    images=images))
        except (TypeError, ValueError, ContractViolation):
            continue
    return out


class _Group:
    """One API response (all lines sharing a ``message.id``) while it is open."""

    __slots__ = ("appended", "best", "content_bytes", "error_status", "first_ts", "is_error",
                 "lane", "last_ts", "mid", "mismatch", "n_lines", "offset", "out", "overage",
                 "reopened", "rq", "sig", "stop", "tool_ids", "ts_start", "best_counts",
                 "attr", "params", "at")

    def __init__(self, mid: str, lane: _LaneRef, offset: int, ts: int | None,
                 appended: tuple[AppendedItem, ...], ts_start: int | None) -> None:
        self.mid = mid
        self.lane = lane
        self.offset = offset
        self.first_ts = ts
        self.last_ts = ts
        self.ts_start = ts_start
        self.appended = appended
        self.best: dict[str, Any] | None = None
        self.out = -1
        self.sig: tuple[int, ...] | None = None
        self.mismatch = False
        self.stop: str | None = None
        self.tool_ids: set[str] = set()
        self.n_lines = 0
        self.content_bytes = 0
        self.error_status: int | None = None
        self.is_error = False
        self.overage = False
        self.reopened = False
        self.rq: str | None = None
        self.best_counts: tuple[int, ...] | None = None
        self.attr: Attribution | None = None      # kept from the first finalization (re-open)
        self.params: RequestParams | None = None
        self.at: int | None = None                # index of the request a re-open replaces


@dataclass(frozen=True)
class _LaneRef:
    lane_key: str
    session_key: str
    session_id: str
    kind: LaneKind
    parent: str | None


class ContextError(SourceError):
    """A collector context (the parser state saved in the collector state file) of the wrong
    shape; the collector discards it and re-reads the file from the start."""


@dataclass
class ParseOutcome:
    """What one parser pass over a file produced (internal; used by the collector)."""

    run: _Run
    end_offset: int                 # offset after the last consumed byte
    earliest_open: int | None       # start of the earliest group still open at the end
    unterminated: int | None        # start of an unterminated last line (collector mode)
    context: dict[str, Any]
    last_trigger: int | None
    recent_uuids: list[str]
    sha256: str
    n_bytes: int


class _FileParser:
    """Streams one transcript file (SPEC §5.3 steps 1-12) into a :class:`_Run`."""

    def __init__(self, run: _Run, path: Path, *, start_offset: int = 0,
                 context: Mapping[str, Any] | None = None,
                 recent_uuids: Iterable[str] = (), last_trigger: int | None = None,
                 size_limit: int | None = None, stop_at: int | None = None,
                 finalize_open: bool = True, process_unterminated: bool = True,
                 hash_lines: bool = False) -> None:
        self.run = run
        self.hash_lines = hash_lines
        self.path = path
        self.layout = file_layout(path)
        self.start_offset = start_offset
        self.size_limit = size_limit
        self.stop_at = stop_at
        self.finalize_open = finalize_open
        self.process_unterminated = process_unterminated
        ctx = context or {}
        # duplicate-line detection over a sliding window of the last UUID_WINDOW uuids: the same
        # bound as the collector cursor, so one-shot and incremental reads drop the same lines
        self.recent: deque[str] = deque(list(recent_uuids)[-UUID_WINDOW:])
        self.seen: dict[str, int] = {}
        for u in self.recent:
            self.seen[u] = self.seen.get(u, 0) + 1
        try:
            self._restore(ctx, last_trigger)
        except (TypeError, ValueError, AttributeError, KeyError, IndexError,
                RecursionError) as exc:
            raise ContextError(f"{path.name}: collector context unusable "
                               f"({type(exc).__name__})") from None
        #: message ids this parser emitted a request for in this read; the id → request index map
        #: is built only when a message re-opens beyond the closed window (a rare path)
        self.emitted: set[str] = set()
        self._index: dict[str, int] | None = None
        self._first_request = len(run.requests)
        self._lane_cache: dict[tuple[str, str, LaneKind], _LaneRef] = {}
        self._lane_fast: dict[tuple, _LaneRef] = {}
        self._source_refs: dict[Fidelity, SourceRef] = {}
        self.open: dict[str, _Group] = {}
        self.earliest_open: int | None = None
        self.unterminated: int | None = None
        self.end_offset = start_offset
        # collector chunks: the hash covers the source, the start offset and the consumed lines,
        # so two chunks with identical bytes at different offsets are distinct sources
        self.hasher = hashlib.sha256(f"{run.source_id}:{start_offset}:".encode())
        self.n_bytes = 0
        self.assistant_lines = 0

    def _restore(self, ctx: Mapping[str, Any], last_trigger: int | None) -> None:
        """Parser state from a collector context (``{}`` for a fresh read). A context of the
        wrong shape raises (the caller turns it into :class:`ContextError`)."""
        if not isinstance(ctx, Mapping):
            raise TypeError("context")
        self.lanes: dict[str, _LaneState] = {}
        for lk, d in (ctx.get("lanes") or {}).items():
            if isinstance(lk, str) and isinstance(d, Mapping):
                self.lanes[lk] = _LaneState.from_ctx(d)
        self.file_trigger = last_trigger if last_trigger is not None else _int(ctx.get("trigger"))
        self.tool_names: OrderedDict[str, str | None] = OrderedDict()
        for k, v in (ctx.get("tools") or []):
            if isinstance(k, str) and (v is None or isinstance(v, str)):
                self.tool_names[k] = v
        self.stats: dict[tuple[str, str], deque[int]] = {}
        for k, vals in (ctx.get("stats") or []):
            if isinstance(k, str) and isinstance(vals, list):
                model, _, lk = k.partition("|")
                self.stats[(model, lk)] = deque((v for v in vals if _int(v) is not None),
                                                maxlen=STATS_WINDOW)
        self.overage: dict[str, bool] = {k: v for k, v in (ctx.get("overage") or {}).items()
                                         if isinstance(k, str) and isinstance(v, bool)}
        self.quota_attrs: dict[str, tuple] = {
            k: tuple(tuple(p) for p in v) for k, v in (ctx.get("quota_attrs") or {}).items()
            if isinstance(k, str) and isinstance(v, list)}
        self.closed: OrderedDict[str, tuple] = OrderedDict()
        for mid, meta in (ctx.get("closed") or []):
            if isinstance(mid, str) and isinstance(meta, dict):
                out = meta.get("out")
                self.closed[mid] = (_int(meta.get("offset")) or 0, _int(meta.get("first_ts")),
                                    _int(meta.get("last_ts")), _int(meta.get("ts_start")),
                                    out if type(out) is int and out >= -1 else -1,
                                    token(meta.get("stop"), 32),
                                    tuple(_appended_from(meta.get("appended"))),
                                    _record_from(Attribution, meta.get("attr")),
                                    _record_from(RequestParams, meta.get("params")))
        self.meta_emitted = bool(ctx.get("meta_emitted")) or self.layout.kind is LaneKind.MAIN

    # ---------- lanes ----------
    def lane_for(self, obj: Mapping[str, Any]) -> _LaneRef:
        raw = (obj.get("sessionId"), obj.get("agentId"), obj.get("isSidechain"))
        if all(x is None or type(x) in (str, bool) for x in raw):
            ref = self._lane_fast.get(raw)
            if ref is None:
                ref = self._lane_fast[raw] = self._lane_for(obj)
            return ref
        return self._lane_for(obj)

    def _lane_for(self, obj: Mapping[str, Any]) -> _LaneRef:
        layout = self.layout
        sid = obj.get("sessionId")
        if not isinstance(sid, str) or not sid:
            sid = layout.session_hint or "unknown"
        agent = token(obj.get("agentId"))
        if layout.kind is LaneKind.MAIN:
            if obj.get("isSidechain") is True and agent:
                name, kind = agent, LaneKind.SUBAGENT
            else:
                name, kind = "main", LaneKind.MAIN
        else:
            name, kind = agent or layout.agent_id or "unknown", layout.kind
        return self._lane_ref(sid, name, kind)

    def _lane_ref(self, sid: str, name: str, kind: LaneKind) -> _LaneRef:
        cache = self._lane_cache
        key = (sid, name, kind)
        ref = cache.get(key)
        if ref is not None:
            return ref
        session_key = stable_id("ses", "claude-code", sid)
        lane_key = stable_id("ln", sid, name)
        # subagent and workflow lanes link to the main lane of the same session (SPEC §5.3 step
        # 11), nested agents (meta ``parentAgentId``) included
        parent = stable_id("ln", sid, "main") if kind is not LaneKind.MAIN else None
        ref = cache[key] = _LaneRef(lane_key, session_key, sid, kind, parent)
        self.run.shell(lane_key, session_key, kind, parent)
        return ref

    def lane_state(self, ref: _LaneRef) -> _LaneState:
        st = self.lanes.get(ref.lane_key)
        if st is None:
            st = self.lanes[ref.lane_key] = _LaneState()
            if ref.kind is LaneKind.MAIN:
                st.trigger = self.file_trigger
        return st

    # ---------- main loop ----------
    def parse(self) -> ParseOutcome:
        """Stream the file. The cyclic garbage collector is paused meanwhile (the records hold
        no reference cycles; a bulk load otherwise pays repeated full-heap scans) and restored."""
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            return self._parse()
        finally:
            if was_enabled:
                gc.enable()

    def _parse(self) -> ParseOutcome:
        run = self.run
        path = self.path
        size = self.size_limit
        if size is None:
            try:
                size = os.stat(path).st_size
            except OSError as exc:
                raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None
        compressed = path.suffix.lower() in (".gz", ".zst")
        start = self.start_offset
        try:
            lines = jsonl.iter_lines(path, start_offset=start)
        except ValueError as exc:
            raise SourceError(f"{path.name}: {exc}") from None
        stop_at = self.stop_at
        hash_lines = self.hash_lines
        hasher = self.hasher
        line = self.line
        n_lines = n_records = n_bytes = 0
        for _line_no, offset, raw in lines:
            if stop_at is not None and offset >= stop_at:
                break
            if not compressed and offset >= size:
                break
            end = offset + len(raw)
            terminated = compressed or end < size
            if not terminated and not self.process_unterminated:
                self.unterminated = offset
                break
            self.end_offset = end if not terminated else end + 1
            n_lines += 1
            if hash_lines:
                hasher.update(raw)
                hasher.update(b"\n")
                n_bytes += len(raw) + 1
            if not raw:
                run.quarantine(offset, "oversize_line", path)
                continue
            obj = parse_line(raw)
            if obj is None:
                reason = "bad_json" if raw.lstrip()[:1] == b"{" else "not_object"
                run.quarantine(offset, reason, path)
                continue
            n_records += 1
            try:
                line(obj, offset, raw)
            except (ContractViolation, BadUsageError, TypeError, ValueError, KeyError,
                    AttributeError, IndexError, OverflowError, RecursionError) as exc:
                logger.debug("line at offset %d quarantined (%s)", offset, type(exc).__name__)
                run.quarantine(offset, _reason_for(exc), path)
        run.stats["lines"] += n_lines
        run.stats["records"] += n_records
        run.stats["assistant_lines"] += self.assistant_lines
        self.n_bytes = n_bytes
        self.finish()
        return ParseOutcome(
            run=run, end_offset=self.end_offset, earliest_open=self.earliest_open,
            unterminated=self.unterminated, context=self.context(),
            last_trigger=self.file_trigger,
            recent_uuids=list(self.recent),
            sha256=self.hasher.hexdigest(), n_bytes=self.n_bytes)

    def line(self, obj: dict[str, Any], offset: int, raw: bytes = b"") -> None:
        """Step 1: drop duplicate uuids (sliding window), then dispatch by entry type."""
        run = self.run
        uuid = obj.get("uuid")
        if type(uuid) is str:
            seen = self.seen
            if uuid in seen:
                run.dq["dq.duplicate_uuid_lines"] += 1
                run.stats["duplicate_lines"] += 1
                return
            seen[uuid] = 1
            recent = self.recent
            recent.append(uuid)
            if len(recent) > UUID_WINDOW:
                old = recent.popleft()
                n = seen.get(old, 0) - 1
                if n > 0:
                    seen[old] = n
                else:
                    seen.pop(old, None)
        etype = obj.get("type")
        if etype == "assistant":
            self.assistant(obj, offset)
        elif etype == "user":
            self.user(obj, offset)
        elif etype == "attachment":
            self.attachment(obj, offset)
        elif etype == "system":
            self.system(obj, offset)
        elif etype == "cost-state":
            self.cost_state(obj, offset, raw)
        elif etype not in _BOOKKEEPING_TYPES:
            name = token(etype, 32) if isinstance(etype, str) else None
            run.unknown_types[name or "(invalid)"] += 1
            run.dq["dq.unknown_entry_type"] += 1

    # ---------- assistant ----------
    def assistant(self, obj: dict[str, Any], offset: int) -> None:
        """Step 3: add one assistant line to its message group (max output wins; ties → the
        later line; stop reason, tool_use ids, quota state, naive sum)."""
        run = self.run
        self.assistant_lines += 1
        msg = obj.get("message")
        if not isinstance(msg, dict):
            run.quarantine(offset, "missing:message", self.path)
            return
        mid = msg.get("id")
        if not isinstance(mid, str) or not mid or len(mid) > 128:
            run.quarantine(offset, "missing:message.id", self.path)
            return
        model_field = msg.get("model")
        if model_field == "<synthetic>":
            run.dq["dq.synthetic_skipped"] += 1
            return
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            run.quarantine(offset, "missing:message.usage", self.path)
            return
        counts = usage_counts(usage)
        fast = counts is not None
        if counts is None:
            b, _codes = normalize_anthropic_messages(usage)
            counts = (b.uncached_input, b.cache_read, b.cache_write_5m, b.cache_write_1h,
                      b.cache_write_unknown, b.output, b.web_search_requests, b.web_fetch_requests)
        if not run.saw_split and type(usage.get("cache_creation")) is dict:
            run.saw_split = True
        model_raw = token(model_field) or ""
        norm = run.model_id(model_raw).model
        ts = parse_ts_ms(obj.get("timestamp"))
        if norm:
            run.add_naive(norm, counts, ts)
        ref = self.lane_for(obj)
        st = self.lane_state(ref)
        if not self.meta_emitted:
            self.session_meta(ref, ts)
        quota = obj.get("quotaLimits")
        if isinstance(quota, dict):
            self.quota(ref, quota, ts)
        group = self.open.get(mid)
        if group is None:
            meta = self.closed.pop(mid, None)
            at = None
            if meta is None and mid in self.emitted:
                # a late line of a message emitted earlier in this read but no longer in the
                # closed window: re-open it from its emitted request (one request per id)
                at = self._find_request(mid)
                meta = _closed_meta(self.run.requests[at]) if at is not None else None
            if meta is not None:
                group = self._reopen(mid, ref, meta)
                group.at = at
            else:
                group = _Group(mid, ref, offset, ts, tuple(st.pending),
                               st.trigger if st.trigger is not None else ts)
                st.pending = []
            self.open[mid] = group
        group.n_lines += 1
        if ts is not None:
            if group.first_ts is None:
                group.first_ts = ts
                if group.ts_start is None:
                    group.ts_start = ts
            if group.last_ts is None or ts > group.last_ts:
                group.last_ts = ts
        out = counts[5]
        sig = counts[:5]
        if group.sig is None:
            group.sig = sig
        elif sig != group.sig:
            group.mismatch = True
        if out > group.out or (out == group.out and not group.reopened):
            group.out = out
            group.best = obj
            group.best_counts = counts if fast else None
        stop = msg.get("stop_reason")
        if stop is not None:
            group.stop = token(stop, 32) or "other"
        if obj.get("isApiErrorMessage") is True:
            group.is_error = True
            status = _int(obj.get("apiErrorStatus"))
            if status is not None:
                group.error_status = status
        group.overage = self.overage.get(ref.session_id, False)
        content = msg.get("content")
        if isinstance(content, list):
            low = group.stop is None and group.out <= MSO_MAX_OUTPUT
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    tid = block.get("id")
                    if isinstance(tid, str) and len(tid) <= 128:
                        group.tool_ids.add(tid)
                        self.remember_tool(tid, block.get("name"))
                    if low:
                        group.content_bytes += _json_nbytes(block.get("input"))
                elif btype == "fallback":
                    self.fallback_event(ref, ts, block.get("originalModel"),
                                        block.get("fallbackModel"))
                elif low:
                    if btype == "text" and isinstance(block.get("text"), str):
                        group.content_bytes += _nbytes(block["text"])
                    elif btype == "thinking" and isinstance(block.get("thinking"), str):
                        group.content_bytes += _nbytes(block["thinking"])
        rq = obj.get("requestId")
        if type(rq) is str and rq:
            first = run.rq_first.get(rq)
            if first is None:
                run.rq_first[rq] = mid
                run.rq_order.append(rq)
                if len(run.rq_order) > REQUEST_ID_WINDOW:
                    run.rq_first.pop(run.rq_order.popleft(), None)
            elif first != mid:
                run.rq_bad.add(rq)
            if group.rq is None:
                group.rq = rq

    def _reopen(self, mid: str, ref: _LaneRef, meta: tuple) -> _Group:
        offset, first_ts, last_ts, ts_start, out, stop, appended, attr, params = meta
        g = _Group(mid, ref, offset, first_ts, appended, ts_start)
        g.attr = attr
        g.params = params
        g.last_ts = last_ts
        g.out = out
        g.stop = stop
        g.reopened = True
        g.best = None
        return g

    def _find_request(self, mid: str) -> int | None:
        """Index in ``run.requests`` of the request this parser emitted for message *mid*: a short
        scan back over the latest requests (a re-opened message is usually recent), else an id →
        index map built once (a message re-opened far away), so no input makes this quadratic."""
        index = self._index
        if index is None:
            requests = self.run.requests
            stop = max(self._first_request, len(requests) - _RECENT_SCAN) - 1
            for i in range(len(requests) - 1, stop, -1):
                if requests[i].attempts[0].provider_message_id == mid:
                    return i
            index = self._index = {}
            for i in range(self._first_request, len(requests)):
                pmid = requests[i].attempts[0].provider_message_id
                if pmid is not None:
                    index[pmid] = i
        return index.get(mid)

    def remember_tool(self, tid: str, name: object) -> None:
        names = self.tool_names
        names[tid] = self.run.names.tool(name) if isinstance(name, str) else None
        names.move_to_end(tid)
        while len(names) > TOOL_NAME_WINDOW:
            names.popitem(last=False)

    def quota(self, ref: _LaneRef, q: Mapping[str, Any], ts: int | None) -> None:
        run = self.run
        run.saw_quota = True
        over = q.get("isUsingOverage")
        using = over if isinstance(over, bool) else None
        resets = q.get("resetsAt")
        resets_ms: int | None
        if type(resets) is int:
            resets_ms = resets * 1000 if 0 <= resets < 10**11 else _int(resets)
        elif isinstance(resets, float) and math.isfinite(resets) and resets >= 0:
            resets_ms = _int(int(resets * 1000)) if resets < 10**11 else _int(int(resets))
        else:
            resets_ms = parse_ts_ms(resets)
        attrs = (("overage_status", token(q.get("overageStatus"), 32)),
                 ("rate_limit_type", token(q.get("rateLimitType"), 32)),
                 ("resets_at_ms", resets_ms),
                 ("status", token(q.get("status"), 32)),
                 ("using_overage", using))
        self.overage[ref.session_id] = using is True
        if self.quota_attrs.get(ref.session_id) == attrs:
            return
        self.quota_attrs[ref.session_id] = attrs
        if ts is not None:
            self.event(ref, ts, LaneEventKind.QUOTA_STATE, attrs)

    def fallback_event(self, ref: _LaneRef, ts: int | None, frm: object, to: object) -> None:
        """MODEL_FALLBACK (trigger refusal). The assistant ``fallback`` block and the
        ``model_refusal_fallback`` system entry mark the same fallback: one event per (from, to)
        within :data:`FALLBACK_DEDUPE_MS`."""
        if ts is None:
            return
        frm_t, to_t = token(frm), token(to)
        st = self.lane_state(ref)
        last = st.last_fallback
        if last is not None and last[0] == frm_t and last[1] == to_t \
                and abs(ts - last[2]) <= FALLBACK_DEDUPE_MS:
            return
        st.last_fallback = [frm_t, to_t, ts]
        attrs = [("trigger", "refusal")]
        if frm_t:
            attrs.append(("from_model", frm_t))
        if to_t:
            attrs.append(("to_model", to_t))
        self.event(ref, ts, LaneEventKind.MODEL_FALLBACK, tuple(attrs))

    # ---------- conversation entries ----------
    def user(self, obj: dict[str, Any], offset: int) -> None:
        """A user entry: closes open groups (MSO check against its tool_result ids), sets the
        trigger, queues appended sizes, emits HUMAN_PROMPT per the detection rule (step 8)."""
        run = self.run
        ts = parse_ts_ms(obj.get("timestamp"))
        ref = self.lane_for(obj)
        st = self.lane_state(ref)
        self.session_meta(ref, ts)
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        answered: set[str] = set()
        items: list[AppendedItem] = []
        has_tool_result = False
        text_bytes = 0
        if isinstance(content, str):
            text_bytes += _nbytes(content)
            run.count_secrets(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_result":
                    has_tool_result = True
                    tid = block.get("tool_use_id")
                    name = None
                    if isinstance(tid, str):
                        answered.add(tid)
                        name = self.tool_names.get(tid)
                    n_bytes, images = self._tool_result_size(block.get("content"))
                    items.append(AppendedItem(kind="tool_result", name=name, n_bytes=n_bytes,
                                              is_error=block.get("is_error") is True,
                                              images=images))
                elif btype == "text" and isinstance(block.get("text"), str):
                    text_bytes += _nbytes(block["text"])
                    run.count_secrets(block["text"])
                elif btype == "image":
                    src = block.get("source")
                    data = src.get("data") if isinstance(src, dict) else None
                    items.append(AppendedItem(kind="image", name=None,
                                              n_bytes=len(data) if isinstance(data, str) else 0,
                                              images=1))
        compact_summary = obj.get("isCompactSummary") is True
        if text_bytes and not compact_summary:
            # R-E33: a compact summary is the COMPACTION output (its synthetic request carries
            # ``postTokens``), not human text appended to the next request
            items.insert(0, AppendedItem(kind="user_text", name=None, n_bytes=text_bytes))
        tur = obj.get("toolUseResult")
        if isinstance(tur, dict) and "totalTokens" in tur:
            run.dq["dq.rollup_not_spend"] += 1
        self.close_groups(answered, is_user=True)
        if ts is not None:
            st.trigger = ts
            if ref.kind is LaneKind.MAIN:
                self.file_trigger = ts
        st.pending.extend(items)
        origin = obj.get("origin")
        if isinstance(origin, dict) and origin.get("kind") is not None:
            human = origin.get("kind") == "human"
        else:
            human = (obj.get("isMeta") is not True and not compact_summary
                     and not has_tool_result)
        if human and ref.kind is LaneKind.MAIN and ts is not None:
            self.event(ref, ts, LaneEventKind.HUMAN_PROMPT, ())

    def _tool_result_size(self, content: object) -> tuple[int, int]:
        run = self.run
        if isinstance(content, str):
            run.count_secrets(content)
            return _nbytes(content), 0
        n = images = 0
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text" and isinstance(part.get("text"), str):
                    n += _nbytes(part["text"])
                    run.count_secrets(part["text"])
                elif ptype == "image":
                    images += 1
        return n, images

    def attachment(self, obj: dict[str, Any], offset: int) -> None:
        """CONTEXT_INJECTION with the attachment type and the byte length of its JSON values."""
        run = self.run
        ts = parse_ts_ms(obj.get("timestamp"))
        ref = self.lane_for(obj)
        st = self.lane_state(ref)
        self.session_meta(ref, ts)
        att = obj.get("attachment")
        att_type = "other"
        n_bytes = 0
        if isinstance(att, dict):
            att_type = token(att.get("type"), 48) or "other"
            for key in _ATTACHMENT_SIZE_KEYS:
                if key in att:
                    try:
                        text = json.dumps(att[key], ensure_ascii=False, separators=(",", ":"))
                    except (TypeError, ValueError, RecursionError):
                        continue
                    n_bytes += _nbytes(text)
                    run.count_secrets(text)
        self.close_groups(frozenset(), is_user=False)
        if ts is not None:
            st.trigger = ts
            if ref.kind is LaneKind.MAIN:
                self.file_trigger = ts
            self.event(ref, ts, LaneEventKind.CONTEXT_INJECTION,
                       (("att_type", att_type), ("n_bytes", n_bytes)))
        st.pending.append(AppendedItem(kind="attachment", name=att_type, n_bytes=n_bytes))

    def system(self, obj: dict[str, Any], offset: int) -> None:
        """compact_boundary, model_refusal_fallback and api_error entries (step 8)."""
        ts = parse_ts_ms(obj.get("timestamp"))
        ref = self.lane_for(obj)
        self.session_meta(ref, ts)
        self.close_groups(frozenset(), is_user=False)
        sub = obj.get("subtype")
        if ts is None:
            return
        if sub == "compact_boundary":
            self.compaction(obj, ref, ts, offset)
        elif sub == "model_refusal_fallback":
            self.fallback_event(ref, ts, obj.get("originalModel"), obj.get("fallbackModel"))
        elif sub == "api_error":
            err = obj.get("error")
            err = err if isinstance(err, dict) else {}
            status = _int(err.get("status"))
            if status is None:
                status = _int(obj.get("status"))
            attrs = (("error_type", _error_type(status, err)), ("max_retries",
                     _int(obj.get("maxRetries"))), ("retry_attempt", _int(obj.get("retryAttempt"))),
                     ("retry_in_ms", _int(obj.get("retryInMs"))), ("status", status))
            self.event(ref, ts, LaneEventKind.API_ERROR, attrs)

    def compaction(self, obj: Mapping[str, Any], ref: _LaneRef, ts: int, offset: int) -> None:
        """COMPACTION event plus the ESTIMATED compaction inference on ``<lane>#compaction``."""
        run = self.run
        meta = obj.get("compactMetadata")
        meta = meta if isinstance(meta, dict) else {}
        pre, post = _int(meta.get("preTokens")), _int(meta.get("postTokens"))
        duration = _int(meta.get("durationMs"))
        attrs: list[tuple[str, Any]] = []
        trigger = meta.get("trigger")
        if trigger in ("auto", "manual"):
            attrs.append(("trigger", trigger))
        for key, value in (("pre_tokens", pre), ("post_tokens", post), ("duration_ms", duration)):
            if value is not None:
                attrs.append((key, value))
        if "cumulativeDroppedTokens" in meta:
            attrs.append(("dropped_tokens", _int(meta.get("cumulativeDroppedTokens"))))
        self.event(ref, ts, LaneEventKind.COMPACTION, tuple(attrs))
        if pre is None:
            return
        st = self.lane_state(ref)
        start = ts - duration if duration is not None and duration <= ts else ts
        ttl = st.last_write_ttl or "5m"
        tau_ms = 3_600_000 if ttl == "1h" else 300_000
        # the compaction call starts `duration` before the boundary; the prefix is warm when the
        # previous request of the lane started at most one TTL before it
        warm = st.last_req_ts is not None and start - st.last_req_ts <= tau_ms
        if warm:
            usage = UsageBuckets(cache_read=pre, output=post or 0)
        elif ttl == "1h":
            usage = UsageBuckets(cache_write_1h=pre, output=post or 0)
        else:
            usage = UsageBuckets(cache_write_5m=pre, output=post or 0)
        model_raw = st.last_model or ""
        lane_key = ref.lane_key + "#compaction"
        run.shell(lane_key, ref.session_key, LaneKind.COMPACTION, ref.lane_key)
        uid = obj.get("uuid") if isinstance(obj.get("uuid"), str) else str(offset)
        request_id = stable_id("rq", "claude-code", "compaction", ref.session_id, uid)
        billing = self.billing_path(ref)
        ctx = run.pricing(model_raw, "standard", "standard", None, billing)
        inf = Inference(inference_id=stable_id("inf", request_id, 0),
                        kind=InferenceKind.COMPACTION, usage=usage, pricing=ctx,
                        usage_source=UsageSource.ESTIMATED, billable=True)
        if not ctx.model:
            run.dq["dq.unpriced_model"] += 1
        attempt = Attempt(attempt_id=stable_id("at", request_id, 0), attempt_no=0,
                          ts_start_ms=start, ttft_ms=None, duration_ms=duration,
                          outcome=Outcome.OK, http_status=None, error_type=None,
                          retry_layer=None, retry_after_ms=None, should_retry=None,
                          provider_request_id=None, provider_message_id=None,
                          model_served=model_raw or None, stop_reason=None, inferences=(inf,))
        attr = run.attribution(query_source="compaction", billing_path=_attr_path(billing),
                               client_version=st.last_version)
        req = Request(request_id=request_id, session_key=ref.session_key, lane_key=lane_key,
                      seq=offset, attribution=attr, params=run.params(model_raw, None, None, None),
                      attempts=(attempt,), source=self.source_ref(offset, Fidelity.ESTIMATED))
        run.emit_request(req)
        run.touch_session(ref.session_key, start)
        run.dq["dq.hidden_compaction_estimated"] += 1
        run.dq_tokens["dq.hidden_compaction_estimated"] += pre + (post or 0)
        if billing == "subscription":
            run.dq["dq.subscription_allowance"] += 1

    def cost_state(self, obj: Mapping[str, Any], offset: int, raw: bytes) -> None:
        ts = parse_ts_ms(obj.get("timestamp"))
        if ts is None:
            return
        ref = self.lane_for(obj)
        nano = None
        exact: Any = obj
        if raw:  # re-read the line with decimals as Decimal: money is never a float
            try:
                exact = loads_decimal(raw)
            except (ValueError, RecursionError, UnicodeDecodeError):
                exact = {}
        for key in _COST_STATE_KEYS:
            if isinstance(exact, dict) and key in exact:
                nano = decimal_usd_to_nano(exact.get(key))
                break
        if nano is None:
            self.run.dq["dq.unknown_fields"] += 1
            return
        self.event(ref, ts, LaneEventKind.COST_STATE,
                   (("reported_total_nano", nano), ("reporter", "claude_code.cost_state")))

    def session_meta(self, ref: _LaneRef, ts: int | None) -> None:
        if self.meta_emitted or ts is None or self.layout.kind is LaneKind.MAIN:
            return
        if ref.kind is LaneKind.MAIN:
            return
        self.meta_emitted = True
        meta = self.layout.meta
        attrs = (("agent_type", meta.get("agentType")), ("model_alias", meta.get("model")),
                 ("spawn_depth", meta.get("spawnDepth")))
        self.event(ref, ts, LaneEventKind.SESSION_META, attrs)

    def event(self, ref: _LaneRef, ts: int, kind: LaneEventKind,
              attrs: tuple[tuple[str, Any], ...]) -> None:
        run = self.run
        run.events.append(LaneEvent(lane_key=ref.lane_key, ts_ms=ts, kind=kind, attrs=attrs))
        run.touch_session(ref.session_key, ts)

    def source_ref(self, offset: int, fidelity: Fidelity = Fidelity.FULL) -> SourceRef:
        """The request's :class:`SourceRef`. One shared object per (file, fidelity): the locator
        names the file (``file``, or ``<f_…>:file`` in a directory read) and ``Request.seq`` holds
        the byte offset of the message's first line, so a 200k-line import stays in budget."""
        ref = self._source_refs.get(fidelity)
        if ref is None:
            run = self.run
            ref = self._source_refs[fidelity] = SourceRef(
                adapter=ADAPTER_NAME, source_id=run.source_id,
                locator=f"{run.locator_prefix}file", fidelity=fidelity,
                priority=SOURCE_PRIORITY)
        return ref

    def billing_path(self, ref: _LaneRef, overage: bool | None = None) -> str:
        configured = self.run.billing_path
        if configured is None:
            return "unknown"
        if overage is None:
            overage = self.overage.get(ref.session_id, False)
        if overage and configured in _OVERAGE_BILLING_PATH:
            return _OVERAGE_BILLING_PATH[configured]
        return configured

    # ---------- closing and finalizing groups ----------
    def close_groups(self, answered: set[str] | frozenset[str], *, is_user: bool) -> None:
        """Finalize every open group (a non-assistant entry closes them); MESSAGE_START_ONLY
        when a user entry answers the group's tool_use and the group qualifies (step 6)."""
        if not self.open:
            return
        groups = list(self.open.values())
        if len(groups) > 1:
            groups.sort(key=lambda g: g.offset)
        self.open = {}
        for g in groups:
            mso = (is_user and g.stop is None and 0 <= g.out <= MSO_MAX_OUTPUT
                   and not g.reopened and bool(g.tool_ids & answered))
            self.finalize(g, mso)

    def finish(self) -> None:
        """End of input: finalize the open groups (one-shot / quiescent file), or (collector)
        leave them unemitted and record the earliest one's offset. A trailing group whose last
        line carries a stop reason is withheld too: a one-shot import finalizes it only at the
        next non-assistant entry, and emitting it early could split its side effects (upgrade,
        lane state) across runs. The offset still never passes an unclosed group (§5.3)."""
        if not self.open:
            return
        groups = sorted(self.open.values(), key=lambda g: g.offset)
        self.open = {}
        if self.finalize_open:
            for g in groups:
                self.finalize(g, False)
            return
        self.earliest_open = groups[0].offset

    def finalize(self, g: _Group, mso: bool) -> None:
        """Build the request of a closed group; a failure quarantines the group."""
        try:
            self._finalize(g, mso)
        except (ContractViolation, BadUsageError, TypeError, ValueError, KeyError,
                AttributeError, IndexError, OverflowError, RecursionError) as exc:
            logger.debug("group at offset %d quarantined (%s)", g.offset, type(exc).__name__)
            self.run.quarantine(g.offset, _reason_for(exc), self.path)

    def _finalize(self, g: _Group, mso: bool) -> None:
        run = self.run
        obj = g.best
        if obj is None:  # a re-opened group whose new lines did not raise the output
            self.remember_closed(g)
            return
        if g.ts_start is None:
            run.quarantine(g.offset, "missing:timestamp", self.path)
            return
        msg = obj["message"]
        usage = msg["usage"]
        ref = g.lane
        st = self.lane_state(ref)
        model_raw = token(msg.get("model")) or ""
        tier = token(usage.get("service_tier"), 32) or "standard"
        speed = token(usage.get("speed"), 32) or "standard"
        geo = token(usage.get("inference_geo"), 32)
        if geo in ("not_available", ""):
            geo = None
        if g.attr is not None:
            # a re-opened message keeps the billing path of its first emission (the quota state
            # at the time of the call), so its pricing and attribution stay consistent
            billing = g.attr.billing_path or "unknown"
        else:
            billing = self.billing_path(ref, g.overage)
        ctx = run.pricing(model_raw, tier, speed, geo, billing)
        request_id = request_id_for("anthropic", g.mid, run.source_id, "")
        advisor = token(obj.get("advisorModel"))
        source = UsageSource.MESSAGE_START_ONLY if mso else UsageSource.FINAL
        infs, codes = message_inferences(usage, model_raw, ctx, request_id, source, advisor,
                                         g.best_counts)
        # a re-emission (re-opened message) replaces its first emission: per-request counters,
        # the MSO statistics and the lane state were already updated by that first emission
        # (possibly in an earlier collector run), so they are not applied twice
        first = not g.reopened
        if first:
            for code in codes:
                run.dq[code] += 1
            if not ctx.model:
                run.dq["dq.unpriced_model"] += 1
        if mso:
            infs = self._mso_upper(infs, g, ctx, ref)
        elif first and g.stop is not None and g.tool_ids and ctx.model:
            key = (ctx.model, ref.kind.value)
            dq_ = self.stats.get(key)
            if dq_ is None:
                dq_ = self.stats[key] = deque(maxlen=STATS_WINDOW)
            dq_.append(max(g.out, 0))
        if g.mismatch and first:
            run.dq["dq.split_usage_mismatch"] += 1
        duration = g.last_ts - g.ts_start if g.last_ts is not None else None
        if duration is not None and duration < 0:
            duration = None
        diag = _diagnostics(msg.get("diagnostics"))
        if "diagnostics" in msg:
            run.saw_diag = True
        edits = _applied_edits(msg.get("context_management"))
        dropped = _thinking_dropped(msg.get("input_transformations"))
        rq = obj.get("requestId")
        if rq == g.rq:
            rq = g.rq  # the shared string object
        if type(rq) is not str or len(rq) > 64 or _TOKEN_RE.match(rq) is None:
            rq = None  # validated without the token cache (ids are unique)
        outcome = Outcome.HTTP_ERROR if g.is_error else Outcome.OK
        attempt = Attempt(
            attempt_id=stable_id("at", request_id, 0), attempt_no=0, ts_start_ms=g.ts_start,
            ttft_ms=None, duration_ms=duration, outcome=outcome,
            http_status=g.error_status if g.is_error else None,
            error_type=_error_type(g.error_status, {}) if g.is_error else None,
            retry_layer=None, retry_after_ms=None, should_retry=None,
            provider_request_id=rq, provider_message_id=g.mid, model_served=model_raw or None,
            stop_reason=g.stop, inferences=tuple(infs), diagnostics=diag, applied_edits=edits,
            thinking_dropped=dropped, raw_usage_json=safe_usage_json(usage),
            convention_id=ANTHROPIC_MESSAGES)
        version = token(obj.get("version"), 32)
        effort = token(obj.get("effort"), 32)
        per_turn = token(obj.get("perTurnEffort"), 32)
        cwd = obj.get("cwd")
        names = run.names
        agent_type = token(obj.get("attributionAgent")) or (
            self.layout.meta.get("agentType") if ref.kind is not LaneKind.MAIN else None)
        skill, mcp, plugin = (obj.get("attributionSkill"), obj.get("attributionMcpServer"),
                              obj.get("attributionPlugin"))
        entry = obj.get("entrypoint")
        cacheable = all(x is None or (type(x) is str and len(x) <= _CACHE_MAX_LEN)
                        for x in (skill, mcp, plugin, entry, cwd))
        key = (agent_type, ref.kind, skill, mcp, plugin, entry, version, billing, cwd)
        attr = g.attr
        if attr is None and cacheable:
            attr = run.attr_by_key.get(key)
        if attr is None:
            attr = run.attribution(
                agent_type=agent_type, query_source=_QUERY_SOURCE.get(ref.kind),
                skill=names.name(skill), mcp_server=names.name(mcp), plugin=names.name(plugin),
                entrypoint=token(entry, 48), client_version=version,
                billing_path=_attr_path(billing),
                cwd_key=names.hashed(cwd) if isinstance(cwd, str) and cwd else None)
            if cacheable and len(run.attr_by_key) < 4096:
                run.attr_by_key[key] = attr
        # a re-opened message keeps the attribution and parameters of its first emission, so
        # re-emitting it (possibly in a later collector run) never changes merged attributes
        params = g.params or run.params(model_raw, per_turn or effort, effort, advisor)
        self.remember_closed(g, attr, params)
        req = Request(request_id=request_id, session_key=ref.session_key, lane_key=ref.lane_key,
                      seq=g.offset, attribution=attr, params=params, attempts=(attempt,),
                      appended=g.appended, source=self.source_ref(g.offset))
        at = g.at
        if at is None and g.reopened and g.mid in self.emitted:
            at = self._find_request(g.mid)     # re-emitted in this read: replace, never duplicate
        at = run.emit_request(req, at=at)
        self.emitted.add(g.mid)
        if self._index is not None:
            self._index[g.mid] = at
        span = run.session_ts.get(ref.session_key)
        hi = g.last_ts if g.last_ts is not None and g.last_ts > g.ts_start else g.ts_start
        if span is None:
            run.session_ts[ref.session_key] = [g.ts_start, hi]
        else:
            if g.ts_start < span[0]:
                span[0] = g.ts_start
            if hi > span[1]:
                span[1] = hi
        if not first:
            return
        if billing == "subscription":
            run.dq["dq.subscription_allowance"] += 1
        if version is not None:
            run.versions[version] += 1
            if st.last_version is not None and st.last_version != version:
                self.event(ref, g.ts_start, LaneEventKind.UPGRADE,
                           (("from_version", st.last_version), ("to_version", version)))
            st.last_version = version
        if st.last_req_ts is None or g.ts_start >= st.last_req_ts:
            st.last_req_ts = g.ts_start
        for inf in reversed(infs):
            w = inf.usage
            if w.cache_write_5m:
                st.last_write_ttl = "5m"
                break
            if w.cache_write_1h:
                st.last_write_ttl = "1h"
                break
            if w.cache_write_unknown or w.cache_write_other:
                break
        if model_raw:
            st.last_model = model_raw

    def _mso_upper(self, infs: list[Inference], g: _Group, ctx: PricingContext,
                   ref: _LaneRef) -> list[Inference]:
        run = self.run
        idx = next((i for i in range(len(infs) - 1, -1, -1)
                    if infs[i].kind in (InferenceKind.MESSAGE, InferenceKind.FALLBACK)),
                   len(infs) - 1)
        serving = infs[idx]
        logged = serving.usage.output
        window = self.stats.get((ctx.model, ref.kind.value))
        median = 0
        if window:
            ordered = sorted(window)
            median = ordered[len(ordered) // 2]
        by_bytes = ceil_tokens(g.content_bytes, ctx.model)
        upper = min(max(logged, median, by_bytes), MAX_TOKENS)
        infs = list(infs)
        infs[idx] = dataclasses.replace(serving, output_upper=upper)
        run.dq["dq.message_start_only"] += 1
        run.dq_tokens["dq.message_start_only"] += upper - logged
        return infs

    def remember_closed(self, g: _Group, attr: Attribution | None = None,
                        params: RequestParams | None = None) -> None:
        """Remember a finalized group (bounded) so a later line of its id re-opens it with the
        same start, appended items, attribution and parameters."""
        closed = self.closed
        closed[g.mid] = (g.offset, g.first_ts, g.last_ts, g.ts_start, g.out, g.stop, g.appended,
                         attr if attr is not None else g.attr,
                         params if params is not None else g.params)
        closed.move_to_end(g.mid)
        if len(closed) > CLOSED_WINDOW:
            closed.popitem(last=False)

    # ---------- context ----------
    def context(self) -> dict[str, Any]:
        """The content-free parser state at :attr:`end_offset` (JSON-serializable)."""
        return {
            "lanes": {k: v.to_ctx() for k, v in sorted(self.lanes.items())},
            "trigger": self.file_trigger,
            "tools": [[k, v] for k, v in self.tool_names.items()],
            "stats": [[f"{m}|{lk}", list(vals)] for (m, lk), vals in sorted(self.stats.items())],
            "overage": dict(sorted(self.overage.items())),
            "quota_attrs": {k: [list(p) for p in v] for k, v in sorted(self.quota_attrs.items())},
            "closed": [[k, {"offset": m[0], "first_ts": m[1], "last_ts": m[2], "ts_start": m[3],
                            "out": m[4], "stop": m[5],
                            "appended": [[a.kind, a.name, a.n_bytes, a.is_error, a.images]
                                         for a in m[6]],
                            "attr": to_json(m[7]) if m[7] is not None else None,
                            "params": to_json(m[8]) if m[8] is not None else None}]
                       for k, m in self.closed.items()],
            "meta_emitted": self.meta_emitted,
        }


def _closed_meta(req: Request) -> tuple:
    """The closed-group meta (see ``_FileParser.remember_closed``) of an emitted request."""
    att = req.attempts[0]
    out = -1
    if att.raw_usage_json is not None:
        raw = parse_line(att.raw_usage_json.encode())
        if raw is not None and _int(raw.get("output_tokens")) is not None:
            out = raw["output_tokens"]
    if out < 0:
        out = max(inf.usage.output for inf in att.inferences)
    last = att.ts_start_ms + (att.duration_ms or 0)
    return (req.seq, att.ts_start_ms, last, att.ts_start_ms, out, att.stop_reason, req.appended,
            req.attribution, req.params)


def _refuse_constant(tok: str) -> Any:
    raise ValueError("non-finite number")


def _finite_float(tok: str) -> float:
    value = float(tok)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


_LINE_DECODER = json.JSONDecoder(parse_constant=_refuse_constant, parse_float=_finite_float)
_SURROGATE_ESCAPE = re.compile(rb"\\[uU][dD][89a-fA-F]")


def parse_line(raw: bytes) -> dict[str, Any] | None:
    """``core.jsonl.parse_json_line`` with a reused decoder (the hot path): a ``dict``, or None for
    invalid UTF-8/JSON, non-objects, NaN/Infinity (also overflowing literals), excessive nesting
    and strings holding unpaired surrogates (property-tested equal to the core function)."""
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        obj = _LINE_DECODER.decode(text)
    except (ValueError, RecursionError):
        return None
    if type(obj) is not dict:
        return None
    if _SURROGATE_ESCAPE.search(raw) and _has_lone_surrogate(obj):
        return None
    return obj


def _has_lone_surrogate(value: Any) -> bool:
    """Whether a string (key or value) anywhere in the decoded JSON *value* holds an unpaired
    surrogate. Iterative (the ``core.jsonl`` port, F-CORE-C review D7), so a document nested as
    deeply as the JSON decoder accepts never raises ``RecursionError`` here."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return False


def usage_counts(u: Mapping[str, Any]) -> tuple[int, int, int, int, int, int, int, int] | None:
    """``(uncached, read, w5, w1, unknown, output, web_search, web_fetch)`` of a well-formed usage
    object under the ``anthropic.messages`` mapping (§3.11), or None when anything is unusual — the
    caller then uses ``core.conventions.normalize_anthropic_messages`` (which raises on bad usage).
    A hot-path shortcut: property-tested equal to the core normalizer."""
    vals = []
    for key in ("input_tokens", "cache_read_input_tokens", "output_tokens"):
        v = u.get(key)
        if v is None:
            vals.append(0)
        elif type(v) is int and 0 <= v <= MAX_TOKENS:
            vals.append(v)
        else:
            return None
    total = u.get("cache_creation_input_tokens")
    if total is not None and not (type(total) is int and 0 <= total <= MAX_TOKENS):
        return None
    w5 = w1 = 0
    cc = u.get("cache_creation")
    if cc is not None:
        if type(cc) is not dict:
            return None
        for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
            v = cc.get(key)
            if v is not None and not (type(v) is int and 0 <= v <= MAX_TOKENS):
                return None
        w5 = cc.get("ephemeral_5m_input_tokens") or 0
        w1 = cc.get("ephemeral_1h_input_tokens") or 0
    ws = wf = 0
    stu = u.get("server_tool_use")
    if stu is not None:
        if type(stu) is not dict:
            return None
        for key in ("web_search_requests", "web_fetch_requests"):
            v = stu.get(key)
            if v is not None and not (type(v) is int and 0 <= v <= MAX_TOKENS):
                return None
        ws = stu.get("web_search_requests") or 0
        wf = stu.get("web_fetch_requests") or 0
    if u.get("output_tokens_details") is not None:
        return None
    unknown = 0
    if total is not None and total - w5 - w1 > 0:
        unknown = total - w5 - w1
    return (vals[0], vals[1], w5, w1, unknown, vals[2], ws, wf)


def message_inferences(usage: Mapping[str, Any], model_raw: str, ctx: PricingContext,
                       request_id: str, source: UsageSource, advisor: str | None,
                       counts: tuple[int, ...] | None = None
                       ) -> tuple[list[Inference], list[str]]:
    """``core.conventions.anthropic_inferences`` for one response. The common case — no
    ``iterations`` and a well-formed usage object (:func:`usage_counts`) — is built directly with
    the same ids, buckets and notes (property-tested equal); everything else goes through the core
    function."""
    if usage.get("iterations"):
        counts = None
    elif counts is None:
        counts = usage_counts(usage)
    if counts is None:
        return anthropic_inferences(usage, message_model=model_raw, ctx=ctx, id_prefix=request_id,
                                    usage_source=source, advisor_model=advisor)
    uncached, read, w5, w1, unknown, out, ws, wf = counts
    notes: list[str] = []
    total = usage.get("cache_creation_input_tokens")
    if total is not None:
        if unknown:
            notes.append("dq.ttl_split_residual")
        elif total < w5 + w1:
            notes.append("dq.ttl_split_exceeds_total")
    buckets = UsageBuckets(uncached_input=uncached, cache_read=read, cache_write_5m=w5,
                           cache_write_1h=w1, cache_write_unknown=unknown, output=out,
                           web_search_requests=ws, web_fetch_requests=wf)
    return [Inference(inference_id=stable_id("inf", request_id, 0), kind=InferenceKind.MESSAGE,
                      usage=buckets, pricing=ctx, usage_source=source)], notes


def _reason_for(exc: BaseException) -> str:
    if isinstance(exc, BadUsageError):
        return "bad_usage"
    return "bad_type:entry"


def _attr_path(billing: str) -> str | None:
    return None if billing == "unknown" else billing


def _error_type(status: int | None, err: Mapping[str, Any]) -> str:
    if err.get("connection"):
        return "connection"
    if status is not None and status in _ERROR_TYPE_BY_STATUS:
        return _ERROR_TYPE_BY_STATUS[status]
    etype = err.get("type")
    inner = err.get("error")
    if isinstance(inner, dict):
        etype = etype or inner.get("type")
    if isinstance(etype, str) and "overloaded" in etype:
        return "overloaded"
    if isinstance(etype, str) and "rate_limit" in etype:
        return "rate_limit"
    if status is not None and status >= 500:
        return "server_error"
    return "other"


def _diagnostics(diag: object) -> CacheDiagnostic | None:
    if not isinstance(diag, dict):
        return None
    reason = diag.get("cache_miss_reason")
    if not isinstance(reason, dict):
        return None
    rtype = token(reason.get("type"), 48)
    if rtype is None:
        return None
    canonical = rtype if rtype in DIAG_REASONS else "unavailable"
    return CacheDiagnostic(reason=canonical, provider_reason=rtype,
                           missed_input_tokens_estimate=_int(
                               reason.get("cache_missed_input_tokens")),
                           source="anthropic.cache_diagnostics")


def _applied_edits(cm: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(cm, dict):
        return ()
    edits = cm.get("applied_edits")
    if not isinstance(edits, list):
        return ()
    out = []
    for e in edits:
        if isinstance(e, dict):
            etype = token(e.get("type"), 48)
            cleared = _int(e.get("cleared_input_tokens"))
            if etype is not None:
                out.append((etype, cleared or 0))
    return tuple(out)


def _thinking_dropped(transforms: object) -> int:
    if not isinstance(transforms, list):
        return 0
    return sum(1 for t in transforms if isinstance(t, dict) and t.get("type") == "thinking_dropped")


# ---------------------------------------------------------------------------------------------
# result assembly
# ---------------------------------------------------------------------------------------------

def source_id_for(opts: IngestOptions, path: Path, adapter: str = ADAPTER_NAME) -> str:
    """``s_`` + HMAC of the adapter and the absolute path under the name key (never the path)."""
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = str(path)
    if opts.name_key:
        return pseudonym(opts.name_key, "s", f"{adapter}:{resolved}")
    return stable_id("s", adapter, resolved)


def _name_hmac(opts: IngestOptions, path: Path) -> str:
    if opts.name_key:
        return pseudonym(opts.name_key, "h", Path(path).name)
    return ""


def _notes(run: _Run, extra: Iterable[DataQualityNote] = ()) -> list[DataQualityNote]:
    notes: list[DataQualityNote] = list(extra)
    for code, count in sorted(run.dq.items()):
        if count <= 0:
            continue
        detail = _DETAILS.get(code, code)
        if code == "dq.unknown_entry_type":
            detail = "unknown entry types: " + ", ".join(
                f"{k}={v}" for k, v in sorted(run.unknown_types.items()))
        tokens = run.dq_tokens.get(code)
        notes.append(DataQualityNote(code=code, severity=_SEVERITY.get(code, "info"),
                                     count=count, detail=detail[:_DETAIL_MAX],
                                     tokens=tokens if tokens is not None and code in
                                     run.dq_tokens else None))
    if run.versions:
        detail = "requests per client version: " + ", ".join(
            f"{v}={n}" for v, n in sorted(run.versions.items()))
        notes.append(DataQualityNote(code="dq.version_histogram", severity="info",
                                     count=len(run.versions), detail=detail[:_DETAIL_MAX]))
    if run.secrets:
        detail = "likely secrets in tool results / prompts (counts by type only): " + ", ".join(
            f"{k}={v}" for k, v in sorted(run.secrets.items()))
        notes.append(DataQualityNote(code="dq.secrets_observed", severity="warn",
                                     count=sum(run.secrets.values()),
                                     detail=detail[:_DETAIL_MAX]))
    notes.sort(key=lambda n: n.code)
    return notes


def _collisions(run: _Run) -> None:
    """Count requestIds that map to several message ids in this read (``dq.request_id_collision``,
    D4). Requests are keyed by ``message.id`` only; ``provider_request_id`` stays a hint that the
    store never joins on once it has seen the collision (§7.3), so incremental reads and one-shot
    imports carry identical records."""
    if run.rq_bad:
        run.dq["dq.request_id_collision"] += len(run.rq_bad)


def _in_window(ts: int, opts: IngestOptions) -> bool:
    if opts.since_ms is not None and ts < opts.since_ms:
        return False
    return not (opts.until_ms is not None and ts >= opts.until_ms)


def build_result(run: _Run, source: SourceInfo, *, capabilities: frozenset[str] | None = None,
                 extra_notes: Iterable[DataQualityNote] = (), source_kind: str = SOURCE_KIND,
                 aggregates: Iterable[Any] = ()) -> IngestResult:
    """Assemble the :class:`IngestResult` of a run (sessions and lane shells, notes, stats)."""
    _collisions(run)
    opts = run.opts
    requests = [r for r in run.requests if _in_window(r.ts_start_ms, opts)]
    events = [e for e in run.events if _in_window(e.ts_ms, opts)]
    events.sort(key=lambda e: (e.lane_key, e.ts_ms, e.kind.value,
                               tuple((k, repr(v)) for k, v in e.attrs)))
    by_session: dict[str, list[Lane]] = {}
    for lane_key, (session_key, kind, parent, exact) in sorted(run.shells.items()):
        by_session.setdefault(session_key, []).append(Lane(
            lane_key=lane_key, session_key=session_key, kind=kind, parent_lane_key=parent,
            cache_scope_key=run.cache_scope_key, requests=(), lane_exact=exact))
    base_attr = run.attribution()
    sessions = []
    for session_key, lanes in sorted(by_session.items()):
        span = run.session_ts.get(session_key, [0, 0])
        sessions.append(Session(session_key=session_key, source_kind=source_kind,
                                attribution=base_attr, lanes=tuple(lanes), started_ms=span[0],
                                ended_ms=span[1]))
    naive = {m: UsageBuckets(uncached_input=a[0], cache_read=a[1], cache_write_5m=a[2],
                             cache_write_1h=a[3], cache_write_unknown=a[4], output=a[5],
                             web_search_requests=a[6], web_fetch_requests=a[7])
             for m, a in sorted(run.naive.items())}
    if capabilities is None:
        caps: set[str] = set()
        if requests:
            caps |= _BASE_CAPABILITIES
            if run.saw_split:
                caps.add("ttl_split")
            if run.saw_diag:
                caps.add("diagnostics")
            if run.saw_quota:
                caps.add("quota_state")
        capabilities = frozenset(caps)
    stats = dict(sorted(run.stats.items()))
    stats["requests"] = len(requests)
    stats["events"] = len(events)
    stats["quarantined"] = len(run.quarantined)
    return IngestResult(
        source=source, requests=requests, sessions=sessions, events=events,
        aggregates=list(aggregates),
        cost_lines=[], outcomes=[], quarantined=list(run.quarantined),
        notes=_notes(run, extra_notes), stats=stats, capabilities=capabilities,
        naive_usage=naive)


def source_info(run: _Run, source_id: str, path: Path, sha256: str, n_bytes: int,
                adapter: str = ADAPTER_NAME) -> SourceInfo:
    """The :class:`SourceInfo` of a run: key ids only when the run produced ``h_`` / ``p_`` /
    ``c_`` values under them."""
    opts = run.opts
    name_hmac = _name_hmac(opts, path)
    name_key_id = run.names.key_id if (run.names.used or name_hmac) else None
    principal = run.identity.principal
    pkid = None
    if principal and principal[:2] in ("p_", "c_"):
        pkid = run.identity.principal_key_id or opts.principal_key_id or (
            key_id(opts.principal_key) if opts.principal_key else None)
    return SourceInfo(source_id=source_id, adapter=adapter, name_hmac=name_hmac, sha256=sha256,
                      bytes=n_bytes, name_key_id=name_key_id, principal_key_id=pkid)


def retention_note(paths: Iterable[Path], now_ms: int) -> DataQualityNote | None:
    """``dq.retention_warning`` when the oldest transcript's mtime is older than
    ``cleanupPeriodDays − 3`` days (default 30 → 27) before *now_ms* (injected clock)."""
    if now_ms <= 0:
        return None
    limit = now_ms - (RETENTION_DEFAULT_DAYS - RETENTION_MARGIN_DAYS) * _DAY_MS
    oldest: int | None = None
    n_old = 0
    for p in paths:
        try:
            mtime_ms = os.stat(p).st_mtime_ns // 1_000_000
        except OSError:
            continue
        oldest = mtime_ms if oldest is None else min(oldest, mtime_ms)
        if mtime_ms < limit:
            n_old += 1
    if oldest is None or oldest >= limit:
        return None
    age_days = (now_ms - oldest) // _DAY_MS
    return DataQualityNote(
        code="dq.retention_warning", severity="warn", count=n_old,
        detail=(f"oldest transcript is {age_days} days old; Claude Code deletes transcripts after "
                f"cleanupPeriodDays (default {RETENTION_DEFAULT_DAYS}); collect before they are "
                "removed")[:_DETAIL_MAX])


def check_options(opts: IngestOptions, adapter: str = ADAPTER_NAME) -> None:
    """Content tier ``none`` only (D40): ``fingerprint``/``full`` → :class:`UsageError`."""
    if not isinstance(opts, IngestOptions):
        raise UsageError("read expects IngestOptions")
    try:
        tier = ContentTier(opts.content_tier)
    except ValueError:
        raise UsageError("unknown content tier") from None
    if tier is not ContentTier.NONE:
        raise UsageError(f"{adapter} supports content tier none only (D40)")


def sha256_file(path: Path) -> tuple[str, int]:
    """SHA-256 (hex) and size of a file's bytes as stored."""
    h = hashlib.sha256()
    n = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                n += len(chunk)
    except OSError as exc:
        raise SourceError(f"{Path(path).name}: unreadable ({type(exc).__name__})") from None
    return h.hexdigest(), n


# ---------------------------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------------------------

_SNIFF_TYPES = frozenset({"user", "assistant", "system", "attachment", "summary", "cost-state",
                          "file-history-snapshot"})


@dataclass
class ClaudeCodeAdapter:
    """Claude Code transcript importer (registry name ``claude-code``; SPEC §5.3)."""

    name: str = ADAPTER_NAME
    capabilities: frozenset[str] = field(default=CAPABILITIES)

    def sniff(self, path: Path, head: bytes) -> bool:
        """A ``*.jsonl`` (optionally ``.gz``) file other than ``journal.jsonl`` whose first
        complete lines are Claude Code entries (``type`` plus camelCase ``sessionId``/``uuid``)."""
        name = Path(path).name
        if name == "journal.jsonl" or not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
            return False
        if not isinstance(head, (bytes, bytearray)):
            return False
        head = bytes(head)
        for raw in head.split(b"\n")[:32]:
            raw = raw.strip()
            if not raw:
                continue
            obj = parse_line(raw)
            if obj is None:
                continue
            if "session_id" in obj:
                return False
            if obj.get("type") in _SNIFF_TYPES and ("sessionId" in obj or "leafUuid" in obj
                                                    or "uuid" in obj):
                return True
        # a first line longer than the head (a huge pasted prompt): look at its keys
        first = head.lstrip()[:1] == b"{" and b"\n" not in head.strip()
        return first and b'"sessionId":' in head and b'"session_id"' not in head and any(
            m in head for m in (b'"type":"user"', b'"type":"assistant"', b'"type": "user"',
                                b'"type": "assistant"'))

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Import one transcript, or every transcript under a directory (one result)."""
        check_options(opts)
        path = Path(path)
        if path.is_dir():
            files = list(iter_claude_files(path))
            source_id = source_id_for(opts, path)
            run = _Run(opts, source_id)
            h = hashlib.sha256()
            total = 0
            for f in files:
                # the file's path below the root (unique, unlike its name), never in clear
                rel = f.relative_to(path).as_posix()
                run.locator_prefix = pseudonym(opts.name_key or b"\0", "f", rel)[:14] + ":"
                digest, n = sha256_file(f)
                h.update(digest.encode())
                total += n
                _FileParser(run, f).parse()
            run.locator_prefix = ""
            # workflow roll-up files (totalTokens) are never read as spend: counted only
            run.dq["dq.rollup_not_spend"] += sum(
                1 for p in sorted(path.rglob("*.json"))
                if "workflows" in p.relative_to(path).parts[:-1]
                and not p.name.endswith(".meta.json") and p.is_file())
            note = retention_note(files, opts.now_ms)
            info = source_info(run, source_id, path, h.hexdigest(), total)
            return build_result(run, info, extra_notes=[note] if note else [])
        if not path.is_file():
            raise SourceError(f"{path.name}: not a file")
        source_id = source_id_for(opts, path)
        run = _Run(opts, source_id)
        digest, n = sha256_file(path)
        _FileParser(run, path, size_limit=None if path.suffix.lower() in (".gz", ".zst")
                    else n).parse()
        note = retention_note([path], opts.now_ms)
        info = source_info(run, source_id, path, digest, n)
        return build_result(run, info, extra_notes=[note] if note else [])


def parse_file(opts: IngestOptions, path: Path, *, source_id: str, start_offset: int,
               context: Mapping[str, Any] | None, recent_uuids: Iterable[str],
               last_trigger: int | None, size_limit: int, stop_at: int | None,
               finalize_open: bool, process_unterminated: bool) -> ParseOutcome:
    """One resumable parser pass over *path* (the collector's entry point; internal API)."""
    run = _Run(opts, source_id)
    parser = _FileParser(run, path, start_offset=start_offset, context=context,
                         recent_uuids=recent_uuids, last_trigger=last_trigger,
                         size_limit=size_limit, stop_at=stop_at, finalize_open=finalize_open,
                         process_unterminated=process_unterminated, hash_lines=True)
    return parser.parse()

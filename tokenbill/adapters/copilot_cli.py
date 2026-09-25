"""GitHub Copilot CLI importer: ``session-state/*/events.jsonl`` plus the experimental
``session-store.db`` reader (addendum §5.9, CP-LOCAL; non-blocking).

Inputs live under ``$COPILOT_HOME`` or ``~/.copilot`` (CI: ``$HOME/.copilot``). Only two things
are ever opened: ``session-state/<id>/events.jsonl`` (always) and ``session-store.db`` (only with
``"copilot-store" in opts.experimental``, read-only URI, because its schema is known from
third-party readers only). ``config.json``, ``mcp-*``, ``providers.json``, ``logs/``,
``session.db``, ``plan.md``, ``checkpoints/``, ``files/`` and ``apps.json`` are never opened
(:func:`never_opened`).

``events.jsonl`` follows the ``github/copilot-sdk`` ``session-events.ts`` schema (@075f027; the
interfaces used here are unchanged on ``main``, verified 2026-09-25): an envelope ``{id, parentId,
timestamp, type, data, agentId?}`` per line. The lenient reader recovers a concatenated line from
its **last** ``{"type":`` (``dq.copilot_concatenated_line``) and counts unknown types
(``dq.unknown_entry_type``). Mapping (content fields never leave the parser: text, reasoning, tool
arguments and results, summaries, paths and branch names are reduced to byte counts or ``h_``
pseudonyms before any record is built):

* ``session.start`` / ``session.resume`` → a new *leg*; SESSION_META (``routing_mode`` from
  ``selectedModel == "auto"``, ``credit_limit_nano`` from ``sessionLimits.maxAiCredits`` × $0.01,
  ``context_tier``), ``contextTier`` → ``PricingContext.context_tier`` of later requests,
  ``reasoningEffort`` → request params, ``context.repository`` / ``context.cwd`` → ``h_``;
* ``assistant.message`` → one output-only request per ``apiCallId`` (chunks grouped, the maximum
  ``outputTokens`` kept; ``apiCallId`` → ``provider_message_id``, ``requestId`` →
  ``provider_request_id``); with the store, rows supply the input side (joined by session, turn
  index, model and ``created_at`` ± 2 s) and unjoined outputs become ``OUTPUT_RESIDUAL``;
* ``session.compaction_complete`` → COMPACTION event (``trigger`` auto/manual, ``copilot_trigger``,
  static token counts) and, when ``copilotUsage.tokenDetails`` is present, an exact
  ``Inference(kind=COMPACTION)`` under ``github_copilot.token_details`` on the COMPACTION lane;
* ``session.model_change`` → MODEL_SWITCH_USER or MODEL_FALLBACK; ``session.auto_mode_resolved`` →
  routing ``auto``; ``session.truncation`` → CONTEXT_EDIT ``copilot_truncation``;
  ``session.usage_checkpoint`` → COST_STATE ``copilot.cli.checkpoint`` and the per-model
  ``write_ttl_hint`` (``cacheTtlSeconds`` 300 → ``5m``, 3600 → ``1h``); ``session.error`` →
  API_ERROR; ``tool.execution_complete`` → appended tool-result sizes; ``user.message`` →
  HUMAN_PROMPT plus its size;
* ``session.shutdown`` → per-model ``UsageAggregate(copilot.cli_rollup)`` under
  ``github_copilot.shutdown_rollup`` when no per-request input exists (no store rows): counters are
  cumulative across resume legs and are differenced (``dq.copilot_resume_legs``); a leg with a
  successful compaction is taken at face value (the CLI resets its counters,
  ``dq.copilot_compaction_reset``); a leg without a shutdown is ``dq.copilot_unclean_shutdown``.

Every request is priced by the caller on provider ``github``, channel ``github_copilot``, billing
path ``opts.attribution.billing_path`` when it is a Copilot path, else ``copilot_pool``
(``dq.copilot_billing_path_assumed``; ruling R-E43), compliance from
``dict(opts.attribution.extra).get("copilot_compliance")``, fidelity ``NO_TTL_SPLIT``, source
priority 39, ``agent_product="copilot_cli"``, cache scope ``"unknown"``; lanes MAIN / SUBAGENT /
COMPACTION with ``core.ids.copilot_session_key`` / ``copilot_lane_key`` (the keys CP-OTEL and
CP-VSCODE compute for the same conversation). Events-only lanes carry no ``usage_sequence``.

The parser is resumable (:class:`SessionParser` keeps a content-free context), which
:mod:`tokenbill.adapters.copilot_collect` uses for incremental fleet collection.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.adapters.copilot_conventions import (
    CONVENTION_SESSION_STORE,
    CONVENTION_SHUTDOWN_ROLLUP,
    CONVENTION_TOKEN_DETAILS,
    DQ_CONVENTION_MISMATCH,
    NegativeUncachedError,
    billing_rule,
    exact_number,
    normalize_session_store,
    normalize_shutdown_rollup,
    normalize_token_details,
)
from tokenbill.core.conventions import BadUsageError
from tokenbill.core.errors import ContractViolation, SourceError, UsageError
from tokenbill.core.ids import (
    copilot_lane_key,
    copilot_session_key,
    is_opaque_ref,
    key_id,
    natural_id,
    pseudonym,
    request_id_for,
    stable_id,
)
from tokenbill.core.jsonl import iter_lines, parse_json_line
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.money import NANO_USD_PER_CREDIT, nano_aiu_to_nano
from tokenbill.core.records import (
    COPILOT_BILLING_PATHS,
    MAX_TOKENS,
    AppendedItem,
    Attempt,
    Attribution,
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
    "ADAPTER_NAME",
    "CAPABILITIES",
    "EVENTS_FILE",
    "JOIN_WINDOW_MS",
    "KNOWN_EVENT_TYPES",
    "SESSION_STATE_DIR",
    "SOURCE_PRIORITY",
    "STORE_FILE",
    "STORE_FLAG",
    "CopilotCliAdapter",
    "SessionParser",
    "StoreRead",
    "StoreRow",
    "check_options",
    "copilot_home",
    "iter_events_files",
    "map_store_row",
    "never_opened",
    "parse_event_line",
    "parse_ts_ms",
    "read_store",
    "session_ids",
]

logger = logging.getLogger("tokenbill.adapters.copilot_cli")

ADAPTER_NAME = "copilot-cli"
SOURCE_KIND = "copilot-cli"
SOURCE_PRIORITY = 39
AGENT_PRODUCT = "copilot_cli"
PROVIDER = "github"
CHANNEL = "github_copilot"
STORE_FLAG = "copilot-store"
STORE_FILE = "session-store.db"
EVENTS_FILE = "events.jsonl"
SESSION_STATE_DIR = "session-state"
#: Declared maximum capabilities; ``usage_sequence`` only when store rows supply per-request input.
CAPABILITIES = frozenset({"timing", "events", "lanes_exact", "params", "credits", "appended",
                          "usage_sequence"})
#: Store row ↔ ``assistant.message`` join window (addendum §5.9).
JOIN_WINDOW_MS = 2000
#: Busy timeout of the read-only store connection, seconds (one retry after it, then skip).
STORE_TIMEOUT_S = 1
#: COST_STATE reporter of ``session.usage_checkpoint`` (C-8).
CHECKPOINT_REPORTER = "copilot.cli.checkpoint"

#: File names and directories under the Copilot home that are never opened (addendum §5.9).
NEVER_OPEN_FILES = frozenset({"config.json", "mcp-config.json", "providers.json", "session.db",
                              "plan.md", "apps.json"})
NEVER_OPEN_DIRS = frozenset({"logs", "checkpoints", "files", "mcp-oauth-config", "mcp-secrets"})

#: Every event type of ``session-events.ts`` @075f027 (persisted or ephemeral). Types outside this
#: set are counted as ``dq.unknown_entry_type``; known types without a mapping are ignored.
KNOWN_EVENT_TYPES = frozenset("""
session.start session.resume session.remote_steerable_changed session.error session.idle
session.title_changed session.schedule_created session.schedule_cancelled session.schedule_rearmed
session.autopilot_objective_changed session.info session.indexed_search session.warning
session.model_change session.model_deselected session.auto_tier_recommendation
session.auto_tier_switch_failed session.mode_changed session.mode_notice_delivered
session.session_limits_changed session.permissions_changed session.plan_changed
session.todos_changed session.workspace_file_changed session.handoff session.truncation
session.snapshot_rewind session.shutdown session.usage_checkpoint session.context_changed
session.usage_info session.context_cleared session.compaction_start session.compaction_complete
session.task_complete session.completion_receipt session.fusion_route_started
session.fusion_route_failed session.fusion_resolved session.fusion_completed
session.permission_recovery user.message pending_messages.modified assistant.turn_start
assistant.intent assistant.fusion_phase_started assistant.fusion_phase_activity
assistant.fusion_phase_completed assistant.fusion_phase_failed assistant.server_tool_progress
assistant.reasoning assistant.reasoning_delta assistant.tool_call_delta assistant.streaming_delta
assistant.message assistant.message_start assistant.message_delta assistant.turn_end
assistant.idle assistant.usage model.call_failure model.call_finished abort tool.user_requested
tool.execution_start tool.execution_partial_result tool.execution_progress
tool.execution_complete tool_search.activated skill.invoked subagent.started subagent.configured
subagent.completed subagent.failed subagent.selected subagent.deselected hook.start hook.end
hook.progress session.binary_asset system.message system.notification permission.requested
permission.completed permission.carriedForward permission.messageAuthorization
permission.messageAuthorizationRead permission.messageAuthorizationDegraded
permission.assentDetected permission.contextualAuthorization user_input.requested
user_input.completed elicitation.requested elicitation.completed sampling.requested
sampling.completed mcp.oauth_required mcp.oauth_completed mcp.headers_refresh_required
mcp.headers_refresh_completed session.custom_notification ui.ephemeral_query
external_tool.requested external_tool.completed command.queued command.execute
command.completed auto_mode_switch.requested auto_mode_switch.completed
session_limits_exhausted.requested session_limits_exhausted.completed session.auto_mode_resolved
session.managed_settings_resolved session.managed_settings_enforced commands.changed
capabilities.changed exit_plan_mode.requested exit_plan_mode.completed session.tools_updated
session.background_tasks_changed factory.run_updated factory.run_started factory.run_settled
session.skills_loaded session.custom_agents_updated session.mcp_servers_loaded
session.mcp_server_status_changed session.mcp_server_removed session.mcp_server_needs_reconnect
mcp.tools.list_changed mcp.resources.list_changed mcp.prompts.list_changed
session.extensions_loaded session.canvas.opened session.canvas.registry_changed
session.canvas.closed session.canvas.unavailable session.canvas.recorded session.canvas.removed
session.extensions.attachments_pushed mcp_app.tool_call_complete
""".split())

#: ``session-store.db`` ``assistant_usage_events`` columns (third-party schema, experimental).
STORE_REQUIRED = ("id", "session_id", "model", "input_tokens", "cache_read_tokens",
                  "cache_write_tokens", "created_at")
STORE_OPTIONAL = ("turn_index", "copilot_usage_model", "output_tokens", "reasoning_tokens",
                  "total_nano_aiu", "duration_ms", "initiator", "request_multiplier")
#: ``sessions`` columns that may be read (``summary`` never is).
STORE_SESSION_COLUMNS = ("id", "cwd", "repository")

_DETAIL_MAX = 256
_TOOL_WINDOW = 512
_PENDING_MAX = 256
_RECOVERY_CANDIDATES = 64
_TTL_HINTS = {300: "5m", 3600: "1h"}
_COMPACTION_TRIGGERS = frozenset({"threshold", "manual", "context_limit_retry", "memory_pressure",
                                  "model_switch"})
_CONTEXT_TIERS = frozenset({"default", "long_context"})
_FALLBACK_CAUSES = {"refusal_fallback": "refusal", "rate_limit_auto_switch": "availability"}
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+=-]{0,127}\Z")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/()\[\]+-]{0,63}\Z")
_ENUM_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_TYPE_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-zA-Z0-9_]+)+\Z")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}\Z")
_TYPE_START_RE = re.compile(rb'\{\s*"type"\s*:')
_TS_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?"
    r"(Z|z|[+-]\d{2}:?\d{2})?\Z")
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

_SEVERITY = {
    "dq.quarantined": "warn",
    "dq.copilot_concatenated_line": "warn",
    "dq.copilot_compaction_reset": "warn",
    "dq.copilot_unclean_shutdown": "warn",
    "dq.copilot_store_schema": "warn",
    "dq.copilot_store_unavailable": "warn",
    DQ_CONVENTION_MISMATCH: "warn",
    "dq.copilot_nano_aiu_mismatch": "warn",
    "dq.sum_check_failed": "warn",
}
_DETAILS = {
    "dq.quarantined": "malformed records quarantined (content-free reasons)",
    "dq.copilot_concatenated_line": "concatenated events.jsonl lines recovered from the last "
                                    "{\"type\": (the truncated prefix is dropped)",
    "dq.copilot_resume_legs": "resumed session legs whose cumulative session.shutdown rollups "
                              "were differenced",
    "dq.copilot_compaction_reset": "legs with a successful compaction: the CLI resets its rollup "
                                   "counters, so the rollup covers post-compaction requests only",
    "dq.copilot_unclean_shutdown": "session legs without a clean session.shutdown (no rollup, or "
                                   "shutdownType error)",
    "dq.copilot_store_schema": "session-store.db schema differs from the expected columns "
                               "(experimental reader)",
    "dq.copilot_store_unavailable": "session-store.db locked or unreadable after one retry; "
                                    "skipped this run",
    DQ_CONVENTION_MISMATCH: "cache read + write exceed the cache-inclusive input",
    "dq.copilot_nano_aiu_mismatch": "tokenDetails do not reproduce totalNanoAiu",
    "dq.copilot_billing_path_assumed": "billing path not given: assumed copilot_pool "
                                       "(--billing-path copilot_direct for GITHUB_TOKEN runs)",
    "dq.copilot_session_covered_by_vscode": "sessions covered by VS Code spans: lane events "
                                            "only, no requests, inferences or rollups",
    "dq.sum_check_failed": "reasoning tokens exceed output tokens (subset dropped)",
}


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------

def _match(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.match(value) else None


def _count(value: object) -> int | None:
    """A token count (int in [0, 2**53]) or None."""
    return value if type(value) is int and 0 <= value <= MAX_TOKENS else None


def _int(value: object) -> int | None:
    return value if type(value) is int else None


def _nano_aiu(value: object) -> int | None:
    """A nano-AIU figure as an int (fractions rounded half-even); None when not a number."""
    number = exact_number(value)
    if number is None or number > 2**63:
        return None
    return round(number)


def _credit_limit_nano(value: object) -> int | None:
    """``maxAiCredits`` × $0.01 in nano-USD (exact; fractions rounded half-even)."""
    number = exact_number(value)
    if number is None or number > 2**40:
        return None
    return round(number * NANO_USD_PER_CREDIT)


def _days_from_civil(y: int, m: int, d: int) -> int:
    y -= m <= 2
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_ts_ms(value: object) -> int | None:
    """ISO-8601 (``Z`` or an offset; no offset = UTC) or epoch seconds / milliseconds → epoch ms
    (UTC); None for anything unparseable or before 1970."""
    if type(value) is int:
        ms = value * 1000 if 0 <= value < 10**11 else value
        return ms if 0 <= ms <= 253_402_300_799_999 else None
    if not isinstance(value, str) or len(value) > 40:
        return None
    m = _TS_RE.match(value.strip())
    if m is None:
        return None
    y, mo, d, hh, mi, ss = (int(m.group(i)) for i in range(1, 7))
    if not (1970 <= y <= 9999 and 1 <= mo <= 12 and hh < 24 and mi < 60 and ss < 61):
        return None
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    if not 1 <= d <= _DAYS_IN_MONTH[mo - 1] + (1 if mo == 2 and leap else 0):
        return None
    frac = (m.group(7) or "0")[:3].ljust(3, "0")
    ms = ((_days_from_civil(y, mo, d) * 24 + hh) * 60 + mi) * 60_000 + ss * 1000 + int(frac)
    tz = m.group(8)
    if tz and tz not in ("Z", "z"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        ms -= sign * (int(digits[:2]) * 60 + int(digits[2:])) * 60_000
    return ms if ms >= 0 else None


def parse_event_line(raw: bytes) -> tuple[list[dict[str, Any]], bool]:
    """The event objects of one ``events.jsonl`` line and whether it needed recovery.

    A line that is not one JSON object is scanned for ``{"type":`` from the end: the **last**
    position whose tail parses as an object is the recovered event
    (``dq.copilot_concatenated_line``); a prefix that is itself a complete object (two events
    without a newline) is kept too. At most 64 candidate positions are tried; nothing recoverable
    → ``([], False)``."""
    obj = parse_json_line(raw, exact_numbers=True)
    if obj is not None:
        return [obj], False
    starts = [m.start() for m in _TYPE_START_RE.finditer(raw)][-_RECOVERY_CANDIDATES:]
    for pos in reversed(starts):
        if pos == 0:
            continue
        tail = parse_json_line(raw[pos:], exact_numbers=True)
        if tail is None or not isinstance(tail.get("type"), str):
            continue
        head = parse_json_line(raw[:pos], exact_numbers=True)
        if head is not None and isinstance(head.get("type"), str):
            return [head, tail], True
        return [tail], True
    return [], False


def copilot_home(env: Mapping[str, str] | None = None) -> Path:
    """``$COPILOT_HOME`` when set, else ``~/.copilot`` (CI runners: ``$HOME/.copilot``)."""
    environ = os.environ if env is None else env
    home = environ.get("COPILOT_HOME")
    if home:
        return Path(home)
    user_home = environ.get("HOME")
    return (Path(user_home) if user_home else Path.home()) / ".copilot"


def never_opened(path: Path) -> bool:
    """Whether *path* names a Copilot file this package never opens (addendum §5.9)."""
    parts = Path(path).parts
    if not parts:
        return False
    name = parts[-1]
    if name in NEVER_OPEN_FILES or name.startswith("mcp-"):
        return True
    return any(p in NEVER_OPEN_DIRS or p.startswith("mcp-") for p in parts[:-1][-3:])


def session_ids(home: Path) -> frozenset[str]:
    """Directory names under ``<home>/session-state/`` (the raw session ids; no file is opened)."""
    root = Path(home) / SESSION_STATE_DIR
    try:
        with os.scandir(root) as it:
            return frozenset(e.name for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return frozenset()


def iter_events_files(home: Path) -> list[Path]:
    """``<home>/session-state/<id>/events.jsonl`` files, sorted by session id."""
    root = Path(home) / SESSION_STATE_DIR
    out = []
    for sid in sorted(session_ids(home)):
        path = root / sid / EVENTS_FILE
        if path.is_file():
            out.append(path)
    return out


def check_options(opts: IngestOptions) -> None:
    """Content tier ``none`` only (addendum §8.1): other tiers raise :class:`UsageError`."""
    if not isinstance(opts, IngestOptions):
        raise UsageError("read expects IngestOptions")
    try:
        tier = ContentTier(opts.content_tier)
    except ValueError:
        raise UsageError("unknown content tier") from None
    if tier is not ContentTier.NONE:
        raise UsageError(f"{ADAPTER_NAME} supports content tier none only (addendum §8.1)")


def source_id_for(opts: IngestOptions, path: Path) -> str:
    """``s_`` + HMAC of the adapter and the absolute path under the name key (never the path)."""
    try:
        resolved = str(Path(path).resolve())
    except OSError:  # pragma: no cover - resolve() only fails on exotic file systems
        resolved = str(path)
    if opts.name_key:
        return pseudonym(opts.name_key, "s", f"{ADAPTER_NAME}:{resolved}")
    return stable_id("s", ADAPTER_NAME, resolved)


def _identity(opts: IngestOptions) -> tuple[str | None, str | None]:
    """``(principal, principal_key_id)`` per identity mode (SPEC §5.1): ``central`` → ``r_<ref>``,
    ``two-stage`` → ``c_`` HMAC, ``install`` / ``central-ingest`` → ``p_`` HMAC; without a ref the
    caller's ``opts.attribution.principal`` is kept."""
    mode, ref = opts.identity_mode, opts.principal_ref
    if mode not in ("central", "two-stage", "install", "central-ingest"):
        raise UsageError(f"unknown identity mode {str(mode)[:32]!r}")
    if ref is None:
        return opts.attribution.principal, None
    if not is_opaque_ref(ref):
        raise UsageError("principal ref must match [A-Za-z0-9._-]{1,64} (never an email)")
    if mode == "central":
        return "r_" + ref, None
    if not opts.principal_key:
        raise UsageError(f"identity mode {mode} needs a principal key")
    prefix = "c" if mode == "two-stage" else "p"
    return (pseudonym(opts.principal_key, prefix, ref),
            opts.principal_key_id or key_id(opts.principal_key))


def _canonical(obj: object) -> str:
    """Canonical JSON (sorted keys, no spaces) with exact ``Decimal`` numbers."""
    if isinstance(obj, Mapping):
        return "{" + ",".join(json.dumps(str(k)) + ":" + _canonical(obj[k])
                              for k in sorted(obj, key=str)) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in obj) + "]"
    if isinstance(obj, Decimal):
        text = format(obj, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return json.dumps(obj)


def _sha256_file(path: Path, start: int = 0, end: int | None = None) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    try:
        with open(path, "rb") as f:
            f.seek(start)
            remaining = None if end is None else max(0, end - start)
            while remaining is None or remaining > 0:
                chunk = f.read(1 << 20 if remaining is None else min(1 << 20, remaining))
                if not chunk:
                    break
                h.update(chunk)
                n += len(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
    except OSError as exc:
        raise SourceError(f"{Path(path).name}: unreadable ({type(exc).__name__})") from None
    return h.hexdigest(), n


class _Bad(Exception):
    """A malformed event (content-free quarantine reason)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------------------------
# the run accumulator
# ---------------------------------------------------------------------------------------------

class _Run:
    """Everything one read (a file, a Copilot home, or one collector chunk) accumulates."""

    def __init__(self, opts: IngestOptions, source_id: str) -> None:
        self.opts = opts
        self.source_id = source_id
        self.principal, self.principal_key_id = _identity(opts)
        self.name_key = opts.name_key or b""
        self.name_key_id = (opts.name_key_id or key_id(self.name_key)) if self.name_key else None
        self.names_used = False
        self.allow = frozenset(opts.name_allowlist)
        base = opts.attribution
        path = base.billing_path
        self.billing_assumed = path not in COPILOT_BILLING_PATHS
        self.billing_path = "copilot_pool" if self.billing_assumed else str(path)
        compliance = dict(base.extra).get("copilot_compliance")
        self.compliance = compliance if compliance in ("data_residency", "fedramp") else None
        self.base_attr = dataclasses.replace(base, principal=self.principal,
                                             agent_product=AGENT_PRODUCT,
                                             billing_path=self.billing_path)
        self.requests: list[Request] = []
        self.events: list[LaneEvent] = []
        self.aggregates: list[UsageAggregate] = []
        self.quarantined: list[QuarantineItem] = []
        self.dq: Counter[str] = Counter()
        self.dq_tokens: Counter[str] = Counter()
        self.stats: Counter[str] = Counter()
        self.unknown_types: Counter[str] = Counter()
        self.shells: dict[str, tuple[str, LaneKind, str | None]] = {}
        self.spans: dict[str, list[int]] = {}
        self.session_attr: dict[str, Attribution] = {}
        self.saw_credits = False
        self.saw_appended = False
        self.saw_store_requests = False
        self._ctx_cache: dict[tuple, PricingContext] = {}
        self._attr_cache: dict[tuple, Attribution] = {}

    # --- quarantine and names ---
    def quarantine(self, locator: str, reason: str) -> None:
        if not self.opts.lenient:
            raise SourceError(f"{locator}: {reason}")
        self.quarantined.append(QuarantineItem(source_id=self.source_id, locator=locator,
                                               reason=reason))
        self.dq["dq.quarantined"] += 1

    def hashed(self, value: object) -> str | None:
        """``h_`` pseudonym under the name key (None without a key or for non-strings)."""
        if not isinstance(value, str) or not value or not self.name_key:
            return None
        self.names_used = True
        return pseudonym(self.name_key, "h", value)

    def name(self, value: object) -> str | None:
        """A tool / MCP name: clear when allowlisted, else ``h_``."""
        if not isinstance(value, str) or not value:
            return None
        if value in self.allow and _match(value, _MODEL_RE):
            return value
        return self.hashed(value)

    # --- contexts ---
    def pricing(self, model_raw: str, routing_mode: str | None, tier: str | None,
                hint: str | None) -> PricingContext:
        key = (model_raw, routing_mode, tier, hint)
        ctx = self._ctx_cache.get(key)
        if ctx is None:
            cm = normalize_copilot_model(model_raw)
            routing = cm.routing
            if routing == "direct" and routing_mode == "auto":
                routing = "auto"
            ctx = self._ctx_cache[key] = PricingContext(
                provider=PROVIDER, channel=CHANNEL, model=cm.model, model_raw=model_raw,
                service_tier="standard", speed=cm.speed, inference_geo=None,
                endpoint_scope="unknown", write_ttl_hint=hint, billing_path=self.billing_path,
                routing=routing, compliance=self.compliance,
                context_tier=tier if tier in _CONTEXT_TIERS else None)
        return ctx

    def attribution(self, session: SessionState | None, query_source: str | None) -> Attribution:
        repo = session.repo if session is not None else None
        cwd = session.cwd if session is not None else None
        version = session.version if session is not None else None
        key = (repo, cwd, version, query_source)
        attr = self._attr_cache.get(key)
        if attr is None:
            fields: dict[str, Any] = {"query_source": query_source}
            if repo is not None:
                fields["repo"] = repo
            if cwd is not None:
                fields["cwd_key"] = cwd
            if version is not None:
                fields["client_version"] = version
            attr = self._attr_cache[key] = dataclasses.replace(self.base_attr, **fields)
        return attr

    def shell(self, lane_key: str, session_key: str, kind: LaneKind, parent: str | None) -> None:
        self.shells.setdefault(lane_key, (session_key, kind, parent))

    def touch(self, session_key: str, ts: int | None) -> None:
        if ts is None:
            return
        span = self.spans.get(session_key)
        if span is None:
            self.spans[session_key] = [ts, ts]
        else:
            span[0] = min(span[0], ts)
            span[1] = max(span[1], ts)

    def source_ref(self, locator: str) -> SourceRef:
        return SourceRef(adapter=ADAPTER_NAME, source_id=self.source_id, locator=locator,
                         fidelity=Fidelity.NO_TTL_SPLIT, priority=SOURCE_PRIORITY)

    # --- result ---
    def _in_window(self, ts: int) -> bool:
        opts = self.opts
        if opts.since_ms is not None and ts < opts.since_ms:
            return False
        return not (opts.until_ms is not None and ts >= opts.until_ms)

    def notes(self, extra: Iterable[DataQualityNote] = ()) -> list[DataQualityNote]:
        notes = list(extra)
        dq = Counter(self.dq)
        if self.billing_assumed and (self.requests or self.aggregates):
            dq["dq.copilot_billing_path_assumed"] += 1
        for code, count in sorted(dq.items()):
            if count <= 0:
                continue
            detail = _DETAILS.get(code, code)
            if code == "dq.unknown_entry_type":
                detail = "unknown event types: " + ", ".join(
                    f"{k}={v}" for k, v in sorted(self.unknown_types.items()))
            tokens = self.dq_tokens.get(code)
            notes.append(DataQualityNote(code=code, severity=_SEVERITY.get(code, "info"),
                                         count=count, detail=detail[:_DETAIL_MAX],
                                         tokens=tokens or None))
        notes.sort(key=lambda n: (n.code, n.detail))
        return notes

    def source_info(self, path: Path, sha256: str, n_bytes: int) -> SourceInfo:
        name_hmac = ""
        if self.name_key:
            name_hmac = pseudonym(self.name_key, "h", "/".join(Path(path).parts[-2:]))
        principal = self.principal
        pkid = None
        if principal and principal[:2] in ("p_", "c_"):
            pkid = self.principal_key_id or self.opts.principal_key_id or (
                key_id(self.opts.principal_key) if self.opts.principal_key else None)
        return SourceInfo(source_id=self.source_id, adapter=ADAPTER_NAME, name_hmac=name_hmac,
                          sha256=sha256, bytes=n_bytes,
                          name_key_id=self.name_key_id if (self.names_used or name_hmac)
                          else None,
                          principal_key_id=pkid)

    def result(self, source: SourceInfo, extra_notes: Iterable[DataQualityNote] = ()
               ) -> IngestResult:
        """Assemble the :class:`IngestResult` (sessions and lane shells, notes, stats)."""
        requests = sorted((r for r in self.requests if self._in_window(r.ts_start_ms)),
                          key=lambda r: (r.session_key, r.lane_key, r.ts_start_ms, r.seq,
                                         r.request_id))
        events = sorted((e for e in self.events if self._in_window(e.ts_ms)),
                        key=lambda e: (e.lane_key, e.ts_ms, e.kind.value,
                                       tuple((k, repr(v)) for k, v in e.attrs)))
        aggregates = sorted((a for a in self.aggregates if self._in_window(a.bucket_start_ms)),
                            key=lambda a: (a.bucket_start_ms, a.agg_id))
        by_session: dict[str, list[Lane]] = {}
        for lane_key, (session_key, kind, parent) in sorted(self.shells.items()):
            by_session.setdefault(session_key, []).append(Lane(
                lane_key=lane_key, session_key=session_key, kind=kind, parent_lane_key=parent,
                cache_scope_key="unknown", requests=()))
        sessions = []
        for session_key, lanes in sorted(by_session.items()):
            span = self.spans.get(session_key, [0, 0])
            sessions.append(Session(
                session_key=session_key, source_kind=SOURCE_KIND,
                attribution=self.session_attr.get(session_key, self.base_attr),
                lanes=tuple(lanes), started_ms=span[0], ended_ms=span[1]))
        caps: set[str] = set()
        if requests:
            caps |= {"timing", "params", "lanes_exact"}
        if events:
            caps.add("events")
        if self.saw_credits and (requests or events or aggregates):
            caps.add("credits")
        if any(r.appended for r in requests):
            caps.add("appended")
        if self.saw_store_requests and requests:
            caps.add("usage_sequence")
        stats = dict(sorted(self.stats.items()))
        stats["requests"] = len(requests)
        stats["events"] = len(events)
        stats["aggregates"] = len(aggregates)
        stats["quarantined"] = len(self.quarantined)
        return IngestResult(
            source=source, requests=requests, sessions=sessions, events=events,
            aggregates=aggregates, cost_lines=[], outcomes=[],
            quarantined=list(self.quarantined), notes=self.notes(extra_notes), stats=stats,
            capabilities=frozenset(caps))


# ---------------------------------------------------------------------------------------------
# per-session parser (resumable)
# ---------------------------------------------------------------------------------------------

@dataclass
class MsgRecord:
    """One assistant API call seen in ``events.jsonl`` (content-free; a collector context item)."""

    rid: str                   # request id
    api: str | None            # apiCallId → provider_message_id
    lane: str                  # lane key
    kind: str                  # "main" | "subagent"
    ts: int                    # first chunk timestamp (ms)
    seq: int
    leg: int                   # session leg index at the first chunk
    locator: str
    model: str = ""            # raw model token
    out: int = 0               # max outputTokens over the chunks
    preq: str | None = None    # requestId (x-github-request-id)
    turn: int | None = None
    app: list[list[Any]] = field(default_factory=list)   # [kind, name, n_bytes, is_error]
    routing: str | None = None
    tier: str | None = None
    effort: str | None = None
    ttl: dict[str, str] = field(default_factory=dict)    # normalized model → write_ttl_hint
    expect: int | None = None  # chunkCount
    seen_last: bool = False
    offset: int = 0

    @property
    def open(self) -> bool:
        """True while a chunked response still misses its last chunk."""
        return self.expect is not None and not self.seen_last

    def to_json(self) -> dict[str, Any]:
        """Collector-context form."""
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: object) -> MsgRecord:
        """Parse a context item (``ValueError`` when malformed)."""
        if not isinstance(d, Mapping):
            raise ValueError("message")
        try:
            rec = cls(**{f.name: d[f.name] for f in dataclasses.fields(cls) if f.name in d})
        except TypeError:
            raise ValueError("message") from None
        if (not isinstance(rec.rid, str) or not isinstance(rec.lane, str)
                or type(rec.ts) is not int or type(rec.seq) is not int
                or type(rec.out) is not int or not isinstance(rec.app, list)
                or not isinstance(rec.ttl, dict)):
            raise ValueError("message")
        return rec


@dataclass
class LegRollup:
    """The per-model deltas of one ``session.shutdown`` (``(model, counts, nano_aiu)``)."""

    leg: int
    start_ms: int
    end_ms: int
    items: list[tuple[str, dict[str, int], int | None]]


@dataclass
class SessionState:
    """Session-level state of one ``events.jsonl`` (content-free: tokens, ``h_`` values, ints)."""

    dir_id: str
    raw_sid: str | None = None
    version: str | None = None
    repo: str | None = None
    cwd: str | None = None
    model: str | None = None           # current model token
    routing: str | None = None         # "auto" | "direct" | None
    tier: str | None = None
    effort: str | None = None
    ttl: dict[str, str] = field(default_factory=dict)
    seq: dict[str, int] = field(default_factory=dict)
    pend: dict[str, list[list[Any]]] = field(default_factory=dict)
    tools: dict[str, str | None] = field(default_factory=dict)
    lanes: dict[str, list[Any]] = field(default_factory=dict)   # lane key → [kind, parent]
    leg: int = -1
    leg_start: int = 0
    leg_comp: bool = False
    leg_shut: bool = False
    closed_until: int = -1             # rows created at or before this ms belong to closed legs
    prev: dict[str, list[int | None]] = field(default_factory=dict)
    span: list[int] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)  # store mode: held messages
    has_rows: bool = False

    @property
    def session_key(self) -> str:
        """``core.ids.copilot_session_key`` of the raw session id (else the directory name)."""
        return copilot_session_key(self.raw_sid or self.dir_id)

    def to_json(self) -> dict[str, Any]:
        """Collector-context form."""
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: object) -> SessionState:
        """Parse a collector context (``ValueError`` when malformed)."""
        if not isinstance(d, Mapping) or not isinstance(d.get("dir_id"), str):
            raise ValueError("context")
        try:
            st = cls(**{f.name: d[f.name] for f in dataclasses.fields(cls) if f.name in d})
        except TypeError:
            raise ValueError("context") from None
        dicts = (st.ttl, st.seq, st.pend, st.tools, st.lanes, st.prev)
        if (not all(isinstance(x, dict) for x in dicts) or not isinstance(st.span, list)
                or not isinstance(st.pending, list) or type(st.leg) is not int
                or type(st.closed_until) is not int):
            raise ValueError("context")
        st.tools = OrderedDict(st.tools)
        return st


class SessionParser:
    """Parses one ``session-state/<id>/events.jsonl`` into content-free records.

    Store-independent output is collected on the parser (``events``, ``compactions``,
    ``messages``, ``legs``); :func:`emit_session` turns it into requests and aggregates once the
    store rows (if any) are known. ``state`` is the resumable context at the last parsed offset."""

    def __init__(self, run: _Run, path: Path, *, state: SessionState | None = None,
                 locator_prefix: str = "") -> None:
        self.run = run
        self.path = Path(path)
        self.state = state if state is not None else SessionState(dir_id=self.path.parent.name)
        if not isinstance(self.state.tools, OrderedDict):
            self.state.tools = OrderedDict(self.state.tools)
        self.prefix = locator_prefix
        self.events: list[LaneEvent] = []
        self.compactions: list[tuple[int, str, Request]] = []   # (ts, normalized model, request)
        self.messages: list[MsgRecord] = []
        self.legs: list[LegRollup] = []
        self.groups: dict[str, MsgRecord] = {}
        self.open_offset: int | None = None
        self.unterminated: int | None = None
        self.lines = 0

    # --- lanes ---
    @property
    def main_key(self) -> str:
        return copilot_lane_key(self.state.session_key, LaneKind.MAIN.value, None)

    def _lane(self, key: str, kind: LaneKind, parent: str | None) -> str:
        st = self.state
        if key not in st.lanes:
            st.lanes[key] = [kind.value, parent]
        self.run.shell(key, st.session_key, kind, parent)
        return key

    def lane_for(self, agent: str | None) -> tuple[str, LaneKind]:
        main = self._lane(self.main_key, LaneKind.MAIN, None)
        if agent is None:
            return main, LaneKind.MAIN
        key = copilot_lane_key(self.state.session_key, LaneKind.SUBAGENT.value, agent)
        return self._lane(key, LaneKind.SUBAGENT, main), LaneKind.SUBAGENT

    def compaction_lane(self, agent: str | None) -> str:
        parent, _ = self.lane_for(agent)
        key = copilot_lane_key(self.state.session_key, LaneKind.COMPACTION.value, agent)
        return self._lane(key, LaneKind.COMPACTION, parent)

    def restore_shells(self) -> None:
        """Re-register the lane shells of a restored context with the run."""
        sk = self.state.session_key
        for key, (kind, parent) in sorted(self.state.lanes.items()):
            self.run.shell(key, sk, LaneKind(kind), parent)

    def next_seq(self, lane_key: str) -> int:
        n = self.state.seq.get(lane_key, 0)
        self.state.seq[lane_key] = n + 1
        return n

    def event(self, lane_key: str, ts: int, kind: LaneEventKind,
              attrs: Mapping[str, object]) -> None:
        pairs = tuple(sorted(((k, v) for k, v in attrs.items() if v is not None),
                             key=lambda kv: kv[0]))
        try:
            ev = LaneEvent(lane_key=lane_key, ts_ms=ts, kind=kind, attrs=pairs)  # type: ignore[arg-type]
        except ContractViolation:
            raise _Bad("bad_usage") from None
        self.events.append(ev)

    # --- parse loop ---
    def parse(self, *, start_offset: int = 0, stop_at: int | None = None, final: bool = True,
              size: int | None = None, flag_unclean_eof: bool = False) -> None:
        """Parse from *start_offset* to *stop_at* (or the end). Without *final*, an unterminated
        last line and chunked responses still missing their last chunk are left for a later
        pass (:attr:`unterminated`, :attr:`open_offset`)."""
        path = self.path
        if size is None:
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None
        ends_nl = True
        if size > 0 and not final:
            try:
                with open(path, "rb") as f:
                    f.seek(size - 1)
                    ends_nl = f.read(1) == b"\n"
            except OSError as exc:
                raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None
        for _line_no, offset, raw in iter_lines(path, start_offset=start_offset):
            if offset >= size or (stop_at is not None and offset >= stop_at):
                break
            if not final and not ends_nl and raw and offset + len(raw) >= size:
                self.unterminated = offset
                break
            self.lines += 1
            self.run.stats["lines"] += 1
            locator = f"{self.prefix}offset:{offset}"
            if not raw:
                self.run.quarantine(locator, "oversize_line")
                continue
            objs, recovered = parse_event_line(raw)
            if not objs:
                self.run.quarantine(locator, "bad_json")
                continue
            if recovered:
                self.run.dq["dq.copilot_concatenated_line"] += 1
            for obj in objs:
                try:
                    self.handle(obj, offset, locator)
                except _Bad as bad:
                    self.run.quarantine(locator, bad.reason)
        for key in list(self.groups):
            grp = self.groups[key]
            if final or not grp.open:
                self.messages.append(self.groups.pop(key))
        if self.groups:
            self.open_offset = min(g.offset for g in self.groups.values())
        if flag_unclean_eof and self.state.leg >= 0 and not self.state.leg_shut:
            self.run.dq["dq.copilot_unclean_shutdown"] += 1

    def handle(self, obj: Mapping[str, Any], offset: int, locator: str) -> None:
        """Dispatch one event object."""
        typ = obj.get("type")
        if not isinstance(typ, str):
            raise _Bad("missing:type" if typ is None else "bad_type:type")
        if obj.get("ephemeral") is True:
            self.run.stats["ephemeral_skipped"] += 1
            return
        data = obj.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise _Bad("bad_type:data")
        ts = parse_ts_ms(obj.get("timestamp"))
        if ts is None:
            raise _Bad("missing:timestamp" if obj.get("timestamp") is None
                       else "bad_type:timestamp")
        agent = _match(obj.get("agentId"), _ID_RE)
        handler = _HANDLERS.get(typ)
        if handler is not None:
            handler(self, data, ts, agent, obj, offset, locator)
        elif typ in KNOWN_EVENT_TYPES:
            self.run.stats["events_ignored"] += 1
        else:
            self.run.dq["dq.unknown_entry_type"] += 1
            name = typ if len(typ) <= 48 and _TYPE_NAME_RE.match(typ) else "other"
            self.run.unknown_types[name] += 1
        st = self.state
        if st.span:
            st.span = [min(st.span[0], ts), max(st.span[1], ts)]
        else:
            st.span = [ts, ts]
        self.run.touch(st.session_key, ts)

    # --- handlers ---
    def _context(self, ctx: object) -> None:
        if not isinstance(ctx, Mapping):
            return
        repo = self.run.hashed(ctx.get("repository"))
        cwd = self.run.hashed(ctx.get("cwd"))
        if repo is not None:
            self.state.repo = repo
        if cwd is not None:
            self.state.cwd = cwd

    def _selection(self, model: object) -> str | None:
        """Apply a selected model; returns the routing mode it implies."""
        token = _match(model, _MODEL_RE)
        if token is None:
            return None
        self.state.model = token
        mode = "auto" if token.strip().lower() == "auto" else "direct"
        self.state.routing = mode
        return mode

    def _tier(self, data: Mapping[str, Any]) -> None:
        if "contextTier" in data:
            tier = data.get("contextTier")
            self.state.tier = tier if tier in _CONTEXT_TIERS else None

    def _effort(self, data: Mapping[str, Any], key: str = "reasoningEffort") -> None:
        if key in data:
            self.state.effort = _match(data.get(key), _ENUM_RE)

    def _new_leg(self, ts: int) -> None:
        st = self.state
        if st.leg >= 0:
            if not st.leg_shut:
                self.run.dq["dq.copilot_unclean_shutdown"] += 1
            st.closed_until = max(st.closed_until, ts - 1)
        st.leg += 1
        st.leg_start = ts
        st.leg_comp = False
        st.leg_shut = False

    def _session_meta(self, data: Mapping[str, Any], ts: int, routing: str | None, *,
                      always: bool = True) -> None:
        limits = data.get("sessionLimits")
        limit = (_credit_limit_nano(limits.get("maxAiCredits"))
                 if isinstance(limits, Mapping) else None)
        if not always and limit is None:
            return
        main, _ = self.lane_for(None)
        self.event(main, ts, LaneEventKind.SESSION_META, {
            "routing_mode": routing, "context_tier": self.state.tier,
            "credit_limit_nano": limit})

    def on_start(self, data, ts, agent, obj, offset, locator) -> None:
        st = self.state
        sid = _match(data.get("sessionId"), _ID_RE)
        if st.raw_sid is None and sid is not None and not st.lanes:
            st.raw_sid = sid
        version = _match(data.get("copilotVersion"), _VERSION_RE)
        if version is not None:
            if st.version is not None and st.version != version:
                main, _ = self.lane_for(None)
                self.event(main, ts, LaneEventKind.UPGRADE,
                           {"from_version": st.version, "to_version": version})
            st.version = version
        self._new_leg(ts)
        self._context(data.get("context"))
        routing = self._selection(data.get("selectedModel"))
        self._tier(data)
        self._effort(data)
        self._session_meta(data, ts, routing)

    def on_resume(self, data, ts, agent, obj, offset, locator) -> None:
        self._new_leg(ts)
        self._context(data.get("context"))
        routing = self._selection(data.get("selectedModel"))
        self._tier(data)
        self._effort(data)
        self._session_meta(data, ts, routing)

    def on_context(self, data, ts, agent, obj, offset, locator) -> None:
        self._context(data)

    def on_limits(self, data, ts, agent, obj, offset, locator) -> None:
        self._session_meta(data, ts, None, always=False)

    def on_model_change(self, data, ts, agent, obj, offset, locator) -> None:
        st = self.state
        new = _match(data.get("newModel"), _MODEL_RE)
        if new is None:
            raise _Bad("missing:newModel")
        previous = _match(data.get("previousModel"), _MODEL_RE) or st.model
        self._selection(new)
        self._tier(data)
        self._effort(data)
        cause = data.get("cause")
        lane, _ = self.lane_for(agent)
        attrs: dict[str, object] = {"to_model": _model_label(new),
                                    "from_model": _model_label(previous) if previous else None}
        if data.get("source") == "automatic" or cause in _FALLBACK_CAUSES:
            attrs["trigger"] = _FALLBACK_CAUSES.get(cause if isinstance(cause, str) else "",
                                                    "unknown")
            self.event(lane, ts, LaneEventKind.MODEL_FALLBACK, attrs)
        else:
            self.event(lane, ts, LaneEventKind.MODEL_SWITCH_USER, attrs)

    def on_auto_resolved(self, data, ts, agent, obj, offset, locator) -> None:
        self.state.routing = "auto"
        self.run.stats["auto_mode_resolved"] += 1

    def on_truncation(self, data, ts, agent, obj, offset, locator) -> None:
        removed = _count(data.get("tokensRemovedDuringTruncation"))
        if removed is None:
            raise _Bad("bad_type:tokensRemovedDuringTruncation")
        lane, _ = self.lane_for(agent)
        self.event(lane, ts, LaneEventKind.CONTEXT_EDIT,
                   {"edit_type": "copilot_truncation", "cleared_input_tokens": removed})

    def on_error(self, data, ts, agent, obj, offset, locator) -> None:
        status = _int(data.get("statusCode"))
        lane, _ = self.lane_for(agent)
        self.event(lane, ts, LaneEventKind.API_ERROR, {
            "status": status if status is not None and 100 <= status <= 599 else None,
            "error_type": _match(data.get("errorType"), _ENUM_RE) or "other"})

    def on_checkpoint(self, data, ts, agent, obj, offset, locator) -> None:
        nano = _nano_aiu(data.get("totalNanoAiu"))
        if nano is None:
            raise _Bad("bad_type:totalNanoAiu")
        lane, _ = self.lane_for(agent)
        self.event(lane, ts, LaneEventKind.COST_STATE,
                   {"reported_total_nano": nano_aiu_to_nano(nano)[0],
                    "reporter": CHECKPOINT_REPORTER})
        self.run.saw_credits = True
        states = data.get("modelCacheState")
        if isinstance(states, list):
            for item in states[:64]:
                if not isinstance(item, Mapping):
                    continue
                model = _match(item.get("modelId"), _MODEL_RE)
                if model is None:
                    continue
                norm = normalize_copilot_model(model).model or model
                hint = _TTL_HINTS.get(_int(item.get("cacheTtlSeconds")) or -1)
                if hint is None:
                    self.state.ttl.pop(norm, None)
                else:
                    self.state.ttl[norm] = hint

    def on_tool_start(self, data, ts, agent, obj, offset, locator) -> None:
        call = _match(data.get("toolCallId"), _ID_RE)
        if call is None:
            return
        tools = self.state.tools
        tools[stable_id("tc", call)] = self.run.name(data.get("toolName"))
        while len(tools) > _TOOL_WINDOW:
            tools.popitem(last=False)  # type: ignore[call-arg]

    def _append(self, lane: str, kind: str, name: str | None, n_bytes: int,
                is_error: bool = False) -> None:
        pend = self.state.pend.setdefault(lane, [])
        if len(pend) < _PENDING_MAX:
            pend.append([kind, name, min(n_bytes, MAX_TOKENS), is_error])

    def on_tool_complete(self, data, ts, agent, obj, offset, locator) -> None:
        call = _match(data.get("toolCallId"), _ID_RE)
        name = self.state.tools.pop(stable_id("tc", call), None) if call else None
        if name is None:
            desc = data.get("toolDescription")
            if isinstance(desc, Mapping):
                name = self.run.name(desc.get("name"))
        result = data.get("result")
        content = result.get("content") if isinstance(result, Mapping) else None
        n_bytes = len(content.encode("utf-8", "surrogatepass")) if isinstance(content, str) \
            else 0
        lane, _ = self.lane_for(agent)
        self._append(lane, "tool_result", name, n_bytes, data.get("success") is False)
        self.run.stats["tool_results"] += 1

    def on_user(self, data, ts, agent, obj, offset, locator) -> None:
        content = data.get("content")
        n_bytes = len(content.encode("utf-8", "surrogatepass")) if isinstance(content, str) \
            else 0
        lane, _ = self.lane_for(agent)
        self._append(lane, "user_text", None, n_bytes)
        self.event(lane, ts, LaneEventKind.HUMAN_PROMPT, {})

    def on_message(self, data, ts, agent, obj, offset, locator) -> None:
        st = self.state
        api = _match(data.get("apiCallId"), _ID_RE)
        event_id = _match(obj.get("id"), _ID_RE)
        key = api or ("ev:" + (event_id or f"off:{offset}"))
        grp = self.groups.get(key)
        if grp is None:
            lane, kind = self.lane_for(agent)
            if api is not None:
                rid = request_id_for(PROVIDER, api, "", "")
            else:
                rid = stable_id("rq", ADAPTER_NAME, st.session_key, key)
            grp = MsgRecord(rid=rid, api=api, lane=lane, kind=kind.value, ts=ts,
                            seq=self.next_seq(lane), leg=st.leg, locator=locator,
                            app=st.pend.pop(lane, []), routing=st.routing, tier=st.tier,
                            effort=st.effort, ttl=dict(st.ttl), offset=offset,
                            model=st.model or "")
            self.groups[key] = grp
        model = _match(data.get("model"), _MODEL_RE)
        if model is not None:
            grp.model = model
        out = _count(data.get("outputTokens"))
        if out is not None:
            grp.out = max(grp.out, out)
        if grp.preq is None:
            grp.preq = _match(data.get("requestId"), _ID_RE)
        turn = data.get("turnId")
        if grp.turn is None and isinstance(turn, str) and turn.isdigit() and len(turn) <= 9:
            grp.turn = int(turn)
        count = _count(data.get("chunkCount"))
        index = _count(data.get("chunkIndex"))
        if count is not None and count >= 1:
            grp.expect = count
            if index is not None and index >= count - 1:
                grp.seen_last = True
        if not grp.open:
            self.messages.append(self.groups.pop(key))

    def on_compaction(self, data, ts, agent, obj, offset, locator) -> None:
        st = self.state
        used = data.get("compactionTokensUsed")
        used = used if isinstance(used, Mapping) else {}
        success = data.get("success") is True
        lane, _ = self.lane_for(agent)
        if success:
            st.leg_comp = True
            trigger = data.get("trigger")
            copilot_trigger = trigger if trigger in _COMPACTION_TRIGGERS else None
            self.event(lane, ts, LaneEventKind.COMPACTION, {
                "trigger": "manual" if trigger == "manual" else "auto",
                "copilot_trigger": copilot_trigger,
                "pre_tokens": _count(data.get("preCompactionTokens")),
                "post_tokens": _count(data.get("postCompactionTokens")),
                "duration_ms": _count(used.get("duration")),
                "dropped_tokens": _count(data.get("tokensRemoved")),
                "system_tokens": _count(data.get("systemTokens")),
                "tool_definitions_tokens": _count(data.get("toolDefinitionsTokens"))})
        usage = used.get("copilotUsage")
        if not isinstance(usage, Mapping) or "tokenDetails" not in usage:
            return
        try:
            buckets, notes = normalize_token_details(usage)
        except BadUsageError:
            self.run.quarantine(locator, "bad_usage")
            return
        for code in notes:
            self.run.dq[code] += 1
        total = _nano_aiu(usage.get("totalNanoAiu"))
        model_raw = (_match(used.get("model"), _MODEL_RE) or _match(usage.get("model"), _MODEL_RE)
                     or _detail_model(usage) or st.model or "")
        # a runtime-initiated call on a named model: routing from the label only (never the
        # session's Auto selection; VERIFY whether GitHub applies the Auto discount to it)
        ctx = self.run.pricing(model_raw, None, st.tier,
                               st.ttl.get(normalize_copilot_model(model_raw).model))
        billable, rule = billing_rule(ctx.model, nano_aiu=total,
                                      interaction_type="conversation-compaction")
        event_id = _match(obj.get("id"), _ID_RE) or f"off:{offset}"
        comp_lane = self.compaction_lane(agent)
        request_id = stable_id("rq", ADAPTER_NAME, st.session_key, "compaction", event_id)
        cost = nano_aiu_to_nano(total)[0] if total is not None else None
        inf = Inference(
            inference_id=stable_id("inf", request_id, 0), kind=InferenceKind.COMPACTION,
            usage=buckets, pricing=ctx, usage_source=UsageSource.FINAL, billable=billable,
            billing_rule_id=rule, provider_reported_cost_nano=cost,
            provider_reported_cost_basis="provider_estimate" if cost is not None else None)
        raw_json = _canonical({"tokenDetails": [
            {k: item[k] for k in ("tokenType", "tokenCount", "batchSize", "costPerBatch")
             if k in item} for item in usage["tokenDetails"]],
            **({"totalNanoAiu": usage["totalNanoAiu"]} if "totalNanoAiu" in usage else {})})
        attempt = Attempt(
            attempt_id=stable_id("at", request_id, 0), attempt_no=0, ts_start_ms=ts, ttft_ms=None,
            duration_ms=_count(used.get("duration")),
            outcome=Outcome.OK if success else Outcome.UNKNOWN, http_status=None,
            error_type=None, retry_layer=None, retry_after_ms=None, should_retry=None,
            provider_request_id=_match(data.get("requestId"), _ID_RE), provider_message_id=None,
            model_served=model_raw or None, stop_reason=None, inferences=(inf,),
            raw_usage_json=raw_json if len(raw_json) <= 8192 else None,
            convention_id=CONVENTION_TOKEN_DETAILS)
        req = Request(
            request_id=request_id, session_key=st.session_key, lane_key=comp_lane,
            seq=self.next_seq(comp_lane),
            attribution=self.run.attribution(st, LaneKind.COMPACTION.value),
            params=RequestParams(model_requested=model_raw, effort=st.effort,
                                 session_effort=st.effort),
            attempts=(attempt,), source=self.run.source_ref(locator))
        if cost is not None:
            self.run.saw_credits = True
        self.compactions.append((ts, ctx.model, req))

    def on_shutdown(self, data, ts, agent, obj, offset, locator) -> None:
        st = self.state
        metrics = data.get("modelMetrics")
        if not isinstance(metrics, Mapping):
            raise _Bad("missing:modelMetrics" if metrics is None else "bad_type:modelMetrics")
        cumulative: dict[str, list[int | None]] = {}
        for model, metric in sorted(metrics.items(), key=lambda kv: str(kv[0])):
            token = _match(model, _MODEL_RE)
            if token is None or not isinstance(metric, Mapping):
                raise _Bad("bad_usage")
            usage = metric.get("usage")
            if not isinstance(usage, Mapping):
                raise _Bad("bad_usage")
            counts = [usage.get(k) for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens",
                                             "outputTokens")]
            if any(_count(c if c is not None else 0) is None for c in counts):
                raise _Bad("bad_usage")
            reasoning = usage.get("reasoningTokens")
            if reasoning is not None and _count(reasoning) is None:
                raise _Bad("bad_usage")
            nano = _nano_aiu(metric.get("totalNanoAiu"))
            cumulative[token] = [*(c or 0 for c in counts), reasoning, nano]
        if st.leg < 0:
            self._new_leg(ts)
        items: list[tuple[str, dict[str, int], int | None]] = []
        differenced = False
        for model, cum in cumulative.items():
            prev = st.prev.get(model)
            delta = list(cum)
            if (not st.leg_comp and prev is not None
                    and all(c is None or p is None or c >= p for c, p in zip(cum, prev,
                                                                           strict=True))):
                delta = [None if c is None else c - (p or 0) for c, p in zip(cum, prev,
                                                                          strict=True)]
                differenced = True
            counts = {"inputTokens": delta[0] or 0, "cacheReadTokens": delta[1] or 0,
                      "cacheWriteTokens": delta[2] or 0, "outputTokens": delta[3] or 0}
            if delta[4] is not None:
                counts["reasoningTokens"] = delta[4]
            if any(counts.values()) or delta[5]:
                items.append((model, counts, delta[5]))
        if st.leg_comp:
            self.run.dq["dq.copilot_compaction_reset"] += 1
        if differenced and st.leg > 0:
            self.run.dq["dq.copilot_resume_legs"] += 1
        if data.get("shutdownType") == "error":
            self.run.dq["dq.copilot_unclean_shutdown"] += 1
        st.prev = cumulative
        st.leg_shut = True
        st.closed_until = max(st.closed_until, ts)
        self.legs.append(LegRollup(leg=st.leg, start_ms=st.leg_start, end_ms=ts, items=items))


def _model_label(model: str) -> str:
    """A model-switch attr: the normalized Copilot id, else the (safe) raw token."""
    return normalize_copilot_model(model).model or model.strip().lower()


def _detail_model(usage: Mapping[str, Any]) -> str | None:
    details = usage.get("tokenDetails")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, Mapping):
                model = _match(item.get("model"), _MODEL_RE)
                if model is not None:
                    return model
    return None


_HANDLERS: dict[str, Any] = {
    "session.start": SessionParser.on_start,
    "session.resume": SessionParser.on_resume,
    "session.context_changed": SessionParser.on_context,
    "session.session_limits_changed": SessionParser.on_limits,
    "session.model_change": SessionParser.on_model_change,
    "session.auto_mode_resolved": SessionParser.on_auto_resolved,
    "session.truncation": SessionParser.on_truncation,
    "session.error": SessionParser.on_error,
    "session.usage_checkpoint": SessionParser.on_checkpoint,
    "session.compaction_complete": SessionParser.on_compaction,
    "session.shutdown": SessionParser.on_shutdown,
    "tool.execution_start": SessionParser.on_tool_start,
    "tool.execution_complete": SessionParser.on_tool_complete,
    "user.message": SessionParser.on_user,
    "assistant.message": SessionParser.on_message,
}


# ---------------------------------------------------------------------------------------------
# session store (experimental)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreRow:
    """One mapped ``assistant_usage_events`` row (content-free)."""

    row_id: int
    session_id: str
    model: str                 # raw model token used for pricing
    created_ms: int
    usage: UsageBuckets        # input side, plus output when the column exists
    has_output: bool
    turn: int | None
    nano_aiu: int | None
    duration_ms: int | None
    initiator: str | None
    raw_json: str


@dataclass
class StoreRead:
    """Result of one read of ``session-store.db``."""

    status: str                           # "ok" | "absent" | "unavailable" | "schema"
    columns: frozenset[str] = frozenset()
    rows: list[StoreRow] = field(default_factory=list)
    sessions: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    max_id: int | None = None
    missing_optional: tuple[str, ...] = ()
    bad_ids: list[int] = field(default_factory=list)   # quarantined row ids


def map_store_row(values: Mapping[str, object]) -> StoreRow:
    """Map one ``assistant_usage_events`` row (column → value) to a :class:`StoreRow`.

    Raises :class:`~tokenbill.core.conventions.BadUsageError` for bad token counts (a
    :class:`NegativeUncachedError` when read + write exceed the inclusive input) and
    :class:`~tokenbill.core.errors.SourceError` (``bad_type:<column>``) for other bad values; only
    :class:`~tokenbill.core.errors.TokenbillError` subclasses escape."""
    if not isinstance(values, Mapping):
        raise SourceError("bad_type:row")
    row_id = values.get("id")
    if type(row_id) is not int or row_id < 0:
        raise SourceError("bad_type:id")
    session = _match(values.get("session_id"), _ID_RE)
    if session is None:
        raise SourceError("bad_type:session_id")
    model = _match(values.get("model"), _MODEL_RE)
    billing_model = _match(values.get("copilot_usage_model"), _MODEL_RE)
    if model is None or not normalize_copilot_model(model).model:
        model = billing_model or model
    if model is None:
        raise SourceError("bad_type:model")
    created = values.get("created_at")
    if isinstance(created, Decimal) or type(created) is float:
        number = exact_number(created)
        created = round(number) if number is not None else None
    created_ms = parse_ts_ms(created)
    if created_ms is None:
        raise SourceError("bad_type:created_at")
    raw: dict[str, object] = {}
    for col in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
                "reasoning_tokens"):
        if col in values and values[col] is not None:
            raw[col] = values[col]
    usage, _notes = normalize_session_store(raw)
    turn = values.get("turn_index")
    if turn is not None and (type(turn) is not int or turn < 0):
        raise SourceError("bad_type:turn_index")
    nano_raw = values.get("total_nano_aiu")
    nano = _nano_aiu(nano_raw) if nano_raw is not None else None
    if nano_raw is not None and nano is None:
        raise SourceError("bad_type:total_nano_aiu")
    duration = values.get("duration_ms")
    duration = _count(duration) if duration is not None else None
    initiator = values.get("initiator")
    initiator = _match(initiator, _ENUM_RE) if initiator is not None else None
    if nano is not None:
        raw["total_nano_aiu"] = nano
    return StoreRow(row_id=row_id, session_id=session, model=model, created_ms=created_ms,
                    usage=usage, has_output="output_tokens" in values, turn=turn,
                    nano_aiu=nano, duration_ms=duration, initiator=initiator,
                    raw_json=_canonical(raw))


def _store_connect(path: Path) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(str(Path(path).resolve())) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=STORE_TIMEOUT_S)


def _columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")'))


def _read_store_once(path: Path, run: _Run, after_id: int) -> StoreRead:
    conn = _store_connect(path)
    try:
        cols = _columns(conn, "assistant_usage_events")
        if not cols:
            run.dq["dq.copilot_store_schema"] += 1
            return StoreRead(status="schema")
        missing = [c for c in STORE_REQUIRED if c not in cols]
        if missing:
            run.dq["dq.copilot_store_schema"] += 1
            return StoreRead(status="schema", columns=cols)
        optional = tuple(c for c in STORE_OPTIONAL if c not in cols)
        if optional:
            run.dq["dq.copilot_store_schema"] += 1
        selected = [c for c in (*STORE_REQUIRED, *STORE_OPTIONAL) if c in cols]
        sql = ("SELECT " + ", ".join(f'"{c}"' for c in selected)
               + ' FROM "assistant_usage_events" WHERE "id" > ? ORDER BY "id"')
        out = StoreRead(status="ok", columns=cols, missing_optional=optional)
        for values in conn.execute(sql, (after_id,)):
            mapping = dict(zip(selected, values, strict=True))
            rid = mapping.get("id")
            if type(rid) is int:
                out.max_id = rid if out.max_id is None else max(out.max_id, rid)
            locator = f"store:row:{rid if type(rid) is int else '?'}"
            try:
                out.rows.append(map_store_row(mapping))
                continue
            except NegativeUncachedError:
                run.dq[DQ_CONVENTION_MISMATCH] += 1
                reason = "bad_usage"
            except BadUsageError:
                reason = "bad_usage"
            except SourceError as exc:
                reason = str(exc)[:64]
            if type(rid) is int:
                out.bad_ids.append(rid)
            run.quarantine(locator, reason)
        session_cols = _columns(conn, "sessions")
        if "id" in session_cols and ({"cwd", "repository"} & session_cols):
            chosen = [c for c in STORE_SESSION_COLUMNS if c in session_cols]
            sql = "SELECT " + ", ".join(f'"{c}"' for c in chosen) + ' FROM "sessions"'
            for values in conn.execute(sql):
                mapping = dict(zip(chosen, values, strict=True))
                sid = _match(mapping.get("id"), _ID_RE)
                if sid is not None:
                    out.sessions[sid] = (run.hashed(mapping.get("repository")),
                                         run.hashed(mapping.get("cwd")))
        return out
    finally:
        conn.close()


def read_store(path: Path, run: _Run, *, after_id: int = 0) -> StoreRead:
    """Read ``assistant_usage_events`` rows with ``id > after_id`` (read-only URI).

    Column probe: missing required columns or table → ``dq.copilot_store_schema`` and no rows;
    missing optional columns → the same note, rows still read. A locked or busy database is retried
    once, then skipped (``dq.copilot_store_unavailable``); any other SQLite error skips it too
    (strict mode raises :class:`SourceError`)."""
    path = Path(path)
    if not path.is_file():
        return StoreRead(status="absent")
    for attempt in (0, 1):
        try:
            return _read_store_once(path, run, after_id)
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if attempt == 0 and ("locked" in text or "busy" in text):
                run.stats["store_retries"] += 1
                continue
            break
        except sqlite3.Error:
            break
    if not run.opts.lenient:
        raise SourceError(f"{path.name}: unreadable")
    run.dq["dq.copilot_store_unavailable"] += 1
    return StoreRead(status="unavailable")


# ---------------------------------------------------------------------------------------------
# emission: requests and aggregates of one session
# ---------------------------------------------------------------------------------------------

def _message_request(run: _Run, st: SessionState, m: MsgRecord, kind: InferenceKind,
                     *, row: StoreRow | None = None) -> Request:
    model_raw = row.model if row is not None else m.model
    norm = normalize_copilot_model(model_raw).model
    ctx = run.pricing(model_raw, m.routing, m.tier, m.ttl.get(norm))
    raw_json = None
    convention = None
    cost = None
    billable, rule = True, None
    duration = None
    if row is not None:
        usage = row.usage
        if not row.has_output:
            usage = dataclasses.replace(usage, output=m.out)
        else:
            raw_json = row.raw_json
        convention = CONVENTION_SESSION_STORE
        billable, rule = billing_rule(norm, nano_aiu=row.nano_aiu)
        if row.nano_aiu is not None:
            cost = nano_aiu_to_nano(row.nano_aiu)[0]
            run.saw_credits = True
        duration = row.duration_ms
        run.saw_store_requests = True
    else:
        usage = UsageBuckets(output=m.out)
    inf = Inference(
        inference_id=stable_id("inf", m.rid, 0), kind=kind, usage=usage, pricing=ctx,
        usage_source=UsageSource.FINAL, billable=billable, billing_rule_id=rule,
        provider_reported_cost_nano=cost,
        provider_reported_cost_basis="provider_estimate" if cost is not None else None)
    attempt = Attempt(
        attempt_id=stable_id("at", m.rid, 0), attempt_no=0, ts_start_ms=m.ts, ttft_ms=None,
        duration_ms=duration, outcome=Outcome.OK, http_status=None, error_type=None,
        retry_layer=None, retry_after_ms=None, should_retry=None, provider_request_id=m.preq,
        provider_message_id=m.api, model_served=model_raw or None, stop_reason=None,
        inferences=(inf,), raw_usage_json=raw_json, convention_id=convention)
    appended = []
    for item in m.app:
        try:
            appended.append(AppendedItem(kind=item[0], name=item[1], n_bytes=item[2],
                                         is_error=bool(item[3])))
        except (ContractViolation, IndexError, TypeError):
            continue
    if appended:
        run.saw_appended = True
    return Request(
        request_id=m.rid, session_key=st.session_key, lane_key=m.lane, seq=m.seq,
        attribution=run.attribution(st, m.kind),
        params=RequestParams(model_requested=m.model or model_raw, effort=m.effort,
                             session_effort=m.effort),
        attempts=(attempt,), appended=tuple(appended), source=run.source_ref(m.locator))


def _row_request(run: _Run, st: SessionState, parser: SessionParser | None,
                 row: StoreRow) -> Request:
    """A request from a store row without an ``assistant.message`` partner."""
    lane_kind = LaneKind.SUBAGENT if row.initiator == "sub-agent" else LaneKind.MAIN
    kind = InferenceKind.MESSAGE
    if parser is not None:
        main, _ = parser.lane_for(None)
        if row.initiator == "compaction":
            lane = parser.compaction_lane(None)
            lane_kind, kind = LaneKind.COMPACTION, InferenceKind.COMPACTION
        elif lane_kind is LaneKind.SUBAGENT:
            lane = parser._lane(copilot_lane_key(st.session_key, "subagent", None),
                                LaneKind.SUBAGENT, main)
        else:
            lane = main
    else:
        main = copilot_lane_key(st.session_key, LaneKind.MAIN.value, None)
        run.shell(main, st.session_key, LaneKind.MAIN, None)
        lane = main
        if row.initiator == "compaction":
            lane = copilot_lane_key(st.session_key, LaneKind.COMPACTION.value, None)
            run.shell(lane, st.session_key, LaneKind.COMPACTION, main)
            lane_kind, kind = LaneKind.COMPACTION, InferenceKind.COMPACTION
        elif lane_kind is LaneKind.SUBAGENT:
            lane = copilot_lane_key(st.session_key, LaneKind.SUBAGENT.value, None)
            run.shell(lane, st.session_key, LaneKind.SUBAGENT, main)
    rid = stable_id("rq", "copilot-store", row.session_id, row.row_id, row.created_ms)
    norm = normalize_copilot_model(row.model).model
    ctx = run.pricing(row.model, st.routing, st.tier, st.ttl.get(norm))
    billable, rule = billing_rule(norm, nano_aiu=row.nano_aiu)
    cost = nano_aiu_to_nano(row.nano_aiu)[0] if row.nano_aiu is not None else None
    if cost is not None:
        run.saw_credits = True
    run.saw_store_requests = True
    inf = Inference(
        inference_id=stable_id("inf", rid, 0), kind=kind, usage=row.usage, pricing=ctx,
        usage_source=UsageSource.FINAL, billable=billable, billing_rule_id=rule,
        provider_reported_cost_nano=cost,
        provider_reported_cost_basis="provider_estimate" if cost is not None else None)
    attempt = Attempt(
        attempt_id=stable_id("at", rid, 0), attempt_no=0, ts_start_ms=row.created_ms,
        ttft_ms=None, duration_ms=row.duration_ms, outcome=Outcome.OK, http_status=None,
        error_type=None, retry_layer=None, retry_after_ms=None, should_retry=None,
        provider_request_id=None, provider_message_id=None, model_served=row.model,
        stop_reason=None, inferences=(inf,),
        raw_usage_json=row.raw_json if row.has_output else None,
        convention_id=CONVENTION_SESSION_STORE)
    run.touch(st.session_key, row.created_ms)
    return Request(
        request_id=rid, session_key=st.session_key, lane_key=lane, seq=row.row_id,
        attribution=run.attribution(st, lane_kind.value),
        params=RequestParams(model_requested=row.model, effort=st.effort,
                             session_effort=st.effort),
        attempts=(attempt,), source=run.source_ref(f"store:row:{row.row_id}"))


def _join(messages: list[MsgRecord], rows: list[StoreRow]
          ) -> tuple[list[tuple[MsgRecord, StoreRow]], list[MsgRecord], list[StoreRow]]:
    """Pair rows with messages by (turn index, model, created_at ± 2 s); closest in time wins,
    rows in id order. Returns (pairs, unjoined messages, unjoined rows)."""
    order = sorted(messages, key=lambda m: (m.ts, m.seq, m.rid))
    times = [m.ts for m in order]
    used: set[int] = set()
    pairs: list[tuple[MsgRecord, StoreRow]] = []
    lonely_rows: list[StoreRow] = []
    for row in sorted(rows, key=lambda r: r.row_id):
        norm = normalize_copilot_model(row.model).model
        lo = bisect.bisect_left(times, row.created_ms - JOIN_WINDOW_MS)
        hi = bisect.bisect_right(times, row.created_ms + JOIN_WINDOW_MS)
        best: tuple[int, int] | None = None
        for i in range(lo, hi):
            if i in used:
                continue
            m = order[i]
            if normalize_copilot_model(m.model).model != norm:
                continue
            if m.turn is not None and row.turn is not None and m.turn != row.turn:
                continue
            cand = (abs(m.ts - row.created_ms), i)
            if best is None or cand < best:
                best = cand
        if best is None:
            lonely_rows.append(row)
        else:
            used.add(best[1])
            pairs.append((order[best[1]], row))
    lonely = [m for i, m in enumerate(order) if i not in used]
    return pairs, lonely, lonely_rows


def _rollup_aggregates(run: _Run, st: SessionState, legs: Iterable[LegRollup]) -> None:
    dims_base = [("channel", CHANNEL), ("convention", CONVENTION_SHUTDOWN_ROLLUP),
                 ("source", run.source_id)]
    if run.base_attr.team:
        dims_base.append(("team", run.base_attr.team))
    for leg in legs:
        for model, counts, nano in leg.items:
            try:
                usage, notes = normalize_shutdown_rollup(counts)
            except BadUsageError:  # pragma: no cover - counts were validated at parse time
                continue
            for code in notes:
                run.dq[code] += 1
            norm = normalize_copilot_model(model).model or model
            cost = nano_aiu_to_nano(nano)[0] if nano is not None else None
            if cost is not None:
                run.saw_credits = True
            run.aggregates.append(UsageAggregate(
                agg_id=natural_id("agg", "copilot.cli_rollup", st.session_key, leg.leg, model),
                source_kind="copilot.cli_rollup", bucket_start_ms=min(leg.start_ms, leg.end_ms),
                bucket_end_ms=leg.end_ms, dims=tuple(sorted([*dims_base, ("model", norm)])),
                usage=usage, reported_cost_nano=cost,
                reported_cost_basis="provider_estimate" if cost is not None else None,
                finality="final", fetched_ms=max(0, run.opts.now_ms)))


def emit_session(run: _Run, parser: SessionParser, *, rows: list[StoreRow] | None,
                 store_mode: bool, covered: bool, settle_all: bool) -> list[StoreRow]:
    """Turn one parsed session into records on *run*; returns the store rows it consumed.

    *covered* (a conversation CP-VSCODE already covers): lane events only, without COST_STATE and
    without ``credit_limit_nano`` — no request, inference or aggregate
    (``dq.copilot_session_covered_by_vscode``). *store_mode*: messages are joined with *rows*
    (settled rows only unless *settle_all*); messages of open legs stay pending in the state.
    Otherwise messages are output-only requests and closed legs give rollup aggregates."""
    st = parser.state
    run.session_attr.setdefault(st.session_key, run.attribution(st, None))
    rows = list(rows or [])
    if covered:
        run.dq["dq.copilot_session_covered_by_vscode"] += 1
        for ev in parser.events:
            if ev.kind is LaneEventKind.COST_STATE:
                continue
            if ev.kind is LaneEventKind.SESSION_META:
                attrs = tuple((k, v) for k, v in ev.attrs if k != "credit_limit_nano")
                ev = dataclasses.replace(ev, attrs=attrs)
            run.events.append(ev)
        parser.messages.clear()
        st.pending.clear()
        return rows
    run.events.extend(parser.events)
    consumed: list[StoreRow] = []
    comp_rows: list[StoreRow] = []
    if store_mode:
        if rows:
            st.has_rows = True
        messages = [MsgRecord.from_json(p) for p in st.pending] + parser.messages
        st.pending = []
        settled_msgs: list[MsgRecord] = []
        held_msgs: list[MsgRecord] = []
        for m in messages:
            done = settle_all or m.leg < st.leg or (m.leg == st.leg and st.leg_shut)
            (settled_msgs if done else held_msgs).append(m)
        settled_rows = [r for r in rows if settle_all or r.created_ms <= st.closed_until]
        comp_rows = [r for r in settled_rows if r.initiator == "compaction"]
        chat_rows = [r for r in settled_rows if r.initiator != "compaction"]
        pairs, lonely_msgs, lonely_rows = _join(settled_msgs, chat_rows)
        for m, row in pairs:
            run.requests.append(_message_request(run, st, m, InferenceKind.MESSAGE, row=row))
        for m in lonely_msgs:
            run.requests.append(_message_request(run, st, m, InferenceKind.OUTPUT_RESIDUAL))
        for row in lonely_rows:
            run.requests.append(_row_request(run, st, parser, row))
        consumed = [row for _, row in pairs] + lonely_rows
        st.pending = [m.to_json() for m in held_msgs][-_PENDING_MAX:]
        run.stats["store_rows_joined"] += len(pairs)
        run.stats["store_rows_unjoined"] += len(lonely_rows)
        run.stats["output_residuals"] += len(lonely_msgs)
    else:
        for m in parser.messages:
            run.requests.append(_message_request(run, st, m, InferenceKind.MESSAGE))
    comp_seen: list[tuple[int, str]] = []
    for ts, model, req in parser.compactions:
        run.requests.append(req)
        comp_seen.append((ts, model))
    for row in comp_rows:
        norm = normalize_copilot_model(row.model).model
        if any(model == norm and abs(ts - row.created_ms) <= JOIN_WINDOW_MS
               for ts, model in comp_seen):
            run.stats["store_compaction_rows_covered"] += 1
        else:
            run.requests.append(_row_request(run, st, parser, row))
        consumed.append(row)
    if not (store_mode and st.has_rows):
        _rollup_aggregates(run, st, parser.legs)
    elif parser.legs:
        run.stats["rollups_replaced_by_store"] += len(parser.legs)
    parser.messages.clear()
    return consumed


def emit_store_only(run: _Run, sid: str, rows: list[StoreRow],
                    session_meta: tuple[str | None, str | None] | None) -> None:
    """Requests of store rows whose session has no ``events.jsonl``."""
    st = SessionState(dir_id=sid, raw_sid=sid, has_rows=True)
    if session_meta is not None:
        st.repo, st.cwd = session_meta
    run.session_attr.setdefault(st.session_key, run.attribution(st, None))
    for row in sorted(rows, key=lambda r: r.row_id):
        run.requests.append(_row_request(run, st, None, row))


# ---------------------------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------------------------

def _sniff_obj(obj: Mapping[str, Any]) -> bool:
    typ = obj.get("type")
    return (isinstance(typ, str) and (typ in KNOWN_EVENT_TYPES or bool(_TYPE_NAME_RE.match(typ)))
            and "." in typ and isinstance(obj.get("data", {}), Mapping)
            and ("id" in obj or "timestamp" in obj))


def _locator_prefix(opts: IngestOptions, rel: str) -> str:
    return pseudonym(opts.name_key or b"\0", "f", rel)[:14] + ":"


@dataclass
class CopilotCliAdapter:
    """GitHub Copilot CLI events importer (registry name ``copilot-cli``; addendum §5.9)."""

    name: str = ADAPTER_NAME
    capabilities: frozenset[str] = field(default=CAPABILITIES)

    def sniff(self, path: Path, head: bytes) -> bool:
        """A ``*.jsonl`` file whose first complete lines are Copilot session events
        (``{"type": "<namespace>.<event>", "data": {…}, "id", "timestamp"}``)."""
        name = Path(path).name
        if never_opened(Path(path)) or not name.endswith(".jsonl"):
            return False
        if not isinstance(head, (bytes, bytearray)):
            return False
        for raw in bytes(head).split(b"\n")[:16]:
            raw = raw.strip()
            if not raw:
                continue
            objs, _ = parse_event_line(raw)
            if objs:
                return all(_sniff_obj(o) for o in objs)
        return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Import one ``events.jsonl``, one session directory, ``session-state/`` or a whole
        Copilot home (with ``session-store.db`` only under ``--experimental copilot-store``)."""
        check_options(opts)
        path = Path(path)
        if never_opened(path):
            raise UsageError(f"{path.name} is never read by {ADAPTER_NAME} (addendum §5.9)")
        if path.is_file():
            if path.name == STORE_FILE:
                if STORE_FLAG not in opts.experimental:
                    raise UsageError(f"{STORE_FILE} is experimental: pass --experimental "
                                     f"{STORE_FLAG}")
                return self._read_home(path.parent, opts, events=False)
            run = _Run(opts, source_id_for(opts, path))
            parser = SessionParser(run, path)
            digest, n = _sha256_file(path)
            parser.parse(final=True, flag_unclean_eof=True, size=n)
            emit_session(run, parser, rows=None, store_mode=False, covered=False,
                         settle_all=True)
            return run.result(run.source_info(path, digest, n))
        if not path.is_dir():
            raise SourceError(f"{path.name}: not a file or directory")
        if (path / EVENTS_FILE).is_file() and not (path / SESSION_STATE_DIR).is_dir():
            return self.read(path / EVENTS_FILE, opts)
        if path.name == SESSION_STATE_DIR:
            return self._read_home(path.parent, opts, root=path)
        return self._read_home(path, opts)

    def _read_home(self, home: Path, opts: IngestOptions, *, root: Path | None = None,
                   events: bool = True) -> IngestResult:
        base = root or home
        run = _Run(opts, source_id_for(opts, base))
        files = iter_events_files(home) if events else []
        h = hashlib.sha256()
        total = 0
        parsers: list[SessionParser] = []
        for f in files:
            rel = f.relative_to(home).as_posix()
            digest, n = _sha256_file(f)
            h.update(digest.encode())
            total += n
            parser = SessionParser(run, f, locator_prefix=_locator_prefix(opts, rel))
            parser.parse(final=True, flag_unclean_eof=True, size=n)
            parsers.append(parser)
        store_mode = False
        store = StoreRead(status="absent")
        store_path = home / STORE_FILE
        if STORE_FLAG in opts.experimental and root is None:
            store = read_store(store_path, run)
            store_mode = store.status == "ok"
            if store.status != "absent" and store_path.is_file():
                digest, n = _sha256_file(store_path)
                h.update(digest.encode())
                total += n
        rows_by_sid: dict[str, list[StoreRow]] = {}
        for row in store.rows:
            rows_by_sid.setdefault(row.session_id, []).append(row)
        for parser in parsers:
            st = parser.state
            rows: list[StoreRow] = []
            for sid in dict.fromkeys(x for x in (st.raw_sid, st.dir_id) if x):
                rows.extend(rows_by_sid.pop(sid, []))
            if store_mode and st.repo is None and st.cwd is None:
                meta = store.sessions.get(st.raw_sid or st.dir_id)
                if meta is not None:
                    st.repo, st.cwd = meta
            emit_session(run, parser, rows=rows, store_mode=store_mode, covered=False,
                         settle_all=True)
        for sid, rows in sorted(rows_by_sid.items()):
            emit_store_only(run, sid, rows, store.sessions.get(sid))
        run.stats["sessions"] = len(parsers)
        run.stats["store_rows"] = len(store.rows)
        return run.result(run.source_info(base, h.hexdigest(), total))


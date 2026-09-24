"""OTLP/JSON file-exporter adapter (SPEC §5.9, D23; registry name ``otlp``).

Reads OpenTelemetry Collector ``file`` exporter output: JSON lines of ``ExportLogsServiceRequest``,
``ExportMetricsServiceRequest`` and ``ExportTraceServiceRequest`` (plain, ``.gz``, or ``.zst`` on
Python ≥ 3.14). OTLP/JSON encodes int64 values (``intValue``, ``*UnixNano``) as decimal strings;
numbers are accepted too. Attributes are ``[{key, value: {stringValue|intValue|doubleValue|
boolValue|arrayValue|kvlistValue}}]``.

What becomes what:

* ``claude_code.api_request`` log events → one :class:`Request` each (fidelity NO_TTL_SPLIT,
  convention ``claude_code.otel``: writes → ``cache_write_unknown`` with ``dq.no_ttl_split``;
  ``cost_usd`` → ``provider_reported_cost_nano`` on basis ``provider_estimate``, never billed);
  ``ts_start_ms = time − duration_ms``; lanes from ``session.id`` + ``query_source`` (the
  requesting subsystem: ``repl_main_thread`` / ``sdk`` → MAIN, ``agent:…`` → SUBAGENT with the
  built-in agent type in clear, ``compact`` → COMPACTION, any other subsystem → HELPER; the metric
  categories ``main`` / ``subagent`` / ``auxiliary`` are accepted too), refined by the beta
  ``claude_code.llm_request`` span of the same ``request_id`` (``agent_id`` → exact subagent
  lanes, ``ttft_ms``). Without spans, all subagent calls of a session share one lane with
  ``lane_exact=False`` (``dq.lanes_inferred``). An ``llm_request`` span with no matching event in
  the file becomes a request itself (trace-only exports).
* ``claude_code.api_error`` → an API_ERROR lane event, plus a failed attempt on the request it
  joins by ``request_id`` (else ``client_request_id``).
* ``claude_code.tool_result`` → an :class:`AppendedItem` on the next request of its lane (the lane
  of the ``claude_code.tool`` span with the same ``tool_use_id`` when present, else the session's
  main lane); ``claude_code.user_prompt`` → a HUMAN_PROMPT event on the main lane.
* ``claude_code.api_request_body`` / ``api_response_body`` (``OTEL_LOG_RAW_API_BODIES``) are
  counted in ``dq.raw_bodies_ignored`` and never decoded.
* Metrics ``claude_code.token.usage`` / ``claude_code.cost.usage`` →
  ``UsageAggregate(source_kind="otel.metric")`` (coverage only, never in the ledger): one row per
  UTC hour (of each point's end) × channel × model × team × speed; per-user series are summed
  into the team row so no row carries an identity or a single person's series.
* GenAI spans (``gen_ai.operation.name`` ∈ {chat, generate_content, text_completion}) →
  conventions ``otel.genai`` / ``otel.genai.legacy`` by the attribute names present; OpenInference
  spans with ``openinference.span.kind == "LLM"`` only (never AGENT/CHAIN roll-ups) → convention
  ``openinference``, lane = (trace, nearest AGENT ancestor); an LLM span without an agent ancestor
  is its own lane. A model-call span that wraps another model-call span of the same trace (a
  framework span around an instrumented SDK call) is skipped: only the innermost span is a
  request (``stats["nested_model_spans"]``).

Identity (``central-ingest``): ``user.email`` / ``user.account_uuid`` / ``user.account_id`` /
``user.id`` map to a team through ``opts.team_map``, the first one present becomes a ``p_``
pseudonym (of the value as reported) under ``opts.principal_key``, and every raw value is
dropped. Resource and record attributes ``team.id``, ``cost_center``, ``department``,
``tokenbill.arm``, ``tokenbill.wave``, ``app.entrypoint``, ``app.version`` and ``vcs.*`` (→ ``h_``
repo) feed the attribution. A Bedrock / Vertex model id reported without a channel (no billing
path, no provider table entry) is priced on the channel its id names, never on the Claude API.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tokenbill.adapters.conventions_ext import (
    BILLING_PATH_BY_CHANNEL,
    CHANNEL_BY_BILLING_PATH,
    CLAUDE_CODE_OTEL,
    CLAUDE_CODE_OTEL_KEYS,
    GENAI_KEYS,
    GENAI_LEGACY_KEYS,
    OPENINFERENCE,
    OPENINFERENCE_KEYS,
    OTEL_GENAI,
    OTEL_GENAI_LEGACY,
    TTL_HINT_BY_BILLING_PATH,
    Draft,
    LaneShell,
    SourceScan,
    assemble,
    attempt_id,
    cache_scope,
    canonical_usage_json,
    clean_attr,
    clean_label,
    error_type_for,
    guarded,
    head_record,
    key_part,
    lane_capabilities,
    member,
    model_channel,
    name_or_hash,
    normalize_claude_code_otel,
    normalize_identity,
    normalize_openinference,
    normalize_otel_genai,
    normalize_otel_genai_legacy,
    parse_iso_ms,
    source_ref,
    team_for,
    to_int,
    usd_to_nano,
)
from tokenbill.core.conventions import BadUsageError
from tokenbill.core.ids import pseudonym, request_id_for, stable_id
from tokenbill.core.models import normalize_model
from tokenbill.core.records import (
    AppendedItem,
    Attempt,
    Attribution,
    Fidelity,
    Inference,
    InferenceKind,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Outcome,
    PricingContext,
    RequestParams,
    UsageAggregate,
    UsageBuckets,
)
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["BUILTIN_TOOLS", "OtlpJsonAdapter"]

#: Claude Code built-in tool names emitted in clear (§5.1); anything else is ``h_`` unless
#: allowlisted.
BUILTIN_TOOLS = frozenset({
    "Agent", "AskUserQuestion", "Bash", "BashOutput", "Edit", "ExitPlanMode", "Glob", "Grep",
    "KillShell", "LS", "MultiEdit", "NotebookEdit", "NotebookRead", "Read", "Skill",
    "SlashCommand", "Task", "TodoRead", "TodoWrite", "ToolSearch", "WebFetch", "WebSearch",
    "Write",
})

_PRIORITY = 20  # SourceRef.priority of otlp (§3.2)
_OTLP_KEYS = ("resourceLogs", "resourceMetrics", "resourceSpans")
_OTLP_HEAD_RE = re.compile(rb'\A\s*\{\s*"resource(?:Logs|Metrics|Spans)"\s*:')
_HEX_RE = re.compile(r"[0-9a-fA-F]{1,64}\Z")
_NANOS_RE = re.compile(r"\s*\d{1,19}\s*\Z")
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_MAX_DEPTH = 8
_HOUR_MS = 3_600_000  # otel.metric aggregates are UTC-hour team rows
_MAX_ATTRS = 2048

_API_REQUEST = "claude_code.api_request"
_API_ERROR = "claude_code.api_error"
_TOOL_RESULT = "claude_code.tool_result"
_USER_PROMPT = "claude_code.user_prompt"
_RAW_BODY_EVENTS = frozenset({"claude_code.api_request_body", "claude_code.api_response_body"})
_LLM_SPAN = "claude_code.llm_request"
_TOOL_SPAN = "claude_code.tool"
_TOKEN_METRIC = "claude_code.token.usage"
_COST_METRIC = "claude_code.cost.usage"
_GENAI_OPERATIONS = frozenset({"chat", "generate_content", "text_completion"})
_GENAI_AGENT_OPERATIONS = frozenset({"invoke_agent", "create_agent"})

#: ``query_source`` → (lane kind, ``Attribution.query_source``). The ``claude_code.api_request``
#: event (and the beta ``llm_request`` span) carry the requesting *subsystem* (``repl_main_thread``,
#: ``sdk``, ``compact``, ``agent:builtin:Explore`` / ``agent:<name>``, ``away_summary``,
#: ``prompt_suggestion``, ``web_search_tool`` …, sometimes with a ``:outputStyle:<style>`` suffix);
#: only the token/cost *metrics* use the ``main`` / ``subagent`` / ``auxiliary`` categories (the
#: monitoring reference as quoted in anthropics/claude-code#82274 and #92057, 2026). Both
#: vocabularies are accepted: exact names first, then prefixes; any other non-empty subsystem is
#: an auxiliary (helper) call; a missing value leaves the lane kind unknown.
_QUERY_SOURCE_EXACT: Mapping[str, tuple[LaneKind, str]] = {
    "main": (LaneKind.MAIN, "main"), "repl_main_thread": (LaneKind.MAIN, "main"),
    "sdk": (LaneKind.MAIN, "main"), "subagent": (LaneKind.SUBAGENT, "subagent"),
    "auxiliary": (LaneKind.HELPER, "auxiliary"), "compaction": (LaneKind.COMPACTION, "compaction"),
    "compact": (LaneKind.COMPACTION, "compaction"),
}
_QUERY_SOURCE_PREFIXES: tuple[tuple[str, tuple[LaneKind, str]], ...] = (
    ("repl_main_thread:", (LaneKind.MAIN, "main")), ("sdk:", (LaneKind.MAIN, "main")),
    ("agent:", (LaneKind.SUBAGENT, "subagent")), ("compact:", (LaneKind.COMPACTION, "compaction")),
)
_AUXILIARY = (LaneKind.HELPER, "auxiliary")
_BUILTIN_AGENT = "agent:builtin:"
_STYLE_SUFFIX = ":outputStyle:"
#: metric ``type`` attribute → bucket of claude_code.token.usage.
_METRIC_BUCKET = {"input": "uncached_input", "output": "output", "cacheRead": "cache_read",
                  "cacheCreation": "cache_write_unknown"}
#: GenAI ``gen_ai.provider.name`` (legacy ``gen_ai.system``) → (provider, channel); a None
#: provider is derived from the model id.
_GENAI_PROVIDERS: Mapping[str, tuple[str | None, str]] = {
    "anthropic": ("anthropic", "anthropic_api"), "openai": ("openai", "openai_api"),
    "azure.ai.openai": ("openai", "azure_openai"), "az.ai.openai": ("openai", "azure_openai"),
    "aws.bedrock": (None, "bedrock"), "gcp.vertex_ai": (None, "vertex"),
    "gcp.gen_ai": (None, "vertex"), "vertex_ai": (None, "vertex"),
}
#: OpenInference ``llm.provider`` / ``llm.system`` → (provider, channel).
_OI_PROVIDERS: Mapping[str, tuple[str | None, str]] = {
    "anthropic": ("anthropic", "anthropic_api"), "openai": ("openai", "openai_api"),
    "azure": ("openai", "azure_openai"), "aws": (None, "bedrock"), "google": (None, "vertex"),
    "vertexai": (None, "vertex"),
}
#: OpenAI served tiers (``openai.response.service_tier``) → PricingContext.service_tier.
_OPENAI_TIERS = {"default": "standard", "standard": "standard", "flex": "flex",
                 "priority": "priority", "fast": "fast", "batch": "batch"}
#: user attributes, in pseudonymization precedence order (raw values never leave the adapter).
_USER_KEYS = ("user.email", "user.account_uuid", "user.account_id", "user.id", "enduser.id")


# =============================================================================================
# OTLP/JSON decoding
# =============================================================================================

def _nanos(value: object) -> int | None:
    """An OTLP ``fixed64`` nanosecond timestamp (decimal string or int); None for 0 / invalid."""
    if isinstance(value, str) and _NANOS_RE.match(value):
        n = int(value)
    elif type(value) is int:
        n = value
    else:
        return None
    return n if 0 < n < 2**63 else None


def _decode(value: object, depth: int = 0) -> Any:
    """One OTLP ``AnyValue`` → str | int | float | bool | list | dict | None."""
    if not isinstance(value, dict) or depth > _MAX_DEPTH:
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
        if isinstance(x, (int, float)) and type(x) is not bool:
            try:  # a JSON integer literal beyond the float range is one bad value, not a bad line
                f = float(x)
            except OverflowError:
                return None
            return f if math.isfinite(f) else None
        return None
    if "boolValue" in value:
        x = value["boolValue"]
        return x if type(x) is bool else None
    if "arrayValue" in value:
        arr = value["arrayValue"]
        items = arr.get("values") if isinstance(arr, dict) else None
        return [_decode(v, depth + 1) for v in items[:_MAX_ATTRS]] if isinstance(items, list) \
            else []
    if "kvlistValue" in value:
        kv = value["kvlistValue"]
        items = kv.get("values") if isinstance(kv, dict) else None
        return _attrs(items, depth + 1)
    return None


def _attrs(items: object, depth: int = 0) -> dict[str, Any]:
    """An OTLP attribute list → dict (last key wins)."""
    out: dict[str, Any] = {}
    if not isinstance(items, list) or depth > _MAX_DEPTH:
        return out
    for kv in items[:_MAX_ATTRS]:
        if isinstance(kv, dict) and isinstance(kv.get("key"), str):
            out[kv["key"]] = _decode(kv.get("value"), depth)
    return out


def _list(obj: object, key: str) -> list[Any]:
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, list) else []


def _resource_attrs(block: object) -> dict[str, Any]:
    res = block.get("resource") if isinstance(block, dict) else None
    return _attrs(res.get("attributes")) if isinstance(res, dict) else {}


def _event_name(rec: dict[str, Any]) -> str | None:
    """The event name of a log record without decoding its other attributes (raw-body events
    must never be decoded): ``eventName``, a ``claude_code.*`` body, or ``event.name``."""
    name = rec.get("eventName")
    if isinstance(name, str) and name:
        return name
    body = rec.get("body")
    if isinstance(body, dict) and isinstance(body.get("stringValue"), str):
        text = body["stringValue"]
        if text.startswith("claude_code.") and len(text) <= 64:
            return text
    for kv in _list(rec, "attributes"):
        if isinstance(kv, dict) and kv.get("key") == "event.name":
            value = _decode(kv.get("value"))
            if isinstance(value, str) and value:
                return value if value.startswith("claude_code.") else f"claude_code.{value}"
    return None


def _hex(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and _HEX_RE.match(value) else None


def _truthy(value: object) -> bool | None:
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def _usage_raw(attrs: Mapping[str, Any], keys: Iterable[str]) -> dict[str, int]:
    """The flat usage mapping a span/event convention consumes; unparsable counts raise."""
    raw: dict[str, int] = {}
    for key in keys:
        if key in attrs and attrs[key] is not None:
            n = to_int(attrs[key])
            if n is None:
                raise BadUsageError(f"bad_usage: {key}")
            raw[key] = n
    return raw


# =============================================================================================
# collected records
# =============================================================================================

@dataclass
class _Rec:
    """One decoded log record or span."""

    locator: str
    order: tuple[int, int]
    ts_ms: int
    attrs: dict[str, Any]
    start_ms: int | None = None      # spans only
    end_ms: int | None = None
    trace: str | None = None
    span: str | None = None
    parent: str | None = None
    error: bool = False


@dataclass
class _CcCall:
    """A Claude Code API call being assembled (event, optional span, failed attempts)."""

    rec: _Rec
    span: _Rec | None
    request_id: str
    session_id: str
    lane_key: str
    kind: LaneKind
    ts_start: int
    duration: int | None
    failures: list[tuple[int, _Rec]] = field(default_factory=list)
    draft: Draft | None = None


class _Reader:
    """One ``read()`` of an OTLP/JSON file."""

    def __init__(self, path: Path, opts: IngestOptions) -> None:
        self.opts = opts
        self.scan = SourceScan("otlp", path, opts)
        self.cc_requests: list[_Rec] = []
        self.cc_errors: list[_Rec] = []
        self.tool_results: list[_Rec] = []
        self.prompts: list[_Rec] = []
        self.llm_spans: dict[str, _Rec] = {}
        self.orphan_llm_spans: list[_Rec] = []
        self.tool_spans: dict[str, _Rec] = {}
        self.genai_spans: list[_Rec] = []
        self.oi_spans: list[_Rec] = []
        self.span_index: dict[tuple[str, str], tuple[str | None, bool]] = {}
        self.points: list[tuple[str, int, int, int, dict[str, Any], object]] = []
        self.shells: dict[str, LaneShell] = {}
        self.drafts: list[Draft] = []
        self.events: list[LaneEvent] = []

    # ---------- pass 1: decode ----------
    def run(self) -> IngestResult:
        """Read the whole file and build the :class:`IngestResult`."""
        for line_no, obj in self.scan.records():
            self.scan.count("records")
            if not any(k in obj for k in _OTLP_KEYS):
                self.scan.quarantine(f"line:{line_no}", "missing:resource")
                continue
            guarded(self.scan, f"line:{line_no}", lambda o=obj, n=line_no: self._line(o, n))
        self._assemble_claude_code()
        nested = self._nested_model_spans()
        self._assemble_spans(self.genai_spans, self._genai_draft, nested)
        self._assemble_spans(self.oi_spans, self._oi_draft, nested)
        aggregates = self._aggregates()
        requests, sessions = assemble(self.drafts, self.shells, self.opts, self.scan)
        events = sorted(self.events, key=lambda e: (
            e.lane_key, e.ts_ms, e.kind.value, tuple((k, repr(v)) for k, v in e.attrs)))
        caps = lane_capabilities(requests, self.shells)
        if events:
            caps.add("events")
        if any(e.kind is LaneEventKind.HUMAN_PROMPT for e in events):
            caps.add("human_prompts")
        if aggregates:
            caps.add("aggregates")
        return self.scan.finish(requests=requests, sessions=sessions, events=events,
                                aggregates=aggregates, capabilities=caps)

    def _line(self, obj: dict[str, Any], line_no: int) -> None:
        k = 0
        for rl in _list(obj, "resourceLogs"):
            res = _resource_attrs(rl)
            for sl in _list(rl, "scopeLogs"):
                for rec in _list(sl, "logRecords"):
                    k += 1
                    if isinstance(rec, dict):
                        self._log(rec, res, line_no, k)
        for rs in _list(obj, "resourceSpans"):
            res = _resource_attrs(rs)
            for ss in _list(rs, "scopeSpans"):
                for span in _list(ss, "spans"):
                    k += 1
                    if isinstance(span, dict):
                        self._span(span, res, line_no, k)
        for rm in _list(obj, "resourceMetrics"):
            res = _resource_attrs(rm)
            for sm in _list(rm, "scopeMetrics"):
                for metric in _list(sm, "metrics"):
                    if isinstance(metric, dict):
                        self._metric(metric, res, line_no)

    def _log(self, rec: dict[str, Any], res: dict[str, Any], line_no: int, k: int) -> None:
        name = _event_name(rec)
        if name in _RAW_BODY_EVENTS:
            self.scan.note("dq.raw_bodies_ignored")
            return
        handlers: dict[str, list[_Rec]] = {
            _API_REQUEST: self.cc_requests, _API_ERROR: self.cc_errors,
            _TOOL_RESULT: self.tool_results, _USER_PROMPT: self.prompts,
        }
        target = handlers.get(name or "")
        if target is None:
            self.scan.count("ignored_log_records")
            return
        attrs = {**res, **_attrs(rec.get("attributes"))}
        if "api_request_body" in attrs or "api_response_body" in attrs or "body_ref" in attrs:
            self.scan.note("dq.raw_bodies_ignored")
            return
        ns = _nanos(rec.get("timeUnixNano")) or _nanos(rec.get("observedTimeUnixNano"))
        ts = ns // 1_000_000 if ns else parse_iso_ms(attrs.get("event.timestamp"))
        locator = f"line:{line_no}#{k}"
        if ts is None:
            self.scan.quarantine(locator, "missing:timeUnixNano")
            return
        target.append(_Rec(locator=locator, order=(line_no, k), ts_ms=ts, attrs=attrs))

    def _span(self, span: dict[str, Any], res: dict[str, Any], line_no: int, k: int) -> None:
        name = span.get("name") if isinstance(span.get("name"), str) else ""
        attrs = {**res, **_attrs(span.get("attributes"))}
        trace, span_id = _hex(span.get("traceId")), _hex(span.get("spanId"))
        parent = _hex(span.get("parentSpanId"))
        oi_kind = attrs.get("openinference.span.kind")
        operation = attrs.get("gen_ai.operation.name")
        is_agent = oi_kind == "AGENT" or member(operation, _GENAI_AGENT_OPERATIONS)
        if trace and span_id:
            self.span_index[(trace, span_id)] = (parent, is_agent)
        start_ns = _nanos(span.get("startTimeUnixNano"))
        end_ns = _nanos(span.get("endTimeUnixNano"))
        start = start_ns // 1_000_000 if start_ns else None
        end = end_ns // 1_000_000 if end_ns else start
        status = span.get("status")
        error = (isinstance(status, dict) and status.get("code") in (2, "2", "STATUS_CODE_ERROR")) \
            or attrs.get("error.type") is not None
        locator = f"line:{line_no}#{k}"
        rec = _Rec(locator=locator, order=(line_no, k), ts_ms=end or 0, attrs=attrs,
                   start_ms=start, end_ms=end, trace=trace, span=span_id, parent=parent,
                   error=bool(error))
        if name == _LLM_SPAN:
            rid = clean_label(attrs.get("request_id"))
            if rid and rid not in self.llm_spans:
                self.llm_spans[rid] = rec
            else:
                self.orphan_llm_spans.append(rec)
        elif name == _TOOL_SPAN:
            tool_use = clean_label(attrs.get("tool_use_id"))
            if tool_use:
                self.tool_spans.setdefault(tool_use, rec)
        elif oi_kind is not None:
            if oi_kind == "LLM":
                self.oi_spans.append(rec)
            else:
                self.scan.count("openinference_non_llm_spans")
        elif member(operation, _GENAI_OPERATIONS):
            self.genai_spans.append(rec)
        else:
            self.scan.count("ignored_spans")

    def _metric(self, metric: dict[str, Any], res: dict[str, Any], line_no: int) -> None:
        name = metric.get("name")
        if name not in (_TOKEN_METRIC, _COST_METRIC):
            self.scan.count("ignored_metrics")
            return
        data = metric.get("sum") if isinstance(metric.get("sum"), dict) else metric.get("gauge")
        if not isinstance(data, dict):
            self.scan.quarantine(f"line:{line_no}", "missing:sum")
            return
        cumulative = data.get("aggregationTemporality") in (2, "2")
        for dp in _list(data, "dataPoints"):
            if not isinstance(dp, dict):
                continue
            attrs = {**res, **_attrs(dp.get("attributes"))}
            end_ns = _nanos(dp.get("timeUnixNano"))
            if end_ns is None:
                self.scan.quarantine(f"line:{line_no}", "missing:timeUnixNano")
                continue
            start_ns = _nanos(dp.get("startTimeUnixNano")) or end_ns
            value: object = dp.get("asInt") if "asInt" in dp else dp.get("asDouble")
            self.points.append((name, start_ns // 1_000_000, end_ns // 1_000_000,
                                1 if cumulative else 0, attrs, value))

    # ---------- attribution ----------
    def _attribution(self, attrs: Mapping[str, Any], *, product: str | None,
                     query_source: str | None = None) -> Attribution:
        opts = self.opts
        candidates, raw_principal = _identities(attrs)
        team = team_for(opts, candidates) or clean_attr(attrs.get("team.id")) \
            or opts.attribution.team
        principal = self.scan.principal(raw_principal)
        updates: dict[str, Any] = {"team": team}
        if principal is not None:
            updates["principal"] = principal
        for attr_key, field_name in (("cost_center", "cost_center"), ("tokenbill.arm", "arm"),
                                     ("tokenbill.wave", "wave"), ("app.entrypoint", "entrypoint"),
                                     ("app.version", "client_version")):
            value = clean_attr(attrs.get(attr_key))
            if value is not None:
                updates[field_name] = value
        repo = attrs.get("vcs.repository.url.full") or attrs.get("vcs.repository.name")
        if isinstance(repo, str) and repo.strip():
            updates["repo"] = pseudonym(opts.name_key, "h", repo.strip())
        for attr_key, field_name in (("skill.name", "skill"), ("mcp_server.name", "mcp_server"),
                                     ("plugin.name", "plugin"), ("agent.name", "agent_type")):
            value = name_or_hash(opts, attrs.get(attr_key))
            if value is not None:
                updates[field_name] = value
        department = clean_attr(attrs.get("department"))
        if department is not None:
            extra = dict(opts.attribution.extra)
            extra["department"] = department
            updates["extra"] = tuple(sorted(extra.items()))
        if product is not None and opts.attribution.agent_product is None:
            updates["agent_product"] = product
        if query_source is not None:
            updates["query_source"] = query_source
        return dataclasses.replace(opts.attribution, **updates)

    def _shell(self, lane_key: str, session_key: str, kind: LaneKind, parent: str | None,
               scope: str, exact: bool, source_kind: str) -> None:
        if lane_key not in self.shells:
            self.shells[lane_key] = LaneShell(lane_key=lane_key, session_key=session_key,
                                              kind=kind, parent_lane_key=parent,
                                              cache_scope_key=scope, lane_exact=exact,
                                              source_kind=source_kind)

    # ---------- Claude Code ----------
    def _cc_channel(self, model_raw: str | None = None) -> tuple[str, str]:
        """(channel, billing path): the channel named by ``opts.attribution.billing_path``;
        with an unknown billing path, a Bedrock/Vertex model id's channel hint; else the Claude
        API. The billing path itself only ever comes from the options (§5.9)."""
        billing_path = self.opts.attribution.billing_path or "unknown"
        channel = CHANNEL_BY_BILLING_PATH.get(billing_path, "anthropic_api")
        if billing_path == "unknown" and model_raw:
            channel = model_channel(channel, normalize_model(model_raw))
        return channel, billing_path

    def _cc_session(self, attrs: Mapping[str, Any]) -> str:
        return key_part(attrs.get("session.id")) or f"nosession:{self.scan.source_id}"

    def _cc_lane(self, session_id: str, kind: LaneKind, agent_id: str | None,
                 parent_agent: str | None, channel: str | None = None) -> str:
        """Lane key (and shell) of a Claude Code call; subagent/helper lanes shared by a session
        are inexact."""
        session_key = stable_id("ses", "otlp", session_id)
        channel = channel or self._cc_channel()[0]
        scope = cache_scope(channel, self.opts.attribution.workspace_id)
        main = stable_id("ln", "otlp", session_id, "main")
        if kind is LaneKind.MAIN:
            self._shell(main, session_key, LaneKind.MAIN, None, scope, True, CLAUDE_CODE_OTEL)
            return main
        if agent_id is not None:
            lane = stable_id("ln", "otlp", session_id, "agent", agent_id)
            parent = stable_id("ln", "otlp", session_id, "agent", parent_agent) \
                if parent_agent else main
            self._shell(lane, session_key, LaneKind.SUBAGENT, parent, scope, True,
                        CLAUDE_CODE_OTEL)
            return lane
        lane = stable_id("ln", "otlp", session_id, kind.value)
        self._shell(lane, session_key, kind, main, scope, False, CLAUDE_CODE_OTEL)
        return lane

    def _assemble_claude_code(self) -> None:
        calls: list[_CcCall] = []
        by_rid: dict[str, _CcCall] = {}
        by_crid: dict[str, _CcCall] = {}
        seen: set[str] = set()
        for rec in sorted(self.cc_requests, key=lambda r: r.order):
            rid = clean_label(rec.attrs.get("request_id"))
            if rid is not None and rid in seen:
                self.scan.count("duplicate_records")
                continue
            if rid is not None:
                seen.add(rid)
            span = self.llm_spans.pop(rid, None) if rid else None

            def one(rec: _Rec = rec, span: _Rec | None = span, rid: str | None = rid) -> None:
                call = self._cc_call(rec, span, rid, from_span=False)
                calls.append(call)
                if rid:
                    by_rid[rid] = call
                crid = clean_label(rec.attrs.get("client_request_id"))
                if crid:
                    by_crid.setdefault(crid, call)
            guarded(self.scan, rec.locator, one)
        orphans = sorted([*self.llm_spans.values(), *self.orphan_llm_spans],
                         key=lambda r: r.order)
        for span in orphans:
            rid = clean_label(span.attrs.get("request_id"))
            if rid is not None and rid in seen:
                self.scan.count("duplicate_records")
                continue
            if rid is not None:
                seen.add(rid)

            def one_span(span: _Rec = span, rid: str | None = rid) -> None:
                if span.start_ms is None:  # never date a request 1970-01-01
                    self.scan.quarantine(span.locator, "missing:startTimeUnixNano")
                    return
                call = self._cc_call(span, span, rid, from_span=True)
                calls.append(call)
                if rid:
                    by_rid[rid] = call
            guarded(self.scan, span.locator, one_span)
        for err in sorted(self.cc_errors, key=lambda r: r.order):
            guarded(self.scan, err.locator, lambda e=err: self._cc_error(e, by_rid, by_crid))
        for call in calls:
            self._finish_call(call)
        lanes: dict[str, list[Draft]] = {}
        for call in calls:
            if call.draft is not None:
                self.drafts.append(call.draft)
                lanes.setdefault(call.lane_key, []).append(call.draft)
        for lane_drafts in lanes.values():
            lane_drafts.sort(key=lambda d: (d.ts_ms, d.order))
        for rec in sorted(self.tool_results, key=lambda r: r.order):
            guarded(self.scan, rec.locator, lambda r=rec: self._tool_result(r, lanes))
        for rec in sorted(self.prompts, key=lambda r: r.order):
            session_id = self._cc_session(rec.attrs)
            lane = self._cc_lane(session_id, LaneKind.MAIN, None, None)
            if self.scan.in_window(rec.ts_ms):
                self.events.append(LaneEvent(lane_key=lane, ts_ms=rec.ts_ms,
                                             kind=LaneEventKind.HUMAN_PROMPT))

    def _cc_call(self, rec: _Rec, span: _Rec | None, rid: str | None, *,
                 from_span: bool) -> _CcCall:
        attrs = rec.attrs
        sattrs = span.attrs if span is not None else {}
        kind = _query_source(_query_source_value(attrs, sattrs))[0]
        agent_id = key_part(sattrs.get("agent_id"))
        if agent_id is not None:
            kind = LaneKind.SUBAGENT
        session_id = self._cc_session(attrs)
        model_raw = clean_label(attrs.get("model")) or clean_label(sattrs.get("model"))
        lane_key = self._cc_lane(session_id, kind, agent_id,
                                 key_part(sattrs.get("parent_agent_id")),
                                 self._cc_channel(model_raw)[0])
        duration = to_int(attrs.get("duration_ms"))
        if from_span and rec.start_ms is not None:
            ts_start = rec.start_ms
            if duration is None and rec.end_ms is not None:
                duration = rec.end_ms - rec.start_ms
        else:
            ts_start = max(0, rec.ts_ms - (duration or 0))
        request_id = request_id_for("anthropic", None, self.scan.source_id,
                                    f"req:{rid}" if rid else rec.locator)
        return _CcCall(rec=rec, span=span, request_id=request_id, session_id=session_id,
                       lane_key=lane_key, kind=kind, ts_start=ts_start, duration=duration)

    def _cc_error(self, rec: _Rec, by_rid: dict[str, _CcCall],
                  by_crid: dict[str, _CcCall]) -> None:
        attrs = rec.attrs
        rid = clean_label(attrs.get("request_id"))
        crid = clean_label(attrs.get("client_request_id"))
        call = (by_rid.get(rid) if rid else None) or (by_crid.get(crid) if crid else None)
        status = to_int(attrs.get("status_code"))
        status = status if status is not None and 100 <= status <= 599 else None
        event_attrs: dict[str, Any] = {"status": status, "error_type": error_type_for(status)}
        retry = to_int(attrs.get("attempt"))
        if retry is not None:
            event_attrs["retry_attempt"] = retry
        if call is not None:
            call.failures.append((rec.ts_ms, rec))
            lane = call.lane_key
        else:
            kind = _query_source(_query_source_value(attrs, {}))[0]
            lane = self._cc_lane(self._cc_session(attrs), kind, None, None,
                                 self._cc_channel(clean_label(attrs.get("model")))[0])
            self.scan.count("api_errors_unjoined")
        if self.scan.in_window(rec.ts_ms):
            self.events.append(LaneEvent(lane_key=lane, ts_ms=rec.ts_ms,
                                         kind=LaneEventKind.API_ERROR,
                                         attrs=tuple(event_attrs.items())))

    def _finish_call(self, call: _CcCall) -> None:
        def build() -> None:
            call.draft = self._cc_draft(call)
        guarded(self.scan, call.rec.locator, build)

    def _cc_draft(self, call: _CcCall) -> Draft | None:
        rec, opts = call.rec, self.opts
        attrs = rec.attrs
        sattrs = call.span.attrs if call.span is not None else {}
        merged = {**sattrs, **attrs} if call.span is not rec else dict(attrs)
        raw = _usage_raw(merged, CLAUDE_CODE_OTEL_KEYS)
        if not raw:
            self.scan.quarantine(rec.locator, "missing:input_tokens")
            return None
        if not self.scan.in_window(call.ts_start):
            self.scan.count("outside_window")
            return None
        buckets, codes = normalize_claude_code_otel(raw)
        self.scan.notes(codes, tokens=buckets.cache_write_unknown or None)
        model_raw = clean_label(merged.get("model")) or ""
        channel, billing_path = self._cc_channel(model_raw)
        mid = normalize_model(model_raw, channel if channel in ("bedrock", "vertex") else None)
        if not mid.model:
            self.scan.note("dq.unpriced_model")
        scope = mid.endpoint_scope
        if scope == "unknown":
            scope = dict(opts.attribution.extra).get("endpoint_scope", "unknown")
        hints = TTL_HINT_BY_BILLING_PATH.get(billing_path)
        hint = (hints[0] if call.kind is LaneKind.MAIN else hints[1]) if hints else None
        fast = merged.get("speed") == "fast"
        ctx = PricingContext(provider="anthropic", channel=channel, model=mid.model,
                             model_raw=model_raw, speed="fast" if fast else "standard",
                             endpoint_scope=scope if scope in ("global", "regional") else "unknown",
                             write_ttl_hint=hint, billing_path=billing_path)
        cost = usd_to_nano(merged.get("cost_usd"))
        attempts: list[Attempt] = []
        for n, (_ts, err) in enumerate(sorted(call.failures, key=lambda f: (f[0], f[1].order))):
            status = to_int(err.attrs.get("status_code"))
            status = status if status is not None and 100 <= status <= 599 else None
            dur = to_int(err.attrs.get("duration_ms"))
            attempts.append(Attempt(
                attempt_id=attempt_id(call.request_id, n), attempt_no=n,
                ts_start_ms=max(0, err.ts_ms - (dur or 0)), ttft_ms=None, duration_ms=dur,
                outcome=Outcome.HTTP_ERROR if status is not None else Outcome.NETWORK_ERROR,
                http_status=status, error_type=error_type_for(status), retry_layer=None,
                retry_after_ms=None, should_retry=None,
                provider_request_id=clean_label(err.attrs.get("request_id")),
                provider_message_id=None, model_served=None, stop_reason=None, inferences=()))
        n = len(attempts)
        att_id = attempt_id(call.request_id, n)
        total_attempts = to_int(merged.get("attempt"))
        effort = merged.get("effort") if member(merged.get("effort"), _EFFORTS) else None
        attempts.append(Attempt(
            attempt_id=att_id, attempt_no=n, ts_start_ms=call.ts_start,
            ttft_ms=to_int(sattrs.get("ttft_ms")), duration_ms=call.duration,
            outcome=Outcome.OK, http_status=None, error_type=None, retry_layer=None,
            retry_after_ms=None, should_retry=None,
            provider_request_id=clean_label(merged.get("request_id")), provider_message_id=None,
            model_served=model_raw or None,
            stop_reason=clean_label(merged.get("stop_reason"), enum=True),
            inferences=(Inference(
                inference_id=stable_id("inf", att_id, 0), kind=InferenceKind.MESSAGE,
                usage=buckets, pricing=ctx, provider_reported_cost_nano=cost,
                provider_reported_cost_basis="provider_estimate" if cost is not None else None),),
            sdk_retry_count=total_attempts - 1 if total_attempts else None,
            raw_usage_json=canonical_usage_json(raw), convention_id=CLAUDE_CODE_OTEL))
        shell = self.shells[call.lane_key]
        if not shell.lane_exact:
            self.scan.note("dq.lanes_inferred")
        query_source = {LaneKind.MAIN: "main", LaneKind.SUBAGENT: "subagent",
                        LaneKind.HELPER: "auxiliary", LaneKind.COMPACTION: "compaction"}.get(
                            call.kind)
        attribution = self._attribution(merged, product="claude_code", query_source=query_source)
        agent_type = _agent_type(opts, _query_source_value(attrs, sattrs))
        if agent_type is not None and attribution.agent_type is None:
            attribution = dataclasses.replace(attribution, agent_type=agent_type)
        params = RequestParams(model_requested=model_raw, effort=effort,
                               speed="fast" if fast else None)
        end = call.ts_start + (call.duration or 0)
        # the request starts with its first attempt (Request.ts_start_ms): seq follows that order
        return Draft(request_id=call.request_id, session_key=shell.session_key,
                     lane_key=call.lane_key, ts_ms=attempts[0].ts_start_ms, order=call.rec.order,
                     attribution=attribution, params=params, attempts=attempts,
                     source=source_ref(self.scan, f"line:{call.rec.order[0]}",
                                       Fidelity.NO_TTL_SPLIT, _PRIORITY),
                     end_ms=end)

    def _tool_result(self, rec: _Rec, lanes: dict[str, list[Draft]]) -> None:
        attrs = rec.attrs
        tool_use = clean_label(attrs.get("tool_use_id"))
        span = self.tool_spans.get(tool_use) if tool_use else None
        agent_id = key_part(span.attrs.get("agent_id")) if span is not None else None
        session_id = self._cc_session(attrs)
        kind = LaneKind.SUBAGENT if agent_id else LaneKind.MAIN
        parent = key_part(span.attrs.get("parent_agent_id")) if span is not None else None
        lane = self._cc_lane(session_id, kind, agent_id, parent)
        if span is not None:
            tokens = to_int(span.attrs.get("result_tokens"))
            if tokens is not None:
                self.scan.count("tool_result_tokens", tokens)
        target = next((d for d in lanes.get(lane, []) if d.ts_ms >= rec.ts_ms), None)
        if target is None:
            self.scan.count("tool_results_unattached")
            return
        success = _truthy(attrs.get("success"))
        target.appended.append(AppendedItem(
            kind="tool_result", name=name_or_hash(self.opts, attrs.get("tool_name"), BUILTIN_TOOLS),
            n_bytes=to_int(attrs.get("tool_result_size_bytes")) or 0,
            is_error=success is False))

    # ---------- GenAI and OpenInference spans ----------
    def _agent_ancestor(self, rec: _Rec) -> tuple[str, str] | None:
        """The nearest agent-kind ancestor span (trace, span id) present in this file."""
        if rec.trace is None:
            return None
        parent, hops = rec.parent, 0
        while parent is not None and hops < 256:
            entry = self.span_index.get((rec.trace, parent))
            if entry is None:
                return None
            if entry[1]:
                return rec.trace, parent
            parent, hops = entry[0], hops + 1
        return None

    def _span_lane(self, rec: _Rec, session_id: str | None, conversation: str | None,
                   channel: str, source_kind: str) -> tuple[str, str]:
        """(session key, lane key) of a GenAI/OpenInference span; creates the shell."""
        trace = rec.trace or f"notrace:{rec.locator}"
        session_key = stable_id("ses", "otlp", session_id or conversation or f"trace:{trace}")
        scope = cache_scope(channel, self.opts.attribution.workspace_id)
        if conversation is not None and source_kind != OPENINFERENCE:
            lane = stable_id("ln", "otlp", "conv", conversation)
            self._shell(lane, session_key, LaneKind.API_RUN, None, scope, True, source_kind)
        else:
            agent = self._agent_ancestor(rec)
            if agent is None:
                lane = stable_id("ln", "otlp", "span", trace, rec.span or rec.locator)
                self._shell(lane, session_key, LaneKind.API_RUN, None, scope, False,
                            source_kind)
                self.scan.note("dq.lanes_inferred")
            else:
                lane = stable_id("ln", "otlp", "agent", *agent)
                outer = self._agent_ancestor(_Rec(locator="", order=(0, 0), ts_ms=0, attrs={},
                                                  trace=agent[0], span=agent[1],
                                                  parent=self.span_index[agent][0]))
                parent_lane = stable_id("ln", "otlp", "agent", *outer) if outer else None
                self._shell(lane, session_key,
                            LaneKind.SUBAGENT if outer else LaneKind.API_RUN,
                            parent_lane, scope, True, source_kind)
        # every request of a lane carries the lane's session (the first span that opened it),
        # even when a later span of the same lane reports another session.id
        return self.shells[lane].session_key, lane

    def _nested_model_spans(self) -> set[tuple[str, str]]:
        """Model-call spans (GenAI chat / OpenInference LLM) with a model-call descendant in the
        same trace. A framework's LLM span wrapping an instrumented SDK call reports the same
        tokens twice; only the innermost span is a request (never double count)."""
        spans = [r for r in (*self.genai_spans, *self.oi_spans) if r.trace and r.span]
        model = {(r.trace, r.span) for r in spans}
        nested: set[tuple[str, str]] = set()
        for rec in spans:
            parent, hops = rec.parent, 0
            while parent is not None and hops < 256:
                key = (rec.trace or "", parent)
                if key in model:
                    nested.add(key)
                entry = self.span_index.get(key)
                if entry is None:
                    break
                parent, hops = entry[0], hops + 1
        return nested

    def _assemble_spans(self, recs: list[_Rec], build: Callable[[_Rec], Draft | None],
                        nested: set[tuple[str, str]]) -> None:
        for rec in sorted(recs, key=lambda r: r.order):
            if rec.trace and rec.span and (rec.trace, rec.span) in nested:
                self.scan.count("nested_model_spans")
                continue

            def one(rec: _Rec = rec) -> None:
                draft = build(rec)
                if draft is not None:
                    self.drafts.append(draft)
            guarded(self.scan, rec.locator, one)

    def _provider_channel(self, table: Mapping[str, tuple[str | None, str]],
                          names: Iterable[object], model_raw: str) -> tuple[str, str]:
        for name in names:
            if isinstance(name, str) and name.lower() in table:
                provider, channel = table[name.lower()]
                if provider is None:
                    provider = "anthropic" if "claude" in model_raw or "anthropic" in model_raw \
                        else "openai" if model_raw.startswith(("gpt", "o1", "o3", "o4")) \
                        else "unknown"
                return provider, channel
        label = next((clean_label(n, enum=True) for n in names if clean_label(n, enum=True)),
                     None)
        return (label or "unknown").lower(), "unknown"

    def _span_draft(self, rec: _Rec, *, convention: str, keys: tuple[str, ...],
                    normalize: Callable[[Mapping[str, object]], tuple[UsageBuckets, list[str]]],
                    provider: str, channel: str, model_raw: str, requested: str,
                    session_id: str | None, conversation: str | None, response_id: str | None,
                    tier: str, max_tokens: int | None) -> Draft | None:
        opts = self.opts
        if rec.start_ms is None:  # never date a request 1970-01-01
            self.scan.quarantine(rec.locator, "missing:startTimeUnixNano")
            return None
        ts = rec.start_ms
        raw = _usage_raw(rec.attrs, keys)
        if not raw and not rec.error:
            self.scan.quarantine(rec.locator, "missing:usage")
            return None
        if not self.scan.in_window(ts):
            self.scan.count("outside_window")
            return None
        mid = normalize_model(model_raw, channel if channel in ("bedrock", "vertex") else None)
        hinted = model_channel(channel, mid)
        if hinted != channel:  # a Bedrock / Vertex id reported under a generic provider
            channel = hinted
            if provider == "unknown" and "claude" in model_raw:
                provider = "anthropic"
        session_key, lane = self._span_lane(rec, session_id, conversation, channel, convention)
        msg_id = response_id if provider == "anthropic" and response_id \
            and response_id.startswith("msg_") else None
        request_id = request_id_for(provider, msg_id, self.scan.source_id,
                                    f"span:{rec.trace}/{rec.span}" if rec.span else rec.locator)
        att_id = attempt_id(request_id, 0)
        duration = rec.end_ms - ts if rec.end_ms is not None and rec.end_ms >= ts else None
        if not mid.model:
            self.scan.note("dq.unpriced_model")
        scope = mid.endpoint_scope
        if scope == "unknown":
            scope = dict(opts.attribution.extra).get("endpoint_scope", "unknown")
        billing_path = opts.attribution.billing_path or BILLING_PATH_BY_CHANNEL.get(channel,
                                                                                   "unknown")
        inferences: tuple[Inference, ...] = ()
        outcome, status, error_type = Outcome.OK, None, None
        if raw:
            buckets, codes = normalize(raw)
            self.scan.notes(codes)
            ctx = PricingContext(provider=provider, channel=channel, model=mid.model,
                                 model_raw=model_raw, service_tier=tier,
                                 endpoint_scope=scope if scope in ("global", "regional")
                                 else "unknown", billing_path=billing_path)
            inferences = (Inference(inference_id=stable_id("inf", att_id, 0),
                                    kind=InferenceKind.MESSAGE, usage=buckets, pricing=ctx),)
        else:
            err = rec.attrs.get("error.type")
            status = to_int(err) if isinstance(err, (str, int)) else None
            status = status if status is not None and 100 <= status <= 599 else None
            outcome = Outcome.HTTP_ERROR if status is not None else Outcome.UNKNOWN
            error_type = error_type_for(status) if status is not None \
                else clean_label(err, enum=True) or "other"
        attempt = Attempt(
            attempt_id=att_id, attempt_no=0, ts_start_ms=ts, ttft_ms=None, duration_ms=duration,
            outcome=outcome, http_status=status, error_type=error_type, retry_layer=None,
            retry_after_ms=None, should_retry=None,
            provider_request_id=None if msg_id else response_id, provider_message_id=msg_id,
            model_served=model_raw or None, stop_reason=None, inferences=inferences,
            raw_usage_json=canonical_usage_json(raw) if raw else None,
            convention_id=convention if raw else None)
        attribution = self._attribution(rec.attrs, product=None)
        if attribution.billing_path is None and billing_path != "unknown":
            attribution = dataclasses.replace(attribution, billing_path=billing_path)
        return Draft(request_id=request_id, session_key=session_key, lane_key=lane, ts_ms=ts,
                     order=rec.order, attribution=attribution,
                     params=RequestParams(model_requested=requested, max_tokens=max_tokens),
                     attempts=[attempt],
                     source=source_ref(self.scan, f"line:{rec.order[0]}", Fidelity.NO_TTL_SPLIT,
                                       _PRIORITY),
                     end_ms=ts + (duration or 0))

    def _genai_draft(self, rec: _Rec) -> Draft | None:
        attrs = rec.attrs
        model_raw = clean_label(attrs.get("gen_ai.response.model")) \
            or clean_label(attrs.get("gen_ai.request.model")) or ""
        provider, channel = self._provider_channel(
            _GENAI_PROVIDERS, (attrs.get("gen_ai.provider.name"), attrs.get("gen_ai.system")),
            model_raw)
        legacy = "gen_ai.usage.cache_write.input_tokens" not in attrs and any(
            k in attrs for k in ("gen_ai.usage.cache_creation.input_tokens",
                                 "gen_ai.usage.prompt_tokens", "gen_ai.usage.completion_tokens"))
        tier_raw = attrs.get("openai.response.service_tier") \
            or attrs.get("gen_ai.openai.response.service_tier")
        tier = _OPENAI_TIERS.get(tier_raw, "unknown") if isinstance(tier_raw, str) else "standard"
        return self._span_draft(
            rec, convention=OTEL_GENAI_LEGACY if legacy else OTEL_GENAI,
            keys=GENAI_LEGACY_KEYS if legacy else GENAI_KEYS,
            normalize=normalize_otel_genai_legacy if legacy else normalize_otel_genai,
            provider=provider, channel=channel, model_raw=model_raw,
            requested=clean_label(attrs.get("gen_ai.request.model")) or model_raw,
            session_id=None, conversation=key_part(attrs.get("gen_ai.conversation.id")),
            response_id=clean_label(attrs.get("gen_ai.response.id")), tier=tier,
            max_tokens=to_int(attrs.get("gen_ai.request.max_tokens")))

    def _oi_draft(self, rec: _Rec) -> Draft | None:
        attrs = rec.attrs
        model_raw = clean_label(attrs.get("llm.model_name")) or ""
        provider, channel = self._provider_channel(
            _OI_PROVIDERS, (attrs.get("llm.provider"), attrs.get("llm.system")), model_raw)
        if provider == "unknown" and isinstance(attrs.get("llm.system"), str):
            provider = clean_label(attrs["llm.system"], enum=True) or "unknown"
        return self._span_draft(
            rec, convention=OPENINFERENCE, keys=OPENINFERENCE_KEYS,
            normalize=normalize_openinference, provider=provider, channel=channel,
            model_raw=model_raw, requested=model_raw,
            session_id=key_part(attrs.get("session.id")), conversation=None, response_id=None,
            tier="standard", max_tokens=None)

    # ---------- metrics ----------
    def _aggregates(self) -> list[UsageAggregate]:
        opts = self.opts
        latest: dict[tuple[Any, ...], tuple[int, int, int, dict[str, Any], object, str]] = {}
        deltas: list[tuple[int, int, dict[str, Any], object, str]] = []
        for name, start, end, cumulative, attrs, value in self.points:
            if cumulative:
                series = (name, start, tuple(sorted((k, repr(v)) for k, v in attrs.items())))
                prev = latest.get(series)
                if prev is None or end >= prev[1]:
                    latest[series] = (start, end, cumulative, attrs, value, name)
            else:
                deltas.append((start, end, attrs, value, name))
        points = deltas + [(s, e, a, v, n) for s, e, _c, a, v, n in latest.values()]
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for _start, end, attrs, value, name in points:
            if not self.scan.in_window(end):
                continue
            # one bucket per UTC hour of the point's end: every user's series (each process has
            # its own export interval) is summed into the team row, so no row is one person's
            bucket = end - end % _HOUR_MS
            model_raw = clean_label(attrs.get("model")) or ""
            channel, _ = self._cc_channel(model_raw)
            model = normalize_model(model_raw).model or model_raw
            team = team_for(opts, _identities(attrs)[0]) or clean_attr(attrs.get("team.id")) \
                or opts.attribution.team
            dims = {"channel": channel, "model": model, "product": "claude_code"}
            if attrs.get("speed") == "fast":
                dims["speed"] = "fast"
            if team:
                dims["team"] = team
            key = (bucket, tuple(sorted(dims.items())))
            group = groups.setdefault(key, {"usage": {}, "cost": None})
            if name == _COST_METRIC:
                nano = usd_to_nano(value)
                if nano is None:
                    self.scan.count("bad_metric_points")
                    continue
                group["cost"] = (group["cost"] or 0) + nano
                continue
            bucket = _METRIC_BUCKET.get(attrs.get("type") if isinstance(attrs.get("type"), str)
                                        else "")
            tokens = to_int(value)
            if bucket is None or tokens is None:
                self.scan.count("bad_metric_points")
                continue
            group["usage"][bucket] = group["usage"].get(bucket, 0) + tokens
        out: list[UsageAggregate] = []
        for (start, dims), group in sorted(groups.items()):
            try:
                usage = UsageBuckets(**group["usage"])
            except Exception:  # noqa: BLE001 - a sum beyond 2**53 tokens is not a real metric
                self.scan.count("bad_metric_points")
                continue
            end = start + _HOUR_MS
            out.append(UsageAggregate(
                agg_id=stable_id("agg", self.scan.source_id, "otel.metric", start, end,
                                 *(f"{k}={v}" for k, v in dims)),
                source_kind="otel.metric", bucket_start_ms=start, bucket_end_ms=end,
                dims=dims, usage=usage, reported_cost_nano=group["cost"],
                reported_cost_basis="provider_estimate" if group["cost"] is not None else None,
                fetched_ms=opts.now_ms if opts.now_ms >= 0 else 0))
        return out


def _query_source_value(attrs: Mapping[str, Any], sattrs: Mapping[str, Any]) -> str | None:
    """The raw ``query_source`` of an event (else its span's, else the span's bounded
    ``query_source_safe``). Never stored: only its category leaves the adapter."""
    for value in (attrs.get("query_source"), sattrs.get("query_source"),
                  sattrs.get("query_source_safe")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _query_source(value: str | None) -> tuple[LaneKind, str | None]:
    """(lane kind, ``Attribution.query_source``) of a ``query_source`` value (table-driven)."""
    if not value:
        return LaneKind.UNKNOWN, None
    hit = _QUERY_SOURCE_EXACT.get(value)
    if hit is not None:
        return hit
    for prefix, category in _QUERY_SOURCE_PREFIXES:
        if value.startswith(prefix):
            return category
    return _AUXILIARY


def _agent_type(opts: IngestOptions, value: str | None) -> str | None:
    """The subagent type named by an ``agent:…`` query source: built-in agents
    (``agent:builtin:Explore``) in clear, any other agent name as its ``h_`` (unless
    allowlisted); None for non-agent sources. An output-style suffix is ignored."""
    if not value or not value.startswith("agent:"):
        return None
    value = value.split(_STYLE_SUFFIX, 1)[0]
    if value.startswith(_BUILTIN_AGENT):
        return clean_label(value[len(_BUILTIN_AGENT):], enum=True)
    return name_or_hash(opts, value[len("agent:"):])


def _identities(attrs: Mapping[str, Any]) -> tuple[list[str], str | None]:
    """(team-map lookup candidates, the raw identity to pseudonymize) from the user attributes.

    Candidates are every user value as reported (the team map is matched exactly, then
    case-insensitively); the principal is the first present value in :data:`_USER_KEYS` order,
    as reported (§5.1). Nothing here is stored.
    """
    candidates: list[str] = []
    principal: str | None = None
    for key in _USER_KEYS:
        normalized = normalize_identity(attrs.get(key))
        if normalized is None:
            continue
        candidates.append(normalized)
        if principal is None:
            principal = normalized
    return candidates, principal


class OtlpJsonAdapter:
    """``otlp``: OpenTelemetry Collector file-exporter JSON lines (SPEC §5.9)."""

    name = "otlp"
    capabilities = frozenset({"usage_sequence", "timing", "ttft", "attempts", "events",
                              "appended", "human_prompts", "lanes_exact", "params",
                              "attribution.team", "workload", "aggregates"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """True when the first line is an OTLP export request (``resourceLogs`` /
        ``resourceMetrics`` / ``resourceSpans``)."""
        obj, line = head_record(head)
        if obj is not None:
            return any(k in obj for k in _OTLP_KEYS)
        return bool(line) and _OTLP_HEAD_RE.match(line) is not None

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Read one OTLP/JSON file (see the module docstring)."""
        return _Reader(Path(path), opts).run()

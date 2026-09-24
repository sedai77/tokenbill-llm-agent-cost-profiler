"""Anthropic Messages responses and Message Batches results (SPEC §5.10, ``anthropic-responses``).

Reads JSONL of Messages API response objects (``type: "message"``), Message Batches result lines
(``{custom_id, result: {type, message}}``; only ``succeeded`` results are billed and they carry
``service_tier = "batch"``), and ``{request_meta, response}`` pairs written by gateways:
``request_meta = {ts_ms, lane, session, attribution, channel, endpoint_scope, model_raw,
billing_path, request_id, account}``. Vertex responses carry no model in the body: ``model_raw``
comes from ``request_meta`` (the endpoint URL's model id) and ``endpoint_scope`` from
``request_meta.endpoint_scope`` (the region is not in the id).

Usage goes through ``core.conventions.anthropic_inferences`` (convention ``anthropic.messages``:
iterations, refusal rule, TTL split). The request id is keyed by the message id
(``request_id_for("anthropic", msg_id, …)``), so the same response seen by the recorder or a
transcript merges in the store. ``diagnostics.cache_miss_reason`` becomes the attempt's
:class:`CacheDiagnostic` (source ``anthropic.cache_diagnostics``) and
``context_management.applied_edits`` its ``applied_edits``. Error responses (``type: "error"``)
become failed attempts. Anthropic response objects carry no timestamp, so a record without
``request_meta.ts_ms`` is quarantined (``missing:request_meta.ts_ms``). Content blocks are never
read.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tokenbill.adapters.conventions_ext import (
    BILLING_PATH_BY_CHANNEL,
    Draft,
    LaneShell,
    SourceScan,
    assemble,
    attempt_id,
    attribution_from,
    cache_scope,
    canonical_usage_json,
    clean_label,
    guarded,
    head_record,
    key_part,
    lane_capabilities,
    member,
    meta_ts,
    source_ref,
    to_int,
)
from tokenbill.core.conventions import ANTHROPIC_MESSAGES, anthropic_inferences
from tokenbill.core.ids import pseudonym, request_id_for, stable_id
from tokenbill.core.models import normalize_model
from tokenbill.core.records import (
    BILLING_PATHS,
    DIAG_REASONS,
    Attempt,
    CacheDiagnostic,
    Fidelity,
    LaneKind,
    Outcome,
    PricingContext,
    RequestParams,
)
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["AnthropicResponsesAdapter"]

_PRIORITY = 35  # SourceRef.priority of anthropic-responses (§3.2)
_CHANNELS = frozenset({"anthropic_api", "claude_platform_aws", "foundry", "bedrock", "vertex"})
_SCOPES = frozenset({"global", "regional", "multi_region"})
_BATCH_TYPES = frozenset({"succeeded", "errored", "canceled", "expired"})
#: ``usage.service_tier`` → PricingContext.service_tier.
SERVICE_TIERS: Mapping[str, str] = {"standard": "standard", "priority": "priority",
                                    "batch": "batch"}
#: Error ``error.type`` → (HTTP status, Attempt.error_type).
ERRORS: Mapping[str, tuple[int, str]] = {
    "invalid_request_error": (400, "invalid_request"), "authentication_error": (401, "auth"),
    "billing_error": (402, "spend_cap"), "permission_error": (403, "auth"),
    "not_found_error": (404, "invalid_request"), "request_too_large": (413, "prompt_too_long"),
    "rate_limit_error": (429, "rate_limit"), "api_error": (500, "other"),
    "timeout_error": (504, "timeout"), "overloaded_error": (529, "overloaded"),
}


def _unwrap(obj: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, Any, bool]:
    """(request_meta, message or error object, is a batch result); a non-succeeded batch
    result yields ``None`` as the message."""
    meta = obj.get("request_meta") if isinstance(obj.get("request_meta"), Mapping) else None
    resp: Any = obj.get("response") if meta is not None or (
        "response" in obj and obj.get("type") not in ("message", "error")) else obj
    batch = False
    if isinstance(resp, Mapping) and "custom_id" in resp and isinstance(resp.get("result"),
                                                                         Mapping):
        batch = True
        result = resp["result"]
        resp = result.get("message") if result.get("type") == "succeeded" else None
    return meta, resp, batch


def _is_anthropic(obj: Mapping[str, Any]) -> bool:
    meta = obj.get("request_meta") if isinstance(obj.get("request_meta"), Mapping) else None
    resp = obj.get("response") if meta is not None else obj
    if not isinstance(resp, Mapping):
        return False
    if "custom_id" in resp and isinstance(resp.get("result"), Mapping):
        return member(resp["result"].get("type"), _BATCH_TYPES)
    return resp.get("type") == "message" and isinstance(resp.get("usage"), Mapping)


def _diagnostic(resp: Mapping[str, Any]) -> CacheDiagnostic | None:
    diag = resp.get("diagnostics")
    miss = diag.get("cache_miss_reason") if isinstance(diag, Mapping) else None
    if not isinstance(miss, Mapping):
        return None
    label = clean_label(miss.get("type"), enum=True)
    if label is None:
        return None
    return CacheDiagnostic(reason=label if label in DIAG_REASONS else "unavailable",
                           provider_reason=label,
                           missed_input_tokens_estimate=to_int(
                               miss.get("cache_missed_input_tokens")),
                           source="anthropic.cache_diagnostics")


def _edits(resp: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    cm = resp.get("context_management")
    edits = cm.get("applied_edits") if isinstance(cm, Mapping) else None
    out = []
    for edit in edits if isinstance(edits, list) else []:
        if isinstance(edit, Mapping):
            etype = clean_label(edit.get("type"), enum=True)
            cleared = to_int(edit.get("cleared_input_tokens"))
            if etype is not None:
                out.append((etype, cleared or 0))
    return tuple(out)


class _Reader:
    def __init__(self, path: Path, opts: IngestOptions) -> None:
        self.opts = opts
        self.scan = SourceScan("anthropic-responses", path, opts)
        self.shells: dict[str, LaneShell] = {}
        self.drafts: dict[str, tuple[int, Draft]] = {}

    def run(self) -> IngestResult:
        """Read the whole file and build the :class:`IngestResult`."""
        for line_no, obj in self.scan.records():
            self.scan.count("records")
            guarded(self.scan, f"line:{line_no}", lambda o=obj, n=line_no: self._record(o, n))
        drafts = [d for _out, d in self.drafts.values()]
        requests, sessions = assemble(drafts, self.shells, self.opts)
        caps = lane_capabilities(requests, self.shells)
        if requests and not any(inf.usage.cache_write_unknown for r in requests
                                for inf in r.billable_inferences):
            caps.add("ttl_split")
        if any(len(a.inferences) > 1 for r in requests for a in r.attempts):
            caps.add("iterations")
        return self.scan.finish(requests=requests, sessions=sessions, events=[],
                                capabilities=caps)

    def _record(self, obj: Mapping[str, Any], line_no: int) -> None:
        locator = f"line:{line_no}"
        meta, resp, batch = _unwrap(obj)
        if resp is None and batch:
            self.scan.count("batch_results_not_billed")
            return
        if not isinstance(resp, Mapping) or resp.get("type") not in ("message", "error"):
            self.scan.quarantine(locator, "missing:type")
            return
        ts = meta_ts(meta)
        if ts is None:
            self.scan.quarantine(locator, "missing:request_meta.ts_ms")
            return
        if not self.scan.in_window(ts):
            self.scan.count("outside_window")
            return
        opts = self.opts
        channel = meta.get("channel") if meta and member(meta.get("channel"), _CHANNELS) \
            else "anthropic_api"
        billing = meta.get("billing_path") if meta else None
        billing_path = billing if billing in BILLING_PATHS else (
            opts.attribution.billing_path or BILLING_PATH_BY_CHANNEL.get(channel, "unknown"))
        model_raw = (clean_label(meta.get("model_raw")) if meta else None) \
            or clean_label(resp.get("model")) or ""
        mid = normalize_model(model_raw, channel if channel in ("bedrock", "vertex") else None)
        scope = meta.get("endpoint_scope") if meta else None
        if not member(scope, _SCOPES):
            scope = mid.endpoint_scope if mid.endpoint_scope in _SCOPES else dict(
                opts.attribution.extra).get("endpoint_scope", "unknown")
        if not member(scope, _SCOPES):
            scope = "unknown"
        attr_meta = meta.get("attribution") if meta and isinstance(meta.get("attribution"),
                                                                    Mapping) else None
        attribution = attribution_from(opts, self.scan, attr_meta)
        account_raw = meta.get("account") if meta else None
        account = pseudonym(opts.name_key, "h", account_raw.strip()) \
            if isinstance(account_raw, str) and account_raw.strip() else None
        scope_key = cache_scope(channel, attribution.workspace_id
                                if channel in ("anthropic_api", "claude_platform_aws", "foundry")
                                else account)
        msg_id = clean_label(resp.get("id")) if resp.get("type") == "message" else None
        request_id = request_id_for("anthropic", msg_id, self.scan.source_id, locator)
        att_id = attempt_id(request_id, 0)
        provider_rid = clean_label(meta.get("request_id")) if meta else None
        usage = resp.get("usage")
        output = 0
        if resp.get("type") == "error" or not isinstance(usage, Mapping):
            if resp.get("type") == "message":
                self.scan.quarantine(locator, "missing:usage")
                return
            err = resp.get("error")
            etype = err.get("type") if isinstance(err, Mapping) else None
            status, error_type = ERRORS.get(etype, (None, "other")) if isinstance(etype, str) \
                else (None, "other")
            status = to_int(meta.get("http_status")) if meta and meta.get("http_status") \
                else status
            attempt = Attempt(
                attempt_id=att_id, attempt_no=0, ts_start_ms=ts, ttft_ms=None,
                duration_ms=to_int(meta.get("duration_ms")) if meta else None,
                outcome=Outcome.HTTP_ERROR, http_status=status, error_type=error_type,
                retry_layer=None, retry_after_ms=None, should_retry=None,
                provider_request_id=provider_rid or clean_label(resp.get("request_id")),
                provider_message_id=None, model_served=None, stop_reason=None, inferences=())
        else:
            tier_raw = usage.get("service_tier")
            geo = usage.get("inference_geo")
            ctx = PricingContext(
                provider="anthropic", channel=channel, model=mid.model, model_raw=model_raw,
                service_tier="batch" if batch else SERVICE_TIERS.get(tier_raw, "unknown")
                if isinstance(tier_raw, str) else "standard",
                speed="fast" if usage.get("speed") == "fast" else "standard",
                inference_geo=geo if geo in ("us", "global") else None,
                endpoint_scope=scope, billing_path=billing_path)
            inferences, codes = anthropic_inferences(usage, message_model=model_raw, ctx=ctx,
                                                     id_prefix=att_id)
            self.scan.notes(codes)
            if not mid.model:
                self.scan.note("dq.unpriced_model")
            output = to_int(usage.get("output_tokens")) or 0
            attempt = Attempt(
                attempt_id=att_id, attempt_no=0, ts_start_ms=ts,
                ttft_ms=to_int(meta.get("ttft_ms")) if meta else None,
                duration_ms=to_int(meta.get("duration_ms")) if meta else None,
                outcome=Outcome.OK, http_status=None, error_type=None, retry_layer=None,
                retry_after_ms=None, should_retry=None, provider_request_id=provider_rid,
                provider_message_id=msg_id, model_served=clean_label(resp.get("model")),
                stop_reason=clean_label(resp.get("stop_reason"), enum=True),
                inferences=tuple(inferences), diagnostics=_diagnostic(resp),
                applied_edits=_edits(resp), raw_usage_json=canonical_usage_json(usage),
                convention_id=ANTHROPIC_MESSAGES)
        session = key_part(meta.get("session")) if meta else None
        lane = key_part(meta.get("lane")) if meta else None
        session_key, lane_key = self._lane(session, lane, request_id, scope_key)
        draft = Draft(request_id=request_id, session_key=session_key, lane_key=lane_key, ts_ms=ts,
                      order=(line_no,), attribution=attribution,
                      params=RequestParams(model_requested=model_raw), attempts=[attempt],
                      source=source_ref(self.scan, locator, Fidelity.FULL, _PRIORITY),
                      end_ms=ts + (attempt.duration_ms or 0))
        previous = self.drafts.get(request_id)
        if previous is not None:
            self.scan.count("duplicate_records")
            if previous[0] >= output:  # split-entry rule: keep the larger output
                return
        self.drafts[request_id] = (output, draft)

    def _lane(self, session: str | None, lane: str | None, request_id: str,
              scope: str) -> tuple[str, str]:
        exact = True
        if session or lane:
            session = session or lane
            session_key = stable_id("ses", "anthropic-responses", session or "")
            lane_key = stable_id("ln", "anthropic-responses", session or "", lane or "main")
        else:
            session_key = stable_id("ses", "anthropic-responses", "single", request_id)
            lane_key = stable_id("ln", "anthropic-responses", "single", request_id)
            exact = False
            self.scan.note("dq.lanes_inferred")
        if lane_key not in self.shells:
            self.shells[lane_key] = LaneShell(lane_key=lane_key, session_key=session_key,
                                              kind=LaneKind.API_RUN, parent_lane_key=None,
                                              cache_scope_key=scope, lane_exact=exact,
                                              source_kind="anthropic-responses")
        return session_key, lane_key


class AnthropicResponsesAdapter:
    """``anthropic-responses``: Messages API responses and batch results (SPEC §5.10)."""

    name = "anthropic-responses"
    capabilities = frozenset({"usage_sequence", "timing", "ttft", "ttl_split", "iterations",
                              "diagnostics", "attempts", "params", "lanes_exact",
                              "attribution.team", "workload"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """True for a Messages response, a batch result line, or a pair wrapping one."""
        obj, line = head_record(head)
        if obj is not None:
            return _is_anthropic(obj)
        prefix = line[:64].replace(b" ", b"")
        return prefix.startswith((b'{"id":"msg_', b'{"custom_id":'))

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Read one Anthropic responses / batch results JSONL file (see the module docstring)."""
        return _Reader(Path(path), opts).run()

"""OpenAI Responses / Chat Completions usage adapter (SPEC §5.10, D43; registry name ``openai``).

Reads JSONL of OpenAI **Responses** (``object: "response"``) or **Chat Completions**
(``object: "chat.completion"``) response objects, ``{request_meta, response}`` pairs written by
gateways, and Batch API output lines (``{custom_id, response: {status_code, request_id, body}}``,
served tier ``batch``). Conventions ``openai.responses`` / ``openai.chat`` (§5.2):
``input_tokens`` includes cached and cache-write tokens; writes are one class with a 30-minute
TTL (``cache_write_other``); a negative uncached residual fails the sum check
(``dq.sum_check_failed``) and quarantines the record.

* ``service_tier`` is the **served** tier from the response (``default`` → ``standard``);
  ``provider_request_id`` is the response ``id``.
* ``incomplete_details.reason == "max_output_tokens"`` (Chat: ``finish_reason == "length"``) →
  ``stop_reason = "max_tokens"``.
* ``prompt_cache_diagnostics`` (Responses, GPT-5.6+) → :class:`CacheDiagnostic` with source
  ``openai.prompt_cache_diagnostics``, the provider label verbatim and ``cache_missed_tokens`` as a
  magnitude that is never priced. Field paths verified 2026-09-23 against
  https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics: top-level
  ``prompt_cache_diagnostics.{type, reason, comparison_reusable_tokens, cache_missed_tokens}``,
  ``type`` ∈ {cache_hit, cache_miss, comparison_response_not_found, unavailable} and miss reasons
  spelled ``model_changed``, ``prompt_cache_key_changed``, ``tools_changed`` … — the SPEC's short
  labels (``model``, ``prompt_cache_key`` …) are accepted too.
* ``request_meta.channel == "azure_openai"`` → channel ``azure_openai``, cache scope
  ``sub:<h_ of request_meta.subscription_id>`` (caches are isolated per subscription, D43).

Lanes come from ``request_meta.{session, lane}``, else the Responses ``conversation`` id, else the
``previous_response_id`` chain inside the file; otherwise each response is its own (inexact) lane.
Response bodies (``output``, ``choices[].message``, instructions) are never read.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tokenbill.adapters.conventions_ext import (
    BILLING_PATH_BY_CHANNEL,
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    Draft,
    LaneShell,
    SourceScan,
    assemble,
    attempt_id,
    attribution_from,
    cache_scope,
    canonical_usage_json,
    clean_label,
    error_type_for,
    guarded,
    head_record,
    key_part,
    lane_capabilities,
    member,
    meta_ts,
    normalize_openai_chat,
    normalize_openai_responses,
    source_ref,
    to_int,
)
from tokenbill.core.ids import pseudonym, request_id_for, stable_id
from tokenbill.core.models import normalize_model
from tokenbill.core.records import (
    BILLING_PATHS,
    Attempt,
    CacheDiagnostic,
    Fidelity,
    Inference,
    InferenceKind,
    LaneKind,
    Outcome,
    PricingContext,
    RequestParams,
)
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["DIAGNOSTIC_REASONS", "OpenAIUsageAdapter", "canonical_diagnostic"]

_PRIORITY = 10
_OBJECTS = frozenset({"response", "chat.completion"})
_CHANNELS = frozenset({"openai_api", "azure_openai"})
_SCOPES = frozenset({"global", "regional", "multi_region"})

#: Served ``service_tier`` → PricingContext.service_tier (``priority`` was renamed ``fast`` on
#: 2026-07-30; both are kept verbatim for the pricer).
SERVICE_TIERS: Mapping[str, str] = {
    "default": "standard", "standard": "standard", "flex": "flex", "priority": "priority",
    "fast": "fast", "batch": "batch",
}
#: ``prompt_cache_diagnostics`` miss reason (with or without the ``_changed`` suffix) → canonical
#: :data:`~tokenbill.core.records.DIAG_REASONS` value (SPEC §5.10).
DIAGNOSTIC_REASONS: Mapping[str, str] = {
    "model": "model_changed", "tools": "tools_changed", "input": "messages_changed",
    "reasoning_effort": "param_changed", "text_format": "param_changed",
    "verbosity": "param_changed", "service_tier": "param_changed",
    "prompt_cache_key": "key_changed", "context_compacted": "compacted",
}
#: ``prompt_cache_diagnostics.type`` values that are themselves the outcome.
_DIAGNOSTIC_TYPES: Mapping[str, str] = {
    "comparison_response_not_found": "previous_message_not_found", "unavailable": "unavailable",
}
#: Provider stop labels that mean the output limit was hit.
_STOP_REASONS: Mapping[str, str] = {"max_output_tokens": "max_tokens", "length": "max_tokens"}
#: Responses ``error.code`` → Attempt.error_type.
_ERROR_CODES: Mapping[str, str] = {
    "rate_limit_exceeded": "rate_limit", "server_error": "other", "invalid_prompt":
    "invalid_request", "context_length_exceeded": "prompt_too_long", "timeout": "timeout",
}
_EFFORT_LABELS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})


def canonical_diagnostic(diag: object) -> CacheDiagnostic | None:
    """The :class:`CacheDiagnostic` of a ``prompt_cache_diagnostics`` object (None for a hit, an
    unknown ``type`` or a non-object). ``cache_missed_tokens`` is a magnitude only."""
    if not isinstance(diag, Mapping):
        return None
    dtype = diag.get("type")
    if not isinstance(dtype, str):
        return None
    missed = to_int(diag.get("cache_missed_tokens"))
    if dtype in _DIAGNOSTIC_TYPES:
        return CacheDiagnostic(reason=_DIAGNOSTIC_TYPES[dtype], provider_reason=dtype,
                               missed_input_tokens_estimate=missed,
                               source="openai.prompt_cache_diagnostics")
    if dtype != "cache_miss":
        return None
    label = clean_label(diag.get("reason"), enum=True)
    key = label[:-len("_changed")] if label and label.endswith("_changed") else label
    reason = DIAGNOSTIC_REASONS.get(key or "", "unavailable")
    return CacheDiagnostic(reason=reason, provider_reason=label or "cache_miss",
                           missed_input_tokens_estimate=missed,
                           source="openai.prompt_cache_diagnostics")


def _unwrap(obj: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, Any, bool,
                                            str | None, int | None]:
    """(request_meta, response object, is Batch API output, batch request id, batch HTTP
    status) of one JSONL record."""
    meta = obj.get("request_meta") if isinstance(obj.get("request_meta"), Mapping) else None
    resp: Any = obj
    if meta is not None or (not member(obj.get("object"), _OBJECTS)
                            and isinstance(obj.get("response"), Mapping)):
        resp = obj.get("response")
    if isinstance(resp, Mapping) and "body" in resp and "status_code" in resp:
        body = resp.get("body")
        status = to_int(resp.get("status_code"))
        return (meta, body if isinstance(body, Mapping) else None, True,
                clean_label(resp.get("request_id")), status)
    return meta, resp, False, None, None


def _is_openai(obj: Mapping[str, Any]) -> bool:
    resp = _unwrap(obj)[1]
    return isinstance(resp, Mapping) and member(resp.get("object"), _OBJECTS)


class _Reader:
    def __init__(self, path: Path, opts: IngestOptions) -> None:
        self.opts = opts
        self.scan = SourceScan("openai", path, opts)
        self.shells: dict[str, LaneShell] = {}
        self.drafts: list[Draft] = []
        self.pending: list[tuple[Draft, _LaneInfo]] = []

    def run(self) -> IngestResult:
        for line_no, obj in self.scan.records():
            self.scan.count("records")
            guarded(self.scan, f"line:{line_no}", lambda o=obj, n=line_no: self._record(o, n))
        self._assign_lanes()
        requests, sessions = assemble(self.drafts, self.shells, self.opts)
        return self.scan.finish(requests=requests, sessions=sessions, events=[],
                                capabilities=lane_capabilities(requests, self.shells))

    def _record(self, obj: Mapping[str, Any], line_no: int) -> None:
        locator = f"line:{line_no}"
        meta, resp, batch, batch_rid, http = _unwrap(obj)
        if not isinstance(resp, Mapping):
            self.scan.quarantine(locator, "missing:response")
            return
        usage = resp.get("usage")
        kind = resp.get("object")
        if not member(kind, _OBJECTS):
            if isinstance(usage, Mapping) and "prompt_tokens" in usage:
                kind = "chat.completion"
            elif isinstance(usage, Mapping) and "input_tokens" in usage:
                kind = "response"
            else:
                self.scan.quarantine(locator, "missing:object")
                return
        responses = kind == "response"
        ts = meta_ts(meta)
        if ts is None:
            created = resp.get("created_at" if responses else "created")
            ts = _seconds_ms(created)
        if ts is None:
            self.scan.quarantine(locator, "missing:created_at" if responses else "missing:created")
            return
        if not self.scan.in_window(ts):
            self.scan.count("outside_window")
            return
        response_id = clean_label(resp.get("id"))
        status = resp.get("status") if responses else None
        failed = status == "failed" or (isinstance(http, int) and http >= 400)
        if not isinstance(usage, Mapping) and not failed and status != "cancelled":
            self.scan.quarantine(locator, "missing:usage")
            return

        # channel, scope, billing path
        channel = meta.get("channel") if meta and member(meta.get("channel"), _CHANNELS) \
            else "openai_api"
        account_raw = None
        if meta:
            account_raw = meta.get("subscription_id") or meta.get("account")
        account = pseudonym(self.opts.name_key, "h", account_raw.strip()) \
            if isinstance(account_raw, str) and account_raw.strip() else None
        scope_key = cache_scope(channel, account)
        billing = meta.get("billing_path") if meta else None
        billing_path = billing if billing in BILLING_PATHS else (
            self.opts.attribution.billing_path or BILLING_PATH_BY_CHANNEL[channel])
        endpoint_scope = meta.get("endpoint_scope") if meta else None
        if not member(endpoint_scope, _SCOPES):
            endpoint_scope = dict(self.opts.attribution.extra).get("endpoint_scope")
        if not member(endpoint_scope, _SCOPES):
            endpoint_scope = "unknown"

        model_raw = clean_label(meta.get("model_raw")) if meta else None
        model_raw = model_raw or clean_label(resp.get("model")) or ""
        mid = normalize_model(model_raw)
        if not mid.model:
            self.scan.note("dq.unpriced_model")
        tier_raw = resp.get("service_tier")
        tier = "batch" if batch else SERVICE_TIERS.get(tier_raw, "unknown") \
            if isinstance(tier_raw, str) else "standard"
        ctx = PricingContext(provider="openai", channel=channel, model=mid.model,
                             model_raw=model_raw, service_tier=tier,
                             endpoint_scope=endpoint_scope, billing_path=billing_path)

        conv = resp.get("conversation")
        lane_info = _LaneInfo(
            session=key_part(meta.get("session")) if meta else None,
            lane=key_part(meta.get("lane")) if meta else None,
            conversation=key_part(conv.get("id") if isinstance(conv, Mapping) else conv),
            response_id=response_id, previous=clean_label(resp.get("previous_response_id")),
            locator=locator, scope=scope_key)
        request_id = request_id_for("openai", None, self.scan.source_id,
                                    f"resp:{response_id}" if response_id else locator)
        att_id = attempt_id(request_id, 0)

        inferences: tuple[Inference, ...] = ()
        raw_json = None
        convention = OPENAI_RESPONSES if responses else OPENAI_CHAT
        if isinstance(usage, Mapping):
            buckets, codes = (normalize_openai_responses if responses
                              else normalize_openai_chat)(usage)
            self.scan.notes(codes)
            inferences = (Inference(inference_id=stable_id("inf", att_id, 0),
                                    kind=InferenceKind.MESSAGE, usage=buckets, pricing=ctx),)
            raw_json = canonical_usage_json(usage)

        outcome, http_status, error_type = Outcome.OK, None, None
        if failed:
            http_status = http if isinstance(http, int) and 100 <= http <= 599 else None
            outcome = Outcome.HTTP_ERROR
            err = resp.get("error")
            code = err.get("code") if isinstance(err, Mapping) else None
            error_type = _ERROR_CODES.get(code, error_type_for(http_status)) \
                if isinstance(code, str) else error_type_for(http_status)
        elif status == "cancelled":
            outcome = Outcome.ABORTED

        diag = resp.get("prompt_cache_diagnostics")
        if diag is None and isinstance(usage, Mapping):
            diag = usage.get("prompt_cache_diagnostics")
        diagnostic = canonical_diagnostic(diag)
        if diag is not None and diagnostic is None and not (
                isinstance(diag, Mapping) and diag.get("type") == "cache_hit"):
            self.scan.note("dq.unknown_fields")

        duration = to_int(meta.get("duration_ms")) if meta else None
        if duration is None:
            done = _seconds_ms(resp.get("completed_at"))
            duration = done - ts if done is not None and done >= ts else None
        attempt = Attempt(
            attempt_id=att_id, attempt_no=0, ts_start_ms=ts,
            ttft_ms=to_int(meta.get("ttft_ms")) if meta else None, duration_ms=duration,
            outcome=outcome, http_status=http_status, error_type=error_type, retry_layer=None,
            retry_after_ms=None, should_retry=None,
            provider_request_id=response_id or batch_rid, provider_message_id=None,
            model_served=clean_label(resp.get("model")),
            stop_reason=self._stop_reason(resp, responses), inferences=inferences,
            diagnostics=diagnostic, raw_usage_json=raw_json,
            convention_id=convention if inferences else None)
        reasoning = resp.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
        params = RequestParams(
            model_requested=model_raw,
            max_tokens=to_int(resp.get("max_output_tokens" if responses
                                       else "max_completion_tokens")),
            effort=effort if member(effort, _EFFORT_LABELS) else None,
            service_tier_requested=None)
        attribution = attribution_from(
            self.opts, self.scan,
            meta.get("attribution") if meta and isinstance(meta.get("attribution"), Mapping)
            else None)
        if attribution.agent_product is None:
            attribution = dataclasses.replace(attribution, agent_product="api")
        draft = Draft(
            request_id=request_id, session_key="", lane_key="", ts_ms=ts,
            order=(line_no,), attribution=attribution, params=params, attempts=[attempt],
            source=source_ref(self.scan, locator, Fidelity.FULL, _PRIORITY),
            end_ms=ts + (duration or 0))
        self.pending.append((draft, lane_info))

    @staticmethod
    def _stop_reason(resp: Mapping[str, Any], responses: bool) -> str | None:
        if responses:
            details = resp.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, Mapping) else None
        else:
            choices = resp.get("choices")
            first = choices[0] if isinstance(choices, list) and choices else None
            reason = first.get("finish_reason") if isinstance(first, Mapping) else None
        label = clean_label(reason, enum=True)
        return _STOP_REASONS.get(label or "", label) if label else None

    def _assign_lanes(self) -> None:
        """Lanes: request_meta session/lane, else conversation id, else the
        ``previous_response_id`` chain (rooted inside the file), else one inexact lane each."""
        prev_of = {info.response_id: info.previous for _d, info in self.pending
                   if info.response_id}
        children = {info.previous for _d, info in self.pending if info.previous}

        def root(response_id: str) -> str:
            cur, steps = response_id, 0
            while prev_of.get(cur) and steps < 10_000:
                cur, steps = prev_of[cur], steps + 1  # type: ignore[assignment]
            return cur

        for draft, info in self.pending:
            exact = True
            if info.session or info.lane:
                session = info.session or info.lane
                part: tuple[str, ...] = ("meta", session or "", info.lane or "main")
            elif info.conversation:
                session = f"conv:{info.conversation}"
                part = ("conv", info.conversation)
            elif info.previous or (info.response_id and info.response_id in children):
                chain = root(info.response_id or info.previous or "")
                session = f"chain:{chain}"
                part = ("chain", chain)
            else:
                session = f"single:{info.response_id or info.locator}"
                part = ("single", info.response_id or info.locator)
                exact = False
                self.scan.note("dq.lanes_inferred")
            draft.session_key = stable_id("ses", "openai", session or "")
            draft.lane_key = stable_id("ln", "openai", *part)
            if draft.lane_key not in self.shells:
                self.shells[draft.lane_key] = LaneShell(
                    lane_key=draft.lane_key, session_key=draft.session_key,
                    kind=LaneKind.API_RUN, parent_lane_key=None, cache_scope_key=info.scope,
                    lane_exact=exact, source_kind="openai")
            self.drafts.append(draft)


@dataclass(frozen=True)
class _LaneInfo:
    session: str | None
    lane: str | None
    conversation: str | None
    response_id: str | None
    previous: str | None
    locator: str
    scope: str


def _seconds_ms(value: object) -> int | None:
    """Unix seconds (int, integral or fractional float) → ms."""
    if type(value) is int and 0 < value < 2**40:
        return value * 1000
    if type(value) is float and 0 < value < 2**40:
        return round(value * 1000)
    return None


class OpenAIUsageAdapter:
    """``openai``: OpenAI Responses / Chat Completions usage JSONL (SPEC §5.10)."""

    name = "openai"
    capabilities = frozenset({"usage_sequence", "timing", "ttft", "diagnostics", "attempts",
                              "params", "lanes_exact", "attribution.team", "workload"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """True when the first record is a Responses / Chat Completions object (bare, paired
        with ``request_meta``, or a Batch API output line)."""
        obj, line = head_record(head)
        if obj is not None:
            return _is_openai(obj)
        return bool(line) and (b'"object":"response"' in line[:4096]
                               or b'"object": "response"' in line[:4096]
                               or b'"object":"chat.completion"' in line[:4096]
                               or b'"object": "chat.completion"' in line[:4096])

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Read one OpenAI usage JSONL file (see the module docstring)."""
        return _Reader(Path(path), opts).run()

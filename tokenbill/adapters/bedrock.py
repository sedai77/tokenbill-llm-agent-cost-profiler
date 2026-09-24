"""Amazon Bedrock usage adapter (SPEC §5.10; registry name ``bedrock``).

Reads JSONL of Bedrock **model-invocation log records** (``schemaType: "ModelInvocationLog"``:
``timestamp``, ``accountId``, ``requestId``, ``operation``, ``modelId``, ``identity.arn``,
``requestMetadata``, ``output.outputBodyJson``) and of **Converse responses**, bare or as
``{request_meta, response}`` pairs (a bare response is dated by ``request_meta.ts_ms`` or the boto3
``ResponseMetadata.HTTPHeaders.date``, else quarantined). Usage comes from the logged response body
(``output.outputBodyJson.usage``; streams: the Converse ``metadata`` event, or the Anthropic
``message_start`` + ``message_delta`` events); the log's ``inputTokenCount`` / ``outputTokenCount``
carry no cache split and are never used. Conventions by usage shape: Converse
(``inputTokens`` …) → ``bedrock.converse`` (writes split by ``cacheDetails`` TTL, the remainder is
``cache_write_unknown``); an Anthropic InvokeModel body (``input_tokens`` …) →
``anthropic.messages`` (iterations and refusal rule); OpenAI bodies → ``openai.responses`` /
``openai.chat``.

* Model ids go through ``core.models.normalize_model``: ``global.`` profiles price at the global
  rate, in-region and geo profiles (``us.`` …) at the regional one (``endpoint_scope``). ARNs keep
  only their resource name as ``model_raw``.
* ``identity.arn`` maps to a team through ``opts.team_map`` (the full ARN, then the
  ``assumed-role/<role>`` form), becomes a ``p_`` pseudonym under the principal key, and is
  dropped. ``requestMetadata``: allowlisted attribution keys (``team``, ``cost_center``,
  ``project``, ``agent_product``, ``workload_class``, …, ``EXTRA_KEYS``) plus ``session`` / ``lane``
  for lane grouping (hashed); anything else is dropped (``dq.unknown_fields``).
* Billing path ``bedrock``; cache scope ``org:bedrock:<h_ account>``; served tier from the Converse
  ``serviceTier.type``; ``metrics.latencyMs`` → duration.
"""

from __future__ import annotations

import dataclasses
import email.utils
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tokenbill.adapters.conventions_ext import (
    BEDROCK_CONVERSE,
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
    guarded,
    head_record,
    key_part,
    lane_capabilities,
    meta_ts,
    normalize_bedrock_converse,
    normalize_openai_chat,
    normalize_openai_responses,
    parse_iso_ms,
    source_ref,
    team_for,
    to_int,
)
from tokenbill.core.conventions import ANTHROPIC_MESSAGES, BadUsageError, anthropic_inferences
from tokenbill.core.ids import pseudonym, request_id_for, stable_id
from tokenbill.core.models import ModelId, normalize_model
from tokenbill.core.records import (
    Attempt,
    Fidelity,
    Inference,
    InferenceKind,
    LaneKind,
    Outcome,
    PricingContext,
    RequestParams,
)
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["BedrockAdapter", "bedrock_model"]

_PRIORITY = 10
_LOG_SCHEMA = "ModelInvocationLog"
_OPENAI_ID_RE = re.compile(
    r"\A(?:(?P<geo>[a-z]{2,8}(?:-[a-z]+)?)\.)?openai\.(?P<model>[A-Za-z0-9.-]+?)"
    r"(?:-v\d+(?::\d+)?)?\Z")
#: Converse ``serviceTier.type`` / Anthropic ``usage.service_tier`` → PricingContext tier.
SERVICE_TIERS: Mapping[str, str] = {"default": "standard", "standard": "standard",
                                    "priority": "priority", "flex": "flex", "batch": "batch"}
#: Bedrock error codes (Converse API reference) → (HTTP status, Attempt.error_type).
ERRORS: Mapping[str, tuple[int, str]] = {
    "ThrottlingException": (429, "rate_limit"), "ModelNotReadyException": (429, "overloaded"),
    "ServiceUnavailableException": (503, "overloaded"), "ModelTimeoutException": (408, "timeout"),
    "ValidationException": (400, "invalid_request"), "AccessDeniedException": (403, "auth"),
    "ResourceNotFoundException": (404, "invalid_request"),
    "InternalServerException": (500, "other"), "ModelErrorException": (424, "other"),
}
#: ``requestMetadata`` keys used for lane grouping (hashed into keys, never stored).
_LANE_KEYS = ("session", "lane")


def bedrock_model(model_id: object) -> tuple[str, str, ModelId]:
    """(provider, ``model_raw`` label, normalized id) of a Bedrock model / profile id or ARN."""
    raw = model_id.strip() if isinstance(model_id, str) else ""
    label = raw.rsplit("/", 1)[-1] if raw.startswith("arn:") else raw
    m = _OPENAI_ID_RE.match(label)
    if m is not None:
        scope = "global" if m.group("geo") == "global" else "regional"
        return "openai", clean_label(label) or "", ModelId(
            model=m.group("model"), channel_hint="bedrock", endpoint_scope=scope, reason=None)
    if "anthropic." in label:
        return "anthropic", clean_label(label) or "", normalize_model(raw, "bedrock")
    provider = clean_label(label.split(".")[-2] if label.count(".") >= 1 else "", enum=True)
    return provider or "unknown", clean_label(label) or "", ModelId(
        model="", channel_hint="bedrock", endpoint_scope="unknown", reason="unknown model")


def _stream_usage(events: list[Any]) -> tuple[dict[str, Any] | None, str | None, Any]:
    """(usage, stop reason, latency) of a logged stream body (Converse or Anthropic events)."""
    usage: dict[str, Any] | None = None
    stop = latency = None
    for ev in events:
        if not isinstance(ev, Mapping):
            continue
        meta = ev.get("metadata")
        if isinstance(meta, Mapping) and isinstance(meta.get("usage"), Mapping):
            usage = dict(meta["usage"])
            metrics = meta.get("metrics")
            latency = metrics.get("latencyMs") if isinstance(metrics, Mapping) else None
        message_stop = ev.get("messageStop")
        if isinstance(message_stop, Mapping):
            stop = message_stop.get("stopReason")
        etype = ev.get("type")
        if etype == "message_start" and isinstance(ev.get("message"), Mapping):
            start_usage = ev["message"].get("usage")
            if isinstance(start_usage, Mapping):
                usage = dict(start_usage)
        elif etype == "message_delta":
            delta_usage = ev.get("usage")
            if isinstance(delta_usage, Mapping):
                usage = {**(usage or {}), **delta_usage}
            delta = ev.get("delta")
            if isinstance(delta, Mapping) and delta.get("stop_reason"):
                stop = delta.get("stop_reason")
    return usage, stop, latency


def _body_usage(body: Any) -> tuple[Mapping[str, Any] | None, Any, Any, Any]:
    """(usage, stop reason, served tier, latency) of a logged or returned response body."""
    if isinstance(body, list):
        usage, stop, latency = _stream_usage(body)
        return usage, stop, None, latency
    if not isinstance(body, Mapping):
        return None, None, None, None
    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else None
    stop = body.get("stopReason") or body.get("stop_reason")
    tier = body.get("serviceTier")
    tier = tier.get("type") if isinstance(tier, Mapping) else None
    metrics = body.get("metrics")
    latency = metrics.get("latencyMs") if isinstance(metrics, Mapping) else None
    return usage, stop, tier, latency


def _http_date_ms(metadata: object) -> int | None:
    """The ``date`` header of a boto3 ``ResponseMetadata`` (RFC 7231) as epoch ms, else None."""
    headers = metadata.get("HTTPHeaders") if isinstance(metadata, Mapping) else None
    value = headers.get("date") if isinstance(headers, Mapping) else None
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    ms = int(when.timestamp()) * 1000
    return ms if ms > 0 else None


def _role_arn(arn: str) -> str | None:
    """``arn:aws:sts::<acct>:assumed-role/<Role>/<session>`` → ``…:assumed-role/<Role>``."""
    parts = arn.split("/")
    return "/".join(parts[:2]) if ":assumed-role/" in arn and len(parts) >= 3 else None


class _Reader:
    def __init__(self, path: Path, opts: IngestOptions) -> None:
        self.opts = opts
        self.scan = SourceScan("bedrock", path, opts)
        self.shells: dict[str, LaneShell] = {}
        self.drafts: list[Draft] = []

    def run(self) -> IngestResult:
        """Read the whole file and build the :class:`IngestResult`."""
        for line_no, obj in self.scan.records():
            self.scan.count("records")
            guarded(self.scan, f"line:{line_no}", lambda o=obj, n=line_no: self._record(o, n))
        requests, sessions = assemble(self.drafts, self.shells, self.opts, self.scan)
        caps = lane_capabilities(requests, self.shells)
        if requests and not any(inf.usage.cache_write_unknown for r in requests
                                for inf in r.billable_inferences):
            caps.add("ttl_split")
        if any(r.final_attempt.convention_id == ANTHROPIC_MESSAGES and len(
                r.final_attempt.inferences) > 1 for r in requests):
            caps.add("iterations")
        return self.scan.finish(requests=requests, sessions=sessions, events=[],
                                capabilities=caps)

    def _record(self, obj: Mapping[str, Any], line_no: int) -> None:
        locator = f"line:{line_no}"
        if obj.get("schemaType") == _LOG_SCHEMA:
            self._log(obj, locator, line_no)
            return
        meta = obj.get("request_meta") if isinstance(obj.get("request_meta"), Mapping) else None
        body = obj.get("response") if meta is not None or "response" in obj else obj
        if not isinstance(body, Mapping):
            self.scan.quarantine(locator, "missing:response")
            return
        headers = body.get("ResponseMetadata")
        ts = meta_ts(meta)
        if ts is None:
            ts = _http_date_ms(headers)
        if ts is None:
            self.scan.quarantine(locator, "missing:request_meta.ts_ms")
            return
        rid = clean_label(meta.get("request_id")) if meta else None
        if rid is None and isinstance(headers, Mapping):
            rid = clean_label(headers.get("RequestId"))
        attr_meta = meta.get("attribution") if meta and isinstance(
            meta.get("attribution"), Mapping) else None
        self._build(locator=locator, line_no=line_no, ts=ts,
                    model_id=(meta.get("model_raw") or meta.get("modelId")) if meta else None,
                    body=body, rid=rid, arn=None,
                    account=meta.get("account") if meta else None,
                    session=key_part(meta.get("session")) if meta else None,
                    lane=key_part(meta.get("lane")) if meta else None,
                    attr_meta=attr_meta, error_code=None)

    def _log(self, obj: Mapping[str, Any], locator: str, line_no: int) -> None:
        ts = parse_iso_ms(obj.get("timestamp"))
        if ts is None:
            self.scan.quarantine(locator, "missing:timestamp")
            return
        identity = obj.get("identity")
        arn = identity.get("arn") if isinstance(identity, Mapping) else None
        metadata = obj.get("requestMetadata")
        attr_meta: dict[str, Any] = {}
        session = lane = None
        if isinstance(metadata, Mapping):
            for key, value in metadata.items():
                if key in _LANE_KEYS:
                    if key == "session":
                        session = key_part(value)
                    else:
                        lane = key_part(value)
                else:
                    attr_meta[key] = value
        output = obj.get("output")
        body = output.get("outputBodyJson") if isinstance(output, Mapping) else None
        code = obj.get("errorCode")
        self._build(locator=locator, line_no=line_no, ts=ts, model_id=obj.get("modelId"),
                    body=body, rid=clean_label(obj.get("requestId")),
                    arn=arn if isinstance(arn, str) and arn.strip() else None,
                    account=obj.get("accountId"), session=session, lane=lane,
                    attr_meta=attr_meta, error_code=code if isinstance(code, str) else None)

    def _build(self, *, locator: str, line_no: int, ts: int, model_id: object, body: Any,
               rid: str | None, arn: str | None, account: object, session: str | None,
               lane: str | None, attr_meta: Mapping[str, Any] | None,
               error_code: str | None) -> None:
        opts = self.opts
        if not self.scan.in_window(ts):
            self.scan.count("outside_window")
            return
        usage, stop, tier_raw, latency = _body_usage(body)
        if usage is None and error_code is None:
            self.scan.quarantine(locator, "missing:output.outputBodyJson.usage")
            return
        provider, model_raw, mid = bedrock_model(model_id)
        if not mid.model:
            self.scan.note("dq.unpriced_model")
        acct = pseudonym(opts.name_key, "h", str(account).strip()) \
            if isinstance(account, (str, int)) and str(account).strip() else None
        scope_key = cache_scope("bedrock", acct)
        request_id = request_id_for("bedrock", None, self.scan.source_id,
                                    f"req:{rid}" if rid else locator)
        att_id = attempt_id(request_id, 0)
        endpoint_scope = mid.endpoint_scope if mid.endpoint_scope in ("global", "regional") \
            else dict(opts.attribution.extra).get("endpoint_scope", "unknown")
        if endpoint_scope not in ("global", "regional"):
            endpoint_scope = "unknown"
        tier = SERVICE_TIERS.get(tier_raw, "unknown") if isinstance(tier_raw, str) \
            else "standard"
        ctx = PricingContext(provider=provider, channel="bedrock", model=mid.model,
                             model_raw=model_raw, service_tier=tier,
                             endpoint_scope=endpoint_scope, billing_path="bedrock")
        inferences: list[Inference] = []
        convention = None
        if usage is not None:
            inferences, convention = self._inferences(usage, ctx, model_raw, att_id)
        outcome, status, error_type = Outcome.OK, None, None
        if not inferences and error_code is not None:
            status, error_type = ERRORS.get(error_code, (None, "other"))
            outcome = Outcome.HTTP_ERROR if status is not None else Outcome.UNKNOWN
        fidelity = Fidelity.NO_TTL_SPLIT if any(i.usage.cache_write_unknown for i in inferences) \
            else Fidelity.FULL
        latency_ms = to_int(latency)
        attempt = Attempt(
            attempt_id=att_id, attempt_no=0, ts_start_ms=ts, ttft_ms=None,
            duration_ms=latency_ms, outcome=outcome, http_status=status, error_type=error_type,
            retry_layer=None, retry_after_ms=None, should_retry=None, provider_request_id=rid,
            provider_message_id=None, model_served=model_raw or None,
            stop_reason=clean_label(stop, enum=True), inferences=tuple(inferences),
            raw_usage_json=canonical_usage_json(usage) if usage is not None else None,
            convention_id=convention)
        team = team_for(opts, [arn, _role_arn(arn) if arn else None])
        attribution = attribution_from(opts, self.scan, attr_meta, raw_principal=arn,
                                       team=team)
        if attribution.billing_path is None:
            attribution = dataclasses.replace(attribution, billing_path="bedrock")
        session_key, lane_key = self._lane(session, lane, request_id, scope_key)
        self.drafts.append(Draft(
            request_id=request_id, session_key=session_key, lane_key=lane_key, ts_ms=ts,
            order=(line_no,), attribution=attribution,
            params=RequestParams(model_requested=model_raw), attempts=[attempt],
            source=source_ref(self.scan, locator, fidelity, _PRIORITY),
            end_ms=ts + (latency_ms or 0)))

    def _inferences(self, usage: Mapping[str, Any], ctx: PricingContext, model_raw: str,
                    att_id: str) -> tuple[list[Inference], str]:
        if "inputTokens" in usage or "outputTokens" in usage:
            buckets, codes = normalize_bedrock_converse(usage)
            convention = BEDROCK_CONVERSE
        elif "prompt_tokens" in usage:
            buckets, codes = normalize_openai_chat(usage)
            convention = OPENAI_CHAT
        elif "input_tokens_details" in usage or ctx.provider == "openai":
            buckets, codes = normalize_openai_responses(usage)
            convention = OPENAI_RESPONSES
        elif "input_tokens" in usage or "output_tokens" in usage:
            tier_raw = usage.get("service_tier")
            geo = usage.get("inference_geo")
            actx = dataclasses.replace(
                ctx, service_tier=SERVICE_TIERS.get(tier_raw, ctx.service_tier)
                if isinstance(tier_raw, str) else ctx.service_tier,
                speed="fast" if usage.get("speed") == "fast" else "standard",
                inference_geo=geo if geo in ("us", "global") else None)
            infs, codes = anthropic_inferences(usage, message_model=model_raw, ctx=actx,
                                               id_prefix=att_id)
            self.scan.notes(codes)
            return infs, ANTHROPIC_MESSAGES
        else:
            raise BadUsageError("bad_usage: usage")
        self.scan.notes(codes, tokens=buckets.cache_write_unknown or None)
        return [Inference(inference_id=stable_id("inf", att_id, 0), kind=InferenceKind.MESSAGE,
                          usage=buckets, pricing=ctx)], convention

    def _lane(self, session: str | None, lane: str | None, request_id: str,
              scope: str) -> tuple[str, str]:
        exact = True
        if session or lane:
            session = session or lane
            session_key = stable_id("ses", "bedrock", session or "")
            lane_key = stable_id("ln", "bedrock", session or "", lane or "main")
        else:
            session_key = stable_id("ses", "bedrock", "single", request_id)
            lane_key = stable_id("ln", "bedrock", "single", request_id)
            exact = False
            self.scan.note("dq.lanes_inferred")
        if lane_key not in self.shells:
            self.shells[lane_key] = LaneShell(lane_key=lane_key, session_key=session_key,
                                              kind=LaneKind.API_RUN, parent_lane_key=None,
                                              cache_scope_key=scope, lane_exact=exact,
                                              source_kind="bedrock")
        return session_key, lane_key


def _is_bedrock(obj: Mapping[str, Any]) -> bool:
    if obj.get("schemaType") == _LOG_SCHEMA:
        return True
    body = obj.get("response") if isinstance(obj.get("request_meta"), Mapping) else obj
    usage = body.get("usage") if isinstance(body, Mapping) else None
    return isinstance(usage, Mapping) and "inputTokens" in usage and "outputTokens" in usage


class BedrockAdapter:
    """``bedrock``: Bedrock invocation logs and Converse responses (SPEC §5.10)."""

    name = "bedrock"
    capabilities = frozenset({"usage_sequence", "timing", "ttl_split", "iterations", "attempts",
                              "params", "lanes_exact", "attribution.team", "workload"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """True for a model-invocation log record or a Converse response (``usage.inputTokens``)."""
        obj, line = head_record(head)
        if obj is not None:
            return _is_bedrock(obj)
        return bool(line) and re.search(rb'"schemaType"\s*:\s*"ModelInvocationLog"',
                                        line[:4096]) is not None

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Read one Bedrock JSONL file (see the module docstring)."""
        return _Reader(Path(path), opts).run()

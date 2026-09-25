"""GitHub Agentic Workflows ``token-usage.jsonl`` adapter (addendum §5.14, DC21; package CP-OTEL;
registry name ``gh-aw-token-usage``).

The gh-aw API proxy writes one JSON line per model call (``pkg/cli/token_usage_types.go``):
``_schema, timestamp, event, request_id, provider, model, path, status, streaming, input_tokens,
output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens?, duration_ms,
response_bytes, x_initiator, ai_credits_this_response, ai_credits_total, ai_credits_pricing_source,
ai_credits_pricing_tier, ai_credits_accounting_policy, ai_credits_fallback_pricing_used,
input_tokens_include_cache?``. Only this file of a gh-aw run artifact is ever opened (the copied
session-state directories are never read).

* Only ``provider == "copilot"`` lines become requests (others are counted in
  ``stats["non_copilot_lines"]`` and skipped); one request per line; ``request_id`` →
  ``provider_request_id`` (and the request id, so a line seen in two artifacts counts once);
  tokens under the convention ``gh_aw.token_usage`` registered here (input inclusive of cache read
  and write iff ``input_tokens_include_cache`` is true; absent → decided by the sum-check; writes →
  ``cache_write_unknown``); ``status`` → attempt outcome; ``path`` kept (as
  ``Attribution.entrypoint``) only when it is ``/chat/completions``, ``/responses`` or
  ``/v1/messages``.
* ``ai_credits_this_response`` → ``provider_reported_cost_nano`` on basis ``provider_estimate``
  (gh-aw prices from ``ai_credits_pricing_source``, e.g. models.dev: a tool estimate, R12 — never a
  price; a model without a Copilot rate row, such as GitHub's fixture model
  ``gpt-4o-mini-2024-07-18``, stays unpriced); the last ``ai_credits_total`` → one COST_STATE event
  ``gh_aw.run_total`` on the run's lane.
* One session per file (session key from the source id), lane MAIN; ``workload_class=ci``,
  ``agent_product="copilot_gh_aw"``; billing path from ``--attr`` else ``copilot_direct``
  (``dq.copilot_billing_path_assumed``); repository and workflow from ``--attr`` (``repo``,
  ``extra.workflow``) or the ``GITHUB_REPOSITORY`` / ``GITHUB_WORKFLOW_REF`` /
  ``GITHUB_WORKFLOW`` environment, always as ``h_`` values.
* One ``UsageAggregate(source_kind="gh_aw.run")`` per file (dims ``channel``, ``repo``,
  ``workflow``, ``source``; usage = Σ tokens; ``reported_cost_nano`` = the run total, basis
  ``provider_estimate``) so the aggregate detector gets per-run statistics without lanes.

Sniff: the first line carries ``"_schema":"token-usage/`` and ``"event":"token_usage"``.
Capabilities ``{credits, timing, aggregates}``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tokenbill.adapters.copilot_otel import (
    Scan,
    billing_path_of,
    compliance_of,
    parse_iso_ms,
)
from tokenbill.core.conventions import BadUsageError, Convention, register_convention
from tokenbill.core.errors import ContractViolation
from tokenbill.core.ids import copilot_lane_key, natural_id, stable_id
from tokenbill.core.jsonl import parse_json_line
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.money import credits_str_to_nano
from tokenbill.core.records import (
    MAX_TOKENS,
    Attempt,
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
    WorkloadClass,
)
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["ADAPTER_NAME", "CONVENTION", "GhAwTokenUsageAdapter", "normalize_gh_aw"]

ADAPTER_NAME = "gh-aw-token-usage"
CONVENTION = "gh_aw.token_usage"
SOURCE_KIND = "gh_aw.run"
REPORTER_RUN_TOTAL = "gh_aw.run_total"
PRIORITY = 20
_PATHS = frozenset({"/chat/completions", "/responses", "/v1/messages"})
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
               "reasoning_tokens")
_FLAG = "input_tokens_include_cache"
_MAX_CREDITS = Decimal(10) ** 12


def _count(raw: Mapping[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_TOKENS:
        raise BadUsageError(f"bad_usage: {key}")
    return value


def normalize_gh_aw(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """The ``gh_aw.token_usage`` mapping (addendum §5.11): ``input_tokens`` includes
    ``cache_read_tokens`` and ``cache_write_tokens`` iff ``input_tokens_include_cache`` is true;
    when the flag is absent the sum-check decides (read + write ≤ input → inclusive); a failed
    check, or a false flag, keeps the counts exclusive (the failed check notes
    ``dq.convention_mismatch``). Writes → ``cache_write_unknown``; reasoning is a subset of
    output."""
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    flag = raw.get(_FLAG)
    if flag is not None and type(flag) is not bool:
        raise BadUsageError(f"bad_usage: {_FLAG}")
    notes: list[str] = []
    inp = _count(raw, "input_tokens") or 0
    read = _count(raw, "cache_read_tokens") or 0
    write = _count(raw, "cache_write_tokens") or 0
    out = _count(raw, "output_tokens") or 0
    reasoning = _count(raw, "reasoning_tokens")
    uncached = inp
    if flag is not False:
        if read + write <= inp:
            uncached = inp - read - write
        else:
            notes.append("dq.convention_mismatch")
    if reasoning is not None and reasoning > out:
        reasoning = None
        notes.append("dq.sum_check_failed")
    try:
        buckets = UsageBuckets(uncached_input=uncached, cache_read=read, cache_write_unknown=write,
                               output=out, output_reasoning=reasoning)
    except ContractViolation:
        raise BadUsageError("bad_usage: total") from None
    return buckets, notes


register_convention(
    Convention(convention_id=CONVENTION, provider="github", inclusive_input=True, enabled=True,
               notes="gh-aw proxy token-usage.jsonl: input includes cache read and write iff "
                     "input_tokens_include_cache (absent: sum-check); writes -> unknown TTL"),
    normalize_gh_aw)


def _credits_nano(value: object) -> int | None:
    """AI credits (a JSON number or decimal string, exact) → nano-USD; None when invalid."""
    if type(value) is int:
        text = str(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            return None
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount > _MAX_CREDITS:
        return None
    try:
        return credits_str_to_nano(text)[0]
    except (ValueError, TypeError):
        return None


def _label(value: object, max_len: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_len or any(ord(c) < 32 or ord(c) == 127 for c in text):
        return None
    return text


def _nonneg(value: object) -> int | None:
    if type(value) is int and 0 <= value <= MAX_TOKENS:
        return value
    return None


def _head_line(head: bytes) -> dict[str, Any] | None:
    for raw in head.split(b"\n"):
        if raw.strip():
            return parse_json_line(raw.strip())
    return None


class GhAwTokenUsageAdapter:
    """gh-aw ``token-usage.jsonl`` → one Copilot request per proxy line, the run total and one
    per-run aggregate. *env* (default ``os.environ``) supplies ``GITHUB_*`` names when ``--attr``
    does not."""

    name = ADAPTER_NAME
    capabilities = frozenset({"credits", "timing", "aggregates"})

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env

    def sniff(self, path: Path, head: bytes) -> bool:
        """The first line has ``_schema`` ``token-usage/…`` and event ``token_usage``."""
        obj = _head_line(head)
        if obj is None:
            return False
        schema = obj.get("_schema")
        return isinstance(schema, str) and schema.startswith("token-usage/") \
            and obj.get("event") == "token_usage"

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse every line of *path*."""
        return _Reader(self.name, Path(path), opts,
                       self._env if self._env is not None else os.environ).run()


class _Reader:
    def __init__(self, adapter: str, path: Path, opts: IngestOptions,
                 env: Mapping[str, str]) -> None:
        self.scan = Scan(adapter, path, opts)
        self.opts = opts
        self.env = env
        self.session_key = stable_id("ses", SOURCE_KIND, self.scan.source_id)
        self.lane_key = copilot_lane_key(self.session_key, LaneKind.MAIN.value, None)
        self.billing_path: str | None = None
        self.compliance = compliance_of(opts)
        self.repo = self._hashed(opts.attribution.repo, ("GITHUB_REPOSITORY",))
        self.workflow = self._hashed(dict(opts.attribution.extra).get("workflow"),
                                     ("GITHUB_WORKFLOW_REF", "GITHUB_WORKFLOW"))

    def _hashed(self, stated: str | None, env_keys: tuple[str, ...]) -> str | None:
        value = stated
        if not value:
            value = next((self.env[k] for k in env_keys
                          if isinstance(self.env.get(k), str) and self.env[k].strip()), None)
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if value.startswith("h_") and len(value) == 22:
            return value
        return self.scan.name_hash(value)

    def run(self) -> IngestResult:
        requests: list[Request] = []
        seen: set[str] = set()
        total: tuple[int, int] | None = None     # (nano, ts)
        usage_sum = UsageBuckets()
        first_ts: int | None = None
        last_ts: int | None = None
        for line_no, obj in self.scan.records():
            self.scan.count("records")
            locator = f"line:{line_no}"
            if obj.get("event") != "token_usage":
                self.scan.count("other_events")
                continue
            if obj.get("provider") != "copilot":
                self.scan.count("non_copilot_lines")
                continue
            ts = parse_iso_ms(obj.get("timestamp"))
            if ts is None:
                self.scan.quarantine(locator, "missing:timestamp")
                continue
            if not self.scan.in_window(ts):
                self.scan.count("out_of_window")
                continue
            try:
                req = self._request(obj, ts, locator, len(requests))
            except BadUsageError:
                self.scan.quarantine(locator, "bad_usage")
                continue
            if req.request_id in seen:
                self.scan.count("duplicate_lines")
                continue
            seen.add(req.request_id)
            requests.append(req)
            usage_sum = usage_sum + req.attempts[0].inferences[0].usage
            start = req.ts_start_ms
            first_ts = start if first_ts is None else min(first_ts, start)
            last_ts = ts if last_ts is None else max(last_ts, ts)
            run_total = _credits_nano(obj.get("ai_credits_total"))
            if run_total is not None:
                total = (run_total, ts)
            elif obj.get("ai_credits_total") is not None:
                self.scan.count("bad_credits")
        return self._finish(requests, total, usage_sum, first_ts, last_ts)

    def _request(self, obj: Mapping[str, Any], ts: int, locator: str, seq: int) -> Request:
        opts = self.opts
        raw: dict[str, Any] = {}
        for key in _TOKEN_KEYS:
            value = obj.get(key)
            if value is not None:
                if type(value) is not int:
                    raise BadUsageError(f"bad_usage: {key}")
                raw[key] = value
        flag = obj.get(_FLAG)
        if type(flag) is bool:
            raw[_FLAG] = flag
        buckets, codes = normalize_gh_aw(raw)
        for code in codes:
            self.scan.note(code, tokens=buckets.total_input)
        if buckets.cache_write_unknown:
            self.scan.note("dq.no_ttl_split", tokens=buckets.cache_write_unknown)
        rid_raw = _label(obj.get("request_id"))
        request_id = stable_id("rq", SOURCE_KIND, rid_raw) if rid_raw is not None \
            else stable_id("rq", self.scan.source_id, locator)
        model_raw = _label(obj.get("model")) or ""
        cm = normalize_copilot_model(model_raw)
        if self.billing_path is None:
            self.billing_path = billing_path_of(opts, self.scan, "copilot_direct")
        elif opts.attribution.billing_path not in ("copilot_pool", "copilot_direct"):
            self.scan.note("dq.copilot_billing_path_assumed")
        cost = _credits_nano(obj.get("ai_credits_this_response"))
        if cost is None and obj.get("ai_credits_this_response") is not None:
            self.scan.count("bad_credits")
        pricing = PricingContext(provider="github", channel="github_copilot", model=cm.model,
                                 model_raw=model_raw, speed=cm.speed,
                                 billing_path=self.billing_path, routing=cm.routing,
                                 compliance=self.compliance)
        inference = Inference(
            inference_id=stable_id("inf", request_id, 0, 0), kind=InferenceKind.MESSAGE,
            usage=buckets, pricing=pricing, usage_source=UsageSource.FINAL, billable=True,
            provider_reported_cost_nano=cost,
            provider_reported_cost_basis="provider_estimate" if cost is not None else None)
        duration = _nonneg(obj.get("duration_ms"))
        start = ts - duration if duration is not None and duration <= ts else ts
        status = obj.get("status")
        http = status if type(status) is int and 100 <= status <= 599 else None
        if http is None:
            outcome = Outcome.UNKNOWN
        else:
            outcome = Outcome.OK if http < 400 else Outcome.HTTP_ERROR
        stream = obj.get("streaming")
        attempt = Attempt(
            attempt_id=stable_id("at", request_id, 0), attempt_no=0, ts_start_ms=start,
            ttft_ms=None, duration_ms=duration, outcome=outcome, http_status=http,
            error_type="http_error" if outcome is Outcome.HTTP_ERROR else None, retry_layer=None,
            retry_after_ms=None, should_retry=None, provider_request_id=rid_raw,
            provider_message_id=None, model_served=cm.model or None, stop_reason=None,
            inferences=(inference,), raw_usage_json=_canonical(raw), convention_id=CONVENTION)
        path = obj.get("path")
        entrypoint = path if isinstance(path, str) and path in _PATHS else None
        if entrypoint is None and path is not None:
            self.scan.count("paths_dropped")
        updates: dict[str, Any] = {"agent_product": "copilot_gh_aw",
                                   "workload_class": WorkloadClass.CI,
                                   "billing_path": self.billing_path, "query_source": "main"}
        if entrypoint is not None:
            updates["entrypoint"] = entrypoint
        if self.repo is not None:
            updates["repo"] = self.repo
        extra = dict(opts.attribution.extra)
        extra.pop("workflow", None)
        if self.workflow is not None:
            extra["workflow"] = self.workflow
        updates["extra"] = tuple(sorted(extra.items()))
        attribution = replace(opts.attribution, **updates)
        params = RequestParams(model_requested=cm.model or model_raw[:64],
                               stream=stream if type(stream) is bool else None)
        ref = SourceRef(adapter=self.scan.adapter, source_id=self.scan.source_id, locator=locator,
                        fidelity=Fidelity.NO_TTL_SPLIT, priority=PRIORITY)
        return Request(request_id=request_id, session_key=self.session_key, lane_key=self.lane_key,
                       seq=seq, attribution=attribution, params=params, attempts=(attempt,),
                       source=ref)

    def _finish(self, requests: list[Request], total: tuple[int, int] | None,
                usage: UsageBuckets, first_ts: int | None, last_ts: int | None
                ) -> IngestResult:
        sessions: list[Session] = []
        events: list[LaneEvent] = []
        aggregates: list[UsageAggregate] = []
        caps: set[str] = set()
        if requests and first_ts is not None and last_ts is not None:
            caps |= {"credits", "timing", "aggregates"}
            lane = Lane(lane_key=self.lane_key, session_key=self.session_key, kind=LaneKind.MAIN,
                        parent_lane_key=None, cache_scope_key="unknown", requests=())
            sessions.append(Session(session_key=self.session_key, source_kind=SOURCE_KIND,
                                    attribution=requests[0].attribution, lanes=(lane,),
                                    started_ms=first_ts, ended_ms=max(first_ts, last_ts)))
            if total is not None:
                events.append(LaneEvent(lane_key=self.lane_key, ts_ms=total[1],
                                        kind=LaneEventKind.COST_STATE,
                                        attrs=(("reported_total_nano", total[0]),
                                               ("reporter", REPORTER_RUN_TOTAL))))
            dims = [("channel", "github_copilot"), ("source", self.scan.source_id)]
            if self.repo is not None:
                dims.append(("repo", self.repo))
            if self.workflow is not None:
                dims.append(("workflow", self.workflow))
            aggregates.append(UsageAggregate(
                agg_id=natural_id("agg", SOURCE_KIND, self.scan.source_id),
                source_kind=SOURCE_KIND, bucket_start_ms=first_ts,
                bucket_end_ms=max(first_ts, last_ts), dims=tuple(sorted(dims)), usage=usage,
                reported_cost_nano=total[0] if total is not None else None,
                reported_cost_basis="provider_estimate" if total is not None else None,
                fetched_ms=self.opts.now_ms if 0 <= self.opts.now_ms <= MAX_TOKENS else 0))
        return self.scan.finish(self.scan.source_info(None), requests=requests,
                                sessions=sessions, events=events, aggregates=aggregates,
                                capabilities=caps)


def _canonical(raw: Mapping[str, Any]) -> str:
    return json.dumps(dict(raw), sort_keys=True, separators=(",", ":"))

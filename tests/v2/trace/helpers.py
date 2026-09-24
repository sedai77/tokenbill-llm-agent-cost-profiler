"""Test helpers of the TRACE area: keys, synthetic conversations, sample trace@2 record sets
covering every record type, and fake Anthropic-SDK doubles (sync / async clients, streams, an
httpx-like client with event hooks). Imported only by tests of this area (D45)."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tokenbill.adapters.fingerprint import fingerprint_request
from tokenbill.adapters.trace_v2 import write_trace_v2
from tokenbill.core.builders import CANARY
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.records import (
    AppendedItem,
    Attempt,
    Attribution,
    Breakpoint,
    CacheDiagnostic,
    ContentFingerprint,
    ContentTier,
    CostLine,
    Fidelity,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneKind,
    OutcomeAggregate,
    PricingContext,
    Request,
    RequestParams,
    Session,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    UsageSource,
)
from tokenbill.core.testing import CONFORMANCE_NAME_KEY, CONFORMANCE_PRINCIPAL_KEY
from tokenbill.core.types import DataQualityNote

NAME_KEY = CONFORMANCE_NAME_KEY
PRINCIPAL_KEY = CONFORMANCE_PRINCIPAL_KEY
FP_KEY = bytes(range(160, 192))
NAME_KID = key_id(NAME_KEY)
PRINCIPAL_KID = key_id(PRINCIPAL_KEY)
FP_KID = key_id(FP_KEY)
T0 = 1_790_000_000_000   # 2026-09-21T13:33:20Z
PRINCIPAL = pseudonym(PRINCIPAL_KEY, "p", "dev-7")
REPO = pseudonym(NAME_KEY, "h", "acme/payments")
API_KEY = pseudonym(NAME_KEY, "h", "sk-ant-api03-example")


# ---------------------------------------------------------------------------------------------
# synthetic conversations
# ---------------------------------------------------------------------------------------------


def tools() -> list[dict[str, Any]]:
    return [
        {"name": "read_file", "description": "Read a file. " + CANARY,
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        {"name": "run_tests", "description": "Run the test suite",
         "input_schema": {"type": "object", "properties": {}}},
    ]


def conversation(turns: int, *, canary: bool = True, filler: int = 200) -> list[dict[str, Any]]:
    """A user task followed by *turns* tool-use exchanges (assistant tool_use, user
    tool_result)."""
    mark = f" {CANARY}" if canary else ""
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "Fix the failing test." + mark}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": f"Step {i}: reading the code." + mark},
            {"type": "tool_use", "id": f"toolu_{i:04d}", "name": "read_file",
             "input": {"path": f"src/module_{i}.py"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i:04d}",
             "content": (f"line {i} " * filler) + mark}]})
    return msgs


SYSTEM = "You are a careful coding agent. " + CANARY


def fingerprint_of(messages: list[dict[str, Any]], tier: ContentTier = ContentTier.FINGERPRINT
                   ) -> tuple[ContentFingerprint, tuple[Breakpoint, ...], dict[str, str]]:
    return fingerprint_request(tools=tools(), system=SYSTEM, messages=messages, key=FP_KEY,
                               tier=tier)


# ---------------------------------------------------------------------------------------------
# sample record sets (every trace@2 record type)
# ---------------------------------------------------------------------------------------------


def ctx(model: str = "claude-opus-5-5", **kw: Any) -> PricingContext:
    return PricingContext(provider=kw.pop("provider", "anthropic"),
                          channel=kw.pop("channel", "anthropic_api"), model=model,
                          model_raw=kw.pop("model_raw", model), **kw)


@dataclasses.dataclass
class Sample:
    header: dict[str, Any]
    sessions: list[Session]
    requests: list[Request]
    events: list[LaneEvent]
    aggregates: list[UsageAggregate]
    cost_lines: list[CostLine]
    outcomes: list[OutcomeAggregate]
    notes: list[DataQualityNote]
    content: dict[str, str] | None

    def write(self, path: Path) -> int:
        return write_trace_v2(path, header=self.header, sessions=self.sessions,
                              requests=self.requests, events=self.events,
                              aggregates=self.aggregates, cost_lines=self.cost_lines,
                              outcomes=self.outcomes, notes=self.notes, content=self.content)


def header(profile: str = "usage", **kw: Any) -> dict[str, Any]:
    h: dict[str, Any] = {
        "trace_id": "t_sample0001", "profile": profile, "identity_mode": "install",
        "name_key_id": NAME_KID, "principal_key_id": PRINCIPAL_KID,
        "fp_key_id": FP_KID if profile != "usage" else None,
        "producer": {"name": "tokenbill", "version": "0.2.0", "adapter": "test"},
        "created_ms": T0, "attribution": Attribution(team="payments"),
    }
    h.update(kw)
    return h


def _raw_usage(u: UsageBuckets, **extra: Any) -> str:
    import json
    obj = {"input_tokens": u.uncached_input, "cache_read_input_tokens": u.cache_read,
           "cache_creation_input_tokens": u.cache_write_5m + u.cache_write_1h,
           "cache_creation": {"ephemeral_5m_input_tokens": u.cache_write_5m,
                              "ephemeral_1h_input_tokens": u.cache_write_1h},
           "output_tokens": u.output, "service_tier": "standard", **extra}
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sample(profile: str = "usage") -> Sample:
    """Sessions, lanes, three requests (retries, iterations, diagnostics, edits, raw usage,
    appended items, Copilot pricing fields, MESSAGE_START_ONLY), events of several kinds, an
    aggregate, a cost line with Copilot fields and a credit (negative nano), an outcome with extra
    counts, a data-quality note — plus fingerprints (``fingerprint`` / ``full``) and a content map
    (``full``)."""
    fp_tier = {"usage": None, "fingerprint": ContentTier.FINGERPRINT,
               "full": ContentTier.FULL}[profile]
    session_key = stable_id("ses", "sample", 1)
    lane1 = stable_id("ln", session_key, "main")
    lane2 = stable_id("ln", session_key, "helper")
    attr = Attribution(principal=PRINCIPAL, team="payments", repo=REPO, api_key_id=API_KEY,
                       agent_product="agent_sdk", workload_class="ci", billing_path="api_key",
                       extra=(("gateway", "litellm"), ("environment", "prod")))
    src = SourceRef(adapter="recorder", source_id="s_sample", locator="call:1",
                    fidelity=Fidelity.FULL, priority=50)
    content: dict[str, str] = {}
    fps: list[ContentFingerprint | None] = [None, None]
    bps: list[tuple[Breakpoint, ...]] = [(), ()]
    if fp_tier is not None:
        for i, turns in enumerate((2, 3)):
            msgs = conversation(turns)
            msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            fp, marks, cmap = fingerprint_of(msgs, fp_tier)
            fps[i], bps[i] = fp, marks
            content.update(cmap)
    u1 = UsageBuckets(uncached_input=12, cache_read=900, cache_write_1h=300, output=250,
                      output_reasoning=40)
    comp = UsageBuckets(uncached_input=5000, output=800)
    att_fail = Attempt(attempt_id="at_1a", attempt_no=0, ts_start_ms=T0, ttft_ms=None,
                       duration_ms=812, outcome="http_error", http_status=529,
                       error_type="overloaded", retry_layer="sdk", retry_after_ms=1500,
                       should_retry=True, provider_request_id="req_01", provider_message_id=None,
                       model_served=None, stop_reason=None, inferences=(), sdk_retry_count=0)
    att_ok = Attempt(
        attempt_id="at_1b", attempt_no=1, ts_start_ms=T0 + 2400, ttft_ms=640, duration_ms=5100,
        outcome="ok", http_status=200, error_type=None, retry_layer="sdk", retry_after_ms=None,
        should_retry=None, provider_request_id="req_02", provider_message_id="msg_01",
        model_served="claude-opus-5-5", stop_reason="tool_use",
        inferences=(Inference(inference_id="inf_1c", kind=InferenceKind.COMPACTION, usage=comp,
                              pricing=ctx()),
                    Inference(inference_id="inf_1m", kind="message", usage=u1,
                              pricing=ctx(write_ttl_hint=None, inference_geo="us"))),
        diagnostics=CacheDiagnostic(reason="system_changed", provider_reason="system_changed",
                                    missed_input_tokens_estimate=1200,
                                    source="anthropic.cache_diagnostics"),
        applied_edits=(("clear_tool_uses_20250919", 3400),), thinking_dropped=1,
        sdk_retry_count=1, raw_usage_json=None, convention_id="anthropic.messages")
    r1 = Request(request_id="rq_sample_1", session_key=session_key, lane_key=lane1, seq=0,
                 attribution=attr,
                 params=RequestParams(model_requested="claude-opus-5-5", max_tokens=4096,
                                      stream=True, thinking="adaptive", effort="high",
                                      tool_choice="auto", betas=("context-management-2025-06-27",),
                                      breakpoints=bps[0], automatic_caching=False,
                                      web_search_enabled=False, has_images=False),
                 attempts=(att_fail, att_ok), fingerprint=fps[0],
                 appended=(AppendedItem(kind="user_text", name=None, n_bytes=120),
                           AppendedItem(kind="tool_result", name="Bash", n_bytes=4000,
                                        is_error=True)),
                 source=src)
    u2 = UsageBuckets(cache_read=1200, cache_write_1h=410, output=90)
    r2 = Request(request_id="rq_sample_2", session_key=session_key, lane_key=lane1, seq=1,
                 attribution=attr,
                 params=RequestParams(model_requested="claude-opus-5-5", breakpoints=bps[1]),
                 attempts=(Attempt(
                     attempt_id="at_2", attempt_no=0, ts_start_ms=T0 + 60_000, ttft_ms=None,
                     duration_ms=2000, outcome="ok", http_status=None, error_type=None,
                     retry_layer=None, retry_after_ms=None, should_retry=None,
                     provider_request_id=None, provider_message_id="msg_02",
                     model_served="claude-opus-5-5", stop_reason="end_turn",
                     inferences=(Inference(inference_id="inf_2", kind="message", usage=u2,
                                           pricing=ctx()),),
                     raw_usage_json=_raw_usage(u2), convention_id="anthropic.messages"),),
                 fingerprint=fps[1], source=src)
    u3 = UsageBuckets(uncached_input=3000, output=4)
    r3 = Request(request_id="rq_sample_3", session_key=session_key, lane_key=lane2, seq=0,
                 attribution=Attribution(team="payments", billing_path="copilot_pool"),
                 params=RequestParams(model_requested="claude-sonnet-5"),
                 attempts=(Attempt(
                     attempt_id="at_3", attempt_no=0, ts_start_ms=T0 + 70_000, ttft_ms=None,
                     duration_ms=None, outcome="ok", http_status=None, error_type=None,
                     retry_layer=None, retry_after_ms=None, should_retry=None,
                     provider_request_id=None, provider_message_id=None, model_served=None,
                     stop_reason=None,
                     inferences=(Inference(
                         inference_id="inf_3", kind="message", usage=u3,
                         pricing=ctx("claude-sonnet-5", provider="github",
                                     channel="github_copilot", billing_path="copilot_pool",
                                     routing="auto", compliance="fedramp",
                                     context_tier="long_context"),
                         usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=400),),
                     raw_usage_json='{"contextTier":"long_context","tokenCount":3004,'
                                    '"tokenType":"input","totalNanoAiu":17}',
                     convention_id="github.copilot.test"),))
    lanes = (Lane(lane_key=lane1, session_key=session_key, kind=LaneKind.API_RUN,
                  parent_lane_key=None, cache_scope_key="ws:wrkspc_01", requests=(),
                  ttl_observed="1h"),
             Lane(lane_key=lane2, session_key=session_key, kind=LaneKind.HELPER,
                  parent_lane_key=lane1, cache_scope_key="unknown", requests=(),
                  lane_exact=False))
    session = Session(session_key=session_key, source_kind="recorder",
                      attribution=Attribution(principal=PRINCIPAL, team="payments"),
                      lanes=lanes, started_ms=T0, ended_ms=T0 + 72_000)
    events = [
        LaneEvent(lane_key=lane1, ts_ms=T0 + 30_000, kind="compaction",
                  attrs=(("trigger", "auto"), ("pre_tokens", 180_000), ("post_tokens", 9_000),
                         ("duration_ms", 4000), ("dropped_tokens", None))),
        LaneEvent(lane_key=lane1, ts_ms=T0 + 1000, kind="api_error",
                  attrs=(("status", 529), ("error_type", "overloaded"), ("retry_attempt", 1),
                         ("max_retries", 10), ("retry_in_ms", -1))),
        LaneEvent(lane_key=lane1, ts_ms=T0, kind="human_prompt"),
        LaneEvent(lane_key=lane2, ts_ms=T0 + 70_000, kind="quota_state",
                  attrs=(("status", "allowed"), ("using_overage", False))),
    ]
    aggregates = [UsageAggregate(
        agg_id="agg_1", source_kind="anthropic.usage_report", bucket_start_ms=T0,
        bucket_end_ms=T0 + 86_400_000,
        dims=(("model", "claude-opus-5-5"), ("workspace_id", "wrkspc_01")),
        usage=UsageBuckets(uncached_input=10, cache_read=20, output=5),
        reported_cost_nano=-5, reported_cost_basis="invoice", finality="final", fetched_ms=T0)]
    cost_lines = [CostLine(
        line_id="cl_1", source_kind="github.ai_usage_report", date_utc="2026-09-21",
        channel="github_copilot", workspace_id=None, description="Copilot AI credits",
        model="claude-sonnet-5", cost_type="ai_credit.user", token_type=None, sku="copilot_ai",
        service_tier=None, inference_geo=None, endpoint_scope=None, amount_nano=-1_250_000,
        principal=PRINCIPAL, quantity="12.5", unit="ai-credits", cost_center="cc-7",
        team="payments", repo=REPO, workload="copilot_code_review", routing="auto",
        speed="standard", pseudo=None)]
    outcomes = [OutcomeAggregate(date_utc="2026-09-21", team="payments", n_users=6, sessions=40,
                                 commits=12, pull_requests=3, lines_added=900, lines_removed=120,
                                 edits_accepted=33, edits_rejected=4,
                                 extra=(("prs_merged", 2),))]
    notes = [DataQualityNote(code="dq.message_start_only", severity="info", count=1,
                             detail="streaming placeholder output", tokens=4)]
    return Sample(header=header(profile), sessions=[session], requests=[r1, r2, r3],
                  events=events, aggregates=aggregates, cost_lines=cost_lines,
                  outcomes=outcomes, notes=notes,
                  content=content if profile == "full" else None)


# ---------------------------------------------------------------------------------------------
# fake Anthropic SDK doubles (no anthropic / httpx import anywhere)
# ---------------------------------------------------------------------------------------------


class FakeUsage:
    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


def default_usage() -> FakeUsage:
    return FakeUsage(input_tokens=100, cache_read_input_tokens=40, cache_creation_input_tokens=10,
                     cache_creation=FakeUsage(ephemeral_5m_input_tokens=10,
                                              ephemeral_1h_input_tokens=0),
                     output_tokens=7, service_tier="standard")


class FakeMessage:
    def __init__(self, usage: Any = None, *, msg_id: str = "msg_fake_1",
                 model: str = "claude-sonnet-5", stop_reason: str = "end_turn",
                 diagnostics: Any = None) -> None:
        self.id = msg_id
        self.model = model
        self.usage = usage if usage is not None else default_usage()
        self.stop_reason = stop_reason
        self.diagnostics = diagnostics
        self._request_id = "req_fake_1"


class HttpRequest:
    def __init__(self, retry: int) -> None:
        self.headers = {"x-stainless-retry-count": str(retry)}


class HttpResponse:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status_code = status
        self.headers = headers


class FakeHttp:
    """An httpx-like client: ``event_hooks`` lists and a ``send`` that performs one HTTP attempt
    per status (a non-2xx status that is not the last is retried by the "SDK")."""

    def __init__(self, *, is_async: bool = False) -> None:
        self.event_hooks: dict[str, list[Any]] = {"request": [], "response": []}
        self.is_async = is_async

    @staticmethod
    def _headers(status: int) -> dict[str, str]:
        if status == 529:
            return {"retry-after-ms": "1500", "x-should-retry": "true", "request-id": "req_x"}
        if status == 429:
            return {"retry-after": "2", "x-should-retry": "true"}
        return {"request-id": "req_ok"}

    def send(self, statuses: list[int]) -> None:
        for i, st in enumerate(statuses):
            for hook in self.event_hooks["request"]:
                hook(HttpRequest(i))
            for hook in self.event_hooks["response"]:
                hook(HttpResponse(st, self._headers(st)))

    async def asend(self, statuses: list[int]) -> None:
        for i, st in enumerate(statuses):
            for hook in self.event_hooks["request"]:
                await hook(HttpRequest(i))
            for hook in self.event_hooks["response"]:
                await hook(HttpResponse(st, self._headers(st)))


class APIStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}")
        self.status_code = status


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class FakeStream:
    def __init__(self, final: FakeMessage, events: list[Any] | None = None) -> None:
        self._final = final
        self.events = events or ["event-1", "event-2"]
        self.current_message_snapshot = FakeMessage(
            FakeUsage(input_tokens=100, cache_read_input_tokens=0,
                      cache_creation_input_tokens=0, output_tokens=3), msg_id="msg_partial")

    def __iter__(self) -> Iterator[Any]:
        return iter(self.events)

    def get_final_message(self) -> FakeMessage:
        return self._final


class FakeStreamManager:
    def __init__(self, owner: FakeMessages, stream: FakeStream) -> None:
        self._owner = owner
        self._stream = stream

    def __enter__(self) -> FakeStream:
        self._owner.http_send()
        return self._stream

    def __exit__(self, *exc: Any) -> bool:
        return False


class FakeMessages:
    def __init__(self, http: FakeHttp | None, statuses: list[int] | None = None,
                 response: FakeMessage | None = None, error: BaseException | None = None) -> None:
        self.http = http
        self.statuses = statuses or [200]
        self.response = response or FakeMessage()
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def http_send(self) -> None:
        if self.http is not None:
            self.http.send(self.statuses)
        if self.error is not None:
            raise self.error

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        self.http_send()
        if kwargs.get("stream"):
            return RawEvents(self.response)
        return self.response

    def stream(self, **kwargs: Any) -> FakeStreamManager:
        self.requests.append(kwargs)
        return FakeStreamManager(self, FakeStream(self.response))


class RawEvents:
    """A raw ``create(stream=True)`` event stream (no ``usage`` attribute)."""

    def __init__(self, final: FakeMessage, *, stop: bool = True) -> None:
        start = FakeMessage(FakeUsage(input_tokens=100, cache_read_input_tokens=40,
                                      cache_creation_input_tokens=10, output_tokens=1),
                            msg_id=final.id)
        self.events: list[Any] = [
            FakeUsage(type="message_start", message=start),
            FakeUsage(type="content_block_delta"),
            FakeUsage(type="message_delta", usage=FakeUsage(output_tokens=7),
                      delta=FakeUsage(stop_reason="end_turn")),
        ]
        if stop:
            self.events.append(FakeUsage(type="message_stop"))
        self.closed = False

    def __iter__(self) -> Iterator[Any]:
        return iter(self.events)

    def close(self) -> None:
        self.closed = True


class FakeBeta:
    def __init__(self, messages: Any) -> None:
        self.messages = messages


class FakeClient:
    """``client.messages`` / ``client.beta.messages`` sharing one httpx-like ``_client``."""

    def __init__(self, *, hooks: bool = True, statuses: list[int] | None = None,
                 response: FakeMessage | None = None, error: BaseException | None = None) -> None:
        http = FakeHttp() if hooks else None
        if hooks:
            self._client = http
        self.messages = FakeMessages(http, statuses, response, error)
        self.beta = FakeBeta(FakeMessages(http, statuses, response, error))


class FakeAsyncStream:
    def __init__(self, final: FakeMessage) -> None:
        self._final = final
        self.current_message_snapshot = FakeMessage(
            FakeUsage(input_tokens=100, output_tokens=3), msg_id="msg_partial")

    async def get_final_message(self) -> FakeMessage:
        return self._final


class FakeAsyncStreamManager:
    def __init__(self, owner: FakeAsyncMessages, stream: FakeAsyncStream) -> None:
        self._owner = owner
        self._stream = stream

    async def __aenter__(self) -> FakeAsyncStream:
        await self._owner.http_send()
        return self._stream

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeAsyncMessages:
    def __init__(self, http: FakeHttp | None, statuses: list[int] | None = None,
                 response: FakeMessage | None = None, error: BaseException | None = None) -> None:
        self.http = http
        self.statuses = statuses or [200]
        self.response = response or FakeMessage()
        self.error = error
        self.requests: list[dict[str, Any]] = []

    async def http_send(self) -> None:
        if self.http is not None:
            await self.http.asend(self.statuses)
        if self.error is not None:
            raise self.error

    async def create(self, **kwargs: Any) -> FakeMessage:
        self.requests.append(kwargs)
        await self.http_send()
        return self.response

    def stream(self, **kwargs: Any) -> FakeAsyncStreamManager:
        self.requests.append(kwargs)
        return FakeAsyncStreamManager(self, FakeAsyncStream(self.response))


class FakeAsyncClient:
    def __init__(self, *, hooks: bool = True, statuses: list[int] | None = None,
                 error: BaseException | None = None) -> None:
        http = FakeHttp(is_async=True) if hooks else None
        if hooks:
            self._client = http
        self.messages = FakeAsyncMessages(http, statuses, None, error)
        self.beta = FakeBeta(FakeAsyncMessages(http, statuses, None, error))

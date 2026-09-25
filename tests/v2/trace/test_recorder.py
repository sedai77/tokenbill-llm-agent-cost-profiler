"""Recorder v2 (SPEC §5.7, D13, D35): trace@2 mode against fake SDK doubles. The v0.1 path is
covered by the frozen ``tests/test_instrument.py``; here only its dispatch is re-checked."""

from __future__ import annotations

import asyncio
import copy
import json
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.v2.trace.helpers import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    FakeAsyncClient,
    FakeClient,
    FakeMessage,
    FakeUsage,
    RawEvents,
    conversation,
    tools,
)
from tokenbill import instrument as I
from tokenbill.adapters.fingerprint import fingerprint_request
from tokenbill.adapters.trace_v2 import TraceV2Adapter, TraceV2Writer, iter_trace_v2
from tokenbill.common import TokenbillError
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.keys import load
from tokenbill.core.records import ContentTier, InferenceKind, Outcome, UsageSource
from tokenbill.core.testing import assert_adapter_conforms, conformance_ingest_options
from tokenbill.core.types import DataQualityNote

SYSTEM = "You are terse. " + CANARY


def _rec(tmp_path: Path, name: str = "rec.jsonl", **kw: Any) -> I.Recorder:
    kw.setdefault("format", "trace@2")
    if kw.get("content", "none") != "none":
        kw.setdefault("key_file", tmp_path / "key")
    return I.Recorder(tmp_path / name, run_id="run-test", **kw)


def _read(path: Path) -> Any:
    return TraceV2Adapter().read(path, conformance_ingest_options())


def _call(client: Any, **kw: Any) -> Any:
    kw.setdefault("model", "claude-sonnet-5")
    kw.setdefault("system", SYSTEM)
    kw.setdefault("messages", [{"role": "user", "content": "hi " + CANARY}])
    kw.setdefault("max_tokens", 64)
    return client.messages.create(**kw)


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


# ---------------------------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------------------------


def test_sdk_retries_become_separate_attempts(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient(statuses=[529, 529, 200]))
    response = _call(client)
    assert response is client.messages.response
    rec.close()
    (req,) = _read(rec.path).requests
    assert [a.http_status for a in req.attempts] == [529, 529, 200]
    assert [a.sdk_retry_count for a in req.attempts] == [0, 1, 2]
    assert [a.retry_after_ms for a in req.attempts] == [1500, 1500, None]
    assert [a.should_retry for a in req.attempts] == [True, True, None]
    assert [a.outcome for a in req.attempts] == [Outcome.HTTP_ERROR, Outcome.HTTP_ERROR,
                                                 Outcome.OK]
    assert [a.error_type for a in req.attempts] == ["overloaded", "overloaded", None]
    assert all(a.retry_layer == "sdk" for a in req.attempts)
    assert [len(a.inferences) for a in req.attempts] == [0, 0, 1]
    ok = req.final_attempt
    assert ok.provider_message_id == "msg_fake_1" and ok.provider_request_id == "req_ok"
    assert ok.convention_id == "anthropic.messages"
    assert json.loads(ok.raw_usage_json)["cache_creation"] == {
        "ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 0}
    (inf,) = ok.inferences
    assert (inf.usage.uncached_input, inf.usage.cache_read, inf.usage.cache_write_5m,
            inf.usage.output) == (100, 40, 10, 7)
    assert inf.pricing.model == "claude-sonnet-5" and inf.pricing.billing_path == "api_key"
    assert [a.ts_start_ms for a in req.attempts] == sorted(a.ts_start_ms for a in req.attempts)
    notes = [n.code for n in _read(rec.path).notes]
    assert "dq.sdk_retries_invisible" not in notes
    assert rec.dropped == 0


def test_retry_after_seconds_header(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    _call(rec.wrap(FakeClient(statuses=[429, 200])))
    rec.close()
    (req,) = _read(rec.path).requests
    assert [a.retry_after_ms for a in req.attempts] == [2000, None]
    assert req.attempts[0].error_type == "rate_limit"


def test_client_without_hooks_is_one_attempt_and_retries_invisible(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    _call(rec.wrap(FakeClient(hooks=False)))
    rec.close()
    lines = _lines(rec.path)
    assert lines[0]["rec"] == "header"
    assert lines[1]["rec"] == "dq" and lines[1]["code"] == "dq.sdk_retries_invisible"
    result = _read(rec.path)
    (req,) = result.requests
    (att,) = req.attempts
    assert att.retry_layer is None and att.sdk_retry_count is None and att.outcome is Outcome.OK
    assert [n.code for n in result.notes] == ["dq.sdk_retries_invisible"]


def test_http_hooks_opt_out_is_invisible_too(tmp_path: Path) -> None:
    rec = _rec(tmp_path, http_hooks=False)
    client = FakeClient(statuses=[529, 200])
    _call(rec.wrap(client))
    rec.close()
    assert client._client.event_hooks == {"request": [], "response": []}
    (req,) = _read(rec.path).requests
    assert len(req.attempts) == 1
    assert _lines(rec.path)[1]["code"] == "dq.sdk_retries_invisible"


def test_exception_yields_an_http_error_attempt_without_usage(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient(hooks=False, error=APIStatusError(500)))
    with pytest.raises(APIStatusError):
        _call(client)
    rec.close()
    (req,) = _read(rec.path).requests
    (att,) = req.attempts
    assert (att.outcome, att.http_status, att.error_type) == (Outcome.HTTP_ERROR, 500,
                                                              "api_error")
    assert att.inferences == () and att.raw_usage_json is None


def test_exception_after_hooked_retries(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient(statuses=[529, 529, 529], error=APIStatusError(529)))
    with pytest.raises(APIStatusError):
        _call(client)
    rec.close()
    (req,) = _read(rec.path).requests
    assert [(a.outcome, a.http_status) for a in req.attempts] == [(Outcome.HTTP_ERROR, 529)] * 3
    assert all(not a.inferences for a in req.attempts)


@pytest.mark.parametrize(("exc", "outcome", "etype"), [
    (APITimeoutError(), Outcome.TIMEOUT, "timeout"),
    (APIConnectionError(), Outcome.NETWORK_ERROR, "connection"),
    (RuntimeError("boom " + CANARY), Outcome.HTTP_ERROR, None),
    (KeyboardInterrupt(), Outcome.ABORTED, None),
])
def test_exception_outcomes(tmp_path: Path, exc: BaseException, outcome: Outcome,
                            etype: str | None) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient(hooks=False, error=exc))
    with pytest.raises(type(exc)):
        _call(client)
    rec.close()
    (req,) = _read(rec.path).requests
    assert (req.final_attempt.outcome, req.final_attempt.error_type) == (outcome, etype)
    assert_no_canary(rec.path.read_bytes())


def test_connection_error_during_a_hooked_retry(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = FakeClient(statuses=[529], error=APIConnectionError())
    rec.wrap(client)

    def send(statuses: list[int]) -> None:        # the second attempt never gets a response
        http = client._client
        for hook in http.event_hooks["request"]:
            hook(type("R", (), {"headers": {"x-stainless-retry-count": "0"}})())
        for hook in http.event_hooks["response"]:
            hook(type("S", (), {"status_code": 529, "headers": {}})())
        for hook in http.event_hooks["request"]:
            hook(type("R", (), {"headers": {"x-stainless-retry-count": "1"}})())

    client.messages.http.send = send  # type: ignore[method-assign]
    with pytest.raises(APIConnectionError):
        _call(client)
    rec.close()
    (req,) = _read(rec.path).requests
    assert [(a.outcome, a.http_status, a.sdk_retry_count) for a in req.attempts] == [
        (Outcome.HTTP_ERROR, 529, 0), (Outcome.NETWORK_ERROR, None, 1)]


def test_aborted_stream_is_partial(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient())
    with pytest.raises(RuntimeError, match="user stopped"):
        with client.messages.stream(model="claude-sonnet-5", messages=[
                {"role": "user", "content": "go"}]) as stream:
            next(iter(stream))
            raise RuntimeError("user stopped")
    rec.close()
    (req,) = _read(rec.path).requests
    att = req.final_attempt
    assert att.outcome is Outcome.ABORTED and att.provider_message_id == "msg_partial"
    (inf,) = att.inferences
    assert inf.usage_source is UsageSource.PARTIAL_STREAM
    assert (inf.usage.uncached_input, inf.usage.output) == (100, 3)
    assert req.params.stream is True


def test_stream_records_final_message_and_get_final_failure(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient(statuses=[529, 200]))
    with client.messages.stream(model="claude-sonnet-5", messages=[
            {"role": "user", "content": "go"}]) as stream:
        assert list(stream) == ["event-1", "event-2"]
    boom = FakeClient()

    def fail() -> Any:
        raise RuntimeError("stream broke")

    rec.wrap(boom)
    with pytest.raises(RuntimeError, match="stream broke"):
        with boom.messages.stream(model="claude-sonnet-5", messages=[]) as s2:
            s2.get_final_message = fail
    rec.close()
    first, second = _read(rec.path).requests
    assert [a.http_status for a in first.attempts] == [529, 200]
    assert first.final_attempt.outcome is Outcome.OK
    assert first.final_attempt.inferences[0].usage_source is UsageSource.FINAL
    assert second.final_attempt.outcome is Outcome.ABORTED


def test_stream_enter_failure_records_the_error(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient(statuses=[529, 529], error=APIStatusError(529)))
    with pytest.raises(APIStatusError):
        with client.messages.stream(model="claude-sonnet-5", messages=[]):
            pass
    rec.close()
    (req,) = _read(rec.path).requests
    assert [a.http_status for a in req.attempts] == [529, 529]


def test_beta_messages_create_and_stream_are_recorded(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient())
    client.beta.messages.create(model="claude-opus-5-5", messages=[], max_tokens=5,
                                betas=["context-management-2025-06-27"])
    with client.beta.messages.stream(model="claude-opus-5-5", messages=[]) as s:
        list(s)
    rec.close()
    first, second = _read(rec.path).requests
    assert first.params.betas == ("context-management-2025-06-27",)
    assert second.params.stream is True
    assert [r.seq for r in (first, second)] == [0, 1]


def test_payload_mutated_after_the_call_is_recorded_as_sent(tmp_path: Path) -> None:
    rec = _rec(tmp_path, content="fingerprint")
    client = rec.wrap(FakeClient())
    msgs = conversation(2)
    sent = copy.deepcopy(msgs)
    t = tools()
    _call(client, messages=msgs, tools=t)
    msgs[0]["content"] = "rewritten after the call"
    msgs.append({"role": "user", "content": "late"})
    t.clear()
    rec.close()
    (req,) = _read(rec.path).requests
    key = load(tmp_path / "key")
    expected, _, _ = fingerprint_request(tools=tools(), system=SYSTEM, messages=sent, key=key,
                                         tier=ContentTier.FINGERPRINT)
    assert req.fingerprint == expected
    assert req.fingerprint.key_id == key_id(key)
    assert_no_canary(rec.path.read_bytes())


def test_async_client_with_async_hooks(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeAsyncClient(statuses=[529, 200]))

    async def scenario() -> None:
        await client.messages.create(model="claude-sonnet-5", messages=[], max_tokens=5)
        async with client.messages.stream(model="claude-sonnet-5", messages=[]) as s:
            await s.get_final_message()
        await client.beta.messages.create(model="claude-sonnet-5", messages=[], max_tokens=5)
        with pytest.raises(RuntimeError):
            async with client.messages.stream(model="claude-sonnet-5", messages=[]):
                raise RuntimeError("abort")

    asyncio.run(scenario())
    rec.close()
    reqs = _read(rec.path).requests
    assert len(reqs) == 4
    for req in reqs[:3]:
        assert [a.http_status for a in req.attempts] == [529, 200]
        assert [a.sdk_retry_count for a in req.attempts] == [0, 1]
    assert reqs[3].final_attempt.inferences[0].usage_source is UsageSource.PARTIAL_STREAM
    assert all(callable(h) and asyncio.iscoroutinefunction(h)
               for h in client._client.event_hooks["request"])


def test_async_client_exception_and_stream_enter_failure(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeAsyncClient(hooks=False, error=APIStatusError(400)))

    async def scenario() -> None:
        with pytest.raises(APIStatusError):
            await client.messages.create(model="claude-sonnet-5", messages=[])
        with pytest.raises(APIStatusError):
            async with client.messages.stream(model="claude-sonnet-5", messages=[]):
                pass

    asyncio.run(scenario())
    rec.close()
    reqs = _read(rec.path).requests
    assert [(r.final_attempt.outcome, r.final_attempt.error_type) for r in reqs] == [
        (Outcome.HTTP_ERROR, "invalid_request")] * 2


def test_sync_looking_async_create_is_awaited_and_recorded(tmp_path: Path) -> None:
    class Opaque:
        def __init__(self) -> None:
            self.inner = FakeAsyncClient(hooks=False).messages

        def create(self, **kw: Any) -> Any:
            return self.inner.create(**kw)

    class Client:
        messages = Opaque()

    rec = _rec(tmp_path)
    client = rec.wrap(Client())
    response = asyncio.run(client.messages.create(model="claude-sonnet-5", messages=[]))
    assert isinstance(response, FakeMessage)
    failing = Opaque()
    failing.inner = FakeAsyncClient(hooks=False, error=APIStatusError(503)).messages

    class Client2:
        messages = failing

    client2 = rec.wrap(Client2())
    with pytest.raises(APIStatusError):
        asyncio.run(client2.messages.create(model="claude-sonnet-5", messages=[]))
    rec.close()
    ok, bad = _read(rec.path).requests
    assert ok.final_attempt.outcome is Outcome.OK
    assert bad.final_attempt.error_type == "overloaded"


def test_failing_writer_never_raises_and_counts_dropped(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: TraceV2Writer, r: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(TraceV2Writer, "request", broken)
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient())
    for _ in range(3):
        assert _call(client) is client.messages.response
    rec.close()
    assert rec.dropped == 3


def test_unwritable_path_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    rec = I.Recorder(blocker / "sub" / "t.jsonl", format="trace@2")
    client = rec.wrap(FakeClient())
    _call(client)
    rec.close()
    assert rec.dropped >= 1


def test_bounded_queue_drops_instead_of_blocking(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    original = I._V2Recorder._handle

    def slow(self: Any, item: Any) -> None:
        gate.wait(5)
        original(self, item)

    monkeypatch.setattr(I._V2Recorder, "_handle", slow)
    rec = _rec(tmp_path, queue_size=2)
    client = rec.wrap(FakeClient())
    start = time.monotonic()
    for _ in range(10):
        _call(client)
    assert time.monotonic() - start < 2          # never blocked on the full queue
    assert rec.dropped >= 6
    gate.set()
    rec.close()
    assert len(_read(rec.path).requests) == 10 - rec.dropped


def test_diagnostics_adds_the_beta_header_and_previous_message_id(tmp_path: Path) -> None:
    rec = _rec(tmp_path, diagnostics=True, lane="agent-a")
    fake = FakeClient()
    client = rec.wrap(fake)
    _call(client, extra_headers={"anthropic-beta": "context-management-2025-06-27"})
    _call(client)
    client.beta.messages.create(model="claude-sonnet-5", messages=[], betas=["x-1"])
    rec.close()
    first, second = fake.messages.requests
    assert first["extra_headers"]["anthropic-beta"] == \
        "context-management-2025-06-27," + I.DIAGNOSTICS_BETA
    assert "extra_body" not in first
    assert second["extra_headers"]["anthropic-beta"] == I.DIAGNOSTICS_BETA
    assert second["extra_body"] == {"diagnostics": {"previous_message_id": "msg_fake_1"}}
    (beta,) = fake.beta.messages.requests
    assert beta["betas"] == ["x-1", I.DIAGNOSTICS_BETA]
    reqs = _read(rec.path).requests
    assert all(I.DIAGNOSTICS_BETA in r.params.betas for r in reqs)
    assert len({r.lane_key for r in reqs}) == 1


def test_diagnostics_response_is_recorded(tmp_path: Path) -> None:
    diag = FakeUsage(cache_miss_reason=FakeUsage(type="tools_changed",
                                                 cache_missed_input_tokens=900))
    rec = _rec(tmp_path, diagnostics=True)
    _call(rec.wrap(FakeClient(response=FakeMessage(diagnostics=diag))))
    rec.close()
    d = _read(rec.path).requests[0].final_attempt.diagnostics
    assert (d.reason, d.provider_reason, d.missed_input_tokens_estimate) == (
        "tools_changed", "tools_changed", 900)


def test_diagnostics_are_first_party_only(tmp_path: Path) -> None:
    class AnthropicBedrock(FakeClient):
        pass

    rec = _rec(tmp_path, diagnostics=True)
    fake = AnthropicBedrock()
    _call(rec.wrap(fake))
    _call(fake)
    rec.close()
    assert all("extra_headers" not in r for r in fake.messages.requests)
    reqs = _read(rec.path).requests
    assert {r.serving_inference.pricing.channel for r in reqs} == {"bedrock"}
    assert {r.attribution.billing_path for r in reqs} == {"bedrock"}


def test_300_call_session_is_under_2_percent_of_trace1(tmp_path: Path) -> None:
    """A 300-call session growing to a ~150k-token context (~600k characters)."""
    t1 = I.Recorder(tmp_path / "t1.jsonl", run_id="r")
    rec2 = _rec(tmp_path, "t2.jsonl", content="fingerprint")
    c1, c2 = t1.wrap(FakeClient(hooks=False)), rec2.wrap(FakeClient())
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "Refactor the billing module."}]
    for i in range(300):
        for c in (c1, c2):
            _call(c, messages=msgs, tools=tools(), system=SYSTEM)
        msgs = msgs + [
            {"role": "assistant", "content": [
                {"type": "text", "text": f"Reading part {i}. " + "a" * 300},
                {"type": "tool_use", "id": f"toolu_{i}", "name": "read_file",
                 "input": {"path": f"billing/{i}.py"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{i}",
                 "content": f"def f_{i}():\n" + "    return 1\n" * 110}]}]
    rec2.close()
    size1 = (tmp_path / "t1.jsonl").stat().st_size
    size2 = rec2.path.stat().st_size
    assert size1 > 150_000 * 4 * 300 // 2          # the full payload is re-sent every call
    assert size2 < 0.02 * size1
    result = _read(rec2.path)
    assert len(result.requests) == 300 and result.quarantined == []
    last = result.requests[-1].fingerprint
    assert last is not None and len(last.blocks) == 2 + 1 + 1 + 3 * 299


# ---------------------------------------------------------------------------------------------
# tiers, params, lifecycle
# ---------------------------------------------------------------------------------------------


def test_content_tier_none_writes_a_usage_profile_without_a_key(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch
                                                                ) -> None:
    def no_key(*a: Any, **k: Any) -> bytes:
        raise AssertionError("tier none needs no key")

    monkeypatch.setattr(I, "load_or_create", no_key)
    rec = _rec(tmp_path)
    msgs = conversation(1)
    msgs[-1]["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    _call(rec.wrap(FakeClient()), messages=msgs, tools=tools(),
          cache_control={"type": "ephemeral"})
    rec.close()
    header = _lines(rec.path)[0]
    assert header["profile"] == "usage" and header["fp_key_id"] is None
    assert header["producer"]["adapter"] == "recorder"
    (req,) = _read(rec.path).requests
    assert req.fingerprint is None
    assert [(b.block_index, b.ttl) for b in req.params.breakpoints] == [(6, "1h")]
    assert req.params.automatic_caching is True
    assert_no_canary(rec.path.read_bytes())


def test_fingerprint_and_full_tiers(tmp_path: Path) -> None:
    for tier in ("fingerprint", "full"):
        rec = _rec(tmp_path, f"{tier}.jsonl", content=tier)
        client = rec.wrap(FakeClient())
        _call(client, messages=conversation(1), tools=tools())
        _call(client, messages=conversation(2), tools=tools())
        rec.close()
        items = list(iter_trace_v2(rec.path))
        header = items[0]
        assert header.profile == tier and header.fp_key_id == key_id(load(tmp_path / "key"))
        contents = [i for i in items if type(i).__name__ == "ContentItem"]
        if tier == "full":
            hashes = {b.h for r in _read(rec.path).requests for b in r.fingerprint.blocks}
            assert {c.h for c in contents} == hashes
            assert len(contents) == len(hashes)                  # each block written once
            assert CANARY.encode() in rec.path.read_bytes()
        else:
            assert contents == []
            assert_no_canary(rec.path.read_bytes())
    assert stat.S_IMODE((tmp_path / "key").stat().st_mode) == 0o600


def test_request_params_are_recorded_content_free(tmp_path: Path) -> None:
    rec = _rec(tmp_path, content="fingerprint")
    t = tools() + [{"type": "web_search_20250305", "name": "web_search"}]
    image = {"type": "image", "source": {"type": "url", "url": "https://x/" + CANARY}}
    _call(rec.wrap(FakeClient()), tools=t, max_tokens=2048, stream=False,
          thinking={"type": "enabled", "budget_tokens": 1024},
          output_config={"effort": "high", "format": {"type": "json_schema", "x": CANARY}},
          tool_choice={"type": "tool", "name": "read_file", "disable_parallel_tool_use": True},
          speed="fast", service_tier="auto", inference_geo="us",
          context_management={"edits": [CANARY]},
          messages=[{"role": "user", "content": [image, {"type": "text", "text": "?"}]}])
    rec.close()
    p = _read(rec.path).requests[0].params
    assert (p.model_requested, p.max_tokens, p.stream, p.thinking, p.effort) == (
        "claude-sonnet-5", 2048, False, "enabled:1024", "high")
    assert p.tool_choice.startswith("tool:") and len(p.tool_choice) == 25
    assert p.disable_parallel_tool_use is True
    assert (p.speed, p.service_tier_requested, p.inference_geo_requested) == (
        "fast", "auto", "us")
    assert len(p.output_format) == 20 and len(p.context_management) == 20
    assert p.web_search_enabled is True and p.has_images is True
    assert p.automatic_caching is False
    assert_no_canary(rec.path.read_bytes())


def test_params_without_a_key_and_odd_values(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient())
    _call(client, thinking={"type": "adaptive"}, tool_choice={"type": "tool", "name": "x"},
          output_format={"type": "json"}, max_tokens="lots", model="bad model id!")
    _call(client, thinking={"type": "disabled"}, tool_choice={"type": "any"}, effort="low",
          thinking_extra=1)
    _call(client, thinking={"type": "enabled"})
    rec.close()
    a, b, c = (r.params for r in _read(rec.path).requests)
    assert (a.thinking, a.tool_choice, a.output_format, a.max_tokens, a.model_requested) == (
        "adaptive", "tool", "set", None, "")
    assert (b.thinking, b.tool_choice, b.effort) == ("off", "any", "low")
    assert c.thinking == "enabled"


def test_appended_items_summarize_new_messages(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient())
    msgs = conversation(1)
    _call(client, messages=msgs[:1])
    msgs[2]["content"][0]["is_error"] = True
    msgs[2]["content"][0]["content"] = [{"type": "text", "text": "x"}, {"type": "image"}]
    _call(client, messages=msgs + [{"role": "user", "content": [
        {"type": "document", "source": {}}]}])
    _call(client, messages=msgs[:1])                 # context shrank: everything is new
    rec.close()
    first, second, third = _read(rec.path).requests
    assert [a.kind for a in first.appended] == ["user_text"]
    assert [(a.kind, a.is_error, a.images) for a in second.appended] == [
        ("assistant", False, 0), ("assistant", False, 0), ("tool_result", True, 1),
        ("attachment", False, 0)]
    assert all(a.name is None and a.n_bytes > 0 for a in second.appended)
    assert [a.kind for a in third.appended] == ["user_text"]


def test_raw_stream_create_is_recorded(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient())
    events = list(_call(client, stream=True))
    assert len(events) == 4
    partial = _call(client, stream=True)
    with partial:
        next(iter(partial))
    closed = _call(client, stream=True)
    closed.close()
    assert closed.closed is True                     # delegated to the SDK stream
    no_stop = rec.wrap(FakeClient())
    no_stop.messages.create = no_stop.messages.create  # already wrapped
    rec.close()
    reqs = _read(rec.path).requests
    assert len(reqs) == 3
    final, part, shut = reqs
    (inf,) = final.final_attempt.inferences
    assert inf.usage_source is UsageSource.FINAL
    assert (inf.usage.uncached_input, inf.usage.output) == (100, 7)
    assert final.final_attempt.stop_reason == "end_turn"
    assert part.final_attempt.inferences[0].usage_source is UsageSource.PARTIAL_STREAM
    assert shut.final_attempt.outcome is Outcome.ABORTED
    assert shut.final_attempt.inferences == ()        # closed before any event


def test_raw_stream_without_message_stop_is_partial(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    fake = FakeClient()
    fake.messages.create = lambda **kw: RawEvents(FakeMessage(), stop=False)
    client = rec.wrap(fake)
    assert len(list(client.messages.create(model="m", messages=[], stream=True))) == 3
    rec.close()
    (req,) = _read(rec.path).requests
    assert req.final_attempt.inferences[0].usage_source is UsageSource.PARTIAL_STREAM


def test_async_raw_stream(tmp_path: Path) -> None:
    class AsyncEvents:
        def __init__(self) -> None:
            self._events = RawEvents(FakeMessage()).events
            self.closed = False

        def __aiter__(self) -> Any:
            return self._gen()

        async def _gen(self) -> Any:
            for e in self._events:
                yield e

        async def close(self) -> None:
            self.closed = True

    class Msgs:
        async def create(self, **kw: Any) -> Any:
            return AsyncEvents()

    class Client:
        messages = Msgs()

    rec = _rec(tmp_path)
    client = rec.wrap(Client())

    async def scenario() -> None:
        stream = await client.messages.create(model="m", messages=[], stream=True)
        assert len([e async for e in stream]) == 4
        other = await client.messages.create(model="m", messages=[], stream=True)
        async with other:
            pass
        third = await client.messages.create(model="m", messages=[], stream=True)
        await third.aclose()

    asyncio.run(scenario())
    rec.close()
    reqs = _read(rec.path).requests
    assert [r.final_attempt.outcome for r in reqs] == [Outcome.OK, Outcome.ABORTED,
                                                       Outcome.ABORTED]


def test_response_without_usage_is_recorded_without_inferences(tmp_path: Path) -> None:
    class NoUsage:
        usage = None
        id = "msg_x"

    rec = _rec(tmp_path)
    fake = FakeClient(hooks=False)
    fake.messages.create = lambda **kw: NoUsage()
    _call(rec.wrap(fake))
    rec.close()
    (req,) = _read(rec.path).requests
    assert req.final_attempt.inferences == ()


def test_usage_objects_with_model_dump_and_iterations(tmp_path: Path) -> None:
    class Usage:
        def model_dump(self) -> dict[str, Any]:
            return {"input_tokens": 5, "output_tokens": 9, "service_tier": "priority",
                    "inference_geo": "not_available", "speed": "fast", "note": CANARY,
                    "iterations": [{"type": "compaction", "input_tokens": 700,
                                    "output_tokens": 50},
                                   {"type": "message", "input_tokens": 5, "output_tokens": 9}]}

    rec = _rec(tmp_path)
    _call(rec.wrap(FakeClient(response=FakeMessage(usage=Usage()))))
    rec.close()
    att = _read(rec.path).requests[0].final_attempt
    assert [i.kind for i in att.inferences] == [InferenceKind.COMPACTION, InferenceKind.MESSAGE]
    ctx = att.inferences[1].pricing
    assert (ctx.service_tier, ctx.speed, ctx.inference_geo) == ("priority", "fast", None)
    assert json.loads(att.raw_usage_json)["note"] is None
    assert_no_canary(rec.path.read_bytes())


def test_bad_usage_keeps_the_attempt(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    _call(rec.wrap(FakeClient(response=FakeMessage(usage={"input_tokens": 5,
                                                          "iterations": [5]}))))
    rec.close()
    att = _read(rec.path).requests[0].final_attempt
    assert att.inferences == () and att.raw_usage_json is not None


def test_recording_context_manager_closes(tmp_path: Path) -> None:
    path = tmp_path / "cm.jsonl"
    with I.recording(path, run_id="cm", format="trace@2") as rec:
        _call(rec.wrap(FakeClient()))
    lines = _lines(path)
    assert lines[-1]["rec"] == "session" and lines[-1]["source_kind"] == "recorder"
    (session,) = _read(path).sessions
    assert len(session.lanes) == 1
    assert rec.dropped == 0


def test_close_is_idempotent_and_later_calls_pass_through(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient())
    rec.close()
    rec.close()
    assert _call(client) is client.messages.response
    assert rec.dropped == 1
    assert [x["rec"] for x in _lines(rec.path)] == ["header"]


def test_trace2_options_and_paths_are_validated(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        I.Recorder(tmp_path / "a.jsonl", format="trace@3")
    for kw in ({"content": "fingerprint"}, {"diagnostics": True}, {"lane": "x"},
               {"key_file": tmp_path / "k"}):
        with pytest.raises(UsageError):
            I.Recorder(tmp_path / "a.jsonl", **kw)
    with pytest.raises(UsageError):
        I.Recorder(tmp_path / "a.jsonl", format="trace@2", content="everything")
    with pytest.raises(UsageError):
        I.Recorder(tmp_path / "a.jsonl", format="trace@2", queue_size=0)
    existing = tmp_path / "exists.jsonl"
    existing.write_text("{}\n")
    with pytest.raises(UsageError):
        I.Recorder(existing, format="trace@2")
    with pytest.raises(TokenbillError, match="messages.create"):
        I.Recorder(tmp_path / "b.jsonl", format="trace@2").wrap(object())
    v1 = I.Recorder(tmp_path / "v1.jsonl")
    assert v1.dropped == 0 and v1.format == "trace@1"
    v1.close()
    assert not (tmp_path / "v1.jsonl").exists()


def test_gzip_output_and_conformance(tmp_path: Path) -> None:
    rec = _rec(tmp_path, "rec.jsonl.gz", content="fingerprint")
    client = rec.wrap(FakeClient(statuses=[529, 200]))
    _call(client, messages=conversation(1), tools=tools())
    _call(client, messages=conversation(2), tools=tools())
    rec.close()
    result = assert_adapter_conforms(
        TraceV2Adapter(), rec.path,
        expect_capabilities={"usage_sequence", "timing", "params", "blocks", "attempts",
                             "appended", "lanes_exact", "ttl_split"})
    assert len(result.requests) == 2


def test_hooks_ignore_requests_outside_wrapped_calls(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    fake = FakeClient()
    rec.wrap(fake)
    rec.wrap(fake)                                   # hooks attached once
    assert len(fake._client.event_hooks["request"]) == 1
    fake._client.send([200])                         # e.g. models.list(): not a wrapped call
    other = _rec(tmp_path, "other.jsonl")
    other.wrap(FakeClient())
    rec.close()
    other.close()
    assert _read(rec.path).requests == []


def test_broken_hook_inputs_never_raise(tmp_path: Path) -> None:
    rec = _rec(tmp_path)
    v2 = rec._v2
    assert v2 is not None
    v2._on_request(object())
    v2._on_response(object())
    token = I._CALL.set(v2._begin({"model": "m"}, "anthropic_api", "api_key", stream=False))
    try:
        v2._on_response(object())                    # no attempt yet
        v2._on_request(type("R", (), {"headers": None})())
        v2._on_response(type("S", (), {"status_code": "x", "headers": 5})())
        v2._on_response(type("S", (), {"status_code": 200, "headers": {}})())   # already done
    finally:
        I._CALL.reset(token)
    assert I._retry_after_ms({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert I._retry_after_ms({"Retry-After-Ms": "250.4"}) == 250
    assert I._should_retry({"x-should-retry": "maybe"}) is None
    assert I._should_retry({"x-should-retry": "false"}) is False
    assert I._hget(None, "a") is None and I._int_or_none("12") == 12
    assert I._int_or_none("99999999999999999999") is None
    rec.close()


def test_begin_failure_is_counted_not_raised(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise ValueError("snapshot failed")

    monkeypatch.setattr(I, "_request_params", boom)
    rec = _rec(tmp_path)
    client = rec.wrap(FakeClient())
    assert _call(client) is client.messages.response
    with client.messages.stream(model="m", messages=[]) as s:
        list(s)
    rec.close()
    assert rec.dropped == 2


def test_note_items_and_dropped_warning_once(tmp_path: Path, caplog: pytest.LogCaptureFixture
                                             ) -> None:
    rec = _rec(tmp_path)
    v2 = rec._v2
    v2._enqueue(("note", DataQualityNote(code="dq.x", severity="info", count=1, detail="d")))
    with caplog.at_level("WARNING", logger="tokenbill.instrument"):
        v2._drop()
        v2._drop()
    rec.close()
    assert [m for m in caplog.messages if "dropped" in m].__len__() == 1
    assert rec.dropped == 2

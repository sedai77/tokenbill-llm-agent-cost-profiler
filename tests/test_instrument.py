"""instrument.py: duck-typed recording against a tiny fake client double.

Never touches the real Anthropic SDK — the fakes below are the whole client.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tokenbill.common import TokenbillError
from tokenbill.instrument import SCHEMA, Recorder, recording

# --- fake client double (SDK-shaped, five moving parts) ---------------------


class _FakeUsage:
    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _FakeMessage:
    def __init__(self, usage: Any = None, stop_reason: str = "end_turn") -> None:
        self.usage = usage
        self.stop_reason = stop_reason


class _FakeStream:
    def __init__(self, final: _FakeMessage) -> None:
        self._final = final

    def __iter__(self) -> Any:
        return iter(["event-1", "event-2"])

    def get_final_message(self) -> _FakeMessage:
        return self._final


class _FakeStreamManager:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    def __enter__(self) -> _FakeStream:
        return self._stream

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeMessages:
    def __init__(self, response: _FakeMessage) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.requests.append(kwargs)
        return self.response

    def stream(self, **kwargs: Any) -> _FakeStreamManager:
        self.requests.append(kwargs)
        return _FakeStreamManager(_FakeStream(self.response))


class _FakeClient:
    def __init__(self, response: _FakeMessage | None = None) -> None:
        self.messages = _FakeMessages(
            response
            or _FakeMessage(
                _FakeUsage(
                    input_tokens=100,
                    cache_read_input_tokens=40,
                    cache_creation_input_tokens=10,
                    output_tokens=7,
                )
            )
        )


# --- async fake client double (AsyncAnthropic-shaped) ------------------------


class _FakeAsyncStream:
    def __init__(self, final: _FakeMessage) -> None:
        self._final = final

    async def get_final_message(self) -> _FakeMessage:
        return self._final


class _FakeAsyncStreamManager:
    def __init__(self, stream: _FakeAsyncStream) -> None:
        self._stream = stream

    async def __aenter__(self) -> _FakeAsyncStream:
        return self._stream

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeAsyncMessages:
    def __init__(self, response: _FakeMessage) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeMessage:
        self.requests.append(kwargs)
        return self.response

    def stream(self, **kwargs: Any) -> _FakeAsyncStreamManager:
        self.requests.append(kwargs)
        return _FakeAsyncStreamManager(_FakeAsyncStream(self.response))


class _FakeAsyncClient:
    def __init__(self) -> None:
        self.messages = _FakeAsyncMessages(
            _FakeMessage(
                _FakeUsage(
                    input_tokens=100,
                    cache_read_input_tokens=40,
                    cache_creation_input_tokens=10,
                    output_tokens=7,
                )
            )
        )


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- tests ------------------------------------------------------------------


def test_wrap_rejects_object_without_messages_create(tmp_path: Path) -> None:
    class Empty:
        pass

    class NotCallable:
        class messages:  # deliberately SDK-shaped attribute name
            create = "not-a-function"

    recorder = Recorder(tmp_path / "t.jsonl")
    for bad in (Empty(), NotCallable(), object()):
        with pytest.raises(TokenbillError, match="messages.create"):
            recorder.wrap(bad)


def test_create_records_one_line_with_request_and_usage(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    recorder = Recorder(path, run_id="run-x")
    client = recorder.wrap(_FakeClient())

    messages = [{"role": "user", "content": "hello"}]
    tools = [{"name": "read_file", "input_schema": {"type": "object"}}]
    response = client.messages.create(
        model="claude-sonnet-5",
        system="be terse",
        tools=tools,
        messages=messages,
        max_tokens=64,
    )
    assert response is client.messages.response  # response passes through untouched

    (record,) = _lines(path)
    assert record["schema"] == SCHEMA
    assert record["run_id"] == "run-x"
    assert record["index"] == 0
    assert isinstance(record["ts"], float) and record["ts"] > 0
    assert record["model"] == "claude-sonnet-5"
    assert record["system"] == "be terse"
    assert record["tools"] == tools
    assert record["messages"] == messages
    assert record["cache_breakpoints"] == 0
    assert record["usage"] == {
        "input_tokens": 100,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 10,
        "output_tokens": 7,
    }
    assert record["stop_reason"] == "end_turn"


def test_each_call_appends_immediately_and_indexes_increment(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(_FakeClient())

    client.messages.create(model="claude-sonnet-5", messages=[{"role": "user", "content": "a"}])
    assert len(_lines(path)) == 1  # crash-safe: on disk before the next call

    client.messages.create(model="claude-sonnet-5", messages=[{"role": "user", "content": "b"}])
    records = _lines(path)
    assert [r["index"] for r in records] == [0, 1]
    assert len({r["run_id"] for r in records}) == 1


def test_missing_or_none_usage_fields_become_zero(tmp_path: Path) -> None:
    # cache_read is None, cache_creation attribute absent entirely.
    response = _FakeMessage(
        _FakeUsage(input_tokens=5, cache_read_input_tokens=None, output_tokens=2),
        stop_reason="max_tokens",
    )
    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(_FakeClient(response))
    client.messages.create(model="m", messages=[])
    (record,) = _lines(path)
    assert record["usage"] == {
        "input_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 2,
    }
    assert record["stop_reason"] == "max_tokens"


def test_cache_control_markers_counted_across_payload(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(_FakeClient())
    client.messages.create(
        model="claude-sonnet-5",
        system=[{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        tools=[{"name": "t1", "cache_control": {"type": "ephemeral"}}, {"name": "t2"}],
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
            }
        ],
    )
    (record,) = _lines(path)
    assert record["cache_breakpoints"] == 3


def test_system_block_list_is_rendered_to_text(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(_FakeClient())
    client.messages.create(
        model="m",
        system=[{"type": "text", "text": "alpha "}, {"type": "text", "text": "beta"}],
        messages=[],
    )
    (record,) = _lines(path)
    assert record["system"] == "alpha beta"


def test_stream_records_usage_from_get_final_message(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(_FakeClient())
    with client.messages.stream(
        model="claude-sonnet-5", messages=[{"role": "user", "content": "go"}]
    ) as stream:
        events = list(stream)  # iteration flows through the SDK's own stream
        assert not path.exists() or _lines(path) == []  # not recorded until exit
    assert events == ["event-1", "event-2"]
    (record,) = _lines(path)
    assert record["usage"]["input_tokens"] == 100
    assert record["usage"]["output_tokens"] == 7
    assert record["model"] == "claude-sonnet-5"


def test_stream_body_exception_records_nothing(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(_FakeClient())
    with pytest.raises(RuntimeError, match="boom"):
        with client.messages.stream(model="m", messages=[]):
            raise RuntimeError("boom")
    assert not path.exists() or _lines(path) == []


def test_create_exception_propagates_and_records_nothing(tmp_path: Path) -> None:
    class ExplodingMessages:
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("api down")

    class ExplodingClient:
        messages = ExplodingMessages()

    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(ExplodingClient())
    with pytest.raises(RuntimeError, match="api down"):
        client.messages.create(model="m", messages=[])
    assert not path.exists()


def test_async_create_awaits_before_recording(tmp_path: Path) -> None:
    # Regression: wrapping an AsyncAnthropic-shaped client used to call the
    # coroutine function without awaiting it and record a garbage line (all
    # usage zeros) BEFORE the API call even ran.
    path = tmp_path / "t.jsonl"
    client = Recorder(path, run_id="run-async").wrap(_FakeAsyncClient())

    async def scenario() -> Any:
        return await client.messages.create(
            model="claude-sonnet-5", messages=[{"role": "user", "content": "hi"}]
        )

    response = asyncio.run(scenario())
    assert response is client.messages.response  # awaited response, passed through
    (record,) = _lines(path)
    assert record["usage"]["input_tokens"] == 100
    assert record["usage"]["output_tokens"] == 7
    assert record["stop_reason"] == "end_turn"


def test_async_create_behind_sync_decorator_is_recorded(tmp_path: Path) -> None:
    # Regression: the real SDK wraps AsyncMessages.create in @required_args —
    # a plain sync `def wrapper` with functools.wraps — so
    # inspect.iscoroutinefunction(client.messages.create) is False. The
    # recorder used to take the sync branch, receive an un-awaited coroutine
    # with no .usage, and silently skip the call: billed spend understated.
    def required_args_like(func: Any) -> Any:  # mirrors anthropic._utils.required_args
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return wrapper

    class _DecoratedAsyncMessages(_FakeAsyncMessages):
        @required_args_like
        async def create(self, **kwargs: Any) -> _FakeMessage:  # type: ignore[override]
            self.requests.append(kwargs)
            return self.response

    client = _FakeAsyncClient()
    client.messages = _DecoratedAsyncMessages(client.messages.response)
    assert not inspect.iscoroutinefunction(client.messages.create)  # the trap

    path = tmp_path / "t.jsonl"
    Recorder(path, run_id="run-dec").wrap(client)

    async def scenario() -> Any:
        return await client.messages.create(
            model="claude-sonnet-5", messages=[{"role": "user", "content": "hi"}]
        )

    response = asyncio.run(scenario())
    assert response is client.messages.response
    (record,) = _lines(path)
    assert record["usage"]["input_tokens"] == 100
    assert record["usage"]["cache_read_input_tokens"] == 40
    assert record["model"] == "claude-sonnet-5"


def test_sync_looking_create_returning_awaitable_is_awaited_and_recorded(
    tmp_path: Path,
) -> None:
    # Defence in depth: a decorator without functools.wraps leaves no
    # __wrapped__ chain to unwrap, so detection must fall back to the returned
    # object — an awaitable response is awaited by a shim that records after.
    class _OpaqueAsyncMessages(_FakeAsyncMessages):
        def create(self, **kwargs: Any) -> Any:  # type: ignore[override]
            inner = _FakeAsyncMessages.create  # the real async implementation
            return inner(self, **kwargs)  # returns a coroutine; no __wrapped__

    client = _FakeAsyncClient()
    client.messages = _OpaqueAsyncMessages(client.messages.response)
    path = tmp_path / "t.jsonl"
    Recorder(path).wrap(client)

    async def scenario() -> Any:
        return await client.messages.create(model="m", messages=[])

    response = asyncio.run(scenario())
    assert response is client.messages.response
    (record,) = _lines(path)
    assert record["usage"]["output_tokens"] == 7
    assert record["usage"]["input_tokens"] == 100


def test_async_stream_records_usage_via_aexit(tmp_path: Path) -> None:
    # Regression: the recording stream manager had no __aenter__/__aexit__,
    # so `async with client.messages.stream(...)` raised TypeError in the
    # caller's own code.
    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(_FakeAsyncClient())

    async def scenario() -> None:
        async with client.messages.stream(
            model="claude-sonnet-5", messages=[{"role": "user", "content": "go"}]
        ) as stream:
            assert not path.exists() or _lines(path) == []  # not recorded until exit
            final = await stream.get_final_message()
            assert final is client.messages.response

    asyncio.run(scenario())
    (record,) = _lines(path)
    assert record["usage"]["input_tokens"] == 100
    assert record["model"] == "claude-sonnet-5"


def test_create_stream_true_is_not_recorded_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Regression: messages.create(stream=True) returns a raw stream with no
    # .usage; the recorder used to write a phantom all-zero-usage line that
    # silently understated every billed total.
    class _RawStream:  # deliberately no `usage` attribute
        pass

    class _StreamingMessages:
        def create(self, **kwargs: Any) -> _RawStream:
            return _RawStream()

    class _StreamingClient:
        messages = _StreamingMessages()

    path = tmp_path / "t.jsonl"
    client = Recorder(path).wrap(_StreamingClient())
    with caplog.at_level(logging.WARNING, logger="tokenbill.instrument"):
        first = client.messages.create(model="m", messages=[], stream=True)
        second = client.messages.create(model="m", messages=[], stream=True)
    assert isinstance(first, _RawStream)  # the caller's stream still works
    assert isinstance(second, _RawStream)
    assert not path.exists()  # no phantom zero-usage trace lines
    warnings = [r for r in caplog.records if "NOT recorded" in r.getMessage()]
    assert len(warnings) == 1  # warned once per recorder, not per call
    assert "messages.stream" in warnings[0].getMessage()


def test_recording_contextmanager_yields_recorder(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    with recording(path, run_id="run-cm") as recorder:
        assert isinstance(recorder, Recorder)
        client = recorder.wrap(_FakeClient())
        client.messages.create(model="m", messages=[])
    (record,) = _lines(path)
    assert record["run_id"] == "run-cm"


def test_default_run_ids_are_generated_and_distinct(tmp_path: Path) -> None:
    a = Recorder(tmp_path / "a.jsonl")
    b = Recorder(tmp_path / "b.jsonl")
    assert a.run_id.startswith("run-")
    assert a.run_id != b.run_id


def test_recorded_file_round_trips_through_trace_reader(tmp_path: Path) -> None:
    trace = pytest.importorskip("tokenbill.trace", reason="Module A's trace.py not present yet")
    path = tmp_path / "t.jsonl"
    client = Recorder(path, run_id="run-rt").wrap(_FakeClient())
    client.messages.create(
        model="claude-sonnet-5",
        system="sys",
        tools=[{"name": "t", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": "one"}],
    )
    client.messages.create(
        model="claude-sonnet-5",
        system="sys",
        messages=[{"role": "user", "content": "two"}],
    )

    (run,) = trace.read_trace(path)
    assert run.run_id == "run-rt"
    assert [c.index for c in run.calls] == [0, 1]
    call = run.calls[0]
    assert call.model == "claude-sonnet-5"
    assert call.system == "sys"
    assert call.usage.input_tokens == 100
    assert call.usage.cache_read_input_tokens == 40
    assert call.stop_reason == "end_turn"

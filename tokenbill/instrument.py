"""SDK-level trace recorder: duck-typed wrap of an Anthropic-shaped client.

Never imports the ``anthropic`` package. Any object exposing a callable
``messages.create`` (and optionally ``messages.stream``) can be wrapped; the
recorder patches those attributes in place, captures each request's payload
at call time and the billed ``usage`` + ``stop_reason`` from the returned
message, and appends one ``tokenbill/trace@1`` JSONL line per **completed**
call — the file is opened, written, and closed per call, so a later crash
never loses earlier calls.

Async clients are supported: when ``messages.create`` is a coroutine function
(``AsyncAnthropic``), the wrapper awaits the response before recording, and
the stream wrapper implements the async context-manager protocol (awaiting
``get_final_message()``). Detection looks through decorator layers
(``functools.wraps`` sets ``__wrapped__``; the real SDK wraps ``create`` in a
plain sync decorator, so ``iscoroutinefunction`` on the surface function says
sync), and as a final net the sync wrapper checks the *returned* object: an
awaitable response is awaited by an async shim that records afterwards, so an
async ``create`` behind any decorator stack is still recorded.

Streaming is supported by wrapping the ``messages.stream`` context manager
and reading ``get_final_message()`` on clean exit. Raw streaming via
``messages.create(stream=True)`` returns an object with no ``usage``; such
calls are NOT recorded (a zero-usage line would silently understate spend)
and a warning points at ``messages.stream`` instead. Failed calls
(exceptions) propagate untouched and record nothing.

Payloads are recorded as sent; non-JSON-serializable content blocks fall back
to their ``repr`` rather than breaking the caller's API call.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tokenbill.common import TokenbillError

logger = logging.getLogger(__name__)

SCHEMA = "tokenbill/trace@1"

_USAGE_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _render_system(system: Any) -> str:
    """Normalize the ``system`` kwarg (str, block list, or None) to text."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for block in system:
        if isinstance(block, dict):
            parts.append(str(block.get("text", "")))
        else:
            parts.append(str(getattr(block, "text", block)))
    return "".join(parts)


def _count_cache_control(value: Any) -> int:
    """Count ``cache_control`` markers anywhere in a request payload."""
    if isinstance(value, dict):
        count = 1 if value.get("cache_control") is not None else 0
        return count + sum(_count_cache_control(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_count_cache_control(v) for v in value)
    return 0


def _is_async_create(create: Any) -> bool:
    """Is *create* a coroutine function, possibly behind decorator layers?

    The real SDK wraps ``AsyncMessages.create`` in a plain sync decorator
    (``@required_args`` uses ``functools.wraps``), so
    ``inspect.iscoroutinefunction`` on the surface callable answers False for
    an async client. Unwrapping via ``__wrapped__`` recovers the truth; a
    malformed ``__wrapped__`` chain (cycle) falls back to the surface answer.
    """
    if inspect.iscoroutinefunction(create):
        return True
    try:
        return inspect.iscoroutinefunction(inspect.unwrap(create))
    except ValueError:  # cycle in the __wrapped__ chain
        return False


def _usage_dict(response: Any) -> dict[str, int]:
    """Billed usage from a response object; missing/None fields become 0."""
    usage = getattr(response, "usage", None)
    result: dict[str, int] = {}
    for field in _USAGE_FIELDS:
        raw = getattr(usage, field, 0) if usage is not None else 0
        result[field] = int(raw or 0)
    return result


class Recorder:
    """Records every call a wrapped client makes to a JSONL trace file.

    Usage::

        recorder = Recorder("trace.jsonl")
        client = recorder.wrap(client)   # patches messages.create / .stream
        ... use the client exactly as before ...

    then ``tokenbill analyze trace.jsonl``.
    """

    def __init__(self, path: str | Path, run_id: str | None = None) -> None:
        self.path = Path(path)
        self.run_id = run_id if run_id is not None else f"run-{uuid.uuid4().hex[:12]}"
        self._index = 0
        self._lock = threading.Lock()  # index allocation + append are atomic
        self._warned_no_usage = False

    def wrap(self, client: Any) -> Any:
        """Patch ``client.messages.create``/``.stream`` to record; return client.

        Duck-typed: no ``anthropic`` import, no isinstance checks. Sync and
        async (coroutine-function ``create``) clients both work. Raises
        :class:`TokenbillError` when the object has no callable
        ``messages.create``.
        """
        messages = getattr(client, "messages", None)
        create = getattr(messages, "create", None)
        if not callable(create):
            raise TokenbillError(
                f"cannot instrument {type(client).__name__!r}: it has no callable "
                "`messages.create`. Recorder.wrap expects an Anthropic-SDK-shaped "
                "client (anything with client.messages.create)."
            )

        if _is_async_create(create):
            # AsyncAnthropic: the response must be awaited before its usage
            # exists; recording the un-awaited coroutine would write garbage.
            async def async_create_wrapper(*args: Any, **kwargs: Any) -> Any:
                request = self._capture_request(kwargs)
                ts = time.time()
                response = await create(*args, **kwargs)
                self._append_if_usage(request, ts, response)
                return response

            messages.create = async_create_wrapper
        else:

            def create_wrapper(*args: Any, **kwargs: Any) -> Any:
                request = self._capture_request(kwargs)
                ts = time.time()
                response = create(*args, **kwargs)
                if inspect.isawaitable(response):
                    # An async `create` hiding behind a decorator stack that
                    # _is_async_create could not see through: the coroutine has
                    # no usage yet, so hand back an awaitable shim that awaits
                    # it, records the real response, and returns it — instead
                    # of silently dropping the call from the trace.
                    return self._await_and_record(request, ts, response)
                self._append_if_usage(request, ts, response)
                return response

            messages.create = create_wrapper

        stream = getattr(messages, "stream", None)
        if callable(stream):

            def stream_wrapper(*args: Any, **kwargs: Any) -> _RecordingStreamManager:
                request = self._capture_request(kwargs)
                ts = time.time()
                return _RecordingStreamManager(self, request, ts, stream(*args, **kwargs))

            messages.stream = stream_wrapper

        return client

    async def _await_and_record(self, request: dict[str, Any], ts: float, awaitable: Any) -> Any:
        """Await a response produced by a sync-looking async ``create``; record it."""
        response = await awaitable
        self._append_if_usage(request, ts, response)
        return response

    def _capture_request(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Snapshot the request payload at call time (kwargs, as the SDK is called)."""
        return {
            "model": str(kwargs.get("model", "")),
            "system": _render_system(kwargs.get("system")),
            "tools": list(kwargs.get("tools") or []),
            "messages": list(kwargs.get("messages") or []),
            "cache_breakpoints": _count_cache_control(
                [kwargs.get("system"), kwargs.get("tools"), kwargs.get("messages")]
            ),
        }

    def _append_if_usage(self, request: dict[str, Any], ts: float, response: Any) -> None:
        """Record *response* only when it actually carries billed usage.

        ``messages.create(stream=True)`` returns a raw stream object with no
        ``usage``; writing a zero-usage line for a call that cost real money
        would silently understate every downstream total, so the call is
        skipped with a warning (once per recorder) instead.
        """
        if getattr(response, "usage", None) is None:
            if not self._warned_no_usage:
                self._warned_no_usage = True
                logger.warning(
                    "response of type %s has no `usage` (raw streaming via "
                    "messages.create(stream=True)?); call NOT recorded — use "
                    "client.messages.stream(...) so usage can be read from "
                    "get_final_message(), or billed totals would be understated",
                    type(response).__name__,
                )
            return
        self._append(request, ts, response)

    def _append(self, request: dict[str, Any], ts: float, response: Any) -> None:
        """Append one completed call as a trace line. Open/write/close: crash-safe."""
        record = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "index": None,  # allocated under the lock just before writing
            "ts": ts,
            "model": request["model"],
            "system": request["system"],
            "tools": request["tools"],
            "messages": request["messages"],
            "cache_breakpoints": request["cache_breakpoints"],
            "usage": _usage_dict(response),
            "stop_reason": str(getattr(response, "stop_reason", "") or ""),
        }
        # One lock covers index allocation AND the file append: concurrent
        # calls (parallel tool-running agents) must not duplicate an index or
        # write out of order — read_trace rejects non-monotonic indexes.
        with self._lock:
            record["index"] = self._index
            self._index += 1
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=repr) + "\n")
                fh.flush()


class _RecordingStreamManager:
    """Wraps the SDK's ``messages.stream(...)`` context manager.

    ``__enter__`` returns the SDK's own stream object, so iteration and
    helpers behave exactly as unwrapped. On clean exit the final message's
    usage is read via ``get_final_message()`` and the call is recorded; on
    exception nothing is recorded and the exception propagates. Both the
    sync (``with``) and async (``async with``) protocols are implemented, so
    ``AsyncAnthropic``'s ``messages.stream(...)`` records too instead of
    crashing the caller with a missing-``__aenter__`` TypeError.
    """

    def __init__(
        self,
        recorder: Recorder,
        request: dict[str, Any],
        ts: float,
        inner: Any,
    ) -> None:
        self._recorder = recorder
        self._request = request
        self._ts = ts
        self._inner = inner
        self._stream: Any = None

    def __enter__(self) -> Any:
        self._stream = self._inner.__enter__()
        return self._stream

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if exc_type is None:
            try:
                final = self._stream.get_final_message()
            except BaseException:
                self._inner.__exit__(exc_type, exc, tb)
                raise
            self._recorder._append_if_usage(self._request, self._ts, final)
        return self._inner.__exit__(exc_type, exc, tb)

    async def __aenter__(self) -> Any:
        self._stream = await self._inner.__aenter__()
        return self._stream

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if exc_type is None:
            try:
                final = await self._stream.get_final_message()
            except BaseException:
                await self._inner.__aexit__(exc_type, exc, tb)
                raise
            self._recorder._append_if_usage(self._request, self._ts, final)
        return await self._inner.__aexit__(exc_type, exc, tb)


@contextmanager
def recording(path: str | Path, run_id: str | None = None) -> Iterator[Recorder]:
    """Sugar: ``with recording("trace.jsonl") as rec: client = rec.wrap(client)``."""
    yield Recorder(path, run_id=run_id)

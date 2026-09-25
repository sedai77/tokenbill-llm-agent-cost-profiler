"""SDK-level trace recorder: duck-typed wrap of an Anthropic-shaped client.

Never imports the ``anthropic`` package (nor ``httpx``). Any object exposing a callable
``messages.create`` (and optionally ``messages.stream``) can be wrapped.

**``format="trace@1"`` (default) is exactly the v0.1 recorder** (D13): the recorder patches those
attributes in place, captures each request's payload at call time and the billed ``usage`` +
``stop_reason`` from the returned message, and appends one ``tokenbill/trace@1`` JSONL line per
**completed** call — the file is opened, written, and closed per call, so a later crash never
loses earlier calls.

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

**``format="trace@2"``** (SPEC §5.7, opt-in) writes a ``tokenbill/trace@2`` file through a bounded
background queue (a full queue drops records and counts them in :attr:`Recorder.dropped`; nothing
ever raises into the caller; :meth:`Recorder.close` flushes). It wraps ``messages.create`` /
``.stream`` and ``beta.messages.create`` / ``.stream``, sync and async; snapshots the request
**before** sending (SDK objects via ``model_dump()``, never ``repr``) and records every
prompt-affecting parameter (:class:`~tokenbill.core.records.RequestParams`, breakpoints, appended
sizes); in the ``fingerprint`` / ``full`` content tiers it fingerprints blocks (SPEC §5.8) under
``core.keys.load_or_create(key_file)``. It keeps the full usage object (``raw_usage`` with
convention ``anthropic.messages``) and records **every attempt**: duck-typed httpx event hooks on
``client._client.event_hooks`` turn SDK-internal retries into separate attempts (status,
``x-stainless-retry-count``, ``retry-after-ms`` / ``retry-after``, ``x-should-retry``; bodies are
never read; a ``contextvars.ContextVar`` ties hooks to the wrapped call) — or, when hooks cannot be
attached, one attempt per call and ``dq.sdk_retries_invisible``. Exceptions become attempts with
outcome ``http_error`` / ``timeout`` / ``network_error`` and no usage; aborted streams become
``PARTIAL_STREAM`` attempts (message_start input usage + streamed output). ``diagnostics=True``
injects the ``cache-diagnosis-2026-04-07`` beta and ``diagnostics.previous_message_id`` per lane
(first-party only; it changes the request, hence opt-in).
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import dataclasses
import gzip
import inspect
import io
import json
import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from tokenbill.adapters.fingerprint import (
    FingerprintCache,
    fingerprint_blocks,
    plain,
    scan_request,
    snapshot_blocks,
    tokenizer_family,
)
from tokenbill.adapters.trace_v2 import TraceV2Writer, sanitize_raw_usage
from tokenbill.common import TokenbillError, canonical_json
from tokenbill.core.conventions import ANTHROPIC_MESSAGES, anthropic_inferences
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import hmac_hex, key_id, request_id_for, stable_id
from tokenbill.core.jsonl import open_private
from tokenbill.core.keys import load_or_create
from tokenbill.core.models import normalize_model
from tokenbill.core.records import (
    DIAG_REASONS,
    AppendedItem,
    Attempt,
    Attribution,
    Breakpoint,
    CacheDiagnostic,
    ContentFingerprint,
    ContentTier,
    Fidelity,
    Inference,
    Lane,
    LaneKind,
    Outcome,
    PricingContext,
    Request,
    RequestParams,
    Session,
    SourceRef,
    UsageSource,
)
from tokenbill.core.types import DataQualityNote

__all__ = ["DIAGNOSTICS_BETA", "DQ_SDK_RETRIES_INVISIBLE", "SCHEMA", "Recorder", "recording"]

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

    ``format="trace@2"`` (with ``content`` ``"none"`` | ``"fingerprint"`` | ``"full"``,
    ``key_file``, ``diagnostics``, ``lane``, ``queue_size`` and ``http_hooks``) records the
    SPEC §5.7 trace@2 file instead; call :meth:`close` (or use :func:`recording`) to flush it.
    The trace@2 options need ``format="trace@2"``.
    """

    def __init__(self, path: str | Path, run_id: str | None = None, *, format: str = "trace@1",
                 content: str = "none", key_file: str | Path | None = None,
                 diagnostics: bool = False, lane: str | None = None, queue_size: int = 10_000,
                 http_hooks: bool = True) -> None:
        self.path = Path(path)
        self.run_id = run_id if run_id is not None else f"run-{uuid.uuid4().hex[:12]}"
        self._index = 0
        self._lock = threading.Lock()  # index allocation + append are atomic
        self._warned_no_usage = False
        if format not in ("trace@1", "trace@2"):
            raise UsageError("format must be 'trace@1' or 'trace@2'")
        self.format = format
        self._v2: _V2Recorder | None = None
        if format == "trace@1":
            if content != "none" or diagnostics or key_file is not None or lane is not None:
                raise UsageError("content, key_file, diagnostics and lane need format='trace@2'")
        else:
            self._v2 = _V2Recorder(self, content=content, key_file=key_file,
                                   diagnostics=diagnostics, lane=lane, queue_size=queue_size,
                                   http_hooks=http_hooks)

    @property
    def dropped(self) -> int:
        """Records dropped by the bounded trace@2 queue or a failing writer (trace@1: 0)."""
        return self._v2.dropped if self._v2 is not None else 0

    def close(self) -> None:
        """Flush and close the trace@2 file (idempotent; a no-op for trace@1)."""
        if self._v2 is not None:
            self._v2.close()

    def wrap(self, client: Any) -> Any:
        """Patch ``client.messages.create``/``.stream`` to record; return client.

        Duck-typed: no ``anthropic`` import, no isinstance checks. Sync and
        async (coroutine-function ``create``) clients both work. Raises
        :class:`TokenbillError` when the object has no callable
        ``messages.create``.
        """
        if self._v2 is not None:
            return self._v2.wrap(client)
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



# =============================================================================================
# trace@2 mode (SPEC §5.7, D13, D35)
# =============================================================================================

#: Beta header value that enables Anthropic cache diagnostics (SPEC §19.3, ``diagnostics=True``).
DIAGNOSTICS_BETA = "cache-diagnosis-2026-04-07"
#: Data-quality code recorded when SDK-internal retries cannot be observed (D35).
DQ_SDK_RETRIES_INVISIBLE = "dq.sdk_retries_invisible"
_RECORDER = "recorder"
_PRIORITY = 50
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_MODEL_RE = re.compile(r"[A-Za-z0-9_.:@/+\[\]-]{1,128}\Z")
_STOP = object()
_CALL: contextvars.ContextVar[_Call | None] = contextvars.ContextVar("tokenbill_recorder_call",
                                                                    default=None)
_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
               "output_tokens", "cache_creation", "server_tool_use", "output_tokens_details",
               "service_tier", "inference_geo", "speed", "iterations")
_USAGE_NESTED = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens", "web_search_requests",
                 "web_fetch_requests", "thinking_tokens", "type", "model", "input_tokens",
                 "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens",
                 "cache_creation")
_ERROR_TYPES = {400: "invalid_request", 401: "auth", 403: "auth", 404: "not_found",
                408: "timeout", 413: "request_too_large", 429: "rate_limit", 500: "api_error",
                502: "api_error", 503: "overloaded", 504: "timeout", 529: "overloaded"}
_CHANNELS = (("Bedrock", "bedrock", "bedrock"), ("Vertex", "vertex", "vertex"),
             ("Foundry", "foundry", "foundry"))
_BLOCK_APPENDED = {"tool_result": "tool_result", "image": "image", "document": "attachment"}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _token(value: Any, pattern: re.Pattern[str] = _TOKEN_RE) -> str | None:
    return value if isinstance(value, str) and pattern.match(value) else None


def _int_or_none(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= 2**53:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
        return n if n <= 2**53 else None
    return None


def _hget(headers: Any, name: str) -> str | None:
    """A header value (case-insensitive) from an httpx ``Headers`` or a plain mapping."""
    if headers is None:
        return None
    try:
        value = headers.get(name)
        if value is None and isinstance(headers, Mapping):
            for k, v in headers.items():
                if isinstance(k, str) and k.lower() == name:
                    value = v
                    break
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _retry_after_ms(headers: Any) -> int | None:
    """``retry-after-ms``, else ``retry-after`` seconds × 1000 (HTTP dates are not parsed)."""
    for name, factor in (("retry-after-ms", 1), ("retry-after", 1000)):
        raw = _hget(headers, name)
        if raw is None:
            continue
        try:
            value = float(raw.strip())
        except ValueError:
            continue
        if value >= 0 and value * factor <= 2**53:
            return int(round(value * factor))
    return None


def _should_retry(headers: Any) -> bool | None:
    raw = _hget(headers, "x-should-retry")
    if raw is None:
        return None
    raw = raw.strip().lower()
    return True if raw == "true" else False if raw == "false" else None


def _obj_get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _usage_plain(usage: Any, _depth: int = 0) -> Any:
    """A JSON-shaped copy of an SDK usage object (``model_dump`` when present, else the documented
    attributes, SPEC §19.4)."""
    if usage is None or isinstance(usage, (bool, int, float, str)) or _depth > 6:
        return usage
    if isinstance(usage, Mapping):
        return {str(k): _usage_plain(v, _depth + 1) for k, v in usage.items()}
    if isinstance(usage, (list, tuple)):
        return [_usage_plain(v, _depth + 1) for v in usage]
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        try:
            return _usage_plain(dump(), _depth + 1)
        except Exception:
            pass
    keys = _USAGE_KEYS if _depth == 0 else _USAGE_NESTED
    out = {}
    for key in keys:
        value = getattr(usage, key, None)
        if value is not None:
            out[key] = _usage_plain(value, _depth + 1)
    return out


def _error_outcome(exc: BaseException) -> tuple[Outcome, int | None, str | None]:
    """``(outcome, http status, error type)`` of an exception raised by a wrapped call."""
    if not isinstance(exc, Exception):
        return Outcome.ABORTED, None, None
    status = _int_or_none(getattr(exc, "status_code", None))
    if status is not None:
        return Outcome.HTTP_ERROR, status, _ERROR_TYPES.get(
            status, "api_error" if status >= 500 else "invalid_request")
    name = type(exc).__name__
    if "Timeout" in name:
        return Outcome.TIMEOUT, None, "timeout"
    if "Connection" in name:
        return Outcome.NETWORK_ERROR, None, "connection"
    return Outcome.HTTP_ERROR, None, None


def _diagnostic(diag: Any) -> CacheDiagnostic | None:
    reason = _obj_get(diag, "cache_miss_reason")
    rtype = _token(_obj_get(reason, "type"))
    if rtype is None:
        return None
    return CacheDiagnostic(reason=rtype if rtype in DIAG_REASONS else "unavailable",
                           provider_reason=rtype,
                           missed_input_tokens_estimate=_int_or_none(
                               _obj_get(reason, "cache_missed_input_tokens")),
                           source="anthropic.cache_diagnostics")


def _applied_edits(cm: Any) -> tuple[tuple[str, int], ...]:
    edits = _obj_get(cm, "applied_edits")
    if not isinstance(edits, (list, tuple)):
        return ()
    out = []
    for e in edits:
        etype = _token(_obj_get(e, "type"))
        if etype is not None:
            out.append((etype, _int_or_none(_obj_get(e, "cleared_input_tokens")) or 0))
    return tuple(out)


def _channel_of(client: Any) -> tuple[str, str]:
    """``(channel, billing path)`` from the client class name (``AnthropicBedrock`` …)."""
    name = type(client).__name__
    for marker, channel, path in _CHANNELS:
        if marker in name:
            return channel, path
    return "anthropic_api", "api_key"


@dataclass
class _Att:
    """One HTTP attempt observed by the event hooks."""

    ts_start_ms: int
    mono: float
    retry_count: int | None
    status: int | None = None
    duration_ms: int | None = None
    retry_after_ms: int | None = None
    should_retry: bool | None = None
    request_id: str | None = None


@dataclass
class _Call:
    """Caller-side state of one wrapped call (snapshot taken before sending)."""

    recorder: _V2Recorder
    n: int
    seq: int
    lane_key: str
    channel: str
    billing_path: str
    ts_start_ms: int
    mono: float
    params: RequestParams
    payload: tuple[Any, Any, Any] | None      # pre-send (tools, system, messages) copy: fp tiers
    appended: tuple[AppendedItem, ...]
    attempts: list[_Att] = dc_field(default_factory=list)


@dataclass
class _Result:
    """What the worker needs to build the request record (all plain values)."""

    call: _Call
    outcome: Outcome
    duration_ms: int
    usage: dict | None = None
    usage_source: UsageSource = UsageSource.FINAL
    message_id: str | None = None
    model_served: str | None = None
    stop_reason: str | None = None
    request_id_hint: str | None = None
    diagnostics: CacheDiagnostic | None = None
    applied_edits: tuple[tuple[str, int], ...] = ()
    http_status: int | None = None
    error_type: str | None = None


def _response_result(call: _Call, response: Any, *, source: UsageSource = UsageSource.FINAL,
                     outcome: Outcome = Outcome.OK) -> _Result:
    usage = _usage_plain(_obj_get(response, "usage"))
    return _Result(
        call=call, outcome=outcome,
        duration_ms=max(0, int((time.monotonic() - call.mono) * 1000)),
        usage=usage if isinstance(usage, dict) else None, usage_source=source,
        message_id=_token(_obj_get(response, "id"), _MODEL_RE),
        model_served=_token(_obj_get(response, "model"), _MODEL_RE),
        stop_reason=_token(_obj_get(response, "stop_reason")),
        request_id_hint=_token(getattr(response, "_request_id", None), _MODEL_RE),
        diagnostics=_diagnostic(_obj_get(response, "diagnostics")),
        applied_edits=_applied_edits(_obj_get(response, "context_management")))


def _thinking(value: Any) -> str | None:
    kind = _obj_get(value, "type")
    if kind == "enabled":
        budget = _int_or_none(_obj_get(value, "budget_tokens"))
        return f"enabled:{budget}" if budget is not None else "enabled"
    if kind == "adaptive":
        return "adaptive"
    if kind == "disabled":
        return "off"
    return None


def _has_type(blocks: Any, prefix: str) -> bool:
    return isinstance(blocks, (list, tuple)) and any(
        isinstance(_obj_get(b, "type"), str) and _obj_get(b, "type").startswith(prefix)
        for b in blocks)


def _message_blocks(messages: Any) -> Iterator[tuple[str | None, Any]]:
    for msg in messages or ():
        role = _obj_get(msg, "role")
        content = _obj_get(msg, "content")
        if isinstance(content, str):
            yield role, {"type": "text", "text": content}
        elif isinstance(content, (list, tuple)):
            for block in content:
                yield role, block


def _betas(kwargs: Mapping[str, Any]) -> tuple[str, ...]:
    out: list[str] = []
    for b in kwargs.get("betas") or ():
        if _token(b):
            out.append(b)
    header = _hget(kwargs.get("extra_headers"), "anthropic-beta")
    if header:
        out.extend(v.strip() for v in header.split(",") if _token(v.strip()))
    return tuple(sorted(set(out)))


def _secret_hash(key: bytes | None, value: Any) -> str:
    if key is None:
        return "set"
    try:
        data = canonical_json(plain(value)).encode("utf-8", "surrogatepass")
    except (TypeError, ValueError):
        return "set"
    return hmac_hex(key, data)


def _request_params(kwargs: Mapping[str, Any], *, stream: bool, key: bytes | None,
                    breakpoints: tuple[Breakpoint, ...], has_images: bool) -> RequestParams:
    """Every prompt-affecting parameter of a request, content-free (SPEC §5.7)."""
    output_config = kwargs.get("output_config")
    effort = _token(_obj_get(output_config, "effort")) or _token(kwargs.get("effort"))
    fmt = _obj_get(output_config, "format") or kwargs.get("output_format")
    tc = kwargs.get("tool_choice")
    tc_type = _obj_get(tc, "type") if tc is not None else None
    tool_choice: str | None = None
    if tc_type in ("auto", "any", "none"):
        tool_choice = tc_type
    elif tc_type == "tool":
        name = _obj_get(tc, "name")
        tool_choice = "tool:" + hmac_hex(key, str(name).encode()) if key is not None else "tool"
    dptu = _obj_get(tc, "disable_parallel_tool_use") if tc is not None else None
    cm = kwargs.get("context_management")
    tools = kwargs.get("tools")
    return RequestParams(
        model_requested=_token(kwargs.get("model"), _MODEL_RE) or "",
        max_tokens=_int_or_none(kwargs.get("max_tokens")),
        stream=stream,
        thinking=_thinking(kwargs.get("thinking")),
        effort=effort,
        tool_choice=tool_choice,
        output_format=None if fmt is None else _secret_hash(key, fmt),
        speed=_token(kwargs.get("speed")),
        service_tier_requested=_token(kwargs.get("service_tier")),
        inference_geo_requested=_token(kwargs.get("inference_geo")),
        betas=_betas(kwargs),
        breakpoints=breakpoints,
        automatic_caching=kwargs.get("cache_control") is not None,
        context_management=None if cm is None else _secret_hash(key, cm),
        web_search_enabled=_has_type(tools, "web_search"),
        has_images=has_images,
        disable_parallel_tool_use=dptu if isinstance(dptu, bool) else None,
    )


def _appended(messages: Any, since: int) -> tuple[AppendedItem, ...]:
    """Content-free summary of the messages at index ≥ *since* (what entered the context since
    the previous request of the lane): kind, UTF-8 size, error flag, images."""
    if not isinstance(messages, (list, tuple)) or since >= len(messages):
        return ()
    items = []
    for role, block in _message_blocks(messages[since:]):
        btype = _obj_get(block, "type")
        if role == "assistant":
            kind = "assistant"
        else:
            kind = _BLOCK_APPENDED.get(btype, "user_text") if isinstance(btype, str) \
                else "user_text"
        try:
            n = len(json.dumps(plain(block), ensure_ascii=False, separators=(",", ":"))
                    .encode("utf-8", "surrogatepass"))
        except (TypeError, ValueError):
            n = 0
        images = 1 if btype == "image" else 0
        if btype == "tool_result":
            inner = _obj_get(block, "content")
            if isinstance(inner, (list, tuple)):
                images = sum(1 for b in inner if _obj_get(b, "type") == "image")
        items.append(AppendedItem(kind=kind, name=None, n_bytes=n,
                                  is_error=_obj_get(block, "is_error") is True, images=images))
    return tuple(items)


class _V2Recorder:
    """The trace@2 engine behind :class:`Recorder` (``format="trace@2"``)."""

    def __init__(self, owner: Recorder, *, content: str, key_file: str | Path | None,
                 diagnostics: bool, lane: str | None, queue_size: int, http_hooks: bool) -> None:
        try:
            self.tier = ContentTier(content)
        except ValueError:
            raise UsageError("content must be none, fingerprint or full") from None
        if type(queue_size) is not int or queue_size < 1:
            raise UsageError("queue_size must be a positive int")
        path = owner.path
        try:
            if path.exists() and (path.is_dir() or path.stat().st_size > 0):
                raise UsageError(f"{path.name}: a trace@2 recorder writes a new file; choose "
                                 "another path")
        except OSError:
            pass
        self.owner = owner
        self.path = path
        self.diagnostics = bool(diagnostics)
        self.http_hooks = bool(http_hooks)
        self.key: bytes | None = None
        if self.tier is not ContentTier.NONE:
            self.key = load_or_create(Path(key_file) if key_file is not None else None)
        self.trace_id = "t_" + uuid.uuid4().hex[:24]
        self.created_ms = _now_ms()
        self.session_key = stable_id("ses", _RECORDER, self.trace_id, owner.run_id)
        self.source_id = stable_id("s", _RECORDER, self.trace_id)
        self.lane_key = stable_id("ln", self.session_key, lane or "main")
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._dropped = 0
        self._n = 0
        self._seq: dict[str, int] = {}
        self._msg_count: dict[str, int] = {}
        self._prev_message: dict[str, str] = {}
        self._hooked: dict[int, Any] = {}
        self._invisible_noted = False
        self._thread: threading.Thread | None = None
        self.closed = False
        self._warned = False
        # worker-side state
        self._writer: TraceV2Writer | None = None
        self._stack: contextlib.ExitStack | None = None
        self._lanes_written: set[str] = set()
        self._content_written: set[str] = set()
        self._first_ts: int | None = None
        self._last_ts: int | None = None
        self._cache = FingerprintCache()
        self._handles: list[Any] = []
        atexit.register(self.close)

    # -- counters -----------------------------------------------------------------------------

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def _drop(self, n: int = 1, exc: BaseException | None = None) -> None:
        with self._lock:
            self._dropped += n
            warn = not self._warned
            self._warned = True
        if warn:
            logger.warning("trace@2 recorder dropped a record (%s); dropped records are counted "
                           "in Recorder.dropped", type(exc).__name__ if exc else "queue full")

    def _enqueue(self, item: Any) -> None:
        if self.closed:
            self._drop()
            return
        self._ensure_thread()
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self._drop()

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="tokenbill-recorder",
                                                daemon=True)
                self._thread.start()

    # -- wrapping -----------------------------------------------------------------------------

    def wrap(self, client: Any) -> Any:
        messages = getattr(client, "messages", None)
        create = getattr(messages, "create", None)
        if not callable(create):
            raise TokenbillError(
                f"cannot instrument {type(client).__name__!r}: it has no callable "
                "`messages.create`. Recorder.wrap expects an Anthropic-SDK-shaped "
                "client (anything with client.messages.create)."
            )
        is_async = _is_async_create(create)
        channel, billing_path = _channel_of(client)
        self._patch(messages, False, channel, billing_path)
        beta_messages = getattr(getattr(client, "beta", None), "messages", None)
        if beta_messages is not None and callable(getattr(beta_messages, "create", None)):
            self._patch(beta_messages, True, channel, billing_path)
        attached = self.http_hooks and self._attach_hooks(client, is_async)
        if not attached and not self._invisible_noted:
            self._invisible_noted = True
            self._enqueue(("note", DataQualityNote(
                code=DQ_SDK_RETRIES_INVISIBLE, severity="info", count=1,
                detail="SDK-internal HTTP retries are not observable for this client; a call's "
                       "retries collapse into one attempt")))
        return client

    def _attach_hooks(self, client: Any, is_async: bool) -> bool:
        http = getattr(client, "_client", None)
        hooks = getattr(http, "event_hooks", None)
        if not isinstance(hooks, MutableMapping):
            return False
        req, resp = hooks.get("request"), hooks.get("response")
        if not isinstance(req, list) or not isinstance(resp, list):
            return False
        if id(http) in self._hooked:
            return True
        if is_async:
            req.append(self._on_request_async)
            resp.append(self._on_response_async)
        else:
            req.append(self._on_request)
            resp.append(self._on_response)
        self._hooked[id(http)] = http
        return True

    def _patch(self, messages: Any, beta: bool, channel: str, billing_path: str) -> None:
        create = messages.create
        if _is_async_create(create):
            async def async_create(*args: Any, **kwargs: Any) -> Any:
                kwargs = self._prepare(kwargs, beta, channel)
                call = self._begin(kwargs, channel, billing_path, stream=bool(
                    kwargs.get("stream")))
                token = _CALL.set(call)
                try:
                    response = await create(*args, **kwargs)
                except BaseException as exc:
                    _CALL.reset(token)
                    self._fail(call, exc)
                    raise
                _CALL.reset(token)
                return self._succeed(call, response)

            messages.create = async_create
        else:
            def sync_create(*args: Any, **kwargs: Any) -> Any:
                kwargs = self._prepare(kwargs, beta, channel)
                call = self._begin(kwargs, channel, billing_path, stream=bool(
                    kwargs.get("stream")))
                token = _CALL.set(call)
                try:
                    response = create(*args, **kwargs)
                except BaseException as exc:
                    _CALL.reset(token)
                    self._fail(call, exc)
                    raise
                _CALL.reset(token)
                if inspect.isawaitable(response):
                    return self._await_create(call, response)
                return self._succeed(call, response)

            messages.create = sync_create
        stream = getattr(messages, "stream", None)
        if callable(stream):
            def stream_wrapper(*args: Any, **kwargs: Any) -> _V2StreamManager:
                kwargs = self._prepare(kwargs, beta, channel)
                call = self._begin(kwargs, channel, billing_path, stream=True)
                return _V2StreamManager(self, call, stream(*args, **kwargs))

            messages.stream = stream_wrapper

    async def _await_create(self, call: _Call | None, awaitable: Any) -> Any:
        token = _CALL.set(call)
        try:
            response = await awaitable
        except BaseException as exc:
            _CALL.reset(token)
            self._fail(call, exc)
            raise
        _CALL.reset(token)
        return self._succeed(call, response)

    # -- caller side (never raises into the caller) ------------------------------------------

    def _prepare(self, kwargs: dict[str, Any], beta: bool, channel: str) -> dict[str, Any]:
        """Inject the diagnostics beta and ``previous_message_id`` (opt-in, first-party only)."""
        if not self.diagnostics or channel != "anthropic_api":
            return kwargs
        try:
            out = dict(kwargs)
            if beta:
                betas = list(out.get("betas") or [])
                if DIAGNOSTICS_BETA not in betas:
                    betas.append(DIAGNOSTICS_BETA)
                out["betas"] = betas
            else:
                headers = dict(out.get("extra_headers") or {})
                existing = headers.get("anthropic-beta")
                values = [v.strip() for v in existing.split(",")] if isinstance(
                    existing, str) and existing else []
                if DIAGNOSTICS_BETA not in values:
                    values.append(DIAGNOSTICS_BETA)
                headers["anthropic-beta"] = ",".join(values)
                out["extra_headers"] = headers
            prev = self._prev_message.get(self.lane_key)
            if prev is not None:
                body = dict(out.get("extra_body") or {})
                diag = dict(body.get("diagnostics") or {})
                diag["previous_message_id"] = prev
                body["diagnostics"] = diag
                out["extra_body"] = body
            return out
        except Exception:  # never break the caller's request
            return kwargs

    def _begin(self, kwargs: Mapping[str, Any], channel: str, billing_path: str, *,
               stream: bool) -> _Call | None:
        try:
            tools, system, messages = (kwargs.get("tools"), kwargs.get("system"),
                                       kwargs.get("messages"))
            if self.tier is ContentTier.NONE:
                payload = None
                bps, kinds = scan_request(tools=tools, system=system, messages=messages)
            else:
                # a cheap deep copy before sending (the caller may mutate its objects later); the
                # worker serializes and hashes it off the caller's thread
                payload = (plain(tools or []), plain(system), plain(messages or []))
                _bps, kinds = scan_request(tools=[], system=None, messages=payload[2])
                bps = ()
            has_images = "image" in kinds
            params = _request_params(kwargs, stream=stream, key=self.key, breakpoints=bps,
                                     has_images=has_images)
            with self._lock:
                self._n += 1
                n = self._n
                seq = self._seq.get(self.lane_key, 0)
                self._seq[self.lane_key] = seq + 1
                prev_count = self._msg_count.get(self.lane_key, 0)
                n_msgs = len(messages) if isinstance(messages, (list, tuple)) else 0
                self._msg_count[self.lane_key] = n_msgs
            appended = _appended(messages, prev_count if n_msgs >= prev_count else 0)
            return _Call(recorder=self, n=n, seq=seq, lane_key=self.lane_key, channel=channel,
                         billing_path=billing_path, ts_start_ms=_now_ms(),
                         mono=time.monotonic(), params=params, payload=payload,
                         appended=appended)
        except Exception as exc:
            self._drop(exc=exc)
            return None

    def _succeed(self, call: _Call | None, response: Any) -> Any:
        if call is None:
            return response
        try:
            if getattr(response, "usage", None) is None and not isinstance(response, Mapping):
                if hasattr(response, "__aiter__"):
                    return _RawStreamProxy(self, call, response, is_async=True)
                if hasattr(response, "__iter__"):
                    return _RawStreamProxy(self, call, response, is_async=False)
            result = _response_result(call, response)
            if result.message_id is not None:
                self._prev_message[call.lane_key] = result.message_id
            self._enqueue(("call", result))
        except Exception as exc:
            self._drop(exc=exc)
        return response

    def _fail(self, call: _Call | None, exc: BaseException) -> None:
        if call is None:
            return
        try:
            outcome, status, etype = _error_outcome(exc)
            self._enqueue(("call", _Result(
                call=call, outcome=outcome,
                duration_ms=max(0, int((time.monotonic() - call.mono) * 1000)),
                http_status=status, error_type=etype)))
        except Exception as err:
            self._drop(exc=err)

    def _partial(self, call: _Call | None, snapshot: Any, exc: BaseException | None) -> None:
        """An aborted stream: message_start input usage + streamed output (PARTIAL_STREAM)."""
        if call is None:
            return
        try:
            if snapshot is not None and _obj_get(snapshot, "usage") is not None:
                result = _response_result(call, snapshot, source=UsageSource.PARTIAL_STREAM,
                                          outcome=Outcome.ABORTED)
            else:
                outcome, status, etype = (_error_outcome(exc) if exc is not None
                                          else (Outcome.ABORTED, None, None))
                result = _Result(call=call, outcome=outcome,
                                 duration_ms=max(0, int((time.monotonic() - call.mono) * 1000)),
                                 http_status=status, error_type=etype)
            self._enqueue(("call", result))
        except Exception as err:
            self._drop(exc=err)

    # -- HTTP event hooks (read status and retry headers only, never bodies) -------------------

    def _on_request(self, request: Any) -> None:
        try:
            call = _CALL.get()
            if call is None or call.recorder is not self:
                return
            retry = _int_or_none(_hget(getattr(request, "headers", None),
                                       "x-stainless-retry-count"))
            call.attempts.append(_Att(ts_start_ms=_now_ms(), mono=time.monotonic(),
                                      retry_count=retry))
        except Exception:  # a hook never breaks the SDK
            pass

    def _on_response(self, response: Any) -> None:
        try:
            call = _CALL.get()
            if call is None or call.recorder is not self or not call.attempts:
                return
            att = call.attempts[-1]
            if att.status is not None:
                return
            headers = getattr(response, "headers", None)
            att.status = _int_or_none(getattr(response, "status_code", None))
            att.duration_ms = max(0, int((time.monotonic() - att.mono) * 1000))
            att.retry_after_ms = _retry_after_ms(headers)
            att.should_retry = _should_retry(headers)
            att.request_id = _token(_hget(headers, "request-id"), _MODEL_RE)
        except Exception:
            pass

    async def _on_request_async(self, request: Any) -> None:
        self._on_request(request)

    async def _on_response_async(self, response: Any) -> None:
        self._on_response(response)

    # -- worker ---------------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is _STOP:
                break
            try:
                self._handle(item)
            except Exception as exc:
                self._drop(exc=exc)
            if self.queue.empty():
                self._flush()
        self._finish()

    def _flush(self) -> None:
        stack = self._stack
        if stack is None:
            return
        try:
            for handle in self._handles:
                handle.flush()
        except Exception:
            pass

    def _open(self) -> TraceV2Writer:
        if self._writer is not None:
            return self._writer
        stack = contextlib.ExitStack()
        try:
            if self.path.suffix.lower() == ".gz":
                raw = stack.enter_context(open_private(self.path, "wb"))
                gz = stack.enter_context(gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                                                       mtime=0))
                text = stack.enter_context(io.TextIOWrapper(gz, encoding="utf-8", newline=""))
                self._handles = [text, gz, raw]
            else:
                text = stack.enter_context(open_private(self.path, "w"))
                self._handles = [text]
            profile = {ContentTier.NONE: "usage", ContentTier.FINGERPRINT: "fingerprint",
                       ContentTier.FULL: "full"}[self.tier]
            from tokenbill import __version__
            header = {"trace_id": self.trace_id, "profile": profile, "identity_mode": "install",
                      "name_key_id": None, "principal_key_id": None,
                      "fp_key_id": key_id(self.key) if self.key is not None else None,
                      "producer": {"name": "tokenbill", "version": __version__,
                                   "adapter": _RECORDER},
                      "created_ms": self.created_ms,
                      "attribution": Attribution(agent_product="api")}
            self._writer = TraceV2Writer(text, header)
        except BaseException:
            stack.close()
            raise
        self._stack = stack
        return self._writer

    def _handle(self, item: tuple[str, Any]) -> None:
        kind, value = item
        writer = self._open()
        if kind == "note":
            writer.note(value)
        elif kind == "call":
            self._write_call(writer, value)
        elif kind == "session":
            if self._first_ts is not None and self._last_ts is not None:
                writer.session(Session(session_key=self.session_key, source_kind=_RECORDER,
                                       attribution=Attribution(agent_product="api"), lanes=(),
                                       started_ms=self._first_ts, ended_ms=self._last_ts))

    def _finish(self) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            try:
                stack.close()
            except Exception as exc:
                self._drop(exc=exc)

    def _write_call(self, writer: TraceV2Writer, result: _Result) -> None:
        call = result.call
        params = call.params
        fingerprint = None
        if call.payload is not None and self.key is not None:
            model = normalize_model(params.model_requested).model
            tools, system, messages = call.payload
            fingerprint, bps, content = fingerprint_blocks(
                snapshot_blocks(tools=tools, system=system, messages=messages), key=self.key,
                tier=self.tier, tokenizer_family=tokenizer_family(model), cache=self._cache)
            params = dataclasses.replace(params, breakpoints=bps)
            for h in sorted(content):
                if h not in self._content_written:
                    writer.content(h, content[h])
                    self._content_written.add(h)
        request = self._request(call, result, params, fingerprint)
        if call.lane_key not in self._lanes_written:
            writer.lane(Lane(lane_key=call.lane_key, session_key=self.session_key,
                             kind=LaneKind.API_RUN, parent_lane_key=None,
                             cache_scope_key="unknown", requests=()))
            self._lanes_written.add(call.lane_key)
        writer.request(request)
        end = call.ts_start_ms + result.duration_ms
        self._first_ts = call.ts_start_ms if self._first_ts is None else min(
            self._first_ts, call.ts_start_ms)
        self._last_ts = end if self._last_ts is None else max(self._last_ts, end)

    def _inferences(self, call: _Call, result: _Result, attempt_id: str
                    ) -> tuple[tuple[Inference, ...], str | None]:
        if result.usage is None:
            return (), None
        clean = sanitize_raw_usage(json.dumps(result.usage, default=lambda _o: None))
        if clean is None:
            return (), None
        served = result.model_served or call.params.model_requested
        geo = clean.get("inference_geo")
        ctx = PricingContext(
            provider="anthropic", channel=call.channel, model=normalize_model(served).model,
            model_raw=served,
            service_tier=clean.get("service_tier") if isinstance(
                clean.get("service_tier"), str) else "standard",
            speed=clean.get("speed") if isinstance(clean.get("speed"), str) else "standard",
            inference_geo=geo if isinstance(geo, str) and geo != "not_available" else None,
            billing_path=call.billing_path)
        try:
            infs, _codes = anthropic_inferences(clean, message_model=served, ctx=ctx,
                                                id_prefix=attempt_id,
                                                usage_source=result.usage_source)
        except UsageError:
            return (), json.dumps(clean, sort_keys=True, separators=(",", ":"))
        return tuple(infs), json.dumps(clean, sort_keys=True, separators=(",", ":"))

    def _request(self, call: _Call, result: _Result, params: RequestParams,
                 fingerprint: ContentFingerprint | None) -> Request:
        locator = f"call:{call.n}"
        rid = request_id_for("anthropic", result.message_id, self.source_id, locator)
        observed = list(call.attempts)
        attempts: list[Attempt] = []
        n_obs = len(observed)
        for i, att in enumerate(observed):
            last = i == n_obs - 1
            aid = stable_id("at", rid, i)
            if last:
                infs, raw = self._inferences(call, result, aid)
                outcome = result.outcome
                status = att.status if att.status is not None else result.http_status
                if outcome is Outcome.OK and status is not None and status >= 400:
                    outcome = Outcome.HTTP_ERROR
                etype = result.error_type
                if etype is None and status is not None and status >= 400:
                    etype = _ERROR_TYPES.get(status, "api_error" if status >= 500
                                             else "invalid_request")
                attempts.append(Attempt(
                    attempt_id=aid, attempt_no=i, ts_start_ms=att.ts_start_ms, ttft_ms=None,
                    duration_ms=att.duration_ms if att.duration_ms is not None
                    else max(0, call.ts_start_ms + result.duration_ms - att.ts_start_ms),
                    outcome=outcome, http_status=status, error_type=etype, retry_layer="sdk",
                    retry_after_ms=att.retry_after_ms, should_retry=att.should_retry,
                    provider_request_id=att.request_id or result.request_id_hint,
                    provider_message_id=result.message_id, model_served=result.model_served,
                    stop_reason=result.stop_reason, inferences=infs,
                    diagnostics=result.diagnostics, applied_edits=result.applied_edits,
                    sdk_retry_count=att.retry_count, raw_usage_json=raw,
                    convention_id=ANTHROPIC_MESSAGES if raw is not None else None))
                continue
            status = att.status
            ok = status is not None and status < 400
            attempts.append(Attempt(
                attempt_id=aid, attempt_no=i, ts_start_ms=att.ts_start_ms, ttft_ms=None,
                duration_ms=att.duration_ms,
                outcome=Outcome.UNKNOWN if ok else (Outcome.HTTP_ERROR if status is not None
                                                    else Outcome.NETWORK_ERROR),
                http_status=status,
                error_type=None if status is None or ok else _ERROR_TYPES.get(
                    status, "api_error" if status >= 500 else "invalid_request"),
                retry_layer="sdk", retry_after_ms=att.retry_after_ms,
                should_retry=att.should_retry, provider_request_id=att.request_id,
                provider_message_id=None, model_served=None, stop_reason=None, inferences=(),
                sdk_retry_count=att.retry_count))
        if not attempts:
            aid = stable_id("at", rid, 0)
            infs, raw = self._inferences(call, result, aid)
            attempts.append(Attempt(
                attempt_id=aid, attempt_no=0, ts_start_ms=call.ts_start_ms, ttft_ms=None,
                duration_ms=result.duration_ms, outcome=result.outcome,
                http_status=result.http_status, error_type=result.error_type, retry_layer=None,
                retry_after_ms=None, should_retry=None,
                provider_request_id=result.request_id_hint,
                provider_message_id=result.message_id, model_served=result.model_served,
                stop_reason=result.stop_reason, inferences=infs, diagnostics=result.diagnostics,
                applied_edits=result.applied_edits, raw_usage_json=raw,
                convention_id=ANTHROPIC_MESSAGES if raw is not None else None))
        return Request(
            request_id=rid, session_key=self.session_key, lane_key=call.lane_key, seq=call.seq,
            attribution=Attribution(agent_product="api", billing_path=call.billing_path),
            params=params, attempts=tuple(attempts), fingerprint=fingerprint,
            appended=call.appended,
            source=SourceRef(adapter=_RECORDER, source_id=self.source_id, locator=locator,
                             fidelity=Fidelity.FULL, priority=_PRIORITY))

    # -- lifecycle ------------------------------------------------------------------------------

    def close(self) -> None:
        """Write the session record, flush the queue and close the file (idempotent)."""
        if self.closed:
            return
        self.closed = True
        self._ensure_thread()
        try:
            self.queue.put(("session", None), timeout=60)
            self.queue.put(_STOP, timeout=60)
        except queue.Full:  # pragma: no cover - the worker is wedged
            self._drop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=120)
        try:
            atexit.unregister(self.close)
        except Exception:  # pragma: no cover
            pass


class _V2StreamManager:
    """``messages.stream(...)`` in trace@2 mode: the SDK's own stream is returned by
    ``__enter__``; a clean exit records the final message, an exception inside the block records
    the partial stream (PARTIAL_STREAM) and propagates."""

    def __init__(self, recorder: _V2Recorder, call: _Call | None, inner: Any) -> None:
        self._recorder = recorder
        self._call = call
        self._inner = inner
        self._stream: Any = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self) -> Any:
        token = _CALL.set(self._call)
        try:
            self._stream = self._inner.__enter__()
        except BaseException as exc:
            self._recorder._fail(self._call, exc)
            raise
        finally:
            _CALL.reset(token)
        return self._stream

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if exc_type is None:
            try:
                final = self._stream.get_final_message()
            except BaseException as err:
                self._recorder._partial(self._call, _snapshot(self._stream), err)
                self._inner.__exit__(exc_type, exc, tb)
                raise
            self._recorder._succeed(self._call, final)
        else:
            self._recorder._partial(self._call, _snapshot(self._stream), exc)
        return self._inner.__exit__(exc_type, exc, tb)

    async def __aenter__(self) -> Any:
        token = _CALL.set(self._call)
        try:
            self._stream = await self._inner.__aenter__()
        except BaseException as exc:
            self._recorder._fail(self._call, exc)
            raise
        finally:
            _CALL.reset(token)
        return self._stream

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if exc_type is None:
            try:
                final = await self._stream.get_final_message()
            except BaseException as err:
                self._recorder._partial(self._call, _snapshot(self._stream), err)
                await self._inner.__aexit__(exc_type, exc, tb)
                raise
            self._recorder._succeed(self._call, final)
        else:
            self._recorder._partial(self._call, _snapshot(self._stream), exc)
        return await self._inner.__aexit__(exc_type, exc, tb)


def _snapshot(stream: Any) -> Any:
    """The accumulated message of an SDK stream (``current_message_snapshot``), or None."""
    try:
        return getattr(stream, "current_message_snapshot", None)
    except Exception:
        return None


class _RawStreamProxy:
    """``messages.create(stream=True)`` in trace@2 mode: the caller iterates the SDK's own event
    stream through this proxy, which accumulates ``message_start`` / ``message_delta`` usage and
    records the call once — FINAL after ``message_stop``, PARTIAL_STREAM when the stream is closed
    or abandoned early (on ``close()``, context exit or exhaustion without ``message_stop``)."""

    def __init__(self, recorder: _V2Recorder, call: _Call, inner: Any, *, is_async: bool) -> None:
        self._recorder = recorder
        self._call = call
        self._inner = inner
        self._is_async = is_async
        self._it: Any = None
        self._message: dict[str, Any] = {}
        self._usage: dict[str, Any] = {}
        self._stopped = False
        self._done = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _observe(self, event: Any) -> None:
        try:
            etype = _obj_get(event, "type")
            if etype == "message_start":
                message = _obj_get(event, "message")
                for key in ("id", "model"):
                    value = _obj_get(message, key)
                    if value is not None:
                        self._message[key] = value
                usage = _usage_plain(_obj_get(message, "usage"))
                if isinstance(usage, dict):
                    self._usage.update(usage)
            elif etype == "message_delta":
                usage = _usage_plain(_obj_get(event, "usage"))
                if isinstance(usage, dict):
                    self._usage.update({k: v for k, v in usage.items() if v is not None})
                stop = _obj_get(_obj_get(event, "delta"), "stop_reason")
                if stop is not None:
                    self._message["stop_reason"] = stop
            elif etype == "message_stop":
                self._stopped = True
        except Exception:
            pass

    def _record(self) -> None:
        if self._done:
            return
        self._done = True
        message = dict(self._message)
        message["usage"] = dict(self._usage) if self._usage else None
        if self._stopped:
            self._recorder._succeed(self._call, message)
        else:
            self._recorder._partial(self._call, message if self._usage else None, None)

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        if self._it is None:
            self._it = iter(self._inner)
        try:
            event = next(self._it)
        except StopIteration:
            self._record()
            raise
        self._observe(event)
        return event

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        if self._it is None:
            self._it = self._inner.__aiter__()
        try:
            event = await self._it.__anext__()
        except StopAsyncIteration:
            self._record()
            raise
        self._observe(event)
        return event

    def close(self) -> Any:
        """Close the SDK stream; an unfinished stream is recorded as PARTIAL_STREAM."""
        try:
            closer = getattr(self._inner, "close", None)
            return closer() if callable(closer) else None
        finally:
            self._record()

    async def aclose(self) -> Any:
        """Close the async SDK stream; an unfinished stream is recorded as PARTIAL_STREAM."""
        try:
            closer = getattr(self._inner, "close", None) or getattr(self._inner, "aclose", None)
            if callable(closer):
                result = closer()
                if inspect.isawaitable(result):
                    result = await result
                return result
            return None
        finally:
            self._record()

    def __enter__(self) -> Any:
        enter = getattr(self._inner, "__enter__", None)
        if callable(enter):
            enter()
        return self

    def __exit__(self, *exc: Any) -> Any:
        try:
            exit_ = getattr(self._inner, "__exit__", None)
            return exit_(*exc) if callable(exit_) else None
        finally:
            self._record()

    async def __aenter__(self) -> Any:
        enter = getattr(self._inner, "__aenter__", None)
        if callable(enter):
            await enter()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        try:
            exit_ = getattr(self._inner, "__aexit__", None)
            return await exit_(*exc) if callable(exit_) else None
        finally:
            self._record()


@contextmanager
def recording(path: str | Path, run_id: str | None = None, **kw: Any) -> Iterator[Recorder]:
    """Sugar: ``with recording("trace.jsonl") as rec: client = rec.wrap(client)``.

    Keyword arguments go to :class:`Recorder`; the recorder is closed (trace@2 flushed) on exit.
    """
    recorder = Recorder(path, run_id=run_id, **kw)
    try:
        yield recorder
    finally:
        recorder.close()

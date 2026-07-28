"""Trace schema, JSONL IO, and the canonical rendering used for byte comparison.

A *trace* is the sequence of API calls one agent run made: the full request
payload (model, system, tools, messages) plus the real billed usage. This
module owns the ``tokenbill/trace@1`` schema, JSONL reading/writing with
precise :class:`~tokenbill.common.TraceError` messages, and the canonical
rendering every downstream byte comparison (prefix reuse, divergence
detection, cache simulation) is built on.

Rendering follows the provider's documented order — tools, then system, then
messages — and serializes structured parts with ``common.canonical_json``, so
a dict-key-order difference can never masquerade as a prompt change.
"""

from __future__ import annotations

import json
import math
import weakref
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NamedTuple

from tokenbill.common import TraceError, canonical_json

__all__ = [
    "CHARS_PER_TOKEN",
    "SCHEMA",
    "Call",
    "Run",
    "Segment",
    "Usage",
    "approx_tokens",
    "common_prefix_chars",
    "diverging_segment",
    "read_trace",
    "render_segments",
    "rendered_text",
    "write_trace",
]

SCHEMA: Final[str] = "tokenbill/trace@1"

# Average characters per token for the English/code mix agents send to Claude
# models. Deliberately approximate: Token Bill uses it ONLY to split a call's
# exact billed totals proportionally across segments and to place divergence
# points in token terms — never to produce a billed number. A chars-based
# ratio is tokenizer-agnostic (tiktoken would silently be the wrong tokenizer
# for Claude), and the residual error washes out because every approximate
# split is rescaled to sum to the call's exact billed total.
CHARS_PER_TOKEN: Final[float] = 3.7


def approx_tokens(text: str) -> float:
    """Approximate token count of *text*: ``len(text) / CHARS_PER_TOKEN``.

    Attribution-only. Every number derived from this is labeled "approx"
    wherever it surfaces; billed dollar totals never touch it.
    """
    return len(text) / CHARS_PER_TOKEN


@dataclass(frozen=True)
class Usage:
    """Real billed usage for one API call, exactly as the provider reported it."""

    input_tokens: int  # uncached input (per provider semantics)
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int

    @property
    def total_input(self) -> int:
        """Sum of the three input components (uncached + cache read + cache write)."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens


@dataclass(frozen=True)
class Call:
    """One API call: the request payload as sent, plus the billed result."""

    run_id: str
    index: int  # 0-based position within the run
    ts: float  # unix seconds at request time (drives TTL simulation)
    model: str
    system: str  # rendered system prompt ("" if none)
    tools: tuple[dict, ...]  # tool definitions as sent, order preserved
    messages: tuple[dict, ...]  # messages as sent, order preserved (role + content;
    # content is str or list of content-block dicts)
    cache_breakpoints: int  # how many cache_control markers the request carried
    usage: Usage
    stop_reason: str


@dataclass(frozen=True)
class Run:
    """All calls sharing a ``run_id``, sorted by ``index``."""

    run_id: str
    calls: tuple[Call, ...]


class Segment(NamedTuple):
    """One canonically rendered piece of a request, in provider render order."""

    kind: str  # "tools" | "system" | "message"
    label: str  # human label, e.g. "tools", "system", "messages[3]"
    text: str  # canonical text — the byte-comparison substrate


# Rendering is the pipeline's hot path: the analyzer, breaker detection, and
# every simulator replay all consume the same canonical rendering, and
# re-serializing every call's full payload ~20x turns a 1,500-call analysis
# into minutes. Call is frozen and its payload dicts are part of the frozen
# contract (never mutated after construction), so each instance is rendered
# exactly once. The cache is keyed by id() with a weakref finalizer evicting
# the entry when the Call is collected — finalizers run during collection,
# before the id can be reused by a new object.
_RENDER_CACHE: dict[int, tuple[tuple[Segment, ...], str]] = {}

#: Component-level caches keyed by id() of a call's ``tools``/``messages``
#: tuple. Repaired calls (``breakers.repaired_calls``) share those tuples
#: with the originals via ``dataclasses.replace``, so re-rendering a repaired
#: run costs only its actually-changed bytes. Each value keeps a strong
#: reference to the tuple, so the id key cannot be reused while the entry
#: lives; the entry is evicted when the first Call that rendered it is
#: collected (early eviction for other sharers is harmless — they re-render).
#: One cache per component kind: ``()`` is an interned singleton, so an empty
#: tools tuple and an empty messages tuple share the same id and MUST NOT
#: share a cache slot.
_TOOLS_CACHE: dict[int, tuple[tuple, str]] = {}
_MESSAGES_CACHE: dict[int, tuple[tuple, tuple[Segment, ...]]] = {}


def _part(call: Call, part: tuple, cache: dict[int, tuple[tuple, Any]], render: Any) -> Any:
    key = id(part)
    hit = cache.get(key)
    if hit is None:
        hit = (part, render(part))
        cache[key] = hit
        weakref.finalize(call, cache.pop, key, None)
    return hit[1]


def _render_tools(tools: tuple[dict, ...]) -> str:
    return canonical_json(list(tools))


def _render_messages(messages: tuple[dict, ...]) -> tuple[Segment, ...]:
    return tuple(
        Segment("message", f"messages[{i}]", canonical_json(message))
        for i, message in enumerate(messages)
    )


def _rendered(call: Call) -> tuple[tuple[Segment, ...], str]:
    """Segments and their concatenation for *call*, computed once per instance."""
    key = id(call)
    cached = _RENDER_CACHE.get(key)
    if cached is None:
        segments = (
            Segment("tools", "tools", _part(call, call.tools, _TOOLS_CACHE, _render_tools)),
            Segment("system", "system", call.system),
            *_part(call, call.messages, _MESSAGES_CACHE, _render_messages),
        )
        cached = (segments, "".join(segment.text for segment in segments))
        _RENDER_CACHE[key] = cached
        weakref.finalize(call, _RENDER_CACHE.pop, key, None)
    return cached


def render_segments(call: Call) -> list[Segment]:
    """Render *call* into ordered segments: tools, system, then one per message.

    This mirrors the provider's documented render order (tools -> system ->
    messages). Structured parts go through ``canonical_json`` so key order in
    the source dicts never affects the rendered bytes. Empty parts (no tools,
    empty system) still occupy their position, keeping segment indexes stable.
    Renderings are cached per Call instance (Call and its payload are frozen);
    the returned list is a fresh copy each time.
    """
    return list(_rendered(call)[0])


def rendered_text(call: Call) -> str:
    """Concatenation of the canonical segment texts for *call* (cached per Call)."""
    return _rendered(call)[1]


def common_prefix_chars(a: Call, b: Call) -> int:
    """Length in characters of the longest common prefix of the two renderings."""
    ta, tb = rendered_text(a), rendered_text(b)
    n = min(len(ta), len(tb))
    if n == 0 or ta[0] != tb[0]:
        return 0
    if ta[:n] == tb[:n]:
        # The common agent pattern — the next call extends the previous one —
        # resolves in a single C-speed comparison.
        return n
    # Gallop (doubling probes), then binary-search the last doubling. Probes
    # compare from the known-matched offset, so total slice-copy work stays
    # proportional to the shared prefix — not to the full rendering, which a
    # plain binary search from zero would re-copy on every probe.
    lo, step = 1, 1
    while True:
        probe = min(lo + step, n)
        if ta[lo:probe] == tb[lo:probe]:
            lo = probe
            step <<= 1
        else:
            hi = probe - 1
            break
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ta[lo:mid] == tb[lo:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def diverging_segment(prev: Call, cur: Call) -> tuple[int, str] | None:
    """Index and kind of the first segment whose text differs between the calls.

    Returns ``None`` when *cur* extends *prev* (every segment of *prev* is
    reproduced byte-identically, in order, at the same position). When *cur*
    is a truncation of *prev* (all shared positions match but *prev* has more
    segments), the divergence is the first missing position, reported with the
    kind of *prev*'s segment there.
    """
    prev_segments = render_segments(prev)
    cur_segments = render_segments(cur)
    for i, (p, c) in enumerate(zip(prev_segments, cur_segments, strict=False)):
        if p.text != c.text:
            return (i, c.kind)
    if len(cur_segments) < len(prev_segments):
        i = len(cur_segments)
        return (i, prev_segments[i].kind)
    return None


# --------------------------------------------------------------------------
# JSONL IO
# --------------------------------------------------------------------------

_USAGE_FIELDS: Final[tuple[str, ...]] = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)

#: Ceiling for trace integers (token counts, indexes, breakpoint counts).
#: 2**53 is where float arithmetic on ints turns lossy; anything larger is a
#: corrupted trace, not a bill — real providers report nowhere near this.
_MAX_TRACE_INT: Final[int] = 2**53


def _reject_nonfinite(token: str) -> Any:
    """``parse_constant`` hook: NaN/Infinity are not valid JSON (RFC 8259).

    Accepting them would silently disable TTL simulation (NaN comparisons are
    always False) and produce files ``write_trace`` could never round-trip.
    """
    raise ValueError(f"{token} is not valid JSON (RFC 8259 forbids non-finite numbers)")


def _get(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise TraceError(f"{where}: missing field '{key}'")
    return obj[key]


def _typed(
    obj: dict[str, Any],
    key: str,
    where: str,
    expected: type | tuple[type, ...],
    name: str,
) -> Any:
    value = _get(obj, key, where)
    if isinstance(value, bool) or not isinstance(value, expected):
        raise TraceError(f"{where}: field '{key}' must be {name}, got {type(value).__name__}")
    return value


def _non_negative_int(obj: dict[str, Any], key: str, where: str) -> int:
    value = _typed(obj, key, where, int, "an int")
    if value < 0:
        raise TraceError(f"{where}: field '{key}' must be non-negative, got {value}")
    if value > _MAX_TRACE_INT:
        raise TraceError(
            f"{where}: field '{key}' is implausibly large ({value}); "
            f"the maximum accepted value is 2**53"
        )
    return int(value)


def _clean_str(obj: dict[str, Any], key: str, where: str) -> str:
    """A string field that must survive every output sink: no lone surrogates.

    ``\\ud800`` is a legal JSON *escape*, so ``json.loads`` happily decodes
    it — but the result cannot be encoded back to UTF-8, which would crash
    the terminal summary and the HTML report long after parsing. Reject at
    IO, as this module's error contract promises.
    """
    value = _typed(obj, key, where, str, "a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TraceError(
            f"{where}: field '{key}' contains an unpaired surrogate "
            f"(\\u{ord(value[exc.start]):04x} at char {exc.start}) and cannot "
            "be encoded as UTF-8"
        ) from exc
    return value


def _dict_tuple(obj: dict[str, Any], key: str, where: str) -> tuple[dict, ...]:
    raw = _typed(obj, key, where, list, "a list")
    items: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TraceError(
                f"{where}: field '{key}[{i}]' must be an object, got {type(item).__name__}"
            )
        items.append(item)
    return tuple(items)


def _parse_usage(obj: dict[str, Any], where: str) -> Usage:
    raw = _typed(obj, "usage", where, dict, "an object")
    values: dict[str, int] = {}
    for key in _USAGE_FIELDS:
        if key not in raw:
            raise TraceError(f"{where}: missing field 'usage.{key}'")
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TraceError(
                f"{where}: field 'usage.{key}' must be an int, got {type(value).__name__}"
            )
        if value < 0:
            raise TraceError(f"{where}: field 'usage.{key}' must be non-negative, got {value}")
        if value > _MAX_TRACE_INT:
            raise TraceError(
                f"{where}: field 'usage.{key}' is implausibly large ({value}); "
                f"the maximum accepted value is 2**53"
            )
        values[key] = value
    return Usage(**values)


def _finite_ts(obj: dict[str, Any], where: str) -> float:
    value = float(_typed(obj, "ts", where, (int, float), "a number"))
    if not math.isfinite(value):
        # NaN/Infinity ts would silently defeat every TTL comparison in the
        # simulator, collapsing optimal-cache to the no-cache price.
        raise TraceError(f"{where}: field 'ts' must be a finite number, got {value}")
    return value


def _parse_call(obj: dict[str, Any], where: str) -> Call:
    return Call(
        run_id=_clean_str(obj, "run_id", where),
        index=_non_negative_int(obj, "index", where),
        ts=_finite_ts(obj, where),
        model=_clean_str(obj, "model", where),
        system=_clean_str(obj, "system", where),
        tools=_dict_tuple(obj, "tools", where),
        messages=_dict_tuple(obj, "messages", where),
        cache_breakpoints=_non_negative_int(obj, "cache_breakpoints", where),
        usage=_parse_usage(obj, where),
        stop_reason=_clean_str(obj, "stop_reason", where),
    )


def read_trace(path: str | Path) -> list[Run]:
    """Read a ``tokenbill/trace@1`` JSONL file into runs grouped by ``run_id``.

    Runs keep first-appearance order; calls keep file order, which validation
    guarantees is strictly increasing ``index`` order per run. Every rejection
    raises :class:`TraceError` naming the file, the 1-based line, and the
    offending field. Blank lines are skipped.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise TraceError(f"{p}: cannot read trace file ({exc})") from exc
    grouped: dict[str, list[Call]] = {}
    last_index: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{p}, line {lineno}"
        try:
            obj = json.loads(line, parse_constant=_reject_nonfinite)
        except json.JSONDecodeError as exc:
            raise TraceError(f"{where}: invalid JSON ({exc.msg})") from exc
        except (RecursionError, ValueError) as exc:
            # RecursionError: pathologically deep nesting inside json.loads.
            # ValueError: _reject_nonfinite (NaN/Infinity literals). Both must
            # surface as TraceError per this function's error contract.
            raise TraceError(f"{where}: could not parse JSON ({exc})") from exc
        if not isinstance(obj, dict):
            raise TraceError(f"{where}: expected a JSON object, got {type(obj).__name__}")
        schema = obj.get("schema")
        if schema is None:
            raise TraceError(f"{where}: missing field 'schema'")
        if schema != SCHEMA:
            raise TraceError(f"{where}: unsupported schema {schema!r}; supported: {SCHEMA!r}")
        call = _parse_call(obj, where)
        prev = last_index.get(call.run_id)
        if prev is not None and call.index <= prev:
            raise TraceError(
                f"{where}: non-monotonic index {call.index} for run {call.run_id!r} "
                f"(previous call had index {prev})"
            )
        last_index[call.run_id] = call.index
        grouped.setdefault(call.run_id, []).append(call)
    return [Run(run_id=run_id, calls=tuple(calls)) for run_id, calls in grouped.items()]


def write_trace(path: str | Path, calls: Iterable[Call]) -> None:
    """Write *calls* as ``tokenbill/trace@1`` JSONL, one call per line.

    Lines are serialized with ``canonical_json`` so output is deterministic
    for identical inputs. Non-finite numbers (NaN/Infinity) raise
    :class:`TraceError`: they are not valid JSON, and emitting them would
    produce a file :func:`read_trace` rejects.
    """
    lines = []
    for call in calls:
        record = {
            "schema": SCHEMA,
            "run_id": call.run_id,
            "index": call.index,
            "ts": call.ts,
            "model": call.model,
            "system": call.system,
            "tools": list(call.tools),
            "messages": list(call.messages),
            "cache_breakpoints": call.cache_breakpoints,
            "usage": {key: getattr(call.usage, key) for key in _USAGE_FIELDS},
            "stop_reason": call.stop_reason,
        }
        try:
            lines.append(canonical_json(record))
        except ValueError as exc:
            raise TraceError(
                f"{path}: call index {call.index} of run {call.run_id!r} contains "
                f"a non-finite number ({exc}); refusing to write invalid JSON"
            ) from exc
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

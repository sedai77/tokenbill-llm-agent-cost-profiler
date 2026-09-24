"""The ``tokenbill/trace@2`` interchange format: codec and adapter (SPEC §4; package TRACE).

One JSONL format (``.gz`` allowed), one record per line, canonical JSON (sorted keys, no
whitespace), files created ``0600``. Every line carries ``"schema": "tokenbill/trace@2"`` and a
``"rec"`` discriminator; the first line is the ``header``. Three profiles (§4.1): ``usage``
(content tier none: no ``blocks``, no ``request.fp``, no ``content``), ``fingerprint`` (+
``blocks`` records and delta-encoded ``request.fp``) and ``full`` (+ ``content`` records; local
only).

**Closed key sets** (§4.3, CORE-AMENDMENTS A-3): the keys of every record and nested object are
derived from the core dataclasses with :func:`~tokenbill.core.records.record_fields` (plus ``rec`` /
``schema``; ``request.fp`` replaces ``Request.fingerprint``; ``attempt.raw_usage`` — an object of
numbers, booleans, nulls and allowlisted enum strings (``service_tier``, ``speed``,
``inference_geo``, ``type``, ``model`` and the keys of
:data:`~tokenbill.core.records.RAW_USAGE_ENUMS`) — replaces ``Attempt.raw_usage_json``). Fields
appended to core records later (GitHub Copilot, wave 1.5) are therefore accepted without a codec
change, and ``to_json`` leaves them out while at their default.

**Reader rules** (strict: :class:`~tokenbill.core.errors.SourceError`; lenient: the line is
quarantined with a content-free reason): unknown ``rec`` values or keys (``unknown_key:<name>``);
strings longer than 256 characters (except ``content.text``); floats anywhere, integers beyond
``2**53`` in magnitude, negative integers outside ``*_nano`` money fields and event attrs;
principals must be ``p_``/``c_``/``r_`` pseudonyms consistent with the header (``r_`` only
in ``central`` identity mode, ``c_`` only in ``two-stage`` with a ``principal_key_id``, ``p_``
only with a ``principal_key_id``, never ``@``); ``h_`` values only with a header ``name_key_id``;
``repo``, ``api_key_id`` and ``cwd_key`` must be ``h_<20 hex>``; ``attribution.extra`` keys in
``EXTRA_KEYS``; profile rules; delta-encoded fingerprints must resolve (``blocks`` definitions
precede their use, a ``parent`` precedes its child); a missing header quarantines every line
(``missing:header``).

**Fingerprints** are delta-encoded: a request's block list is the first ``keep`` blocks of its
``parent`` request's list plus the blocks named by ``append`` (hashes defined by earlier ``blocks``
records; a later definition of the same hash replaces it for later requests). ``lookback_pos`` is
positional, so readers recompute it from the reconstructed list (CONTRACT-CHANGE-SYNTH-FLEET-1,
option 1) and writers leave it out of ``blocks`` records. ``fp.markers`` mirror
``params.breakpoints``.

Round trip: :func:`write_trace_v2` → :class:`TraceV2Adapter` / :func:`iter_trace_v2` →
:func:`write_trace_v2` is byte-identical. The reader decodes with its own constructors (ruling
R-E3: ``core.records.from_json`` stays the reference in tests). The writer validates every line with
the reader before writing it, so it never produces a file the reader rejects; provider usage objects
are sanitized (disallowed strings and floats become ``null``). Fingerprints and content found in a
file are kept as recorded (the producer opted into that tier; the reader derives nothing from
content); ``content`` records are validated and yielded by :func:`iter_trace_v2` but never enter an
:class:`~tokenbill.core.types.IngestResult`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

from tokenbill.core.conventions import (
    ANTHROPIC_MESSAGES,
    anthropic_inferences,
    get_convention,
    normalize,
)
from tokenbill.core.errors import ContractViolation, PricingError, SourceError, UsageError
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.jsonl import iter_lines, open_private
from tokenbill.core.records import (
    RAW_USAGE_ENUMS,
    RAW_USAGE_NUMERIC,
    AppendedItem,
    Attempt,
    Attribution,
    BlockRef,
    Breakpoint,
    CacheDiagnostic,
    ContentFingerprint,
    CostLine,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    OutcomeAggregate,
    PricingContext,
    Request,
    RequestParams,
    Session,
    SourceRef,
    UsageAggregate,
    UsageBuckets,
    UsageSource,
    WorkloadClass,
    record_fields,
    to_json,
)
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    SourceInfo,
)

__all__ = [
    "ADAPTER_NAME",
    "CAPABILITIES",
    "IDENTITY_MODES",
    "MAX_STRING",
    "PROFILES",
    "SCHEMA",
    "ContentItem",
    "TraceHeader",
    "TraceV2Adapter",
    "TraceV2Writer",
    "iter_trace_v2",
    "make_header",
    "sanitize_raw_usage",
    "write_trace_v2",
]

SCHEMA = "tokenbill/trace@2"
ADAPTER_NAME = "trace@2"
PROFILES = ("usage", "fingerprint", "full")
#: Header identity modes: the SPEC §4.2 three plus ``central-ingest`` (files written at the central
#: host, whose ``p_`` values are under the org key).
IDENTITY_MODES = ("central", "two-stage", "install", "central-ingest")
#: Longest string value (and key) allowed anywhere except ``content.text`` (§4.3).
MAX_STRING = 256
_MAX_INT = 2**53
_RAW_USAGE_MAX = 8 * 1024
#: Every capability a trace@2 file can carry (§5.1); a result reports the ones actually present.
CAPABILITIES = frozenset({
    "usage_sequence", "timing", "ttft", "ttl_split", "iterations", "attempts", "diagnostics",
    "appended", "events", "human_prompts", "lanes_exact", "params", "blocks", "attribution.team",
    "workload", "aggregates", "cost", "outcomes", "quota_state",
})

_TRACE_ID_RE = re.compile(r"t_[A-Za-z0-9_-]{1,64}\Z")
_KEY_ID_RE = re.compile(r"k_[A-Za-z0-9_-]{1,64}\Z")
_HASH_RE = re.compile(r"h_[0-9a-f]{20}\Z")
_BLOCK_HASH_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DQ_CODE_RE = re.compile(r"dq\.[A-Za-z0-9_.:-]{1,120}\Z")
_KEY_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_RAW_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_ENUM_VALUE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_MODEL_VALUE_RE = re.compile(r"[A-Za-z0-9_.:+/@\[\]-]{1,128}\Z")
_CV_FIELD_RE = re.compile(r"[A-Za-z]+\.([A-Za-z_]+):")
# raw-line pre-checks: any JSON string literal longer than MAX_STRING raw characters; any long
# digit run (a number that may exceed 2**53); any negative number; any h_ / p_ / c_ value; a
# possible escaped lone surrogate
_LONG_STRING = re.compile(rb'"(?:[^"\\]|\\.){%d,}"' % (MAX_STRING + 1))
_BIG_OR_NEGATIVE = re.compile(rb"[0-9]{16,}|[:,\[]-[0-9]")
_NAME_HASH = re.compile(rb'"h_[0-9a-f]{20}"')
_PRINCIPAL_HASH = re.compile(rb'"[pc]_[0-9a-f]{20}"')
_SURROGATE = re.compile(rb"\\[uU][dD][89a-fA-F]")

#: Strings allowed in a raw provider usage object, by key (§4.2) — plus RAW_USAGE_ENUMS keys.
_RAW_STRING_KEYS = frozenset({"service_tier", "speed", "inference_geo", "type"})
_RAW_MODEL_KEYS = frozenset({"model"})

_FINGERPRINT_PROFILES = frozenset({"fingerprint", "full"})
_RENORM_KINDS = frozenset({InferenceKind.MESSAGE, InferenceKind.COMPACTION, InferenceKind.ADVISOR,
                           InferenceKind.FALLBACK, InferenceKind.FALLBACK_DECLINED,
                           InferenceKind.OTHER})
_RENORM_SOURCES = frozenset({UsageSource.FINAL, UsageSource.MESSAGE_START_ONLY,
                             UsageSource.PARTIAL_STREAM})
_SEVERITIES = frozenset({"info", "warn", "error"})


# ---------------------------------------------------------------------------------------------
# closed key sets (derived from the core dataclasses, CORE-AMENDMENTS C-4 / A-3)
# ---------------------------------------------------------------------------------------------


def _required(cls: type) -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(cls)
                     if f.default is dataclasses.MISSING
                     and f.default_factory is dataclasses.MISSING)


@dataclass(frozen=True, slots=True)
class _Spec:
    allowed: frozenset[str]
    required: frozenset[str]


def _spec(cls: type, *, drop: Iterable[str] = (), add: Iterable[str] = (),
          add_required: Iterable[str] = ()) -> _Spec:
    dropped = frozenset(drop)
    allowed = (record_fields(cls) - dropped) | frozenset(add) | frozenset(add_required)
    required = (_required(cls) - dropped) | frozenset(add_required)
    return _Spec(frozenset(allowed), frozenset(required))


_REC = ("rec", "schema")
_S_ATTRIBUTION = _spec(Attribution)
_S_PARAMS = _spec(RequestParams)
_S_BREAKPOINT = _spec(Breakpoint)
_S_APPENDED = _spec(AppendedItem)
_S_SOURCE = _spec(SourceRef)
_S_DIAGNOSTIC = _spec(CacheDiagnostic)
_S_USAGE = _spec(UsageBuckets)
_S_PRICING = _spec(PricingContext)
_S_INFERENCE = _spec(Inference)
_S_ATTEMPT = _spec(Attempt, drop=("raw_usage_json",), add=("raw_usage",))
_S_BLOCKREF = _spec(BlockRef)
_S_SESSION = _spec(Session, drop=("lanes",), add_required=_REC)
_S_LANE = _spec(Lane, drop=("requests", "events"), add_required=_REC)
_S_REQUEST = _spec(Request, drop=("fingerprint",), add=("fp",), add_required=_REC)
_S_EVENT = _spec(LaneEvent, add_required=_REC)
_S_AGGREGATE = _spec(UsageAggregate, add_required=_REC)
_S_COST_LINE = _spec(CostLine, add_required=_REC)
_S_OUTCOME = _spec(OutcomeAggregate, add_required=_REC)
_S_DQ = _spec(DataQualityNote, drop=("figure",), add_required=_REC)
_S_HEADER = _Spec(
    frozenset({"rec", "schema", "trace_id", "profile", "name_key_id", "principal_key_id",
               "fp_key_id", "identity_mode", "producer", "created_ms", "attribution"}),
    frozenset({"rec", "schema", "trace_id", "profile", "identity_mode"}))
_S_PRODUCER = _Spec(frozenset({"name", "version", "adapter"}), frozenset())
_S_BLOCKS = _Spec(frozenset({"rec", "schema", "key_id", "blocks"}),
                  frozenset({"rec", "schema", "key_id", "blocks"}))
_S_CONTENT = _Spec(frozenset({"rec", "schema", "h", "text"}),
                   frozenset({"rec", "schema", "h", "text"}))
_S_FP = _Spec(frozenset({"parent", "keep", "append", "markers", "tier_end"}),
              frozenset({"parent", "keep", "append", "tier_end"}))
_RECS = frozenset({"header", "session", "lane", "blocks", "request", "event", "aggregate",
                   "cost_line", "outcome", "dq", "content"})


# ---------------------------------------------------------------------------------------------
# public value types of the stream
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceHeader:
    """The ``header`` record of a trace@2 file (§4.2)."""

    trace_id: str
    profile: str                     # "usage" | "fingerprint" | "full"
    identity_mode: str               # IDENTITY_MODES
    name_key_id: str | None = None   # key of every h_ value (→ SourceInfo.name_key_id)
    principal_key_id: str | None = None   # key of every p_/c_ value (→ SourceInfo)
    fp_key_id: str | None = None     # key of block hashes (fingerprint / full only)
    producer: tuple[tuple[str, str], ...] = ()   # sorted (name | version | adapter, value)
    created_ms: int = 0
    attribution: Attribution = Attribution()    # attribution defaults of the producer


@dataclass(frozen=True, slots=True)
class ContentItem:
    """A ``content`` record of a ``full`` profile file: block hash → wire text (local only)."""

    h: str
    text: str


class _Bad(Exception):
    """A record violates the format; ``reason`` is content-free (§5.1 quarantine reasons)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _key_name(k: object) -> str:
    return k if isinstance(k, str) and _KEY_NAME_RE.match(k) else "?"


def _obj(v: Any, what: str) -> dict:
    if type(v) is not dict:
        raise _Bad(f"bad_type:{what}")
    return v


def _list(v: Any, what: str) -> list:
    if type(v) is not list:
        raise _Bad(f"bad_type:{what}")
    return v


def _check(d: dict, spec: _Spec) -> None:
    keys = d.keys()
    if not keys <= spec.allowed:
        raise _Bad("unknown_key:" + _key_name(min(k for k in keys if k not in spec.allowed)))
    if not spec.required <= keys:
        raise _Bad("missing:" + min(spec.required - keys))


def _cv(exc: ContractViolation, what: str) -> _Bad:
    m = _CV_FIELD_RE.match(str(exc))
    return _Bad(f"bad_type:{m.group(1)}" if m else f"bad_type:{what}")


def _build(cls: type, d: dict, spec: _Spec, what: str) -> Any:
    _check(d, spec)
    try:
        return cls(**d)
    except ContractViolation as exc:
        raise _cv(exc, what) from None
    except TypeError:  # pragma: no cover - keys were checked against the dataclass fields
        raise _Bad(f"bad_type:{what}") from None


# ---------------------------------------------------------------------------------------------
# raw usage objects
# ---------------------------------------------------------------------------------------------


def _raw_string_ok(key: str | None, value: str) -> bool:
    if key in _RAW_MODEL_KEYS:
        return bool(_MODEL_VALUE_RE.match(value))
    enum = RAW_USAGE_ENUMS.get(key) if key is not None else None
    if enum is not None:
        return value in enum if enum else bool(_ENUM_VALUE_RE.match(value))
    return key in _RAW_STRING_KEYS and bool(_ENUM_VALUE_RE.match(value))


def _raw_value(v: Any, key: str | None, depth: int, fix: bool) -> Any:
    """Validate (or, with *fix*, sanitize) one raw usage value; returns the (sanitized) value."""
    if depth > 8:
        if fix:
            return None
        raise _Bad("bad_usage")
    t = type(v)
    if v is None or t is bool:
        if key in RAW_USAGE_NUMERIC and t is bool:
            if fix:
                return None
            raise _Bad("bad_usage")
        return v
    if t is int:
        if 0 <= v <= _MAX_INT:
            return v
        if fix:
            return None
        raise _Bad("bad_usage")
    if t is str:
        if key not in RAW_USAGE_NUMERIC and _raw_string_ok(key, v):
            return v
        if fix:
            return None
        raise _Bad("bad_usage")
    if t is dict:
        out = {}
        for k, x in v.items():
            if not isinstance(k, str) or not _RAW_KEY_RE.match(k):
                if fix:
                    continue
                raise _Bad("bad_usage")
            out[k] = _raw_value(x, k, depth + 1, fix)
        return out
    if t is list:
        return [_raw_value(x, key, depth + 1, fix) for x in v]
    if fix:
        return None
    raise _Bad("bad_usage")


def _raw_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sanitize_raw_usage(raw_usage_json: str | None) -> dict | None:
    """The ``raw_usage`` object a writer emits for ``Attempt.raw_usage_json``: numbers in
    ``[0, 2**53]``, booleans, nulls and allowlisted enum strings kept; any other value (a free
    string, a float, a negative number) becomes ``null``; keys that are not identifier-like are
    dropped. None when the text is not a JSON object or exceeds 8 KiB after sanitizing."""
    if raw_usage_json is None:
        return None
    try:
        obj = json.loads(raw_usage_json, parse_float=lambda _s: None,
                         parse_constant=lambda _s: None)
    except (ValueError, RecursionError):
        return None
    if type(obj) is not dict:
        return None
    clean = _raw_value(obj, None, 0, True)
    if len(_raw_json(clean)) > _RAW_USAGE_MAX:
        return None
    return clean


# ---------------------------------------------------------------------------------------------
# the reader
# ---------------------------------------------------------------------------------------------


class _Float:
    """Marker for a JSON number with a fraction or exponent (floats are rejected, §4.3)."""

    __slots__ = ()


_FLOAT = _Float()


def _find_bad_number(obj: Any, key: str, in_attrs: bool) -> str | None:
    """The key of the first float, out-of-range or misplaced negative number in *obj*."""
    stack: list[tuple[Any, str, bool]] = [(obj, key, in_attrs)]
    while stack:
        v, k, attrs = stack.pop()
        t = type(v)
        if v is _FLOAT:
            return k
        if t is int:
            if v > _MAX_INT or v < -_MAX_INT:
                return k
            if v < 0 and not attrs and not k.endswith("_nano"):
                return k
        elif t is dict:
            for kk, x in v.items():
                stack.append((x, kk if isinstance(kk, str) else "?", attrs or kk == "attrs"))
        elif t is list:
            for x in v:
                stack.append((x, k, attrs))
    return None


def _find_long_string(obj: Any, key: str) -> str | None:
    stack: list[tuple[Any, str]] = [(obj, key)]
    while stack:
        v, k = stack.pop()
        if type(v) is str:
            if len(v) > MAX_STRING:
                return k
        elif type(v) is dict:
            for kk, x in v.items():
                if isinstance(kk, str) and len(kk) > MAX_STRING:
                    return "?"
                stack.append((x, kk))
        elif type(v) is list:
            stack.extend((x, k) for x in v)
    return None


def _lone_surrogate(obj: Any) -> bool:
    stack = [obj]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                return True
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return False


def _reject_constant(_token: str) -> Any:
    raise ValueError("non-finite number")


class _Reader:
    """Stateful line decoder: header, block definitions, resolved fingerprints of earlier requests.
    """

    def __init__(self, *, name: str, source_id: str, lenient: bool) -> None:
        self.name = name
        self.source_id = source_id
        self.lenient = lenient
        self.header: TraceHeader | None = None
        self.header_failed = False
        self.first = True
        self.defs: dict[str, BlockRef] = {}
        self.resolved: dict[str, tuple[BlockRef, ...]] = {}
        self.seen_requests: set[str] = set()
        self.n_floats = 0
        self.n_blocks = 0

    # -- entry --------------------------------------------------------------------------------

    def feed(self, line_no: int, raw: bytes) -> object | QuarantineItem:
        """Decode one line: a record object or a :class:`QuarantineItem` (strict: raises)."""
        try:
            try:
                return self._decode(raw)
            except (TypeError, ValueError, KeyError, AttributeError, IndexError, RecursionError,
                    OverflowError):  # defensive: a shape no specific check anticipated
                raise _Bad("bad_type:record") from None
        except _Bad as bad:
            if not self.lenient:
                raise SourceError(f"{self.name}: line {line_no}: {bad.reason}") from None
            return QuarantineItem(source_id=self.source_id, locator=f"line:{line_no}",
                                  reason=bad.reason)

    def _parse_float(self, _token: str) -> _Float:
        self.n_floats += 1
        return _FLOAT

    def _decode(self, raw: bytes) -> object:
        first = self.first
        self.first = False
        if not raw:
            raise _Bad("oversize_line")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _Bad("bad_json") from None
        if first and text.startswith("﻿"):
            text = text[1:]
        floats = self.n_floats
        try:
            d = json.loads(text, parse_float=self._parse_float, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise _Bad("bad_json") from None
        if type(d) is not dict:
            raise _Bad("not_object")
        if _SURROGATE.search(raw) and _lone_surrogate(d):
            raise _Bad("bad_json")
        rec = d.get("rec")
        if d.get("schema") != SCHEMA:
            raise _Bad("missing:schema" if "schema" not in d else "bad_type:schema")
        if not isinstance(rec, str) or rec not in _RECS:
            raise _Bad("missing:rec" if rec is None else "bad_type:rec")
        if self.n_floats != floats or _BIG_OR_NEGATIVE.search(raw):
            bad = _find_bad_number(d, "record", False)
            if bad is not None:
                raise _Bad(f"bad_type:{_key_name(bad)}")
        if rec != "content" and _LONG_STRING.search(raw):
            long = _find_long_string(d, "record")
            if long is not None:
                raise _Bad(f"bad_type:{_key_name(long)}")
        if rec == "header":
            if not first or self.header is not None:
                raise _Bad("bad_type:rec")
            self.header = self._header(d)
            return self.header
        hdr = self.header
        if hdr is None:
            raise _Bad("missing:header")
        if hdr.name_key_id is None and _NAME_HASH.search(raw):
            raise _Bad("bad_type:name_key_id")
        if hdr.principal_key_id is None and _PRINCIPAL_HASH.search(raw):
            raise _Bad("bad_type:principal_key_id")
        return _DECODERS[rec](self, d, hdr)

    # -- identity -----------------------------------------------------------------------------

    @staticmethod
    def _principal(p: str | None, hdr_mode: str, pkid: str | None) -> None:
        if p is None:
            return
        if "@" in p:
            raise _Bad("bad_type:principal")
        head = p[:2]
        if head == "r_" and hdr_mode != "central":
            raise _Bad("bad_type:principal")
        if head == "c_" and (hdr_mode != "two-stage" or pkid is None):
            raise _Bad("bad_type:principal")
        if head == "p_" and pkid is None:
            raise _Bad("bad_type:principal")

    def _attribution(self, v: Any, hdr: TraceHeader | None = None, *, mode: str | None = None,
                     pkid: str | None = None) -> Attribution:
        a = _build(Attribution, _obj(v, "attribution"), _S_ATTRIBUTION, "attribution")
        if hdr is not None:
            mode, pkid = hdr.identity_mode, hdr.principal_key_id
        self._principal(a.principal, mode or "", pkid)
        if a.repo is not None and not _HASH_RE.match(a.repo):
            raise _Bad("bad_type:repo")
        return a

    # -- records ------------------------------------------------------------------------------

    def _header(self, d: dict) -> TraceHeader:
        _check(d, _S_HEADER)
        trace_id = d["trace_id"]
        if not isinstance(trace_id, str) or not _TRACE_ID_RE.match(trace_id):
            raise _Bad("bad_type:trace_id")
        profile = d["profile"]
        if profile not in PROFILES:
            raise _Bad("bad_type:profile")
        mode = d["identity_mode"]
        if mode not in IDENTITY_MODES:
            raise _Bad("bad_type:identity_mode")
        kids = {}
        for name in ("name_key_id", "principal_key_id", "fp_key_id"):
            v = d.get(name)
            if v is not None and (not isinstance(v, str) or not _KEY_ID_RE.match(v)):
                raise _Bad(f"bad_type:{name}")
            kids[name] = v
        if kids["fp_key_id"] is not None and profile == "usage":
            raise _Bad("bad_type:fp_key_id")
        producer = d.get("producer")
        pairs: tuple[tuple[str, str], ...] = ()
        if producer is not None:
            _check(_obj(producer, "producer"), _S_PRODUCER)
            if not all(isinstance(x, str) for x in producer.values()):
                raise _Bad("bad_type:producer")
            pairs = tuple(sorted(producer.items()))
        created = d.get("created_ms", 0)
        if type(created) is not int or created < 0:
            raise _Bad("bad_type:created_ms")
        attr_raw = d.get("attribution")
        attribution = Attribution() if attr_raw is None else self._attribution(
            attr_raw, mode=mode, pkid=kids["principal_key_id"])
        return TraceHeader(trace_id=trace_id, profile=profile, identity_mode=mode,
                           name_key_id=kids["name_key_id"],
                           principal_key_id=kids["principal_key_id"],
                           fp_key_id=kids["fp_key_id"], producer=pairs, created_ms=created,
                           attribution=attribution)

    def _session(self, d: dict, hdr: TraceHeader) -> Session:
        _check(d, _S_SESSION)
        attr = self._attribution(d["attribution"], hdr)
        try:
            return Session(session_key=d["session_key"], source_kind=d["source_kind"],
                           attribution=attr, lanes=(), started_ms=d["started_ms"],
                           ended_ms=d["ended_ms"])
        except ContractViolation as exc:
            raise _cv(exc, "session") from None

    def _lane(self, d: dict, hdr: TraceHeader) -> Lane:
        _check(d, _S_LANE)
        kw = {k: v for k, v in d.items() if k not in _REC}
        try:
            return Lane(requests=(), events=(), **kw)
        except ContractViolation as exc:
            raise _cv(exc, "lane") from None

    def _blocks(self, d: dict, hdr: TraceHeader) -> None:
        _check(d, _S_BLOCKS)
        if hdr.profile not in _FINGERPRINT_PROFILES:
            raise _Bad("bad_type:blocks")
        if d["key_id"] != hdr.fp_key_id or hdr.fp_key_id is None:
            raise _Bad("bad_type:key_id")
        defs = []
        for b in _list(d["blocks"], "blocks"):
            ref = _build(BlockRef, _obj(b, "blocks"), _S_BLOCKREF, "blocks")
            if not _BLOCK_HASH_RE.match(ref.h):
                raise _Bad("bad_type:h")
            defs.append(ref)
        for ref in defs:
            self.defs[ref.h] = ref if ref.lookback_pos == 0 else replace(ref, lookback_pos=0)
        self.n_blocks += len(defs)
        return None

    def _usage(self, v: Any) -> UsageBuckets:
        return _build(UsageBuckets, _obj(v, "usage"), _S_USAGE, "usage")

    def _inference(self, v: Any) -> Inference:
        d = _obj(v, "inferences")
        _check(d, _S_INFERENCE)
        kw = dict(d)
        kw["usage"] = self._usage(d["usage"])
        kw["pricing"] = _build(PricingContext, _obj(d["pricing"], "pricing"), _S_PRICING,
                               "pricing")
        try:
            return Inference(**kw)
        except ContractViolation as exc:
            raise _cv(exc, "inferences") from None

    def _attempt(self, v: Any) -> Attempt:
        d = _obj(v, "attempts")
        _check(d, _S_ATTEMPT)
        kw = dict(d)
        kw["inferences"] = tuple(self._inference(x) for x in _list(d["inferences"],
                                                                     "inferences"))
        diag = d.get("diagnostics")
        if diag is not None:
            kw["diagnostics"] = _build(CacheDiagnostic, _obj(diag, "diagnostics"),
                                       _S_DIAGNOSTIC, "diagnostics")
        raw = kw.pop("raw_usage", None)
        if raw is not None:
            if type(raw) is not dict:
                raise _Bad("bad_usage")
            _raw_value(raw, None, 0, False)
            text = _raw_json(raw)
            if len(text) > _RAW_USAGE_MAX:
                raise _Bad("bad_usage")
            kw["raw_usage_json"] = text
        edits = d.get("applied_edits")
        if edits is not None:
            _list(edits, "applied_edits")
        try:
            return Attempt(**kw)
        except ContractViolation as exc:
            raise _cv(exc, "attempts") from None

    def _params(self, v: Any) -> RequestParams:
        d = _obj(v, "params")
        _check(d, _S_PARAMS)
        bps = d.get("breakpoints")
        if bps:
            d = dict(d)
            d["breakpoints"] = tuple(
                _build(Breakpoint, _obj(b, "breakpoints"), _S_BREAKPOINT, "breakpoints")
                for b in _list(bps, "breakpoints"))
        try:
            return RequestParams(**d)
        except ContractViolation as exc:
            raise _cv(exc, "params") from None

    def _fingerprint(self, fp: dict, params: RequestParams, hdr: TraceHeader
                     ) -> tuple[ContentFingerprint, RequestParams]:
        _check(fp, _S_FP)
        if hdr.profile not in _FINGERPRINT_PROFILES or hdr.fp_key_id is None:
            raise _Bad("bad_type:fp")
        parent, keep = fp["parent"], fp["keep"]
        if type(keep) is not int or keep < 0:
            raise _Bad("bad_type:keep")
        if parent is None:
            if keep:
                raise _Bad("bad_type:keep")
            base: tuple[BlockRef, ...] = ()
        else:
            got = self.resolved.get(parent) if isinstance(parent, str) else None
            if got is None:
                raise _Bad("missing:parent")
            if keep > len(got):
                raise _Bad("bad_type:keep")
            base = got[:keep]
        blocks = list(base)
        prev_kind = blocks[-1].kind if blocks else None
        pos = blocks[-1].lookback_pos if blocks else -1
        for h in _list(fp["append"], "append"):
            ref = self.defs.get(h) if isinstance(h, str) else None
            if ref is None:
                raise _Bad("missing:append")
            if not (ref.kind in ("tool_use", "tool_result") and ref.kind == prev_kind):
                pos += 1
            if ref.lookback_pos != pos:
                ref = replace(ref, lookback_pos=pos)
            blocks.append(ref)
            prev_kind = ref.kind
        te = _list(fp["tier_end"], "tier_end")
        try:
            cf = ContentFingerprint(key_id=hdr.fp_key_id, blocks=tuple(blocks), tier_end=tuple(te))
        except ContractViolation:
            raise _Bad("bad_type:tier_end") from None
        markers = []
        for m in _list(fp.get("markers") or [], "markers"):
            if (type(m) is not list or len(m) != 2 or type(m[0]) is not int
                    or not isinstance(m[1], str)):
                raise _Bad("bad_type:markers")
            markers.append((m[0], m[1]))
        declared = [(bp.block_index, bp.ttl) for bp in params.breakpoints]
        if markers and not declared:
            try:
                params = replace(params, breakpoints=tuple(Breakpoint(i, t) for i, t in markers))
            except ContractViolation:
                raise _Bad("bad_type:markers") from None
        elif markers and markers != declared:
            raise _Bad("bad_type:markers")
        if any(bp.block_index >= len(blocks) for bp in params.breakpoints):
            raise _Bad("bad_type:markers")
        return cf, params

    def _request(self, d: dict, hdr: TraceHeader) -> Request:
        _check(d, _S_REQUEST)
        rid = d["request_id"]
        if not isinstance(rid, str):
            raise _Bad("bad_type:request_id")
        if rid in self.seen_requests:
            raise _Bad("bad_type:request_id")
        attribution = self._attribution(d["attribution"], hdr)
        params = self._params(d["params"])
        appended: tuple[AppendedItem, ...] = ()
        app = d.get("appended")
        if app:
            appended = tuple(_build(AppendedItem, _obj(a, "appended"), _S_APPENDED, "appended")
                             for a in _list(app, "appended"))
        src = d.get("source")
        source = None if src is None else _build(SourceRef, _obj(src, "source"), _S_SOURCE,
                                                 "source")
        attempts = tuple(self._attempt(a) for a in _list(d["attempts"], "attempts"))
        fingerprint = None
        fp = d.get("fp")
        if fp is not None:
            fingerprint, params = self._fingerprint(_obj(fp, "fp"), params, hdr)
        try:
            req = Request(request_id=rid, session_key=d["session_key"], lane_key=d["lane_key"],
                          seq=d["seq"], attribution=attribution, params=params,
                          attempts=attempts, fingerprint=fingerprint, appended=appended,
                          source=source)
        except ContractViolation as exc:
            raise _cv(exc, "request") from None
        self.seen_requests.add(rid)
        if fingerprint is not None:
            self.resolved[rid] = fingerprint.blocks
        return req

    def _event(self, d: dict, hdr: TraceHeader) -> LaneEvent:
        _check(d, _S_EVENT)
        attrs = d.get("attrs", {})
        try:
            return LaneEvent(lane_key=d["lane_key"], ts_ms=d["ts_ms"], kind=d["kind"],
                             attrs=tuple(_obj(attrs, "attrs").items()))
        except ContractViolation as exc:
            raise _cv(exc, "event") from None

    def _aggregate(self, d: dict, hdr: TraceHeader) -> UsageAggregate:
        _check(d, _S_AGGREGATE)
        kw = {k: v for k, v in d.items() if k not in _REC}
        kw["usage"] = self._usage(d["usage"])
        _list(d["dims"], "dims")
        try:
            return UsageAggregate(**kw)
        except ContractViolation as exc:
            raise _cv(exc, "aggregate") from None

    def _cost_line(self, d: dict, hdr: TraceHeader) -> CostLine:
        _check(d, _S_COST_LINE)
        kw = {k: v for k, v in d.items() if k not in _REC}
        try:
            return CostLine(**kw)
        except ContractViolation as exc:
            raise _cv(exc, "cost_line") from None

    def _outcome(self, d: dict, hdr: TraceHeader) -> OutcomeAggregate:
        _check(d, _S_OUTCOME)
        kw = {k: v for k, v in d.items() if k not in _REC}
        if "extra" in kw:
            _list(kw["extra"], "extra")
        try:
            return OutcomeAggregate(**kw)
        except ContractViolation as exc:
            raise _cv(exc, "outcome") from None

    def _dq(self, d: dict, hdr: TraceHeader) -> DataQualityNote:
        _check(d, _S_DQ)
        code, sev, count, detail = d["code"], d["severity"], d["count"], d["detail"]
        tokens = d.get("tokens")
        if not isinstance(code, str) or not _DQ_CODE_RE.match(code):
            raise _Bad("bad_type:code")
        if not isinstance(sev, str) or sev not in _SEVERITIES:
            raise _Bad("bad_type:severity")
        if type(count) is not int or count < 0:
            raise _Bad("bad_type:count")
        if not isinstance(detail, str):
            raise _Bad("bad_type:detail")
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise _Bad("bad_type:tokens")
        return DataQualityNote(code=code, severity=sev, count=count, detail=detail, tokens=tokens)

    def _content(self, d: dict, hdr: TraceHeader) -> ContentItem:
        _check(d, _S_CONTENT)
        if hdr.profile != "full":
            raise _Bad("bad_type:content")
        h, text = d["h"], d["text"]
        if not isinstance(h, str) or not _BLOCK_HASH_RE.match(h):
            raise _Bad("bad_type:h")
        if not isinstance(text, str):
            raise _Bad("bad_type:text")
        return ContentItem(h=h, text=text)


_DECODERS: dict[str, Any] = {
    "session": _Reader._session, "lane": _Reader._lane, "blocks": _Reader._blocks,
    "request": _Reader._request, "event": _Reader._event, "aggregate": _Reader._aggregate,
    "cost_line": _Reader._cost_line, "outcome": _Reader._outcome, "dq": _Reader._dq,
    "content": _Reader._content,
}


def _default_source_id(path: Path) -> str:
    try:
        resolved = str(Path(path).resolve())
    except OSError:  # pragma: no cover - resolve() only fails on exotic file systems
        resolved = str(path)
    return stable_id("s", ADAPTER_NAME, resolved)


def _iter(path: Path, *, lenient: bool, source_id: str) -> Iterator[tuple[int, object]]:
    reader = _Reader(name=Path(path).name, source_id=source_id, lenient=lenient)
    for line_no, _offset, raw in iter_lines(Path(path)):
        item = reader.feed(line_no, raw)
        if item is not None:
            yield line_no, item


def iter_trace_v2(path: Path, *, lenient: bool = True) -> Iterator[object]:
    """Stream a trace@2 file (plain or ``.gz``): the :class:`TraceHeader`, then — in file order —
    :class:`~tokenbill.core.records.Session` (without lanes), :class:`~tokenbill.core.records.Lane`
    shells, :class:`~tokenbill.core.records.Request` (fingerprints resolved),
    :class:`~tokenbill.core.records.LaneEvent`, ``UsageAggregate``, ``CostLine``,
    ``OutcomeAggregate``, :class:`~tokenbill.core.types.DataQualityNote` and :class:`ContentItem`
    records, or a :class:`~tokenbill.core.types.QuarantineItem` for each rejected line (lenient).
    ``blocks`` records are consumed internally. With ``lenient=False`` the first bad line raises
    :class:`~tokenbill.core.errors.SourceError` (file name, line number and reason only)."""
    for _line_no, item in _iter(Path(path), lenient=lenient,
                                source_id=_default_source_id(Path(path))):
        yield item


# ---------------------------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------------------------


def _canonical(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def make_header(header: Mapping[str, Any] | TraceHeader) -> TraceHeader:
    """A :class:`TraceHeader` from a mapping of header fields (``trace_id`` and ``profile``
    required; ``identity_mode`` defaults to ``install``, ``producer`` to tokenbill and its version,
    ``attribution`` may be an :class:`~tokenbill.core.records.Attribution` or its JSON form).
    Invalid values raise :class:`~tokenbill.core.errors.UsageError`."""
    if isinstance(header, TraceHeader):
        d: dict[str, Any] = _header_json(header)
    elif isinstance(header, Mapping):
        d = {k: v for k, v in header.items() if k not in _REC}
        if "trace_id" not in d or "profile" not in d:
            raise UsageError("a trace@2 header needs trace_id and profile")
        d.setdefault("identity_mode", "install")
        if "producer" not in d:
            from tokenbill import __version__
            d["producer"] = {"name": "tokenbill", "version": __version__}
        producer = d.get("producer")
        if isinstance(producer, (tuple, list)):
            d["producer"] = dict(producer)
        attr = d.get("attribution")
        if isinstance(attr, Attribution):
            d["attribution"] = to_json(attr)
        d = {"rec": "header", "schema": SCHEMA, **d}
    else:
        raise UsageError("header must be a mapping or a TraceHeader")
    reader = _Reader(name="header", source_id="", lenient=False)
    try:
        return reader._header(json.loads(_canonical(d)))
    except _Bad as bad:
        raise UsageError(f"invalid trace@2 header: {bad.reason}") from None
    except (TypeError, ValueError):
        raise UsageError("invalid trace@2 header: not JSON") from None


def _header_json(h: TraceHeader) -> dict[str, Any]:
    return {"rec": "header", "schema": SCHEMA, "trace_id": h.trace_id, "profile": h.profile,
            "name_key_id": h.name_key_id, "principal_key_id": h.principal_key_id,
            "fp_key_id": h.fp_key_id, "identity_mode": h.identity_mode,
            "producer": dict(h.producer), "created_ms": h.created_ms,
            "attribution": to_json(h.attribution)}


def _def_key(b: BlockRef) -> tuple:
    return (b.h, b.h_sorted, b.h_norm, b.tier, b.kind, b.role, b.n_bytes, b.est_tokens,
            b.image_px, b.volatile_classes, b.deferred)


class TraceV2Writer:
    """Streaming trace@2 writer over an open text handle (the recorder's and
    :func:`write_trace_v2`'s engine).

    The header is written on construction. Every record is encoded canonically and validated by
    the reader before it is written (a violation raises
    :class:`~tokenbill.core.errors.ContractViolation` naming the reason and nothing is written for
    that record). Fingerprints are delta-encoded against the previous fingerprinted request of the
    same lane; ``blocks`` records define new (or changed) hashes just before their first use. In
    the ``usage`` profile fingerprints are dropped; ``content`` needs the ``full`` profile.
    """

    def __init__(self, handle: IO[str], header: Mapping[str, Any] | TraceHeader) -> None:
        self.header = make_header(header)
        self._handle = handle
        self._reader = _Reader(name="writer", source_id="", lenient=False)
        self._emitted: dict[str, tuple] = {}
        self._last: dict[str, tuple[str, tuple[BlockRef, ...]]] = {}
        self.count = 0
        self._line(_header_json(self.header))

    @property
    def profile(self) -> str:
        """The header profile."""
        return self.header.profile

    def _line(self, record: Mapping[str, Any]) -> None:
        text = _canonical(record)
        raw = text.encode("utf-8", "surrogatepass")
        try:
            self._reader._decode(raw)
        except _Bad as bad:
            raise ContractViolation(f"trace@2 writer: {record.get('rec')}: {bad.reason}") from None
        self._handle.write(text + "\n")
        self.count += 1

    def session(self, s: Session) -> None:
        """A ``session`` record and one ``lane`` record per lane shell (requests and events inside
        the lanes are not written here)."""
        d = to_json(s)
        d.pop("lanes")
        self._line({"rec": "session", "schema": SCHEMA, **d})
        for lane in s.lanes:
            self.lane(lane)

    def lane(self, lane: Lane) -> None:
        """A ``lane`` shell record."""
        d = to_json(lane)
        d.pop("requests")
        d.pop("events")
        self._line({"rec": "lane", "schema": SCHEMA, **d})

    def request(self, r: Request) -> None:
        """A ``request`` record, preceded by a ``blocks`` record when it uses new hashes."""
        fp_obj = None
        fp = r.fingerprint
        if fp is not None and self.header.profile in _FINGERPRINT_PROFILES:
            fp_obj = self._fp(r, fp)
        d = {"rec": "request", "schema": SCHEMA, "request_id": r.request_id,
             "session_key": r.session_key, "lane_key": r.lane_key, "seq": r.seq,
             "attribution": to_json(r.attribution), "params": to_json(r.params),
             "appended": [to_json(a) for a in r.appended],
             "source": to_json(r.source) if r.source is not None else None,
             "fp": fp_obj, "attempts": [self._attempt(a) for a in r.attempts]}
        self._line(d)
        if fp_obj is not None and fp is not None:
            self._last[r.lane_key] = (r.request_id, fp.blocks)

    @staticmethod
    def _attempt(a: Attempt) -> dict[str, Any]:
        d = to_json(a)
        d["raw_usage"] = sanitize_raw_usage(d.pop("raw_usage_json"))
        return d

    def _fp(self, r: Request, fp: ContentFingerprint) -> dict[str, Any]:
        if fp.key_id != self.header.fp_key_id:
            raise ContractViolation("trace@2 writer: fingerprint key id differs from fp_key_id")
        fresh: list[dict[str, Any]] = []
        seen: dict[str, tuple] = {}
        for b in fp.blocks:
            k = _def_key(b)
            prior = seen.get(b.h)
            if prior is not None:
                if prior != k:
                    raise ContractViolation("trace@2 writer: two blocks share a hash")
                continue
            seen[b.h] = k
            if self._emitted.get(b.h) != k:
                enc = to_json(b)
                enc.pop("lookback_pos", None)
                fresh.append(enc)
        if fresh:
            self._line({"rec": "blocks", "schema": SCHEMA, "key_id": fp.key_id,
                        "blocks": fresh})
            for enc in fresh:
                self._emitted[enc["h"]] = seen[enc["h"]]
        parent_id: str | None = None
        keep = 0
        last = self._last.get(r.lane_key)
        if last is not None:
            parent_id, prev = last
            n = min(len(prev), len(fp.blocks))
            while keep < n and _def_key(prev[keep]) == _def_key(fp.blocks[keep]):
                keep += 1
        return {"parent": parent_id, "keep": keep,
                "append": [b.h for b in fp.blocks[keep:]],
                "markers": [[bp.block_index, bp.ttl] for bp in r.params.breakpoints],
                "tier_end": list(fp.tier_end)}

    def event(self, e: LaneEvent) -> None:
        """An ``event`` record (attrs as an object)."""
        self._line({"rec": "event", "schema": SCHEMA, "lane_key": e.lane_key, "ts_ms": e.ts_ms,
                    "kind": e.kind.value, "attrs": dict(e.attrs)})

    def aggregate(self, a: UsageAggregate) -> None:
        """An ``aggregate`` record."""
        self._line({"rec": "aggregate", "schema": SCHEMA, **to_json(a)})

    def cost_line(self, c: CostLine) -> None:
        """A ``cost_line`` record."""
        self._line({"rec": "cost_line", "schema": SCHEMA, **to_json(c)})

    def outcome(self, o: OutcomeAggregate) -> None:
        """An ``outcome`` record."""
        self._line({"rec": "outcome", "schema": SCHEMA, **to_json(o)})

    def note(self, n: DataQualityNote) -> None:
        """A ``dq`` record (a note's pipeline ``figure`` is not part of the format)."""
        self._line({"rec": "dq", "schema": SCHEMA, "code": n.code, "severity": n.severity,
                    "count": n.count, "detail": n.detail, "tokens": n.tokens})

    def content(self, h: str, text: str) -> None:
        """A ``content`` record (``full`` profile only)."""
        if self.header.profile != "full":
            raise UsageError("content records need the full profile")
        self._line({"rec": "content", "schema": SCHEMA, "h": h, "text": text})


def write_trace_v2(path: Path, *, header: Mapping[str, object], sessions: Iterable[Session] = (),
                   requests: Iterable[Request] = (), events: Iterable[LaneEvent] = (),
                   aggregates: Iterable[UsageAggregate] = (), cost_lines: Iterable[CostLine] = (),
                   outcomes: Iterable[OutcomeAggregate] = (),
                   notes: Iterable[DataQualityNote] = (),
                   content: Mapping[str, str] | None = None) -> int:
    """Write a trace@2 file (``.gz`` when the path ends so; owner-only) and return the number of
    records written (header included).

    Order: header, ``dq`` notes, sessions (each followed by its lane shells), requests (those
    inside session lanes first, then *requests*; a request id is written once; ``blocks`` records
    precede first use), events (lane events first), aggregates, cost lines, outcomes and — ``full``
    profile only — ``content`` records sorted by hash. Same inputs give the same bytes (gzip with
    no name and mtime 0).
    """
    hdr = make_header(header)
    if content and hdr.profile != "full":
        raise UsageError("content needs the full profile")
    sessions = list(sessions)
    path = Path(path)
    with contextlib.ExitStack() as stack:
        if path.suffix.lower() == ".gz":
            handle = stack.enter_context(open_private(path, "wb"))
            raw = stack.enter_context(gzip.GzipFile(filename="", mode="wb", fileobj=handle,
                                                    mtime=0))
            text: IO[str] = stack.enter_context(io.TextIOWrapper(raw, encoding="utf-8",
                                                                 newline=""))
        else:
            text = stack.enter_context(open_private(path, "w"))
        w = TraceV2Writer(text, hdr)
        for n in notes:
            w.note(n)
        lane_requests: list[Request] = []
        lane_events: list[LaneEvent] = []
        for s in sessions:
            w.session(s)
            for lane in s.lanes:
                lane_requests.extend(lane.requests)
                lane_events.extend(lane.events)
        written: set[str] = set()
        for r in (*lane_requests, *requests):
            if r.request_id in written:
                continue
            written.add(r.request_id)
            w.request(r)
        for e in (*lane_events, *events):
            w.event(e)
        for a in aggregates:
            w.aggregate(a)
        for c in cost_lines:
            w.cost_line(c)
        for o in outcomes:
            w.outcome(o)
        for h in sorted(content or {}):
            w.content(h, (content or {})[h])
        return w.count


# ---------------------------------------------------------------------------------------------
# renormalization (IngestOptions.renormalize)
# ---------------------------------------------------------------------------------------------


def _serving(att: Attempt) -> Inference:
    for inf in reversed(att.inferences):
        if inf.kind in (InferenceKind.MESSAGE, InferenceKind.FALLBACK):
            return inf
    return att.inferences[-1]


def _renormalize_attempt(att: Attempt, codes: Counter[str]) -> Attempt | None:
    """The attempt with inferences rebuilt from its raw usage, or None when it cannot be."""
    if att.raw_usage_json is None or att.convention_id is None or not att.inferences:
        return None
    if not all(i.kind in _RENORM_KINDS and i.usage_source in _RENORM_SOURCES
               for i in att.inferences):
        return None
    try:
        if not get_convention(att.convention_id).enabled:
            return None
        raw = json.loads(att.raw_usage_json)
        base = _serving(att)
        if att.convention_id == ANTHROPIC_MESSAGES:
            infs, found = anthropic_inferences(raw, message_model=base.pricing.model_raw,
                                               ctx=base.pricing, id_prefix=att.attempt_id,
                                               usage_source=base.usage_source)
        else:
            if len(att.inferences) != 1:
                return None
            buckets, found = normalize(att.convention_id, raw)
            infs = [replace(base, usage=buckets, output_upper=None)]
        if len(infs) == len(att.inferences):
            merged = []
            for old, new in zip(att.inferences, infs, strict=True):
                upper = old.output_upper
                if upper is not None and upper < new.usage.output:
                    upper = None
                merged.append(replace(old, usage=new.usage, output_upper=upper))
            infs = merged
        rebuilt = replace(att, inferences=tuple(infs))
    except (UsageError, PricingError, ContractViolation, ValueError, TypeError):
        codes["renormalize_failed"] += 1
        return None
    for code in found:
        codes[code] += 1
    return rebuilt


def _renormalize(req: Request, codes: Counter[str]) -> Request:
    attempts = []
    changed = False
    for att in req.attempts:
        new = _renormalize_attempt(att, codes)
        if new is None:
            attempts.append(att)
        else:
            changed = True
            codes["dq.renormalized"] += 1
            attempts.append(new)
    return replace(req, attempts=tuple(attempts)) if changed else req


# ---------------------------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------------------------


def _sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                n += len(chunk)
    except OSError as exc:
        raise SourceError(f"{Path(path).name}: unreadable ({type(exc).__name__})") from None
    return h.hexdigest(), n


def _capabilities(requests: list[Request], lanes: list[Lane], events: list[LaneEvent],
                  aggregates: list, cost_lines: list, outcomes: list) -> frozenset[str]:
    caps: set[str] = set()
    if requests:
        caps |= {"usage_sequence", "timing", "params"}
    if lanes and all(lane.lane_exact for lane in lanes):
        caps.add("lanes_exact")
    for r in requests:
        if r.fingerprint is not None:
            caps.add("blocks")
        if r.appended:
            caps.add("appended")
        if r.attribution.team:
            caps.add("attribution.team")
        if r.attribution.workload_class is not WorkloadClass.UNKNOWN:
            caps.add("workload")
        if len(r.attempts) > 1:
            caps.add("attempts")
        for a in r.attempts:
            if a.ttft_ms is not None:
                caps.add("ttft")
            if a.diagnostics is not None:
                caps.add("diagnostics")
            if a.retry_layer is not None or a.sdk_retry_count is not None:
                caps.add("attempts")
            if len(a.inferences) > 1:
                caps.add("iterations")
            for i in a.inferences:
                if i.usage.cache_write_5m or i.usage.cache_write_1h:
                    caps.add("ttl_split")
    for e in events:
        caps.add("events")
        if e.kind is LaneEventKind.HUMAN_PROMPT:
            caps.add("human_prompts")
        elif e.kind is LaneEventKind.QUOTA_STATE:
            caps.add("quota_state")
    if aggregates:
        caps.add("aggregates")
    if cost_lines:
        caps.add("cost")
    if outcomes:
        caps.add("outcomes")
    return frozenset(caps)


def _source_id(opts: IngestOptions, path: Path) -> str:
    try:
        resolved = str(Path(path).resolve())
    except OSError:  # pragma: no cover
        resolved = str(path)
    if opts.name_key:
        return pseudonym(opts.name_key, "s", f"{ADAPTER_NAME}:{resolved}")
    return stable_id("s", ADAPTER_NAME, resolved)


class TraceV2Adapter:
    """Reads ``tokenbill/trace@2`` files (registry name ``trace@2``, SPEC §4.4).

    Capabilities of a result are those present in the file (§5.1); the header key ids become
    :class:`~tokenbill.core.types.SourceInfo` key ids (D39). ``opts.renormalize`` rebuilds
    inferences from ``raw_usage`` with the current conventions (``dq.renormalized``);
    ``opts.lenient`` selects quarantine or :class:`~tokenbill.core.errors.SourceError`;
    ``opts.since_ms`` / ``until_ms`` filter requests and events by start time.
    """

    name = ADAPTER_NAME
    capabilities = CAPABILITIES

    def sniff(self, path: Path, head: bytes) -> bool:
        """True when the first line is a trace@2 header."""
        line = head.split(b"\n", 1)[0].strip()
        if line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        if b'"tokenbill/trace@2"' not in line or b'"header"' not in line:
            return False
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            return b"\n" not in head  # a header longer than the sniffed head
        return isinstance(obj, dict) and obj.get("schema") == SCHEMA and \
            obj.get("rec") == "header"

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Decode *path* into an :class:`~tokenbill.core.types.IngestResult`."""
        if not isinstance(opts, IngestOptions):
            raise UsageError("read expects IngestOptions")
        path = Path(path)
        source_id = _source_id(opts, path)
        sha, n_bytes = _sha256_file(path)
        header: TraceHeader | None = None
        sessions: list[Session] = []
        lanes: list[tuple[int, Lane]] = []
        requests: list[Request] = []
        events: list[LaneEvent] = []
        aggregates: list[UsageAggregate] = []
        cost_lines: list[CostLine] = []
        outcomes: list[OutcomeAggregate] = []
        file_notes: list[DataQualityNote] = []
        quarantined: list[QuarantineItem] = []
        stats: Counter[str] = Counter()
        seen_sessions: set[str] = set()
        seen_lanes: set[str] = set()
        since, until = opts.since_ms, opts.until_ms

        def in_window(ts: int) -> bool:
            return (since is None or ts >= since) and (until is None or ts < until)

        for line_no, item in _iter(path, lenient=opts.lenient, source_id=source_id):
            stats["lines"] += 1
            if isinstance(item, QuarantineItem):
                quarantined.append(item)
                continue
            stats["records"] += 1
            if isinstance(item, Request):
                if in_window(item.ts_start_ms):
                    requests.append(item)
                else:
                    stats["skipped_window"] += 1
            elif isinstance(item, LaneEvent):
                if in_window(item.ts_ms):
                    events.append(item)
                else:
                    stats["skipped_window"] += 1
            elif isinstance(item, Lane):
                if item.lane_key in seen_lanes:
                    quarantined.append(QuarantineItem(source_id, f"line:{line_no}",
                                                      "bad_type:lane_key"))
                    if not opts.lenient:
                        raise SourceError(f"{path.name}: line {line_no}: bad_type:lane_key")
                    continue
                seen_lanes.add(item.lane_key)
                lanes.append((line_no, item))
            elif isinstance(item, Session):
                if item.session_key in seen_sessions:
                    quarantined.append(QuarantineItem(source_id, f"line:{line_no}",
                                                      "bad_type:session_key"))
                    if not opts.lenient:
                        raise SourceError(f"{path.name}: line {line_no}: bad_type:session_key")
                    continue
                seen_sessions.add(item.session_key)
                sessions.append(item)
            elif isinstance(item, UsageAggregate):
                aggregates.append(item)
            elif isinstance(item, CostLine):
                cost_lines.append(item)
            elif isinstance(item, OutcomeAggregate):
                outcomes.append(item)
            elif isinstance(item, DataQualityNote):
                file_notes.append(item)
            elif isinstance(item, TraceHeader):
                header = item
            elif isinstance(item, ContentItem):
                stats["content"] += 1
        by_session: dict[str, list[Lane]] = {}
        for line_no, lane in lanes:
            if lane.session_key not in seen_sessions:
                if not opts.lenient:
                    raise SourceError(f"{path.name}: line {line_no}: missing:session")
                quarantined.append(QuarantineItem(source_id, f"line:{line_no}",
                                                  "missing:session"))
                continue
            by_session.setdefault(lane.session_key, []).append(lane)
        sessions = [replace(s, lanes=tuple(by_session.get(s.session_key, ())))
                    if s.session_key in by_session else s for s in sessions]
        notes = list(file_notes)
        if opts.renormalize and requests:
            codes: Counter[str] = Counter()
            requests = [_renormalize(r, codes) for r in requests]
            n_renorm = codes.pop("dq.renormalized", 0)
            failed = codes.pop("renormalize_failed", 0)
            stats["renormalize_failed"] += failed
            if n_renorm:
                notes.append(DataQualityNote(
                    code="dq.renormalized", severity="info", count=n_renorm,
                    detail="inferences rebuilt from raw_usage with the current conventions"))
            for code, count in sorted(codes.items()):
                notes.append(DataQualityNote(code=code, severity="info", count=count,
                                             detail="raised while renormalizing raw_usage"))
        if quarantined:
            reasons = Counter(q.reason for q in quarantined)
            detail = ", ".join(f"{r}={c}" for r, c in sorted(reasons.items()))
            notes.append(DataQualityNote(code="dq.quarantined", severity="warn",
                                         count=len(quarantined), detail=detail[:MAX_STRING]))
        stats.update({"requests": len(requests), "sessions": len(sessions),
                      "lanes": sum(len(s.lanes) for s in sessions), "events": len(events),
                      "aggregates": len(aggregates), "cost_lines": len(cost_lines),
                      "outcomes": len(outcomes), "notes": len(file_notes),
                      "quarantined": len(quarantined)})
        all_lanes = [lane for s in sessions for lane in s.lanes]
        caps = _capabilities(requests, all_lanes, events, aggregates, cost_lines, outcomes)
        hdr_nk = header.name_key_id if header is not None else None
        name_hmac = ""
        name_key_id = hdr_nk
        if opts.name_key:
            own = key_id(opts.name_key)
            if hdr_nk is None or hdr_nk == own:
                name_hmac = pseudonym(opts.name_key, "h", path.name)
                name_key_id = own
        source = SourceInfo(source_id=source_id, adapter=ADAPTER_NAME, name_hmac=name_hmac,
                            sha256=sha, bytes=n_bytes, name_key_id=name_key_id,
                            principal_key_id=header.principal_key_id if header else None)
        return IngestResult(source=source, requests=requests, sessions=sessions, events=events,
                            aggregates=aggregates, cost_lines=cost_lines, outcomes=outcomes,
                            quarantined=quarantined, notes=notes,
                            stats={k: int(v) for k, v in sorted(stats.items())},
                            capabilities=caps)


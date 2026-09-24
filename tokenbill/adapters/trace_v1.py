"""The ``tokenbill/trace@1`` adapter: v0.1 recorder traces into the v2 ledger (SPEC §5.5; TRACE).

Reads line by line (``.gz`` too): ``json`` + the frozen ``trace._parse_call`` per line; a bad line
is quarantined (``bad_json``, ``not_object``, ``missing:<field>``, ``bad_type:<field>``,
``oversize_line``) or, with ``lenient=False``, raises :class:`~tokenbill.core.errors.SourceError`.
The v0.1 monotonic-index rule applies per run (a non-increasing index is ``bad_type:index``).

Each call becomes one :class:`~tokenbill.core.records.Request` with one attempt and one MESSAGE
inference on channel ``anthropic_api``, billing path ``api_key`` (unless the options' attribution
names another; both are assumptions of the format): uncached = ``input_tokens``, reads =
``cache_read_input_tokens``, writes = ``cache_creation_input_tokens`` as **5m writes, EXACT**,
unless a ``cache_control`` in the call's tools or messages has ``ttl: "1h"`` — then the writes are
``cache_write_unknown`` with ``write_ttl_hint="1h"`` (priced as the range [5m, 1h], D12) and
``dq.ttl_1h_markers_in_trace1`` is recorded.

**Breakpoints** (``RequestParams.breakpoints``, relied on by BLOCK's dual-engine gate): markers
found in tools / messages become breakpoints at their fingerprint block index; when the call's
``cache_breakpoints`` count exceeds the recoverable markers (the system prompt is stored rendered,
and the v0.1 demo traces carry only the count) **one** assumed breakpoint is placed at the last
block (``Breakpoint(last, "5m", assumed=True)``, ``dq.breakpoint_assumed_end``); a count of 0 with
no markers means no breakpoints and ``automatic_caching=False``. Fingerprints (content tier
``fingerprint`` / ``full``, key = ``opts.name_key``) come from
:func:`~tokenbill.adapters.fingerprint.fingerprint_request`.

**Sessions and lanes** (ruling R-E27): ``session_key = stable_id("ses", source_id, run_id)`` — two
files reusing a run id never merge — and the run is one ``api_run`` lane. Only a run that
interleaves models is split, one lane per model (``lane_exact=False``, ``dq.lanes_inferred``): the
provider caches per model, so a single lane would count every model switch as a cache break. Runs
are never split by tools-tier hash (the v0.1 ``tool-churn`` demo stays one lane).
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tokenbill.adapters.fingerprint import (
    FingerprintCache,
    fingerprint_request,
    request_breakpoints,
)
from tokenbill.common import TraceError
from tokenbill.core import facts as core_facts
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import is_opaque_ref, key_id, pseudonym, request_id_for, stable_id
from tokenbill.core.jsonl import iter_lines
from tokenbill.core.models import normalize_model
from tokenbill.core.records import (
    MAX_TOKENS,
    Attempt,
    Attribution,
    Breakpoint,
    ContentTier,
    Fidelity,
    Inference,
    InferenceKind,
    Lane,
    LaneKind,
    Outcome,
    PricingContext,
    Request,
    RequestParams,
    Session,
    SourceRef,
    UsageBuckets,
)
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    SourceInfo,
)
from tokenbill.trace import SCHEMA as TRACE1_SCHEMA
from tokenbill.trace import Call, _parse_call

__all__ = ["ADAPTER_NAME", "CAPABILITIES", "PRIORITY", "TraceV1Adapter"]

ADAPTER_NAME = "trace@1"
#: Declared maximum capabilities (§5.5): ``blocks`` only in the fingerprint / full tiers.
CAPABILITIES = frozenset({"usage_sequence", "timing", "params", "blocks"})
#: Source priority of v0.1 recorder traces (SourceRef.priority: recorder 50).
PRIORITY = 50
_CHANNEL = "anthropic_api"
_DEFAULT_BILLING_PATH = "api_key"
_DEFAULT_FAMILY = "claude-4.7+"
_FIELD_RE = re.compile(r"field '([A-Za-z_.]+)")
_MISSING_RE = re.compile(r"missing field '([A-Za-z_.]+)'")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_MODEL_RE = re.compile(r"[A-Za-z0-9_.:@/+\[\]-]{1,128}\Z")
_DETAILS = {
    "dq.ttl_1h_markers_in_trace1": "calls with ttl 1h cache_control markers: cache writes are "
                                   "unknown-TTL ranges [5m, 1h] (trace@1 has no TTL split)",
    "dq.breakpoint_assumed_end": "cache_breakpoints count without marker positions: one assumed "
                                 "breakpoint at the last block",
    "dq.lanes_inferred": "runs with more than one model split into one lane per model",
}


def _reject_constant(_token: str) -> Any:
    raise ValueError("non-finite number")


def _reason(exc: TraceError) -> str:
    msg = str(exc)
    m = _MISSING_RE.search(msg)
    if m:
        return f"missing:{m.group(1)}"
    m = _FIELD_RE.search(msg)
    return f"bad_type:{m.group(1)}" if m else "bad_type:call"


def _ts_ms(ts: float) -> int | None:
    """Unix seconds → int ms, rounded half-even exactly (None when out of range)."""
    try:
        ms = (Decimal(repr(ts)) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError):
        return None
    if ms < 0 or ms > MAX_TOKENS:
        return None
    return int(ms)


def _has_1h(value: Any, _depth: int = 0) -> bool:
    if _depth > 64:
        return False
    if isinstance(value, Mapping):
        cc = value.get("cache_control")
        if isinstance(cc, Mapping) and cc.get("ttl") == "1h":
            return True
        return any(_has_1h(v, _depth + 1) for v in value.values()
                   if isinstance(v, (Mapping, list, tuple)))
    if isinstance(value, (list, tuple)):
        return any(_has_1h(v, _depth + 1) for v in value)
    return False


@functools.lru_cache(maxsize=256)
def _tokenizer_family(model: str) -> str:
    """The tokenizer family of *model*'s latest ``anthropic_api`` rate row (default 4.7+)."""
    if not model:
        return _DEFAULT_FAMILY
    try:
        rows = core_facts.load().rows_for(model)
    except Exception:  # pragma: no cover - facts.json is packaged with core
        return _DEFAULT_FAMILY
    return rows[-1].tokenizer_family if rows else _DEFAULT_FAMILY


def _identity(opts: IngestOptions) -> tuple[str | None, str | None]:
    """``(principal, principal key id)`` for the §5.1 identity modes."""
    ref = opts.principal_ref
    if ref is None:
        p = opts.attribution.principal
        if p is not None and p[:2] in ("p_", "c_"):
            kid = opts.principal_key_id or (key_id(opts.principal_key)
                                            if opts.principal_key else None)
            return p, kid
        return p, None
    if not is_opaque_ref(ref):
        raise UsageError("principal_ref must be an opaque id ([A-Za-z0-9._-]{1,64}, no '@')")
    mode = opts.identity_mode
    if mode == "central":
        return "r_" + ref, None
    if not opts.principal_key:
        raise UsageError(f"identity mode {mode} needs a principal key")
    kid = opts.principal_key_id or key_id(opts.principal_key)
    return pseudonym(opts.principal_key, "c" if mode == "two-stage" else "p", ref), kid


def _source_id(opts: IngestOptions, path: Path) -> str:
    try:
        resolved = str(Path(path).resolve())
    except OSError:  # pragma: no cover
        resolved = str(path)
    if opts.name_key:
        return pseudonym(opts.name_key, "s", f"{ADAPTER_NAME}:{resolved}")
    return stable_id("s", ADAPTER_NAME, resolved)


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


@dataclass
class _Ctx:
    source_id: str
    tier: ContentTier
    key: bytes
    attribution: Attribution
    billing_path: str
    cache: FingerprintCache
    dq: Counter[str]


class _Bad(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse(raw: bytes, line_no: int, first: bool) -> Call:
    if not raw:
        raise _Bad("oversize_line")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Bad("bad_json") from None
    if first and text.startswith("﻿"):
        text = text[1:]
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _Bad("bad_json") from None
    if not isinstance(obj, dict):
        raise _Bad("not_object")
    schema = obj.get("schema")
    if schema is None:
        raise _Bad("missing:schema")
    if schema != TRACE1_SCHEMA:
        raise _Bad("bad_type:schema")
    try:
        call = _parse_call(obj, f"line {line_no}")
    except TraceError as exc:
        raise _Bad(_reason(exc)) from None
    except RecursionError:  # pragma: no cover - json.loads already bounded the nesting
        raise _Bad("bad_json") from None
    if not _MODEL_RE.match(call.model):
        raise _Bad("bad_type:model")
    return call


def _request(call: Call, line_no: int, ts_ms: int, session_key: str, lane_key: str,
             ctx: _Ctx) -> Request:
    rid = request_id_for("anthropic", None, ctx.source_id, f"run:{call.run_id}#{call.index}")
    u = call.usage
    one_hour = _has_1h(call.tools) or _has_1h(call.messages)
    writes = u.cache_creation_input_tokens
    if one_hour:
        ctx.dq["dq.ttl_1h_markers_in_trace1"] += 1
    usage = UsageBuckets(uncached_input=u.input_tokens, cache_read=u.cache_read_input_tokens,
                         cache_write_5m=0 if one_hour else writes,
                         cache_write_unknown=writes if one_hour else 0, output=u.output_tokens)
    model = normalize_model(call.model).model
    pricing = PricingContext(provider="anthropic", channel=_CHANNEL, model=model,
                             model_raw=call.model, billing_path=ctx.billing_path,
                             write_ttl_hint="1h" if one_hour else None)
    fingerprint = None
    if ctx.tier is ContentTier.NONE:
        markers, n_blocks = request_breakpoints(tools=call.tools, system=call.system,
                                                messages=call.messages)
    else:
        fingerprint, markers, _content = fingerprint_request(
            tools=call.tools, system=call.system, messages=call.messages, key=ctx.key,
            tier=ctx.tier, tokenizer_family=_tokenizer_family(model), cache=ctx.cache)
        n_blocks = len(fingerprint.blocks)
    bps: list[Breakpoint] = list(markers)
    if call.cache_breakpoints > len(bps) and n_blocks:
        last = n_blocks - 1
        if all(bp.block_index != last for bp in bps):
            bps.append(Breakpoint(block_index=last, ttl="5m", assumed=True))
            ctx.dq["dq.breakpoint_assumed_end"] += 1
    params = RequestParams(model_requested=call.model, breakpoints=tuple(bps),
                           automatic_caching=False if call.cache_breakpoints == 0 and not bps
                           else None)
    inf = Inference(inference_id=stable_id("inf", rid, 0), kind=InferenceKind.MESSAGE,
                    usage=usage, pricing=pricing)
    raw_usage = json.dumps({"cache_creation_input_tokens": writes,
                            "cache_read_input_tokens": u.cache_read_input_tokens,
                            "input_tokens": u.input_tokens, "output_tokens": u.output_tokens},
                           sort_keys=True, separators=(",", ":"))
    stop = call.stop_reason if _TOKEN_RE.match(call.stop_reason) else None
    attempt = Attempt(attempt_id=stable_id("at", rid, 0), attempt_no=0, ts_start_ms=ts_ms,
                      ttft_ms=None, duration_ms=None, outcome=Outcome.OK, http_status=None,
                      error_type=None, retry_layer=None, retry_after_ms=None, should_retry=None,
                      provider_request_id=None, provider_message_id=None, model_served=None,
                      stop_reason=stop, inferences=(inf,), raw_usage_json=raw_usage)
    source = SourceRef(adapter=ADAPTER_NAME, source_id=ctx.source_id, locator=f"line:{line_no}",
                       fidelity=Fidelity.NO_TTL_SPLIT, priority=PRIORITY)
    return Request(request_id=rid, session_key=session_key, lane_key=lane_key, seq=call.index,
                   attribution=ctx.attribution, params=params, attempts=(attempt,),
                   fingerprint=fingerprint, source=source)


class TraceV1Adapter:
    """Reads ``tokenbill/trace@1`` JSONL files (registry name ``trace@1``, SPEC §5.5)."""

    name = ADAPTER_NAME
    capabilities = CAPABILITIES

    def sniff(self, path: Path, head: bytes) -> bool:
        """True when the first line is a trace@1 call (its schema, or the canonical key order of
        ``trace.write_trace``, whose ``schema`` key may lie beyond the sniffed head)."""
        line = head.split(b"\n", 1)[0].lstrip()
        if line.startswith(b"\xef\xbb\xbf"):
            line = line[3:]
        if not line.startswith(b"{"):
            return False
        if b'"tokenbill/trace@1"' in line:
            return True
        return line.startswith(b'{"cache_breakpoints":') and b'"index":' in line[:80]

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Decode *path* into an :class:`~tokenbill.core.types.IngestResult`."""
        if not isinstance(opts, IngestOptions):
            raise UsageError("read expects IngestOptions")
        try:
            tier = ContentTier(opts.content_tier)
        except ValueError:
            raise UsageError("unknown content tier") from None
        if tier is not ContentTier.NONE and not opts.name_key:
            raise UsageError("trace@1 fingerprints need a name key (IngestOptions.name_key)")
        path = Path(path)
        source_id = _source_id(opts, path)
        sha, n_bytes = _sha256_file(path)
        principal, principal_kid = _identity(opts)
        billing_path = opts.attribution.billing_path or _DEFAULT_BILLING_PATH
        attribution = replace(opts.attribution, principal=principal, billing_path=billing_path)
        ctx = _Ctx(source_id=source_id, tier=tier, key=bytes(opts.name_key), dq=Counter(),
                   attribution=attribution, billing_path=billing_path, cache=FingerprintCache())
        runs: dict[str, list[tuple[int, int, Call]]] = {}
        last_index: dict[str, int] = {}
        quarantined: list[QuarantineItem] = []
        stats: Counter[str] = Counter()
        since, until = opts.since_ms, opts.until_ms
        first = True
        for line_no, _off, raw in iter_lines(path):
            stats["lines"] += 1
            try:
                call = _parse(raw, line_no, first)
                prev = last_index.get(call.run_id)
                if prev is not None and call.index <= prev:
                    raise _Bad("bad_type:index")
                ts_ms = _ts_ms(call.ts)
                if ts_ms is None:
                    raise _Bad("bad_type:ts")
            except _Bad as bad:
                if not opts.lenient:
                    raise SourceError(f"{path.name}: line {line_no}: {bad.reason}") from None
                quarantined.append(QuarantineItem(source_id=source_id, locator=f"line:{line_no}",
                                                  reason=bad.reason))
                continue
            finally:
                first = False
            last_index[call.run_id] = call.index
            stats["records"] += 1
            if (since is not None and ts_ms < since) or (until is not None and ts_ms >= until):
                stats["skipped_window"] += 1
                continue
            runs.setdefault(call.run_id, []).append((line_no, ts_ms, call))
        requests: list[Request] = []
        sessions: list[Session] = []
        for run_id, calls in runs.items():
            session_key = stable_id("ses", source_id, run_id)
            models = list(dict.fromkeys(c.model for _n, _t, c in calls))
            split = len(models) > 1
            lane_of: dict[str, str] = {}
            for m in models:
                lane_of[m] = (stable_id("ln", session_key, "model", m) if split
                              else stable_id("ln", session_key, "run"))
            if split:
                ctx.dq["dq.lanes_inferred"] += 1
            for line_no, ts_ms, call in calls:
                requests.append(_request(call, line_no, ts_ms, session_key, lane_of[call.model],
                                         ctx))
            lanes = tuple(Lane(lane_key=key, session_key=session_key, kind=LaneKind.API_RUN,
                               parent_lane_key=None, cache_scope_key="unknown", requests=(),
                               lane_exact=not split)
                          for key in dict.fromkeys(lane_of[m] for m in models))
            times = [t for _n, t, _c in calls]
            sessions.append(Session(session_key=session_key, source_kind=ADAPTER_NAME,
                                    attribution=attribution, lanes=lanes,
                                    started_ms=min(times), ended_ms=max(times)))
        notes = [DataQualityNote(code=code, severity="info", count=count, detail=_DETAILS[code])
                 for code, count in sorted(ctx.dq.items()) if count]
        if quarantined:
            reasons = Counter(q.reason for q in quarantined)
            notes.append(DataQualityNote(
                code="dq.quarantined", severity="warn", count=len(quarantined),
                detail=", ".join(f"{r}={c}" for r, c in sorted(reasons.items()))[:256]))
        stats.update({"requests": len(requests), "runs": len(runs),
                      "quarantined": len(quarantined)})
        caps: set[str] = set()
        if requests:
            caps |= {"usage_sequence", "timing", "params"}
            if any(r.fingerprint is not None for r in requests):
                caps.add("blocks")
        name_hmac = pseudonym(opts.name_key, "h", path.name) if opts.name_key else ""
        source = SourceInfo(source_id=source_id, adapter=ADAPTER_NAME, name_hmac=name_hmac,
                            sha256=sha, bytes=n_bytes,
                            name_key_id=key_id(opts.name_key) if opts.name_key else None,
                            principal_key_id=principal_kid)
        return IngestResult(source=source, requests=requests, sessions=sessions, events=[],
                            aggregates=[], cost_lines=[], outcomes=[], quarantined=quarantined,
                            notes=notes, stats={k: int(v) for k, v in sorted(stats.items())},
                            capabilities=frozenset(caps))


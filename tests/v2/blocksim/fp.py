"""A small test-local block fingerprinter following SPEC §5.8, and a trace@1 → Lane bridge
following SPEC §5.5 (imported only by tests and the fixture build script of this area).

TRACE owns the real ``adapters.fingerprint`` / ``adapters.trace_v1``; this helper lets the BLOCK
tests build realistic ``ContentFingerprint``s from request payloads (tools, system, messages)
without depending on a sibling package. Rules implemented (§5.8): blocks in wire order (one per
tool definition; a string system prompt is one ``system_text`` block, a list one block per
element; a string message content is one ``text`` block, a list one block per element); every
``cache_control`` key removed recursively before hashing and recorded as a breakpoint
``(block index, ttl or "5m")``; ``h`` = HMAC of the tier tag + the wire JSON in the key order as
sent; ``h_sorted`` = HMAC of the key-sorted canonical JSON; volatile classes (frozen
``breakers.VOLATILE_PATTERNS``) found in the block's strings before hashing and ``h_norm`` = HMAC
of the wire after replacing them by ``<class>`` placeholders; ``n_bytes`` = UTF-8 length of the
wire; ``est_tokens = ceil(n_bytes / cpt)`` (2.5 tool results, 3.6 text, 2.7 tool definitions);
collapsed lookback positions (runs of tool_use / tool_result share a position); ``deferred`` for
``defer_loading`` tools.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from tokenbill.breakers import VOLATILE_PATTERNS
from tokenbill.common import canonical_json
from tokenbill.core.builders import make_attempt, make_inference, make_lane, make_request
from tokenbill.core.ids import key_id, stable_id
from tokenbill.core.models import normalize_model
from tokenbill.core.records import (
    Attribution,
    BlockRef,
    Breakpoint,
    ContentFingerprint,
    Lane,
    LaneKind,
    Request,
    RequestParams,
    UsageBuckets,
)

KEY = b"tokenbill-blocksim-test-key-0001"
KEY_ID = key_id(KEY)
VOLATILE_CLASSES = ("iso_datetime", "iso_date", "clock_time", "unix_ts", "uuid", "counter")
_CPT = {"tool_result": 2.5, "tool_def": 2.7}
_KINDS = frozenset({"text", "tool_use", "tool_result", "image", "document", "thinking",
                    "redacted_thinking", "compaction"})
_RUNS = frozenset({"tool_use", "tool_result"})


def _hmac(key: bytes, data: bytes, n: int = 32) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()[:n]


def volatile_spans(text: str) -> list[tuple[str, int, int]]:
    """Non-overlapping ``(class, start, end)`` spans; earlier patterns win (an ISO datetime is
    one span, not a date plus a time)."""
    spans: list[tuple[str, int, int]] = []
    for cls, pattern in zip(VOLATILE_CLASSES, VOLATILE_PATTERNS, strict=True):
        for m in pattern.finditer(text):
            if all(m.end() <= s or m.start() >= e for _c, s, e in spans):
                spans.append((cls, m.start(), m.end()))
    return sorted(spans, key=lambda sp: sp[1])


def normalize_volatile(text: str) -> str:
    out, pos = [], 0
    for cls, start, end in volatile_spans(text):
        out.append(text[pos:start])
        out.append(f"<{cls}>")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _strip(obj: Any, markers: list[str]) -> Any:
    if isinstance(obj, Mapping):
        cc = obj.get("cache_control")
        if isinstance(cc, Mapping):
            markers.append(str(cc.get("ttl") or "5m"))
        return {k: _strip(v, markers) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip(v, markers) for v in obj]
    return obj


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, Mapping):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def _block(raw: Mapping[str, Any], tier: str, kind: str, role: str | None, key: bytes,
           markers: list[str]) -> BlockRef:
    before = len(markers)
    stripped = _strip(raw, markers)
    del markers[before + 1:]                    # one breakpoint per block at most
    wire = json.dumps(stripped, ensure_ascii=False, separators=(",", ":"))
    tag = f"{tier}\x1f".encode()
    classes = sorted({cls for s in _strings(stripped) for cls, _a, _b in volatile_spans(s)})
    n_bytes = len(wire.encode("utf-8"))
    cpt = _CPT.get(kind, 3.6)
    return BlockRef(
        h=_hmac(key, tag + wire.encode("utf-8")),
        h_sorted=_hmac(key, tag + canonical_json(stripped).encode("utf-8")),
        h_norm=_hmac(key, tag + normalize_volatile(wire).encode("utf-8")),
        tier=tier, kind=kind, role=role, n_bytes=n_bytes,
        est_tokens=math.ceil(n_bytes / cpt), volatile_classes=tuple(classes),
        deferred=bool(raw.get("defer_loading")) if tier == "tools" else False,
    )


def fingerprint(tools: Sequence[Mapping[str, Any]], system: str | Sequence[Mapping] | None,
                messages: Sequence[Mapping[str, Any]], *, key: bytes = KEY
                ) -> tuple[ContentFingerprint, tuple[Breakpoint, ...]]:
    """``(fingerprint, observed breakpoints)`` of one request payload (SPEC §5.8)."""
    blocks: list[BlockRef] = []
    bps: list[Breakpoint] = []

    def push(raw: Mapping[str, Any], tier: str, kind: str, role: str | None) -> None:
        markers: list[str] = []
        blocks.append(_block(raw, tier, kind, role, key, markers))
        if markers:
            bps.append(Breakpoint(len(blocks) - 1, markers[0]))

    for t in tools:
        push(t, "tools", "tool_def", None)
    te0 = len(blocks)
    if isinstance(system, str):
        push({"type": "text", "text": system}, "system", "system_text", None)
    elif system:
        for sb in system:
            push(sb, "system", "system_text", None)
    te1 = len(blocks)
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            push({"type": "text", "text": content}, "messages", "text", role)
        else:
            for cb in content or ():
                kind = cb.get("type") if cb.get("type") in _KINDS else "other"
                push(cb, "messages", kind, role)
    positions = []
    for i, b in enumerate(blocks):
        if i and b.kind in _RUNS and blocks[i - 1].kind == b.kind:
            positions.append(positions[-1])
        else:
            positions.append(positions[-1] + 1 if positions else 0)
    blocks = [BlockRef(**{**{f: getattr(b, f) for f in BlockRef.__slots__},
                          "lookback_pos": p}) for b, p in zip(blocks, positions, strict=True)]
    fp = ContentFingerprint(key_id=KEY_ID if key == KEY else key_id(key), blocks=tuple(blocks),
                            tier_end=(te0, te1, len(blocks)))
    return fp, tuple(bps)


def payload_request(lane_key: str, seq: int, ts_ms: int, *, tools: Sequence[Mapping],
                    system: str | Sequence[Mapping] | None, messages: Sequence[Mapping],
                    billed: UsageBuckets, model: str = "claude-opus-5-5",
                    attribution: Mapping[str, Any] | None = None, ttft_ms: int | None = None,
                    breakpoint_count: int | None = None, **params: Any) -> Request:
    """A Request from a raw payload: fingerprint + markers (+ one assumed end breakpoint when
    *breakpoint_count* exceeds the recoverable markers, §5.5)."""
    fp, bps = fingerprint(tools, system, messages)
    marks = list(bps)
    if breakpoint_count is not None and breakpoint_count > len(marks) and fp.blocks:
        last = len(fp.blocks) - 1
        if all(b.block_index != last for b in marks):
            marks.append(Breakpoint(last, "5m", assumed=True))
    auto = False if breakpoint_count == 0 and not marks else None
    rp = RequestParams(model_requested=model, breakpoints=tuple(marks), automatic_caching=auto,
                       **params)
    rid = stable_id("rq", lane_key, seq)
    inf = make_inference(billed, model=normalize_model(model).model or model,
                         inference_id=stable_id("inf", rid, 0), billing_path="api_key")
    att = make_attempt([inf], ts_ms=ts_ms, attempt_id=stable_id("at", rid, 0), ttft_ms=ttft_ms,
                       model_served=model)
    attr = Attribution(**{"billing_path": "api_key", **dict(attribution or {})})
    return make_request(lane_key, seq, ts_ms, billed, model, request_id=rid, attribution=attr,
                        params=rp, attempts=[att], fingerprint=fp)


def calls_to_lane(calls: Sequence[Any], *, lane_key: str | None = None,
                  scope: str = "unknown") -> Lane:
    """The trace@1 bridge of SPEC §5.5 for one v0.1 run: one API_RUN lane, one request per call
    (uncached = input_tokens, reads, writes as 5m), explicit markers as breakpoints and one
    assumed end breakpoint for a positive ``cache_breakpoints`` count without positions."""
    key = lane_key or stable_id("ln", calls[0].run_id)
    reqs = []
    for call in calls:
        u = call.usage
        billed = UsageBuckets(uncached_input=u.input_tokens, cache_read=u.cache_read_input_tokens,
                              cache_write_5m=u.cache_creation_input_tokens,
                              output=u.output_tokens)
        reqs.append(payload_request(key, call.index, int(round(call.ts * 1000)),
                                    tools=call.tools, system=call.system,
                                    messages=call.messages, billed=billed, model=call.model,
                                    breakpoint_count=call.cache_breakpoints))
    return make_lane(reqs, kind=LaneKind.API_RUN, scope=scope, lane_key=key)

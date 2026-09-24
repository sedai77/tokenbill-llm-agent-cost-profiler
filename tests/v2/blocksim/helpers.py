"""Area-local fixture builders for the BLOCK tests (imported only by tests in
``tests/v2/blocksim``).

Blocks are built with ``core.builders.make_block``: ``h`` is derived from a short name, sizes are
exact integers (``est_tokens``) and billed totals equal the sum of the block sizes, so every
expected token count and dollar figure below can be computed by hand. Every fixture is synthetic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tokenbill.core.builders import (
    make_attempt,
    make_block,
    make_fingerprint,
    make_inference,
    make_lane,
    make_request,
)
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.ids import stable_id
from tokenbill.core.records import (
    Attribution,
    BlockRef,
    Breakpoint,
    Lane,
    LaneEvent,
    LaneKind,
    Request,
    RequestParams,
    UsageBuckets,
)
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import AnalysisContext

#: 2026-09-23 00:00 UTC (every FakePricer model is effective by then).
T0 = 1_790_121_600_000
S = 1000
CAPS = frozenset({"blocks", "usage_sequence", "timing", "params"})

# Opus 5.5 unit prices in nano-USD per token (SPEC Appendix A: $4 / $0.20 / $5 / $8 per MTok).
U, R, W5, W1 = 4000, 200, 5000, 8000

_BLOCKS: dict[tuple, BlockRef] = {}


def blk(name: str, tokens: int = 1000, *, tier: str = "messages", kind: str = "text",
        role: str | None = "user", norm: str | None = None, srt: str | None = None,
        volatile: Sequence[str] = ()) -> BlockRef:
    """A block of *tokens* size units whose hash is ``h:<name>`` (the same arguments return the
    same object, like a delta-encoded trace reader). *norm* / *srt* name the normalized and
    key-sorted hashes (default: the block's own)."""
    key = (name, tokens, tier, kind, role, norm, srt, tuple(volatile))
    got = _BLOCKS.get(key)
    if got is None:
        got = make_block(f"h:{name}", tier=tier, kind=kind, n_bytes=tokens * 4, role=role,
                         est_tokens=tokens, h_sorted=f"s:{srt or name}",
                         h_norm=f"n:{norm or name}", volatile_classes=tuple(volatile))
        _BLOCKS[key] = got
    return got


def tool(name: str, tokens: int = 500, **kw: Any) -> BlockRef:
    return blk(name, tokens, tier="tools", kind="tool_def", role=None, **kw)


def system(name: str, tokens: int = 2000, **kw: Any) -> BlockRef:
    return blk(name, tokens, tier="system", kind="system_text", role=None, **kw)


def usage(r: int = 0, w5: int = 0, w1: int = 0, u: int = 0, o: int = 100) -> UsageBuckets:
    return UsageBuckets(cache_read=r, cache_write_5m=w5, cache_write_1h=w1, uncached_input=u,
                        output=o)


def size(blocks: Sequence[BlockRef]) -> int:
    return sum(b.est_tokens or 0 for b in blocks)


def req(lane_key: str, seq: int, ts_s: float, blocks: Sequence[BlockRef], billed: UsageBuckets,
        *, model: str = "claude-opus-5-5", bps: Sequence[tuple[int, str]] | str | None = "end",
        assumed: bool = False, auto: bool | None = None, ttft_ms: int | None = None,
        duration_ms: int | None = None, attribution: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None, request_id: str | None = None,
        **ctx_kw: Any) -> Request:
    """A fingerprinted request. *bps*: ``"end"`` (one 5m breakpoint on the last block), a list of
    ``(block index, ttl)`` or None (no markers)."""
    fp = make_fingerprint(blocks)
    if bps == "end":
        marks: tuple[Breakpoint, ...] = (Breakpoint(len(blocks) - 1, "5m", assumed),)
    elif bps is None:
        marks = ()
    else:
        marks = tuple(Breakpoint(i, ttl, assumed) for i, ttl in bps)
    rp = RequestParams(model_requested=model, breakpoints=marks, automatic_caching=auto,
                       **dict(params or {}))
    rid = request_id or stable_id("rq", lane_key, seq)
    ts = T0 + int(round(ts_s * S))
    inf = make_inference(billed, model=model, inference_id=stable_id("inf", rid, 0), **ctx_kw)
    att = make_attempt([inf], ts_ms=ts, attempt_id=stable_id("at", rid, 0), ttft_ms=ttft_ms,
                       duration_ms=duration_ms, model_served=model)
    attr = Attribution(**dict(attribution or {}))
    return make_request(lane_key, seq, ts, billed, model, request_id=rid, attribution=attr,
                        params=rp, attempts=[att], fingerprint=fp)


def lane(requests: Sequence[Request], *, kind: LaneKind | str = LaneKind.API_RUN,
         scope: str = "ws:test", events: Sequence[LaneEvent] = ()) -> Lane:
    return make_lane(list(requests), kind=kind, scope=scope, events=list(events))


def conversation(lane_key: str, prefix: Sequence[BlockRef], turns: Sequence[Sequence[BlockRef]],
                 *, billed: str = "working", gap_s: float = 30, start_s: float = 0,
                 **req_kw: Any) -> list[Request]:
    """Requests whose block list grows by one turn each: ``prefix + turns[0] + … + turns[i]``.
    *billed*: ``working`` (reads = previous total, writes = the rest), ``cold`` (full 5m writes)
    or ``none`` (all uncached)."""
    out = []
    blocks: list[BlockRef] = list(prefix)
    prev_total = 0
    for i, turn in enumerate(turns):
        blocks = blocks + list(turn)
        total = size(blocks)
        if billed == "working":
            bu = usage(r=prev_total, w5=total - prev_total)
        elif billed == "cold":
            bu = usage(w5=total)
        else:
            bu = usage(u=total)
        out.append(req(lane_key, i, start_s + i * gap_s, blocks, bu, **req_kw))
        prev_total = total
    return out


def context(**thresholds: str) -> AnalysisContext:
    """An AnalysisContext on the foundation fakes (FakePricer, RulesTable) with *thresholds*."""
    return AnalysisContext(pricer=FakePricer(), rules=RulesTable(), replayer=None,
                           calibration=None, window=(0, 2**53), capabilities=CAPS,
                           thresholds=dict(thresholds))


def kinds(findings: Sequence[Any]) -> list[str]:
    return sorted(f.kind for f in findings)

"""Area-local fixture helpers for the F-SEM tests (imported only by tests in ``tests/v2/sem``).

Every fixture is synthetic, built with ``core.builders`` (never real transcripts).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tokenbill.core.builders import make_ctx, make_inference, make_lane, make_request
from tokenbill.core.records import (
    Attribution,
    Lane,
    LaneEvent,
    LaneKind,
    Request,
    RequestParams,
    UsageBuckets,
)

S = 1000  # ms per second


def usage(r: int = 0, w5: int = 0, w1: int = 0, u: int = 0, o: int = 500,
          **kw: int) -> UsageBuckets:
    """UsageBuckets from short names: reads, 5m writes, 1h writes, uncached, output."""
    return UsageBuckets(cache_read=r, cache_write_5m=w5, cache_write_1h=w1, uncached_input=u,
                        output=o, **kw)


def req(lane_key: str, seq: int, ts_s: float, u: UsageBuckets, model: str = "claude-opus-5-5", *,
        attribution: Attribution | dict[str, Any] | None = None,
        params: RequestParams | None = None, **kw: Any) -> Request:
    """``make_request`` with the timestamp in seconds."""
    if params is None:
        params = RequestParams(model_requested=model)
    return make_request(lane_key, seq, int(round(ts_s * S)), u, model,
                        attribution=attribution, params=params, **kw)


def lane(requests: Sequence[Request], *, kind: LaneKind | str = LaneKind.MAIN,
         events: Sequence[LaneEvent] = (), scope: str = "ws:test", lane_key: str = "L") -> Lane:
    return make_lane(list(requests), kind=kind, events=list(events), scope=scope, lane_key=lane_key,
                     session_key="s_test")


def two_step(first: UsageBuckets, second: UsageBuckets, *, gap_s: float = 30,
             model: str = "claude-opus-5-5", model2: str | None = None,
             events: Sequence[LaneEvent] = (), kind: LaneKind | str = LaneKind.MAIN,
             kw1: dict[str, Any] | None = None, kw2: dict[str, Any] | None = None) -> Lane:
    """A two-request lane (one transition)."""
    r0 = req("L", 0, 0, first, model, **(kw1 or {}))
    r1 = req("L", 1, gap_s, second, model2 or model, **(kw2 or {}))
    return lane([r0, r1], events=events, kind=kind)


def event(kind: str, ts_s: float, lane_key: str = "L", **attrs: Any) -> LaneEvent:
    return LaneEvent(lane_key=lane_key, ts_ms=int(round(ts_s * S)), kind=kind,
                     attrs=tuple(attrs.items()))


__all__ = ["S", "event", "lane", "make_ctx", "make_inference", "req", "two_step", "usage"]

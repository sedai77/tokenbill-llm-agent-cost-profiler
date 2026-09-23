"""Lane assembly (SPEC §3.13).

Adapters emit loose requests and events plus Session/Lane *shells* (lane kind, parent, cache scope);
:func:`group_lanes` assembles the final, sorted :class:`~tokenbill.core.records.Lane` objects
deterministically.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from tokenbill.core.records import Lane, LaneEvent, LaneKind, Request, Session

__all__ = ["group_lanes", "ttl_of_last_write"]

_HINT_TTL_S = {"5m": 300, "1h": 3600}


def _ttl_observed(requests: Iterable[Request]) -> str:
    saw_5m = saw_1h = False
    for req in requests:
        for inf in req.billable_inferences:
            saw_5m = saw_5m or inf.usage.cache_write_5m > 0
            saw_1h = saw_1h or inf.usage.cache_write_1h > 0
    if saw_5m and saw_1h:
        return "mixed"
    if saw_1h:
        return "1h"
    if saw_5m:
        return "5m"
    return "unknown"


def group_lanes(
    requests: Iterable[Request], events: Iterable[LaneEvent], sessions: Iterable[Session] = ()
) -> list[Lane]:
    """Group requests and events by ``lane_key`` into Lanes.

    Requests are sorted by ``(ts_start_ms, seq)`` and events by ``ts_ms`` (ties broken by stable
    keys). Lane kind, parent and cache scope come from the Session/Lane shells when present, else
    ``UNKNOWN`` / ``None`` / ``"unknown"``. Requests or events carried inside a shell are merged
    with the loose ones (a request id or an identical event counts once). ``ttl_observed``: ``"1h"``
    if any billed ``cache_write_1h`` and no 5m, ``"5m"`` if 5m only, ``"mixed"`` if both, else
    ``"unknown"``. Output order: by ``(session_key, lane_key)``.
    """
    shells: dict[str, Lane] = {}
    for session in sessions:
        for lane in session.lanes:
            shells.setdefault(lane.lane_key, lane)

    by_lane: dict[str, dict[str, Request]] = {}
    for req in requests:
        by_lane.setdefault(req.lane_key, {}).setdefault(req.request_id, req)
    events_by_lane: dict[str, list[LaneEvent]] = {}
    for ev in events:
        events_by_lane.setdefault(ev.lane_key, []).append(ev)

    for key, shell in shells.items():
        bucket = by_lane.setdefault(key, {})
        for req in shell.requests:
            bucket.setdefault(req.request_id, req)
        if shell.events:
            loose = events_by_lane.setdefault(key, [])
            remaining = Counter(loose)
            for ev in shell.events:
                if remaining[ev] > 0:
                    remaining[ev] -= 1
                else:
                    loose.append(ev)

    lanes: list[Lane] = []
    for key in set(by_lane) | set(events_by_lane) | set(shells):
        reqs = tuple(by_lane.get(key, {}).values())
        shell = shells.get(key)
        if shell is not None:
            session_key = shell.session_key
        elif reqs:
            session_key = min(r.session_key for r in reqs)
        else:
            session_key = ""
        lanes.append(
            Lane(
                lane_key=key,
                session_key=session_key,
                kind=shell.kind if shell is not None else LaneKind.UNKNOWN,
                parent_lane_key=shell.parent_lane_key if shell is not None else None,
                cache_scope_key=shell.cache_scope_key if shell is not None else "unknown",
                requests=reqs,
                events=tuple(events_by_lane.get(key, ())),
                ttl_observed=_ttl_observed(reqs),
                lane_exact=shell.lane_exact if shell is not None else True,
            )
        )
    lanes.sort(key=lambda lane: (lane.session_key, lane.lane_key))
    return lanes


def ttl_of_last_write(lane: Lane, before_index: int) -> int | None:
    """TTL in seconds of the most recent billed cache write before ``lane.requests[before_index]``.

    3600 (1h), 300 (5m), the source's ``cache_write_other_ttl_s``, or the ``write_ttl_hint`` of an
    unknown-TTL write; when one inference wrote several classes the shortest TTL is returned (the
    tail of the prefix expires first). None when there is no earlier write or its TTL is unknown.
    """
    last = min(before_index, len(lane.requests))
    for j in range(last - 1, -1, -1):
        for inf in reversed(lane.requests[j].billable_inferences):
            usage = inf.usage
            if usage.cache_write == 0:
                continue
            candidates: list[int] = []
            if usage.cache_write_5m:
                candidates.append(300)
            if usage.cache_write_1h:
                candidates.append(3600)
            if usage.cache_write_other and usage.cache_write_other_ttl_s is not None:
                candidates.append(usage.cache_write_other_ttl_s)
            if usage.cache_write_unknown and inf.pricing.write_ttl_hint in _HINT_TTL_S:
                candidates.append(_HINT_TTL_S[inf.pricing.write_ttl_hint])
            return min(candidates) if candidates else None
    return None

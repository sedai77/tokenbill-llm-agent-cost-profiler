"""Area-local helpers for the SYNTH-ORACLE tests (imported only inside ``tests/v2/synth_oracle``).

* :func:`lane` / :func:`req` build small hand-computable lanes on top of ``core.builders``, with
  timestamps in seconds from 2026-09-23 00:00 UTC (every FakePricer model is priced then).
* :func:`replay` runs a replayer (the oracle by default) with the FakePricer and F-SEM's rules.
* :func:`first_difference` is the differential comparator used by the merge-gate test: it compares
  two :class:`ReplayResult` objects to the nano (points and bounds) and names the first differing
  request in lane order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tokenbill.core.builders import lane_from_table, make_lane, make_request
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.labels import Figure
from tokenbill.core.policy import parse_policy
from tokenbill.core.protocols import Pricer, Replayer
from tokenbill.core.records import Inference, Lane, Request, UsageBuckets
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import CalibrationReport, Policy, ReplayResult
from tokenbill.synth.lanes_gen import EPOCH_MS
from tokenbill.synth.oracle import ReferenceReplay, outcome_bounds

PRICER = FakePricer()
RULES = RulesTable()
ORACLE = ReferenceReplay()
T0_S = EPOCH_MS // 1000
NANO = 1_000      # nano per token at $1/MTok


def shift(rows: Sequence[tuple[int, ...]]) -> list[tuple[int, ...]]:
    """Rows of ``(ts_s, R, W5, W1, U, O)`` with ``ts_s`` counted from 2026-09-23 00:00 UTC."""
    return [(T0_S + r[0], *r[1:]) for r in rows]


def lane(rows: Sequence[tuple[int, ...]], **kw: Any) -> Lane:
    """``core.builders.lane_from_table`` on shifted rows."""
    return lane_from_table(shift(rows), **kw)


def req(lane_key: str, seq: int, ts_s: int, usage: Mapping[str, int] | UsageBuckets,
        model: str = "claude-opus-5-5", **kw: Any) -> Request:
    """``core.builders.make_request`` with a timestamp in seconds from the test epoch."""
    return make_request(lane_key, seq, EPOCH_MS + ts_s * 1000, usage, model, **kw)


def lane_of(requests: Sequence[Request], **kw: Any) -> Lane:
    """``core.builders.make_lane``."""
    return make_lane(requests, **kw)


def policy(spec: str | Policy) -> Policy:
    return spec if isinstance(spec, Policy) else parse_policy(spec)


def replay(lanes: Sequence[Lane], spec: str | Policy, *, replayer: Replayer | None = None,
           pricer: Pricer = PRICER, mode: str = "documented",
           calibration: CalibrationReport | None = None,
           floor: Mapping[tuple[str, str], int] | None = None,
           keep: bool = True) -> ReplayResult:
    """Replay *lanes* under *spec* (a policy spec string or Policy)."""
    engine = replayer if replayer is not None else ORACLE
    return engine.replay(lanes, policy(spec), mode=mode, pricer=pricer, rules=RULES,
                         calibration=calibration, static_prefix_floor=floor,
                         keep_outcomes=keep)


def costs(result: ReplayResult) -> list[tuple[int | None, int | None, int | None]]:
    """Per-request ``(point, low, high)`` in outcome order (bounds normalized)."""
    assert result.outcomes is not None
    return [outcome_bounds(o.cost_nano, o.low_nano, o.high_nano) for o in result.outcomes]


def points(result: ReplayResult) -> list[int | None]:
    return [c[0] for c in costs(result)]


def bounds(fig: Figure) -> tuple[int | None, int | None, int | None]:
    return outcome_bounds(fig.nano, fig.low_nano, fig.high_nano)


# ---------------------------------------------------------------------------------------------
# differential comparison (merge gate SPEC §9.8)
# ---------------------------------------------------------------------------------------------

PRICED_BUCKETS = ("uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
                  "cache_write_other", "cache_write_other_ttl_s", "cache_write_unknown", "output",
                  "web_search_requests", "web_fetch_requests")


def priced_usage(u: UsageBuckets) -> tuple[int | None, ...]:
    """The priced fields of a usage (``output_reasoning`` is informational and never priced)."""
    return tuple(getattr(u, name) for name in PRICED_BUCKETS)


def extras_signature(extras: Sequence[Inference]) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Inserted inferences summed per kind: independent of how many ``Inference`` objects an
    engine uses for n pings, strict on what they bill."""
    per_kind: dict[str, UsageBuckets] = {}
    for inf in extras:
        key = inf.kind.value
        per_kind[key] = per_kind[key] + inf.usage if key in per_kind else inf.usage
    return tuple(sorted((k, priced_usage(v)) for k, v in per_kind.items()))  # type: ignore[misc]


def first_difference(expected: ReplayResult, actual: ReplayResult,
                     lanes: Sequence[Lane]) -> str | None:
    """None when *actual* equals *expected* to the nano (points and bounds of every outcome and
    every result figure, serving usage, inserted calls, per-lane totals and counts); otherwise a
    message naming the first differing request in lane order (or the aggregate that differs)."""
    if expected.outcomes is None or actual.outcomes is None:
        return "both replays must keep outcomes"
    exp = {o.request_id: o for o in expected.outcomes}
    act = {o.request_id: o for o in actual.outcomes}
    spec = expected.policy.spec() or "observed"
    for ln in sorted(lanes, key=lambda x: x.lane_key):
        for i, rq in enumerate(ln.requests):
            e, a = exp.get(rq.request_id), act.get(rq.request_id)
            where = (f"policy {spec!r}: lane {ln.lane_key} request #{i} ({rq.request_id}, "
                     f"ts {rq.ts_start_ms})")
            if e is None or a is None:
                return (f"{where}: missing outcome (oracle {e is not None}, "
                        f"actual {a is not None})")
            eb = outcome_bounds(e.cost_nano, e.low_nano, e.high_nano)
            ab = outcome_bounds(a.cost_nano, a.low_nano, a.high_nano)
            if eb != ab:
                return f"{where}: cost (point, low, high) {ab} != oracle {eb}"
            if priced_usage(e.usage) != priced_usage(a.usage):
                return (f"{where}: serving usage {priced_usage(a.usage)} != oracle "
                        f"{priced_usage(e.usage)} (fields {PRICED_BUCKETS})")
            if extras_signature(e.extra) != extras_signature(a.extra):
                return (f"{where}: inserted calls {extras_signature(a.extra)} != oracle "
                        f"{extras_signature(e.extra)}")
    if set(act) != set(exp):
        return f"policy {spec!r}: outcome request ids differ"
    for name in ("baseline", "cost", "saving"):
        eb, ab = bounds(getattr(expected, name)), bounds(getattr(actual, name))
        if eb != ab:
            return f"policy {spec!r}: {name} (point, low, high) {ab} != oracle {eb}"
    if dict(expected.per_lane) != dict(actual.per_lane):
        diff = sorted(k for k in set(dict(expected.per_lane)) | set(dict(actual.per_lane))
                      if dict(expected.per_lane).get(k) != dict(actual.per_lane).get(k))
        return f"policy {spec!r}: per_lane differs for {diff[:3]}"
    for name in ("added_calls", "keepalive_pings", "n_lanes", "n_requests"):
        if getattr(expected, name) != getattr(actual, name):
            return (f"policy {spec!r}: {name} {getattr(actual, name)} != oracle "
                    f"{getattr(expected, name)}")
    return None

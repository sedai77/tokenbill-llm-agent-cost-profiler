"""Property and fuzz tests (hypothesis): invariants of every replay on random lanes × random
policies, and malformed policy input (only ``TokenbillError`` subclasses may escape)."""

from __future__ import annotations

import json

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tokenbill.core.errors import TokenbillError, UsageError
from tokenbill.core.labels import Calibration
from tokenbill.core.policy import parse_policy
from tokenbill.core.records import to_json
from tokenbill.core.types import CalibrationReport, Policy

from .helpers import a1_lane, outcomes, priced_request, random_lanes, replay

_CLAUSES = (
    "ttl=1h", "ttl=5m@lane_kind:main", "ttl=1h@agent_product:claude_code",
    "keepalive=240s,max=3600s", "keepalive=120s,max=900s@agent_product:agent_sdk",
    "compact-window=30000,post=12000", "compact-window=9000", "cold-resume=compact,min=20000",
    "cold-resume=clear,min=8000", "model=claude-sonnet-5@lane_kind:subagent",
    "model=claude-sonnet-4-6@agent_product:claude_code", "model=claude-haiku-4-5@lane_kind:main",
    "effort=medium,scale=0.5", "effort=low,scale=0.25@lane_kind:main", "fast=off",
    "geo=global", "regional=global", "batch=eligible", "repair=restore_caching",
    "repair=stagger_fanout", "repair=retry_backoff_cap", "repair=fallback_credit",
    "repair=shared_ci_prefix",
)

_PASSING = CalibrationReport(
    granularity="day", n_periods=20, status="pass", mode_used="calibrated", nmbe_pct="1",
    cvrmse_pct="2", nmbe_pct_calibrated="0", cvrmse_pct_calibrated="1", thresholds=("10", "30"),
    rho=(("0s-60s", 95, 100, "0.9", "0.98"), ("60s-300s", 80, 100, "0.7", "0.87"),
         ("300s-3600s", 7, 10, "0.4", "0.9"), ("3600s+", 0, 0, "0", "1")),
    diag_confusion=(), diag_precision_recall=(), unlabeled=0, no_comparison_labels=0,
    ttl_corroboration=(0, 0), notes=())


def _leaves(obj: object) -> list:
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _leaves(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _leaves(v)]
    return [obj]


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 10_000),
       clauses=st.lists(st.sampled_from(_CLAUSES), min_size=1, max_size=4, unique=True),
       calibrated=st.booleans())
def test_random_policies_keep_every_invariant(seed: int, clauses: list[str],
                                              calibrated: bool) -> None:
    try:
        policy = parse_policy(";".join(clauses))
    except UsageError:
        assume(False)
        return
    lanes = random_lanes(seed, 8)
    mode = "calibrated" if calibrated else "documented"
    res = replay(lanes, policy, mode=mode, calibration=_PASSING if calibrated else None)
    again = replay(lanes, policy, mode=mode, calibration=_PASSING if calibrated else None)
    assert json.dumps(to_json(res), sort_keys=True) == json.dumps(to_json(again), sort_keys=True)
    assert not any(isinstance(v, float) for v in _leaves(to_json(res)))
    assert res.calibration is (Calibration.CALIBRATED if calibrated else Calibration.UNCALIBRATED)
    by_id = {r.request_id: r for lane in lanes for r in lane.requests}
    o = outcomes(res)
    assert set(o) == set(by_id) and res.n_requests == len(by_id)
    saving = 0
    total = 0
    priced = True
    for rid, x in o.items():
        ledger = priced_request(by_id[rid])
        if x.low_nano is not None:
            assert x.low_nano <= x.cost_nano <= x.high_nano
        if not x.changed:
            assert (x.cost_nano, x.low_nano, x.high_nano) == \
                (ledger.nano, ledger.low_nano, ledger.high_nano)
            assert not x.extra
        elif ledger.nano is not None and x.cost_nano is not None:
            saving += ledger.nano - x.cost_nano
        if x.cost_nano is None:
            priced = False
        else:
            total += x.cost_nano
    assert res.saving.nano == saving
    if priced:
        assert res.cost.nano == total
    else:
        assert res.cost.nano is None
    for fig in (res.cost, res.saving):
        if fig.low_nano is not None:
            assert fig.low_nano <= fig.nano <= fig.high_nano
    per_lane = dict(res.per_lane)
    for lane in lanes:
        points = [o[r.request_id].cost_nano for r in lane.requests]
        if any(p is None for p in points):
            assert lane.lane_key not in per_lane
        else:
            assert per_lane[lane.lane_key] == sum(points)
    assert res.keepalive_pings == sum(1 for x in o.values() for e in x.extra
                                      if e.kind.value == "keepalive")


_ANY = st.one_of(st.none(), st.booleans(), st.integers(-5, 10**6), st.text(max_size=12),
                 st.tuples(st.text(max_size=8), st.text(max_size=8)),
                 st.tuples(st.text(max_size=8), st.integers(-3, 10**5), st.integers(-3, 10**5)),
                 st.lists(st.tuples(st.text(max_size=12), st.text(max_size=6)), max_size=2)
                 .map(tuple))


@settings(max_examples=150, deadline=None)
@given(ttl=_ANY, keepalive=_ANY, cw=_ANY, cr=_ANY, remap=_ANY, effort=_ANY, batch=_ANY,
       repairs=_ANY, breakpoints=_ANY, flags=st.tuples(_ANY, _ANY, _ANY))
def test_malformed_policies_raise_only_tokenbill_errors(ttl: object, keepalive: object,
                                                        cw: object, cr: object, remap: object,
                                                        effort: object, batch: object,
                                                        repairs: object, breakpoints: object,
                                                        flags: tuple) -> None:
    policy = Policy(name="fuzz", ttl=ttl, keepalive=keepalive, compaction_window=cw,  # type: ignore[arg-type]
                    cold_resume=cr, model_remap=remap, effort=effort, fast_off=flags[0],  # type: ignore[arg-type]
                    geo_global=flags[1], regional_to_global=flags[2], batch=batch,  # type: ignore[arg-type]
                    repairs=repairs, breakpoint_policy=breakpoints)  # type: ignore[arg-type]
    try:
        replay([a1_lane()], policy)
    except TokenbillError:
        pass

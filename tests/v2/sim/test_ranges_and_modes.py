"""Ambiguity ranges (SPEC §9.2), calibrated mode (§9.4) and sharding (§9.1 #6, D30)."""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from tokenbill.core.labels import Calibration, Evidence
from tokenbill.core.shards import merge_replay
from tokenbill.core.types import CalibrationReport, ReplayResult
from tokenbill.sim.usage_replay import GAP_BANDS, gap_band, rho_from_report

from .helpers import (
    SDK,
    a1_lane,
    outcomes,
    random_lane,
    replay,
    table,
    usd,
)

# ---------------------------------------------------------------------------------- ambiguity


def _hit_lane_1h(gap_s: int) -> object:
    return table([(0, 0, 0, 100_000, 0, 500), (gap_s, 100_000, 0, 2_000, 0, 500)],
                 attribution=SDK)


def test_305_s_gap_under_ttl_5m_spans_hit_and_miss() -> None:
    lane = _hit_lane_1h(305)
    res = replay(lane, "ttl=5m", floor={})
    x = outcomes(res)[lane.requests[1].request_id]
    miss = usd("0.51") + usd("0.01")                     # 102k 5m writes + output
    hit = usd("0.02") + usd("0.01") + usd("0.01")        # 100k read + 2k 5m write + output
    assert (x.cost_nano, x.low_nano, x.high_nano) == (miss, hit, miss)
    assert (x.usage.cache_read, x.usage.cache_write_5m) == (0, 102_000)   # the point: miss
    first = usd("0.50") + usd("0.01")
    assert (res.cost.nano, res.cost.low_nano, res.cost.high_nano) == \
        (first + miss, first + hit, first + miss)
    base = usd("0.81") + usd("0.046")
    assert res.baseline.nano == base
    assert (res.saving.nano, res.saving.low_nano, res.saving.high_nano) == \
        (base - first - miss, base - first - miss, base - first - hit)


def test_295_s_gap_point_hit_with_a_miss_bound() -> None:
    lane = _hit_lane_1h(295)
    x = outcomes(replay(lane, "ttl=5m", floor={}))[lane.requests[1].request_id]
    hit = usd("0.02") + usd("0.01") + usd("0.01")
    miss = usd("0.51") + usd("0.01")
    assert (x.cost_nano, x.low_nano, x.high_nano) == (hit, hit, miss)


def test_ambiguity_near_the_1h_ttl() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500), (3_605, 0, 102_000, 0, 0, 500)], attribution=SDK)
    res = replay(lane, "ttl=1h")
    x = outcomes(res)[lane.requests[1].request_id]
    miss = usd("0.816") + usd("0.01")                    # 102k 1h writes
    hit = usd("0.02") + usd("0.016") + usd("0.01")
    assert (x.cost_nano, x.low_nano, x.high_nano) == (miss, hit, miss)


def test_an_unchanged_ttl_carries_no_ambiguity() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500), (305, 0, 102_000, 0, 0, 500)], attribution=SDK)
    res = replay(lane, "ttl=5m")
    assert not any(o.changed for o in res.outcomes or ())
    assert res.cost.low_nano is None and res.saving.nano == 0


@pytest.mark.parametrize("seed", range(3))
def test_bounds_always_contain_the_point(seed: int) -> None:
    lanes = [random_lane(random.Random(seed * 100 + i), f"b{i}", allow_unpriced=False)
             for i in range(40)]
    for spec in ("ttl=1h", "ttl=5m", "keepalive=240s,max=3600s", "model=claude-sonnet-4-6",
                 "effort=low,scale=0.5", "batch=eligible", "compact-window=20000,post=9000",
                 "cold-resume=compact,min=10000;fast=off"):
        res = replay(lanes, spec)
        for fig in (res.cost, res.saving):
            if fig.low_nano is not None:
                assert fig.low_nano <= fig.nano <= fig.high_nano
        for o in res.outcomes or ():
            if o.low_nano is not None:
                assert o.low_nano <= o.cost_nano <= o.high_nano


# ------------------------------------------------------------------------------ calibrated mode


def _report(status: str = "pass", mode_used: str | None = "calibrated",
            rho: tuple = (("0s-60s", 0, 0, "0", "1"), ("60s-300s", 0, 0, "0", "1"),
                          ("300s-3600s", 90, 100, "0.82", "0.94"), ("3600s+", 0, 0, "0", "1"))
            ) -> CalibrationReport:
    return CalibrationReport(
        granularity="day", n_periods=30, status=status, mode_used=mode_used, nmbe_pct="0",
        cvrmse_pct="0", nmbe_pct_calibrated="0", cvrmse_pct_calibrated="0",
        thresholds=("10", "30"), rho=rho, diag_confusion=(), diag_precision_recall=(),
        unlabeled=0, no_comparison_labels=0, ttl_corroboration=(0, 0), notes=())


def test_calibrated_mode_weights_flips_by_rho() -> None:
    res = replay(a1_lane(), "ttl=1h", mode="calibrated", calibration=_report())
    assert res.mode == "calibrated"
    assert res.calibration is Calibration.CALIBRATED
    assert res.cost.calibration is Calibration.CALIBRATED
    assert res.saving.calibration is Calibration.CALIBRATED
    # 0.9 · hit + 0.1 · no-hit (the observed split re-rated to 1h), rounded once per transition
    expected = usd("0.81") + usd("0.124") + usd("0.12596") + usd("0.12792")
    assert res.cost.nano == expected
    assert res.saving.nano == usd("2.10") - expected
    assert any("calibrated mode" in a for a in res.assumptions)


def test_calibrated_mode_small_bands_use_the_pooled_rho() -> None:
    rho = (("0s-60s", 450, 500, "0.87", "0.92"), ("60s-300s", 0, 0, "0", "1"),
           ("300s-3600s", 10, 20, "0.3", "0.7"), ("3600s+", 0, 0, "0", "1"))
    report = _report(rho=rho)
    assert rho_from_report(report)["300s-3600s"] == Fraction(460, 520)
    assert rho_from_report(report)["0s-60s"] == Fraction(450, 500)
    res = replay(a1_lane(), "ttl=1h", mode="calibrated", calibration=report)
    documented = replay(a1_lane(), "ttl=1h").cost.nano
    assert documented < res.cost.nano < usd("2.10")


def test_rho_from_an_empty_report_is_one() -> None:
    empty = _report(rho=tuple((b, 0, 0, "0", "1") for b, _lo, _hi in GAP_BANDS))
    assert set(rho_from_report(empty).values()) == {Fraction(1)}


@pytest.mark.parametrize("report", [None, _report(status="fail", mode_used=None),
                                    _report(status="insufficient_data", mode_used=None)])
def test_without_a_passing_report_results_are_uncalibrated(report: object) -> None:
    res = replay(a1_lane(), "ttl=1h", mode="calibrated", calibration=report)
    assert res.mode == "documented"
    assert res.calibration is Calibration.UNCALIBRATED
    assert res.saving.calibration is Calibration.UNCALIBRATED
    assert res.cost.nano == usd("0.9492")
    assert any("calibrated mode unavailable" in a for a in res.assumptions)


def test_documented_mode_is_calibrated_only_when_the_documented_rules_passed() -> None:
    passed_documented = _report(mode_used="documented")
    res = replay(a1_lane(), "ttl=1h", calibration=passed_documented)
    assert res.calibration is Calibration.CALIBRATED and res.cost.nano == usd("0.9492")
    passed_calibrated = _report(mode_used="calibrated")
    res = replay(a1_lane(), "ttl=1h", calibration=passed_calibrated)
    assert res.calibration is Calibration.UNCALIBRATED


def test_gap_bands() -> None:
    assert [gap_band(g) for g in (0, 59_999, 60_000, 299_999, 300_000, 3_599_999, 3_600_000,
                                  10**12)] == \
        ["0s-60s", "0s-60s", "60s-300s", "60s-300s", "300s-3600s", "300s-3600s", "3600s+",
         "3600s+"]


# ------------------------------------------------------------------------------------ sharding


def _assert_same(merged: ReplayResult, whole: ReplayResult) -> None:
    assert merged.baseline == whole.baseline
    assert merged.cost == whole.cost
    assert merged.saving == whole.saving
    assert dict(merged.per_lane) == dict(whole.per_lane)
    assert {o.request_id: o for o in merged.outcomes or ()} == \
        {o.request_id: o for o in whole.outcomes or ()}
    assert set(merged.lanes_skipped) == set(whole.lanes_skipped)
    assert set(merged.assumptions) == set(whole.assumptions)
    for attr in ("mode", "calibration", "added_calls", "keepalive_pings", "n_lanes",
                 "n_requests"):
        assert getattr(merged, attr) == getattr(whole, attr)


_SHARD_POLICIES = (
    "ttl=1h", "ttl=5m@lane_kind:main", "keepalive=240s,max=3600s@agent_product:agent_sdk",
    "fast=off;geo=global;regional=global", "compact-window=30000,post=15000;"
    "cold-resume=compact,min=20000", "model=claude-sonnet-5@lane_kind:main",
    "effort=low,scale=0.5", "batch=eligible",
    "repair=fallback_credit;repair=restore_caching;repair=retry_backoff_cap;"
    "repair=shared_ci_prefix;repair=stagger_fanout",
)


@pytest.mark.parametrize("spec", _SHARD_POLICIES)
def test_merging_shard_replays_equals_one_replay(spec: str) -> None:
    rnd = random.Random(5)
    lanes = [random_lane(rnd, f"S{i:02d}", team=("alpha", "beta", None)[i % 3])
             for i in range(36)]
    whole = replay(lanes, spec)
    shards = [[lane for lane in lanes if lane.team == team] for team in (None, "alpha", "beta")]
    merged = merge_replay([replay(shard, spec) for shard in shards])
    _assert_same(merged, whole)
    halves = merge_replay([replay(lanes[:18], spec), replay(lanes[18:], spec)])
    if "repair=" not in spec:          # cross-lane repairs group within a (team, kind) cohort
        _assert_same(halves, whole)


def test_merge_equals_one_replay_for_the_appendix_lanes() -> None:
    a, b = a1_lane(lane_key="a"), a1_lane(lane_key="b", attribution={**SDK, "team": "t"})
    whole = replay([a, b], "ttl=1h")
    merged = merge_replay([replay([a], "ttl=1h"), replay([b], "ttl=1h")])
    _assert_same(merged, whole)
    assert merged.saving.nano == 2 * usd("1.1508")
    assert merged.saving.evidence is Evidence.ESTIMATED

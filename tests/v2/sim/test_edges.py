"""Edge cases of the engine and the gate: probe rejections, unpriced blends, resets from context
edits, un-repaired parameter changes, empty lanes, passthrough bounds, partial-merge corners."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from tokenbill.core.builders import make_attempt, make_ctx, make_inference, make_lane, make_request
from tokenbill.core.labels import Calibration
from tokenbill.core.records import InferenceKind, Lane, LaneKind, UsageBuckets, UsageSource
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import CalibrationPartial, CalibrationReport, UnitRates
from tokenbill.sim.calibrate import calibrate_lanes, finish_calibration
from tokenbill.sim.usage_replay import _PriceBook

from .helpers import CC, PRICER, RULES, SDK, a1_lane, at, outcomes, replay, table, usd


def _probe(usage: UsageBuckets) -> bool:
    return usage.cache_write_other == 1_000 and usage.web_search_requests == 1


class _Odd(FakePricer):
    """Rewrites the lines ``price_usage`` returns for probe usage with *change*."""

    change: dict[str, Any] = {}

    def price_usage(self, usage: UsageBuckets, ctx: Any, **kw: Any) -> Any:
        got = super().price_usage(usage, ctx, **kw)
        if not _probe(usage):
            return got
        lines = tuple(dataclasses.replace(ln, **self.change.get(ln.bucket, {}))
                      for ln in got.lines)
        return dataclasses.replace(got, lines=lines)


@pytest.mark.parametrize("change", [
    {"output": {"exact": False}},
    {"output": {"unit_usd_per_mtok": "twenty"}},
    {"web_search": {"unit_usd_per_mtok": "0.02"}},
    {"output": {"bucket": "output_audio"}},
    {"cache_read": {"unit_usd_per_mtok": "0.25"}},
])
def test_probe_rejects_inconsistent_lines(change: dict) -> None:
    pricer = type("P", (_Odd,), {"change": change})()
    book = _PriceBook(pricer)
    assert book.unit(make_ctx("claude-opus-5-5"), at(0)) is None
    # replay still prices through price_usage and matches the plain pricer
    assert replay(a1_lane(), "ttl=1h", pricer=pricer).saving.nano == usd("1.1508")


class _RaisingUnits(FakePricer):
    def unit_rates(self, ctx: Any, *, ts_ms: int) -> UnitRates | None:
        raise ArithmeticError("no unit rates")


def test_unit_rates_errors_fall_back() -> None:
    assert _PriceBook(_RaisingUnits()).unit(make_ctx("claude-opus-5-5"), at(0)) is None
    assert replay(a1_lane(), "ttl=1h", pricer=_RaisingUnits()).saving.nano == usd("1.1508")


def test_calibrated_blend_of_an_unpriced_flip_is_unpriced() -> None:
    report = CalibrationReport(
        granularity="day", n_periods=20, status="pass", mode_used="calibrated", nmbe_pct="0",
        cvrmse_pct="0", nmbe_pct_calibrated="0", cvrmse_pct_calibrated="0",
        thresholds=("10", "30"), rho=(("300s-3600s", 90, 100, "0.8", "0.9"),),
        diag_confusion=(), diag_precision_recall=(), unlabeled=0, no_comparison_labels=0,
        ttl_corroboration=(0, 0), notes=())
    lane = table([(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500)],
                 model="claude-not-a-model", attribution=SDK)
    res = replay(lane, "ttl=1h", mode="calibrated", calibration=report)
    second = outcomes(res)[lane.requests[1].request_id]
    assert second.changed and second.cost_nano is None
    assert res.cost.nano is None and res.saving.nano is None      # R2: unknown is not zero
    assert res.saving.calibration is Calibration.CALIBRATED


def test_context_edits_reset_the_prefix_for_the_compaction_call() -> None:
    r0 = make_request("e", 0, at(0), UsageBuckets(cache_write_5m=300_000, output=1_000),
                      "claude-sonnet-5", attribution=CC)
    r1 = make_request("e", 1, at(30), UsageBuckets(cache_read=200_000, cache_write_5m=250_000,
                                                   output=1_000),
                      "claude-sonnet-5", attribution=CC, applied_edits=[("clear_tool_uses", 1)])
    lane = make_lane([r0, r1])
    res = replay(lane, "compact-window=400000,post=20000")
    (comp,) = outcomes(res)[r1.request_id].extra
    assert comp.usage.cache_read == 0 and comp.usage.cache_write_5m == 450_000


def test_an_unrepaired_speed_toggle_blocks_the_ttl_flip() -> None:
    r0 = make_request("s", 0, at(0), UsageBuckets(cache_write_5m=100_000, output=500),
                      attribution=SDK)
    r1 = make_request("s", 1, at(420), UsageBuckets(cache_write_5m=102_000, output=500),
                      attribution=SDK, speed="fast")
    res = replay(make_lane([r0, r1]), "ttl=1h")
    second = outcomes(res)[r1.request_id]
    assert (second.usage.cache_read, second.usage.cache_write_1h) == (0, 102_000)
    with_fast_off = outcomes(replay(make_lane([r0, r1]), "ttl=1h;fast=off"))[r1.request_id]
    assert (with_fast_off.usage.cache_read, with_fast_off.usage.cache_write_1h) == \
        (100_000, 2_000)


def test_empty_lanes_and_lanes_without_serving_inferences() -> None:
    empty = Lane(lane_key="empty", session_key="s", kind=LaneKind.MAIN, parent_lane_key=None,
                 cache_scope_key="ws:test", requests=())
    residual = make_inference({"output": 700}, kind=InferenceKind.OUTPUT_RESIDUAL,
                              inference_id="res")
    only_residual = make_lane([make_request("r", 0, at(0), attribution={**SDK,
                                                                        "workload_class": "ci"},
                                            attempts=[make_attempt([residual], ts_ms=at(0))])],
                              lane_key="r")
    lanes = [empty, only_residual, a1_lane(lane_key="z")]
    res = replay(lanes, "ttl=1h;repair=stagger_fanout;repair=shared_ci_prefix")
    assert dict(res.per_lane)["empty"] == 0
    assert res.n_lanes == 3 and res.saving.nano == usd("1.1508")


def test_ci_chains_use_the_policy_ttl() -> None:
    def run(key: str, start: int) -> Lane:
        return table([(start, 0, 30_000, 0, 0, 0)], lane_key=key, kind=LaneKind.API_RUN,
                     attribution={**SDK, "workload_class": "ci"})

    runs = [run("c1", 0), run("c2", 1_000)]           # 1,000 s apart: a chain only under 1h
    assert replay(runs, "repair=shared_ci_prefix").saving.nano == 0
    res = replay(runs, "ttl=1h;repair=shared_ci_prefix")
    second = outcomes(res)[runs[1].requests[0].request_id]
    assert (second.usage.cache_read, second.usage.cache_write_1h) == (24_000, 6_000)


def test_batch_with_a_calibrated_flip() -> None:
    report = CalibrationReport(
        granularity="day", n_periods=20, status="pass", mode_used="calibrated", nmbe_pct="0",
        cvrmse_pct="0", nmbe_pct_calibrated="0", cvrmse_pct_calibrated="0",
        thresholds=("10", "30"), rho=(("0s-60s", 50, 100, "0.4", "0.6"),),
        diag_confusion=(), diag_precision_recall=(), unlabeled=0, no_comparison_labels=0,
        ttl_corroboration=(0, 0), notes=())
    attr = {"agent_product": "api", "workload_class": "ci"}
    a = table([(0, 0, 20_000, 0, 0, 0)], lane_key="a", kind=LaneKind.SUBAGENT, attribution=attr)
    b = table([(2, 0, 20_000, 0, 0, 0)], lane_key="b", kind=LaneKind.SUBAGENT, attribution=attr)
    res = replay([a, b], "batch=eligible;repair=stagger_fanout", mode="calibrated",
                 calibration=report)
    second = outcomes(res)[b.requests[0].request_id]
    # batch tier (0.5×): half-credit ρ = 0.5 between the read (h-banded) and the rewrite
    assert second.changed and second.low_nano <= second.cost_nano <= second.high_nano
    full = usd("0.05")                                   # 20,000 × $2.50/M at the batch tier
    assert second.cost_nano < full


def test_passthrough_placeholder_output_scales_with_the_band() -> None:
    placeholder = make_inference({"uncached_input": 1_000, "output": 100},
                                 model="claude-sonnet-4-6", kind=InferenceKind.ADVISOR,
                                 usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=400,
                                 inference_id="adv")
    req = make_request("p", 0, at(0), UsageBuckets(cache_write_5m=10_000, output=100),
                       "claude-sonnet-4-6", attribution=CC, extra_inferences=[placeholder])
    res = replay(make_lane([req]), "model=claude-sonnet-5")
    x = outcomes(res)[req.request_id]
    # the advisor's output bound scales with the band: 400 × 1.35 = 540 output tokens at $10/M
    assert x.high_nano >= usd("0.0054")
    assert x.low_nano <= x.cost_nano <= x.high_nano


# ------------------------------------------------------------------------------------ the gate


class _Picky(FakePricer):
    """Unpriced whenever a usage both reads and writes (never for the billed misses here)."""

    def unit_rates(self, ctx: Any, *, ts_ms: int) -> UnitRates | None:
        return None

    def price_usage(self, usage: UsageBuckets, ctx: Any, **kw: Any) -> Any:
        if usage.cache_read and usage.cache_write:
            return super().price_usage(usage, make_ctx(""), **kw)
        return super().price_usage(usage, ctx, **kw)


def test_gate_skips_single_requests_and_unpriced_predictions() -> None:
    single = table([(0, 0, 50_000, 0, 0, 10)], lane_key="one", attribution=SDK)
    missing = table([(0, 0, 50_000, 0, 0, 10), (30, 0, 52_000, 0, 0, 10)], lane_key="two",
                    attribution=SDK)
    report = calibrate_lanes([single, missing], pricer=_Picky(), rules=RULES)
    assert report.n_periods == 0 and report.unlabeled == 1
    report = calibrate_lanes([single], pricer=PRICER, rules=RULES)
    assert report.unlabeled == 0 and report.status == "insufficient_data"


def test_finish_accepts_unqualified_reasons() -> None:
    part = CalibrationPartial(granularity="day", period_billed=(("2026-09-23", 10),),
                              period_documented=(("2026-09-23", 10),), period_calibrated=(),
                              rho_counts=(), confusion=(("model-switch", "model_changed", 2),),
                              ttl_corroboration=(0, 0), unlabeled=0, no_comparison_labels=0)
    report = finish_calibration([part], [], granularity="day", folds=5)
    assert report.diag_confusion == (("model-switch", "model_changed", 2),)
    assert report.diag_precision_recall == (("model_changed", "1", "1"),)
    assert report.nmbe_pct_calibrated is None


def test_a_ci_member_already_reading_more_than_s_ci_is_unchanged_in_calibrated_mode() -> None:
    report = CalibrationReport(
        granularity="day", n_periods=20, status="pass", mode_used="calibrated", nmbe_pct="0",
        cvrmse_pct="0", nmbe_pct_calibrated="0", cvrmse_pct_calibrated="0",
        thresholds=("10", "30"), rho=(("60s-300s", 50, 100, "0.4", "0.6"),),
        diag_confusion=(), diag_precision_recall=(), unlabeled=0, no_comparison_labels=0,
        ttl_corroboration=(0, 0), notes=())
    attr = {**SDK, "workload_class": "ci"}
    c1 = table([(0, 0, 1_000, 0, 0, 0)], lane_key="c1", kind=LaneKind.API_RUN, attribution=attr)
    c2 = table([(100, 5_000, 20_000, 0, 0, 0)], lane_key="c2", kind=LaneKind.API_RUN,
               attribution=attr)
    res = replay([c1, c2], "repair=shared_ci_prefix", mode="calibrated", calibration=report)
    assert not outcomes(res)[c2.requests[0].request_id].changed      # S_ci = 800 < R = 5,000


def test_openai_other_ttl_writes_replay_and_calibrate() -> None:
    from tokenbill.core.records import RequestParams

    def req(seq: int, ts_s: int, usage: UsageBuckets, effort: str) -> object:
        return make_request("oa", seq, at(ts_s), usage, "gpt-5.6-sol", attribution=SDK,
                            params=RequestParams(model_requested="gpt-5.6-sol", effort=effort))

    r0 = req(0, 0, UsageBuckets(cache_write_other=50_000, cache_write_other_ttl_s=1800,
                                output=10), "high")
    r1 = req(1, 2_000, UsageBuckets(cache_read=50_000, uncached_input=10, output=10), "high")
    lane = make_lane([r0, r1])
    res = replay(lane, "effort=low,scale=0.5")
    assert res.saving.nano is not None and res.saving.nano >= 0
    from tokenbill.sim.calibrate import calibrate_pass1

    part = calibrate_pass1([lane], pricer=PRICER, rules=RULES)
    # predicted miss (2,000 s > 1,800 s): the prefix is rewritten in τ's bucket (30m writes)
    expected = FakePricer().price_usage(
        UsageBuckets(cache_write_other=50_000, cache_write_other_ttl_s=1800, uncached_input=10,
                     output=10), make_ctx("gpt-5.6-sol"), ts_ms=at(2_000)).figure.nano
    assert dict(part.period_documented) == {"2026-09-23": expected}

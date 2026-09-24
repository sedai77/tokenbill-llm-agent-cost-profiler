"""Each closed form of ``synth.truth`` reproduces its SPEC Appendix counterpart on a one-lane
version of the plant (A.1 payments, A.4 agents keepalive, A.5 mobile, A.6 search, A.10 agents'
edit churn, A.11 ci-bots truncation) plus hand-computed fixtures for the other plant formulas."""

from __future__ import annotations

import dataclasses
from fractions import Fraction

import pytest

from tokenbill.core.builders import (
    lane_from_table,
    make_block,
    make_fingerprint,
    make_lane,
    make_request,
)
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.records import Attribution, LaneKind, RequestParams, UsageBuckets
from tokenbill.synth import truth as T

#: 2026-10-01T00:00:00Z in seconds: every fixture model is priced from 2026-09-22.
BASE_S = 1_790_812_800
USD = 10**9


def a1_lane(**kw):  # noqa: ANN003, ANN201
    rows = [(BASE_S + t, 0, w, 0, 0, 500) for t, w in
            ((0, 100_000), (420, 102_000), (840, 104_000), (1260, 106_000))]
    return lane_from_table(rows, **kw)


def test_a1_ttl_5m_to_1h_saves_1_1508() -> None:
    c = T.Coster()
    lane = a1_lane()
    assert T.spend([lane], c) == 2_100_000_000
    assert T.ttl_1h_saving([lane], c) == 1_150_800_000


def test_a2_bursty_lane_costs_more_at_1h() -> None:
    rows = [(BASE_S, 0, 100_000, 0, 0, 500)]
    prefix = 100_000
    for i in range(1, 4):
        rows.append((BASE_S + 30 * i, prefix, 2_000, 0, 0, 500))
        prefix += 2_000
    lane = lane_from_table(rows)
    c = T.Coster()
    assert T.spend([lane], c) == 631_200_000
    assert T.ttl_1h_saving([lane], c) == -318_000_000


def test_ttl_closed_form_preconditions() -> None:
    c = T.Coster()
    one_h = lane_from_table([(BASE_S, 0, 0, 10_000, 0, 10), (BASE_S + 30, 10_000, 0, 1_000, 0, 10)])
    with pytest.raises(ContractViolation):
        T.ttl_1h_saving([one_h], c)
    ambiguous = lane_from_table([(BASE_S, 0, 10_000, 0, 0, 10),
                                 (BASE_S + 305, 0, 11_000, 0, 0, 10)])
    with pytest.raises(ContractViolation):
        T.ttl_1h_saving([ambiguous], c)


def test_a4_keepalive_0_6924_and_ping_counts() -> None:
    c = T.Coster()
    saving, pings = T.keepalive_saving([a1_lane()], c)
    assert pings == 3
    assert saving == 2_100_000_000 - 692_400_000
    # a 20-minute gap sends 4 pings (warm); a 2-hour gap 15 pings and stays cold
    for gap, n, warm in ((1_200, 4, True), (7_200, 15, False)):
        lane = lane_from_table([(BASE_S, 0, 100_000, 0, 0, 0), (BASE_S + gap, 0, 101_000, 0, 0, 0)])
        s, p = T.keepalive_saving([lane], c)
        assert p == n
        ping_cost = n * 100_000 * 200      # reads at $0.20/MTok = 200 nano per token
        flipped = 101_000 * 5_000 - (100_000 * 200 + 1_000 * 5_000)
        assert s == (flipped if warm else 0) - ping_cost
    claude_code = a1_lane(attribution=Attribution(agent_product="claude_code"))
    with pytest.raises(ContractViolation):
        T.keepalive_saving([claude_code], c)


def test_a5_cold_resume_4_00_exact_and_3_90_premium() -> None:
    rows = [(BASE_S, 0, 0, 500_000, 0, 0), (BASE_S + 30, 500_000, 0, 0, 0, 0),
            (BASE_S + 7_230, 0, 0, 500_000, 0, 0), (BASE_S + 7_320, 500_000, 0, 0, 0, 0)]
    observed, premium, events = T.cold_resume([lane_from_table(rows)], T.Coster())
    assert (observed, premium, events) == (4 * USD, 3_900_000_000, 1)


def test_a6_compaction_window_negative_saving_and_no_change_above_max() -> None:
    rows = [(BASE_S, 0, 300_000, 0, 0, 1_000), (BASE_S + 30, 300_000, 150_000, 0, 0, 1_000),
            (BASE_S + 60, 450_000, 50_000, 0, 0, 1_000)]
    lane = lane_from_table(rows, model="claude-sonnet-5")
    c = T.Coster()
    assert T.spend([lane], c) == 1_430_000_000
    assert T.compaction_window_saving([lane], c, window=400_000, summary=20_000) == (
        1_430_000_000 - 1_999_000_000, 1)
    assert T.compaction_window_saving([lane], c, window=600_000, summary=20_000) == (0, 0)
    with pytest.raises(ContractViolation):
        T.compaction_window_saving([lane_from_table(rows, kind=LaneKind.SUBAGENT)], c,
                                   window=400_000, summary=20_000)


def _a10_lane():  # noqa: ANN202
    reqs = [make_request("L", 0, BASE_S * 1000, {"cache_write_5m": 120_000})]
    reqs.append(make_request("L", 1, (BASE_S + 30) * 1000,
                             {"cache_read": 20_000, "cache_write_5m": 62_000},
                             applied_edits=(("clear_tool_uses_20250919", 40_000),)))
    prefix = 82_000
    for i in range(2, 7):
        reqs.append(make_request("L", i, (BASE_S + 30 * i) * 1000,
                                 {"cache_read": prefix, "cache_write_5m": 2_000}))
        prefix += 2_000
    return make_lane(reqs, kind=LaneKind.API_RUN)


def test_a10_edit_churn_0_31_exact_and_0_2576_loss() -> None:
    observed, loss, kstars = T.edit_churn([_a10_lane()], T.Coster())
    assert observed == 310_000_000
    assert loss == 257_600_000
    assert kstars == [Fraction(186, 5)]    # K* = 37.2


def test_a11_truncation_0_71376_exact_and_0_53532_recoverable() -> None:
    reqs = [make_request("C", 0, BASE_S * 1000, {"cache_write_5m": 48_000, "output": 100},
                         model="claude-sonnet-5")]
    t = BASE_S
    for i in range(1, 8):
        t += 30
        if i % 2:
            usage = {"cache_read": 48_000, "cache_write_5m": 2_000, "output": 16_384}
            reqs.append(make_request("C", i, t * 1000, usage, model="claude-sonnet-5",
                                     stop_reason="max_tokens"))
        else:
            reqs.append(make_request("C", i, t * 1000, {"cache_read": 48_000, "output": 100},
                                     model="claude-sonnet-5"))
    lane = make_lane(reqs)
    assert T.truncation([lane], T.Coster()) == (713_760_000, 535_320_000, 4, 3)


def test_restore_caching_hand_computed() -> None:
    rows = []
    total, t = 20_000, BASE_S
    for i in range(5):
        if i:
            total += 2_000
            t += 600 if i == 4 else 60
        rows.append((t, 0, 0, 0, total, 0))
    lane = lane_from_table(rows)
    c = T.Coster()
    assert T.spend([lane], c) == 480_000_000
    assert T.restore_caching_saving([lane], c) == 480_000_000 - 283_200_000
    short = lane_from_table(rows[:4])     # fewer than 5 requests: not affected
    assert T.restore_caching_saving([short], c) == 0


def test_model_remap_same_tier_and_family_guard() -> None:
    c = T.Coster()
    assert T.model_remap_saving([a1_lane()], c, "claude-sonnet-5") == 1_050_000_000
    assert T.model_remap_saving([a1_lane()], c, "claude-opus-5-5") == 0
    with pytest.raises(ContractViolation):
        T.model_remap_saving([a1_lane()], c, "claude-sonnet-4-6")   # legacy tokenizer


def test_effort_scale_uses_reasoning_tokens() -> None:
    usage = UsageBuckets(cache_write_5m=10_000, output=1_000, output_reasoning=500)
    high = make_request("E", 0, BASE_S * 1000, usage, model="claude-opus-5",
                        params=RequestParams(model_requested="claude-opus-5", effort="high"))
    medium = make_request("E", 1, (BASE_S + 30) * 1000, usage, model="claude-opus-5",
                          params=RequestParams(model_requested="claude-opus-5", effort="medium"))
    lane = make_lane([high, medium])
    c = T.Coster()
    assert T.effort_saving([lane], c, max_level="medium", scale=Fraction(1, 2)) == 6_250_000
    assert T.effort_saving([lane], c, max_level="medium", scale=Fraction(1)) == 0
    no_reasoning = make_request("F", 0, BASE_S * 1000, {"output": 1_000}, model="claude-opus-5",
                                params=RequestParams(model_requested="claude-opus-5",
                                                     effort="xhigh"))
    # th = floor(0.505·1000) = 505 → 252.5 tokens: not integral
    with pytest.raises(ContractViolation):
        T.effort_saving([make_lane([no_reasoning])], c, max_level="medium",
                        scale=Fraction(1, 2))


def test_batch_rate_premiums_and_size_tax() -> None:
    c = T.Coster()
    shot = make_request("B", 0, BASE_S * 1000, {"uncached_input": 10_000, "output": 1_000},
                        model="claude-sonnet-5", attribution={"workload_class": "service"})
    assert T.batch_saving([make_lane([shot])], c) == (30_000_000, 15_000_000, 1)
    interactive = make_request("B", 0, BASE_S * 1000, {"uncached_input": 10_000},
                               model="claude-sonnet-5")
    assert T.batch_saving([make_lane([interactive])], c) == (0, 0, 0)

    fast = make_request("F", 0, BASE_S * 1000, {"cache_write_5m": 10_000, "output": 1_000},
                        speed="fast")
    assert T.rate_premium([make_lane([fast])], c, T.fast_to_standard) == 70_000_000
    regional = make_request("R", 0, BASE_S * 1000, {"uncached_input": 10_000, "output": 1_000},
                            model="claude-opus-5", channel="bedrock", endpoint_scope="regional")
    assert T.rate_premium([make_lane([regional])], c, T.regional_to_global) == 7_500_000
    assert T.rate_premium([a1_lane()], c, T.fast_to_standard) == 0

    big = make_request("S", 0, BASE_S * 1000,
                       {"cache_read": 450_000, "cache_write_1h": 50_000},
                       model="claude-sonnet-5")
    assert T.size_tax([make_lane([big])], c, 400_000) == 210_000_000
    assert T.size_tax([make_lane([big])], c, 600_000) == 0


def test_tool_defs_bloat_band_contains_truth() -> None:
    tools = [make_block(f"t{i}", tier="tools", kind="tool_def", role=None, est_tokens=700)
             for i in range(20)]
    system = make_block("sys", tier="system", kind="system_text", role=None, est_tokens=3_000)
    msg = make_block("m0", est_tokens=3_000)
    msg2 = make_block("m1", est_tokens=2_000)
    r0 = make_request("A", 0, BASE_S * 1000, {"cache_write_5m": 20_000},
                      fingerprint=make_fingerprint([*tools, system, msg]))
    r1 = make_request("A", 1, (BASE_S + 30) * 1000, {"cache_read": 20_000,
                                                     "cache_write_5m": 2_000},
                      fingerprint=make_fingerprint([*tools, system, msg, msg2]))
    truth, d_phys, d_prop, n = T.tool_defs_bloat([make_lane([r0, r1])], T.Coster())
    assert n == 2
    assert d_phys == 14_000 * 5_000 + 14_000 * 200            # write, then read
    assert d_prop == round(14_000 * 5_000 + Fraction(14_000 * (200 * 20_000 + 5_000 * 2_000),
                                                     22_000))
    assert truth == d_phys * 7 // 10 == 50_960_000
    deferred = [make_block(f"d{i}", tier="tools", kind="tool_def", role=None, est_tokens=700,
                           deferred=True) for i in range(20)]
    r2 = make_request("D", 0, BASE_S * 1000, {"cache_write_5m": 17_000},
                      fingerprint=make_fingerprint([*deferred, system]))
    assert T.tool_defs_bloat([make_lane([r2])], T.Coster())[3] == 0


def test_runaway_rebaseline_and_totals() -> None:
    c = T.Coster()
    loop = [make_request("R", i, (BASE_S + 30 * i) * 1000, {"cache_read": 400_000,
                                                             "output": 5_000})
            for i in range(200)]
    small = [make_request(f"S{k}", 0, (BASE_S + 100_000 + k) * 1000, {"uncached_input": 1_000},
                          session_key=f"s{k}") for k in range(200)]
    loop_lane = make_lane(loop, session_key="loop")
    figs = T.runaway([loop_lane, *(make_lane([q]) for q in small)], c, "loop")
    per_request = 400_000 * 200 + 5_000 * 20_000
    assert figs["session_nano"] == 200 * per_request
    assert figs["rolling_1h_max_nano"] == 120 * per_request
    assert figs["cohort_p99_hourly_nano"] == figs["cohort_p95_session_nano"] == 4_000_000
    assert figs["threshold_nano"] == 50 * USD
    assert figs["sessions"] == 201
    before = [make_request("M", i, (BASE_S + 60 * i) * 1000, {"output": 1_000}) for i in range(2)]
    after = [make_request("M", 2 + i, (BASE_S + 86_400 + 60 * i) * 1000, {"output": 1_350})
             for i in range(2)]
    delta = T.rebaseline_delta([make_lane([*before, *after])], (BASE_S + 86_400) * 1000)
    assert delta == (Fraction(1_000), Fraction(1_350), 2, 2)
    with pytest.raises(ContractViolation):
        T.rebaseline_delta([make_lane(before)], (BASE_S + 86_400) * 1000)
    totals = T.token_totals([*before, *after])
    assert totals.output == 4_700 and totals.cache_write == 0
    assert (totals + totals).output == 9_400
    assert totals.merged_writes() == totals


def test_within_and_plant_accessors() -> None:
    plant = T.PlantTruth(
        plant_id="x", team="t", detector_id="d", kind="k", scope=(("team", "t"),),
        basis="list", policy=None, lane_keys=(), cost_observed_nano=100, recoverable_nano=1_000,
        shapley_nano=None, tolerances=(("cost_observed", "0"), ("recoverable", "0.05"),
                                       ("range_fig", "range")),
        details=(("a", "1"),))
    assert plant.within("cost_observed", 100) and not plant.within("cost_observed", 101)
    assert plant.within("recoverable", 1_050) and not plant.within("recoverable", 1_051)
    assert plant.detail("a") == "1" and plant.detail("b") is None
    assert plant.tolerance("shapley") is None
    with pytest.raises(UsageError):
        plant.within("shapley", 1)
    ranged = dataclasses.replace(plant, tolerances=(("recoverable", "range"),))
    assert ranged.within("recoverable", 0, low=900, high=1_100)
    assert not ranged.within("recoverable", 0, low=1_001, high=1_100)
    with pytest.raises(UsageError):
        ranged.within("recoverable", 0)

"""SPEC Appendix A.1–A.6 and A.2b reproduced to the nano with FakePricer (REPLAY brief)."""

from __future__ import annotations

import pytest

from tokenbill.core.builders import make_request
from tokenbill.core.labels import Calibration, Evidence
from tokenbill.core.records import InferenceKind, LaneKind, RequestParams, UsageBuckets

from .helpers import (
    CC,
    SDK,
    a1_lane,
    a2_lane,
    a2b_lane,
    a3_lane,
    a6_lane,
    at,
    outcomes,
    price,
    priced_request,
    replay,
    same,
    table,
    usd,
)

# ------------------------------------------------------------------------------------ A.1 – A.3


def test_a1_5m_to_1h_saves_1_1508() -> None:
    res = replay(a1_lane(), "ttl=1h")
    assert res.baseline.nano == usd("2.10")
    assert res.baseline.evidence is Evidence.EXACT
    assert res.cost.nano == usd("0.9492")
    assert res.saving.nano == usd("1.1508")
    assert res.saving.evidence is Evidence.ESTIMATED
    assert res.saving.calibration is Calibration.UNCALIBRATED
    assert res.cost.low_nano is None and res.saving.low_nano is None   # no ambiguity: 420 ≠ 3600
    outs = list(res.outcomes or ())
    assert all(o.changed for o in outs)
    # requests 1-3 read E and write the appended 2k at 1h
    assert [o.usage.cache_read for o in outs] == [0, 100_000, 102_000, 104_000]
    assert [o.usage.cache_write_1h for o in outs] == [100_000, 2_000, 2_000, 2_000]
    assert all(o.usage.cache_write_5m == 0 for o in outs)


def test_a2_bursty_lane_costs_more_under_1h() -> None:
    res = replay(a2_lane(), "ttl=1h")
    assert res.baseline.nano == usd("0.6312")
    assert res.cost.nano == usd("0.9492")
    assert res.saving.nano == -usd("0.318")


def test_a2b_1h_to_5m_saves_0_318() -> None:
    res = replay(a2b_lane(), "ttl=5m")
    assert res.baseline.nano == usd("0.9492")
    assert res.cost.nano == usd("0.6312")
    assert res.saving.nano == usd("0.318")


def test_a2_under_5m_changes_nothing() -> None:
    res = replay(a2_lane(), "ttl=5m")
    assert same(res.cost, res.baseline)
    assert res.saving.nano == 0
    assert not any(o.changed for o in res.outcomes or ())


def test_a3_1h_to_5m_hit_to_miss_costs_1_1508() -> None:
    res = replay(a3_lane(), "ttl=5m", floor={})
    assert res.baseline.nano == usd("0.9492")
    assert res.cost.nano == usd("2.10")
    assert res.saving.nano == -usd("1.1508")
    assert [o.usage.cache_write_5m for o in res.outcomes or ()] == \
        [100_000, 102_000, 104_000, 106_000]


def test_a3_hit_to_miss_reads_the_static_prefix_floor() -> None:
    lane = a3_lane()
    floor = {(lane.cache_scope_key, "claude-opus-5-5"): 30_000}
    res = replay(lane, "ttl=5m", floor=floor)
    outs = list(res.outcomes or ())
    assert [o.usage.cache_read for o in outs] == [0, 30_000, 30_000, 30_000]
    assert [o.usage.cache_write_5m for o in outs] == [100_000, 72_000, 74_000, 76_000]


# ------------------------------------------------------------------------------------------ A.4


def test_a4_keepalive_0_6924() -> None:
    res = replay(a1_lane(), "keepalive=240s,max=3600s")
    assert res.cost.nano == usd("0.6924")
    assert res.saving.nano == usd("2.10") - usd("0.6924")
    assert res.keepalive_pings == 3
    assert res.lanes_skipped == ()
    outs = list(res.outcomes or ())
    pings = [inf for o in outs for inf in o.extra]
    assert [p.usage.cache_read for p in pings] == [100_000, 102_000, 104_000]
    assert all(p.kind is InferenceKind.KEEPALIVE and p.usage.output == 0 for p in pings)
    assert [o.usage.cache_read for o in outs] == [0, 100_000, 102_000, 104_000]
    assert [o.usage.cache_write_5m for o in outs] == [100_000, 2_000, 2_000, 2_000]


def _two_step_sdk(gap_s: int, **kw: object) -> object:
    return table([(0, 0, 100_000, 0, 0, 500), (gap_s, 0, 102_000, 0, 0, 500)],
                 attribution=SDK, **kw)


def test_a4_twenty_minute_gap_four_pings_warm() -> None:
    res = replay(_two_step_sdk(1_200), "keepalive=240s,max=3600s")
    assert res.keepalive_pings == 4
    # 4 pings reading 100k ($0.08) + hit: 100k read ($0.02) + 2k 5m write ($0.01) + outputs
    assert res.cost.nano == usd("0.50") + usd("0.08") + usd("0.02") + usd("0.01") + usd("0.02")
    o = outcomes(res)
    second = [x for x in o.values() if x.extra][0]
    assert second.usage.cache_read == 100_000 and second.usage.cache_write_5m == 2_000
    assert [p.usage.cache_read for p in second.extra] == [100_000] * 4


def test_a4_two_hour_gap_fifteen_pings_cold() -> None:
    res = replay(_two_step_sdk(7_200), "keepalive=240s,max=3600s")
    assert res.keepalive_pings == 15
    # the pings cost 15 × $0.02 and the request stays a full write
    assert res.cost.nano == usd("0.50") + usd("0.30") + usd("0.51") + usd("0.02")
    assert res.saving.nano == -usd("0.30")


def test_a4_claude_code_lane_is_never_pinged() -> None:
    lane = a1_lane(attribution=CC)
    res = replay(lane, "keepalive=240s,max=3600s")
    assert res.lanes_skipped == ((lane.lane_key, "keepalive not allowed for claude_code"),)
    assert res.keepalive_pings == 0
    assert same(res.cost, res.baseline) and res.saving.nano == 0


def test_a4_streaming_sdk_lane_is_not_skipped() -> None:
    lane = a1_lane(params=RequestParams(model_requested="claude-opus-5-5", stream=True))
    res = replay(lane, "keepalive=240s,max=3600s")
    assert res.lanes_skipped == ()
    assert res.cost.nano == usd("0.6924")


@pytest.mark.parametrize("params, reason", [
    (RequestParams(model_requested="claude-opus-5-5", output_format="set"),
     "keepalive skipped: structured outputs"),
    (RequestParams(model_requested="claude-opus-5-5", tool_choice="any"),
     "keepalive skipped: forced tool_choice"),
    (RequestParams(model_requested="claude-opus-5-5", tool_choice="tool:h_0123456789abcdef0123"),
     "keepalive skipped: forced tool_choice"),
    (RequestParams(model_requested="claude-opus-5-5", thinking="enabled:4096"),
     "keepalive skipped: thinking enabled"),
    (RequestParams(model_requested="claude-opus-5-5", service_tier_requested="batch"),
     "keepalive skipped: batch"),
])
def test_a4_ping_incompatible_lanes_are_skipped(params: RequestParams, reason: str) -> None:
    lane = a1_lane(params=params)
    res = replay(lane, "keepalive=240s,max=3600s")
    assert res.lanes_skipped == ((lane.lane_key, reason),)
    assert same(res.cost, res.baseline)


def test_a4_auto_tool_choice_and_adaptive_thinking_are_fine() -> None:
    lane = a1_lane(params=RequestParams(model_requested="claude-opus-5-5", tool_choice="auto",
                                        thinking="adaptive"))
    assert replay(lane, "keepalive=240s,max=3600s").cost.nano == usd("0.6924")


def test_a4_keepalive_selector_scopes_lanes() -> None:
    sdk = a1_lane(lane_key="sdk")
    other = a1_lane(lane_key="api", attribution={"agent_product": "api"})
    res = replay([sdk, other], "keepalive=240s,max=3600s@agent_product:agent_sdk")
    o = outcomes(res)
    changed = {rid for rid, x in o.items() if x.changed}
    assert changed == {r.request_id for r in sdk.requests[1:]}   # request 0 has no gap
    assert res.keepalive_pings == 3


# ------------------------------------------------------------------------------------------ A.5


def _a5_lane(**kw: object) -> object:
    """Opus 5.5 main lane billed at 1h, context 500,000, gaps [30 s, 2 h, 90 s]."""
    return table([(0, 0, 0, 500_000, 0, 0), (30, 500_000, 0, 0, 0, 0),
                  (7_230, 0, 0, 500_000, 0, 0), (7_320, 500_000, 0, 0, 0, 0)],
                 attribution=CC, **kw)


def test_a5_cold_resume_cost_observed_and_premium() -> None:
    lane = _a5_lane()
    res = replay(lane, "observed")
    o = outcomes(res)
    cold = lane.requests[2]
    assert o[cold.request_id].cost_nano == usd("4.00")
    fig = priced_request(cold)
    assert fig.nano == usd("4.00") and fig.evidence is Evidence.EXACT
    warm = price(UsageBuckets(cache_read=500_000))
    assert fig.nano - warm == usd("3.90")          # the ESTIMATED premium vs a warm read


def test_a5_cold_resume_compact_exactly_one_event() -> None:
    lane = _a5_lane()
    res = replay(lane, "cold-resume=compact,min=200000")
    assert res.added_calls == 1
    s_c = 20_283  # COMPACTION_SUMMARY_TOKENS_DEFAULT (no compaction events in the lane)
    assert any("20283" in a and "COMPACTION_SUMMARY_TOKENS_DEFAULT" in a
               for a in res.assumptions)
    o = outcomes(res)
    cold = o[lane.requests[2].request_id]
    assert [(e.kind, e.usage.uncached_input, e.usage.output) for e in cold.extra] == \
        [(InferenceKind.OTHER, 500_000, s_c)]
    assert (cold.usage.cache_read, cold.usage.cache_write_1h, cold.usage.uncached_input) == \
        (0, s_c, 0)
    after = o[lane.requests[3].request_id]
    assert after.usage.cache_read == s_c and after.usage.total_input == s_c
    expected = (usd("4.00") + usd("0.10")                       # requests 0-1 unchanged
                + usd("2.00") + usd("0.40566") + usd("0.162264")  # summary call + rewrite
                + usd("0.0040566"))                             # warm read of the summary
    assert res.cost.nano == expected
    assert res.saving.nano == usd("8.20") - expected
    assert res.saving.upper_bound
    assert not o[lane.requests[0].request_id].changed
    assert not o[lane.requests[1].request_id].changed


def test_cold_resume_clear_restarts_from_the_first_context() -> None:
    lane = table([(0, 0, 0, 300_000, 0, 0), (30, 300_000, 0, 10_000, 0, 0),
                  (7_230, 0, 0, 320_000, 0, 0), (7_290, 320_000, 0, 10_000, 0, 0)],
                 attribution=CC)
    res = replay(lane, "cold-resume=clear,min=200000")
    o = outcomes(res)
    third, fourth = (o[r.request_id] for r in lane.requests[2:])
    assert (third.usage.cache_read, third.usage.cache_write_1h) == (0, 310_000)
    assert (fourth.usage.cache_read, fourth.usage.cache_write_1h) == (310_000, 10_000)
    assert res.added_calls == 0
    assert res.saving.nano == usd("0.08") + usd("0.002")


def test_cold_resume_needs_a_cold_transition_above_min_context() -> None:
    assert replay(_a5_lane(), "cold-resume=compact,min=600000").added_calls == 0
    warm = table([(0, 0, 0, 500_000, 0, 0), (30, 500_000, 0, 0, 0, 0)], attribution=CC)
    assert replay(warm, "cold-resume=compact,min=200000").saving.nano == 0
    sub = _a5_lane(kind=LaneKind.SUBAGENT)
    assert replay(sub, "cold-resume=compact,min=200000").added_calls == 0


# ------------------------------------------------------------------------------------------ A.6


def test_a6_compaction_window_costs_1_999() -> None:
    lane = a6_lane()
    res = replay(lane, "compact-window=400000,post=20000")
    assert res.baseline.nano == usd("1.43")
    assert res.cost.nano == usd("1.999")
    assert res.saving.nano == -usd("0.569")
    assert res.added_calls == 1
    assert res.saving.upper_bound
    o = outcomes(res)
    r0, r1, r2 = (o[r.request_id] for r in lane.requests)
    assert not r0.changed and r0.cost_nano == usd("0.76")
    (comp,) = r1.extra
    assert comp.kind is InferenceKind.COMPACTION
    assert (comp.usage.cache_read, comp.usage.cache_write_5m, comp.usage.output) == \
        (300_000, 150_000, 20_000)
    assert (r1.usage.cache_read, r1.usage.cache_write_5m) == (0, 170_000)
    assert r1.cost_nano == usd("0.06") + usd("0.375") + usd("0.20") + usd("0.425") + usd("0.01")
    assert (r2.usage.cache_read, r2.usage.cache_write_5m) == (170_000, 50_000)
    assert r2.cost_nano == usd("0.034") + usd("0.125") + usd("0.01")


@pytest.mark.parametrize("window", [500_000, 700_000])
def test_a6_window_above_the_lane_maximum_changes_nothing(window: int) -> None:
    res = replay(a6_lane(), f"compact-window={window},post=20000")
    assert same(res.cost, res.baseline)
    assert res.saving.nano == 0 and res.added_calls == 0
    assert not any(o.changed for o in res.outcomes or ())


def test_a6_compaction_window_only_on_main_lanes_with_1m_context() -> None:
    sub = a6_lane(kind=LaneKind.SUBAGENT)
    assert replay(sub, "compact-window=400000,post=20000").saving.nano == 0
    haiku = table([(0, 0, 300_000, 0, 0, 1_000), (30, 300_000, 150_000, 0, 0, 1_000)],
                  model="claude-haiku-4-5", attribution=CC)
    res = replay(haiku, "compact-window=400000,post=20000")
    assert res.lanes_skipped == ((haiku.lane_key,
                                  "compaction window: model without 1m context"),)
    assert res.saving.nano == 0


def test_compaction_window_summary_defaults_without_post() -> None:
    """S_c is ``post=`` or the documented default; it is never derived from the lanes of one call
    (that would make sharded and unsharded replays differ, §9.1 #6)."""
    from tokenbill.core.records import LaneEvent

    lane = a6_lane(lane_key="a6")
    other = table([(0, 0, 50_000, 0, 0, 100)], lane_key="with-events", attribution=CC,
                  events=[LaneEvent(lane_key="with-events", ts_ms=at(-5), kind="compaction",
                                    attrs=(("post_tokens", 24_000),))])
    res = replay([lane, other], "compact-window=400000")
    assert any("20283" in a and "COMPACTION_SUMMARY_TOKENS_DEFAULT" in a
               for a in res.assumptions)
    comp = [e for o in res.outcomes or () for e in o.extra][0]
    assert comp.usage.output == 20_283


def test_request_without_serving_inference_passes_through() -> None:
    from tokenbill.core.builders import make_attempt, make_inference

    lane = a1_lane()
    residual = make_inference({"output": 900}, kind="output_residual", inference_id="res")
    extra = make_request(lane.lane_key, 9, at(2_000), attribution=SDK,
                         attempts=[make_attempt([residual], ts_ms=at(2_000))])
    from tokenbill.core.builders import make_lane

    lane2 = make_lane([*lane.requests, extra], lane_key=lane.lane_key)
    res = replay(lane2, "ttl=1h")
    o = outcomes(res)
    assert not o[extra.request_id].changed
    assert o[extra.request_id].cost_nano == priced_request(extra).nano
    assert res.saving.nano == usd("1.1508")

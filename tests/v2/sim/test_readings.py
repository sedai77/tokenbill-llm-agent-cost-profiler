"""The engine's readings of points SPEC §9.2–§9.4 leaves open (listed in ``README.md``), each
pinned by a hand-computed case so a ruling that changes one fails loudly here."""

from __future__ import annotations

from tokenbill.core.builders import make_lane, make_request
from tokenbill.core.labels import Evidence
from tokenbill.core.records import LaneKind, RequestParams, UsageBuckets, UsageSource

from .helpers import CC, SDK, a1_lane, at, outcomes, price, priced_request, replay, table, usd


def test_a_ttl_clause_does_not_apply_to_a_keepalive_lane() -> None:
    lane = a1_lane()
    res = replay(lane, "ttl=1h;keepalive=240s,max=3600s")
    assert (lane.lane_key, "ttl policy ignored on a keepalive lane (the TTL stays 5m)") in \
        res.lanes_skipped
    assert res.cost.nano == usd("0.6924")               # exactly the keepalive replay (A.4)


def test_rate_only_policies_are_exact() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500)], attribution=SDK, inference_geo="us")
    res = replay(lane, "geo=global;fast=off")
    assert res.cost.evidence is Evidence.EXACT and res.saving.evidence is Evidence.EXACT
    assert res.saving.nano == usd("0.051")
    # a flip makes it a counterfactual again
    r0 = make_request("f", 0, at(0), UsageBuckets(cache_write_5m=100_000, output=500),
                      attribution=SDK)
    r1 = make_request("f", 1, at(30), UsageBuckets(cache_write_5m=102_000, output=500),
                      attribution=SDK, speed="fast")
    assert replay(make_lane([r0, r1]), "fast=off").saving.evidence is Evidence.ESTIMATED


def test_min_prefix_gate_applies_to_any_changed_request() -> None:
    lane = table([(0, 0, 3_000, 0, 0, 10)], model="claude-haiku-4-5", attribution=SDK,
                 inference_geo="us")
    x = next(iter(outcomes(replay(lane, "geo=global")).values()))
    # Haiku caches from 4,096 tokens: a changed request below it caches nothing
    assert (x.usage.cache_write_5m, x.usage.uncached_input) == (0, 3_000)


def test_fast_off_makes_a_fast_request_batch_eligible() -> None:
    lane = table([(0, 50_000, 10_000, 0, 1_000, 2_000)],
                 attribution={"agent_product": "api", "workload_class": "ci"}, speed="fast")
    assert replay(lane, "batch=eligible").saving.nano == 0
    res = replay(lane, "fast=off;batch=eligible")
    x = next(iter(outcomes(res).values()))
    assert x.usage.cache_read == 32_000                  # h = 0.64 at the batch tier


def test_restore_caching_gap_is_ambiguous_near_the_ttl() -> None:
    lane = table([(t, 0, 0, 0, total, 0) for t, total in
                  zip((0, 30, 60, 90, 395), (10_000, 11_000, 12_000, 13_000, 14_000),
                      strict=True)], attribution=SDK)
    x = outcomes(replay(lane, "repair=restore_caching"))[lane.requests[4].request_id]
    warm = usd("0.0026") + usd("0.005")                  # 13k read + 1k 5m write
    cold = usd("0.07")                                   # 14k 5m writes
    assert (x.cost_nano, x.low_nano, x.high_nano) == (cold, warm, cold)   # 305 s > 300 s


def test_stagger_fanout_conserves_tokens() -> None:
    a = table([(0, 1_000, 20_000, 0, 0, 0)], lane_key="a", kind=LaneKind.SUBAGENT,
              attribution=SDK)
    b = table([(2, 2_000, 22_000, 0, 0, 0)], lane_key="b", kind=LaneKind.SUBAGENT,
              attribution=SDK)
    x = outcomes(replay([a, b], "repair=stagger_fanout"))[b.requests[0].request_id]
    assert (x.usage.cache_read, x.usage.cache_write_5m) == (22_000, 2_000)   # R + min(W, 20k)


def test_ci_chains_follow_each_runs_own_ttl_and_the_group_minimum() -> None:
    def run(key: str, start: int, w5: int = 0, w1: int = 0) -> object:
        return table([(start, 0, w5, w1, 0, 0)], lane_key=key, kind=LaneKind.API_RUN,
                     attribution={**SDK, "workload_class": "ci"})

    runs = [run("c1", 0, w1=40_000), run("c2", 400, w5=30_000), run("c3", 600, w5=50_000),
            run("c4", 90_000, w5=20_000)]
    res = replay(runs, "repair=shared_ci_prefix")
    o = outcomes(res)
    # c2 starts 400 s after c1 but writes at 5m: not chained; c3 is 200 s after c2: chained
    assert not o[runs[1].requests[0].request_id].changed
    third = o[runs[2].requests[0].request_id]
    assert third.usage.cache_read == 16_000             # floor(0.8 × min first W of the group)
    assert not o[runs[3].requests[0].request_id].changed


def test_unknown_ttl_falls_back_to_the_requests_own_writes_then_300_s() -> None:
    lane = table([(0, 0, 0, 0, 0, 1_000), (30, 0, 0, 0, 450_000, 1_000),
                  (340, 0, 0, 0, 500_000, 1_000)], model="claude-sonnet-5", attribution=CC)
    res = replay(lane, "compact-window=400000,post=20000")
    o = outcomes(res)
    second = o[lane.requests[1].request_id]
    (comp,) = second.extra
    assert comp.usage.cache_read == 0                   # no prefix to read (P'_0 = 0)
    third = o[lane.requests[2].request_id]
    (comp3,) = third.extra
    # τ unknown and no writes of its own → 300 s: the 310 s gap is cold (outside ±10 s)
    assert comp3.usage.cache_read == 0


def test_changed_writes_keep_the_requests_own_class() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500), (420, 0, 0, 102_000, 0, 500)], attribution=SDK)
    x = outcomes(replay(lane, "keepalive=240s,max=3600s"))[lane.requests[1].request_id]
    assert (x.usage.cache_read, x.usage.cache_write_5m, x.usage.cache_write_1h) == \
        (100_000, 2_000, 0)                             # keepalive lanes write at 5m
    lane2 = table([(0, 0, 0, 100_000, 0, 500), (420, 100_000, 0, 2_000, 0, 500)],
                  attribution=SDK)
    y = outcomes(replay(lane2, "ttl=5m"))[lane2.requests[1].request_id]
    assert y.usage.cache_write_5m == 102_000


def test_effort_keeps_the_placeholder_upper_bound() -> None:
    params = RequestParams(model_requested="claude-opus-5-5", effort="max")
    req = make_request("p", 0, at(0), UsageBuckets(cache_write_5m=10_000, output=1_000),
                       params=params, attribution=SDK,
                       usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=4_000)
    x = outcomes(replay(make_lane([req]), "effort=high,scale=0.5"))[req.request_id]
    assert x.usage.output == 748
    assert x.high_nano == usd("0.05") + usd("0.08")     # upper 4,000 × $20/M, unchanged


def test_tokenizer_band_is_rounded_half_even() -> None:
    lane = table([(0, 0, 1_001, 0, 0, 3)], model="claude-sonnet-4-6", attribution=CC)
    res = replay(lane, "model=claude-sonnet-5")
    # 1,001 × 1.35 = 1,351.35 → 1,351; 3 × 1.35 = 4.05 → 4
    assert res.cost.high_nano == price(UsageBuckets(cache_write_5m=1_351, output=4),
                                       "claude-sonnet-5")


def test_no_band_between_undocumented_families() -> None:
    lane = table([(0, 0, 0, 0, 20_000, 100)], model="gpt-5.6-sol", attribution=SDK)
    res = replay(lane, "model=claude-sonnet-5")
    assert res.cost.low_nano is None


def test_unchanged_requests_add_nothing_to_the_saving_range() -> None:
    """Savings are per request: an unaffected request with a priced range (unknown-TTL writes)
    contributes exactly 0, never ``[low − high, high − low]``."""
    unknown = table([(0, 0, 0, 0, 0, 100)], lane_key="u", attribution=SDK)
    ranged = make_request("r", 0, at(0), UsageBuckets(cache_write_unknown=50_000, output=10),
                          attribution=SDK)
    ranged_lane = make_lane([ranged], kind=LaneKind.SUBAGENT)
    res = replay([a1_lane(lane_key="a"), ranged_lane, unknown], "ttl=1h@lane_kind:main")
    assert not outcomes(res)[ranged.request_id].changed
    assert res.saving.low_nano is None and res.saving.nano == usd("1.1508")
    assert res.baseline.low_nano is not None                # the ledger itself has a range
    assert priced_request(ranged).low_nano is not None

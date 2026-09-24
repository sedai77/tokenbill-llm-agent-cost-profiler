"""TTL two-way flips, ambiguity ranges, re-rating and keepalive (SPEC §9.2, §9.3.1, §9.3.2) on
small hand-computed lanes. Opus 5.5 rates in nano per token: input 4,000, output 20,000,
read 200, 5m write 5,000, 1h write 8,000."""

from __future__ import annotations

import pytest

from tokenbill.core.builders import make_inference
from tokenbill.core.records import InferenceKind, LaneKind, RequestParams
from tokenbill.core.types import Policy

from .helpers import bounds, costs, lane, lane_of, replay, req

READ, W5, W1, OUT = 200, 5_000, 8_000, 20_000


def _hit_lane_1h(gap_s: int):
    """Billed at 1h: request 1 hits the 100k prefix after *gap_s* seconds."""
    return lane([(0, 0, 0, 100_000, 0, 0), (gap_s, 100_000, 0, 2_000, 0, 0)],
                lane_key="L1h", attribution={"agent_product": "claude_code"})


@pytest.mark.parametrize("gap_s,point,low,high", [
    (305, 102_000 * W5, 100_000 * READ + 2_000 * W5, 102_000 * W5),   # ambiguous, rule: miss
    (310, 102_000 * W5, 100_000 * READ + 2_000 * W5, 102_000 * W5),   # the ±10 s edge is inclusive
    (295, 100_000 * READ + 2_000 * W5, 100_000 * READ + 2_000 * W5, 102_000 * W5),  # rule: hit
    (311, 102_000 * W5, 102_000 * W5, 102_000 * W5),                  # not ambiguous
    (120, 100_000 * READ + 2_000 * W5, 100_000 * READ + 2_000 * W5,
     100_000 * READ + 2_000 * W5),
])
def test_hit_to_miss_with_ambiguity_ranges(gap_s: int, point: int, low: int, high: int) -> None:
    res = replay([_hit_lane_1h(gap_s)], "ttl=5m")
    first, second = costs(res)
    assert first == (100_000 * W5,) * 3                    # re-rated 1h → 5m, exact
    assert second == (point, low, high)
    cost = (100_000 * W5 + point, 100_000 * W5 + low, 100_000 * W5 + high)
    assert bounds(res.cost) == cost
    base = 100_000 * W1 + 100_000 * READ + 2_000 * W1
    assert res.baseline.nano == base and res.baseline.low_nano is None
    assert bounds(res.saving) == (base - cost[0], base - cost[2], base - cost[1])
    assert (res.cost.low_nano is None) == (low == high == point)


def test_hit_to_miss_reads_the_static_prefix_floor() -> None:
    lanes = [_hit_lane_1h(400)]
    floor = {("ws:test", "claude-opus-5-5"): 40_000}
    res = replay(lanes, "ttl=5m", floor=floor)
    assert costs(res)[1] == (40_000 * READ + 62_000 * W5,) * 3
    assert (res.outcomes[1].usage.cache_read, res.outcomes[1].usage.cache_write_5m) == \
        (40_000, 62_000)  # type: ignore[index]
    no_floor = replay(lanes, "ttl=5m")
    assert costs(no_floor)[1] == (102_000 * W5,) * 3


def test_hit_beyond_the_observed_ttl_is_not_flipped() -> None:
    # a hit whose gap exceeds even the observed 1h TTL keeps its split under ttl=5m
    res = replay([_hit_lane_1h(4_000)], "ttl=5m")
    assert costs(res)[1] == (100_000 * READ + 2_000 * W5,) * 3


def test_miss_to_hit_needs_ttl_expiry_and_alive() -> None:
    # SDK lane: request 1 changes effort (no exemption without the beta) after 420 s: the miss
    # is classified ttl-expiry, but alive_π fails (un-repaired param change): no flip
    reqs = [req("Leff", 0, 0, {"cache_write_5m": 100_000},
                attribution={"agent_product": "agent_sdk"},
                params=RequestParams(model_requested="claude-opus-5-5", effort="high")),
            req("Leff", 1, 420, {"cache_write_5m": 102_000},
                attribution={"agent_product": "agent_sdk"},
                params=RequestParams(model_requested="claude-opus-5-5", effort="low"))]
    res = replay([lane_of(reqs)], "ttl=1h")
    assert costs(res) == [(100_000 * W1,) * 3, (102_000 * W1,) * 3]
    # the same lane without the effort change flips
    plain = lane([(0, 0, 100_000, 0, 0, 0), (420, 0, 102_000, 0, 0, 0)], lane_key="Lp",
                 attribution={"agent_product": "agent_sdk"})
    assert costs(replay([plain], "ttl=1h"))[1] == (100_000 * READ + 2_000 * W1,) * 3


def test_model_switch_miss_is_not_a_ttl_flip() -> None:
    reqs = [req("Lm", 0, 0, {"cache_write_5m": 100_000}),
            req("Lm", 1, 420, {"cache_write_5m": 102_000}, model="claude-opus-5")]
    res = replay([lane_of(reqs)], "ttl=1h")
    assert res.outcomes is not None and res.outcomes[1].usage.cache_read == 0
    assert res.outcomes[1].usage.cache_write_1h == 102_000


def test_ttl_policy_skips_non_anthropic_lanes() -> None:
    oa = lane_of([req("Loa", 0, 0, {"uncached_input": 5_000, "output": 10}, "gpt-5.6-sol"),
                  req("Loa", 1, 60, {"uncached_input": 1_000, "cache_read": 4_096,
                                     "output": 10}, "gpt-5.6-sol")], scope="org:openai_api:a")
    res = replay([oa], "ttl=1h")
    assert res.lanes_skipped == (("Loa", "ttl policy applies to Anthropic channels only"),)
    assert res.cost.nano == res.baseline.nano
    assert all(not o.changed for o in res.outcomes or ())


def test_ttl_rerates_passthrough_and_unknown_writes() -> None:
    comp_iter = req("Lp2", 0, 0, {"cache_write_5m": 10_000, "output": 100})
    iteration = make_inference({"cache_read": 5_000, "cache_write_5m": 3_000, "output": 200},
                               kind=InferenceKind.COMPACTION, inference_id="inf_iter")
    with_iter = req("Lp2", 1, 30, {"cache_read": 10_000, "cache_write_5m": 1_000,
                                   "output": 100}, extra_inferences=[iteration])
    unknown = req("Lp2", 2, 60, {"cache_read": 11_000, "cache_write_unknown": 4_000,
                                 "output": 100})
    ln = lane_of([comp_iter, with_iter, unknown])
    obs = replay([ln], Policy.observed())
    assert costs(obs)[2] == (11_000 * READ + 4_000 * W5 + 100 * OUT,
                             11_000 * READ + 4_000 * W5 + 100 * OUT,
                             11_000 * READ + 4_000 * W1 + 100 * OUT)
    res = replay([ln], "ttl=1h")
    one, two, three = costs(res)
    assert one == (10_000 * W1 + 100 * OUT,) * 3
    assert two == (10_000 * READ + 1_000 * W1 + 100 * OUT
                   + 5_000 * READ + 3_000 * W1 + 200 * OUT,) * 3
    assert three == (11_000 * READ + 4_000 * W1 + 100 * OUT,) * 3
    down = replay([ln], "ttl=5m")
    assert costs(down)[2] == (11_000 * READ + 4_000 * W5 + 100 * OUT,) * 3


def test_unknown_ttl_lane_uses_the_hint_as_observed_tau() -> None:
    reqs = [req("Lh", 0, 0, {"cache_write_unknown": 100_000}, write_ttl_hint="1h"),
            req("Lh", 1, 1_000, {"cache_read": 100_000, "cache_write_unknown": 2_000},
                write_ttl_hint="1h")]
    res = replay([lane_of(reqs)], "ttl=5m")
    # τ_obs = 3600 (hint) > τπ = 300 and the 1,000 s gap exceeds 300 s: hit → miss
    assert res.outcomes is not None
    assert (res.outcomes[1].usage.cache_read, res.outcomes[1].usage.cache_write_5m) == \
        (0, 102_000)


def test_selector_scoped_ttl_leaves_other_lanes_unchanged() -> None:
    main = lane([(0, 0, 100_000, 0, 0, 0), (420, 0, 102_000, 0, 0, 0)], lane_key="Lmain")
    sub = lane([(0, 0, 100_000, 0, 0, 0), (420, 0, 102_000, 0, 0, 0)], lane_key="Lsub",
               kind=LaneKind.SUBAGENT)
    res = replay([main, sub], "ttl=1h@lane_kind:main")
    assert dict(res.per_lane)["Lsub"] == 202_000 * W5
    assert dict(res.per_lane)["Lmain"] == 100_000 * W1 + 100_000 * READ + 2_000 * W1
    assert [o.changed for o in res.outcomes or ()] == [True, True, False, False]


# ---------------------------------------------------------------------------------------------
# keepalive
# ---------------------------------------------------------------------------------------------


def _sdk(rows, key="Lk", **params):
    return lane(rows, lane_key=key, kind=LaneKind.API_RUN,
                attribution={"agent_product": "agent_sdk"},
                params=RequestParams(model_requested="claude-opus-5-5", **params)
                if params else None)


def test_keepalive_max_idle_caps_pings_and_leaves_the_gap_cold() -> None:
    rows = [(0, 0, 100_000, 0, 0, 0), (1_200, 0, 102_000, 0, 0, 0)]
    res = replay([_sdk(rows)], "keepalive=240s,max=600s")
    # n = min(ceil(1200/240) − 1, floor(600/240)) = 2; 2·240 + 300 = 780 < 1200: cold
    assert res.keepalive_pings == 2
    assert costs(res)[1] == (102_000 * W5 + 2 * 100_000 * READ,) * 3
    assert res.saving.nano == -2 * 100_000 * READ


def test_keepalive_gap_below_the_interval_sends_no_ping() -> None:
    rows = [(0, 0, 100_000, 0, 0, 0), (200, 100_000, 2_000, 0, 0, 0)]
    res = replay([_sdk(rows)], "keepalive=240s")
    assert res.keepalive_pings == 0 and res.saving.nano == 0


def test_keepalive_ping_reads_prefix_and_uncached_of_the_previous_request() -> None:
    rows = [(0, 0, 100_000, 0, 700, 0), (420, 0, 102_000, 0, 700, 0)]
    res = replay([_sdk(rows)], "keepalive=240s")
    ping = res.outcomes[1].extra[0]  # type: ignore[index]
    assert (ping.usage.cache_read, ping.usage.uncached_input, ping.usage.output) == \
        (100_000, 700, 0)
    assert ping.kind is InferenceKind.KEEPALIVE
    flipped = res.outcomes[1].usage  # type: ignore[index]
    assert (flipped.cache_read, flipped.cache_write_5m, flipped.uncached_input) == \
        (100_000, 2_000, 700)


@pytest.mark.parametrize("params,reason", [
    ({"output_format": "set"}, "keepalive: structured outputs"),
    ({"tool_choice": "any"}, "keepalive: forced tool_choice"),
    ({"tool_choice": "tool:h_0123456789abcdef0123"}, "keepalive: forced tool_choice"),
    ({"thinking": "enabled:2048"}, "keepalive: thinking enabled (rejected with max_tokens 0)"),
    ({"service_tier_requested": "batch"}, "keepalive: batch requests"),
])
def test_keepalive_skips_lanes_it_cannot_ping(params: dict, reason: str) -> None:
    rows = [(0, 0, 100_000, 0, 0, 0), (420, 0, 102_000, 0, 0, 0)]
    res = replay([_sdk(rows, **params)], "keepalive=240s")
    assert res.lanes_skipped == (("Lk", reason),)
    assert res.keepalive_pings == 0 and res.saving.nano == 0


def test_keepalive_allows_auto_tool_choice_and_adaptive_thinking() -> None:
    rows = [(0, 0, 100_000, 0, 0, 0), (420, 0, 102_000, 0, 0, 0)]
    res = replay([_sdk(rows, tool_choice="auto", thinking="adaptive")], "keepalive=240s")
    assert res.keepalive_pings == 1 and res.lanes_skipped == ()


def test_keepalive_lane_ignores_a_ttl_clause_and_selectors_apply() -> None:
    rows = [(0, 0, 100_000, 0, 0, 0), (420, 0, 102_000, 0, 0, 0)]
    res = replay([_sdk(rows)], "ttl=1h;keepalive=240s")
    assert ("Lk", "ttl policy ignored: keepalive lane stays on the 5m TTL") in res.lanes_skipped
    assert res.outcomes is not None and res.outcomes[0].usage.cache_write_5m == 100_000
    other = replay([_sdk(rows)], "keepalive=240s@agent_product:api")
    assert other.keepalive_pings == 0 and other.lanes_skipped == ()

"""The documented cache rules of the block model (SPEC §9.7 #2–#4, §19.3): lookback with collapsed
runs, TTL refresh, first-token visibility, minimum prefix, breakpoint limits, scope and sizing.

Expected token counts are hand-computed: block sizes are exact integers and, unless a test says
otherwise, billed totals equal the sum of the block sizes (scale 1)."""

from __future__ import annotations

import pytest

from tests.v2.blocksim.helpers import blk, lane, req, size, system, usage
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.policy import parse_policy
from tokenbill.core.testing import FakePricer
from tokenbill.sim.block_replay import BlockReplayer

P = FakePricer()


def predicted(*lanes, placement: str = "observed"):
    outs = BlockReplayer().predict(list(lanes), pricer=P, placement=placement)
    return [(o.usage.cache_read, o.usage.cache_write_5m, o.usage.cache_write_1h,
             o.usage.uncached_input) for o in outs]


def texts(prefix: str, n: int, tokens: int = 100) -> list:
    return [blk(f"{prefix}{i}", tokens) for i in range(n)]


BASE = [system("sys", 1000), blk("m0", 1000)]            # positions 0, 1


@pytest.mark.parametrize("added,found", [(18, True), (19, True), (20, False), (21, False)])
def test_lookback_counts_twenty_positions_including_the_breakpoint(added: int, found: bool
                                                                   ) -> None:
    a_blocks = BASE
    b_blocks = BASE + texts("x", added)
    a = req("L", 0, 0, a_blocks, usage(w5=size(a_blocks)))
    b = req("L", 1, 30, b_blocks, usage(w5=size(b_blocks)))
    got = predicted(lane([a, b]))
    assert got[0] == (0, 2000, 0, 0)
    if found:
        assert got[1] == (2000, 100 * added, 0, 0)
    else:
        assert got[1] == (0, 2000 + 100 * added, 0, 0)


def test_a_run_of_25_tool_results_is_one_position() -> None:
    results = [blk(f"r{i}", 40, kind="tool_result") for i in range(25)]
    b_blocks = BASE + results
    a = req("L", 0, 0, BASE, usage(w5=2000))
    b = req("L", 1, 30, b_blocks, usage(w5=size(b_blocks)))
    assert predicted(lane([a, b]))[1] == (2000, 1000, 0, 0)


def test_without_run_collapsing_25_tool_results_are_25_positions() -> None:
    # Bedrock does not collapse tool runs in the cache-rule table: the entry is out of reach.
    results = [blk(f"r{i}", 40, kind="tool_result") for i in range(25)]
    b_blocks = BASE + results
    kw = {"model": "claude-opus-5", "channel": "bedrock", "endpoint_scope": "global"}
    a = req("L", 0, 0, BASE, usage(w5=2000), **kw)
    b = req("L", 1, 30, b_blocks, usage(w5=size(b_blocks)), **kw)
    assert predicted(lane([a, b], scope="org:bedrock:1"))[1] == (0, 3000, 0, 0)


def test_ttl_refresh_on_read_keeps_an_entry_alive() -> None:
    a = req("L", 0, 0, BASE, usage(w5=2000))
    b = req("L", 1, 200, BASE + [blk("m1")], usage(r=2000, w5=1000))
    c = req("L", 2, 450, BASE + [blk("m2")], usage(r=2000, w5=1000))
    got = predicted(lane([a, b, c]))
    assert got[2] == (2000, 1000, 0, 0)          # read at 450 s: refreshed at 200 s + 300 s


def test_without_a_read_the_entry_expires_after_its_ttl() -> None:
    a = req("L", 0, 0, BASE, usage(w5=2000))
    b = req("L", 1, 200, BASE + [blk("m1")], usage(u=3000), bps=None)     # no lookup
    c = req("L", 2, 450, BASE + [blk("m2")], usage(w5=3000))
    assert predicted(lane([a, b, c]))[2] == (0, 3000, 0, 0)


def test_one_hour_ttl_survives_a_twenty_minute_gap() -> None:
    a1 = req("L", 0, 0, BASE, usage(w1=2000), bps=[(1, "1h")])
    b1 = req("L", 1, 1200, BASE + [blk("m1")], usage(r=2000, w1=1000), bps=[(2, "1h")])
    assert predicted(lane([a1, b1])) == [(0, 0, 2000, 0), (2000, 0, 1000, 0)]
    a5 = req("L", 0, 0, BASE, usage(w5=2000))
    b5 = req("L", 1, 1200, BASE + [blk("m1")], usage(w5=3000))
    assert predicted(lane([a5, b5]))[1] == (0, 3000, 0, 0)


def test_same_timestamp_siblings_never_read_each_other() -> None:
    lanes = [lane([req(f"L{i}", 0, 0, BASE, usage(w5=2000))]) for i in range(3)]
    assert predicted(*lanes) == [(0, 2000, 0, 0)] * 3


@pytest.mark.parametrize("ttft_ms,start_s,reads", [
    (1500, 2.0, True), (3000, 2.0, False), (None, 0.5, False), (None, 1.0, True)])
def test_an_entry_is_visible_from_the_writers_first_token(ttft_ms, start_s, reads) -> None:
    a = lane([req("L1", 0, 0, BASE, usage(w5=2000), ttft_ms=ttft_ms)])
    b = lane([req("L2", 0, start_s, BASE + [blk("q")], usage(w5=3000))])
    got = predicted(a, b)
    assert got[1] == ((2000, 1000, 0, 0) if reads else (0, 3000, 0, 0))


def test_duration_is_the_visibility_fallback_before_the_default() -> None:
    a = lane([req("L1", 0, 0, BASE, usage(w5=2000), duration_ms=4000)])
    b = lane([req("L2", 0, 2, BASE + [blk("q")], usage(w5=3000))])
    assert predicted(a, b)[1] == (0, 3000, 0, 0)            # 2 s < 4 s duration


def test_a_breakpoint_below_the_model_minimum_writes_nothing() -> None:
    small = [system("tiny", 300), blk("s", 100)]             # 400 < 512 (Opus 5.5 minimum)
    assert predicted(lane([req("L", 0, 0, small, usage(u=400))])) == [(0, 0, 0, 400)]
    ok = [system("tiny", 300), blk("s2", 300)]               # 600 ≥ 512
    assert predicted(lane([req("L", 0, 0, ok, usage(w5=600))])) == [(0, 600, 0, 0)]


def test_below_minimum_segments_merge_into_the_next_valid_breakpoint() -> None:
    blocks = [system("tiny", 300), blk("a", 300), blk("b", 400)]
    r = req("L", 0, 0, blocks, usage(w5=1000), bps=[(0, "1h"), (1, "5m"), (2, "5m")])
    assert predicted(lane([r])) == [(0, 1000, 0, 0)]         # bp 0 (300 tokens) writes nothing


def test_tokens_are_rescaled_to_the_billed_total_input() -> None:
    r = req("L", 0, 0, BASE, usage(w1=1500, w5=1500), bps=[(0, "1h"), (1, "5m")])
    assert predicted(lane([r])) == [(0, 1500, 1500, 0)]      # units 1000 + 1000 → 3000 billed
    r2 = req("L", 0, 0, BASE + [blk("tail")], usage(w5=2000, u=1000), bps=[(1, "5m")])
    assert predicted(lane([r2])) == [(0, 2000, 0, 1000)]     # after the last breakpoint: uncached


def test_a_hit_reads_the_size_its_entry_recorded() -> None:
    a = req("L", 0, 0, BASE, usage(w5=2000))
    b = req("L", 1, 30, BASE + [blk("m1")], usage(r=2000, w5=400))        # billed total 2,400
    assert predicted(lane([a, b]))[1] == (2000, 400, 0, 0)


def test_a_hit_covering_every_block_reads_the_whole_request() -> None:
    a = req("L", 0, 0, BASE, usage(w5=2000))
    b = req("L", 1, 30, BASE, usage(r=2100))                 # same blocks, billed 2,100
    assert predicted(lane([a, b]))[1] == (2100, 0, 0, 0)


def test_at_most_four_breakpoints_are_used() -> None:
    blocks = [system("s5", 600)] + texts("b", 4, 100)
    r = req("L", 0, 0, blocks, usage(w5=900, u=100), bps=[(i, "5m") for i in range(5)])
    assert predicted(lane([r])) == [(0, 900, 0, 100)]        # the fifth marker is ignored


def test_automatic_caching_takes_the_end_of_the_prompt() -> None:
    r = req("L", 0, 0, BASE, usage(w5=2000), bps=None, auto=True)
    assert predicted(lane([r])) == [(0, 2000, 0, 0)]
    r2 = req("L", 0, 0, BASE, usage(u=2000), bps=None)
    assert predicted(lane([r2])) == [(0, 0, 0, 2000)]


@pytest.mark.parametrize("scope_a,scope_b,shared", [
    ("ws:a", "ws:a", True), ("ws:a", "ws:b", False), ("unknown", "unknown", False)])
def test_entries_are_shared_within_one_cache_scope(scope_a, scope_b, shared) -> None:
    a = lane([req("L1", 0, 0, BASE, usage(w5=2000))], scope=scope_a)
    b = lane([req("L2", 0, 30, BASE + [blk("q")], usage(w5=3000))], scope=scope_b)
    assert predicted(a, b)[1] == ((2000, 1000, 0, 0) if shared else (0, 3000, 0, 0))


def test_entries_are_per_model() -> None:
    a = lane([req("L1", 0, 0, BASE, usage(w5=2000))])
    b = lane([req("L2", 0, 30, BASE + [blk("q")], usage(w5=3000), model="claude-opus-5")])
    assert predicted(a, b)[1] == (0, 3000, 0, 0)


def test_openai_rules_have_no_lookback_limit_and_response_end_visibility() -> None:
    kw = {"model": "gpt-5.6-sol"}
    far = BASE + texts("x", 30)
    a = req("L", 0, 0, [system("osys", 1500), blk("om", 1000)], usage(w5=2500), **kw)
    b = req("L", 1, 30, [system("osys", 1500), blk("om", 1000)] + texts("x", 30),
            usage(w5=5500), **kw)
    got = predicted(lane([a, b], scope="org:openai:1"))
    assert got[1][0] == 2500 and len(far) == 32


def test_ttl_order_violations_are_reported_in_assumptions() -> None:
    r = req("L", 0, 0, BASE, usage(w5=1000, w1=1000), bps=[(0, "5m"), (1, "1h")])
    res = BlockReplayer().replay([lane([r])], parse_policy("repair=block:stagger"),
                                 mode="documented", pricer=P, rules=RulesTable(),
                                 calibration=None)
    assert any("not longest-first" in a for a in res.assumptions)


def test_predict_rejects_an_unknown_placement() -> None:
    from tokenbill.core.errors import UsageError
    with pytest.raises(UsageError):
        BlockReplayer().predict([], pricer=P, placement="middle")

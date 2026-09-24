"""Edge cases of the block engine and the breakers: passthrough requests, empty fingerprints,
salt changes inside a lane, idle expiry, zero-size tails, rewinds, unconfirmed overflow, size
fallbacks and passthrough inferences."""

from __future__ import annotations

import dataclasses

from tests.v2.blocksim.helpers import (
    W5,
    blk,
    context,
    conversation,
    kinds,
    lane,
    req,
    size,
    system,
    usage,
)
from tokenbill.core.builders import make_attempt, make_block, make_inference
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.policy import parse_policy
from tokenbill.core.records import InferenceKind, Lane, Request
from tokenbill.core.testing import FakePricer
from tokenbill.detect.block import BlockBreakers
from tokenbill.sim.block_replay import (
    BlockReplayer,
    chain_hashes,
    first_divergence,
    lane_skip_reason,
)

P = FakePricer()
SYS = system("esys", 1000)


def predicted(lanes, **kw):
    return [(o.usage.cache_read, o.usage.cache_write_5m, o.usage.uncached_input)
            for o in BlockReplayer().predict(list(lanes), pricer=P, **kw)]


def without_serving(r: Request) -> Request:
    att = make_attempt([], ts_ms=r.ts_start_ms, attempt_id=r.attempts[0].attempt_id,
                       outcome="http_error", http_status=529)
    return dataclasses.replace(r, attempts=(att,), fingerprint=None)


def test_requests_without_a_serving_inference_pass_through() -> None:
    r0 = req("PT", 0, 0, [SYS, blk("a")], usage(w5=2000))
    r1 = without_serving(req("PT", 1, 10, [SYS, blk("a")], usage(w5=2000)))
    r2 = req("PT", 2, 30, [SYS, blk("a"), blk("b")], usage(r=2000, w5=1000))
    ln = lane([r0, r1, r2])
    assert lane_skip_reason(ln) is None
    outs = BlockReplayer().predict([ln], pricer=P)
    assert [o.request_id for o in outs] == [r0.request_id, r1.request_id, r2.request_id]
    assert outs[1].cost_nano == 0 and not outs[1].changed
    res = BlockReplayer().replay([ln], parse_policy("breakpoints=every_15"), mode="documented",
                                 pricer=P, rules=RulesTable(), calibration=None,
                                 keep_outcomes=True)
    assert res.n_requests == 3 and not res.outcomes[1].changed
    only_failures = lane([without_serving(req("NF", 0, 0, [SYS], usage(w5=1)))])
    assert lane_skip_reason(only_failures) == "no requests with a serving inference"
    assert first_divergence(r1, r0) is None


def test_a_request_without_a_serving_inference_uses_default_rules() -> None:
    r = without_serving(req("DR", 0, 0, [SYS], usage(w5=1)))
    r = dataclasses.replace(r, fingerprint=req("DR", 0, 0, [SYS], usage(w5=1)).fingerprint)
    assert len(chain_hashes(r)) == 1


def test_an_empty_fingerprint_is_all_uncached() -> None:
    r = req("EF", 0, 0, [], usage(u=500), bps=None)
    assert predicted([lane([r])], placement="every_15") == [(0, 0, 500)]


def test_a_speed_change_inside_a_lane_breaks_the_system_tier() -> None:
    r0 = req("SP", 0, 0, [SYS, blk("a")], usage(w5=2000), speed="standard")
    r1 = req("SP", 1, 30, [SYS, blk("a"), blk("b")], usage(w5=3000), speed="fast")
    assert predicted([lane([r0, r1])])[1] == (0, 3000, 0)
    assert "param-churn" in kinds(BlockBreakers().detect([lane([r0, r1])],
                                                         context(min_usd="0")))


def test_an_unreported_parameter_between_two_values_breaks_no_chain() -> None:
    # effort high → (not reported) → low: core.transitions compares neighbours only and None is
    # never a change, so neither step is an effort change — and the chain reads through
    sdk = {"agent_product": "agent_sdk"}
    msgs = [blk(f"ur{i}") for i in range(3)]
    reqs = []
    for i, effort in enumerate(("high", None, "low")):
        blocks = [SYS, *msgs[: i + 1]]
        reqs.append(req("UR", i, 30 * i, blocks, usage(w5=size(blocks)),
                        params={"effort": effort}, attribution=sdk))
    ln = lane(reqs)
    assert [first_divergence(reqs[i - 1], reqs[i]) for i in (1, 2)] == [None, None]
    assert predicted([ln]) == [(0, 2000, 0), (2000, 1000, 0), (3000, 1000, 0)]
    assert BlockBreakers().detect([ln], context(min_usd="0")) == []


class AllTiers:
    """A rules provider on which effort also salts the tools and system tiers."""

    def rules_for(self, provider: str, channel: str, model: str):
        base = RulesTable().rules_for(provider, channel, model)
        return dataclasses.replace(base, effort_invalidates_all_tiers_models=(model,))


def test_effort_on_an_all_tiers_model_breaks_the_whole_chain() -> None:
    sdk = {"agent_product": "agent_sdk"}
    first = system("at-sys", 1000)
    r0 = req("AT", 0, 0, [first, blk("a")], usage(w5=2000), params={"effort": "high"},
             attribution=sdk, bps=[(0, "5m"), (1, "5m")])
    r1 = req("AT", 1, 30, [first, blk("a"), blk("b")], usage(w5=3000), params={"effort": "low"},
             attribution=sdk, bps=[(0, "5m"), (2, "5m")])
    assert predicted([lane([r0, r1])], rules=AllTiers())[1] == (0, 3000, 0)
    assert predicted([lane([r0, r1])])[1] == (1000, 2000, 0)       # system entry still read


def test_an_expired_unread_entry_is_replaced_and_idle_gaps_are_not_breakers() -> None:
    r0 = req("ID", 0, 0, [SYS, blk("a")], usage(w5=2000))
    r1 = req("ID", 1, 700, [SYS, blk("a")], usage(w5=2000))
    r2 = req("ID", 2, 730, [SYS, blk("a"), blk("b")], usage(r=2000, w5=1000))
    ln = lane([r0, r1, r2])
    assert predicted([ln]) == [(0, 2000, 0), (0, 2000, 0), (2000, 1000, 0)]
    assert BlockBreakers().detect([ln], context(min_usd="0")) == []
    res = BlockReplayer().replay([ln], parse_policy("repair=block:drop_unread"),
                                 mode="documented", pricer=P, rules=RulesTable(),
                                 calibration=None)
    # r0's write expired unread over the idle gap: that is the TTL levers' saving (usage
    # level), not write-never-read's, so block:drop_unread keeps the breakpoint
    assert res.saving.nano == 0
    # without the idle gap, the same never-read write is dropped (a one-shot lane)
    one_shot = lane([req("ID1", 0, 0, [SYS, blk("a")], usage(w5=2000))])
    res1 = BlockReplayer().replay([one_shot], parse_policy("repair=block:drop_unread"),
                                  mode="documented", pricer=P, rules=RulesTable(),
                                  calibration=None)
    assert res1.saving.nano == 2000 * (W5 - 4000)


def test_zero_size_blocks_after_a_hit_write_nothing() -> None:
    empty = make_block("h:empty", n_bytes=0, est_tokens=0)
    r0 = req("ZS", 0, 0, [SYS, blk("a")], usage(w5=2000))
    r1 = req("ZS", 1, 30, [SYS, blk("a"), empty], usage(r=2000))
    assert predicted([lane([r0, r1])])[1] == (2000, 0, 0)


def test_missing_size_estimates_fall_back_to_bytes() -> None:
    sized = make_block("h:bytes", n_bytes=3600, est_tokens=None)
    r = req("BY", 0, 0, [SYS, sized], usage(w5=2000), bps=[(0, "5m"), (1, "5m")])
    assert predicted([lane([r])]) == [(0, 2000, 0)]       # 1000 + 1000 units, both cacheable


def test_a_rewind_to_an_earlier_prefix_is_not_a_divergence_breaker() -> None:
    blocks = [SYS, blk("r0"), blk("r1"), blk("r2")]
    r0 = req("RW", 0, 0, blocks, usage(w5=size(blocks)))
    r1 = req("RW", 1, 30, blocks[:2], usage(w5=size(blocks[:2])))
    assert first_divergence(r0, r1) is None
    # only a placement finding: a breakpoint at the lane's common prefix lets the rewind read
    found = BlockBreakers().detect([lane([r0, r1])], context(min_usd="0"))
    assert kinds(found) == ["breakpoint-placement"]
    assert found[0].recoverable.nano == 2000 * W5 - 2000 * 200


def test_lookback_overflow_needs_billed_confirmation() -> None:
    turns = [[blk(f"lx{t}-{j}", 40) for j in range(25)] for t in range(3)]
    working = lane(conversation("LW", [system("lw", 1000)], turns, billed="working"))
    assert "lookback-overflow" not in kinds(BlockBreakers().detect([working],
                                                                   context(min_usd="0")))


def test_fanout_entries_are_kept_by_drop_unread() -> None:
    lanes = [lane([req(f"FK{i}", 0, 0, [system("fk", 3000)], usage(w5=3000))]) for i in range(2)]
    res = BlockReplayer().replay(lanes, parse_policy("repair=block:drop_unread"),
                                 mode="documented", pricer=P, rules=RulesTable(),
                                 calibration=None)
    assert res.saving.nano == 0


def test_passthrough_inferences_are_priced_as_billed_in_predictions() -> None:
    extra = make_inference({"cache_read": 1000, "output": 50}, kind=InferenceKind.COMPACTION,
                           inference_id="inf-extra")
    declined = make_inference({"uncached_input": 1000, "output": 0},
                              kind=InferenceKind.FALLBACK_DECLINED, inference_id="inf-decl",
                              billable=False)
    r = req("EX", 0, 0, [SYS, blk("a")], usage(w5=2000))
    att = dataclasses.replace(r.attempts[0], inferences=(declined, extra,
                                                         *r.attempts[0].inferences))
    r = dataclasses.replace(r, attempts=(att,))
    out = BlockReplayer().predict([lane([r])], pricer=P)[0]
    assert out.cost_nano == 2000 * W5 + 100 * 20_000 + 1000 * 200 + 50 * 20_000


def test_lanes_type_is_preserved() -> None:
    ln = lane([req("TY", 0, 0, [SYS], usage(w5=1000))])
    assert isinstance(ln, Lane)

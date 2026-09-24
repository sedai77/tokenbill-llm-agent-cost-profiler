"""``BlockReplayer`` as a ``Replayer`` (SPEC §9.1, §9.7, §3.18): conformance, minimal change,
identity, skipped lanes, repairs, shards and determinism.

Opus 5.5 unit prices (nano per token): uncached 4,000, read 200, 5m write 5,000, 1h write 8,000;
every request bills 100 output tokens (``OUT`` = 2,000,000 nano)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from tests.v2.blocksim.helpers import (
    W5,
    R,
    U,
    blk,
    conversation,
    lane,
    req,
    size,
    system,
    tool,
    usage,
)
from tokenbill.core.builders import FlatRates, lane_from_table
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence
from tokenbill.core.policy import parse_policy
from tokenbill.core.records import to_json
from tokenbill.core.shards import merge_replay
from tokenbill.core.testing import FakePricer, assert_replayer_conforms
from tokenbill.core.types import Policy
from tokenbill.sim.block_replay import (
    BLOCK_REPAIRS,
    BlockReplayer,
    lane_skip_reason,
    read_agreement,
)

P = FakePricer()
RT = RulesTable()
OUT = 100 * 20_000


def replay(lanes, spec: str, **kw):
    policy = parse_policy(spec) if isinstance(spec, str) else spec
    return BlockReplayer().replay(list(lanes), policy, mode=kw.pop("mode", "documented"),
                                  pricer=kw.pop("pricer", P), rules=RT, calibration=None,
                                  keep_outcomes=kw.pop("keep_outcomes", True), **kw)


def volatile_lane(key: str = "L", team: str | None = None):
    msgs = [blk(f"m{i}") for i in range(3)]
    reqs = []
    for i in range(3):
        blocks = [system(f"{key}-sys{i}", 2000, norm="sys", volatile=("iso_datetime",))]
        blocks += msgs[: i + 1]
        reqs.append(req(key, i, 30 * i, blocks, usage(w5=size(blocks)),
                        attribution={"team": team} if team else None))
    return lane(reqs)


@pytest.mark.parametrize("pricer", [FakePricer(), FlatRates()])
def test_conforms_to_the_replayer_contract(pricer) -> None:
    assert_replayer_conforms(BlockReplayer(), pricer)


def test_observed_policy_is_the_billed_ledger_on_fingerprinted_lanes() -> None:
    ln = volatile_lane()
    for spec in ("", "breakpoints=observed"):
        res = replay([ln], spec)
        assert res.cost == res.baseline and res.saving.nano == 0
        assert res.outcomes is not None and not any(o.changed for o in res.outcomes)
        assert [o.cost_nano for o in res.outcomes] == [15_000_000 + OUT, 20_000_000 + OUT,
                                                       25_000_000 + OUT]
        assert res.lanes_skipped == ()


def test_minimal_change_prices_billed_minus_the_model_delta() -> None:
    res = replay([volatile_lane()], "repair=block:volatile")
    # model(observed): 3 full writes = (3000+4000+5000)*W5 = 60,000,000
    # model(repair):   15,000,000 + (3000*R + 1000*W5) + (4000*R + 1000*W5) = 26,400,000
    assert res.saving.nano == 33_600_000
    assert res.saving.evidence is Evidence.ESTIMATED
    assert res.saving.calibration is Calibration.UNCALIBRATED
    assert res.cost.nano == res.baseline.nano - 33_600_000
    costs = [o.cost_nano for o in res.outcomes]
    assert costs == [15_000_000 + OUT, 3000 * R + 1000 * W5 + OUT, 4000 * R + 1000 * W5 + OUT]
    assert [o.changed for o in res.outcomes] == [False, True, True]
    usage1 = res.outcomes[1].usage
    assert (usage1.cache_read, usage1.cache_write_5m, usage1.uncached_input) == (3000, 1000, 0)
    assert dict(res.per_lane)["L"] == res.cost.nano


def test_model_error_cancels_when_billing_disagrees_with_the_model() -> None:
    # billed all uncached although a breakpoint is present (like the v0.1 timestamp demo)
    msgs = [blk(f"m{i}") for i in range(2)]
    reqs = []
    for i in range(2):
        blocks = [system(f"e-sys{i}", 2000, norm="esys", volatile=("iso_date",))]
        blocks += msgs[: i + 1]
        reqs.append(req("E", i, 30 * i, blocks, usage(u=size(blocks))))
    res = replay([lane(reqs)], "repair=block:volatile")
    # observed model: writes 3000, 4000 (35,000,000); repair: 15,000,000 + 3000*R + 1000*W5
    assert res.saving.nano == 35_000_000 - (15_000_000 + 3000 * R + 1000 * W5)
    o = res.outcomes[1]
    # billed + (policy − observed) goes negative on reads' complement → the policy prediction
    assert (o.usage.cache_read, o.usage.cache_write_5m, o.usage.uncached_input) == (3000, 1000, 0)
    assert o.cost_nano == 4000 * U + OUT - (4000 * W5 - (3000 * R + 1000 * W5))


def test_outcome_usage_is_billed_shifted_by_the_model_delta_when_possible() -> None:
    msgs = [blk(f"m{i}") for i in range(2)]
    reqs = []
    billed = [usage(w5=3000), usage(r=500, w5=3500)]       # request 1: a partial billed read
    for i in range(2):
        blocks = [system(f"L-sys{i}", 2000, norm="sys", volatile=("iso_datetime",))]
        reqs.append(req("L", i, 30 * i, blocks + msgs[: i + 1], billed[i]))
    res = replay([lane(reqs)], "repair=block:volatile")
    o = res.outcomes[1]
    # model: observed (R 0, W 4000) → repaired (R 3000, W 1000); billed (500, 3500) shifts by it
    assert (o.usage.cache_read, o.usage.cache_write_5m, o.usage.uncached_input) == (3500, 500, 0)
    assert o.cost_nano == 500 * R + 3500 * W5 + OUT - (4000 * W5 - (3000 * R + 1000 * W5))
    same = conversation("C", [system("csys", 2000)], [[blk("c0")], [blk("c1")]], billed="cold")
    res2 = replay([lane(same)], "repair=block:volatile;breakpoints=end")
    assert res2.saving.nano == 0 and not any(o.changed for o in res2.outcomes)


def test_lanes_without_fingerprints_are_skipped_with_a_reason() -> None:
    plain = lane_from_table([(0, 0, 3000, 0, 0, 100), (30, 3000, 1000, 0, 0, 100)],
                            lane_key="plain")
    res = replay([volatile_lane(), plain], "repair=block:volatile")
    assert res.lanes_skipped == (("plain", "no fingerprints (block replay needs the fingerprint "
                                           "tier)"),)
    by_id = {o.request_id: o for o in res.outcomes}
    assert all(not by_id[r.request_id].changed for r in plain.requests)
    assert res.n_lanes == 2 and res.n_requests == 5


def test_partial_fingerprints_skip_the_lane() -> None:
    ln = volatile_lane()
    stripped = dataclasses.replace(ln.requests[1], fingerprint=None)
    partial = dataclasses.replace(ln, requests=(ln.requests[0], stripped, ln.requests[2]))
    assert lane_skip_reason(partial) == "fingerprints missing on 1 of 3 requests"
    res = replay([partial], "repair=block:volatile")
    assert res.saving.nano == 0 and res.lanes_skipped[0][0] == "L"


def test_mixed_billing_classes_raise() -> None:
    allowance = lane([req("A", 0, 0, [system("x")], usage(w5=2000),
                          billing_path="subscription")])
    with pytest.raises(UsageError):
        replay([volatile_lane(), allowance], "breakpoints=end")


def test_allowance_lanes_replay_on_the_list_equivalent_basis() -> None:
    reqs = []
    for i in range(3):
        blocks = [system(f"a-sys{i}", 2000, norm="asys", volatile=("uuid",))]
        blocks += [blk(f"am{j}") for j in range(i + 1)]
        reqs.append(req("AL", i, 30 * i, blocks, usage(w5=size(blocks)),
                        billing_path="subscription"))
    res = replay([lane(reqs)], "repair=block:volatile")
    assert res.baseline.basis is Basis.LIST_EQUIVALENT
    assert res.saving.basis is Basis.LIST_EQUIVALENT and res.saving.nano == 33_600_000


def test_invalid_mode_placement_and_repair_raise() -> None:
    ln = volatile_lane()
    with pytest.raises(UsageError):
        replay([ln], "breakpoints=end", mode="optimistic")
    with pytest.raises(UsageError):
        replay([ln], Policy(name="x", breakpoint_policy="middle"))
    with pytest.raises(UsageError):
        replay([ln], "repair=block:unknown_fix")


def test_calibrated_mode_falls_back_to_documented() -> None:
    res = replay([volatile_lane()], "repair=block:volatile", mode="calibrated")
    assert res.mode == "documented"
    assert any("no calibrated mode" in a for a in res.assumptions)


def test_usage_level_clauses_are_listed_as_ignored() -> None:
    res = replay([volatile_lane()], "ttl=1h;fast=off;repair=restore_caching;breakpoints=end")
    line = next(a for a in res.assumptions if a.startswith("block replay ignores"))
    assert "ttl" in line and "fast" in line and "repair" in line


def test_a_non_block_policy_changes_nothing() -> None:
    res = replay([volatile_lane()], "ttl=1h")
    assert res.cost == res.baseline and res.saving.nano == 0
    assert res.calibration is Calibration.NA


def test_replay_is_deterministic_and_shard_invariant() -> None:
    lanes = [volatile_lane("A", team="t1"), volatile_lane("B", team="t2"),
             volatile_lane("C", team="t1")]
    spec = "repair=block:volatile;breakpoints=static_plus_end"
    whole = replay(lanes, spec)
    again = replay(list(reversed(lanes)), spec)
    assert json.dumps(to_json(whole), sort_keys=True) == json.dumps(to_json(again),
                                                                    sort_keys=True)
    parts = [replay([ln for ln in lanes if ln.team == team], spec) for team in ("t1", "t2")]
    merged = merge_replay(parts)
    assert merged.saving.nano == whole.saving.nano
    assert merged.cost.nano == whole.cost.nano
    assert sorted(merged.per_lane) == sorted(whole.per_lane)


def test_cache_sharing_is_confined_to_team_and_lane_kind() -> None:
    shared = [system("team-sys", 3000)]
    a = lane([req("TA", 0, 0, shared + [blk("qa")], usage(w5=4000),
                  attribution={"team": "t1"})])
    b = lane([req("TB", 0, 60, shared + [blk("qb")], usage(w5=4000),
                  attribution={"team": "t2"})])
    c = lane([req("TC", 0, 60, shared + [blk("qc")], usage(w5=4000),
                  attribution={"team": "t1"})])
    res_bc = replay([a, b], "breakpoints=static_plus_end")
    res_ac = replay([a, c], "breakpoints=static_plus_end")
    assert res_bc.saving.nano <= 0              # different teams: nothing shared
    assert res_ac.saving.nano == 3000 * (W5 - R) - 0   # c reads the shared system prefix


def test_unpriceable_requests_stay_unchanged() -> None:
    reqs = []
    for i in range(2):
        blocks = [system(f"u-sys{i}", 2000, norm="usys", volatile=("uuid",)), blk("um")]
        reqs.append(req("UP", i, 30 * i, blocks, usage(w5=3000), model="claude-unknown-9"))
    res = replay([lane(reqs)], "repair=block:volatile")
    assert res.baseline.nano is None
    assert any("unpriceable" in a for a in res.assumptions)


def test_every_block_repair_is_accepted() -> None:
    ln = volatile_lane()
    for rep in BLOCK_REPAIRS:
        res = replay([ln], f"repair={rep}")
        assert res.saving.nano is not None and res.lanes_skipped == ()


def test_add_end_prices_a_breakpoint_for_marker_less_requests() -> None:
    reqs = conversation("M", [system("msys", 2000)], [[blk("n0")], [blk("n1")], [blk("n2")]],
                        billed="none", bps=None)
    res = replay([lane(reqs)], "repair=block:add_end")
    # observed: all uncached (3000+4000+5000)*U = 48,000,000
    # repair: 3000*W5 + (3000*R + 1000*W5) + (4000*R + 1000*W5) = 26,400,000
    assert res.saving.nano == 48_000_000 - 26_400_000


def test_drop_unread_keeps_the_tail_write_of_a_multi_request_lane() -> None:
    one_shot = [lane([req(f"O{i}", 0, 60 * i, [system(f"o{i}", 2000)], usage(w5=2000))])
                for i in range(2)]
    res = replay(one_shot, "repair=block:drop_unread")
    assert res.saving.nano == 2 * 2000 * (W5 - U)
    reqs = conversation("T", [system("tsys", 2000)], [[blk("t0")], [blk("t1")]])
    res2 = replay([lane(reqs)], "repair=block:drop_unread")
    assert res2.saving.nano == 0


def test_stagger_lets_concurrent_siblings_read_the_first_entry() -> None:
    lanes = [lane([req(f"S{i}", 0, 0, [system("ssys", 3000)], usage(w5=3000), ttft_ms=900)])
             for i in range(3)]
    res = replay(lanes, "repair=block:stagger")
    assert res.saving.nano == 2 * 3000 * (W5 - R)


def test_tool_superset_adds_the_missing_definitions_at_the_request_scale() -> None:
    t1, t2 = tool("st1", 500), tool("st2", 500)
    sysb = system("ssup", 1000)
    r0 = req("SU", 0, 0, [t1, sysb, blk("u0")], usage(w5=2500))
    r1 = req("SU", 1, 30, [t1, t2, sysb, blk("u0"), blk("u1")], usage(w5=4000))
    res = replay([lane([r0, r1])], "repair=block:tool_superset")
    outs = res.outcomes
    assert outs[0].usage.total_input == 3000                 # 2,500 billed + 500 added
    assert outs[1].usage.cache_read == 3000
    # observed model 2500*W5 + 4000*W5; repair 3000*W5 + 3000*R + 1000*W5
    assert res.saving.nano == 6500 * W5 - (4000 * W5 + 3000 * R)


def test_predict_returns_model_usage_and_omits_skipped_lanes() -> None:
    plain = lane_from_table([(0, 0, 3000, 0, 0, 100)], lane_key="plain")
    outs = BlockReplayer().predict([volatile_lane(), plain], pricer=P)
    assert len(outs) == 3
    assert [o.changed for o in outs] == [False, False, False]
    assert outs[0].cost_nano == 3000 * W5 + OUT


def test_read_agreement() -> None:
    reqs = conversation("W", [system("wsys", 2000)], [[blk("w0")], [blk("w1")]])
    ln = lane(reqs)
    outs = BlockReplayer().predict([ln], pricer=P)
    assert read_agreement(outs, [ln]) == 1
    assert read_agreement(BlockReplayer().predict([volatile_lane()], pricer=P),
                          [volatile_lane()]) is None
    res = replay([ln], "breakpoints=every_15")
    assert any("read agreement 1.000" in a for a in res.assumptions)


def test_drop_unread_survives_the_index_shift_of_tool_superset() -> None:
    # r0 marks the system prompt (1h) and its end (5m); r1 reads r0's end entry, so the 1h
    # system entry is shadowed and never read (write-never-read); r2 adds a tool (subset churn)
    t1, t2, sysb = tool("dt1", 500), tool("dt2", 500), system("dsys", 1000)
    a, b, c = blk("da"), blk("db"), blk("dc")
    r0 = req("DS", 0, 0, [t1, sysb, a], usage(w1=1500, w5=1000), bps=[(1, "1h"), (2, "5m")])
    r1 = req("DS", 1, 30, [t1, sysb, a, b], usage(r=2500, w5=1000))
    r2 = req("DS", 2, 60, [t1, t2, sysb, a, b, c], usage(w5=5000))
    ln = lane([r0, r1, r2])
    W1 = 8000
    # model(observed): r0 1500·W1 + 1000·W5; r1 2500·R + 1000·W5; r2 5000·W5 = 47,500,000
    observed = 1500 * W1 + 1000 * W5 + 2500 * R + 1000 * W5 + 5000 * W5
    # drop only: r0's 1h segment is written at 5m instead
    assert replay([ln], "repair=block:drop_unread").saving.nano == 1500 * (W1 - W5)
    # drop + superset: r0 = [t1, t2, sys, a] (3,000 tokens) keeps only its end breakpoint (the
    # dropped one is ordinal 0 — at index 1 before the shift, index 2 after), r1 reads 3,000 and
    # writes 1,000, r2 reads 4,000 and writes 1,000
    repaired = 3000 * W5 + (3000 * R + 1000 * W5) + (4000 * R + 1000 * W5)
    res = replay([ln], "repair=block:drop_unread;repair=block:tool_superset")
    assert res.saving.nano == observed - repaired == 21_100_000

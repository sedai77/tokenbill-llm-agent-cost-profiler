"""Property and fuzz tests (hypothesis) over random fingerprinted lanes: arbitrary block lists,
markers, timings, parameters and billed usage that may disagree with the model.

Invariants: predicted input sums to the billed ``total_input`` and is never negative; the observed
policy is the identity; every replay is deterministic, shard-invariant and internally consistent
(``cost = baseline − saving``); ``first_divergence`` never raises and is None for a request against
itself; the detector never raises and conforms; only ``TokenbillError`` subclasses may escape."""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.v2.blocksim.helpers import blk, context, lane, req, system, tool
from tokenbill.common import TokenbillError
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.policy import parse_policy
from tokenbill.core.records import LaneKind, UsageBuckets, to_json
from tokenbill.core.shards import merge_replay
from tokenbill.core.testing import FakePricer, assert_detector_conforms
from tokenbill.detect.block import BlockBreakers
from tokenbill.sim.block_replay import BLOCK_REPAIRS, PLACEMENTS, BlockReplayer, first_divergence

P = FakePricer()
RT = RulesTable()
SETTINGS = settings(max_examples=60, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

_TOOLS = [tool(n, 300) for n in ("ta", "tb", "tc")]
_SYSTEMS = [system("sa", 700), system("sb", 700, srt="sa"),
            system("sv1", 700, norm="sv", volatile=("iso_date",)),
            system("sv2", 700, norm="sv", volatile=("iso_date",))]
_MSGS = [blk(f"m{i}", 200 + 50 * i) for i in range(6)] + \
        [blk(f"r{i}", 120, kind="tool_result") for i in range(3)] + \
        [blk("u0", 150, kind="tool_use", role="assistant")]


@st.composite
def requests(draw, key: str, team: str | None, kind: LaneKind, t0: int):
    n = draw(st.integers(1, 5))
    model = draw(st.sampled_from(["claude-opus-5-5", "claude-sonnet-5"]))
    product = draw(st.sampled_from([None, "agent_sdk", "claude_code"]))
    out = []
    ts = t0
    for i in range(n):
        tools = draw(st.lists(st.sampled_from(_TOOLS), max_size=3, unique=True))
        sys_blocks = draw(st.lists(st.sampled_from(_SYSTEMS), max_size=2, unique=True))
        msgs = draw(st.lists(st.sampled_from(_MSGS), min_size=0, max_size=12))
        blocks = [*tools, *sys_blocks, *msgs]
        if not blocks:
            blocks = [_MSGS[0]]
        idx = draw(st.lists(st.integers(0, len(blocks) - 1), max_size=5, unique=True))
        bps = [(j, draw(st.sampled_from(["5m", "1h", "30m"]))) for j in sorted(idx)]
        total = draw(st.integers(0, 12_000))
        r = draw(st.integers(0, total))
        w5 = draw(st.integers(0, total - r))
        w1 = draw(st.integers(0, total - r - w5))
        billed = UsageBuckets(cache_read=r, cache_write_5m=w5, cache_write_1h=w1,
                              uncached_input=total - r - w5 - w1, output=draw(st.integers(0, 50)))
        ts += draw(st.integers(0, 700_000))
        out.append(req(key, i, ts / 1000, blocks, billed, model=model, bps=bps or None,
                       auto=draw(st.sampled_from([None, True])),
                       ttft_ms=draw(st.sampled_from([None, 0, 800, 3000])),
                       params={"effort": draw(st.sampled_from([None, "high", "low"])),
                               "tool_choice": draw(st.sampled_from([None, "auto", "any"]))},
                       attribution={"team": team, "agent_product": product,
                                    "client_version": "2.1.270"}))
    return out


@st.composite
def lane_sets(draw):
    n = draw(st.integers(1, 3))
    lanes = []
    for i in range(n):
        team = draw(st.sampled_from([None, "a", "b"]))
        kind = draw(st.sampled_from([LaneKind.API_RUN, LaneKind.MAIN]))
        scope = draw(st.sampled_from(["ws:x", "ws:y", "unknown"]))
        reqs = draw(requests(f"P{i}", team, kind, draw(st.integers(0, 60_000))))
        lanes.append(lane(reqs, kind=kind, scope=scope))
    return lanes


policies = st.builds(
    lambda placement, repairs: ";".join(
        [f"repair={r}" for r in sorted(repairs)]
        + ([f"breakpoints={placement}"] if placement != "observed" else [])),
    st.sampled_from(PLACEMENTS), st.frozensets(st.sampled_from(BLOCK_REPAIRS), max_size=3))


def _replay(lanes, spec):
    return BlockReplayer().replay(lanes, parse_policy(spec), mode="documented", pricer=P,
                                  rules=RT, calibration=None, keep_outcomes=True)


@SETTINGS
@given(lane_sets(), st.sampled_from(PLACEMENTS))
def test_predicted_usage_sums_to_the_billed_input(lanes, placement) -> None:
    billed = {r.request_id: r.serving_inference.usage.total_input
              for ln in lanes for r in ln.requests}
    for o in BlockReplayer().predict(lanes, pricer=P, placement=placement):
        u = o.usage
        assert min(u.cache_read, u.cache_write_5m, u.cache_write_1h, u.cache_write_other,
                   u.uncached_input) >= 0
        assert u.total_input == billed[o.request_id]


@SETTINGS
@given(lane_sets())
def test_observed_policy_is_the_identity(lanes) -> None:
    res = _replay(lanes, "")
    assert res.cost == res.baseline and res.saving.nano == 0
    assert not any(o.changed for o in res.outcomes)


@SETTINGS
@given(lane_sets(), policies)
def test_replays_are_consistent_deterministic_and_shard_invariant(lanes, spec) -> None:
    res = _replay(lanes, spec)
    assert res.cost.nano == res.baseline.nano - res.saving.nano
    assert sum(point for _key, point in res.per_lane) == res.cost.nano
    assert sum(o.cost_nano for o in res.outcomes) == res.cost.nano
    again = _replay(list(reversed(lanes)), spec)
    assert json.dumps(to_json(res), sort_keys=True) == json.dumps(to_json(again),
                                                                  sort_keys=True)
    groups: dict[tuple, list] = {}
    for ln in lanes:
        groups.setdefault((ln.team or "", ln.kind.value), []).append(ln)
    merged = merge_replay([_replay(members, spec) for _k, members in sorted(groups.items())])
    assert merged.saving.nano == res.saving.nano and merged.cost.nano == res.cost.nano


@SETTINGS
@given(lane_sets())
def test_first_divergence_never_raises(lanes) -> None:
    reqs = [r for ln in lanes for r in ln.requests]
    for a in reqs:
        assert first_divergence(a, a) is None
        for b in reqs:
            got = first_divergence(a, b)
            if got is not None:
                tier, index, cause = got
                assert tier in ("tools", "system", "messages") and index >= 0 and cause


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(lane_sets())
def test_the_detector_never_raises_and_conforms(lanes) -> None:
    findings = assert_detector_conforms(BlockBreakers(), lanes, context(min_usd="0"))
    for f in findings:
        assert f.recoverable is None or f.recoverable.nano is None or f.recoverable.nano >= 0


@SETTINGS
@given(lane_sets(), st.text(max_size=12))
def test_only_usage_errors_escape_for_bad_policies(lanes, junk) -> None:
    from tokenbill.core.types import Policy
    try:
        BlockReplayer().replay(lanes, Policy(name="x", breakpoint_policy=junk or None,
                                             repairs=(f"block:{junk}",) if junk else ()),
                               mode="documented", pricer=P, rules=RT, calibration=None)
    except TokenbillError:
        pass

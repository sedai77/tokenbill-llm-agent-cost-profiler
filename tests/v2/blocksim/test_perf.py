"""Performance budget (SPEC §17): block-level replay is O(new blocks + 4 breakpoints × 20
positions) per request; a 4,000-call run replays in ≤ 2 s (v0.1: 6.3 s, quadratic).

The run is the worst case for a quadratic engine: one conversation whose context grows by one
block per call (4,002 blocks at the end), a breakpoint on the newest block, working-cache billing,
delta-shared blocks as a trace@2 reader produces them. The replay prices the baseline, simulates
the observed placement and the policy placement. The full-size budget is a nightly ``perf`` test;
the PR variant runs a tenth of the calls against a tenth of the budget.
"""

from __future__ import annotations

import time

import pytest

from tests.v2.blocksim.helpers import blk, lane, req, size, system, tool, usage
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.policy import parse_policy
from tokenbill.core.testing import FakePricer
from tokenbill.sim.block_replay import BlockReplayer


def growing_run(n_calls: int):
    blocks = (tool("perf-tool", 300), system("perf-sys", 1200))
    reqs = []
    prev_total = 0
    for i in range(n_calls):
        blocks = (*blocks, blk(f"perf-{i}", 90))
        total = size(blocks)
        reqs.append(req("perf", i, i, blocks, usage(r=prev_total, w5=total - prev_total),
                        model="claude-sonnet-5"))
        prev_total = total
    return lane(reqs)


def _time_replay(ln, spec: str) -> float:
    replayer, pricer, rules = BlockReplayer(), FakePricer(), RulesTable()
    start = time.perf_counter()
    res = replayer.replay([ln], parse_policy(spec), mode="documented", pricer=pricer,
                          rules=rules, calibration=None)
    elapsed = time.perf_counter() - start
    assert res.n_requests == len(ln.requests)
    return elapsed


@pytest.mark.perf
def test_a_4000_call_run_replays_within_two_seconds() -> None:
    ln = growing_run(4000)
    assert _time_replay(ln, "breakpoints=every_15") <= 2.0


def test_a_400_call_run_replays_within_a_tenth_of_the_budget() -> None:
    ln = growing_run(400)
    best = min(_time_replay(ln, "breakpoints=every_15") for _ in range(3))
    assert best <= 0.2

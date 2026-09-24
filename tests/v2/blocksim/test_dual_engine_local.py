"""Dual-engine agreement on the four v0.1 demo scenarios (SPEC §10.4), with the lanes built by
this area's §5.5/§5.8 bridge (:func:`tests.v2.blocksim.fp.calls_to_lane`). The same checks run
through TRACE's real ``TraceV1Adapter`` in ``test_gate_dual_engine.py`` at merge gate 1."""

from __future__ import annotations

import pytest

from tests.v2.blocksim import dual
from tests.v2.blocksim.fp import calls_to_lane
from tokenbill.demo_traces import scenario

CHECKS = {
    "timestamp": dual.check_timestamp,
    "tool-churn": dual.check_tool_churn,
    "no-cache": dual.check_no_cache,
    "well-behaved": dual.check_well_behaved,
}


@pytest.mark.parametrize("name", sorted(CHECKS))
@pytest.mark.parametrize("seed", [7, 11])
def test_demo_scenarios_agree_with_the_v01_engine(name: str, seed: int) -> None:
    calls = scenario(name, seed)
    CHECKS[name](calls_to_lane(calls), calls)


def test_assumed_breakpoints_come_from_the_trace1_count() -> None:
    lanes = {name: calls_to_lane(scenario(name)) for name in CHECKS}
    for name, lane in lanes.items():
        marks = [bp for r in lane.requests for bp in r.params.breakpoints]
        if name == "no-cache":
            assert marks == []
        else:
            assert len(marks) == len(lane.requests) and all(bp.assumed for bp in marks)


def test_effort_exemption_agrees_with_core_transitions() -> None:
    dual.check_effort_agreement(calls_to_lane(scenario("well-behaved")))

"""Merge-gate 1 test (PLAN §1.5, SPEC §10.4): dual-engine agreement through TRACE's real
``TraceV1Adapter`` in the fingerprint tier.

The four v0.1 demo scenarios are written with the frozen ``trace.write_trace``, read back by the
adapter (a positive ``cache_breakpoints`` count without marker positions becomes one assumed
end-of-messages breakpoint, §5.5), grouped into lanes with ``core.lanes.group_lanes`` and checked
with the same assertions as ``test_dual_engine_local.py``: ``timestamp`` → exactly
``volatile-system`` at call 1; ``tool-churn`` → exactly ``tool-churn`` (order) at the rotation
calls; ``no-cache`` → exactly ``missing-breakpoint``; ``well-behaved`` → none, with
``predict(placement="end")`` reads equal to v1's optimal-cache reads within rounding; and the
effort exemption agrees with ``core.transitions`` on the same lanes. Skipped until
``tokenbill.adapters.trace_v1`` exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.v2.blocksim import dual
from tokenbill.core.ids import key_id
from tokenbill.core.lanes import group_lanes
from tokenbill.core.records import ContentTier, Lane
from tokenbill.core.types import IngestOptions
from tokenbill.demo_traces import scenario
from tokenbill.trace import write_trace

pytestmark = pytest.mark.gate

trace_v1 = pytest.importorskip("tokenbill.adapters.trace_v1")

KEY = b"tokenbill-blocksim-gate-key-0001"
CHECKS = {
    "timestamp": dual.check_timestamp,
    "tool-churn": dual.check_tool_churn,
    "no-cache": dual.check_no_cache,
    "well-behaved": dual.check_well_behaved,
}


def _read(path: Path) -> list[Lane]:
    opts = IngestOptions(content_tier=ContentTier.FINGERPRINT, name_key=KEY,
                         name_key_id=key_id(KEY))
    result = trace_v1.TraceV1Adapter().read(path, opts)
    return group_lanes(result.requests, result.events, result.sessions)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Lane, list[Any]]]:
    out = {}
    for name in CHECKS:
        calls = scenario(name)
        path = tmp_path_factory.mktemp("demo") / f"{name}.jsonl"
        write_trace(path, calls)
        lanes = _read(path)
        assert len(lanes) == 1, f"{name}: one API_RUN lane per run (§5.5)"
        lane = lanes[0]
        missing = [r.request_id for r in lane.requests if r.fingerprint is None]
        assert not missing, f"{name}: TraceV1Adapter produced no fingerprint (fingerprint tier)"
        out[name] = (lane, calls)
    return out


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_demo_scenarios_agree_through_the_trace1_adapter(demo, name: str) -> None:
    lane, calls = demo[name]
    CHECKS[name](lane, calls)


def test_breakpoint_count_mapping(demo) -> None:
    for name, (lane, _calls) in demo.items():
        marks = [bp for r in lane.requests for bp in r.params.breakpoints]
        if name == "no-cache":
            assert marks == []
        else:
            assert marks and all(bp.assumed for bp in marks)


def test_effort_exemption_agrees_with_core_transitions(demo) -> None:
    lane, _calls = demo["well-behaved"]
    dual.check_effort_agreement(lane)

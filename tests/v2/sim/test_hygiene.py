"""Hygiene of the REPLAY modules: no floats in the engine or the gate (money and gate metrics are
int / Fraction / Decimal), and no content canary in any output (SPEC §2.4, §8.8, §21 #5)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.records import (
    Attribution,
    CacheDiagnostic,
    LaneKind,
    RequestParams,
    to_json,
)
from tokenbill.sim.calibrate import calibrate_lanes

from .helpers import PRICER, RULES, a1_lane, replay, table

REPO = Path(__file__).resolve().parents[3]


def _float_uses(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id == "float":
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("module", ["usage_replay", "calibrate"])
def test_no_float_in_replay_modules(module: str) -> None:
    source = (REPO / "tokenbill" / "sim" / f"{module}.py").read_text(encoding="utf-8")
    assert _float_uses(source) == []


def test_the_float_detector_works() -> None:
    assert _float_uses("x = 0.5\ny = float('1')\n") == [1, 2]


def test_no_canary_in_outputs() -> None:
    """Planted canaries in every free-text-capable field never reach a result or a report."""
    attr = Attribution(agent_product="agent_sdk", agent_type=f"agent {CANARY}",
                       project=f"proj {CANARY}", cost_center=CANARY, entrypoint=CANARY,
                       skill=CANARY, team="t1", workload_class="ci")
    params = RequestParams(model_requested="claude-opus-5-5", output_format=f"fmt {CANARY}",
                           context_management=CANARY, advisor_model=None)
    diag = CacheDiagnostic(reason="messages_changed", provider_reason=f"why {CANARY}",
                           missed_input_tokens_estimate=10, source="anthropic.cache_diagnostics")
    rows = [(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500),
            (440, 102_000, 2_000, 0, 0, 500)]
    lane = table(rows, attribution=attr, params=params, kind=LaneKind.MAIN,
                 diagnostics=diag)
    for spec in ("ttl=1h", "keepalive=240s,max=3600s", "compact-window=150000,post=9000",
                 "cold-resume=compact,min=50000;repair=stagger_fanout;repair=shared_ci_prefix",
                 "model=claude-sonnet-5;effort=low;fast=off;geo=global;batch=eligible"):
        res = replay([lane, a1_lane(lane_key="other")], spec)
        assert_no_canary(json.dumps(to_json(res)))
    report = calibrate_lanes([lane], pricer=PRICER, rules=RULES)
    assert_no_canary(json.dumps(to_json(report)))

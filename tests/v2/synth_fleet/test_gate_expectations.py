"""Merge gate 1: the fleet-level expectations of SPEC §18 that are properties of the generated data
rather than of one plant — the agents' Agent SDK lanes plant no block-level breaker (BLOCK), and the
healthy control team yields no finding with recoverable ≥ ``min_usd`` under every installed
detector (the false-positive gate of §10.1). Each test skips until its packages are merged."""

from __future__ import annotations

import datetime as _dt

import pytest

from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.registry import BUILTIN_DETECTORS, all_detectors
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import AnalysisContext
from tokenbill.synth.fleet import FleetWorld

pytestmark = pytest.mark.gate

MIN_USD_NANO = 10**9      # the default min_usd ($1.00, SPEC §10.1)


def _ctx(world: FleetWorld, replayer: object | None) -> AnalysisContext:
    today = (_dt.date.fromisoformat(world.today) - _dt.date(1970, 1, 1)).days * 86_400_000
    return AnalysisContext(pricer=FakePricer(), rules=RulesTable(), replayer=replayer,  # type: ignore[arg-type]
                           calibration=None, window=(world.since_ms, world.until_ms),
                           capabilities=world.ingest_result().capabilities, now_ms=today)


def test_agents_plant_no_block_breaker(world: FleetWorld) -> None:
    block = pytest.importorskip("tokenbill.detect.block")
    expectation = dict(world.truth.expectations)["agents"]
    assert "block.breakers" in expectation
    findings = block.BlockBreakers().detect(world.lanes("agents"), _ctx(world, None))
    assert findings == [], [(f.kind, f.recoverable) for f in findings]


def test_control_team_is_clean_under_every_installed_detector(world: FleetWorld) -> None:
    replay = pytest.importorskip("tokenbill.sim.usage_replay")
    usage_level = [d for d in all_detectors()
                   if d.id in BUILTIN_DETECTORS and not d.id.startswith(("block.", "aggregate."))]
    if not usage_level:
        pytest.skip("no usage-level detector installed yet (DETECT-CACHE / DETECT-OTHER)")
    ctx = _ctx(world, replay.UsageReplayer())
    lanes = world.lanes(world.truth.control_team)
    for detector in usage_level:
        for finding in detector.detect(lanes, ctx):
            rec = finding.recoverable
            assert rec is None or rec.nano is None or rec.nano < MIN_USD_NANO, (
                detector.id, finding.kind, rec)

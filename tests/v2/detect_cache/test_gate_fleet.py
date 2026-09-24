"""Gate (merge gate 1): on ``synth.fleet.generate()`` the cache plants (payments 1h TTL,
platform no-cache, mobile cold resumes in both billing classes, agents keepalive and edit churn)
are recovered within their ``FleetTruth`` tolerances by the real ``UsageReplayer``, the core
control team produces no cache finding, and the findings publish with k-anonymity."""

from __future__ import annotations

from typing import Any

import pytest

from tokenbill.core import kanon
from tokenbill.core.builders import assert_no_canary
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.findings import min_usd_nano
from tokenbill.core.records import to_json
from tokenbill.core.registry import run_detectors
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import AnalysisContext, Finding

from .helpers import ALL_DETECTORS

pytestmark = pytest.mark.gate
fleet = pytest.importorskip("tokenbill.synth.fleet")
usage_replay = pytest.importorskip("tokenbill.sim.usage_replay")

CAPS = frozenset({"usage_sequence", "timing", "ttl_split", "iterations", "attempts", "appended",
                  "events", "human_prompts", "lanes_exact", "params", "blocks",
                  "attribution.team", "workload"})
#: (team, detector id, kind) of the cache plants of SPEC §18 this package must recover.
EXPECTED_PLANTS = {
    ("payments", "cache.ttl-advisor", "ttl-1h-recommended"),
    ("platform", "cache.gateway-disabled", "no-cache"),
    ("mobile", "cache.cold-resume", "cold-resume"),
    ("agents", "cache.ttl-advisor", "keepalive-recommended"),
    ("agents", "cache.rebuild", "edit-churn"),
}


@pytest.fixture(scope="module")
def run() -> tuple[Any, list[Finding], AnalysisContext]:
    world = fleet.generate()
    lanes = world.lanes()
    ctx = AnalysisContext(pricer=FakePricer(), rules=RulesTable(),
                          replayer=usage_replay.UsageReplayer(), calibration=None,
                          window=(world.since_ms, world.until_ms), capabilities=CAPS,
                          now_ms=world.until_ms)
    return world, run_detectors(lanes, ctx, only=list(ALL_DETECTORS)), ctx


def _cache_plants(world: Any) -> list[Any]:
    return [p for p in world.truth.plants if p.detector_id.startswith("cache.")]


def test_every_expected_plant_exists(run: Any) -> None:
    world, _findings, _ctx = run
    got = {(p.team, p.detector_id, p.kind) for p in _cache_plants(world)}
    assert got >= EXPECTED_PLANTS


def test_cache_plants_recovered_within_tolerance(run: Any) -> None:
    world, findings, _ctx = run
    plants = _cache_plants(world)
    assert plants
    for plant in plants:
        matches = [f for f in findings if f.detector_id == plant.detector_id
                   and f.kind == plant.kind and f.scope.dims == tuple(plant.scope)]
        assert len(matches) == 1, (plant.plant_id, [(f.kind, f.scope.dims) for f in findings])
        f = matches[0]
        assert f.cost_observed.basis.value == plant.basis, plant.plant_id
        for figure, fig in (("cost_observed", f.cost_observed), ("recoverable", f.recoverable)):
            if plant.tolerance(figure) is None:
                continue
            assert fig is not None and fig.nano is not None, (plant.plant_id, figure)
            assert plant.within(figure, fig.nano, low=fig.low_nano, high=fig.high_nano), (
                plant.plant_id, figure, fig.nano, plant.expected(figure))


def test_control_team_is_clean(run: Any) -> None:
    world, findings, ctx = run
    control = world.truth.control_team
    assert not [f for f in findings if dict(f.scope.dims).get("team") == control]
    floor = min_usd_nano(ctx)
    assert not [f for f in findings if dict(f.scope.dims).get("team") == control
                and f.recoverable is not None and (f.recoverable.nano or 0) >= floor]


def test_publication_and_canary(run: Any) -> None:
    _world, findings, _ctx = run
    published = kanon.rescope_findings(findings, k=5)
    for f in published:
        assert f.n_users >= 5
        assert_no_canary(repr(f), str(to_json(f)))

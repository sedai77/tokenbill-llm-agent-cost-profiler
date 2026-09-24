"""Gate (merge gate 1): on ``synth.fleet.generate()`` the DETECT-OTHER plants of the search, infra,
data, ops, ci-bots and agents teams are recovered within their ``FleetTruth`` tolerances by the
real ``UsageReplayer`` — in their exact scopes — the core control team produces no finding with a
recoverable ≥ ``min_usd``, and the findings publish with k-anonymity and without the canary."""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from typing import Any

import pytest

from tokenbill.core import kanon
from tokenbill.core.builders import assert_no_canary
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.findings import min_usd_nano
from tokenbill.core.records import to_json
from tokenbill.core.registry import run_detectors
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import AnalysisContext, Finding, Scope
from tokenbill.detect.premium import StickyEscalation

from .helpers import ALL_DETECTORS

pytestmark = pytest.mark.gate
fleet = pytest.importorskip("tokenbill.synth.fleet")
usage_replay = pytest.importorskip("tokenbill.sim.usage_replay")

#: (team, detector id, kind) of the SPEC §18 plants this package must recover.
EXPECTED_PLANTS = {
    ("search", "context.size-tax", "context-tax"),
    ("search", "context.compaction-window", "compaction-window"),
    ("infra", "premium.modifiers", "fast-premium"),
    ("infra", "premium.sticky-escalation", "sticky-escalation"),
    ("data", "model.routing", "delegation-routing"),
    ("data", "model.routing", "same-tier-upgrade"),
    ("data", "model.routing", "default-model"),
    ("data", "model.routing", "default-effort"),
    ("data", "model.routing", "rebaseline"),
    ("ops", "premium.modifiers", "regional-premium"),
    ("ops", "tail.runaway", "runaway-session"),
    ("ci-bots", "failure.path", "max-tokens-truncation"),
    ("ci-bots", "automation", "ci-cross-run"),
    ("ci-bots", "automation", "batch-eligible"),
    ("ci-bots", "automation", "ci-run-cost"),
    ("agents", "context.static-prefix", "tool-defs-bloat"),
}


@pytest.fixture(scope="module")
def run() -> tuple[Any, list[Finding], AnalysisContext]:
    world = fleet.generate()
    ctx = AnalysisContext(pricer=FakePricer(), rules=RulesTable(),
                          replayer=usage_replay.UsageReplayer(), calibration=None,
                          window=(world.since_ms, world.until_ms),
                          capabilities=world.ingest_result().capabilities,
                          now_ms=world.until_ms)
    return world, run_detectors(world.lanes(), ctx, only=list(ALL_DETECTORS)), ctx


def _plants(world: Any) -> list[Any]:
    return [p for p in world.truth.plants if p.detector_id in ALL_DETECTORS]


def _match(findings: list[Finding], plant: Any) -> Finding:
    found = [f for f in findings if f.detector_id == plant.detector_id and f.kind == plant.kind
             and f.scope.dims == tuple(plant.scope)]
    assert len(found) == 1, (plant.plant_id, [(f.kind, f.scope.dims) for f in findings
                                              if f.detector_id == plant.detector_id])
    return found[0]


def _evidence(f: Finding, ref: str) -> dict[str, Any]:
    for item in f.evidence:
        if item.ref == ref:
            return dict(item.attrs)
    raise AssertionError((f.kind, ref))


def test_every_expected_plant_exists(run: Any) -> None:
    world, _findings, _ctx = run
    assert {(p.team, p.detector_id, p.kind) for p in _plants(world)} >= EXPECTED_PLANTS


def test_plants_recovered_within_tolerance(run: Any) -> None:
    world, findings, _ctx = run
    checked = 0
    for plant in _plants(world):
        if plant.kind == "sticky-escalation":
            continue            # self findings: see the next test
        f = _match(findings, plant)
        assert f.cost_observed.basis.value == plant.basis, plant.plant_id
        if plant.kind == "compaction-window":
            # SPEC §18: "400k point = truth (±5%)": the curve point in the evidence; the
            # recommendation is the best eligible window (w ≥ 300k, ≤ 3 extra compactions)
            window = int(dict(plant.details)["window"])
            point = _evidence(f, f"compaction-window:{window // 1000}k")["saving_nano"]
            assert plant.within("recoverable", point), (plant.plant_id, point)
            assert f.recoverable is not None and f.recoverable.nano >= point
            assert f.title.startswith("Allowance headroom:")
            checked += 1
            continue
        if plant.kind == "rebaseline":
            delta = Decimal(str(_evidence(f, "rebaseline:tokens")["delta_output_per_request"]))
            truth = Decimal(plant.detail("delta_output_per_request"))
            assert abs(delta - truth) <= Decimal(plant.tolerance("delta_output_per_request")) * \
                abs(truth), (delta, truth)
            change = _evidence(f, "rebaseline:change")
            assert change["change_date"] == plant.detail("change_date")
            assert (change["from_model"], change["to_model"]) == (
                plant.detail("from_model"), plant.detail("to_model"))
            checked += 1
            continue
        for figure, fig in (("cost_observed", f.cost_observed), ("recoverable", f.recoverable)):
            if plant.tolerance(figure) is None:
                continue
            assert fig is not None and fig.nano is not None, (plant.plant_id, figure)
            assert plant.within(figure, fig.nano, low=fig.low_nano, high=fig.high_nano), (
                plant.plant_id, figure, fig.nano, plant.expected(figure))
        checked += 1
    assert checked == len([p for p in _plants(world) if p.kind != "sticky-escalation"])
    assert checked >= 17


def test_sticky_escalation_self_findings_and_suppressed_org_count(run: Any) -> None:
    world, findings, ctx = run
    plant = next(p for p in _plants(world) if p.kind == "sticky-escalation")
    assert not [f for f in findings if f.kind == "sticky-escalation-count"
                and dict(f.scope.dims).get("team") == plant.team]      # 3 < k
    lanes = world.lanes(plant.team)
    for key, why in plant.details:
        if not key.startswith("self:"):
            continue
        principal = key[len("self:"):]
        mine = StickyEscalation().detect(lanes, dataclasses.replace(ctx,
                                                                    self_principal=principal))
        assert [(f.kind, f.audience, f.scope.dims) for f in mine] == [
            ("sticky-escalation", "self", tuple(plant.scope))], (why, principal)
        assert ("fast" in mine[0].title) == (why == "fast")


def test_control_team_is_clean(run: Any) -> None:
    world, findings, ctx = run
    control = world.truth.control_team
    floor = min_usd_nano(ctx)
    assert not [f for f in findings if dict(f.scope.dims).get("team") == control
                and f.recoverable is not None and (f.recoverable.nano or 0) >= floor]


def test_publication_and_canary(run: Any) -> None:
    world, findings, _ctx = run
    teams: dict[str, set[str]] = {}
    for lane in world.lanes():
        for req in lane.requests:
            if req.attribution.principal:
                teams.setdefault(req.attribution.team or "", set()).add(req.attribution.principal)

    def count_users(scope: Scope) -> int:
        team = dict(scope.dims).get("team")
        if team is None:
            return sum(len(v) for v in teams.values())
        return len(teams.get(team, ()))

    published = kanon.rescope_findings(findings, k=5, count_users=count_users)
    runaway = [f for f in published if f.kind == "runaway-session"]
    assert [f.scope.dims for f in runaway] == [(("team", "ops"),)]
    for f in published:
        assert f.n_users >= 5
        assert_no_canary(repr(f), json.dumps(to_json(f)))
        assert "session" not in dict(f.scope.dims)

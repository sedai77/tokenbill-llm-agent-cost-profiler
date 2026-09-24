"""Merge gate 1 (SPEC §9.8, §18): each replay-based plant truth — computed by SYNTH-FLEET's closed
forms, independently of REPLAY — equals ``synth.oracle.ReferenceReplay`` on the plant's lanes
within 1 nano. Skips until SYNTH-ORACLE is merged.

Policies the oracle does not implement (``regional=global``; ``repair=shared_ci_prefix``, which
carries no truth figure) are covered by exact rate arithmetic in ``test_closed_forms.py``."""

from __future__ import annotations

import pytest

from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.policy import parse_policy
from tokenbill.core.records import Lane
from tokenbill.core.testing import FakePricer
from tokenbill.synth.fleet import FleetWorld

pytestmark = pytest.mark.gate

CHECKED = (
    "platform.no-cache",
    "payments.ttl-1h",
    "search.compaction-window",
    "infra.fast-premium",
    "data.delegation",
    "data.same-tier.main.claude-opus-5",
    "data.same-tier.main.claude-fable-5",
    "data.same-tier.workflow_agent.claude-opus-5",
    "data.default-model",
    "data.default-effort",
    "ci-bots.batch-eligible",
    "agents.keepalive",
)


def test_every_replay_based_plant_is_checked(world: FleetWorld) -> None:
    with_policy = {p.plant_id for p in world.truth.plants
                   if p.policy is not None and p.recoverable_nano is not None}
    assert with_policy - set(CHECKED) == {"ops.regional-premium"}


@pytest.mark.parametrize("plant_id", CHECKED)
def test_plant_truth_equals_reference_replay(world: FleetWorld, lanes: list[Lane],
                                             plant_id: str) -> None:
    oracle = pytest.importorskip("tokenbill.synth.oracle")
    plant = world.truth.plant(plant_id)
    keys = set(plant.lane_keys)
    selected = [lane for lane in lanes if lane.lane_key in keys]
    assert plant.policy is not None and plant.recoverable_nano is not None
    result = oracle.ReferenceReplay().replay(
        selected, parse_policy(plant.policy), mode="documented", pricer=FakePricer(),
        rules=RulesTable(), calibration=None)
    assert result.saving.nano is not None
    assert abs(result.saving.nano - plant.recoverable_nano) <= 1, (
        plant_id, result.saving.nano, plant.recoverable_nano)

"""Merge gate F: the foundation contracts compose on fakes (SPEC §3.18, Appendix G.1).

builders → MemoryStore → FakePricer → ``core.transitions`` → a registered test detector (through
``core.registry.run_detectors``) → FakeReplayer → ``core.kanon`` → ``core.shapley`` → RunResult.
Skipped until F-SEM is merged; ``test_kit_internals.py`` runs the same plumbing on stand-ins.
"""

from __future__ import annotations

import json

import pytest

from tokenbill.core import testing as kit
from tokenbill.core.builders import assert_no_canary
from tokenbill.core.labels import Basis, Evidence, Figure
from tokenbill.core.records import to_json
from tokenbill.core.types import RunResult

pytestmark = pytest.mark.gate

for _module in ("tokenbill.core.transitions", "tokenbill.core.findings", "tokenbill.core.policy",
                "tokenbill.core.shapley", "tokenbill.core.cache_rules"):
    pytest.importorskip(_module)


def _figures(obj):
    if isinstance(obj, Figure):
        yield obj
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            yield from _figures(x)
    elif hasattr(obj, "__dataclass_fields__"):
        for name in obj.__dataclass_fields__:
            yield from _figures(getattr(obj, name))


def _leaves(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


def test_smoke_pipeline_on_fakes() -> None:
    run = kit.smoke_pipeline_on_fakes()
    assert isinstance(run, RunResult) and run.command == "smoke"
    # findings: the registered test detector found the planted TTL expiries of one cohort
    assert len(run.findings) == 1
    f = run.findings[0]
    assert (f.detector_id, f.kind, f.n_users) == ("test.smoke-ttl", "ttl-expiry", 6)
    assert f.cost_observed.evidence is Evidence.EXACT and f.cost_observed.nano > 0
    assert f.recoverable is not None and f.recoverable.evidence is Evidence.ESTIMATED
    assert "miss events: " in run.notes[0] and int(run.notes[0].split(": ")[1]) >= 18
    # plan: Shapley credits add up to the joint saving; standalone values are never summed
    plan = run.action_plan
    assert plan is not None and plan.method == "shapley-exact"
    assert sum(lv.shapley.nano for lv in plan.levers) == plan.joint_saving.nano
    assert all(lv.params for lv in plan.levers)
    # bill: exact on a billed basis; k-anonymous breakdown
    assert run.bill is not None and run.bill.total.exact.is_billed_eligible
    team = dict(run.bill.breakdowns)["team"]
    assert team.k == 5 and all(r.n_users >= 5 for r in team.rows)
    # every figure is labeled; nothing estimated is shown as billed
    for fig in _figures(run):
        assert isinstance(fig.evidence, Evidence) and isinstance(fig.basis, Basis)
        if fig.is_billed_eligible:
            assert fig.evidence is Evidence.EXACT
    encoded = to_json(run)
    assert not any(isinstance(v, float) for v in _leaves(encoded))
    assert_no_canary(json.dumps(encoded))
    assert to_json(kit.smoke_pipeline_on_fakes()) == encoded  # deterministic

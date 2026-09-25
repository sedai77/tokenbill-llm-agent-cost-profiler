"""Gate (merge gate 1): the cache detectors on real Claude Code adapter output — CC's checked-in
transcript fixtures → ``ClaudeCodeAdapter`` → ``MemoryStore`` (priced with ``FakePricer``) →
lanes → every cache detector through ``assert_detector_conforms`` (shard invariance included)
and ``run_detectors``, with REPLAY's ``UsageReplayer`` when it is merged. Structural invariants
only: declared kinds, labels, no content canary, no floats — the part of the F-KIT gate-1 smoke
path (``tests/v2/gates/test_gate1_smoke.py``) that needs no STORE, RATES, RECON or PLAN."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core.builders import assert_no_canary
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.ids import key_id
from tokenbill.core.records import to_json
from tokenbill.core.registry import BUILTIN_DETECTORS, load, run_detectors
from tokenbill.core.testing import FakePricer, MemoryStore, assert_detector_conforms
from tokenbill.core.types import AnalysisContext, IngestOptions

from .helpers import ALL_DETECTORS

pytestmark = pytest.mark.gate
cc_mod = pytest.importorskip("tokenbill.adapters.claude_code")

CC_PROJECTS = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code" / "projects"
ORG_KEY = bytes(range(224, 256))
NAME_KEY = bytes(range(0, 64, 2))
NOW_MS = 20_719 * 86_400_000   # 2026-09-23T00:00Z


def _leaves(obj: Any) -> Any:
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


@pytest.fixture(scope="module")
def world() -> tuple[list[Any], AnalysisContext]:
    files = sorted(CC_PROJECTS.rglob("*.jsonl")) if CC_PROJECTS.is_dir() else []
    if not files:
        pytest.skip("CC transcript fixtures not merged")
    opts = IngestOptions(identity_mode="install", name_key=NAME_KEY,
                         name_key_id=key_id(NAME_KEY), principal_key=ORG_KEY,
                         principal_key_id=key_id(ORG_KEY), now_ms=NOW_MS)
    adapter = cc_mod.ClaudeCodeAdapter()
    pricer = FakePricer()
    store = MemoryStore()
    caps: frozenset[str] = frozenset()
    for path in files:
        result = adapter.read(path, opts)
        caps |= result.capabilities
        store.ingest(result, pricer=pricer)
    lanes = list(store.iter_lanes())
    try:
        from tokenbill.sim.usage_replay import UsageReplayer
        replayer: Any = UsageReplayer()
    except ImportError:   # REPLAY not merged: the replay-free parts still run
        replayer = None
    ctx = AnalysisContext(pricer=pricer, rules=RulesTable(), replayer=replayer, calibration=None,
                          window=(0, 2**53), capabilities=caps, thresholds={"min_usd": "0"},
                          now_ms=NOW_MS)
    return lanes, ctx


def test_every_cache_detector_conforms_on_adapter_output(world: Any) -> None:
    lanes, ctx = world
    assert lanes
    for detector_id in ALL_DETECTORS:
        detector = load(BUILTIN_DETECTORS[detector_id])()
        if not detector.requires <= ctx.capabilities:
            continue
        for f in assert_detector_conforms(detector, lanes, ctx):
            assert f.cost_observed.nano is not None and f.cost_observed.nano >= 0
            assert_no_canary(repr(f), json.dumps(to_json(f), sort_keys=True))


def test_run_detectors_on_adapter_output(world: Any) -> None:
    lanes, ctx = world
    findings = run_detectors(lanes, ctx, only=list(ALL_DETECTORS))
    ids = [f.finding_id for f in findings]
    assert len(ids) == len(set(ids))
    for f in findings:
        assert not any(isinstance(v, float) for v in _leaves(to_json(f)))
        assert_no_canary(repr(f), json.dumps(to_json(f), sort_keys=True))

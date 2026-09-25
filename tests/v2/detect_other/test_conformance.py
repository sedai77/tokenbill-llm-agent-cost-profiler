"""Cross-cutting acceptance: ``assert_detector_conforms`` for all ten classes (declared kinds,
labels, ids, audience, allowance labeling, allowlisted keys, determinism, shard invariance), the
registry paths, ``run_detectors`` missing-capabilities, the per-kind capability notes, the healthy
control, the canary, no floats in code or output, and k-anonymous publication."""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core import catalog, kanon
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis
from tokenbill.core.records import Attribution, RequestParams, to_json
from tokenbill.core.registry import BUILTIN_DETECTORS, load, run_detectors
from tokenbill.core.shards import merge_findings
from tokenbill.core.testing import assert_detector_conforms
from tokenbill.core.types import Finding

from .helpers import (
    ALL_DETECTORS,
    CAPS,
    FLEET_FLOOR,
    FLEET_THRESHOLDS,
    ctx,
    fleet_saving,
    healthy_lanes,
    mixed_fleet,
    table_replayer,
)

REPO = Path(__file__).resolve().parents[3]
MODULES = ("context", "premium", "model", "failure", "automation", "tail")
EXPECTED_KINDS = {
    "context.size-tax": {"context-tax"},
    "context.compaction-window": {"compaction-window"},
    "context.static-prefix": {"static-prefix", "tool-defs-bloat"},
    "attrib.carry": {"tool-output-carry", "config-tax"},
    "premium.modifiers": {"fast-premium", "geo-premium", "regional-premium"},
    "premium.sticky-escalation": {"sticky-escalation-count"},
    "model.routing": {"delegation-routing", "same-tier-upgrade", "default-model",
                      "default-effort", "effort-mix", "rebaseline"},
    "failure.path": {"cold-retry", "retry-storm", "never-succeeding-400", "tool-error-loop",
                     "max-tokens-truncation"},
    "automation": {"ci-cross-run", "scheduled-cadence", "batch-eligible", "ci-run-cost"},
    "tail.runaway": {"runaway-session", "idle-loop"},
}
_FLEET: list[Any] = []


def _fleet() -> list[Any]:
    if not _FLEET:
        _FLEET.extend(mixed_fleet())
    return _FLEET


def _detector(detector_id: str) -> Any:
    return load(BUILTIN_DETECTORS[detector_id])()


def _fleet_ctx(**kw: Any):
    kw.setdefault("thresholds", FLEET_THRESHOLDS)
    return ctx(replayer=table_replayer(fleet_saving), static_prefix_floor=FLEET_FLOOR, **kw)


def _leaves(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


def test_registry_paths() -> None:
    from tokenbill.detect import automation, context, failure, model, premium, tail

    classes = {
        "context.size-tax": context.SizeTax, "context.compaction-window": context.CompactionWindow,
        "context.static-prefix": context.StaticPrefix, "attrib.carry": context.Carry,
        "premium.modifiers": premium.PremiumModifiers,
        "premium.sticky-escalation": premium.StickyEscalation, "model.routing": model.Routing,
        "failure.path": failure.FailurePath, "automation": automation.Automation,
        "tail.runaway": tail.Runaway,
    }
    assert set(classes) == set(ALL_DETECTORS)
    for detector_id, cls in classes.items():
        assert load(BUILTIN_DETECTORS[detector_id]) is cls
        assert cls.id == detector_id and cls.version == "1"
        assert isinstance(cls.requires, frozenset) and isinstance(cls.kinds, tuple)


@pytest.mark.parametrize("detector_id", ALL_DETECTORS)
def test_every_detector_conforms_on_the_mixed_fleet(detector_id: str) -> None:
    findings = assert_detector_conforms(_detector(detector_id), _fleet(), _fleet_ctx())
    assert {f.kind for f in findings} >= EXPECTED_KINDS[detector_id]
    for f in findings:
        for lever_id in f.lever_ids:
            assert f.kind in catalog.lever(lever_id).finding_kinds or f.kind in (
                "compaction-window", "same-tier-upgrade")


@pytest.mark.parametrize("detector_id", ALL_DETECTORS)
def test_conformance_in_self_and_break_glass_views(detector_id: str) -> None:
    det = _detector(detector_id)
    assert_detector_conforms(det, _fleet(), _fleet_ctx(self_principal="r_infra0"))
    assert_detector_conforms(det, _fleet(), _fleet_ctx(break_glass="incident 7"))


def test_self_view_sticky_escalation_and_break_glass_sessions() -> None:
    mine = _detector("premium.sticky-escalation").detect(_fleet(), _fleet_ctx(
        self_principal="r_infra0"))
    assert [(f.kind, f.audience) for f in mine] == [("sticky-escalation", "self")]
    tail = _detector("tail.runaway").detect(_fleet(), _fleet_ctx(break_glass="incident 7"))
    assert sorted((f.kind, dict(f.scope.dims).get("session")) for f in tail) == [
        ("idle-loop", "s_ops_idle"), ("runaway-session", "s_ops_loop")]


def test_team_shards_merge_to_the_full_run() -> None:
    lanes = _fleet()
    full = run_detectors(lanes, _fleet_ctx(), only=list(ALL_DETECTORS))
    teams = sorted({lane.team or "" for lane in lanes})
    parts = [run_detectors([ln for ln in lanes if (ln.team or "") == t], _fleet_ctx(),
                           only=list(ALL_DETECTORS), emit_missing=False) for t in teams]
    merged = merge_findings(parts)
    assert sorted(json.dumps(to_json(f), sort_keys=True) for f in merged) == \
        sorted(json.dumps(to_json(f), sort_keys=True) for f in full)


def test_unmet_requires_yield_exactly_one_missing_capabilities_finding() -> None:
    no_params = _fleet_ctx(capabilities=CAPS - {"params"})
    out = run_detectors(_fleet(), no_params, only=list(ALL_DETECTORS))
    dq = [f for f in out if f.category == "data-quality"]
    assert [(f.detector_id, f.kind, f.scope.dims) for f in dq] == [
        ("premium.sticky-escalation", "missing-capabilities", ())]
    # the pipeline's lane-free call adds one note per kind whose extra capability is missing
    once = run_detectors([], no_params, only=list(ALL_DETECTORS))
    got = sorted((f.detector_id, dict(f.scope.dims).get("kind", "")) for f in once)
    assert got == [("model.routing", "default-effort"), ("model.routing", "effort-mix"),
                   ("premium.sticky-escalation", "")]
    bare = _fleet_ctx(capabilities=frozenset({"usage_sequence"}))
    missing = [f.detector_id for f in run_detectors(_fleet(), bare, only=list(ALL_DETECTORS))
               if f.kind == "missing-capabilities"]
    assert sorted(missing) == sorted(["attrib.carry", "automation", "context.compaction-window",
                                      "failure.path", "premium.sticky-escalation",
                                      "tail.runaway"])
    everything = run_detectors([], _fleet_ctx(), only=list(ALL_DETECTORS))
    assert everything == []


def test_healthy_control_has_no_finding_worth_min_usd() -> None:
    c = ctx(replayer=table_replayer(fleet_saving))
    for detector_id in ALL_DETECTORS:
        for f in _detector(detector_id).detect(healthy_lanes(), c):
            assert f.recoverable is None or f.recoverable.nano is None or \
                f.recoverable.nano < 10**9, (detector_id, f.kind)
    fleet = run_detectors(_fleet(), _fleet_ctx(thresholds={}), only=list(ALL_DETECTORS))
    core = [f for f in fleet if dict(f.scope.dims).get("team") == "core"]
    assert all(f.recoverable is None for f in core)


def _plant(lane: Any) -> Any:
    attr: Attribution = lane.requests[0].attribution
    planted = dataclasses.replace(
        attr, agent_type=f"{attr.agent_type or 'general'} {CANARY}",
        entrypoint=f"{attr.entrypoint or 'cli'} {CANARY}", project=CANARY,
        cost_center=CANARY, skill=CANARY,
        extra=tuple((k, f"{v} {CANARY}") for k, v in attr.extra) + (("department", CANARY),))
    reqs = []
    for req in lane.requests:
        params: RequestParams = req.params
        reqs.append(dataclasses.replace(
            req, attribution=planted,
            params=dataclasses.replace(params, output_format=CANARY)))
    return dataclasses.replace(lane, requests=tuple(reqs))


def test_canary_is_absent_from_every_finding() -> None:
    lanes = [_plant(lane) for lane in _fleet()]
    findings = run_detectors(lanes, _fleet_ctx(), only=list(ALL_DETECTORS))
    assert findings
    for f in findings:
        assert_no_canary(repr(f), json.dumps(to_json(f)))


def test_no_floats_in_code_or_findings() -> None:
    for name in MODULES:
        tree = ast.parse((REPO / "tokenbill" / "detect" / f"{name}.py").read_text("utf-8"))
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), name
            assert not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "float"), name
    for f in run_detectors(_fleet(), _fleet_ctx(), only=list(ALL_DETECTORS)):
        assert not any(isinstance(v, float) for v in _leaves(to_json(f)))


def test_publication_with_k_anonymity() -> None:
    findings = run_detectors(_fleet(), _fleet_ctx(), only=list(ALL_DETECTORS))
    published = kanon.rescope_findings(findings, k=5)
    assert published
    for f in published:
        assert f.audience == "org"
        assert f.n_users >= 5 or f.category == "data-quality"
        if dict(f.scope.dims).get("billing_class") == "allowance":
            assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
            assert "list-equivalent, not invoice dollars" in f.summary


def test_findings_are_sorted_and_titles_short() -> None:
    findings = run_detectors(_fleet(), _fleet_ctx(), only=list(ALL_DETECTORS))
    keys = [(-(f.recoverable.nano or 0) if f.recoverable else 0, f.detector_id, f.finding_id)
            for f in findings]
    assert keys == sorted(keys)
    assert all(isinstance(f, Finding) and len(f.summary) <= 330 for f in findings)


@pytest.mark.parametrize(("key", "value"), [
    ("context.compaction-window.max_extra_compactions", "-1"),
    ("model.routing.default_model_share", "1.5"),
    ("failure.path.retry_share", "-0.1"),
    ("tail.runaway.min_hourly_usd", "-1"),
    ("automation.cadence_cv", "abc"),
    ("premium.sticky-escalation.min_days", "-2"),
    ("attrib.carry.unused", "1"),
])
def test_invalid_thresholds_raise_usage_errors(key: str, value: str) -> None:
    c = _fleet_ctx(thresholds={"min_usd": "0.01", key: value})
    if key == "attrib.carry.unused":
        run_detectors(_fleet(), c, only=list(ALL_DETECTORS))    # unknown keys are ignored
        return
    with pytest.raises(UsageError):
        run_detectors(_fleet(), c, only=list(ALL_DETECTORS))

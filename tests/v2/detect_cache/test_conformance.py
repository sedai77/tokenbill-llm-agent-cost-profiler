"""Cross-cutting acceptance: ``assert_detector_conforms`` for all eight classes (declared kinds,
labels, ids, audience, allowance labeling, allowlisted keys, determinism, shard invariance), the
registry paths, ``run_detectors`` missing-capabilities, the healthy control, the canary, no floats,
and k-anonymous publication of the findings."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from tokenbill.core import catalog, kanon
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import Attribution, LaneKind, RequestParams, to_json
from tokenbill.core.registry import BUILTIN_DETECTORS, load, run_detectors
from tokenbill.core.shards import merge_findings
from tokenbill.core.testing import assert_detector_conforms
from tokenbill.core.types import Finding

from .helpers import (
    ALL_DETECTORS,
    CAPS,
    ctx,
    fleet_saving,
    fn_replayer,
    healthy_lanes,
    lane,
    mixed_fleet,
)

MIN_1C = {"min_usd": "0.01", "policy.ttl.ops": "1h"}
EXPECTED_KINDS = {
    "cache.miss-by-cause": {"ttl-expiry", "model-switch", "param-change", "compaction",
                            "tools-changed", "unexplained"},
    "cache.switch-churn": {"plan-toggle", "fast-toggle"},
    "cache.rebuild": {"compaction-cold", "edit-churn"},
    "cache.cold-resume": {"cold-resume"},
    "cache.ttl-advisor": {"ttl-1h-recommended", "keepalive-recommended"},
    "cache.gateway-disabled": {"no-cache", "beta-header-dropped", "tool-search-disabled"},
    "cache.unread-write": {"write-never-read", "oversized-ttl", "tail-writes"},
    "cache.cold-fanout": {"cold-fanout"},
}


def _detector(detector_id: str) -> Any:
    return load(BUILTIN_DETECTORS[detector_id])()


def _fleet_ctx(**kw: Any):
    return ctx(replayer=fn_replayer(fleet_saving), thresholds=MIN_1C, **kw)


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
    from tokenbill.detect import cache_miss, cache_structure, cache_ttl

    classes = {
        "cache.miss-by-cause": cache_miss.MissByCause, "cache.switch-churn": cache_miss.SwitchChurn,
        "cache.rebuild": cache_miss.RebuildEvents, "cache.cold-resume": cache_ttl.ColdResume,
        "cache.ttl-advisor": cache_ttl.TtlAdvisor,
        "cache.gateway-disabled": cache_structure.GatewayDisabled,
        "cache.unread-write": cache_structure.UnreadWrite,
        "cache.cold-fanout": cache_structure.ColdFanout,
    }
    for detector_id, cls in classes.items():
        assert load(BUILTIN_DETECTORS[detector_id]) is cls
        assert cls.id == detector_id and cls.version


@pytest.mark.parametrize("detector_id", ALL_DETECTORS)
def test_detector_conforms_on_the_mixed_fleet(detector_id: str) -> None:
    detector = _detector(detector_id)
    findings = assert_detector_conforms(detector, mixed_fleet(), _fleet_ctx())
    kinds = {f.kind for f in findings}
    assert kinds >= EXPECTED_KINDS[detector_id], kinds
    for f in findings:
        assert f.audience == "org"
        assert f.detector_version == "1"
        assert "principal" not in dict(f.scope.dims)
        for lever_id in f.lever_ids:
            catalog.lever(lever_id)
        if f.fix is not None and f.fix.config_patch:
            for key, value in f.fix.config_patch:
                assert key in catalog.ALLOWLIST
                json.loads(value)                  # JSON value strings
        allowance = ("billing_class", "allowance") in f.scope.dims
        figures = [x for x in (f.cost_observed, f.recoverable) if x is not None]
        assert all((x.basis is Basis.LIST_EQUIVALENT) is allowance for x in figures)
        assert f.cost_observed.evidence in (Evidence.EXACT, Evidence.ESTIMATED)
        if f.recoverable is not None:
            assert f.recoverable.evidence is Evidence.ESTIMATED
            assert f.recoverable.nano is not None and f.recoverable.nano >= 10_000_000


@pytest.mark.parametrize("detector_id", ALL_DETECTORS)
def test_shard_invariance_team_by_team(detector_id: str) -> None:
    """Per-team shards (the default plan) concatenate to the unsharded run, with no duplicate
    finding ids across shards."""
    detector = _detector(detector_id)
    lanes = mixed_fleet()
    whole = detector.detect(lanes, _fleet_ctx())
    teams = sorted({lane.team or "" for lane in lanes})
    parts = [detector.detect([ln for ln in lanes if (ln.team or "") == t], _fleet_ctx())
             for t in teams]
    merged = merge_findings(parts)
    assert sorted(f.finding_id for f in merged) == sorted(f.finding_id for f in whole)
    assert sorted(json.dumps(to_json(f), sort_keys=True) for f in merged) == sorted(
        json.dumps(to_json(f), sort_keys=True) for f in whole)


def test_findings_are_ordered_like_the_registry() -> None:
    for detector_id in ALL_DETECTORS:
        found = _detector(detector_id).detect(mixed_fleet(), _fleet_ctx())
        keys = [(-(f.recoverable.nano or 0) if f.recoverable else 0, f.finding_id)
                for f in found]
        assert keys == sorted(keys)


@pytest.mark.parametrize("detector_id", ALL_DETECTORS)
def test_missing_capabilities_through_run_detectors(detector_id: str) -> None:
    detector = _detector(detector_id)
    missing = next(iter(sorted(detector.requires)))
    caps = CAPS - {missing}
    findings = run_detectors(mixed_fleet(), _fleet_ctx(caps=caps), only=[detector_id])
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "missing-capabilities" and f.category == "data-quality"
    assert f.recoverable is None and f.cost_observed.nano is None
    assert missing in f.summary


def test_run_detectors_all_eight() -> None:
    findings = run_detectors(mixed_fleet(), _fleet_ctx(), only=list(ALL_DETECTORS))
    assert {f.detector_id for f in findings} == set(ALL_DETECTORS)
    ids = [f.finding_id for f in findings]
    assert len(ids) == len(set(ids))


def test_healthy_control_team_is_clean() -> None:
    """Every gap within the TTL and every prefix read: no cache finding at all, and in
    particular none with recoverable ≥ min_usd (default $1.00 and $0.01)."""
    lanes = healthy_lanes("core", 6)
    for thresholds in ({}, {"min_usd": "0.01"}):
        c = ctx(replayer=fn_replayer(fleet_saving), thresholds=thresholds)
        assert run_detectors(lanes, c, only=list(ALL_DETECTORS)) == []
    # and inside the mixed fleet, nothing is scoped to the core team
    found = run_detectors(mixed_fleet(), _fleet_ctx(), only=list(ALL_DETECTORS))
    assert not [f for f in found if dict(f.scope.dims).get("team") == "core"]


def test_canary_never_reaches_findings() -> None:
    """Free-text-capable record fields carry the canary; no finding repr or JSON does."""
    planted = []
    for ln in mixed_fleet():
        reqs = []
        for req in ln.requests:
            attr = dataclasses.replace(
                req.attribution, agent_type=f"agent {CANARY}", entrypoint=f"cli {CANARY}",
                project=f"proj {CANARY}", cost_center=f"cc {CANARY}", skill=f"skill {CANARY}",
                extra=tuple(sorted(dict(req.attribution.extra, mdm_group=f"g {CANARY}",
                                        workflow=f"w {CANARY}").items())))
            params = dataclasses.replace(req.params, output_format=f"fmt {CANARY}")
            reqs.append(dataclasses.replace(req, attribution=attr, params=params))
        planted.append(dataclasses.replace(ln, requests=tuple(reqs)))
    findings = run_detectors(planted, _fleet_ctx(), only=list(ALL_DETECTORS))
    assert findings
    for f in findings:
        assert_no_canary(repr(f), json.dumps(to_json(f), sort_keys=True))


def test_no_floats_in_findings() -> None:
    findings = run_detectors(mixed_fleet(), _fleet_ctx(), only=list(ALL_DETECTORS))
    for f in findings:
        assert not any(isinstance(v, float) for v in _leaves(to_json(f)))


def test_findings_publish_with_k_anonymity() -> None:
    """Unpublished findings pass core.kanon.rescope_findings: small teams are re-scoped or
    withheld; allowance and billed figures are never added."""
    findings = run_detectors(mixed_fleet(), _fleet_ctx(), only=list(ALL_DETECTORS))
    published = kanon.rescope_findings(findings, k=2)
    for f in published:
        assert isinstance(f, Finding)
        assert f.n_users >= 2 or f.category == "data-quality"


def test_self_view_is_self_audience_only() -> None:
    lanes = mixed_fleet()
    c = ctx(replayer=fn_replayer(fleet_saving), thresholds=MIN_1C, self_principal="r_pay0")
    findings = run_detectors(lanes, c, only=list(ALL_DETECTORS))
    assert findings
    assert all(f.audience == "self" for f in findings)
    assert all(dict(f.scope.dims).get("team") == "payments" for f in findings)
    assert_detector_conforms(_detector("cache.miss-by-cause"), lanes, c)


def test_unattributed_team_scope_omits_team() -> None:
    lane_ = lane("UA", [(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500)], team=None)
    found = _detector("cache.miss-by-cause").detect([lane_], ctx(thresholds={"min_usd": "0.1"}))
    assert found and "team" not in dict(found[0].scope.dims)
    assert "unattributed" in found[0].title


def test_long_team_names_are_bounded() -> None:
    team = "t" * 300
    lane_ = lane("LT", [(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500)], team=team)
    for detector_id in ALL_DETECTORS:
        for f in _detector(detector_id).detect([lane_], _fleet_ctx()):
            assert len(f.title) <= 120 and len(f.summary) <= 400


def test_lanes_without_serving_inference_are_skipped() -> None:
    """An OUTPUT_RESIDUAL-only request has no serving inference: it never breaks a detector."""
    from tokenbill.core.builders import make_lane, make_request
    from tokenbill.core.records import InferenceKind

    attr = Attribution(team="payments", principal="r_x", agent_product="claude_code",
                       billing_path="api_key")
    residual = make_request("RS", 0, 1_790_121_600_000, {"output": 900}, "claude-opus-5-5",
                            kind=InferenceKind.OUTPUT_RESIDUAL, attribution=attr,
                            params=RequestParams(model_requested="claude-opus-5-5"))
    lanes = [make_lane([residual], kind=LaneKind.MAIN, lane_key="RS")]
    for detector_id in ALL_DETECTORS:
        assert _detector(detector_id).detect(lanes, _fleet_ctx()) == []

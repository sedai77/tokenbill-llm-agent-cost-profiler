"""Merge gate F: F-KIT checks that need F-SEM (PLAN §1.5; SPEC Appendix G.1).

* every ``core.catalog.LEVERS`` grid string parses with ``core.policy.parse_policy`` (and its
  canonical form round-trips); every lever selector is a valid selector;
* ``assert_detector_conforms`` on a test detector built with ``core.findings`` /
  ``core.transitions``, and it still catches a shard-dependent variant;
* the fakes key by the real ``Policy.spec()`` and finding ids agree with ``core.findings``.

Skipped (``importorskip``) until F-SEM is merged.
"""

from __future__ import annotations

import dataclasses

import pytest

from tokenbill.core import catalog
from tokenbill.core import testing as kit
from tokenbill.core.types import Policy, Scope

pytestmark = pytest.mark.gate

policy_mod = pytest.importorskip("tokenbill.core.policy")

W = {"since_ms": 0, "until_ms": 2**53}


def test_every_lever_grid_parses_and_round_trips() -> None:
    parsed = 0
    for lv in catalog.LEVERS:
        for spec in lv.grid:
            p = policy_mod.parse_policy(spec)
            assert isinstance(p, Policy), (lv.lever_id, spec)
            assert not p.is_observed(), (lv.lever_id, spec)
            again = policy_mod.parse_policy(policy_mod.to_spec(p))
            assert again == p, (lv.lever_id, spec)
            parsed += 1
    assert parsed == sum(len(lv.grid) for lv in catalog.LEVERS) > 30


def test_every_lever_selector_is_valid() -> None:
    lanes = kit._replayer_lanes()
    for lv in catalog.LEVERS:
        terms = policy_mod.selector_terms(lv.selector)
        assert isinstance(terms, tuple)
        for lane in lanes:
            assert isinstance(policy_mod.lane_matches(lv.selector, lane), bool)
    main = lanes[0]
    assert policy_mod.lane_matches(catalog.lever("cc.prompt_cache_ttl.main").selector, main)
    assert not policy_mod.lane_matches(catalog.lever("sdk.ttl").selector, main)


def test_fake_replayer_keys_by_the_canonical_spec() -> None:
    p = policy_mod.parse_policy(catalog.lever("cc.prompt_cache_ttl.main").grid[1])
    assert kit.FakeReplayer.policy_key(p) == policy_mod.to_spec(p) == p.spec()


def _smoke_lanes():
    store = kit.MemoryStore(org_key=kit.SMOKE_ORG_KEY, pricer=kit.FakePricer())
    result = kit._smoke_result()
    store.ingest(result)
    return list(store.iter_lanes(**W)), result.capabilities


def test_detector_conformance_with_core_findings() -> None:
    findings_mod = pytest.importorskip("tokenbill.core.findings")
    pytest.importorskip("tokenbill.core.transitions")
    cache_rules = pytest.importorskip("tokenbill.core.cache_rules")
    lanes, caps = _smoke_lanes()
    pricer = kit.FakePricer()
    ctx = kit.AnalysisContext(
        pricer=pricer, rules=cache_rules.RulesTable(),
        replayer=kit.FakeReplayer.from_function(kit._smoke_saving(pricer)), calibration=None,
        window=(0, 2**53), capabilities=caps)
    found = kit.assert_detector_conforms(kit.SmokeTtlDetector(), lanes, ctx)
    assert len(found) == 1 and found[0].n_users == 6
    f = found[0]
    assert f.finding_id == findings_mod.finding_id(f.detector_id, f.kind, f.scope)

    class ShardDependent(kit.SmokeTtlDetector):
        def detect(self, lanes, c):
            return [dataclasses.replace(x, n_lanes=len(lanes)) for x in super().detect(lanes, c)]

    with pytest.raises(AssertionError, match="shard invariance"):
        kit.assert_detector_conforms(ShardDependent(), lanes, ctx)


def test_finding_ids_agree_with_core_findings() -> None:
    findings_mod = pytest.importorskip("tokenbill.core.findings")
    from tokenbill.core.registry import _finding_id

    scope = findings_mod.make_scope(team="payments", lane_kind="main")
    assert _finding_id("d", "k", scope) == findings_mod.finding_id("d", "k", scope)
    assert isinstance(scope, Scope)


def test_replayer_conformance_with_the_rules_table() -> None:
    pytest.importorskip("tokenbill.core.cache_rules")
    rep = kit.FakeReplayer.from_function(lambda lane, p: 3 if lane.kind.value == "main" else 0)
    assert kit.assert_replayer_conforms(rep, kit.FakePricer())["ttl_saving_nano"] == 6

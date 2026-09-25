"""``copilot.lanes`` against the detector contract: ``assert_detector_conforms`` (declared kinds,
labels, allowlisted Copilot fixes, catalog levers, determinism, shard invariance), the family
filter (Claude Code lanes of the same team never enter), registry gating (``ext:copilot``,
``requires={"credits"}``), the self view and the canary."""

from __future__ import annotations

import json

import pytest

from tokenbill.core import catalog, registry
from tokenbill.core.builders import CANARY, assert_no_canary, lane_from_table
from tokenbill.core.findings import COPILOT_TITLE_PREFIX, product_family
from tokenbill.core.labels import Basis
from tokenbill.core.protocols import Detector
from tokenbill.core.records import Attribution, LaneKind, to_json
from tokenbill.core.testing import assert_detector_conforms
from tokenbill.detect.copilot_lanes import KIND_INPUTS, KINDS, CopilotLanes

from .helpers import (
    CAPS,
    CLI,
    DET,
    OPUS48,
    SONNET5,
    T0,
    attr,
    compaction_event,
    ctx,
    lane,
    only,
    req,
    run,
)
from .test_compaction import events_only_cli_lane
from .test_long_context import G5
from .test_static_overhead import vscode_static_lane
from .test_subagent_ci import _cli_ci, _gh_aw, _subagent_lanes


def _claude_code_lane(key: str, team: str = "payments") -> object:
    """A Claude Code lane of the same team (billing path api_key, channel anthropic_api) with a
    300k-token context, compactions and CI workload — every trigger a Copilot kind would see."""
    a = Attribution(principal="r_dev1", team=team, agent_product="claude_code",
                    workload_class="ci", billing_path="api_key")
    ln = lane_from_table([(0, 0, 300_000, 0, 20_000, 4_000), (60, 300_000, 0, 0, 20_000, 4_000)],
                         lane_key=key, attribution=a, billing_path="api_key",
                         session_key=f"s_{key}")
    return ln


def world() -> list:
    """Copilot lanes of several cohorts plus Claude Code lanes of the same teams."""
    vs = lane("vs-1", [req("vs-1", i, 60 * i, **G5) for i in range(4)])
    return [
        vs,
        vscode_static_lane("vs-s", a=attr(team="payments", principal="r_dev2")),
        events_only_cli_lane("cli-ev"),
        *_subagent_lanes(),
        _cli_ci("ci-1", 3), _cli_ci("ci-2", 1, limit_credits=5),
        _gh_aw("aw-1"), _gh_aw("aw-2", u=2_000_000, o=80_000, model=OPUS48),
        _claude_code_lane("cc-1"), _claude_code_lane("cc-2", team="agents"),
    ]


def test_detector_surface() -> None:
    det = CopilotLanes()
    assert isinstance(det, Detector)
    assert det.id == "copilot.lanes" and det.kinds == KINDS and set(KIND_INPUTS) == set(KINDS)
    assert det.requires == frozenset({"credits"}) and det.extension == "copilot"
    assert det.families == frozenset({"copilot"}) and det.aggregate is False
    assert registry.BUILTIN_DETECTORS["copilot.lanes"] == \
        "tokenbill.detect.copilot_lanes:CopilotLanes"
    assert all(catalog.count_source("copilot.lanes", k) == "requests" for k in KINDS)


def test_conforms_and_shard_invariant_on_the_mixed_world() -> None:
    c = ctx(min_usd="0.10")
    found = assert_detector_conforms(DET, world(), c)
    assert {f.kind for f in found} == set(KINDS)
    for f in found:
        dims = dict(f.scope.dims)
        assert dims["product"] == "copilot" and dims["billing_class"] == "pool"
        assert f.title.startswith(COPILOT_TITLE_PREFIX)
        assert f.cost_observed.basis is Basis.LIST_EQUIVALENT and f.headroom is None
        assert f.recoverable is None or f.recoverable.basis is Basis.LIST_EQUIVALENT
        assert f.fix is not None and f.fix.target == "github-copilot"
        assert not any(k.startswith(("CLAUDE_CODE", "env.")) for k, _ in f.fix.config_patch or ())
        assert "CLAUDE_CODE_" not in f.fix.text
        assert f.category == ("aggregate" if f.kind in ("subagent-share", "ci-uncapped")
                              else "lever")
    assert found == sorted(found, key=lambda f: f.finding_id)


def test_claude_code_lanes_of_the_same_team_never_enter() -> None:
    c = ctx(min_usd="0")
    lanes = world()
    copilot = [ln for ln in lanes if product_family(ln) == "copilot"]
    assert len(copilot) == len(lanes) - 2
    assert run(lanes, c) == run(copilot, c)
    assert run([ln for ln in lanes if product_family(ln) != "copilot"], c) == []


def test_registry_gating() -> None:
    lanes = world()
    with_ext = ctx(min_usd="0.10")
    direct = run(lanes, with_ext)
    via = registry.run_detectors(lanes, with_ext, only=["copilot.lanes"])
    assert sorted(via, key=lambda f: f.finding_id) == direct
    # without the extension capability the detector is skipped silently
    no_ext = ctx(min_usd="0.10", caps=CAPS - {"ext:copilot"})
    assert registry.run_detectors(lanes, no_ext, only=["copilot.lanes"]) == []
    # without credits: one missing-capabilities finding, no lane finding
    no_credits = ctx(min_usd="0.10", caps=CAPS - {"credits"})
    [missing] = registry.run_detectors(lanes, no_credits, only=["copilot.lanes"])
    assert missing.kind == "missing-capabilities" and "credits" in missing.summary
    # the per-shard call runs it (aggregate=False)
    assert registry.run_detectors(lanes, with_ext, only=["copilot.lanes"],
                                  aggregates_only=True) == []


def test_self_view_reads_only_own_lanes() -> None:
    c = ctx(min_usd="0.10", self_principal="r_dev2")
    found = run(world(), c)
    assert found and all(f.audience == "self" for f in found)
    # own lanes and lanes without a principal (local self-view data carries none; here the
    # gh-aw runs)
    assert {(f.kind, dict(f.scope.dims).get("team")) for f in found} == \
        {("static-overhead", "payments"), ("ci-uncapped", "ci")}
    assert_detector_conforms(DET, world(), c)


def test_unattributed_team_and_empty_or_allowance_lanes() -> None:
    a = attr(team=None, principal=None)
    ln = lane("vs-x", [req("vs-x", 0, 0, a=a, **G5)])
    [f] = only(run([ln], ctx(min_usd="0.10")), "long-context-band")
    assert "team" not in dict(f.scope.dims) and "unattributed main lanes" in f.title
    assert run([lane("empty", [], kind=LaneKind.MAIN)], ctx(min_usd="0")) == []
    sub = attr(billing_path="subscription")
    allowance = lane("sub-1", [req("sub-1", 0, 0, a=sub, **G5)])
    assert allowance.billing_class == "allowance" and product_family(allowance) == "copilot"
    assert run([allowance], ctx(min_usd="0")) == []


def test_non_pool_copilot_lane_is_a_list_basis_copilot_finding() -> None:
    a = attr(billing_path="unknown")
    ln = lane("vs-b", [req("vs-b", 0, 0, a=a, **G5)])
    assert ln.billing_class == "billed" and product_family(ln) == "copilot"
    [f] = only(run([ln], ctx(min_usd="0.10")), "long-context-band")
    assert dict(f.scope.dims)["product"] == "copilot"
    assert f.cost_observed.basis is Basis.LIST
    assert not f.title.startswith(COPILOT_TITLE_PREFIX)     # labels are for pool cohorts


def test_canary_never_in_output() -> None:
    key = "cli-c"
    a = attr(product=CLI, agent_type=f"agent {CANARY}")
    ln = lane(key, [req(key, 0, 0, r=40_000, u=10, o=10, model=SONNET5, a=a)],
              events=[compaction_event(key, 5, f"trigger {CANARY}", system=1_000, tools=500)])
    found = run([ln, *world()], ctx(min_usd="0"))
    assert found
    blob = json.dumps([to_json(f) for f in found], sort_keys=True)
    assert_no_canary(blob)


@pytest.mark.parametrize("min_usd", ["0", "0.10", "1.00", "100"])
def test_deterministic_across_thresholds(min_usd: str) -> None:
    c = ctx(min_usd=min_usd)
    first = [to_json(f) for f in run(world(), c)]
    second = [to_json(f) for f in run(list(reversed(world())), c)]
    assert first == second
    assert T0 > 0

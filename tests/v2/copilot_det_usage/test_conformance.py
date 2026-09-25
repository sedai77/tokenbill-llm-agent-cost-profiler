"""Conformance, registry wiring, privacy (R-E16, R14), determinism and labels (R-E20) of
``copilot.org-scan``."""

from __future__ import annotations

import dataclasses
import json
import random
import re

import pytest

from tokenbill.core import builders as b
from tokenbill.core import kanon
from tokenbill.core import registry as reg
from tokenbill.core.labels import Basis
from tokenbill.core.records import OutcomeAggregate, to_json
from tokenbill.core.testing import assert_detector_conforms
from tokenbill.core.types import Finding, Scope
from tokenbill.detect.copilot_org import KINDS, CopilotOrgScan

from .worlds import (
    ALL_CAPS,
    REPO,
    WORKFLOW,
    World,
    one,
    only,
    p13_pools,
    pool_month,
    run,
)

_P_RE = re.compile(r"p_[0-9a-f]{20}")


def rich_world(*, plan_unknown: bool = False) -> World:
    """Every kind planted once (teams payments, mobile, data, jetbrains, tiny; direct usage)."""
    w = World()
    w.rows(6, team="payments", date_utc="2026-09-23", model="Claude Opus 5.5", credits="3000",
           input_tokens=200_000, cache_read_tokens=100_000, cache_write_tokens=20_000,
           output_tokens=100_000)
    w.rows(6, team="mobile", date_utc="2026-09-10", model="Claude Opus 4.8 (fast mode)",
           credits="300", input_tokens=100_000, cache_read_tokens=900_000, output_tokens=20_000)
    w.rows(6, team="data", date_utc="2026-09-10", model="GPT-5.4", credits="400",
           input_tokens=1_000_000, cache_read_tokens=900_000, output_tokens=100_000)
    w.rows(6, team="jetbrains", date_utc="2026-09-15", model="Claude Sonnet 5", credits="500",
           input_tokens=500_000, cache_read_tokens=100_000, output_tokens=50_000)
    w.rows(3, team="tiny", date_utc="2026-09-15", model="Claude Opus 4.8", credits="900",
           input_tokens=500_000, output_tokens=50_000)
    w.row(unattributed=True, date_utc="2026-09-12", model="Code Review", credits="3000")
    w.row(unattributed=True, date_utc="2026-09-12", model="Claude Sonnet 5", credits="2000",
          discount_credits="500", repo=REPO)
    w.rows(2, team="agents", date_utc="2026-09-12", model="Copilot Cloud Agent",
           sku="coding_agent_ai_credit", credits="400")
    w.actions("1000", sku="linux_16_core", usd_per_minute="0.042", workload="copilot_code_review")
    w.actions("300", workload="copilot_cloud_agent")
    w.actions("500", workload="agentic_workflow", repo=REPO, workflow=WORKFLOW)
    w.agg("gh_aw.run", "2026-09-12", {"channel": "github_copilot", "repo": REPO})
    w.agg("github.agent_tasks", "2026-09-12", {"state": "failed"}, reported=90_000_000)
    w.ide("payments", 6, vscode=8, intellij=2, mcp_distinct=6, cli_requests=10,
          cli_prompt_tokens=2_000_000)
    w.ide("jetbrains", 6, vscode=1, intellij=9)
    w.ide("tiny", 3, intellij=5)
    w.config.append(b.make_config("run_flags", {"compliance": "fedramp"}))
    w.outcomes.append(OutcomeAggregate(
        date_utc="2026-09-12", team="(enterprise)", n_users=40, sessions=0, commits=0,
        pull_requests=4, lines_added=0, lines_removed=0, edits_accepted=0, edits_rejected=0,
        source_kind="github.copilot_metrics", extra=(("prs_merged_created_by_copilot", 2),)))
    w.pools.extend(p13_pools() if plan_unknown else [pool_month(3_100_000)])
    return w


def test_every_kind_planted_and_detector_conforms() -> None:
    for unknown in (False, True):
        findings = assert_detector_conforms(CopilotOrgScan(), [], rich_world(
            plan_unknown=unknown).ctx(min_usd="0.01"))
        assert {f.kind for f in findings} == set(KINDS) - {"dq.skipped-kinds"}
        if unknown:
            scen = only(findings, "premium-model-share", team="payments")
            assert sorted(dict(f.scope.dims)["plan_scenario"] for f in scen) == [
                "business", "enterprise"]
            assert not [f for f in findings if f.kind in ("forced-migration", "review-cost")
                        and "plan_scenario" in dict(f.scope.dims)]


def test_class_attributes_and_registry() -> None:
    d = CopilotOrgScan()
    assert (d.id, d.aggregate, d.extension, d.families, d.requires) == (
        "copilot.org-scan", True, "copilot", frozenset({"copilot"}), frozenset({"aggregates"}))
    assert d.kinds[-1] == "dq.skipped-kinds" and len(d.kinds) == 19
    assert reg.BUILTIN_DETECTORS["copilot.org-scan"] == ("tokenbill.detect.copilot_org:"
                                                         "CopilotOrgScan")
    assert any(isinstance(x, CopilotOrgScan) for x in reg.all_detectors())


def test_run_detectors_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "BUILTIN_DETECTORS",
                        {"copilot.org-scan": "tokenbill.detect.copilot_org:CopilotOrgScan"})
    ctx = rich_world().ctx(min_usd="0.01")
    once = reg.run_detectors([], ctx, aggregates_only=True)
    assert {f.kind for f in once} == set(KINDS) - {"dq.skipped-kinds"}
    assert reg.run_detectors([], ctx, aggregates_only=False) == []
    silent = dataclasses.replace(ctx, capabilities=ALL_CAPS - {"ext:copilot"})
    assert reg.run_detectors([], silent, aggregates_only=True) == []
    missing = dataclasses.replace(ctx, capabilities=frozenset({"ext:copilot"}))
    (dq,) = reg.run_detectors([], missing, aggregates_only=True)
    assert (dq.kind, dq.detector_id) == ("missing-capabilities", "copilot.org-scan")


def test_no_principal_login_canary_or_repo_hash_in_output() -> None:
    w = rich_world()
    w.lines = [dataclasses.replace(line, description=f"{b.CANARY} {b.CANARY_LOGIN}")
               for line in w.lines]
    blob = json.dumps([to_json(f) for f in run(w.ctx(min_usd="0.01"))], sort_keys=True)
    assert not _P_RE.search(blob) and b.CANARY not in blob and b.CANARY_LOGIN not in blob
    assert REPO not in blob and WORKFLOW not in blob
    assert all("principal" not in dict(f.scope.dims) for f in run(w.ctx()))


def _counter(f: Finding, scope: Scope) -> int:
    """People per scope as a store would count them: team tiny has 3, every team 6, entity 40."""
    team = dict(scope.dims).get("team")
    return 3 if team == "tiny" else 6 if team else 40


def test_small_team_rescoped_although_category_aggregate() -> None:
    findings = run(rich_world().ctx(min_usd="0.01"))
    tiny = [f for f in findings if dict(f.scope.dims).get("team") == "tiny"]
    assert len(tiny) >= 3 and {f.category for f in tiny} == {"aggregate"}   # R-E16
    assert all(f.n_users == 3 for f in tiny)
    published = kanon.rescope_findings(findings, k=5, count_users=_counter)
    assert not [f for f in published if dict(f.scope.dims).get("team") == "tiny"]
    assert not [f for f in published if "tiny" in f.title or "tiny" in f.summary]
    # entity-level findings with count source entity pass unchanged (R-E16)
    migration = one(findings, "forced-migration")
    assert migration in published and one(findings, "direct-org-usage") in published


def test_labels_follow_r_e20_and_credits_never_add_to_dollars() -> None:
    for f in run(rich_world(plan_unknown=True).ctx(min_usd="0.01")):
        assert f.cost_observed.basis in (Basis.LIST_EQUIVALENT, Basis.LIST, Basis.INVOICE)
        for fig in (f.recoverable, f.recoverable_shapley, f.projected_monthly):
            assert fig is None or fig.basis in (Basis.LIST, Basis.LIST_EQUIVALENT)
        assert f.headroom is None or f.headroom.basis is Basis.LIST_EQUIVALENT
        assert dict(f.scope.dims)["product"] == "copilot"
        if f.headroom is not None and f.recoverable is not None:
            assert f.recoverable.basis is Basis.LIST            # invoice part in dollars


def test_deterministic_under_input_permutation() -> None:
    w = rich_world(plan_unknown=True)
    base = [to_json(f) for f in run(w.ctx(min_usd="0.01"))]
    rnd = random.Random(7)
    for _ in range(3):
        for seq in (w.lines, w.aggs, w.activity, w.pools, w.config):
            rnd.shuffle(seq)
        assert [to_json(f) for f in run(w.ctx(min_usd="0.01"))] == base


def test_min_usd_gate_drops_small_findings() -> None:
    w = World().rows(6, team="t", date_utc="2026-09-10", model="Claude Opus 4.8 (fast mode)",
                     credits="1", input_tokens=100, output_tokens=10)
    w.pools.append(pool_month(3_100_000))
    assert only(run(w.ctx()), "fast-mode") == []
    assert only(run(w.ctx(min_usd="0.000001")), "fast-mode")


def test_org_entity_mode_from_pools() -> None:
    w = World().rows(1, team="t", date_utc="2026-09-23", model="Claude Opus 5.5",
                     credits="200000", output_tokens=100_000_000)
    w.pools.append(pool_month(3_100_000, entity_id="org:org-a"))
    f = one(run(w.ctx()), "premium-model-share")
    assert dict(f.scope.dims)["entity"] == "org:org-a" and f.recoverable.nano == 1000 * 10**9

"""Hypothesis properties and fuzz tests for every CP-POLICY parser and reader: only
``TokenbillError`` subclasses may escape (SPEC §21 #5)."""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.copilot.admin_actions import admin_actions, team_editor_shares
from tokenbill.copilot.budgets import budget_design
from tokenbill.copilot.policy import (
    apply_merge_patch,
    build_copilot_packs,
    load_current,
    managed_settings_patch,
)
from tokenbill.core import builders as b
from tokenbill.core import catalog
from tokenbill.core.types import EvidenceItem

from .helpers import TODAY, BudgetWorld, budget_pools, finding, plan, result

SETTINGS = settings(max_examples=60, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
MANAGED_KEYS = sorted(k for k in catalog.COPILOT_ALLOWLIST if k.startswith("copilot.managed."))

_scalar = st.one_of(st.booleans(), st.integers(-10**6, 10**6), st.text(max_size=8))
_json = st.recursive(_scalar, lambda inner: st.one_of(
    st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=6), inner, max_size=3)),
    max_leaves=12)
_documents = st.dictionaries(
    st.sampled_from(["model", "telemetry", "deniedMcpServers", "other", "x"]),
    st.one_of(_json, st.dictionaries(st.sampled_from(
        ["enabled", "endpoint", "headers", "serviceName", "resourceAttributes"]), _json,
        max_size=4)), max_size=5)


@SETTINGS
@given(st.one_of(st.text(max_size=200), st.binary(max_size=200),
                 _json.map(lambda v: json.dumps(v)), _documents.map(json.dumps)))
def test_load_current_returns_an_object_or_raises_a_usage_error(text) -> None:
    try:
        got = load_current(text)
    except TokenbillError:
        return
    assert isinstance(got, dict)


@SETTINGS
@given(_documents, st.dictionaries(st.sampled_from(MANAGED_KEYS), _json, min_size=1,
                                   max_size=4))
def test_patch_then_rollback_restores_the_current_document(current, changes) -> None:
    patch, rollback = managed_settings_patch(changes, current)
    applied = apply_merge_patch(current, patch)
    for key, value in changes.items():
        node = applied
        for part in key[len("copilot.managed."):].split("."):
            node = node[part]
        assert json.dumps(node, sort_keys=True) == json.dumps(value, sort_keys=True) or \
            _has_null(value)
    if not _has_null(current):
        assert apply_merge_patch(applied, rollback) == current
    assert set(patch) <= {k.split(".")[2] for k in changes}


def _has_null(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False


_counts = st.dictionaries(
    st.one_of(st.sampled_from(["ide:intellij", "ide:vscode", "cli", "app", "n_people", "seats",
                               "idle_seats", "vscode", "jetbrains", "interactions"]),
              st.text(max_size=6), st.integers()),
    st.one_of(st.integers(-5, 10**12), st.text(max_size=4), st.booleans(), st.none()),
    max_size=6)


@SETTINGS
@given(st.one_of(st.dictionaries(st.one_of(st.text(max_size=6), st.integers()), _counts,
                                 max_size=4), st.lists(st.integers(), max_size=2), st.none()))
def test_team_counts_reader_accepts_or_raises_usage_error(teams) -> None:
    try:
        got = team_editor_shares(teams)
    except TokenbillError:
        return
    for share, total, _people in got.values():
        assert 0 <= share <= 1 and total > 0


_attr_value = st.one_of(st.integers(-10**12, 10**12), st.text(max_size=12), st.booleans())
_evidence = st.lists(st.builds(
    lambda ref, attrs: EvidenceItem("aggregate", ref, tuple(attrs)),
    st.sampled_from(["assignment:removable", "assignment:team", "aw:runs", "editors", "ci", "x"]),
    st.lists(st.tuples(st.sampled_from(["n", "suggested_max_ai_credits", "jetbrains_share",
                                        "other"]), _attr_value), max_size=4)), max_size=4)
_kinds = st.sampled_from(["idle-seat", "editor-mix", "agentic-workflow-cost", "ci-uncapped",
                          "review-drivers", "overage-forecast", "plan-mix", "auto-adoption",
                          "unattributed-spend", "budget-no-cost-center-pool", "plan-status",
                          "cloud-agent-cost", "mcp-sprawl", "fast-mode"])


@SETTINGS
@given(st.lists(st.tuples(_kinds, _evidence, st.sampled_from([None, "business", "enterprise"])),
                max_size=6),
       st.booleans(), st.sampled_from(["metered", "volume", "azure", "unknown"]))
def test_admin_actions_and_packs_survive_hostile_evidence(items, unknown, mode) -> None:
    fs = []
    for kind, evidence, scenario in items:
        dims = {"team": "t1"}
        if scenario is not None:
            dims["plan_scenario"] = scenario
        fs.append(finding(kind, evidence=evidence, **dims))
    if unknown:
        plans = (("business", plan(result("copilot.seat_reclaim", 0))),
                 ("enterprise", plan(result("copilot.seat_reclaim", 390))))
        pools = [b.make_pool_month(seats={"unknown": "10"}, plan_scenario=s, billing_mode=mode)
                 for s in ("business", "enterprise")]
    else:
        plans = (("known", plan(result("copilot.seat_reclaim", 20))),)
        pools = [b.make_pool_month(billing_mode=mode)]
    # well-formed findings with hostile evidence never make the checklist or the pack fail
    acts = admin_actions(plans, fs, pools, plans=[], today=TODAY)
    packs = build_copilot_packs(plans, fs, acts, current={"model": "x"}, cohort_by=None,
                                include_tradeoffs=True, teams=None, budgets=None, today=TODAY)
    assert all(len(a.what) <= 400 for a in acts)
    assert acts == admin_actions(plans, list(reversed(fs)), pools, plans=[], today=TODAY)
    ids = [a.action_id for a in acts]
    assert len(ids) == len(set(ids))
    if unknown:
        assert ids[0] == "admin:plan_confirm" and "admin:seat_plan_change" not in ids
    for line in dict(packs[0].hooks)["github/requests.jsonl"].splitlines():
        json.loads(line)


@SETTINGS
@given(st.lists(st.tuples(st.sampled_from("ABCD"), st.lists(st.integers(1, 20_000), min_size=1,
                                                            max_size=12)),
                min_size=1, max_size=4, unique_by=lambda t: t[0]),
       st.integers(1, 60), st.booleans(), st.integers(1, 7))
def test_budget_amounts_are_whole_dollars_and_cover_the_cost_centers(ccs, seats, scenarios,
                                                                    k) -> None:
    w = BudgetWorld()
    for name, per_user in ccs:
        w.cost_center(name, per_user, seats=max(1, len(per_user) // 2))
    pools = budget_pools(w, scenarios=scenarios)
    pools = [p.__class__(**{**{f: getattr(p, f) for f in p.__slots__},
                            "seats": (("unknown" if scenarios else "business", str(seats)),)})
             for p in pools]
    specs = budget_design(pools, w.cells(), w.config, k=k, cost_lines=w.lines)
    assert specs == budget_design(list(reversed(pools)), list(reversed(w.cells())), w.config,
                                  k=k, cost_lines=list(reversed(w.lines)))
    for scenario in ({s["scenario"] for s in specs} - {None}) or {None}:
        mine = [s for s in specs if s["scenario"] == scenario and s["kind"] == "budget"]
        cc = sum(s["request"]["body"]["budget_amount"] for s in mine
                 if s["request"]["body"]["budget_scope"] == "cost_center")
        top = [s["request"]["body"]["budget_amount"] for s in mine
               if s["request"]["body"]["budget_scope"] == "enterprise"]
        assert len(top) == 1 and top[0] >= cc
    for s in specs:
        if s["request"] is not None and "budget_amount" in s["request"]["body"]:
            amount = s["request"]["body"]["budget_amount"]
            assert type(amount) is int and amount >= 1
        if s["request"] is not None and s["request"]["body"].get("budget_scope") == \
                "multi_user_cost_center":
            cc_name = s["request"]["body"]["budget_entity_name"]
            assert len(dict(ccs)[cc_name]) >= k

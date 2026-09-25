"""Gate (merge gate 1): the real Copilot detectors recover every org-data plant of the synthetic
enterprise within the addendum §18 tolerance (±0 for the closed forms), the control team has no
finding with an invoice or headroom figure at ``min_usd``, the tiny team is merged away by
k-anonymity, and the plan-unknown / plan-conflict / volume / slack variants behave as their truths
say. Context built as CP-STORE's enricher builds it (``core.pool`` cells, pool months, plans)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from tokenbill.core.findings import min_usd_nano

from .worlds import ctx, dims, evidence, of, world

pytestmark = pytest.mark.gate


def _seats(*variants: str) -> list:
    mod = pytest.importorskip("tokenbill.detect.copilot_seats")
    return mod.CopilotSeatsBudgets().detect([], ctx(world(*variants)))


def _org(*variants: str) -> list:
    mod = pytest.importorskip("tokenbill.detect.copilot_org")
    return mod.CopilotOrgScan().detect([], ctx(world(*variants)))


def _sept(t, entity: str, scenario: str | None = None):
    return next(p for p in t.pool_months if p.entity_id == entity and p.month == "2026-09"
                and p.plan_scenario == scenario)


def test_seat_and_budget_plants() -> None:
    t = world().truth
    found = _seats()
    [platform] = of(found, "idle-seat", team="platform")
    assert evidence(platform, "assignment:removable")["n"] == t.idle_seats["platform"]["removable"]
    assert platform.recoverable.nano == t.idle_seat_saving_nano
    [infra] = of(found, "idle-seat", team="infra")
    assert evidence(infra, "assignment:team")["n"] == 5 and infra.recoverable is None
    [auto] = of(found, "seat-auto-assign")
    assert auto.n_users == 5 and auto.recoverable.nano == t.seat_auto_assign_nano
    assert dims(auto)["org"] == "org-b"
    [mix] = of(found, "plan-mix", team="platform")
    assert mix.n_users == t.plan_mix_seats["platform"]
    [zero] = of(found, "budget-zero-user-budget", team="platform")
    assert zero.n_events == t.zero_user_budgets["platform"]
    assert of(found, "budget-stop-usage-off")
    status = {dims(f)["entity"]: evidence(f, "plan")["plan"] for f in of(found, "plan-status")}
    assert status == {"enterprise": "mixed", "cc:cc-data": "business"}
    for f in of(found, "pool-regime"):
        assert f.cost_observed.nano == _sept(t, dims(f)["entity"]).overage_observed_nano
    [cliff] = of(found, "promo-cliff", entity="enterprise")
    assert cliff.cost_observed.nano == t.promo_cliff_nano["enterprise"]
    [fc] = of(found, "overage-forecast", entity="enterprise")
    assert fc.cost_observed.nano == _sept(t, "enterprise").overage_forecast[0]
    [cc_fc] = of(found, "overage-forecast", entity="cc:cc-data")
    low, high = t.cap_overage_range["2026-09"]
    assert cc_fc.cost_observed.low_nano == low == 0
    assert of(found, "duplicate-seat") == [] and of(found, "completions-only-seat") == []


@pytest.mark.parametrize("variants", [("slack",), ("volume",)])
def test_seat_projections_follow_the_regime(variants: tuple[str, ...]) -> None:
    t = world(*variants).truth
    found = _seats(*variants)
    [platform] = of(found, "idle-seat", team="platform")
    [auto] = of(found, "seat-auto-assign")
    got = (platform.recoverable.nano if platform.recoverable else None,
           auto.recoverable.nano if auto.recoverable else None)
    assert got == (t.idle_seat_saving_nano, t.seat_auto_assign_nano)


def test_plan_unknown_scenarios() -> None:
    t = world("plan_unknown").truth
    found = _seats("plan_unknown")
    [status] = of(found, "plan-status")
    assert evidence(status, "plan")["plan"] == "unknown"
    assert of(found, "plan-mix") == []
    for f in of(found, "idle-seat"):
        key = (dims(f)["team"], dims(f)["plan_scenario"])
        assert f.recoverable.nano == t.idle_seat_saving_by_scenario[key], key
        assert evidence(f, "assignment:unknown")["n"] == t.idle_seats[key[0]]["unknown"]
    got = {dims(f)["plan_scenario"]: f.cost_observed.nano for f in of(found, "pool-regime")}
    assert got == {s: _sept(t, "enterprise", s).overage_observed_nano
                   for s in ("business", "enterprise")}


def test_plan_conflict_and_quota() -> None:
    [status] = of(_seats("plan_conflict"), "plan-status", entity="enterprise")
    assert evidence(status, "plan")["plan"] == "mixed"
    assert str(evidence(status, "plan")["conflict"]).lower() == "true"
    found = _seats("plan_quota")
    [status] = of(found, "plan-status")
    assert evidence(status, "plan")["source"] == "report_quota"
    assert {dims(f)["plan_scenario"] for f in of(found, "pool-regime")} == {
        "business", "enterprise"}


def test_org_scan_plants() -> None:
    t = world().truth
    found = _org()
    [fast] = of(found, "fast-mode", team="mobile")
    assert fast.cost_observed.nano == t.fast_premium_nano
    assert fast.recoverable.nano + fast.headroom.nano == t.fast_premium_nano
    for team, saving in t.premium_remap_saving_by_team.items():
        [f] = of(found, "premium-model-share", team=team)
        assert f.recoverable.nano + f.headroom.nano == saving, team
    for team, saving in t.auto_saving_by_team.items():
        [f] = of(found, "auto-adoption", team=team)
        assert f.recoverable.nano + f.headroom.nano == saving, team
        assert Fraction(evidence(f, "reach")["reach"]) == t.auto_reach[team], team
    assert of(found, "auto-adoption", team="core") == []
    for (entity, model), delta in t.forced_migration_delta.items():
        [f] = of(found, "forced-migration", entity=entity, model=model)
        assert f.cost_observed.nano == delta
    [direct] = of(found, "direct-org-usage")
    assert direct.cost_observed.nano == t.direct_org_net_nano
    [larger] = of(found, "larger-runner")
    net, low, high = t.larger_runner
    assert larger.cost_observed.nano == net
    assert (larger.recoverable.low_nano, larger.recoverable.high_nano) == (low, high)
    [aw] = of(found, "agentic-workflow-cost")
    runs = evidence(aw, "aw:runs")
    assert (runs["runs"], runs["run_p50_nano"], runs["run_p90_nano"]) == (
        len(t.agentic_run_prices), t.agentic_run_p50_nano, t.agentic_run_p90_nano)
    [mix] = of(found, "editor-mix", team="jetbrains")
    assert Fraction(evidence(mix, "editors")["jetbrains_share"]) == \
        t.editor_share["jetbrains"]["jetbrains"]
    assert "server-side model policy" in mix.fix.text


@pytest.mark.parametrize("variants", [(), ("plan_unknown",), ("slack",)])
def test_control_team_is_clean(variants: tuple[str, ...]) -> None:
    w = world(*variants)
    limit = min_usd_nano(ctx(w))
    for f in _seats(*variants) + _org(*variants):
        if dims(f).get("team") != w.truth.control_team:
            continue
        for fig in (f.recoverable, f.headroom):
            assert fig is None or fig.nano is None or fig.nano < limit, f.kind


def test_tiny_team_merged_by_k_anonymity() -> None:
    kanon = pytest.importorskip("tokenbill.core.kanon")
    w = world()
    raw = _seats() + _org()
    assert any(dims(f).get("team") == w.truth.tiny_team for f in raw)
    published = kanon.rescope_findings(raw, k=5)
    assert not [f for f in published if dims(f).get("team") == w.truth.tiny_team]


def test_lane_plants_when_the_lane_detector_exists() -> None:
    lanes_mod = pytest.importorskip("tokenbill.detect.copilot_lanes")
    w = world()
    t = w.truth.lanes
    found = lanes_mod.CopilotLanes().detect(list(w.records.lanes), ctx(w))
    kinds = {f.kind for f in found}
    assert {"long-context-band", "compaction-cost", "ci-uncapped"} <= kinds
    spend = sum(f.cost_observed.nano or 0 for f in found if f.kind == "compaction-cost")
    assert spend == t.compaction_spend_nano

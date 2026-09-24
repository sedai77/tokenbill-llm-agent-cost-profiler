"""Per-kind gating (brief Build 1, addendum §10.0 revision 3): the detector runs whenever
``ext:copilot`` is present (``requires=frozenset()``); each kind group checks its own inputs and
every skipped kind is named once in a single ``dq.skipped-kinds`` data-quality finding."""

from __future__ import annotations

import dataclasses

from tokenbill.core import kanon
from tokenbill.core import registry as reg
from tokenbill.detect.copilot_seats import (
    BUDGET_KINDS,
    KIND_REQUIRES,
    KINDS,
    POOL_KINDS,
    SEAT_KINDS,
    CopilotSeatsBudgets,
)

from .helpers import (
    USD,
    budget,
    cost_center,
    ctx,
    detect,
    dims,
    evidence,
    of_kind,
    org_settings,
    p_plan,
    p_pool,
    seat_count,
)
from .test_pool_kinds import _p9_ctx


def _skipped(found: list) -> dict[str, str]:
    [dq] = of_kind(found, "dq.skipped-kinds")
    return {e.ref.removeprefix("kind:"): str(dict(e.attrs)["missing"]) for e in dq.evidence}


def test_kind_tables() -> None:
    assert set(KIND_REQUIRES) == set(KINDS) - {"dq.skipped-kinds"}
    assert KIND_REQUIRES["plan-status"] == (frozenset(),)
    assert set(POOL_KINDS) | set(SEAT_KINDS) | set(BUDGET_KINDS) | {
        "plan-status", "dq.skipped-kinds"} == set(KINDS)
    d = CopilotSeatsBudgets()
    assert (d.id, d.requires, d.aggregate, d.extension, d.families) == (
        "copilot.seats-budgets", frozenset(), True, "copilot", frozenset({"copilot"}))


def test_pools_and_config_without_licenses() -> None:
    """UI files without a seats list: pool and budget kinds run, seat kinds are skipped."""
    conf = (budget("e", "enterprise", 1000, stop=False), cost_center("data"))
    found = detect(_p9_ctx(config=conf))
    kinds = {f.kind for f in found}
    assert {"plan-status", "pool-regime", "overage-forecast", "budget-stop-usage-off",
            "budget-paid-usage-uncapped"} <= kinds
    assert not kinds & set(SEAT_KINDS)
    skipped = _skipped(found)
    assert {"idle-seat", "seat-auto-assign", "completions-only-seat", "plan-mix",
            "duplicate-seat"} <= set(skipped)
    assert skipped["idle-seat"] == "licenses or seat_counts"
    assert skipped["duplicate-seat"] == "licenses"
    assert not set(skipped) & {"pool-regime", "overage-forecast", "promo-cliff",
                               "budget-stop-usage-off", "budget-zero-user-budget"}
    assert len(of_kind(found, "dq.skipped-kinds")) == 1


def _aggregate_only(extra: list | None = None):
    conf = [seat_count("org-a", 6, team="t1"),
            seat_count("org-a", 5, team="t2", via_team=True),
            seat_count("org-a", 10, team="t3", bucket="0-7"),
            seat_count("org-a", 4, team="t4", via_team=None, created=None, zero_cost=None),
            seat_count("org-a", 3, team="t5", pending=True),
            seat_count("org-a", 2, team="t5", created=False),
            org_settings("org-a", "assign_selected"),
            *(extra or [])]
    pm = p_pool(20_000, seats_map={"business": "30"}, seats_source="seat_counts")
    plans = [p_plan(plan="business", source="seats_api", seats_map={"business": 30})]
    return ctx(pools=[pm], plans=plans, config=conf, today="2026-10-05")


def test_aggregate_only_idle_seats_per_team_from_counts() -> None:
    found = detect(_aggregate_only())
    idle = {dims(f)["team"]: f for f in of_kind(found, "idle-seat")}
    assert set(idle) == {"t1", "t2", "t4"}
    assert evidence(idle["t1"], "assignment:removable")["n"] == 6
    assert idle["t1"].recoverable is not None
    assert idle["t1"].recoverable.nano == 6 * 19 * USD          # slack: the fees are saved
    assert evidence(idle["t2"], "assignment:team")["n"] == 5 and idle["t2"].recoverable is None
    t4 = idle["t4"]
    assert evidence(t4, "assignment:unknown")["n"] == 4
    assert evidence(t4, "caveats") == {"age_unknown": 4, "zero_cost_unverified": 4}
    assert t4.needs_eval and t4.recoverable is not None and t4.recoverable.upper_bound
    skipped = _skipped(found)
    assert {"plan-mix", "completions-only-seat", "duplicate-seat"} <= set(skipped)
    assert "idle-seat" not in skipped and "seat-auto-assign" not in skipped


def test_aggregate_only_assign_all_org() -> None:
    found = detect(_aggregate_only([org_settings("org-a", "assign_all", date="2026-09-20")]))
    [auto] = of_kind(found, "seat-auto-assign")
    # t1 (6), t2 (5, team policy irrelevant under assign_all) and t4 (4)
    assert auto.n_users == 15 and dims(auto)["org"] == "org-a"


def test_only_plans_everything_else_skipped() -> None:
    found = detect(ctx(plans=[p_plan()]))
    assert {f.kind for f in found} == {"plan-status", "dq.skipped-kinds"}
    skipped = _skipped(found)
    assert set(skipped) == set(KINDS) - {"plan-status", "dq.skipped-kinds"}
    [dq] = of_kind(found, "dq.skipped-kinds")
    assert (dq.category, dq.cost_observed.nano, dq.recoverable, dq.fix) == (
        "data-quality", None, None, None)
    assert "no mechanical fix" in dq.summary.lower()
    assert dims(dq) == {"entity": "enterprise", "product": "copilot"}
    assert kanon.rescope_findings([dq], k=5) == [dq]            # R-E1: exempt


def test_dq_entity_scope_in_org_mode() -> None:
    plans = [p_plan(entity="org:a", plan="business", seats_map={"business": 5}),
             p_plan(entity="org:b", plan="business", seats_map={"business": 5})]
    [dq] = of_kind(detect(ctx(plans=plans)), "dq.skipped-kinds")
    assert dims(dq) == {"product": "copilot"}
    [dq] = of_kind(detect(ctx(plans=plans[:1])), "dq.skipped-kinds")
    assert dims(dq) == {"entity": "org:a", "product": "copilot"}


def test_registry_and_run_detectors_phases() -> None:
    assert reg.BUILTIN_DETECTORS["copilot.seats-budgets"] == (
        "tokenbill.detect.copilot_seats:CopilotSeatsBudgets")
    context = _aggregate_only()
    once = reg.run_detectors([], context, only=["copilot.seats-budgets"], aggregates_only=True)
    assert {f.kind for f in once} >= {"plan-status", "idle-seat"}
    assert reg.run_detectors([], context, only=["copilot.seats-budgets"],
                             aggregates_only=False) == []
    silent = dataclasses.replace(context, capabilities=frozenset())
    assert reg.run_detectors([], silent, only=["copilot.seats-budgets"]) == []

"""Conformance (``core.testing.assert_detector_conforms``: aggregate lane independence,
determinism), input-permutation invariance, k-anonymous publication through ``core.kanon`` (R-E16:
scenarios never merged, entity kinds exempt) and the content canary."""

from __future__ import annotations

import dataclasses
import json
import random

import pytest

from tokenbill.core import builders as b
from tokenbill.core import kanon
from tokenbill.core import testing as kit
from tokenbill.core.records import to_json
from tokenbill.core.types import AnalysisContext, Scope
from tokenbill.detect.copilot_seats import CopilotSeatsBudgets

from .helpers import (
    METERED,
    activity_report,
    budget,
    cost_center,
    ctx,
    detect,
    dims,
    enrich,
    flags,
    of_kind,
    org_settings,
    p_plan,
    p_pool,
    people,
    rows,
    seat_count,
    seats,
)


def _p13_world() -> AnalysisContext:
    cost, aggs = rows(250_000, date="2026-10-10", users=people(50, "r"), discount=250_000)
    lics = (activity_report(90, seed="r", bucket="0-7")
            + activity_report(3, seed="idle", team="small")
            + activity_report(10, seed="idle2"))
    conf = [METERED, budget("e", "enterprise", 100),
            *[budget(f"z{i}", "user", 0, stop=True, team="t1") for i in range(6)]]
    pms, plans = enrich(cost, aggs, lics, conf, today="2026-11-10")
    return ctx(pools=pms, plans=plans, licenses=lics, config=conf, cost_lines=cost,
               today="2026-11-10")


def _p1_world() -> AnalysisContext:
    idle = (seats(12, seed="direct") + seats(8, seed="team", via_team=True)
            + seats(10, org="org-b", seed="auto", team="t2")
            + seats(3, org="org-a", seed="multi", team="t3")
            + seats(3, org="org-b", seed="multi", team="t3"))
    act = [b.make_activity(p, date_utc="2026-09-20", counts={"code_generation": 3})
           for p in people(6, "done")]
    lics = idle + seats(6, seed="done", bucket="0-7", plan="enterprise")
    conf = [org_settings("org-a", "assign_selected"), org_settings("org-b", "assign_all"),
            budget("o", "organization", 50, target="org:org-a"), cost_center("data"),
            flags({"renewal_date.enterprise": "2027-01-01"})]
    return ctx(pools=[p_pool(2_000_000, month="2026-08"), p_pool(2_000_000)],
               plans=[p_plan()], licenses=lics, config=conf, activity=act,
               cost_lines=rows(10, date="2026-09-20")[0], today="2026-10-05")


def _counts_world() -> AnalysisContext:
    conf = [seat_count("org-a", 6, team="t1"), seat_count("org-a", 5, team="t2", via_team=True),
            org_settings("org-a", "assign_selected")]
    return ctx(pools=[p_pool(20_000, seats_map={"business": "30"})], config=conf)


WORLDS = {"p13": _p13_world, "p1": _p1_world, "counts": _counts_world}


def _lanes() -> list:
    copilot = b.make_request("cp-lane", 0, 1_790_000_000_000,
                             {"uncached_input": 100, "output": 10}, "claude-opus-5-5",
                             provider="github", channel="github_copilot",
                             billing_path="copilot_pool")
    claude = b.make_request("cc-lane", 0, 1_790_000_000_000,
                            {"uncached_input": 100, "output": 10}, "claude-opus-5-5")
    return [b.make_lane([copilot]), b.make_lane([claude])]


@pytest.mark.parametrize("world", sorted(WORLDS))
def test_assert_detector_conforms(world: str) -> None:
    found = kit.assert_detector_conforms(CopilotSeatsBudgets(), _lanes(), WORLDS[world]())
    assert found and all(f.detector_id == "copilot.seats-budgets" for f in found)


def _json(findings: list) -> list[str]:
    return [json.dumps(to_json(f), sort_keys=True) for f in findings]


def _shuffled(context: AnalysisContext, seed: int) -> AnalysisContext:
    rng = random.Random(seed)
    fields = {}
    for name in ("pools", "plans", "licenses", "config", "activity", "cost_lines"):
        items = list(getattr(context, name))
        rng.shuffle(items)
        fields[name] = tuple(items)
    return dataclasses.replace(context, **fields)


@pytest.mark.parametrize("world", sorted(WORLDS))
def test_identical_findings_across_input_permutations(world: str) -> None:
    context = WORLDS[world]()
    expected = _json(detect(context))
    for seed in range(4):
        assert _json(detect(_shuffled(context, seed))) == expected


def _count(_f: object, scope: Scope) -> int:
    """Store-style people counts: team ``small`` has 3 seated people, every other scope 100."""
    return 3 if ("team", "small") in scope.dims else 100


def test_publication_never_merges_scenarios() -> None:
    found = detect(_p13_world())
    small = [f for f in found if dims(f).get("team") == "small"]
    assert {f.kind for f in small} == {"idle-seat"} and len(small) == 2
    published = kanon.rescope_findings(found, k=5, count_users=_count)
    for kind in ("pool-regime", "idle-seat"):
        scen = sorted(dims(f).get("plan_scenario", "") for f in of_kind(published, kind))
        assert scen == ["business", "enterprise"], kind
    # the three idle seats of team "small" were merged into the entity level, never named
    assert not [f for f in published if "small" in json.dumps(to_json(f))]
    merged = of_kind(published, "idle-seat")
    assert all("team" not in dims(f) and f.n_events == 13 for f in merged)


def test_entity_kinds_are_exempt_and_team_kinds_follow_counts() -> None:
    found = detect(_p13_world())
    counted: list[Scope] = []

    def count(f: object, scope: Scope) -> int:
        counted.append(scope)
        return _count(f, scope)

    published = kanon.rescope_findings(found, k=5, count_users=count)
    kinds = {f.kind for f in published}
    assert {"plan-status", "pool-regime", "idle-seat", "budget-zero-user-budget"} <= kinds
    assert counted and all(("product", "copilot") in s.dims for s in counted)
    for f in of_kind(published, "pool-regime") + of_kind(published, "plan-status"):
        assert f in found                               # exempt: published unchanged


def test_canary_never_reaches_a_finding() -> None:
    canary = b.CANARY
    lics = [dataclasses.replace(x, seat_created=f"2026-01-05T00:00:00Z {canary}")
            for x in seats(6)]
    lics += [dataclasses.replace(x, pending_cancellation=canary) for x in seats(2, seed="p")]
    conf = [b.make_config("budget", {"scope": "user", "amount_nano": 0, "team": "t1",
                                     "prevent_further_usage": True, "expires_at": canary},
                          entity_id=f"budget:{i}", source_kind="github.budgets")
            for i in range(5)]
    conf += [b.make_config("cost_center", {"state": canary, "pool_enabled": False},
                           entity_id="cc:data", source_kind="github.cost_centers"),
             org_settings("org-a", "assign_selected")]
    cost = [dataclasses.replace(x, description=canary) for x in rows(9, date="2026-09-20")[0]]
    found = detect(ctx(pools=[p_pool(2_000_000)], plans=[p_plan()], licenses=lics,
                       config=conf, cost_lines=cost))
    assert {f.kind for f in found} >= {"idle-seat", "budget-zero-user-budget"}
    blob = json.dumps(_json(found))
    b.assert_no_canary(blob)
    assert b.CANARY_LOGIN not in blob


def test_lane_independent_and_no_lanes_needed() -> None:
    context = _p1_world()
    assert _json(CopilotSeatsBudgets().detect(_lanes(), context)) == _json(detect(context))


def test_self_view_context_still_org_audience() -> None:
    context = dataclasses.replace(_counts_world(), self_principal=b.make_principal(1))
    assert {f.audience for f in detect(context)} == {"org"}

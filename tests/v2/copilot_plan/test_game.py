"""The game itself: which levers play (brief Build 1), the transforms (addendum §9.2 steps 1–2),
exact vs Monte Carlo Shapley, canonical params, determinism and input validation."""

from __future__ import annotations

import random

import pytest

from tokenbill.copilot.plan import (
    MAX_EXACT_PLAYERS,
    MC_PERMUTATIONS,
    plan_copilot,
    plan_copilot_scenarios,
)
from tokenbill.core import catalog
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Evidence

from .worlds import (
    PRICER,
    USD,
    C,
    World,
    finding,
    has_lever,
    idle,
    lever,
    p1_world,
    plan_for,
)

AUTO = "copilot.default_model_auto"
POLICY = "copilot.model_policy"
FAST = "copilot.fast_mode_off"
SEAT = "copilot.seat_reclaim"
RUNNER = "copilot.agent_runner_standard"


def _deep_overage() -> World:
    """C.P1 seats with 3,100,000 pooled credits: 420,000 in overage, so small credit savings are
    invoice dollars one for one."""
    return p1_world(3_100_000).ide("t1", {"ide:vscode": 1})


# ---------- who plays ----------


def test_trade_off_lever_needs_include_tradeoffs() -> None:
    w = _deep_overage().usage(100_000, model="Claude Opus 4.8", output_tokens=40_000_000)
    f = finding("premium-model-share", team="t1")
    plain = plan_for(w, [f])
    assert not has_lever(plain, POLICY) and plain.levers == ()
    assert "copilot.model_policy (trade-off; include_tradeoffs off)" in (
        plain.headline_monthly.note)
    assert plain.headline_monthly.nano == 0 and plain.method == "shapley-exact"
    with_t = plan_for(w, [f], include_tradeoffs=True)
    assert lever(with_t, POLICY).shapley.nano == 60_000 * C


def test_behavioral_and_replay_none_levers_never_play() -> None:
    fs = [finding(kind, team="t1") for kind in (
        "review-drivers", "review-cost", "overage-forecast", "static-overhead", "mcp-sprawl",
        "ci-uncapped", "cache-health", "long-context-band", "direct-org-usage")]
    for f in fs:
        linked = catalog.levers_for_kind(f.kind, family="copilot")
        assert linked and all(lv.replay == "none" for lv in linked), f.kind
    plan = plan_for(_deep_overage(), fs, include_tradeoffs=True)
    assert plan.levers == () and plan.groups == ()
    assert "no applicable aggregate Copilot lever" in plan.headline_monthly.note
    assert plan.pool_headroom_monthly.basis is Basis.LIST_EQUIVALENT


def test_lever_ids_link_levers_and_non_copilot_findings_are_ignored() -> None:
    w = _deep_overage().usage(29, model="Claude Opus 4.8 (fast mode)", input_tokens=10_000,
                              cache_read=90_000, output_tokens=2_000)
    by_id = finding("plan-status", detector="copilot.seats-budgets", entity="enterprise",
                    lever_ids=(FAST, "cc.prompt_cache_ttl.main"))
    plan = plan_for(w, [by_id])
    assert lever(plan, FAST).finding_ids == (by_id.finding_id,)
    from tokenbill.core.findings import build_finding, make_scope
    from tokenbill.core.labels import exact
    generic = build_finding(
        detector_id="cache.miss-by-cause", kind="fast-mode", detector_version="1",
        category="lever", lever_class="rate", audience="org", title="t", summary="s",
        scope=make_scope(team="t1"), n_events=1, n_lanes=1, n_users=5, first_seen_ms=0,
        cost_observed=exact(0, Basis.LIST), recoverable=None, references=("x",))
    assert plan_for(w, [generic]).levers == ()


def test_fast_mode_off_reproduces_the_g8_premium() -> None:
    """C.G8: Opus 4.8 fast, uncached 10,000, read 90,000, output 2,000 → premium 145,000,000
    nano on identical tokens; in the overage entity all of it is invoice."""
    w = _deep_overage().usage(29, model="Claude Opus 4.8 (fast mode)", input_tokens=10_000,
                              cache_read=90_000, output_tokens=2_000)
    fast = lever(plan_for(w, [finding("fast-mode", team="t1")]), FAST)
    assert fast.shapley.nano == fast.standalone.nano == 145_000_000
    assert fast.params == "copilot:fast=off@all"
    assert fast.projected_monthly.nano == 145_000_000 and not fast.needs_eval


def test_remap_with_different_tokenizers_spans_the_band() -> None:
    """GPT-5.5 → GPT-5.6 Terra (tokenizer_same False): 1M output tokens $30 → $12 at band 1.00
    ($18 saved), $16.20 at band 1.35 ($13.80 saved)."""
    w = _deep_overage().usage(3_000, model="GPT-5.5", output_tokens=1_000_000)
    plan = plan_for(w, [finding("premium-model-share", model="gpt-5.5")], include_tradeoffs=True)
    pol = lever(plan, POLICY)
    assert pol.params == "copilot:remap=gpt-5.6-terra@model:gpt-5.5"
    assert (pol.shapley.nano, pol.shapley.low_nano, pol.shapley.high_nano) == (
        18 * USD, 13_800_000_000, 18 * USD)
    assert "tokenizer band 1.00-1.35" in pol.shapley.note


def test_remap_to_an_unpriced_target_is_not_counted_and_noted() -> None:
    w = _deep_overage().usage(5_000, model="GPT-6 Astra", output_tokens=1_000_000,
                              date="2026-09-10")
    plan = plan_for(w, [finding("premium-model-share", model="gpt-6-astra")],
                    include_tradeoffs=True, grain="day")
    assert not has_lever(plan, POLICY)
    assert "remap gpt-6-astra: 1 cells unpriced at the target" in plan.headline_monthly.note


def test_remap_pairs_follow_the_findings_models() -> None:
    w = _deep_overage()
    w.usage(100_000, model="Claude Opus 4.8", output_tokens=40_000_000)
    w.usage(5_000, model="Claude Opus 4.7", output_tokens=2_000_000)
    one = plan_for(w, [finding("premium-model-share", model="claude-opus-4-7")],
                   include_tradeoffs=True)
    assert [lv.params for lv in one.levers if lv.basis is Basis.LIST] == [
        "copilot:remap=claude-sonnet-5@model:claude-opus-4-7"]
    both = plan_for(w, [finding("premium-model-share", team="t1")], include_tradeoffs=True)
    assert [lv.params for lv in both.levers if lv.basis is Basis.LIST] == [
        "copilot:remap=claude-sonnet-5@model:claude-opus-4-7",
        "copilot:remap=claude-sonnet-5@model:claude-opus-4-8"]


def test_compliance_run_flag_prices_the_remap_at_the_uplifted_rates() -> None:
    w = _deep_overage().usage(100_000, model="Claude Opus 4.8", output_tokens=40_000_000)
    w.flags(compliance="data_residency")
    pol = lever(plan_for(w, [finding("premium-model-share", team="t1")],
                         include_tradeoffs=True), POLICY)
    assert pol.shapley.nano == 66_000 * C          # 60,000 credits × 1.1


# ---------- Shapley method, params, results ----------


def _eight_player_world() -> World:
    w = _deep_overage()
    for label in ("Claude Opus 4.7", "Claude Opus 4.8", "Claude Opus 5", "Claude Opus 5.5",
                  "Claude Fable 5", "Claude Fable 5.1"):
        w.usage(10_000, model=label, output_tokens=1_000_000)
    w.usage(29, model="Claude Opus 4.8 (fast mode)", output_tokens=5_800)
    return w


def test_more_than_six_players_use_seeded_monte_carlo() -> None:
    w = _eight_player_world()
    fs = [finding("premium-model-share", team="t1"), finding("auto-adoption", team="t1"),
          finding("fast-mode", team="t1")]
    plan = plan_for(w, fs, include_tradeoffs=True)
    listed = [lv for lv in plan.levers if lv.basis is Basis.LIST]
    assert len(listed) == 8 > MAX_EXACT_PLAYERS
    assert plan.method == "shapley-mc"
    assert sorted(k for k, _ in plan.shapley_se) == sorted(lv.params for lv in listed)
    assert sum(lv.shapley.nano for lv in listed) == plan.joint_saving.nano
    assert plan_for(w, fs, include_tradeoffs=True) == plan          # seeded: deterministic
    assert MC_PERMUTATIONS == 200


def test_every_params_round_trips_through_the_aggregate_grammar() -> None:
    w = _eight_player_world()
    w.actions("100")
    fs = [finding("premium-model-share", team="t1"), finding("auto-adoption", team="t1"),
          finding("fast-mode", team="t1"), finding("larger-runner", entity="enterprise"),
          idle(5)]
    plan = plan_for(w, fs, include_tradeoffs=True)
    assert {lv.lever_id for lv in plan.levers} == {AUTO, POLICY, FAST, SEAT, RUNNER}
    for lv in plan.levers:
        spec = catalog.parse_aggregate_spec(lv.params)
        assert catalog.to_aggregate_spec(spec) == lv.params
        assert catalog.lever(lv.lever_id).replay == "aggregate"
        assert lv.lever_class == catalog.lever(lv.lever_id).lever_class
        for fig in (lv.standalone, lv.shapley, lv.projected_monthly):
            assert fig.evidence is Evidence.ESTIMATED and fig.basis is lv.basis


def test_results_groups_and_headroom_totals() -> None:
    w = p1_world(1_000_000).ide("t1", {"ide:vscode": 1})
    plan = plan_for(w, [idle(10), finding("auto-adoption", team="t1")])
    groups = dict(plan.groups)
    assert groups == {"copilot": (AUTO, SEAT), "copilot:pool_headroom": (AUTO, SEAT)}
    heads = [lv for lv in plan.levers if lv.basis is Basis.LIST_EQUIVALENT]
    assert {lv.group for lv in heads} == {"copilot:pool_headroom"}
    assert plan.pool_headroom_monthly.nano == sum(lv.projected_monthly.nano for lv in heads)
    # slack: Auto's 100,000 credits are headroom, the seats save $190
    assert lever(plan, AUTO, Basis.LIST_EQUIVALENT).shapley.nano == 100_000 * C
    assert lever(plan, SEAT).shapley.nano == 190 * USD


def test_seat_threshold_tie_keeps_the_first_grid_value() -> None:
    plan = plan_for(p1_world(3_100_000), [idle(10)])     # overage: both thresholds worth 0
    assert lever(plan, SEAT).params == "copilot:seats_idle=30d@all"


def test_empty_inputs_give_a_zero_plan() -> None:
    w = World()
    plan = plan_copilot([], [], [], PRICER, lines=[], activity=[], month="2026-09")
    assert plan.levers == () and plan.joint_saving.nano == 0
    assert plan.sample == "copilot cells, 0 cells, month 2026-09"
    assert plan.headline_monthly.evidence is Evidence.ESTIMATED
    got = plan_copilot_scenarios([], [], [], PRICER, lines=[], activity=[], config=[],
                                 month=w.month)
    assert got == (("known", plan),)


# ---------- determinism ----------


def _rich() -> tuple[World, list]:
    w = p1_world(2_600_000, team="t1").usage(400_000, team="t2", users=3)
    w.usage(100_000, model="Claude Opus 4.8 (fast mode)", output_tokens=20_000_000, team="t2")
    w.usage(50_000, model="Claude Opus 4.8", output_tokens=20_000_000)
    w.usage(20_000, unattributed=True, discount=5_000)
    w.actions("300")
    w.ide("t1", {"ide:intellij": 3, "ide:vscode": 7}).ide("t2", {"ide:vscode": 1}, user="b")
    fs = [idle(20, team_assigned=3), idle(5, team="t2", unknown=2),
          finding("auto-adoption", team="t1"), finding("auto-adoption", team="t2"),
          finding("fast-mode", team="t2"), finding("premium-model-share", team="t1"),
          finding("larger-runner", entity="enterprise")]
    return w, fs


def test_identical_plans_under_any_input_order() -> None:
    w, fs = _rich()
    cells, pools = w.cells(grain="day"), w.pools()
    base = plan_copilot(cells, pools, fs, PRICER, lines=w.lines, activity=w.activity,
                        month=w.month, include_tradeoffs=True, config=w.config)
    rng = random.Random(7)
    for _ in range(4):
        args = [list(x) for x in (cells, pools, fs, w.lines, w.activity, w.config)]
        for a in args:
            rng.shuffle(a)
        got = plan_copilot(args[0], args[1], args[2], PRICER, lines=args[3], activity=args[4],
                           month=w.month, include_tradeoffs=True, config=args[5])
        assert got == base
    assert len([lv for lv in base.levers if lv.basis is Basis.LIST]) == 5


# ---------- validation ----------


def test_invalid_inputs_raise_usage_errors() -> None:
    w = p1_world(1_000)
    cells, pools = w.cells(), w.pools()

    def call(**kw: object) -> None:
        args = {"cells": cells, "pools": pools, "findings": [], "pricer": PRICER}
        opts = {"lines": w.lines, "activity": [], "month": w.month}
        for k, v in kw.items():
            (args if k in args else opts)[k] = v
        plan_copilot(args["cells"], args["pools"], args["findings"], args["pricer"],
                     **opts)  # type: ignore[arg-type]

    for bad in ({"month": "2026-13"}, {"month": "0000-01"}, {"month": 202609},
                {"cells": ["x"]}, {"pools": [object()]}, {"findings": [1]},
                {"lines": "abc"}, {"activity": [None]}, {"pricer": object()},
                {"pools": pools + pools}):
        with pytest.raises(UsageError):
            call(**bad)
    with pytest.raises(UsageError):
        plan_copilot_scenarios(cells, pools, [], PRICER, lines=w.lines, activity=[], config=[],
                               month="2026-9")

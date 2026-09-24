"""Appendix C.P1–P5 through the aggregate plan: the pool rule turns lever savings into invoice
dollars (R11) and the rest into pool headroom; seat and credit levers substitute by regime."""

from __future__ import annotations

from fractions import Fraction

from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.shapley import shapley_exact

from .worlds import C, USD, World, finding, has_lever, idle, lever, p1_world, plan_for

SEAT = "copilot.seat_reclaim"
AUTO = "copilot.default_model_auto"
FAST = "copilot.fast_mode_off"
POLICY = "copilot.model_policy"


def _p5_world(auto_routed: int) -> World:
    """C.P1 seats; 1,000,000 direct-routed eligible credits (Auto saves 10% = 100,000 credits
    = $1,000 list-equivalent at reach 1) plus *auto_routed* credits already on Auto."""
    w = World().seats("business", 1000).seats("enterprise", 200)
    w.usage(1_000_000, users=5).usage(auto_routed, model="Auto: Claude Sonnet 5", users=5)
    return w.ide("t1", {"ide:vscode": 100})


# ---------- C.P1–P4: the seat lever by regime ----------


def test_p2_seat_lever_in_the_overage_entity_is_worth_zero_and_frees_no_headroom() -> None:
    plan = plan_for(p1_world(3_100_000), [idle(50)])
    seat = lever(plan, SEAT)
    assert seat.params == "copilot:seats_idle=30d@all"
    assert seat.shapley.nano == 0 and seat.standalone.nano == 0
    assert seat.projected_monthly.nano == 0
    assert plan.headline_monthly.nano == 0 and plan.joint_saving.nano == 0
    assert plan.pool_headroom_monthly is not None and plan.pool_headroom_monthly.nano == 0
    assert not any(lv.basis is Basis.LIST_EQUIVALENT for lv in plan.levers)  # headroom 0
    assert "regime enterprise overage" in seat.shapley.note


def test_p3_seat_lever_in_the_slack_entity_saves_950() -> None:
    plan = plan_for(p1_world(2_000_000), [idle(50)])
    seat = lever(plan, SEAT)
    assert seat.shapley.nano == 950 * USD == seat.standalone.nano
    assert seat.projected_monthly.nano == 950 * USD            # rate lever: RR 1 / 1 / 1
    assert plan.headline_monthly.nano == 950 * USD
    assert plan.headline_monthly.evidence is Evidence.ESTIMATED
    assert plan.headline_monthly.basis is Basis.LIST
    assert plan.joint_saving.nano == 950 * USD
    assert not seat.needs_eval and not seat.upper_bound


def test_p4_seat_lever_straddling_saves_300() -> None:
    plan = plan_for(p1_world(2_650_000), [idle(50)])
    assert lever(plan, SEAT).shapley.nano == 300 * USD


# ---------- C.P5: a credit saving by regime ----------


def test_p5_credit_saving_in_overage_is_all_invoice() -> None:
    plan = plan_for(_p5_world(2_100_000), [finding("auto-adoption", team="t1")])
    auto = lever(plan, AUTO)
    assert auto.standalone.nano == auto.shapley.nano == 1_000 * USD
    head = lever(plan, AUTO, Basis.LIST_EQUIVALENT)
    assert head.shapley.nano == 0 and head.standalone.nano == 0
    assert plan.pool_headroom_monthly.nano == 0


def test_p5_credit_saving_with_60000_overage_splits_600_invoice_400_headroom() -> None:
    plan = plan_for(_p5_world(1_740_000), [finding("auto-adoption", team="t1")])
    assert lever(plan, AUTO).shapley.nano == 600 * USD
    assert lever(plan, AUTO, Basis.LIST_EQUIVALENT).shapley.nano == 400 * USD
    assert plan.pool_headroom_monthly.basis is Basis.LIST_EQUIVALENT
    assert "not invoice dollars" in plan.pool_headroom_monthly.note


def test_p5_credit_saving_in_slack_is_all_headroom() -> None:
    plan = plan_for(_p5_world(1_000_000), [finding("auto-adoption", team="t1")])
    assert lever(plan, AUTO).shapley.nano == 0
    assert lever(plan, AUTO, Basis.LIST_EQUIVALENT).shapley.nano == 1_000 * USD
    # the Auto lever is a trajectory lever: projected = φ × RR (−0.2 / 0.5 / 1.0)
    proj = lever(plan, AUTO, Basis.LIST_EQUIVALENT).projected_monthly
    assert (proj.nano, proj.low_nano, proj.high_nano) == (500 * USD, -200 * USD, 1_000 * USD)
    assert plan.pool_headroom_monthly.nano == 500 * USD
    assert plan.headline_monthly.nano == 0          # headroom never enters the headline


# ---------- slack variant: seats save dollars, credit levers only headroom ----------


def test_slack_variant_seat_950_and_credit_levers_invoice_zero_with_headroom() -> None:
    w = p1_world(1_900_000)
    # fast-mode Opus 4.8: 20M output tokens = $1,000 fast (100,000 credits), $500 standard
    w.usage(100_000, model="Claude Opus 4.8 (fast mode)", output_tokens=20_000_000)
    w.ide("t1", {"ide:vscode": 10})
    fs = [idle(50), finding("auto-adoption", team="t1"), finding("fast-mode", team="t1")]
    plan = plan_for(w, fs)
    assert lever(plan, SEAT).shapley.nano == 950 * USD
    assert lever(plan, AUTO).shapley.nano == 0 and lever(plan, FAST).shapley.nano == 0
    fast_h = lever(plan, FAST, Basis.LIST_EQUIVALENT)
    auto_h = lever(plan, AUTO, Basis.LIST_EQUIVALENT)
    assert fast_h.standalone.nano == 50_000 * C                    # the fast premium
    assert auto_h.standalone.nano == 200_000 * C                   # 10% of 2,000,000 eligible
    # joint list-equivalent saving: premium + 10% of the rest after fast off
    joint = 50_000 * C + (1_900_000 + 50_000) * C // 10
    heads = [lv for lv in plan.levers if lv.basis is Basis.LIST_EQUIVALENT]
    assert sum(lv.shapley.nano for lv in heads) == joint
    assert lever(plan, SEAT, Basis.LIST_EQUIVALENT).shapley.nano == 0


# ---------- Auto + model policy in the overage entity: efficiency to the nano ----------


def _auto_policy_world() -> World:
    w = p1_world(3_000_000)
    # Opus 4.8: 40M output tokens = $1,000 (100,000 credits); at Sonnet 5 $400 → saves 60,000
    w.usage(100_000, model="Claude Opus 4.8", output_tokens=40_000_000)
    return w.ide("t1", {"ide:vscode": 7})


def test_auto_and_model_policy_in_overage_shapley_sums_to_the_joint_value() -> None:
    fs = [finding("auto-adoption", team="t1"),
          finding("premium-model-share", team="t1", model="claude-opus-4-8")]
    plan = plan_for(_auto_policy_world(), fs, include_tradeoffs=True)
    auto, policy = lever(plan, AUTO), lever(plan, POLICY)
    assert policy.params == "copilot:remap=claude-sonnet-5@model:claude-opus-4-8"
    assert auto.shapley.nano + policy.shapley.nano == plan.joint_saving.nano
    # hand values: Auto alone 10% × 3,100,000 = 310,000 credits; remap alone 60,000; together
    # Auto sees the remapped cell at 40,000 → 304,000 + 60,000; interaction 6,000 split evenly
    assert policy.standalone.nano == 60_000 * C
    assert auto.standalone.nano == 310_000 * C
    assert plan.joint_saving.nano == 364_000 * C
    assert policy.shapley.nano == 57_000 * C and auto.shapley.nano == 307_000 * C
    assert "trade-off" in policy.shapley.note and policy.needs_eval


def test_three_lever_game_matches_shapley_exact_on_subplan_values() -> None:
    """v(S) of every coalition measured as the joint saving of a plan over S's findings alone
    (independent of the full game), then ``core.shapley.shapley_exact``: the plan's credits
    equal it exactly, including the Fraction rounding."""
    w = p1_world(2_600_000)
    w.usage(100_000, model="Claude Opus 4.8 (fast mode)", output_tokens=20_000_000)
    w.ide("t1", {"ide:vscode": 3})
    by_lever = {SEAT: idle(50), AUTO: finding("auto-adoption", team="t1"),
                FAST: finding("fast-mode", team="t1")}
    full = plan_for(w, list(by_lever.values()))
    specs = {lv.lever_id: lv.params for lv in full.levers}
    values: dict[frozenset[str], int] = {frozenset(): 0}
    for r in (1, 2, 3):
        from itertools import combinations
        for combo in combinations(sorted(by_lever), r):
            sub = plan_for(w, [by_lever[x] for x in combo])
            values[frozenset(specs[x] for x in combo)] = sub.joint_saving.nano
    expected = shapley_exact(sorted(specs.values()), lambda s: values[s])
    got = {lv.params: lv.shapley.nano for lv in full.levers if lv.basis is Basis.LIST}
    assert got == expected
    assert sum(got.values()) == full.joint_saving.nano
    # non-trivial game: the straddling pool makes seat and credit levers interact
    assert values[frozenset(specs.values())] != sum(values[frozenset({s})]
                                                     for s in specs.values())
    assert Fraction(sum(got.values())) == Fraction(values[frozenset(specs.values())])


def test_headline_sums_projected_invoice_credits_only() -> None:
    fs = [idle(50), finding("auto-adoption", team="t1")]
    plan = plan_for(_p5_world(1_000_000), fs)
    listed = [lv for lv in plan.levers if lv.basis is Basis.LIST]
    assert plan.headline_monthly.nano == sum(lv.projected_monthly.nano for lv in listed)
    assert has_lever(plan, SEAT) and has_lever(plan, AUTO)
    assert "standalone values never summed" in plan.headline_monthly.note
    assert plan.method == "shapley-exact" and plan.shapley_se == ()
    assert plan.allowance_headroom_monthly is None
    assert dict(plan.groups)["copilot"] == (AUTO, SEAT)

"""Uncertainty in the plan: an unknown cost-center cap policy (Appendix C.P7), open-month forecasts
(C.P9 series), the larger-runner range (§10.2) and entities whose pool is unknown."""

from __future__ import annotations

from tokenbill.core.labels import Basis

from .worlds import USD, C, World, finding, has_lever, idle, lever, plan_for

FAST = "copilot.fast_mode_off"
SEAT = "copilot.seat_reclaim"
RUNNER = "copilot.agent_runner_standard"


def _p7_world(policy: str | None) -> World:
    """C.P7: capped cost center A (50 Business seats, cap 95,000) uses 130,000 credits — 29,000
    of them fast-mode Opus 4.8 (5.8M output tokens: a 14,500-credit fast premium) — the others
    2,000,000; enterprise pool 2,680,000 (1,000 Business + 200 Enterprise seats)."""
    w = World().seats("business", 950).seats("business", 50, cost_center="A")
    w.seats("enterprise", 200).cap("A", 95_000, policy=policy)
    w.usage(2_000_000, users=4)
    w.usage(101_000, cost_center="A", team="ta", users=2)
    w.usage(29_000, model="Claude Opus 4.8 (fast mode)", cost_center="A", team="ta",
            output_tokens=5_800_000)
    return w


def test_p7_pools_partition_the_enterprise_pool() -> None:
    pools = {pm.entity_id: pm for pm in _p7_world(None).pools()}
    assert pools["cc:A"].pool_credits == "95000" and pools["cc:A"].capped_policy == "unknown"
    assert int(pools["enterprise"].pool_credits) + 95_000 == 2_680_000


def test_p7_unknown_cap_policy_headline_spans_block_and_continue() -> None:
    fs = [finding("fast-mode", team="ta", cost_center="A")]
    cont = plan_for(_p7_world("continue"), fs)
    block = plan_for(_p7_world("block"), fs)
    unknown = plan_for(_p7_world(None), fs)
    assert cont.headline_monthly.nano == 145 * USD and cont.headline_monthly.low_nano is None
    assert block.headline_monthly.nano == 0 and block.headline_monthly.low_nano is None
    head = unknown.headline_monthly
    assert head.low_nano <= block.headline_monthly.nano
    assert head.high_nano >= cont.headline_monthly.nano
    assert (head.nano, head.low_nano, head.high_nano) == (145 * USD, 0, 145 * USD)
    fast = lever(unknown, FAST)
    assert (fast.shapley.low_nano, fast.shapley.high_nano) == (0, 145 * USD)
    # under block the saved credits were blocked demand: headroom, not dollars
    assert lever(block, FAST, Basis.LIST_EQUIVALENT).shapley.nano == 14_500 * C
    assert lever(cont, FAST, Basis.LIST_EQUIVALENT).shapley.nano == 0


def test_p7_seat_removal_in_the_parent_changes_the_shared_pool() -> None:
    # the enterprise remainder: 2,000,000 + min(130,000, 95,000) = 2,095,000 of 2,680,000
    plan = plan_for(_p7_world("continue"), [idle(50)])
    assert lever(plan, SEAT).shapley.nano == 950 * USD


# ---------- forecasts (C.P9 binding series) ----------


def _p9_world(business_seats: int) -> World:
    """2026-09 open (today 2026-09-23, lag 3): the C.P9 daily series — month-end forecast point
    3,100,000, low 2,920,000, high 3,280,000 credits."""
    w = World().seats("business", business_seats)
    series = {f"2026-09-{d:02d}": 130_000 for d in (1, 2, 3, 4)}
    for d, v in zip((7, 8, 9, 10, 11, 14, 15, 16, 17, 18),
                    (110_000, 110_000, 120_000, 120_000, 130_000, 130_000, 140_000, 140_000,
                     150_000, 150_000), strict=True):
        series[f"2026-09-{d:02d}"] = v
    series.update({"2026-09-05": 30_000, "2026-09-06": 30_000, "2026-09-12": 20_000,
                   "2026-09-13": 30_000, "2026-09-19": 30_000, "2026-09-20": 40_000})
    for date, credits in sorted(series.items()):
        w.usage(credits, date=date)
    return w


def test_forecast_consumption_levels_span_the_seat_saving() -> None:
    # pool 1,600 × 1,900 = 3,040,000: p10 in slack, p50 and p90 in overage
    w = _p9_world(1600)
    pools = w.pools(today="2026-09-23", grain="day")
    fc = pools[0].forecast
    assert (fc.nano, fc.low_nano, fc.high_nano) == (3_100_000 * C, 2_920_000 * C, 3_280_000 * C)
    plan = plan_for(w, [idle(50)], forecast=True, pools=pools, grain="day", today="2026-09-23")
    seat = lever(plan, SEAT).shapley
    assert (seat.nano, seat.low_nano, seat.high_nano) == (0, 0, 950 * USD)
    assert "month-end consumption from the pool forecast" in plan.headline_monthly.note


def test_open_month_without_forecast_uses_month_to_date_consumption() -> None:
    w = _p9_world(1600)
    pools = w.pools(today="2026-09-23", grain="day")
    plan = plan_for(w, [idle(50)], pools=pools, grain="day", today="2026-09-23")
    assert lever(plan, SEAT).shapley.nano == 950 * USD      # 2,000,000 observed: slack
    assert "open month: month-to-date consumption" in plan.headline_monthly.note


def test_day_and_month_grain_cells_give_the_same_closed_month_plan() -> None:
    w = World().seats("business", 1000).seats("enterprise", 200)
    for day in ("2026-09-03", "2026-09-17"):
        w.usage(1_300_000, date=day, users=3)
        w.usage(50_000, model="Claude Opus 4.8 (fast mode)", date=day,
                output_tokens=10_000_000)
    fs = [idle(50), finding("fast-mode", team="t1")]
    by_day = plan_for(w, fs, grain="day")
    by_month = plan_for(w, fs, grain="month")
    assert [lv.shapley for lv in by_day.levers] == [lv.shapley for lv in by_month.levers]
    assert by_day.sample == "copilot cells, 4 cells, month 2026-09"
    assert by_month.sample == "copilot cells, 2 cells, month 2026-09"


# ---------- runners ----------


def _runner_world() -> World:
    w = World().seats("business", 100).usage(100_000)
    w.actions("1000")                                      # linux_16_core: $42 net
    w.actions("500", sku="actions_linux", usd_per_minute="0.006")      # standard: ignored
    w.actions("100", workload=None)                        # not a Copilot workload: ignored
    return w


def test_runner_lever_range_low_net_minus_standard_high_net() -> None:
    fs = [finding("larger-runner", entity="enterprise")]
    plan = plan_for(_runner_world(), fs, include_tradeoffs=True)
    runner = lever(plan, RUNNER)
    assert runner.params == "copilot:runner=actions_linux@all"
    # 1,000 minutes: $42 − 1,000 × $0.006 = $36 (point = low), high $42
    assert (runner.shapley.nano, runner.shapley.low_nano, runner.shapley.high_nano) == (
        36 * USD, 36 * USD, 42 * USD)
    assert "point assumes no included minutes left" in runner.shapley.note
    assert not has_lever(plan_for(_runner_world(), fs), RUNNER)     # trade-off lever


def test_runner_line_without_minutes_has_low_zero() -> None:
    w = World().seats("business", 100).usage(100_000)
    w.actions("1000")
    line = w.lines[-1]
    from dataclasses import replace
    w.lines[-1] = replace(line, quantity=None)
    plan = plan_for(w, [finding("larger-runner", entity="enterprise")], include_tradeoffs=True)
    runner = lever(plan, RUNNER).shapley
    assert (runner.nano, runner.low_nano, runner.high_nano) == (0, 0, 42 * USD)
    assert "1 larger-runner lines without minutes: low bound 0" in plan.headline_monthly.note


# ---------- unknown pools ----------


def test_every_entity_with_an_unknown_pool_makes_the_plan_unpriced() -> None:
    from dataclasses import replace
    w = World().seats("business", 10).usage(500_000).ide("t1", {"ide:vscode": 1})
    # an open month whose only days are still inside the report lag: regime unknown
    pools = [replace(pm, regime="unknown") for pm in w.pools()]
    plan = plan_for(w, [finding("auto-adoption", team="t1")], pools=pools)
    assert plan.headline_monthly.nano is None
    assert plan.headline_monthly.note.startswith("unpriced:")
    assert "pool regime unknown for every entity" in plan.headline_monthly.note
    assert plan.joint_saving.nano is None and plan.pool_headroom_monthly.nano is None
    assert plan.levers == ()                  # its only cells are the unknown pool's credits


def test_direct_rows_of_an_unknown_pool_entity_stay_in_dollars() -> None:
    """No seat data and only org-metered rows: the pool is unknown, the direct dollars are not."""
    w = World().usage(500_000, unattributed=True).ide("t1", {"ide:vscode": 1})
    pools = w.pools()
    assert pools[0].regime == "unknown"
    plan = plan_for(w, [finding("auto-adoption")], pools=pools)
    auto = lever(plan, "copilot.default_model_auto")
    assert auto.shapley.nano == 500 * USD           # 10% of 500,000 credits, metered
    assert "excluded (pool regime unknown" in plan.headline_monthly.note


def test_an_entity_with_an_unknown_pool_is_excluded_and_named() -> None:
    from dataclasses import replace
    w = World(entity_mode="org")
    w.seats("business", 1000, org="org-a")
    w.usage(1_500_000, org="org-a", model="Auto: Claude Sonnet 5")        # slack, on Auto
    w.seats("business", 100, org="org-b").usage(300_000, org="org-b", team="t2")
    pools = [replace(pm, regime="unknown") if pm.entity_id == "org:org-b" else pm
             for pm in w.pools()]
    fs = [idle(50, entity="org:org-a"), idle(5, entity="org:org-b", team="t2"),
          finding("auto-adoption", team="t2")]
    plan = plan_for(w, fs, pools=pools)
    assert lever(plan, SEAT).shapley.nano == 950 * USD      # org B's seats are not valued
    assert "excluded (pool regime unknown; savings on their pooled credits are unpriced, not " \
        "zero; their direct org-metered rows are kept): org:org-b" in plan.headline_monthly.note
    assert not has_lever(plan, "copilot.default_model_auto")    # its cells were all excluded


def test_cells_without_a_pool_month_are_named() -> None:
    w = World().seats("business", 1000).usage(2_000_000)
    other = World(entity_mode="org").usage(10, org="org-z")
    pools = w.pools()
    plan = plan_for(w, [idle(50)], pools=pools)
    from tokenbill.copilot.plan import plan_copilot

    from .worlds import PRICER
    plan2 = plan_copilot(w.cells() + other.cells(), pools, [idle(50)], PRICER, lines=w.lines,
                         activity=[], month=w.month)
    assert lever(plan2, SEAT).shapley == lever(plan, SEAT).shapley
    assert "cells without a pool month (not planned): org:org-z" in plan2.headline_monthly.note

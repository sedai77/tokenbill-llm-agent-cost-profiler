"""Cells (addendum §9.2, CA-40; F-POOL brief item 1) and the discount classes (DC22, §19.5 #7)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tokenbill.core import builders as b
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.pool import Cell, build_cells, classify_discounts, direct_draws_pool
from tokenbill.core.records import UsageBuckets

from .worlds import C, people, rows


def _cell(**kw: object) -> Cell:
    base: dict[str, object] = dict(
        month="2026-10", date_utc="2026-10-05", entity_id="enterprise", team=None,
        cost_center=None, org="org-a", sku="copilot_ai_credit", cost_type="ai_credit.user",
        model="claude-sonnet-5", routing="direct", speed="standard", pseudo=None, workload=None,
        usage=UsageBuckets(), credits="100", gross_nano=100 * C, discount_nano=0,
        net_nano=100 * C, n_users=1, final=True)
    base.update(kw)
    if "discount_nano" in kw and "net_nano" not in kw:
        base["net_nano"] = base["gross_nano"] - base["discount_nano"]  # type: ignore[operator]
    return Cell(**base)  # type: ignore[arg-type]


def test_sums_user_counts_and_token_split() -> None:
    lines, aggs = rows(300, date="2026-10-05", users=people(3), discount=120, tokens=9_000)
    d_lines, d_aggs = rows(100, date="2026-10-05", unattributed=True, tokens=9_000)
    cells, clamped = build_cells(aggs + d_aggs, lines + d_lines)
    assert clamped == 0
    user, direct = sorted(cells, key=lambda c: c.cost_type, reverse=True)
    assert (user.cost_type, user.credits, user.gross_nano, user.discount_nano, user.net_nano,
            user.n_users, user.final) == ("ai_credit.user", "300", 300 * C, 120 * C, 180 * C, 3,
                                          True)
    assert (direct.cost_type, direct.credits, direct.n_users) == ("ai_credit.direct", "100", 0)
    # four rows share one aggregate key: 36,000 input tokens split 3:1 by gross
    assert (user.usage.uncached_input, direct.usage.uncached_input) == (27_000, 9_000)
    assert user.usage.output + direct.usage.output == 3_600
    assert (user.entity_id, user.org, user.model, user.routing, user.speed) == (
        "enterprise", "org-a", "claude-sonnet-5", "direct", "standard")


def test_incl_conversion_equals_hand_values_and_clamps() -> None:
    line, agg = b.make_ai_usage_row(date_utc="2026-10-05", credits="10", input_tokens=1_000,
                                    cache_read_tokens=300, cache_write_tokens=200,
                                    output_tokens=50)
    [cell], clamped = build_cells([agg], [line], convention="incl")
    assert (cell.usage.uncached_input, cell.usage.cache_read, cell.usage.cache_write_unknown,
            cell.usage.output, clamped) == (500, 300, 200, 50, 0)
    [excl], _ = build_cells([agg], [line])
    assert excl.usage.uncached_input == 1_000
    line2, agg2 = b.make_ai_usage_row(date_utc="2026-10-06", credits="1", input_tokens=100,
                                      cache_read_tokens=300)
    cells, clamped = build_cells([agg, agg2], [line, line2], convention="incl")
    assert clamped == 1 and [c.usage.uncached_input for c in cells] == [500, 0]


def test_capped_cost_center_rows_and_org_mode() -> None:
    lines, aggs = rows(10, date="2026-10-05", cost_center="A")
    other, other_aggs = rows(10, date="2026-10-05", cost_center="B", org="org-b")
    cells, _ = build_cells(aggs + other_aggs, lines + other, capped={"A": Decimal(95_000)})
    assert sorted(c.entity_id for c in cells) == ["cc:A", "enterprise"]
    cells, _ = build_cells(aggs + other_aggs, lines + other, capped={"A": Decimal(1)},
                           entity_mode="org")
    assert sorted(c.entity_id for c in cells) == ["cc:A", "org:org-b"]


def test_rows_differing_only_in_pseudo_stay_separate() -> None:
    review, r_agg = rows(10, date="2026-10-05", model="Copilot Code Review", tokens=100)
    agent, a_agg = rows(20, date="2026-10-05", model="Copilot Coding Agent", tokens=100)
    cells, _ = build_cells(r_agg + a_agg, review + agent)
    assert [(c.model, c.pseudo, c.workload, c.credits) for c in cells] == [
        ("", "cloud_agent", "copilot_cloud_agent", "20"),
        ("", "code_review", "copilot_code_review", "10")]


def test_month_grain_finality_and_filters() -> None:
    a, a_agg = rows(10, date="2026-10-05")
    p, p_agg = rows(5, date="2026-10-20", finality="provisional")
    legacy = b.make_ai_usage_row(date_utc="2026-10-05", sku="copilot_premium_request",
                                 credits="4")[0]
    foreign = b.make_cost_line(7, date_utc="2026-10-05")                 # Anthropic line
    seat = b.make_seat_line("business", "3", date_utc="2026-10-01")      # not a cell
    cov = b.make_aggregate({"output": 5}, source_kind="github.ai_usage_report.coverage")
    cells, _ = build_cells(a_agg + p_agg + [cov], a + p + [legacy, foreign, seat], grain="month")
    assert [(c.date_utc, c.cost_type, c.credits, c.final) for c in cells] == [
        (None, "ai_credit.user", "15", False), (None, "ai_credit.legacy_pru", "4", True)]


def test_tokens_without_cost_lines_and_lines_without_quantity() -> None:
    _, [agg] = rows(10, date="2026-10-05", tokens=500)
    [cell], _ = build_cells([agg], [])
    assert (cell.cost_type, cell.credits, cell.gross_nano, cell.usage.uncached_input) == (
        "other", "0", 0, 500)
    line = replace(b.make_ai_usage_row(date_utc="2026-10-05", credits="2.5")[0], quantity=None)
    [cell], _ = build_cells([], [line])
    assert cell.credits == "2.5" and cell.usage == UsageBuckets()
    zero_gross = [replace(x, amount_nano=0, list_amount_nano=0) for x in
                  rows(0, date="2026-10-05", users=people(2), tokens=10)[0]]
    d_line, d_agg = rows(0, date="2026-10-05", unattributed=True, tokens=10)
    _, aggs = rows(0, date="2026-10-05", users=people(2), tokens=10)
    cells, _ = build_cells(aggs + d_agg, zero_gross + d_line)
    by_type = {c.cost_type: c.usage.uncached_input for c in cells}
    assert by_type == {"ai_credit.user": 30, "ai_credit.direct": 0}   # zero gross → pooled cell


def test_reasoning_and_other_write_buckets_split() -> None:
    lines, _ = rows(30, date="2026-10-05")
    d_lines, _ = rows(10, date="2026-10-05", unattributed=True)
    _, [agg] = rows(0, date="2026-10-05")
    agg = replace(agg, usage=UsageBuckets(output=10, output_reasoning=10, cache_write_other=7,
                                          cache_write_other_ttl_s=1800))
    cells, _ = build_cells([agg], lines + d_lines)
    assert sum(c.usage.output_reasoning or 0 for c in cells) <= 10
    assert all((c.usage.output_reasoning or 0) <= c.usage.output for c in cells)
    assert sum(c.usage.cache_write_other for c in cells) == 7


def test_build_cells_rejects_bad_arguments() -> None:
    with pytest.raises(UsageError):
        build_cells([], [], grain="week")
    with pytest.raises(UsageError):
        build_cells([], [], convention="undecidable")
    with pytest.raises(UsageError):
        build_cells([], [], entity_mode="team")
    with pytest.raises(UsageError):
        build_cells(["x"], [])  # type: ignore[list-item]
    with pytest.raises(UsageError):
        build_cells([], ["x"])  # type: ignore[list-item]
    far = b.make_aggregate({"output": 1}, source_kind="github.ai_usage_report",
                           bucket_start_ms=2**53, bucket_end_ms=2**53,
                           dims={"channel": "github_copilot"})
    with pytest.raises(UsageError):     # valid record, but past the last calendar date
        build_cells([far], [])


@pytest.mark.parametrize("field,value", [
    ("month", "2026-13"), ("date_utc", "2026-11-01"), ("entity_id", "budget:1"), ("team", 3),
    ("sku", None), ("cost_type", "tokens"), ("usage", {}), ("credits", "1e3"),
    ("gross_nano", 1.0), ("discount_nano", 5), ("n_users", -1), ("final", 1)])
def test_cell_validation(field: str, value: object) -> None:
    with pytest.raises(ContractViolation):
        replace(_cell(), **{field: value})


# ---------- discount classes (DC22) and direct pool draw (§19.5 #7) ----------


def test_classify_discounts() -> None:
    pooled = [_cell(discount_nano=90 * C), _cell(date_utc="2026-10-06", discount_nano=10 * C)]
    assert classify_discounts(pooled, gross_is_list=True, pool_nano=100 * C) == (100 * C, 0, 0)
    assert classify_discounts(pooled, gross_is_list=True, pool_nano=99 * C) == (None, 0, 100 * C)
    assert classify_discounts(pooled, gross_is_list=None, pool_nano=10**15) == (None, 0, 100 * C)
    assert classify_discounts(pooled, gross_is_list=False, pool_nano=10**15) == (None, 0, 100 * C)
    auto = _cell(routing="auto", gross_nano=200 * C, discount_nano=20 * C)     # net 180 > 0
    auto_pool = _cell(routing="auto", gross_nano=50 * C, discount_nano=50 * C)  # net 0: pool
    legacy = _cell(cost_type="ai_credit.legacy_pru", discount_nano=40 * C)
    got = classify_discounts(pooled + [auto, auto_pool, legacy], gross_is_list=True,
                             pool_nano=10**15)
    assert got == (150 * C, 20 * C, 0)
    assert classify_discounts([auto], gross_is_list=None, pool_nano=0) == (None, 20 * C, 0)
    assert classify_discounts([], gross_is_list=True, pool_nano=0) == (0, 0, 0)


def test_direct_draws_pool() -> None:
    pooled = _cell(discount_nano=10 * C)
    direct0 = _cell(cost_type="ai_credit.direct", discount_nano=0, n_users=0)
    direct_paid = _cell(cost_type="ai_credit.direct", discount_nano=5 * C, n_users=0)
    other_day = replace(direct0, date_utc="2026-10-09")
    assert direct_draws_pool([pooled, direct_paid]) == "yes"
    assert direct_draws_pool([pooled, direct0]) == "no"
    assert direct_draws_pool([pooled, other_day]) == "unknown"
    assert direct_draws_pool([pooled]) == "unknown"

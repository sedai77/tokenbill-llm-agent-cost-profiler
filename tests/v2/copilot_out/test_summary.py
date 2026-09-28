"""Bill lines and labels (addendum §14.1, R16, R17) — acceptance C.P1, C.P13, C.P14."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tokenbill.copilot.summary import (
    DOLLAR_LINES,
    apportion,
    assemble_summary,
    capped_of,
    entity_mode_of,
)
from tokenbill.core import builders as b
from tokenbill.core import pool
from tokenbill.core.errors import TokenbillError, UsageError
from tokenbill.core.labels import Basis, Evidence, Finality
from tokenbill.core.types import ChannelVerdict, CopilotBillLine, CopilotSummary

from .worlds import (
    OPEN_TODAY,
    RECONCILED,
    WINDOW,
    World,
    action_plan,
    p1_world,
    p13_world,
    p14_world,
)

USD = 1_000_000_000


def lines_of(s: CopilotSummary, scenario: str | None = None) -> dict[str, CopilotBillLine]:
    return {bl.line: bl for bl in s.lines if bl.scenario == scenario}


# ---------------------------------------------------------------------------------------------
# C.P1
# ---------------------------------------------------------------------------------------------


def test_p1_closed_reconciled_month_is_invoice_31150_plus_actions() -> None:
    s = p1_world().summary()
    ln = lines_of(s)
    assert ln["seats.business"].amount.nano == 19_000 * USD
    assert ln["seats.enterprise"].amount.nano == 7_800 * USD
    assert ln["ai_credits.overage"].amount.nano == 4_200 * USD
    assert ln["ai_credits.direct_org"].amount.nano == 150 * USD
    for name in ("seats.business", "seats.enterprise", "ai_credits.overage",
                 "ai_credits.direct_org", "actions.code_review", "total.invoice"):
        assert ln[name].amount.basis is Basis.INVOICE, name
        assert ln[name].amount.evidence is Evidence.EXACT
    total = ln["total.invoice"]
    actions = ln["actions.code_review"].amount.nano
    assert total.amount.nano == 31_150 * USD + actions
    assert set(total.components) == {"seats.business", "seats.enterprise", "ai_credits.overage",
                                     "ai_credits.direct_org", "actions.code_review"}
    assert ln["ai_credits.gross"].amount.basis is Basis.LIST_EQUIVALENT
    assert "ai_credits.gross" not in total.components
    assert all(c in DOLLAR_LINES for c in total.components)


def test_p1_unreconciled_month_is_exact_list_named_unreconciled() -> None:
    s = p1_world().summary(verdicts={"github_copilot": "not_reconciled",
                                     "github_actions": "reconciled"})
    ln = lines_of(s)
    for name in ("seats.business", "ai_credits.overage", "total.invoice"):
        fig = ln[name].amount
        assert fig.basis is Basis.LIST and fig.evidence is Evidence.EXACT, name
    assert "unreconciled" in ln["seats.business"].amount.note
    assert "seats.business" in ln["total.invoice"].amount.note
    assert ln["actions.code_review"].amount.basis is Basis.INVOICE


def test_p1_without_seat_lines_prices_seats_at_list_estimated() -> None:
    s = p1_world(seat_lines=False).summary()
    ln = lines_of(s)
    seats = ln["seats.business"]
    assert seats.amount.evidence is Evidence.ESTIMATED and seats.amount.basis is Basis.LIST
    assert seats.amount.nano == 19_000 * USD and seats.quantity == "1000"
    assert "proration" in seats.amount.note
    total = ln["total.invoice"].amount
    assert total.evidence is Evidence.ESTIMATED and "seats" in total.note
    assert "seats.business (estimated list)" in total.note


def test_p1_open_month_has_provisional_list_overage_and_no_invoice() -> None:
    s = p1_world().summary(today=OPEN_TODAY)
    assert all(bl.amount.basis is not Basis.INVOICE for bl in s.lines)
    over = lines_of(s)["ai_credits.overage"].amount
    assert over.basis is Basis.LIST and over.finality is Finality.PROVISIONAL
    assert "observed so far" in over.note
    assert s.pools[0].finality == "open" and s.pools[0].forecast is not None


def test_channel_verdict_objects_and_mappings_are_accepted() -> None:
    w = p1_world()
    cv = [ChannelVerdict(channel=c, verdict=v, invoice_sources=(), mapping_verified=True)
          for c, v in RECONCILED.items()]
    s = w.summary(verdicts=cv)
    assert s.channel_verdicts == tuple(sorted(RECONCILED.items()))
    with pytest.raises(UsageError):
        w.summary(verdicts=[("github_copilot",)])


# ---------------------------------------------------------------------------------------------
# discounts (DC22)
# ---------------------------------------------------------------------------------------------


def test_unclassified_discount_is_never_pool_included() -> None:
    ln = lines_of(p1_world().summary())
    assert "ai_credits.discount_pool" not in ln
    unc = ln["ai_credits.discount_unclassified"].amount
    assert unc.basis is Basis.LIST and unc.nano == 26_800 * USD
    assert unc.note == "includes included usage and other discounts"


def test_classified_discount_is_list_equivalent_pool_line() -> None:
    w = p1_world()
    cells, _ = pool.build_cells(w.aggs, w.lines, grain="day")
    pools = pool.pool_months(cells, w.lines, [], [], today="2026-10-20", gross_is_list=True)
    s = assemble_summary(cost_lines=w.lines, aggregates=w.aggs, licenses=[], activity=[],
                         pools=pools, channel_verdicts=RECONCILED, window=WINDOW)
    ln = lines_of(s)
    assert "ai_credits.discount_unclassified" not in ln
    dp = ln["ai_credits.discount_pool"].amount
    assert dp.basis is Basis.LIST_EQUIVALENT and dp.nano == 26_800 * USD
    assert "ai_credits.discount_pool" not in ln["total.invoice"].components


def test_auto_discount_is_other() -> None:
    w = World()
    w.lines.append(b.make_seat_line("business", "10"))
    for i in range(6):
        w.usage("1000", i=i, discount="100", model="Auto: Claude Haiku 4.5")
    ln = lines_of(w.summary())
    other = ln["ai_credits.discount_other"].amount
    assert other.basis is Basis.LIST and other.nano == 6 * 100 * 10_000_000


# ---------------------------------------------------------------------------------------------
# C.P13 / C.P14 (plan unknown, conflict)
# ---------------------------------------------------------------------------------------------


def test_p13_two_scenario_blocks_never_combined() -> None:
    plans = (("business", action_plan()), ("enterprise", action_plan(headline=0)))
    s = p13_world().summary(plans_by_scenario=plans)
    bus, ent = lines_of(s, "business"), lines_of(s, "enterprise")
    assert bus["seats.unknown_plan"].amount.nano == 1_900 * USD
    assert bus["seats.unknown_plan"].quantity == "100"
    assert bus["ai_credits.overage"].amount.nano == 600 * USD
    assert bus["total.invoice"].amount.nano == 2_500 * USD
    assert ent["seats.unknown_plan"].amount.nano == 3_900 * USD
    assert ent["ai_credits.overage"].amount.nano == 0
    assert ent["total.invoice"].amount.nano == 3_900 * USD
    for block in (bus, ent):
        for name in ("seats.unknown_plan", "ai_credits.overage", "total.invoice"):
            assert block[name].amount.evidence is Evidence.ESTIMATED
    assert "plan unknown: priced as business" in bus["seats.unknown_plan"].amount.note
    shared = lines_of(s)
    assert "total.invoice" not in shared and "seats.unknown_plan" not in shared
    assert shared["ai_credits.gross"].amount.basis is Basis.LIST_EQUIVALENT
    assert s.plan is None and dict(s.plans_by_scenario).keys() == {"business", "enterprise"}
    assert s.plan_status[0].plan == "unknown"


def test_p13_known_plan_argument_is_dropped_while_unknown() -> None:
    s = p13_world().summary(plans_by_scenario={"known": action_plan()})
    assert s.plan is None and s.plans_by_scenario == ()


def test_p14_conflict_is_one_block_with_seat_line_fees() -> None:
    s = p14_world().summary(plans_by_scenario=(("known", action_plan()),))
    assert all(bl.scenario is None for bl in s.lines)
    assert len(s.pools) == 1 and s.pools[0].plan_conflict
    assert s.pools[0].pool_credits == "195000"
    assert lines_of(s)["seats.enterprise"].amount.nano == 1_950 * USD
    assert s.plan is not None and s.plan_status[0].conflict


# ---------------------------------------------------------------------------------------------
# k-anonymous team figures
# ---------------------------------------------------------------------------------------------


def test_teams_seat_counts_and_editor_split_are_published() -> None:
    s = p13_world().summary()
    assert s.teams is not None and s.teams.k == 5
    teams = {dict(r.dims)["team"] for r in s.teams.rows}
    assert "gamma" not in teams and any(t and t.startswith("(other") for t in teams)
    counts = {(t, pb): n for t, pb, n in s.seat_counts}
    assert counts[("alpha", "unknown:0-7")] == 52
    assert all(t != "gamma" for t, _, _ in s.seat_counts)
    assert sum(n for _, _, n in s.seat_counts) == 100
    split = {(dict(r.dims)["team"], dict(r.dims)["editor_family"]): r
             for r in s.editor_split.rows}
    assert ("beta", "vscode") in split and ("beta", "jetbrains") in split
    assert ("gamma", "other") not in split and split[("(other: <5 users)", "other")].n_users == 5
    assert split[("beta", "vscode")].priced.pool.evidence is Evidence.ESTIMATED


def test_editor_split_apportions_team_credits_exactly() -> None:
    s = p13_world().summary(k=1)
    rows = [r for r in s.editor_split.rows if dict(r.dims)["team"] == "beta"]
    assert sum(r.priced.pool.nano for r in rows) == 37 * 2_500 * 10_000_000


def test_no_activity_means_no_editor_split_and_no_lines_means_no_teams() -> None:
    s = assemble_summary(cost_lines=[], aggregates=[], licenses=[], activity=[], pools=[],
                         window=WINDOW)
    assert s.editor_split is None and s.teams is None and s.lines == () and s.seat_counts == ()


# ---------------------------------------------------------------------------------------------
# helpers and validation
# ---------------------------------------------------------------------------------------------


def test_apportion_sums_exactly_and_breaks_ties_by_order() -> None:
    assert apportion(10, [1, 1, 1]) == [4, 3, 3]
    assert apportion(0, [1, 2]) == [0, 0]
    assert apportion(7, [0, 0]) == [0, 0]
    assert apportion(-10, [1, 1, 1]) == [-4, -3, -3]
    assert sum(apportion(1_000_000_007, [3, 5, 11, 0])) == 1_000_000_007


def test_entity_mode_and_caps_come_from_the_pools() -> None:
    pm = b.make_pool_month(entity_id="org:acme")
    cc = b.make_pool_month(entity_id="cc:research", seats={"business": "10"})
    assert entity_mode_of([pm]) == "org" and entity_mode_of([cc]) == "enterprise"
    assert capped_of([pm, cc]) == {"research": Decimal(cc.pool_credits)}


def test_bad_arguments_raise_usage_errors() -> None:
    with pytest.raises(UsageError):
        assemble_summary(cost_lines=[], aggregates=[], licenses=[], activity=[], pools=[],
                         window=WINDOW, k=0)
    with pytest.raises(UsageError):
        assemble_summary(cost_lines=[], aggregates=[], licenses=[], activity=[], pools=["x"],
                         window=WINDOW)
    with pytest.raises(UsageError):
        assemble_summary(cost_lines=[], aggregates=[], licenses=[], activity=[], pools=[],
                         window=WINDOW, plans_by_scenario=[("maybe", action_plan())])
    with pytest.raises(UsageError):
        assemble_summary(cost_lines=[], aggregates=[], licenses=[], activity=[], pools=[],
                         window=WINDOW, plans=["x"])
    with pytest.raises(TokenbillError):
        assemble_summary(cost_lines=[], aggregates=[], licenses=[], activity=[], pools=[],
                         window=WINDOW, plans_by_scenario=[("known", "not a plan")])


def test_actions_without_copilot_workload_are_left_out_with_a_note() -> None:
    w = p1_world()
    w.lines.append(b.make_actions_line("10", workload=None, date_utc="2026-09-13"))
    s = w.summary()
    assert any("left out" in n for n in s.notes)


def test_adjacent_only_month_is_never_invoice() -> None:
    w = World()
    w.lines.append(b.make_actions_line("100", date_utc="2026-08-03"))
    w.lines.append(b.make_seat_line("business", "10", date_utc="2026-09-01"))
    s = w.summary()
    aug = [bl for bl in s.lines if bl.month == "2026-08"]
    assert {bl.line for bl in aug} == {"actions.code_review", "total.invoice"}
    assert all(bl.amount.basis is Basis.LIST for bl in aug)


def test_code_quality_lines_and_sandbox() -> None:
    w = p1_world()
    w.usage("500", i=5, sku="code_quality_ai_credit", team="alpha")
    base = b.make_actions_line("20", date_utc="2026-09-14")
    sandbox = replace(base, line_id="cl_sandbox", channel="github_sandbox", cost_type="sandbox",
                      sku="sandbox_linux", workload=None)
    lic = replace(base, line_id="cl_cq", channel="github_copilot",
                  cost_type="code_quality.license", sku="code_quality_licenses", workload=None)
    w.lines += [sandbox, lic]
    ln = lines_of(w.summary(verdicts={**RECONCILED, "github_sandbox": "insufficient_data"}))
    assert "code_quality.ai_credits" not in ln["total.invoice"].components
    assert "already inside" in ln["code_quality.ai_credits"].amount.note
    assert ln["sandbox"].amount.basis is Basis.LIST
    assert ln["code_quality.licenses"].amount.basis is Basis.INVOICE
    assert {"sandbox", "code_quality.licenses"} <= set(ln["total.invoice"].components)

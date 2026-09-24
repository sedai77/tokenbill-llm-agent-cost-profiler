"""Delivery reach of the Auto default (addendum §9.3; owner answer 3: developers on VS Code and
IntelliJ — managed ``model`` does not reach JetBrains) and the Auto transform (§9.2 step 3)."""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction

import pytest

from tokenbill.copilot.plan import EXCLUDED_EDITOR_FAMILIES, TeamReach, team_reach
from tokenbill.core import builders as b
from tokenbill.core import catalog
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis
from tokenbill.core.records import ActivityDay, ConfigSnapshot

from .worlds import USD, C, World, finding, lever, plan_for

AUTO = "copilot.default_model_auto"


def _overage_world() -> World:
    """C.P1 seats, 1,000,000 eligible direct credits on team t1 and 2,100,000 Auto-routed:
    overage 420,000 credits, so every Auto saving is invoice dollars."""
    w = World().seats("business", 1000).seats("enterprise", 200)
    return w.usage(1_000_000, users=4).usage(2_100_000, model="Auto: Claude Sonnet 5", users=4)


AUTO_FINDING = finding("auto-adoption", team="t1")


def test_team_with_20pct_jetbrains_gets_08_of_the_10pct_saving() -> None:
    w = _overage_world().ide("t1", {"ide:intellij": 20, "ide:vscode": 80})
    auto = lever(plan_for(w, [AUTO_FINDING]), AUTO)
    assert auto.standalone.nano == auto.shapley.nano == 800 * USD     # 0.8 × 100,000 credits
    assert auto.shapley.low_nano is None                               # reach known: no range
    assert "JetBrains 20% of interactions" in auto.shapley.note
    assert "managed model does not reach JetBrains" in auto.shapley.note
    assert "reach 80% of eligible credits" in auto.shapley.note


def test_no_activity_means_reach_unknown_point_zero_high_full() -> None:
    auto = lever(plan_for(_overage_world(), [AUTO_FINDING]), AUTO)
    for fig in (auto.standalone, auto.shapley):
        assert (fig.nano, fig.low_nano, fig.high_nano) == (0, 0, 1_000 * USD)
    assert "reach unknown (no activity data) for 100% of eligible credits" in auto.shapley.note
    # projected with the trajectory prior over the reach range
    proj = auto.projected_monthly
    assert (proj.nano, proj.low_nano, proj.high_nano) == (0, -200 * USD, 1_000 * USD)


def test_cloud_agent_cells_reach_one_without_activity() -> None:
    w = World().seats("business", 1000).seats("enterprise", 200)
    w.usage(2_900_000, model="Auto: Claude Sonnet 5", users=3)
    w.lines.append(b.make_ai_usage_row(date_utc="2026-09-11", model="Claude Sonnet 5",
                                       credits="200000", principal=b.make_principal("ca"),
                                       sku="coding_agent_ai_credit", team="t1")[0])
    auto = lever(plan_for(w, [AUTO_FINDING]), AUTO)
    assert auto.shapley.nano == 200 * USD and auto.shapley.low_nano is None


def test_code_review_cells_and_auto_routed_cells_are_not_eligible() -> None:
    w = World().seats("business", 10)
    w.usage(100_000, model="Code Review", users=2).usage(100_000, model="Auto: Claude Sonnet 5")
    w.ide("t1", {"ide:vscode": 5})
    plan = plan_for(w, [AUTO_FINDING])
    assert "copilot.default_model_auto (linked, nothing to act on" in plan.headline_monthly.note


def test_excluded_families_cli_and_app_counts() -> None:
    day = b.make_activity(b.make_principal("x"), date_utc="2026-09-02", team="t9", counts={
        "ide:visualstudio": 10, "ide:xcode": 10, "ide:eclipse": 10, "ide:pycharm": 10,
        "ide:neovim": 20, "cli_requests": 30, "app_requests": 10, "interactions": 999})
    got = team_reach([day], month="2026-09")["t9"]
    assert got == TeamReach(interactions=100, excluded=40, jetbrains=10)
    assert got.reach == Fraction(3, 5)
    assert set(EXCLUDED_EDITOR_FAMILIES) == {"jetbrains", "visual_studio", "xcode", "eclipse"}
    assert TeamReach(0, 0, 0).reach is None


def test_reach_uses_the_month_else_all_activity() -> None:
    days = [b.make_activity(b.make_principal("a"), date_utc="2026-08-30", team="t1",
                            counts={"ide:intellij": 5}),
            b.make_activity(b.make_principal("b"), date_utc="2026-09-03", team="t1",
                            counts={"ide:vscode": 5})]
    assert team_reach(days, month="2026-09")["t1"].reach == 1
    assert team_reach(days, month="2026-11")["t1"].reach == Fraction(1, 2)
    with pytest.raises(UsageError):
        team_reach(days, month="2026-9")
    with pytest.raises(UsageError):
        team_reach(["nope"], month="2026-09")  # type: ignore[list-item]


def _activity_counts(days: list[ActivityDay], month: str) -> list[ConfigSnapshot]:
    """Aggregate-only bundle rows per (team, month) as CP-HANDOFF builds them: ``ide:<family>``
    interaction sums, ``cli_requests``, ``app_interactions`` (= Σ ``app_requests``)."""
    sums: dict[str | None, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    people: dict[str | None, set[str]] = defaultdict(set)
    for d in days:
        people[d.team].add(d.principal)
        for key, value in d.counts:
            if key.startswith("ide:"):
                sums[d.team][f"ide:{catalog.editor_family(key)}"] += value
            elif key == "cli_requests":
                sums[d.team]["cli_requests"] += value
            elif key == "app_requests":
                sums[d.team]["app_interactions"] += value
            elif key == "interactions":
                sums[d.team]["interactions"] += value
    rows = []
    for team, attrs in sums.items():
        row = {"team": team, "month": month, "n_people": len(people[team]), **attrs}
        rows.append(b.make_config("activity_counts", row, entity_id="org:org-a"))
    return rows


def test_reach_from_activity_counts_equals_reach_from_the_activity_days() -> None:
    days = []
    for i in range(6):
        days.append(b.make_activity(b.make_principal(f"p{i}"), date_utc=f"2026-09-0{i + 1}",
                                    team="t1", counts={"ide:intellij": 3 + i, "ide:vscode": 7,
                                                       "cli_requests": i, "app_requests": 1,
                                                       "interactions": 10}))
        days.append(b.make_activity(b.make_principal(f"q{i}"), date_utc="2026-09-04",
                                    team="t2", counts={"ide:pycharm": 1, "ide:rider": 2,
                                                       "ide:vscode": 9}))
    rows = _activity_counts(days, "2026-09")
    from_days = team_reach(days, month="2026-09")
    from_rows = team_reach([], rows, month="2026-09")
    assert {t: r.reach for t, r in from_days.items()} == {t: r.reach for t, r in from_rows.items()}
    assert from_days == from_rows
    # and through the plan: identical Auto figures
    w = _overage_world().usage(300_000, team="t2")
    w.activity = days
    by_days = lever(plan_for(w, [AUTO_FINDING]), AUTO)
    w.activity = []
    w.config = list(rows)
    by_rows = lever(plan_for(w, [AUTO_FINDING]), AUTO)
    assert by_days == by_rows
    assert by_days.shapley.low_nano is None                 # both teams' reach is known


def test_activity_counts_rows_of_another_month_are_used_when_none_match() -> None:
    rows = [b.make_config("activity_counts", {"team": "t1", "month": "2026-08", "n_people": 5,
                                              "ide:jetbrains": 1, "ide:vscode": 3},
                          entity_id="org:org-a"),
            b.make_config("activity_counts", {"team": None, "month": "2026-08", "n_people": 5,
                                              "ide:vscode": 3}, entity_id="org:org-a")]
    got = team_reach([], rows, month="2026-09")
    assert got["t1"].reach == Fraction(3, 4) and got[None].reach == 1


def test_auto_share_from_metrics_when_the_report_has_no_auto_labels() -> None:
    w = World().seats("business", 1000).seats("enterprise", 200)
    w.usage(3_100_000, users=4)                   # no "Auto:" row anywhere
    w.ide("t1", {"ide:vscode": 10, "model:auto": 25, "model:claude-sonnet-5": 75})
    auto = lever(plan_for(w, [AUTO_FINDING]), AUTO)
    # 10% × 3,100,000 × (1 − 0.25 already on Auto) = 232,500 credits; overage 420,000
    assert auto.shapley.nano == 232_500 * C
    plan = plan_for(w, [AUTO_FINDING])
    assert "no Auto labels in the report" in plan.headline_monthly.note


def test_direct_org_cells_save_their_net_share_even_in_slack() -> None:
    w = World().seats("business", 1000)
    w.usage(100_000, users=2)                                   # slack pool
    w.usage(100_000, unattributed=True, discount=50_000)        # direct rows, half discounted
    w.ide("t1", {"ide:vscode": 1})
    plan = plan_for(w, [AUTO_FINDING])
    auto = lever(plan, AUTO)
    # direct: 10% of 100,000 credits = 10,000 → net half $50 invoice, half headroom
    assert auto.shapley.nano == 50 * USD
    head = lever(plan, AUTO, Basis.LIST_EQUIVALENT)
    # pooled: 10,000 credits all headroom (slack) + the discounted direct half $50
    assert head.shapley.nano == 100 * USD + 50 * USD


def test_unattributed_team_reach_matches_unattributed_cells() -> None:
    w = _overage_world()
    w.aggs = []
    w.lines = [ln for ln in w.lines if ln.cost_type == "seat"]
    w.usage(1_000_000, team=None, users=2).usage(2_100_000, model="Auto: Claude Sonnet 5",
                                                  team=None)
    w.ide(None, {"ide:intellij": 1, "ide:vscode": 1})
    auto = lever(plan_for(w, [finding("auto-adoption")]), AUTO)
    assert auto.shapley.nano == 500 * USD

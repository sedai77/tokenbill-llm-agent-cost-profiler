"""Property / fuzz tests: random Copilot worlds through the summary, the three renderers, the
showback allocation and the FOCUS rows. Only ``TokenbillError`` subclasses may escape (CP-OUT owns
no parser; its inputs are records, so the fuzzing is over record worlds)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.copilot.render import CopilotSection
from tokenbill.copilot.showback import allocate_overage, render_copilot_showback
from tokenbill.copilot.summary import DOLLAR_LINES, apportion
from tokenbill.core import builders as b
from tokenbill.core.errors import TokenbillError
from tokenbill.core.labels import Basis, Evidence

from .checks import html_violations, json_violations
from .test_focus import col, rows_of
from .worlds import World, result_of

SETTINGS = settings(max_examples=40, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.function_scoped_fixture])

row = st.fixed_dictionaries({
    "credits": st.integers(min_value=0, max_value=50_000),
    "discount_pct": st.integers(min_value=0, max_value=100),
    "day": st.integers(min_value=1, max_value=28),
    "team": st.sampled_from([None, "alpha", "beta", "gamma"]),
    "user": st.integers(min_value=0, max_value=12),
    "model": st.sampled_from(["Claude Sonnet 4.6", "Auto: Claude Haiku 4.5", "GPT-5.4",
                              "Code Review"]),
    "direct": st.booleans(),
    "final": st.booleans(),
})
world_st = st.fixed_dictionaries({
    "rows": st.lists(row, min_size=0, max_size=25),
    "seats": st.sampled_from([None, "seat_lines", "unknown", "seats_api"]),
    "n_seats": st.integers(min_value=1, max_value=40),
    "actions": st.integers(min_value=0, max_value=3),
    "today": st.sampled_from(["2026-09-15", "2026-10-20"]),
    "verdict": st.sampled_from(["reconciled", "not_reconciled", "insufficient_data"]),
    "k": st.integers(min_value=1, max_value=6),
})


def build(spec: dict) -> World:
    w = World()
    for r in spec["rows"]:
        discount = r["credits"] * r["discount_pct"] // 100
        w.usage(str(r["credits"]), i=r["user"], day=r["day"], discount=str(discount),
                team=r["team"], model=r["model"], unattributed=r["direct"],
                finality="final" if r["final"] else "provisional")
    n = spec["n_seats"]
    if spec["seats"] == "seat_lines":
        w.lines.append(b.make_seat_line("business", str(n)))
    elif spec["seats"] in ("unknown", "seats_api"):
        plan = "unknown" if spec["seats"] == "unknown" else "enterprise"
        kind = ("github.copilot_activity_report" if plan == "unknown"
                else "github.copilot_seats")
        for i in range(n):
            w.licenses.append(b.make_license(b.make_principal(i), plan=plan, source_kind=kind,
                                             assigned_via_team=None if plan == "unknown"
                                             else False, team="alpha" if i % 2 else "beta"))
    for i in range(spec["actions"]):
        w.lines.append(b.make_actions_line(str(10 + i), date_utc=f"2026-09-{i + 2:02d}",
                                           workload=["copilot_code_review",
                                                     "copilot_cloud_agent",
                                                     "agentic_workflow"][i]))
    return w


@SETTINGS
@given(world_st)
def test_random_worlds_render_and_keep_the_rules(spec: dict) -> None:
    w = build(spec)
    verdicts = {c: spec["verdict"] for c in ("github_copilot", "github_actions")}
    try:
        s = w.summary(today=spec["today"], verdicts=verdicts, k=spec["k"])
    except TokenbillError:
        return
    for bl in s.lines:
        if bl.line == "total.invoice":
            assert all(c in DOLLAR_LINES for c in bl.components)
            assert bl.amount.basis in (Basis.INVOICE, Basis.LIST)
            parts = [x for x in s.lines if x.entity_id == bl.entity_id and x.month == bl.month
                     and x.line in bl.components and x.scenario in (None, bl.scenario)
                     and not (bl.scenario and x.line == "ai_credits.overage"
                              and x.scenario is None)]
            assert bl.amount.nano == sum(p.amount.nano for p in parts)
        if bl.amount.basis is Basis.INVOICE:
            assert spec["today"] == "2026-10-20" and bl.amount.evidence is Evidence.EXACT
        if bl.scenario is not None:
            assert bl.line in ("seats.business", "seats.enterprise", "seats.unknown_plan",
                               "ai_credits.overage", "total.invoice",
                               "ai_credits.discount_pool", "ai_credits.discount_other",
                               "ai_credits.discount_unclassified")
    result = result_of(s)
    section = CopilotSection()
    for width in (40, 100):
        text = section.terminal(result, width=width)
        assert all(len(line) <= width for line in text.splitlines())
    assert html_violations(section.html(result)) == []
    assert json_violations(section.json(result)) == []
    for key, alloc in allocate_overage(s).items():
        pm = next(p for p in s.pools if (p.entity_id, p.month, p.plan_scenario or "known") == key)
        assert sum(alloc.values()) == pm.overage_observed_nano


@SETTINGS
@given(world_st)
def test_random_worlds_focus_rows_balance(spec: dict) -> None:
    w = build(spec)
    try:
        rows = rows_of(w, k=spec["k"])
    except TokenbillError:
        return
    total_net = Decimal(0)
    for r in rows:
        disc = sum(Decimal(col(r, c)) for c in ("x_DiscountPool", "x_DiscountOther",
                                                 "x_DiscountUnclassified"))
        assert Decimal(col(r, "ListCost")) - disc == Decimal(col(r, "BilledCost"))
        total_net += Decimal(col(r, "BilledCost"))
    stored = {c.line_id: c for c in w.lines}          # the store keeps one line per natural id
    expected = sum(c.amount_nano for c in stored.values() if c.channel != "github_copilot"
                   or c.source_kind == "github.ai_usage_report" or c.cost_type == "seat")
    assert total_net == Decimal(expected) / Decimal(10**9)


@SETTINGS
@given(st.integers(min_value=-10**15, max_value=10**15),
       st.lists(st.integers(min_value=-5, max_value=10**12), min_size=1, max_size=12))
def test_apportion_always_sums(total: int, weights: list[int]) -> None:
    parts = apportion(total, weights)
    assert len(parts) == len(weights)
    if any(x > 0 for x in weights):
        assert sum(parts) == total
    else:
        assert parts == [0] * len(weights)


@SETTINGS
@given(world_st)
def test_random_worlds_showback_files(tmp_path_factory, spec: dict) -> None:
    w = build(spec)
    try:
        s = w.summary(today=spec["today"], k=spec["k"])
    except TokenbillError:
        return
    out: Path = tmp_path_factory.mktemp("sb")
    for path in render_copilot_showback(result_of(s), out, formats=("html", "csv", "json")):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".html":
            assert html_violations(text) == []

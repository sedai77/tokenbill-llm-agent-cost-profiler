"""CopilotSection: terminal goldens, width, plan block first, HTML rules, JSON rules (§14.2).

Goldens live in ``snapshots/``; regenerate with ``TOKENBILL_UPDATE_SNAPSHOTS=1``."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from tokenbill.copilot.render import CopilotSection, chip, credits_text, label, money, plan_info
from tokenbill.copilot.summary import assemble_summary
from tokenbill.core import builders as b
from tokenbill.core import extensions, pool
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Basis, Figure, estimated, exact, unpriced
from tokenbill.core.protocols import SectionRenderer

from .checks import html_violations, json_violations
from .worlds import (
    OPEN_TODAY,
    RECONCILED,
    WINDOW,
    action_plan,
    admin_action,
    p1_world,
    p13_world,
    p14_world,
    result_of,
    two_entity_world,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"
SECTION = CopilotSection()
SCENARIO_PLANS = (("business", action_plan()), ("enterprise", action_plan(headline=0)))


def snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("TOKENBILL_UPDATE_SNAPSHOTS") == "1" or not path.exists():
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def p13_result():
    return result_of(p13_world().summary(plans_by_scenario=SCENARIO_PLANS,
                                         actions=[admin_action()]))


def two_entity_result():
    return result_of(two_entity_world().summary(actions=[admin_action()]))


def test_section_implements_the_protocol() -> None:
    assert isinstance(SECTION, SectionRenderer) and SECTION.name == "copilot"


def test_terminal_golden_two_entities() -> None:
    snapshot("terminal_two_entities.txt", SECTION.terminal(two_entity_result(), width=100))


def test_terminal_golden_plan_unknown_has_both_columns() -> None:
    text = SECTION.terminal(p13_result(), width=100)
    snapshot("terminal_p13.txt", text)
    head = next(line for line in text.splitlines() if "if Business" in line)
    assert "| if Enterprise" in head
    assert "~$1,900.00" in text and "~$3,900.00" in text and "~$2,500.00" in text


@pytest.mark.parametrize("width", [40, 60, 79, 80, 100, 132])
def test_every_line_fits_the_width(width: int) -> None:
    for result in (p13_result(), two_entity_result(),
                   result_of(p1_world().summary(plans_by_scenario=(("known", action_plan()),)))):
        text = SECTION.terminal(result, width=width)
        assert max(len(line) for line in text.splitlines()) <= width


def test_narrow_terminal_stacks_the_scenarios() -> None:
    text = SECTION.terminal(p13_result(), width=70)
    assert " | " not in text
    assert text.index("if Business") < text.index("if Enterprise")


def test_plan_line_and_hint_come_before_the_first_number() -> None:
    text = SECTION.terminal(p13_result(), width=100)
    first_money = text.index("$")
    assert text.index("PLAN") < first_money
    assert text.index("unknown — shown as Business and as Enterprise") < first_money
    assert text.index("copilot_for_business / copilot_enterprise") < first_money


def test_p14_one_block_names_conflict_and_winner() -> None:
    text = SECTION.terminal(result_of(p14_world().summary()), width=100)
    assert "if Business" not in text
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("PLAN enterprise"))
    block = " ".join(text[text.index(line):].split())
    assert "Enterprise — from seat SKU lines" in block
    assert "conflict: seat_lines vs seats_api, admin_statement" in block
    assert "seat SKU lines win" in block


def test_terminal_shows_labels_pool_verdicts_plan_and_actions() -> None:
    s = p1_world().summary(plans_by_scenario=(("known", action_plan()),),
                           actions=[admin_action()])
    text = SECTION.terminal(result_of(s), width=100)
    assert "COPILOT BILL — enterprise · 2026-09" in text
    assert "$31,156.00  invoice" in text
    assert "exact·list-equivalent (not billed)" in text
    assert "POOL 2,680,000 cr" in text and "regime overage" in text
    assert "VERDICTS github_actions: reconciled" in text
    assert "COPILOT PLAN" in text and "pool headroom" in text
    assert "Shapley" in text and "reach 80%" in text and "needs eval" in text
    assert "WHAT TO CHANGE IN GITHUB" in text and "deadline 2026-10-01" in text
    flat = " ".join(text.split())
    assert "auth: enterprise owner" in flat and "https://" not in text


def test_open_month_terminal_shows_forecast_and_no_invoice() -> None:
    text = SECTION.terminal(result_of(p1_world().summary(today=OPEN_TODAY)), width=100)
    assert "forecast ~$" in text and "provisional" in text
    assert " invoice\n" not in text


def test_terminal_is_sanitized() -> None:
    bad = replace(admin_action(), what="Set \x1b[31mred\x1b[0m model p_0123456789abcdef0123")
    text = SECTION.terminal(result_of(p1_world().summary(actions=[bad])), width=100)
    assert "\x1b" not in text and "p_0123456789abcdef0123" not in text and "(person)" in text


def test_empty_and_invalid_inputs() -> None:
    empty = result_of(None)
    assert SECTION.terminal(empty, width=100) == "" and SECTION.html(empty) == ""
    assert SECTION.json(empty) is None
    with pytest.raises(UsageError):
        SECTION.terminal(p13_result(), width=20)
    with pytest.raises(ContractViolation):
        SECTION.html("not a result")  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        SECTION.json(replace(empty, copilot="x"))  # type: ignore[arg-type]


def test_html_rules_hold() -> None:
    for result in (p13_result(), two_entity_result(),
                   result_of(p1_world().summary(plans_by_scenario=(("known", action_plan()),),
                                                actions=[admin_action()]))):
        text = SECTION.html(result)
        assert text.startswith('<section id="copilot">') and text.endswith("</section>")
        assert html_violations(text) == []
        assert "<svg" in text and "<caption>" in text


def test_html_plan_unknown_uses_a_two_column_table_with_row_headers() -> None:
    text = SECTION.html(p13_result())
    assert '<th scope="col">if Business</th><th scope="col">if Enterprise</th>' in text
    assert '<th scope="row">seats.unknown_plan</th>' in text
    assert "Plan unknown" in text and "If Business" in text


def test_html_escapes_text() -> None:
    bad = replace(admin_action(), what="<b onclick=x>&</b>")
    text = SECTION.html(result_of(p1_world().summary(actions=[bad])))
    assert "<b onclick" not in text and "&lt;b onclick=x&gt;&amp;&lt;/b&gt;" in text


def test_json_known_plan_follows_the_rules() -> None:
    s = p1_world().summary(plans_by_scenario=(("known", action_plan()),),
                           actions=[admin_action()])
    doc = SECTION.json(result_of(s))
    assert json_violations(doc) == []
    assert {"pools", "lines", "teams", "seat_counts", "plan", "actions", "channel_verdicts",
            "evidence", "plan_status"} <= set(doc)
    assert "scenarios" not in doc
    total = next(x for x in doc["lines"] if x["line"] == "total.invoice")
    assert total["amount"]["basis"] == "invoice" and total["amount"]["usd"] == "31156"
    assert doc["plan"]["pool_headroom_monthly"]["basis"] == "list_equivalent"
    assert doc["plan_status"][0]["source"] == "seat_lines"


def test_json_plan_unknown_puts_scenario_figures_only_under_scenarios() -> None:
    doc = SECTION.json(p13_result())
    assert json_violations(doc) == []
    assert all(x["scenario"] is None for x in doc["lines"])
    assert all(p["plan_scenario"] is None for p in doc["pools"])
    assert set(doc["scenarios"]) == {"business", "enterprise"}
    bus = {x["line"]: x for x in doc["scenarios"]["business"]["lines"]}
    assert bus["total.invoice"]["amount"]["usd"] == "2500"
    assert bus["total.invoice"]["amount"]["evidence"] == "estimated"
    ent = {x["line"]: x for x in doc["scenarios"]["enterprise"]["lines"]}
    assert ent["total.invoice"]["amount"]["usd"] == "3900"
    assert doc["scenarios"]["business"]["pools"][0]["regime"] == "overage"
    assert doc["plan"] is None and doc["scenarios"]["enterprise"]["plan"] is not None
    assert doc["plan_status"][0]["hint"] and doc["plan_status"][0]["scenarios"]
    assert doc["editor_split"]["rows"] and doc["teams"]["rows"]


def test_json_mirror_catches_violations() -> None:
    bad = {"x": 1, "headline_monthly": {"usd": "1", "nano": 10**9, "evidence": "exact",
                                        "basis": "list_equivalent", "finality": "n/a",
                                        "range": None, "ci_level_pct": None,
                                        "calibration": "n/a", "upper_bound": False,
                                        "provenance": [], "note": ""},
           "pool_nano": 3, "f": 1.5}
    errors = json_violations(bad)
    assert len(errors) == 4


def test_chip_money_label_and_credits_helpers() -> None:
    assert money(None) == "—" and label(None) == "" and chip(None) == "—"
    assert money(unpriced("no rate")) == "unpriced"
    rng = estimated(5 * 10**9, Basis.LIST, low=10**9, high=9 * 10**9, note="x")
    assert label(rng) == "est.·list ($1.00–$9.00)"
    assert chip(exact(10**9, Basis.INVOICE)) == "$1.00 invoice"
    assert credits_text(None) == "unknown" and credits_text(425_000_000) == "42.5 cr"
    assert "provisional" in label(Figure(nano=1, evidence="exact", basis="list",
                                         finality="provisional"))


def test_plan_info_derives_from_pools_without_evidence() -> None:
    pm = b.make_pool_month(seats={"business": "5", "enterprise": "5"})
    info = plan_info("enterprise", "2026-09", [pm], [])
    assert info.plan == "mixed" and not info.unknown
    none = plan_info("enterprise", "2026-09", [b.make_pool_month(seats={})], [])
    assert none.plan == "unknown" and "no seats found" in none.text()


def test_classified_pool_discount_renders_as_list_equivalent_never_billed() -> None:
    w = p1_world()
    cells, _ = pool.build_cells(w.aggs, w.lines, grain="day")
    pools = pool.pool_months(cells, w.lines, [], [], today="2026-10-20", gross_is_list=True)
    s = assemble_summary(cost_lines=w.lines, aggregates=w.aggs, licenses=[], activity=[],
                         pools=pools, channel_verdicts=RECONCILED, window=WINDOW)
    text = SECTION.terminal(result_of(s), width=100)
    line = next(ln for ln in text.splitlines() if "ai_credits.discount_pool" in ln)
    assert "list-equivalent (not billed)" in line and "invoice" not in line
    doc = SECTION.json(result_of(s))
    assert json_violations(doc) == []
    dp = next(x for x in doc["lines"] if x["line"] == "ai_credits.discount_pool")
    assert dp["amount"]["basis"] == "list_equivalent"


def test_extension_host_renders_and_writes_through_the_registry(tmp_path: Path) -> None:
    result = p13_result()
    assert any("COPILOT BILL" in str(t) for t in extensions.render_sections(result, "terminal"))
    [doc] = extensions.render_sections(result, "json")
    assert set(doc) == {"copilot"} and json_violations(doc) == []
    paths = extensions.showback(result, tmp_path, ("html", "json"))
    assert [p.name for p in paths] == ["copilot-showback.html", "copilot-showback.json"]

"""The admin checklist: triggers, plan unknown (C.P13), JetBrains limits, deadlines, labels
(addendum §11.3, §9.3, §19.4; brief acceptance 3, 5, 6)."""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from tokenbill.copilot.admin_actions import (
    JETBRAINS_ACTION_ID,
    NOT_ASSESSED_PLAN_UNKNOWN,
    admin_actions,
    figure_text,
    lever_projection,
    plan_unknown,
    scenario_projection_text,
    split_plans,
    team_editor_shares,
)
from tokenbill.copilot.policy import build_copilot_packs
from tokenbill.core import builders as b
from tokenbill.core import catalog
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, unpriced
from tokenbill.core.types import Scope

from .helpers import (
    TODAY,
    USD,
    ev,
    fig,
    finding,
    idle_seat,
    plan,
    pools_known,
    pools_unknown,
    result,
    seats_finding,
    teams_with,
)

KNOWN_EVIDENCE = [b.make_plan_evidence(plan="business", source="seat_lines",
                                       seats={"business": 100})]
UNKNOWN_EVIDENCE = [b.make_plan_evidence()]
SEAT = "copilot.seat_reclaim"


def _ids(actions) -> list[str]:
    return [a.action_id for a in actions]


def _by_id(actions, action_id: str):
    return next(a for a in actions if a.action_id == action_id)


def _known(*levers, findings=(), pools=None, **kw):
    return admin_actions((("known", plan(*levers)),), list(findings),
                         pools if pools is not None else pools_known(), plans=KNOWN_EVIDENCE,
                         today=TODAY, **kw)


def _unknown_plans() -> tuple:
    return (("business", plan(result(SEAT, 0))),
            ("enterprise", plan(result(SEAT, 390, upper_bound=True))))


def test_review_mcp_personal_caveat_cap_choice_and_cli_billing_items() -> None:
    fs = [finding("review-drivers"), finding("review-default-balanced"),
          seats_finding("budget-no-cost-center-pool", cost_center="A"),
          finding("agentic-workflow-cost",
                  evidence=[ev("aw:runs", suggested_max_ai_credits=450)])]
    acts = _known(findings=fs)
    ids = _ids(acts)
    for item in ("admin:repo_review_mcp_off", "admin:communicate_personal_review_settings",
                 "admin:cost_center_pool", "admin:org_cli_billing_policy",
                 "admin:review_effort_default", "admin:review_instructions",
                 "admin:review_triggers", "admin:aw_triggers", "rest:cost_center_patch"):
        assert item in ids, item
    assert "block" in _by_id(acts, "admin:cost_center_pool").what
    assert "Suggested max-ai-credits: 450" in _by_id(acts, "admin:aw_triggers").what
    lite = _by_id(acts, "admin:review_effort_default")
    assert lite.deadline == "2026-09-28"
    for a in acts:
        assert a.doc_url.startswith("https://"), a.action_id
        assert len(a.what) <= 400
        assert a.admin_action in catalog.ADMIN_ACTIONS
    # without those findings the items are absent (the opt-in enabler is always listed)
    assert _ids(_known()) == ["admin:vscode_db_exporter_optin"]


def test_every_action_has_a_docs_url_and_a_label_in_the_checklist() -> None:
    fs = [finding("review-drivers"), finding("fast-mode", team="t1"),
          idle_seat(removable=12, team=3, lever_ids=(SEAT, "copilot.seat_reclaim_team")),
          seats_finding("overage-forecast")]
    acts = _known(result(SEAT, 228), result("copilot.fast_mode_off", 145), findings=fs)
    pack = build_copilot_packs((("known", plan(result(SEAT, 228),
                                               result("copilot.fast_mode_off", 145))),),
                               fs, acts, current=None, cohort_by=None, include_tradeoffs=False,
                               teams=None, budgets=None, today=TODAY)[0]
    checklist = dict(pack.hooks)["github/admin-checklist.md"]
    sections = checklist.split("\n## ")[1:]
    items = [s for s in sections if s[0].isdigit()]
    assert len(items) == len(acts)
    for action, section in zip(acts, items, strict=True):
        assert f"- Docs: {action.doc_url}" in section
        assert "- Label: " in section and "- Auth: " in section
    seat = _by_id(acts, "rest:org_selected_users_delete")
    assert seat.projection is not None and seat.projection.nano == 228 * USD
    assert seat.reach == "1" and seat.deadline == "2026-10-01"
    assert seat.rest_file == "github/requests.jsonl"
    assert "manage_billing:copilot" in (seat.auth_note or "")
    assert "$228.00/month (estimated, list)" in checklist
    assert "$145.00/month (estimated, list)" in checklist
    assert "team_membership_review" not in checklist  # ids never printed as titles
    assert "Review Copilot-granting team membership" in checklist
    assert "12 removable seats counted" in seat.what


def test_plan_unknown_starts_with_the_confirmation_and_has_no_downgrade() -> None:
    fs = [idle_seat(unknown=10, plan_scenario="business"),
          idle_seat(unknown=10, plan_scenario="enterprise"),
          seats_finding("plan-mix", lever_ids=["copilot.seat_downgrade"], plan="enterprise"),
          seats_finding("completions-only-seat", team="t1")]
    acts = admin_actions(_unknown_plans(), fs, pools_unknown(), plans=UNKNOWN_EVIDENCE,
                         today=TODAY)
    assert acts[0].action_id == "admin:plan_confirm"
    assert "1 entity unknown" in acts[0].what
    assert "admin:seat_plan_change" not in _ids(acts)
    assert all(a.lever_id != "copilot.seat_downgrade" for a in acts)
    seat = _by_id(acts, "rest:org_selected_users_delete")
    assert seat.projection is None
    assert "if Business: $0.00/month" in seat.what
    assert "if Enterprise: $390.00/month (estimated, list, upper bound)" in seat.what
    pack = build_copilot_packs(_unknown_plans(), fs, acts, current=None, cohort_by=None,
                               include_tradeoffs=False, teams=None, budgets=None)[0]
    checklist = dict(pack.hooks)["github/admin-checklist.md"]
    assert checklist.count(NOT_ASSESSED_PLAN_UNKNOWN) == 1
    assert NOT_ASSESSED_PLAN_UNKNOWN in pack.readme_md
    label = next(line for line in checklist.splitlines()
                 if line.startswith("- Label:") and "if Business" in line)
    assert "if Enterprise: $390.00/month" in label and "per scenario" in label


def test_plan_known_gives_neither_the_confirmation_nor_scenario_values() -> None:
    fs = [idle_seat(removable=10),
          seats_finding("plan-mix", lever_ids=["copilot.seat_downgrade"], plan="enterprise")]
    acts = _known(result(SEAT, 390), result("copilot.seat_downgrade", 200), findings=fs)
    assert "admin:plan_confirm" not in _ids(acts)
    change = _by_id(acts, "admin:seat_plan_change")
    assert change.projection is not None and change.projection.nano == 200 * USD
    assert change.tradeoff and change.needs_eval
    assert all("if Business" not in a.what for a in acts)
    pack = build_copilot_packs((("known", plan(result(SEAT, 390))),), fs, acts, current=None,
                               cohort_by=None, include_tradeoffs=False, teams=None,
                               budgets=None)[0]
    assert NOT_ASSESSED_PLAN_UNKNOWN not in pack.readme_md
    assert "Plan unknown" not in dict(pack.hooks)["github/admin-checklist.md"]


def test_conflicting_evidence_also_puts_the_confirmation_first() -> None:
    conflict = [b.make_plan_evidence(plan="enterprise", source="seat_lines", conflict=True,
                                     seats={"enterprise": 10})]
    acts = admin_actions((("known", plan()),), [], pools_known(), plans=conflict, today=TODAY)
    assert acts[0].action_id == "admin:plan_confirm" and "1 with conflicting" in acts[0].what
    # a known plan's plan-status finding asks for nothing
    ok = _known(findings=[seats_finding("plan-status")])
    assert "admin:plan_confirm" not in _ids(ok)


def test_jetbrains_share_adds_the_limits_item_and_the_model_policy() -> None:
    teams = teams_with({"t1": {"ide:intellij": 30, "ide:vscode": 70}})
    acts = _known(findings=[finding("auto-adoption", team="t1")], teams=teams)
    ids = _ids(acts)
    assert ids[0] == JETBRAINS_ACTION_ID
    jb = acts[0]
    assert jb.reach == "1" and "not applied in JetBrains" in jb.what
    assert "documented inconsistently" in jb.what and "1 team with JetBrains usage" in jb.what
    assert "admin:model_policy" in ids and "admin:communicate_auto_tier" in ids
    assert _by_id(acts, "admin:model_policy").reach == "1"
    none = _known(findings=[finding("auto-adoption", team="t1")],
                  teams=teams_with({"t1": {"ide:vscode": 100}}))
    assert JETBRAINS_ACTION_ID not in _ids(none) and "admin:model_policy" not in _ids(none)


def test_editor_mix_findings_also_trigger_the_limits_item_after_plan_confirmation() -> None:
    mix = finding("editor-mix", team="t2", evidence=[ev("editors", jetbrains_share="0.6")])
    acts = admin_actions(_unknown_plans(), [mix], pools_unknown(), plans=UNKNOWN_EVIDENCE,
                         today=TODAY)
    assert _ids(acts)[:2] == ["admin:plan_confirm", JETBRAINS_ACTION_ID]
    assert "(1 at 50% or more" in acts[1].what


def test_volume_billing_drops_cost_center_items_and_uses_renewal_deadlines() -> None:
    fs = [seats_finding("budget-no-cost-center-pool", cost_center="A"),
          seats_finding("overage-forecast"), idle_seat(removable=10)]
    pools = pools_known(billing_mode="volume")
    renewal = [b.make_config("run_flags", {"renewal_date.enterprise": "2027-03-01"})]
    acts = _known(result(SEAT, 0), findings=fs, pools=pools, config=renewal)
    ids = _ids(acts)
    assert "admin:cost_center_pool" not in ids and "rest:cost_center_patch" not in ids
    assert "admin:budget_stop" in ids and "admin:paid_usage_policy" in ids
    assert _by_id(acts, "rest:org_selected_users_delete").deadline == "2027-03-01"
    no_date = _known(result(SEAT, 0), findings=fs, pools=pools)
    assert _by_id(no_date, "rest:org_selected_users_delete").deadline is None
    empty = _known(result(SEAT, 0), findings=fs, pools=[])
    assert _by_id(empty, "rest:org_selected_users_delete").deadline is None


def test_review_lite_deadline_ends_on_the_date_and_60_day_threshold_moves_the_cutoff() -> None:
    late = admin_actions((("known", plan(result(SEAT, 5, params="copilot:seats_idle=60d@all"))),),
                         [finding("review-cost")], pools_known(), plans=KNOWN_EVIDENCE,
                         today="2026-09-28")
    assert _by_id(late, "admin:review_effort_default").deadline is None
    assert "before 2026-07-30" in _by_id(late, "rest:org_selected_users_delete").what


def test_kind_only_items_and_unattributed_spend() -> None:
    fs = [finding("unattributed-spend"), finding("cloud-agent-cost"),
          finding("direct-org-usage"), finding("ci-uncapped", detector="copilot.lanes",
                                               evidence=[ev("ci", suggested_max_ai_credits=12)])]
    acts = _known(findings=fs)
    ids = _ids(acts)
    for item in ("rest:cost_center_create", "rest:coding_agent_policy", "admin:runner_type",
                 "admin:review_unlicensed_policy", "admin:ci_limits_snippet"):
        assert item in ids, item
    assert "Suggested --max-ai-credits: 30." in _by_id(acts, "admin:ci_limits_snippet").what
    assert _by_id(acts, "rest:coding_agent_policy").lever_id == "copilot.agent_runner_standard"


def test_non_copilot_findings_are_ignored_and_order_is_input_independent() -> None:
    generic = dataclasses.replace(finding("fast-mode", team="t1"),
                                  detector_id="premium.modifiers", scope=Scope((("team", "t1"),)))
    assert _ids(_known(findings=[generic])) == ["admin:vscode_db_exporter_optin"]
    fs = [finding("review-drivers"), idle_seat(removable=3), finding("fast-mode", team="t1"),
          seats_finding("overage-forecast")]
    a = _known(result(SEAT, 1), findings=fs)
    assert a == _known(result(SEAT, 1), findings=list(reversed(fs)))


def test_split_plans_validates_keys() -> None:
    p = plan()
    assert split_plans(None) == (None, ())
    assert split_plans({"known": p}) == (p, ())
    assert split_plans([("enterprise", p), ("business", p)])[1][0][0] == "business"
    for bad in ([("known", p), ("business", p)], [("gold", p)], [("business", p)] * 2,
                [("known", "plan")], ["x"], 5):
        with pytest.raises(UsageError):
            split_plans(bad)


def test_inputs_are_validated() -> None:
    with pytest.raises(UsageError):
        admin_actions((), ["not a finding"], [], plans=[], today=TODAY)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        admin_actions((), [], "pools", plans=[], today=TODAY)  # type: ignore[arg-type]
    for bad in ("tomorrow", "2026-13-45", None):
        with pytest.raises(UsageError):
            admin_actions((), [], [], plans=[], today=bad)
    assert admin_actions((), [], [], plans=[], today=dt.date(2026, 9, 25))
    assert admin_actions((), [], [], plans=[], today=dt.datetime(2026, 9, 25, 12))


def test_projection_helpers() -> None:
    both = plan(result(SEAT, 10), result(SEAT, 5), result(SEAT, 99, basis=Basis.LIST_EQUIVALENT))
    assert lever_projection(both, SEAT).nano == 15 * USD
    assert lever_projection(both, "copilot.fast_mode_off") is None
    assert lever_projection(None, SEAT) is None
    assert figure_text(fig(3, low=1, high=5)) == \
        "$3.00/month (estimated, list, range $1.00–$5.00)"
    assert figure_text(unpriced("no rate")) == "unpriced (exact, list)"
    assert scenario_projection_text((("business", plan()),), SEAT) is None
    assert plan_unknown([], pools_unknown()) and not plan_unknown([], pools_known())


def test_team_editor_shares_validates_counts() -> None:
    got = team_editor_shares({"t": {"ide:intellij": 1, "cli": 1, "app": 2, "n_people": 7,
                                    "seats": 3, "interactions": 9}})
    assert got == {"t": (Decimal("0.25"), 4, 7)}
    assert team_editor_shares({"t": {"seats": 3}}) == {}
    for bad in ([], {"": {}}, {"t": []}, {"t": {1: 2}}, {"t": {"cli": -1}},
                {"t": {"cli": "x"}}, {"t": {"cli": True}}):
        with pytest.raises(UsageError):
            team_editor_shares(bad)  # type: ignore[arg-type]

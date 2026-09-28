"""The pack's files: requests.jsonl, JetBrains reach, VS Code opt-in, repo and CI files,
determinism and the canary (addendum §11.3, §19.4; brief acceptance 2, 6, 7)."""

from __future__ import annotations

import dataclasses
import json
import re

import pytest

from tokenbill.copilot.admin_actions import JETBRAINS_ACTION_ID, admin_actions
from tokenbill.copilot.budgets import budget_design
from tokenbill.copilot.policy import build_copilot_packs, team_counts, write_pack
from tokenbill.core import builders as b
from tokenbill.core.builders import CANARY, CANARY_EMAIL, CANARY_LOGIN
from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.types import Fix

from .helpers import (
    TODAY,
    budget_pools,
    ev,
    finding,
    idle_seat,
    plan,
    pools_known,
    pools_unknown,
    result,
    seats_finding,
    teams_with,
    three_cost_centers,
)

SEAT = "copilot.seat_reclaim"
KEYS = {"method", "path", "api_version", "body", "note", "lever_id", "auth"}
P_RE = re.compile(r"p_[0-9a-f]{20}")


def _world(*, scenarios: bool = False, teams=None, extra=()):
    fs = [idle_seat(removable=12, team=6, lever_ids=(SEAT, "copilot.seat_reclaim_team")),
          finding("review-drivers"), finding("unattributed-spend"), finding("cloud-agent-cost"),
          seats_finding("budget-no-cost-center-pool", cost_center="A"),
          finding("agentic-workflow-cost", evidence=[ev("aw:runs", suggested_max_ai_credits=450)]),
          finding("premium-model-share", team="t1", lever_ids=["copilot.model_policy"]),
          finding("auto-adoption", team="t1"), finding("cache-health", team="t1"), *extra]
    if scenarios:
        plans = (("business", plan(result(SEAT, 0))),
                 ("enterprise", plan(result(SEAT, 390, upper_bound=True))))
        pools, evidence = pools_unknown(), [b.make_plan_evidence()]
    else:
        plans = (("known", plan(result(SEAT, 228), result("copilot.default_model_auto", 80))),)
        pools = pools_known()
        evidence = [b.make_plan_evidence(plan="business", source="seat_lines",
                                         seats={"business": 100})]
    acts = admin_actions(plans, fs, pools, plans=evidence, today=TODAY, teams=teams)
    w = three_cost_centers()
    budgets = budget_design(budget_pools(w, scenarios=scenarios), w.cells(), w.config, k=5,
                            cost_lines=w.lines)
    return plans, fs, acts, budgets


def _packs(*, scenarios: bool = False, teams=None, cohort_by=None, include_tradeoffs=True,
           current=None, extra=()):
    plans, fs, acts, budgets = _world(scenarios=scenarios, teams=teams, extra=extra)
    return build_copilot_packs(plans, fs, acts, current=current or {"model": "gpt-5.4"},
                               cohort_by=cohort_by, include_tradeoffs=include_tradeoffs,
                               teams=teams, budgets=budgets, today=TODAY)


def _lines(pack) -> list[dict]:
    text = dict(pack.hooks)["github/requests.jsonl"]
    return [json.loads(line) for line in text.splitlines()]


def test_requests_parse_without_usernames_tokens_or_pseudonyms() -> None:
    teams = {"idle team": {"n_people": 6, "seats": 6, "idle_seats": 6},
             "busy": {"n_people": 6, "seats": 6, "idle_seats": 2},
             "small": {"n_people": 2, "seats": 2, "idle_seats": 2}}
    for scenarios in (False, True):
        pack = _packs(scenarios=scenarios, teams=teams)[0]
        lines = _lines(pack)
        assert lines, scenarios
        text = dict(pack.hooks)["github/requests.jsonl"]
        assert not P_RE.search(text) and CANARY_LOGIN not in text
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{10,}|github_pat_|Bearer", text)
        for line in lines:
            assert set(line) == KEYS
            assert line["api_version"] == "2026-03-10"
            assert line["auth"] and line["lever_id"].startswith("copilot.")
        seat = [x for x in lines if x["path"].endswith("/selected_users")]
        assert len(seat) == 1
        users = seat[0]["body"]["selected_usernames"]
        assert len(users) == 1
        assert users[0].startswith("<from GET /orgs/{org}/copilot/billing/seats")
        assert "last_activity_at < 2026-08-26" in users[0] and "assigning_team is null" in users[0]
        teams_lines = [x for x in lines if x["path"].endswith("/selected_teams")]
        assert [x["body"]["selected_teams"] for x in teams_lines] == [
            ["<GitHub team slug of team 'idle team'>"]]
        assert "6 seats is idle" in teams_lines[0]["note"]


def test_every_request_has_the_auth_note_of_section_19_4() -> None:
    for line in _lines(_packs()[0]):
        path, auth = line["path"], line["auth"]
        if "/copilot/billing/selected_" in path:
            assert "organization owner" in auth
            assert "manage_billing:copilot" in auth and "admin:org" in auth
        elif "/settings/billing/" in path:
            assert "billing manager" in auth and "Enterprise billing: read and write" in auth
            assert "roles only" in auth
        elif path.endswith("/policies/coding_agent"):
            assert "admin:enterprise" in auth and "manage_billing:copilot" in auth
            assert "VERIFY" in line["note"]
        else:  # pragma: no cover - every emitted path is one of the above
            raise AssertionError(path)
    methods = sorted({(x["method"], x["path"]) for x in _lines(_packs()[0])})
    assert ("POST", "/enterprises/{enterprise}/settings/billing/cost-centers") in methods
    assert ("PUT", "/enterprises/{enterprise}/copilot/policies/coding_agent") in methods
    readme = _packs()[0].readme_md
    assert 'gh api --method DELETE -H "X-GitHub-Api-Version: 2026-03-10" ' \
           "/orgs/ORG/copilot/billing/selected_users" in readme


def test_budget_requests_come_per_scenario_and_a_template_without_a_design() -> None:
    lines = _lines(_packs(scenarios=True)[0])
    notes = [x["note"] for x in lines if x["path"].endswith("/budgets")]
    assert any(n.startswith("if Business:") for n in notes)
    assert any(n.startswith("if Enterprise:") for n in notes)
    plans, fs, acts, _ = _world()
    pack = build_copilot_packs(plans, fs, acts, current=None, cohort_by=None,
                               include_tradeoffs=False, teams=None, budgets=None)[0]
    budget = [x for x in _lines(pack) if x["path"].endswith("/budgets")]
    assert len(budget) == 1 and budget[0]["note"].startswith("Template")
    assert "<today minus 30 days>" in dict(pack.hooks)["github/requests.jsonl"]


def test_team_of_30_percent_intellij_gets_limits_item_policy_and_readme_reach() -> None:
    teams = teams_with({"t1": {"ide:intellij": 30, "ide:vscode": 60, "cli": 10}})
    pack = _packs(teams=teams)[0]
    checklist = dict(pack.hooks)["github/admin-checklist.md"]
    assert "JetBrains limits of the managed settings" in checklist
    assert "Model policies (server-side, every editor)" in checklist
    assert "Recommended for teams with JetBrains usage" in checklist
    readme = pack.readme_md
    assert "NOT JetBrains (managed model is not applied in JetBrains)" in readme
    assert "estimated reach 0.70" in readme
    assert "| t1 | 30% | 0.70 | managed model + server-side model policy for JetBrains users |" \
        in readme
    heavy = _packs(teams=teams_with({"t1": {"ide:intellij": 60, "ide:vscode": 40}}))[0]
    assert "server-side model policy first (reach 1)" in heavy.readme_md
    assert "Reach: CLI, VS Code, the Copilot app and JetBrains; **not** the cloud agent" in \
        _packs(extra=[finding("mcp-sprawl", team="t1")])[0].readme_md


def test_vscode_optin_files_hold_no_enforceable_managed_key() -> None:
    files = dict(_packs()[0].hooks)
    snippet = json.loads(files["vscode/settings.snippet.json"])
    assert snippet == {"github.copilot.chat.otel.dbSpanExporter.enabled": True}
    doc = files["vscode/agent-traces-optin.md"]
    assert "copilot.managed" not in doc and "managed-settings.json" not in doc
    assert "cannot enforce" in doc and "tokenbill copilot collect --source vscode" in doc
    assert "Never" in doc and "7 days" in doc
    for key in json.loads(files["copilot/managed-settings.patch.json"]):
        assert key not in snippet


def test_repo_ci_and_allowed_models_files() -> None:
    files = dict(_packs(include_tradeoffs=True)[0].hooks)
    allowed = files["repo/.github/allowed_models.txt"].splitlines()
    assert sum(1 for line in allowed if line.startswith("fallback:")) == 1
    assert allowed[-1].startswith("fallback: ")
    models = [line for line in allowed if line and not line.startswith(("#", "fallback:"))]
    assert models == sorted(models) and allowed[-1].split(": ")[1] in models
    assert "max-ai-credits: 450" in files["ci/agentic-workflows.md"]
    assert "token-usage.jsonl" in files["ci/agentic-workflows.md"]
    limits = files["ci/copilot-limits.md"]
    assert "--max-ai-credits N" in limits and "--billing-path copilot_direct" in limits
    no_trade = dict(_packs(include_tradeoffs=False)[0].hooks)
    assert "repo/.github/allowed_models.txt" not in no_trade
    capped = _packs(extra=[finding("ci-uncapped", detector="copilot.lanes",
                                   evidence=[ev("ci", suggested_max_ai_credits=80)])])
    assert "--max-ai-credits 80" in dict(capped[0].hooks)["ci/copilot-limits.md"]


def test_output_is_deterministic_across_runs_and_permutations() -> None:
    teams = teams_with({"t1": {"ide:intellij": 3, "ide:vscode": 7}, "t2": {"ide:vscode": 5}})
    plans, fs, acts, budgets = _world(scenarios=True, teams=teams)
    kw = dict(current={"model": "gpt-5.4", "telemetry": {"enabled": False}}, cohort_by="team",
              include_tradeoffs=True, today=TODAY)
    a = build_copilot_packs(plans, fs, acts, teams=teams, budgets=budgets, **kw)
    z = build_copilot_packs(tuple(reversed(plans)), list(reversed(fs)), acts,
                            teams=dict(reversed(list(teams.items()))),
                            budgets=list(reversed(budgets)), **kw)
    assert a == z
    assert a == build_copilot_packs(plans, fs, acts, teams=teams, budgets=budgets, **kw)
    acts2 = admin_actions(tuple(reversed(plans)), list(reversed(fs)), list(reversed(
        pools_unknown())), plans=[b.make_plan_evidence()], today=TODAY, teams=teams)
    assert acts2 == acts
    paths = [p for p, _ in a[0].hooks]
    assert paths == sorted(paths)


def test_canary_is_absent_from_every_file() -> None:
    bad = Fix(text=f"do {CANARY}", config_patch=(("copilot.managed.model", '"auto"'),),
              target="github-copilot", doc_url="https://docs.github.com")
    extra = [finding("auto-adoption", team="t2", title=f"t {CANARY}", summary=CANARY_EMAIL,
                     fix=bad, evidence=[ev(f"ref:{CANARY}", note=CANARY, n=3)])]
    current = {"model": "gpt-5.4", "telemetry": {"headers": {"Authorization": CANARY}},
               "allowedMcpServers": [CANARY_LOGIN]}
    packs = _packs(extra=extra, current=current, cohort_by="team",
                   teams=teams_with({"t1": {"ide:vscode": 4}}))
    for pack in packs:
        blobs = [pack.readme_md, pack.merge_patch_json, pack.rollback_patch_json,
                 *(text for _, text in pack.hooks), *(e.note for e in pack.entries)]
        b.assert_no_canary(*blobs)
        for blob in blobs:
            assert CANARY_LOGIN not in blob


def test_pseudonyms_never_leave_and_the_guard_fires() -> None:
    lic = [b.make_license(b.make_principal(i), team="t1",
                          last_activity_bucket="none_90d" if i < 3 else "0-7")
           for i in range(6)]
    act = [b.make_activity(b.make_principal(i), team="t1",
                           counts={"ide:intellij": 2, "ide:vscode": 8, "cli_requests": 1,
                                   "app_requests": 1})
           for i in range(4)]
    teams = team_counts(act, [], lic)
    assert teams == {"t1": {"app": 4, "cli": 4, "ide:intellij": 8, "ide:vscode": 32,
                            "idle_seats": 3, "n_people": 6, "seats": 6}}
    assert not P_RE.search(json.dumps(teams))
    with pytest.raises(PrivacyError):
        build_copilot_packs((), [], [], current=None, cohort_by="team",
                            include_tradeoffs=False, budgets=None,
                            teams={b.make_principal(1): {"n_people": 9, "ide:vscode": 1}},
                            otel_service_name="svc")


def test_team_counts_uses_activity_counts_rows_and_latest_seats() -> None:
    rows = [b.make_config("activity_counts", {"team": "agg", "n_people": 7, "ide:intellij": 3,
                                              "cli_requests": 2, "app_interactions": 1,
                                              "interactions": 6}),
            b.make_config("activity_counts", {"team": "agg", "n_people": 5, "ide:vscode": 4}),
            b.make_config("seat_counts", {"team": "agg", "n": 3})]
    old = b.make_license(b.make_principal(1), team="x", snapshot_date="2026-08-01",
                         last_activity_bucket="none_90d")
    new = dataclasses.replace(old, snapshot_date="2026-09-01", last_activity_bucket="0-7")
    got = team_counts([], rows, [new, old, b.make_license(b.make_principal(2), team=None)])
    assert got == {"agg": {"app": 1, "cli": 2, "ide:intellij": 3, "ide:vscode": 4,
                           "n_people": 7},
                   "x": {"idle_seats": 0, "n_people": 1, "seats": 1}}
    assert team_counts([b.make_activity(team=None)]) == {}
    for bad in (dict(activity=["x"]), dict(config=["x"]), dict(licenses=["x"])):
        with pytest.raises(UsageError):
            team_counts(**bad)  # type: ignore[arg-type]


def test_write_pack_writes_readme_and_hooks(tmp_path) -> None:
    packs = _packs(cohort_by="team", teams=teams_with({"Data Platform": {"ide:vscode": 3}}))
    written = write_pack(packs[0], tmp_path)
    assert tmp_path / "README.md" in written
    assert (tmp_path / "github" / "requests.jsonl").read_text(encoding="utf-8") == \
        dict(packs[0].hooks)["github/requests.jsonl"]
    team_written = write_pack(packs[1], tmp_path / "t")
    assert any(p.name.startswith("README-data-platform") for p in team_written)
    evil = dataclasses.replace(packs[0], hooks=(("../x.md", "x"),))
    with pytest.raises(UsageError):
        write_pack(evil, tmp_path)
    with pytest.raises(UsageError):
        write_pack(dataclasses.replace(packs[0], hooks=(("/etc/x", "x"),)), tmp_path)


def test_input_validation() -> None:
    plans, fs, acts, budgets = _world()
    base = dict(current=None, cohort_by=None, include_tradeoffs=False, teams=None)
    for bad in ({"findings": "x"}, {"findings": ["x"]}, {"actions": "x"}, {"actions": [1]},
                {"budgets": {"a": 1}}, {"budgets": [{"scenario": "gold", "text": "t"}]},
                {"budgets": [{"scenario": None, "text": "t", "request": {"method": "GET"}}]},
                {"teams": ["t"]}, {"include_tradeoffs": "yes"}, {"k": 0},
                {"today": "soon", "budgets": None}):
        args = dict(plans_by_scenario=plans, findings=fs, actions=acts, budgets=budgets,
                    **base)
        args.update(bad)
        with pytest.raises(UsageError):
            build_copilot_packs(**args)  # type: ignore[arg-type]
    assert JETBRAINS_ACTION_ID not in [a.action_id for a in acts]

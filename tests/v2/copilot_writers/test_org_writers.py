"""Config, metrics, seats, agent-task pages and the handoff UI downloads (addendum §5.4–§5.7,
§5.15–§5.17), read back with the local minimal readers."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY, CANARY_EMAIL, make_license
from tokenbill.core.catalog import editor_family
from tokenbill.synth import copilot_writers as w

from .helpers import canary_split, read_csv, read_jsonl
from .world import World, build_world


def _bodies(path: Path) -> list[tuple[str, dict]]:
    docs = read_jsonl(path)
    return [(d["request"]["path"], d["response"]) if "request" in d else ("", d) for d in docs]


def test_budgets_pages(world: World, full: tuple[Path, dict[str, Path]],
                       ident: w.Identity) -> None:
    _, files = full
    docs = read_jsonl(files["config/budgets.jsonl"])
    assert all("fetched_ms" in d and d["request"]["path"].startswith("/enterprises/")
               for d in docs)
    budgets = [b for d in docs if "budgets" in d["response"] for b in d["response"]["budgets"]]
    assert len(budgets) == 6
    assert any("budget_product_sku" in b for b in budgets)
    assert any("budget_product_skus" in b for b in budgets)
    assert any(b.get("budget_amount") == 500 for b in budgets)                 # int
    assert any(str(b.get("budget_amount")) == "1000.0" for b in budgets)       # 1000.0
    assert any(str(b.get("budget_amount")) == "250.5" for b in budgets)
    users = [b for b in budgets if b["budget_scope"] == "user"]
    assert len(users) == 2 and all(b["budget_amount"] == 0 for b in users)
    team_map = ident.team_map()
    assert all(team_map[b["user"]] == "platform" for b in users)
    assert len({b["user"] for b in users}) == 2
    states = [d for d in docs if d["request"]["path"].endswith("/user-states")]
    assert len(states) == 1
    rows = states[0]["response"]["user_states"]
    values = sorted(Decimal(str(r["consumed_amount"])) for r in rows)
    n = len(values)
    assert n == 6
    assert values[max(1, (50 * n + 99) // 100) - 1] == Decimal("12.5")
    assert values[max(1, (90 * n + 99) // 100) - 1] == Decimal("30")
    over = sum(1 for r in rows if Decimal(str(r["consumed_amount"]))
               >= Decimal(str(r["target_amount"])))
    assert over == 2


def test_cost_centers_and_org_billing(full: tuple[Path, dict[str, Path]],
                                      ident: w.Identity) -> None:
    _, files = full
    [(_, page)] = _bodies(files["config/cost_centers.jsonl"])
    ccs = {c["name"]: c for c in page["costCenters"]}
    assert "ai_credit_pool_state" in ccs["cc-platform"]
    assert "ai_credit_pool_state" not in ccs["cc-infra"]
    assert ccs["cc-infra"]["azure_subscription"].startswith("sub-")
    kinds = Counter(r["type"] for r in ccs["cc-platform"]["resources"])
    assert kinds == {"User": 6, "Team": 1, "Org": 1}
    users = {r["name"] for r in ccs["cc-platform"]["resources"] if r["type"] == "User"}
    assert all(ident.cost_center_map()[u] == "cc-platform" for u in users)
    orgs = dict(_bodies(files["config/org_copilot_billing.jsonl"]))
    assert orgs["/orgs/org-a/copilot/billing"]["seat_management_setting"] == "assign_selected"
    assert orgs["/orgs/org-b/copilot/billing"]["seat_management_setting"] == "assign_all"
    assert orgs["/orgs/org-a/copilot/billing"]["seat_breakdown"]["total"] == 11


def test_users_metrics_documented_fields(world: World, full: tuple[Path, dict[str, Path]],
                                         ident: w.Identity) -> None:
    _, files = full
    recs = read_jsonl(files["metrics/users-1-day.ndjson"])
    daily = [a for a in world.records["activity"]]
    assert len(recs) == len(daily)
    required = {"day", "enterprise_id", "user_id", "user_login", "ai_credits_used",
                "user_initiated_interaction_count", "code_generation_activity_count",
                "code_acceptance_activity_count", "loc_suggested_to_add_sum",
                "loc_suggested_to_delete_sum", "loc_added_sum", "loc_deleted_sum", "used_chat",
                "used_agent", "totals_by_ide", "totals_by_feature", "totals_by_model_feature"}
    assert all(required <= set(r) for r in recs)
    assert any("used_copilot_coding_agent" in r for r in recs)
    assert any("used_copilot_cloud_agent" in r for r in recs)
    cli = [r for r in recs if "totals_by_cli" in r]
    assert cli and set(cli[0]["totals_by_cli"]) == {"session_count", "request_count",
                                                     "prompt_count", "token_usage"}
    assert any("totals_by_copilot_app" in r for r in recs)
    assert any("totals_by_3rd_party_agent" in r for r in recs)
    credits = sum(Decimal(str(r["ai_credits_used"])) for r in recs)
    assert credits * 10_000_000 == sum(a.reported_cost_nano for a in daily)
    per_team: dict = defaultdict(int)
    team_map = ident.team_map()
    for r in recs:
        per_team[team_map[r["user_login"]]] += r["user_initiated_interaction_count"]
    want: dict = defaultdict(int)
    for a in daily:
        want[a.team] += dict(a.counts)["interactions"]
    assert dict(per_team) == dict(want)


def test_outcome_reports_and_user_teams(world: World, full: tuple[Path, dict[str, Path]],
                                        ident: w.Identity) -> None:
    _, files = full
    ent = read_jsonl(files["metrics/enterprise-1-day.ndjson"])
    assert [r["pull_requests"]["total_merged"] for r in ent] == [7, 8, 9]
    [(path, org)] = _bodies(files["metrics/organization-1-day.jsonl"])
    assert path == "/orgs/org-a/copilot/metrics/reports/organization-1-day"
    assert org["organization_id"] and org["daily_active_users"] == 11
    [repo] = read_jsonl(files["metrics/repos-1-day.ndjson"])
    assert repo["pull_requests"]["total_merged"] == 9 and "repo_name" not in repo
    teams = read_jsonl(files["metrics/user-teams-1-day.ndjson"])
    slugs = Counter(r["slug"] for r in teams)
    assert slugs == {"platform": 6, "infra": 5, "ops": 5}        # tiny (3) omitted, < 5 rule
    assert {r["day"] for r in teams} == {"2026-09-22"}
    assert all(ident.team_map()[r["user_login"]] == r["slug"] for r in teams)


def test_org_repo_rows_use_org_envelopes(tmp_path: Path) -> None:
    from tokenbill.core.records import OutcomeAggregate
    row = OutcomeAggregate(date_utc="2026-09-20", team="(org:org-b)", n_users=0, sessions=0,
                           commits=0, pull_requests=2, lines_added=0, lines_removed=0,
                           edits_accepted=0, edits_rejected=0,
                           source_kind="github.copilot_metrics.repos")
    files = w.write_metrics_ndjson([row], tmp_path)
    [(path, body)] = _bodies(files["metrics/org-repos-1-day.jsonl"])
    assert path == "/orgs/org-b/copilot/metrics/reports/repos-1-day"
    assert body["pull_requests"] == {"total_merged": 2}


def test_seats_pages(world: World, full: tuple[Path, dict[str, Path]],
                     ident: w.Identity) -> None:
    _, files = full
    docs = read_jsonl(files["seats/copilot_seats.jsonl"])
    paths = {d["request"]["path"] for d in docs}
    assert paths == {"/orgs/org-a/copilot/billing/seats", "/orgs/org-b/copilot/billing/seats"}
    seats = [s for d in docs for s in d["response"]["seats"]]
    lics = [x for x in world.records["licenses"]]
    assert len(seats) == len(lics)
    assert sum(1 for s in seats if "assigning_team" in s) == sum(
        1 for x in lics if x.assigned_via_team)
    surfaces = Counter(editor_family(s["last_activity_editor"]) for s in seats
                       if s["last_activity_editor"])
    assert surfaces == Counter(x.last_activity_surface for x in lics if x.last_activity_surface)
    assert sum(1 for s in seats if s["last_activity_at"] is None) == sum(
        1 for x in lics if x.last_activity_bucket == "none_90d")
    assert all(s["assignee"]["email"] == CANARY_EMAIL for s in seats)
    hits, leaks = canary_split(docs)
    assert hits == len(seats) * 2 and leaks == []


def test_seat_pages_paginate_and_enterprise_endpoint(tmp_path: Path) -> None:
    lics = [make_license(f"p_{i:020x}", org=None, snapshot_date="2026-09-01") for i in range(5)]
    files = w.write_seats_pages(lics, tmp_path, per_page=2)
    docs = read_jsonl(files["seats/copilot_seats.jsonl"])
    assert [d["request"]["query"]["page"] for d in docs] == ["1", "2", "3"]
    assert all(d["request"]["path"] == "/enterprises/synth-enterprise/copilot/billing/seats"
               for d in docs)
    assert all("organization" not in s for d in docs for s in d["response"]["seats"])


def test_agent_task_pages(world: World, full: tuple[Path, dict[str, Path]],
                          ident: w.Identity) -> None:
    _, files = full
    docs = read_jsonl(files["agents/agent_tasks.jsonl"])
    repo_pages = [d for d in docs if "request" in d]
    plain = [d for d in docs if "request" not in d]
    assert len(repo_pages) == 2 and len(plain) == 1
    assert all(d["request"]["path"].startswith("/agents/repos/") for d in repo_pages)
    sessions = [s for d in docs for t in (d["response"] if "request" in d else d)["tasks"]
                for s in t["sessions"]]
    assert len(sessions) == 3
    amounts = sorted(s["usage"]["amount"] for s in sessions if "usage" in s)
    assert amounts == [5_000_000_000, 123_456_789_000]
    ids = ident.id_team_map()
    assert Counter(ids.get(str(s["user"]["id"]), "(unmapped)") for s in sessions) == {
        "platform": 1, "infra": 1, "(unmapped)": 1}
    hits, leaks = canary_split(docs)
    assert hits == 3 * 3 and leaks == []


def test_activity_report(world: World, full: tuple[Path, dict[str, Path]],
                         ident: w.Identity) -> None:
    _, files = full
    header, rows, bom = read_csv(files["ui/copilot_activity_report.csv"])
    assert bom and tuple(header) == w.ACTIVITY_REPORT_HEADER
    assert len(rows) == len({x.principal for x in world.records["licenses"]})
    surfaces = {r["last_surface_used"] for r in rows}
    assert {"VS Code 1.126.0", "Copilot Chat", "Unspecified"} <= surfaces
    assert any(s.startswith("JetBrains") for s in surfaces)
    assert all(r["report_time"].startswith("2026-09-22T") for r in rows)
    idle = sum(1 for x in world.records["licenses"] if x.last_activity_bucket == "none_90d")
    assert sum(1 for r in rows if r["last_activity_at"] == "") == idle


def test_activity_report_from_own_snapshots(tmp_path: Path) -> None:
    lic = make_license("p_" + "3" * 20, source_kind="github.copilot_activity_report",
                       plan="unknown", assigned_via_team=None, last_activity_surface=None,
                       org=None)
    files = w.write_activity_report_csv([lic], tmp_path, quirks=False)
    _, rows, bom = read_csv(files["ui/copilot_activity_report.csv"])
    assert not bom and rows[0]["last_surface_used"] == "Unspecified"
    assert w.write_activity_report_csv([], tmp_path / "none") == {}


def test_dashboard_export(world: World, full: tuple[Path, dict[str, Path]],
                          ident: w.Identity) -> None:
    _, files = full
    docs = read_jsonl(files["ui/copilot_usage_dashboard.ndjson"])
    users = [d for d in docs if "user_login" in d]
    [ent] = [d for d in docs if "day_totals" in d]
    assert len(users) == len({a.principal for a in world.records["activity"]})
    assert all(d["report_start_day"] == "2026-08-26" and d["report_end_day"] == "2026-09-22"
               for d in docs)
    assert all("totals_by_cli" not in d and "used_cli" not in d for d in users)
    assert all(e["feature"] != "copilot_cli" for d in users for e in d["totals_by_feature"])
    assert len(ent["day_totals"]) == 3
    total = sum(d["user_initiated_interaction_count"] for d in users)
    assert total == sum(dict(a.counts)["interactions"] for a in world.records["activity"])


def test_dashboard_keeps_existing_28day_rows(tmp_path: Path) -> None:
    from tokenbill.core.builders import make_activity
    act = make_activity("p_" + "4" * 20, date_utc="2026-09-22",
                        source_kind="github.copilot_metrics.28day",
                        counts={"interactions": 40, "cli_requests": 3})
    files = w.write_dashboard_ndjson([act], tmp_path)
    [doc] = read_jsonl(files["ui/copilot_usage_dashboard.ndjson"])
    assert doc["report_end_day"] == "2026-09-22" and "totals_by_cli" not in doc
    assert w.write_dashboard_ndjson([], tmp_path / "none") == {}


def test_admin_answers_variants(world: World, tmp_path: Path) -> None:
    files = w.write_admin_answers(world, tmp_path / "a")
    doc = json.loads(files["admin/answers.json"].read_text())
    assert doc["schema"] == "tokenbill/copilot-admin-answers@1"
    assert doc["plan"] == {"org:org-a": "mixed", "org:org-b": "business"}
    assert doc["billing_mode"] == {"enterprise": "metered"}
    assert doc["capped_policy"] == {"cc:cc-platform": "block"}
    assert doc["pool_seats"] == {"org:org-a": {"business": 7}}
    assert doc["seat_policy"] == {"org:org-a": "assign_selected", "org:org-b": "assign_all"}
    assert doc["promo_eligible"] is True and doc["compliance"] == "none"
    assert doc["paid_usage_policy"] == "unknown"
    unknown = json.loads(w.write_admin_answers(world, tmp_path / "b", variant="plan_unknown")[
        "admin/answers.json"].read_text())
    assert "plan" not in unknown
    conflict = json.loads(w.write_admin_answers(world, tmp_path / "c", variant="plan_conflict")[
        "admin/answers.json"].read_text())
    assert conflict["plan"] == {"enterprise": "enterprise"}


def test_admin_answers_conflict_org() -> None:
    world = build_world()
    lics = world.records["licenses"]
    world.records["licenses"] = [make_license(x.principal, plan="enterprise", org=x.org,
                                              team=x.team, snapshot_date=x.snapshot_date)
                                 if x.org == "org-b" else x for x in lics]
    assert w._conflict_orgs(w._collect(world)) == ["org-b"]


@pytest.mark.parametrize("variant", [None, "plan_unknown", "plan_conflict"])
def test_world_variant_is_detected(tmp_path: Path, variant: str | None) -> None:
    world = build_world(variants=(variant,) if variant else ())
    files = w.write_world(world, tmp_path, handoff="ui")
    manifest = json.loads(files["MANIFEST.json"].read_text())
    assert manifest["answers_variant"] == variant
    assert CANARY not in files["admin/answers.json"].read_text()

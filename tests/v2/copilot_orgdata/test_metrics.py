"""github-copilot-metrics: users-1-day, team outcomes, entity PR rows, 28-day / dashboard files
(addendum §5.5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.adapters.github_metrics import (
    SOURCE_KIND,
    SOURCE_KIND_28DAY,
    SOURCE_KIND_REPOS,
    CopilotMetricsAdapter,
    ide_key,
    model_key,
)
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.catalog import editor_family
from tokenbill.core.errors import UsageError
from tokenbill.core.records import Attribution
from tokenbill.core.testing import assert_adapter_conforms, conformance_ingest_options

from .helpers import FIXTURES, assert_no_identity, opts, p_of, write_lines

METRICS = FIXTURES / "metrics"
ADAPTER = CopilotMetricsAdapter()


def _example() -> dict[str, Any]:
    return json.loads((METRICS / "users-1-day_example.ndjson").read_text(encoding="utf-8"))


def test_documented_example_record() -> None:
    res = ADAPTER.read(METRICS / "users-1-day_example.ndjson", opts())
    (day,) = res.activity
    assert day.reported_cost_nano == 125_000_000          # 12.5 credits × $0.01
    assert day.date_utc == "2025-10-01" and day.product == "github_copilot"
    assert day.principal == p_of(CANARY_LOGIN) and day.source_kind == SOURCE_KIND
    assert day.team == "platform" and day.cost_center == "cc-platform"
    counts = dict(day.counts)
    assert counts == {
        "interactions": 1, "code_generation": 3, "code_acceptance": 3, "loc_suggested_add": 34,
        "loc_suggested_delete": 6, "loc_added": 32, "loc_deleted": 6, "cli_sessions": 2,
        "cli_requests": 2, "cli_prompts": 2, "cli_prompt_tokens": 3800, "cli_output_tokens": 5000,
        "app_sessions": 1, "app_requests": 3, "app_prompts": 1, "app_prompt_tokens": 5400,
        "app_output_tokens": 4200, "mcp_distinct": 2, "skill_distinct": 3,
        "custom_agent_distinct": 2, "plugin_distinct": 1, "slash_cmd_distinct": 2,
        "third_party_agent_jobs": 4, "feature:code_completion": 1, "feature:copilot_app": 3,
        "ide:vscode": 0}
    assert day.flags == ("used_cli", "used_copilot_app")
    assert res.source.principal_key_id == opts().principal_key_id
    assert_no_identity(res, "login1", "github-mcp-server", "general-purpose")
    assert "dq.unknown_fields" not in {n.code for n in res.notes}


def test_undocumented_field_names_are_not_counted(tmp_path: Path) -> None:
    rec = _example()
    rec["loc_added"] = rec.pop("loc_added_sum")
    rec["brand_new_metric"] = 5
    res = ADAPTER.read(write_lines(tmp_path / "u.ndjson", [rec]), opts())
    counts = dict(res.activity[0].counts)
    assert "loc_added" not in counts
    (note,) = [n for n in res.notes if n.code == "dq.unknown_fields"]
    assert note.count == 2 and "loc_added" not in note.detail


def test_twelve_users_three_days_team_outcomes() -> None:
    res = ADAPTER.read(METRICS / "users-1-day_12x3.ndjson", opts())
    assert len(res.activity) == 36 and res.capabilities == frozenset({"activity", "outcomes"})
    rows = [(o.date_utc, o.team, o.n_users) for o in res.outcomes]
    assert rows == [(d, t, n) for d in ("2026-09-20", "2026-09-21", "2026-09-22")
                    for t, n in (("payments", 5), ("platform", 6))]
    assert all(o.n_users >= 5 and o.source_kind == SOURCE_KIND for o in res.outcomes)
    assert all(o.team not in ("mobile", "(unmapped)") for o in res.outcomes)
    (dropped,) = [n for n in res.notes if n.code == "dq.outcomes_suppressed"]
    assert dropped.count == 3 and res.stats["users_dropped"] == 3
    by_team = {(a.date_utc, a.team) for a in res.activity}
    assert ("2026-09-20", None) in by_team                    # dev-11: only in the 3-person team
    platform = [a for a in res.activity if a.team == "platform" and a.date_utc == "2026-09-20"]
    out = next(o for o in res.outcomes if o.team == "platform" and o.date_utc == "2026-09-20")
    assert out.lines_added == sum(dict(a.counts)["loc_added"] for a in platform)
    assert out.edits_accepted == sum(dict(a.counts)["code_acceptance"] for a in platform)
    assert_no_identity(res, "dev-02", "dev-11")


def test_small_groups_merge_into_other() -> None:
    k3 = ADAPTER.read(METRICS / "users-1-day_12x3.ndjson", opts(k_anonymity=6))
    teams = {(o.team, o.n_users) for o in k3.outcomes}
    assert teams == {("platform", 6), ("(other)", 6)}        # payments 5 + unmapped 1


def test_ide_counts_and_editor_families() -> None:
    res = ADAPTER.read(METRICS / "users-1-day_12x3.ndjson", opts())
    mixed = next(a for a in res.activity if a.principal == p_of("dev-03")
                 and a.date_utc == "2026-09-20")
    counts = dict(mixed.counts)
    assert counts["ide:vscode"] > 0 and counts["ide:intellij"] > 0
    ides = {k for a in res.activity for k in dict(a.counts) if k.startswith("ide:")}
    assert ides == {"ide:vscode", "ide:intellij", "ide:pycharm", "ide:goland"}
    assert {editor_family(k) for k in ides} == {"vscode", "jetbrains"}
    jetbrains_only = next(a for a in res.activity if a.principal == p_of("dev-02"))
    assert [k for k in dict(jetbrains_only.counts) if k.startswith("ide:")] == ["ide:intellij"]
    models = {k for a in res.activity for k in dict(a.counts) if k.startswith("model:")}
    assert models == {"model:auto", "model:claude-sonnet-4-6"}


@pytest.mark.parametrize(("raw", "key"), [
    ("vscode", "vscode"), ("IntelliJ", "intellij"), ("JetBrains Rider", "jetbrains-rider"),
    ("Visual Studio", "visual-studio"), ("", "unknown"), (None, "unknown"), ("!!!", "unknown")])
def test_ide_key(raw: object, key: str) -> None:
    assert ide_key(raw) == key


@pytest.mark.parametrize(("raw", "key"), [
    ("gpt-5.4", "gpt-5.4"), ("claude-sonnet-4.6", "claude-sonnet-4-6"), ("auto", "auto"),
    ("Others", "others"), ("unknown", "unknown"), ("Copilot code review", "unknown"),
    ("Auto: Claude Haiku 4.5", "claude-haiku-4-5"), (None, "unknown"), (7, "unknown")])
def test_model_key(raw: object, key: str) -> None:
    assert model_key(raw) == key


def test_enterprise_and_org_rows_are_never_added() -> None:
    ent = ADAPTER.read(METRICS / "enterprise-1-day.ndjson", opts())
    org = ADAPTER.read(METRICS / "organization-1-day.ndjson", opts())
    assert {o.team for o in ent.outcomes} == {"(enterprise)"}
    assert {o.team for o in org.outcomes} == {"(org:2002)"}
    first = ent.outcomes[0]
    assert first.pull_requests == 2 and first.n_users == 12 and first.lines_added == 54
    assert dict(first.extra) == {"prs_merged": 2, "prs_created_by_copilot": 1,
                                 "prs_merged_created_by_copilot": 1,
                                 "prs_reviewed_by_copilot": 2, "copilot_suggestions": 3,
                                 "copilot_applied_suggestions": 1}
    assert [o.pull_requests for o in org.outcomes] == [1, 2, 3]
    both = ADAPTER.read(METRICS, opts())
    enterprise_rows = [o for o in both.outcomes if o.team == "(enterprise)"
                       and o.source_kind == SOURCE_KIND]
    assert [o.pull_requests for o in enterprise_rows] == [2, 3, 4]   # never summed with orgs
    named = ADAPTER.read(METRICS / "organization-1-day.ndjson",
                         opts(attribution=Attribution(workspace_id="acme-eng")))
    assert {o.team for o in named.outcomes} == {"(org:acme-eng)"}


def test_repos_give_one_total_and_no_repo_ids() -> None:
    res = ADAPTER.read(METRICS / "repos-1-day.ndjson", opts())
    (row,) = res.outcomes
    assert row.team == "(enterprise)" and row.source_kind == SOURCE_KIND_REPOS
    assert row.pull_requests == 4 and dict(row.extra)["prs_created_by_copilot"] == 2
    assert row.n_users == 0 and res.activity == []
    assert_no_identity(res, "example-service", "900000001", "octodemo")


def test_repos_of_an_org_from_the_request_path(tmp_path: Path) -> None:
    rows = [json.loads(line) for line in (METRICS / "repos-1-day.ndjson").read_text(
        encoding="utf-8").splitlines()]
    env = {"request": {"path": "/orgs/acme-eng/copilot/metrics/reports/repos-1-day"},
           "response": rows}
    path = tmp_path / "repos.json"
    path.write_text(json.dumps(env), encoding="utf-8")
    assert [o.team for o in ADAPTER.read(path, opts()).outcomes] == ["(org:acme-eng)"]


def test_28day_dashboard_export_accepted_with_both_codes() -> None:
    res = ADAPTER.read(METRICS / "users-28-day_dashboard.ndjson", opts())
    codes = {n.code: n.count for n in res.notes}
    assert codes["dq.copilot_dashboard_export"] == 12 and codes["dq.copilot_28day_window"] == 12
    assert len(res.activity) == 12 and res.quarantined == []
    assert {a.date_utc for a in res.activity} == {"2026-09-22"}          # report_end_day
    assert {a.source_kind for a in res.activity} == {SOURCE_KIND_28DAY}
    assert {o.source_kind for o in res.outcomes} == {SOURCE_KIND_28DAY}   # never daily rows
    agg = ADAPTER.read(METRICS / "enterprise-28-day.ndjson", opts())
    assert [(o.date_utc, o.team, o.pull_requests) for o in agg.outcomes] == [
        ("2025-09-30", "(enterprise)", 2), ("2025-10-01", "(enterprise)", 3)]
    assert {o.source_kind for o in agg.outcomes} == {SOURCE_KIND_28DAY}
    assert {n.code for n in agg.notes} >= {"dq.copilot_dashboard_export", "dq.copilot_28day_window"}


def test_daily_row_wins_over_a_28day_row_of_the_same_day(tmp_path: Path) -> None:
    daily = _example()
    window = {k: v for k, v in daily.items() if k not in ("day", "day_partition")}
    window.update(report_start_day="2025-09-04", report_end_day="2025-10-01",
                  loc_added_sum=999)
    res = ADAPTER.read(write_lines(tmp_path / "mix.ndjson", [window, daily]), opts())
    (day,) = res.activity
    assert day.source_kind == SOURCE_KIND and dict(day.counts)["loc_added"] == 32
    assert res.stats["activity_28day_superseded"] == 1


def test_other_dashboard_shapes_are_quarantined() -> None:
    res = ADAPTER.read(METRICS / "dashboard_other_shape.ndjson", opts())
    assert [(q.locator, q.reason) for q in res.quarantined] == [
        ("line:2", "dashboard-shape-unverified")]
    assert len(res.activity) == 1
    assert_no_identity(res, "Daily active users")


def test_28day_window_needs_valid_days(tmp_path: Path) -> None:
    bad = [{"report_start_day": "2026-13-01", "report_end_day": "2026-09-22", "day_totals": []},
           {"report_end_day": "2026-09-22", "user_login": "x"},
           {"report_start_day": "2026-08-26", "report_end_day": "2026-09-22",
            "day_totals": [{"no_day": 1}, 5]}]
    res = ADAPTER.read(write_lines(tmp_path / "w.ndjson", bad), opts())
    assert sorted(q.reason for q in res.quarantined) == [
        "dashboard-shape-unverified", "dashboard-shape-unverified", "missing:day",
        "missing:day"]


def test_legacy_and_user_teams_and_links_are_not_stored(tmp_path: Path) -> None:
    legacy = ADAPTER.read(METRICS / "legacy_metrics.json", opts())
    assert legacy.activity == [] and legacy.outcomes == []
    assert {n.code: n.count for n in legacy.notes} == {"dq.copilot_legacy_metrics": 2}
    teams = ADAPTER.read(METRICS / "user-teams-1-day.ndjson", opts())
    assert teams.activity == [] and teams.stats["user_teams_records"] == 42
    assert_no_identity(teams, "dev-02")
    links = {"request": {"path": "/enterprises/acme/copilot/metrics/reports/users-1-day"},
             "response": {"download_links": [], "report_day": "2026-09-20"}}
    path = tmp_path / "links.json"
    path.write_text(json.dumps(links), encoding="utf-8")
    assert ADAPTER.read(path, opts()).stats["link_documents"] == 1


def test_duplicates_window_and_bad_values(tmp_path: Path) -> None:
    rec = _example()
    dup = dict(rec)
    neg = dict(rec, user_login="dev-02", ai_credits_used=-1)
    bad_count = dict(rec, user_login="dev-03", loc_added_sum="12")
    no_day = dict(rec, user_login="dev-04")
    no_day.pop("day")
    no_login = dict(rec)
    no_login.pop("user_login")
    shapeless = {"hello": 1}
    bad_ide = dict(rec, user_login="dev-05", totals_by_ide="vscode")
    path = write_lines(tmp_path / "u.ndjson", [rec, dup, neg, bad_count, no_day, no_login,
                                               shapeless, bad_ide])
    res = ADAPTER.read(path, opts())
    assert len(res.activity) == 1 and res.stats["duplicate_records"] == 1
    assert sorted(q.reason for q in res.quarantined) == [
        "bad_type:ai_credits_used", "bad_type:loc_added_sum", "bad_type:totals_by_ide",
        "missing:day", "missing:day", "missing:user_login"]
    windowed = ADAPTER.read(path, opts(since_ms=1_900_000_000_000))
    assert windowed.activity == [] and windowed.stats["outside_window"] >= 1


def test_json_array_and_envelope_forms(tmp_path: Path) -> None:
    rec = _example()
    arr = tmp_path / "arr.json"
    arr.write_text(json.dumps([rec], indent=2), encoding="utf-8")
    env = tmp_path / "env.json"
    env.write_text(json.dumps({"request": {"path": "/x"}, "response": {"status": 200,
                                                                      "body": json.dumps([rec])},
                               "fetched_at": "2026-09-25T00:00:00Z"}), encoding="utf-8")
    a, e = ADAPTER.read(arr, opts()), ADAPTER.read(env, opts())
    assert a.activity[0].counts == e.activity[0].counts
    assert e.activity[0].fetched_ms == 1_790_294_400_000


def test_principal_key_is_required() -> None:
    with pytest.raises(UsageError):
        ADAPTER.read(METRICS / "users-1-day_example.ndjson", opts(principal_key=None))
    with pytest.raises(UsageError):
        ADAPTER.read(METRICS / "users-1-day_example.ndjson", opts(identity_mode="central"))
    ADAPTER.read(METRICS / "enterprise-1-day.ndjson", opts(principal_key=None))  # no people


def test_sniff() -> None:
    for path in sorted(METRICS.iterdir()):
        assert ADAPTER.sniff(path, path.read_bytes()[:65536]), path.name
    for other in (FIXTURES / "config" / "budgets.json", FIXTURES / "seats" / "org_seats.json",
                  FIXTURES / "usage_records" / "usage_records.json"):
        assert not ADAPTER.sniff(other, other.read_bytes()[:65536]), other.name


@pytest.mark.parametrize(("name", "caps"), [
    ("users-1-day_example.ndjson", {"activity"}),
    ("users-1-day_12x3.ndjson", {"activity", "outcomes"}),
    ("enterprise-1-day.ndjson", {"outcomes"}),
    ("users-28-day_dashboard.ndjson", {"activity", "outcomes"}),
    ("repos-1-day.ndjson", {"outcomes"})])
def test_conforms(name: str, caps: set[str]) -> None:
    team_map = (("tb-canary-login-7f3a91", "platform"),
                *((f"dev-{i:02d}", "platform") for i in range(2, 13)))
    assert_adapter_conforms(ADAPTER, METRICS / name, expect_capabilities=caps,
                            opts=conformance_ingest_options(team_map=team_map))

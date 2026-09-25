"""Regenerate the CP-ORGDATA fixtures (synthetic data in GitHub's documented shapes).

Run from the repository root: ``uv run python tests/v2/fixtures/copilot_orgdata/build_fixtures.py``.
Output is deterministic (sorted keys, fixed values); ``test_fixtures.py`` checks the checked-in
files equal a fresh build. Shapes are derived field for field from the GHEC OpenAPI description
(ghec.json 1.1.4, 2026-09-23), the usage-metrics field reference and example schema; every value
is invented. ``CANARY``, ``CANARY_LOGIN`` and ``CANARY_EMAIL`` are planted in logins, e-mails,
names, prompts, repository names and bodies (see README.md in tests/v2/copilot_orgdata).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from tokenbill.core.builders import CANARY, CANARY_EMAIL, CANARY_LOGIN

HERE = Path(__file__).resolve().parent
ENTERPRISE = "acme"
ORG = "acme-eng"
FETCHED_MS = 1_790_337_600_000          # 2026-09-25T12:00:00Z
DAYS = ("2026-09-20", "2026-09-21", "2026-09-22")
#: 12 users (u01 is the canary login); teams: platform 6, payments 5, mobile 3 (below k).
USERS = (CANARY_LOGIN, *(f"dev-{i:02d}" for i in range(2, 13)))
TEAMS = {"platform": USERS[0:5] + (USERS[11],), "payments": USERS[5:10],
         "mobile": USERS[9:12]}


def envelope(path: str, body: Any, **extra: Any) -> dict[str, Any]:
    """A CP-PULL recording: ``{"request": {path, query}, "response": body}`` plus the fetch time
    (``fetched_ms`` unless *extra* gives ``fetched_at``)."""
    rec = {"request": {"path": path, "query": extra.pop("query", {})}, "response": body, **extra}
    if "fetched_at" not in rec:
        rec["fetched_ms"] = FETCHED_MS
    return rec


# ---------------------------------------------------------------------------------------------
# configuration (github-copilot-config)
# ---------------------------------------------------------------------------------------------

BUDGET_IDS = {n: f"b{n:02d}0000-0000-4000-8000-00000000000{n}" for n in range(1, 9)}


def budgets_page() -> dict[str, Any]:
    alerting = {"will_alert": True, "alert_recipients": ["enterprise-admin", CANARY_LOGIN]}
    rows = [
        {"id": BUDGET_IDS[1], "budget_type": "ProductPricing", "budget_product_skus": ["actions"],
         "budget_scope": "enterprise", "budget_amount": json_decimal("1000.0"),
         "prevent_further_usage": True, "budget_alerting": alerting},
        {"id": BUDGET_IDS[2], "budget_type": "SkuPricing",
         "budget_product_sku": "copilot_ai_credit", "budget_scope": "organization",
         "budget_entity_name": ORG, "budget_amount": 500,
         "prevent_further_usage": False,
         "budget_alerting": {"will_alert": False, "alert_recipients": []}},
        {"id": BUDGET_IDS[3], "budget_type": "BundlePricing", "budget_product_skus": ["ai_credits"],
         "budget_scope": "cost_center", "budget_entity_name": "cc-platform",
         "budget_amount": json_decimal("250.0"), "prevent_further_usage": True,
         "budget_alerting": {"will_alert": True, "alert_recipients": ["billing-manager"]}},
        {"id": BUDGET_IDS[4], "budget_type": "BundlePricing", "budget_product_skus": ["ai_credits"],
         "budget_scope": "user", "budget_entity_name": CANARY_LOGIN, "user": CANARY_LOGIN,
         "budget_amount": 0, "consumed_amount": 0, "prevent_further_usage": True,
         "expires_at": "2026-12-31",
         "budget_alerting": {"will_alert": False, "alert_recipients": []}},
        {"id": BUDGET_IDS[5], "budget_type": "BundlePricing", "budget_product_skus": ["ai_credits"],
         "budget_scope": "user", "user": USERS[6], "budget_amount": 50,
         "consumed_amount": json_decimal("12.34"), "prevent_further_usage": True,
         "budget_alerting": {"will_alert": False, "alert_recipients": []}},
        {"id": BUDGET_IDS[6], "budget_type": "ProductPricing", "budget_product_skus": ["actions"],
         "budget_scope": "repository", "budget_entity_name": f"{ORG}/secret-{CANARY}",
         "budget_amount": 75, "prevent_further_usage": False,
         "budget_alerting": {"will_alert": True, "alert_recipients": [CANARY_EMAIL]}},
        {"id": BUDGET_IDS[7], "budget_type": "BundlePricing", "budget_product_skus": ["ai_credits"],
         "budget_scope": "multi_user_cost_center", "budget_entity_name": "cc-data",
         "budget_amount": 2000, "prevent_further_usage": True,
         "budget_alerting": {"will_alert": True, "alert_recipients": []}},
        {"id": BUDGET_IDS[8], "budget_type": "ProductPricing",
         "budget_product_skus": ["copilot_for_business"], "budget_scope": "enterprise",
         "budget_amount": 25, "prevent_further_usage": True,
         "budget_alerting": {"will_alert": False, "alert_recipients": []}},
    ]
    return {"budgets": rows, "has_next_page": False, "total_count": len(rows)}


def user_states(consumed: list[str], target: str) -> dict[str, Any]:
    logins = [CANARY_LOGIN, *USERS[1:]]
    rows = [{"user": logins[i], "consumed_amount": json_decimal(c),
             "target_amount": json_decimal(target)} for i, c in enumerate(consumed)]
    return {"user_states": rows, "has_next_page": False, "total_count": len(rows)}


def cost_centers_page() -> dict[str, Any]:
    return {"costCenters": [
        {"id": "2eeb8ffe-6903-11ee-8c99-0242ac120002", "name": "cc-platform", "state": "active",
         "azure_subscription": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
         "resources": [{"type": "User", "name": CANARY_LOGIN}, {"type": "Team", "name": "platform"},
                       {"type": "Repo", "name": f"{ORG}/hello-{CANARY}"}],
         "ai_credit_pool_enabled": True,
         "ai_credit_pool_state": {"target_amount": json_decimal("21000.0"),
                                  "current_amount": json_decimal("7250.5")}},
        {"id": "3ffb9ffe-6903-11ee-8c99-0242ac120003", "name": "cc-data", "state": "active",
         "resources": [{"type": "User", "name": USERS[6]}, {"type": "Team", "name": "payments"},
                       {"type": "Org", "name": "acme-data"}],
         "ai_credit_pool_enabled": False},
        {"id": "4ffb9ffe-6903-11ee-8c99-0242ac120004", "name": "cc-retired", "state": "deleted",
         "resources": [{"type": "User", "name": USERS[3]}], "ai_credit_pool_enabled": False},
    ]}


def org_billing(setting: str, plan: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "seat_breakdown": {"total": 12, "added_this_cycle": 9, "pending_invitation": 0,
                           "pending_cancellation": 1, "active_this_cycle": 11,
                           "inactive_this_cycle": 1},
        "seat_management_setting": setting, "ide_chat": "enabled", "platform_chat": "disabled",
        "cli": "unconfigured", "public_code_suggestions": "block"}
    if plan is not None:
        body["plan_type"] = plan
    return body


# ---------------------------------------------------------------------------------------------
# usage metrics (github-copilot-metrics)
# ---------------------------------------------------------------------------------------------

#: The documented users-1-day example record (example-schema page), user_login replaced by the
#: canary login; every other field as published.
DOC_USER_RECORD: dict[str, Any] = {
    "ai_adoption_phase": {"phase": "Phase 2", "phase_number": 2, "version": "v1"},
    "ai_credits_used": "__DEC__12.5", "code_acceptance_activity_count": 3,
    "code_generation_activity_count": 3, "day": "2025-10-01",
    "distinct_custom_agent_use_count": 2, "distinct_mcp_use_count": 2,
    "distinct_plugin_use_count": 1, "distinct_skill_use_count": 3,
    "distinct_slash_cmd_use_count": 2, "enterprise_id": "1", "loc_added_sum": 32,
    "loc_deleted_sum": 6, "loc_suggested_to_add_sum": 34, "loc_suggested_to_delete_sum": 6,
    "totals_by_cli": {"last_known_cli_version": {"cli_version": "1.0.8",
                                                 "sampled_at": "2025-10-01T00:01:43.000Z"},
                      "prompt_count": 2, "request_count": 2, "session_count": 2,
                      "token_usage": {"avg_tokens_per_request": "__DEC__4400.0",
                                      "output_tokens_sum": 5000, "prompt_tokens_sum": 3800}},
    "totals_by_copilot_app": {"prompt_count": 1, "request_count": 3, "session_count": 1,
                              "token_usage": {"avg_tokens_per_request": "__DEC__3200.0",
                                              "output_tokens_sum": 4200,
                                              "prompt_tokens_sum": 5400}},
    "totals_by_custom_agent": [{"custom_agent": "general-purpose", "interaction_count": 4},
                               {"custom_agent": "other", "interaction_count": 2}],
    "totals_by_3rd_party_agent": [
        {"agent_id": "2246796", "agent_name": "Claude (Anthropic)",
         "user_initiated_interaction_count": 2},
        {"agent_id": "2248422", "agent_name": "Codex (OpenAI)",
         "user_initiated_interaction_count": 2}],
    "totals_by_feature": [
        {"code_acceptance_activity_count": 1, "code_generation_activity_count": 1,
         "feature": "code_completion", "loc_added_sum": 8, "loc_deleted_sum": 0,
         "loc_suggested_to_add_sum": 10, "loc_suggested_to_delete_sum": 0,
         "user_initiated_interaction_count": 0},
        {"code_acceptance_activity_count": 2, "code_generation_activity_count": 2,
         "feature": "copilot_app", "loc_added_sum": 24, "loc_deleted_sum": 6,
         "loc_suggested_to_add_sum": 24, "loc_suggested_to_delete_sum": 6,
         "user_initiated_interaction_count": 1}],
    "totals_by_ide": [
        {"code_acceptance_activity_count": 1, "code_generation_activity_count": 1, "ide": "vscode",
         "last_known_ide_version": {"ide_version": "1.85.0",
                                    "sampled_at": "2025-10-01T00:00:02.000Z"},
         "last_known_plugin_version": {"plugin": "", "plugin_version": "",
                                       "sampled_at": "2025-10-01T00:00:02.000Z"},
         "loc_added_sum": 8, "loc_deleted_sum": 0, "loc_suggested_to_add_sum": 10,
         "loc_suggested_to_delete_sum": 0, "user_initiated_interaction_count": 0}],
    "totals_by_language_feature": [
        {"code_acceptance_activity_count": 1, "code_generation_activity_count": 1,
         "feature": "code_completion", "language": "unknown", "loc_added_sum": 8,
         "loc_deleted_sum": 0, "loc_suggested_to_add_sum": 10, "loc_suggested_to_delete_sum": 0},
        {"code_acceptance_activity_count": 2, "code_generation_activity_count": 2,
         "feature": "copilot_app", "language": "markdown", "loc_added_sum": 24,
         "loc_deleted_sum": 6, "loc_suggested_to_add_sum": 24, "loc_suggested_to_delete_sum": 6}],
    "totals_by_language_model": [],
    "totals_by_mcp": [{"interaction_count": 8, "mcp": "github-mcp-server"},
                      {"interaction_count": 3, "mcp": "other"}],
    "totals_by_model_feature": [],
    "totals_by_plugin": [{"interaction_count": 2, "plugin": "other"}],
    "totals_by_skill": [{"interaction_count": 5, "skill": "other"}],
    "totals_by_slash_cmd": [{"interaction_count": 3, "slash_cmd": "/plan"},
                            {"interaction_count": 1, "slash_cmd": "custom"}],
    "used_agent": False, "used_chat": False, "used_cli": True, "used_copilot_app": True,
    "used_copilot_cloud_agent": False, "used_copilot_code_review_active": None,
    "used_copilot_code_review_passive": None, "used_copilot_coding_agent": False,
    "user_id": 1, "user_login": CANARY_LOGIN, "user_initiated_interaction_count": 1,
    "etl_id": "green", "day_partition": "2025-10-01", "entity_id_partition": 1,
}

#: IDE mix of the 12 users: VS Code only, JetBrains only (IntelliJ / PyCharm), and mixed.
_IDES = {0: ("vscode",), 1: ("intellij",), 2: ("vscode", "intellij"), 3: ("pycharm",),
         4: ("vscode",), 5: ("vscode", "goland")}


def user_day(i: int, login: str, day: str, d: int) -> dict[str, Any]:
    """User *i* on day index *d*: counts vary by user and day; IDEs per ``_IDES``."""
    base = 3 + i + 2 * d
    ides = _IDES[i % 6]
    per_ide = [{"ide": ide, "user_initiated_interaction_count": base + j,
                "code_generation_activity_count": base, "code_acceptance_activity_count": 1,
                "loc_added_sum": 2, "loc_deleted_sum": 0, "loc_suggested_to_add_sum": 3,
                "loc_suggested_to_delete_sum": 0} for j, ide in enumerate(ides)]
    rec: dict[str, Any] = {
        "day": day, "enterprise_id": "1", "user_id": 1000 + i, "user_login": login,
        "ai_credits_used": json_decimal(f"{i + d}.{(i * 7 + d) % 10}"),
        "user_initiated_interaction_count": sum(e["user_initiated_interaction_count"]
                                                for e in per_ide),
        "code_generation_activity_count": base * len(ides),
        "code_acceptance_activity_count": len(ides), "loc_suggested_to_add_sum": 3 * len(ides),
        "loc_suggested_to_delete_sum": 0, "loc_added_sum": 2 * len(ides) + i,
        "loc_deleted_sum": d, "used_agent": i % 2 == 0, "used_chat": True,
        "used_cli": i % 4 == 0, "used_copilot_app": False, "used_copilot_cloud_agent": i == 3,
        "used_copilot_coding_agent": i == 3, "used_copilot_code_review_active": None,
        "used_copilot_code_review_passive": i % 3 == 0 or None,
        "distinct_mcp_use_count": i % 3, "totals_by_ide": per_ide,
        "totals_by_feature": [{"feature": "chat_panel_agent_mode",
                               "user_initiated_interaction_count": base,
                               "code_generation_activity_count": 1},
                              {"feature": "code_completion", "user_initiated_interaction_count": 0,
                               "code_generation_activity_count": base}],
        "totals_by_model_feature": [{"model": "claude-sonnet-4.6",
                                     "feature": "chat_panel_agent_mode",
                                     "user_initiated_interaction_count": base,
                                     "code_generation_activity_count": 1},
                                    {"model": "auto", "feature": "chat_panel_ask_mode",
                                     "user_initiated_interaction_count": 1,
                                     "code_generation_activity_count": 0}],
        "totals_by_mcp": [{"mcp": f"private-{CANARY}", "interaction_count": 1}],
        "etl_id": "green", "day_partition": day, "entity_id_partition": 1,
    }
    if i % 4 == 0:
        rec["totals_by_cli"] = {"session_count": 1, "request_count": 4 + d, "prompt_count": 2,
                                "token_usage": {"prompt_tokens_sum": 1200 * (i + 1),
                                                "output_tokens_sum": 300 * (i + 1),
                                                "avg_tokens_per_request": None}}
    return rec


def user_teams_records() -> list[dict[str, Any]]:
    rows = []
    team_ids = {"platform": 42, "payments": 43, "mobile": 44}
    for day in DAYS:
        for slug in sorted(TEAMS):
            for login in TEAMS[slug]:
                rows.append({"user_id": 1000 + USERS.index(login), "user_login": login,
                             "day": day, "enterprise_id": "1", "team_id": team_ids[slug],
                             "slug": slug})
    return rows


#: The documented enterprise 28-day example's daily totals (example-schema page), as a 1-day
#: aggregated record per day.
def enterprise_day(day: str, merged: int) -> dict[str, Any]:
    return {
        "day": day, "enterprise_id": "1", "daily_active_users": 12, "weekly_active_users": 12,
        "monthly_active_users": 12, "monthly_active_chat_users": 10,
        "monthly_active_agent_users": 6, "code_acceptance_activity_count": 40,
        "code_generation_activity_count": 90, "user_initiated_interaction_count": 120,
        "loc_added_sum": 54, "loc_deleted_sum": 6, "loc_suggested_to_add_sum": 59,
        "loc_suggested_to_delete_sum": 6, "totals_by_ide": [], "totals_by_feature": [],
        "totals_by_language_feature": [], "totals_by_language_model": [],
        "totals_by_model_feature": [],
        "pull_requests": {"total_created": 4, "total_reviewed": 3, "total_merged": merged,
                          "median_minutes_to_merge": json_decimal("2.5"),
                          "total_suggestions": 5, "total_applied_suggestions": 2,
                          "total_created_by_copilot": 1, "total_reviewed_by_copilot": 2,
                          "total_merged_created_by_copilot": 1,
                          "total_merged_reviewed_by_copilot": 1,
                          "total_copilot_suggestions": 3, "total_copilot_applied_suggestions": 1,
                          "copilot_suggestions_by_comment_type": []},
        "etl_id": "green", "day_partition": day, "entity_id_partition": 1,
    }


def repos_records() -> list[dict[str, Any]]:
    """The documented repos-1-day rows (example-schema page), repo names canary-planted."""
    def pr(created: int, merged: int, by_copilot: int) -> dict[str, Any]:
        return {"total_reviewed": 1, "total_created": created,
                "total_created_by_copilot": by_copilot,
                "total_reviewed_by_copilot": 1, "total_merged": merged,
                "median_minutes_to_merge": json_decimal("372.62"), "total_suggestions": 0,
                "total_applied_suggestions": 0, "total_merged_created_by_copilot": by_copilot,
                "total_copilot_suggestions": 0, "total_copilot_applied_suggestions": 1,
                "total_merged_reviewed_by_copilot": 1, "copilot_suggestions_by_comment_type": []}
    return [
        {"day": "2026-07-14", "enterprise_id": "1001", "organization_id": "2002",
         "repo_id": 900000001, "repo_owner_name": "octodemo-metrics",
         "repo_name": f"example-service-alpha-{CANARY}", "repo_visibility": "INTERNAL",
         "pull_requests": pr(1, 1, 1)},
        {"day": "2026-07-14", "enterprise_id": "1001", "organization_id": "2002",
         "repo_id": 900000003, "repo_owner_name": "octodemo-metrics",
         "repo_name": "example-service-gamma", "repo_visibility": "INTERNAL",
         "pull_requests": pr(0, 1, 0)},
        {"day": "2026-07-14", "enterprise_id": "1001", "organization_id": "2002",
         "repo_id": 900000010, "repo_owner_name": "octodemo-metrics",
         "repo_name": "example-service-delta", "repo_visibility": "PRIVATE",
         "pull_requests": pr(0, 2, 1)},
    ]


def enterprise_28day(enterprise_id: str, days: tuple[str, ...]) -> dict[str, Any]:
    """The documented enterprise 28-day wrapper: window at the top, one entry per day."""
    entries = [enterprise_day(day, 2 + d) for d, day in enumerate(days)]
    for entry in entries:
        entry["enterprise_id"] = enterprise_id
    return {"day_totals": entries, "enterprise_id": enterprise_id,
            "copilot_feature_engagement": {"active_user_count": 2, "totals_by_feature": [
                {"feature": "code_completion", "engaged_user_count": 2}]},
            "report_end_day": "2025-10-01", "report_start_day": "2025-09-04", "etl_id": "green",
            "day_partition": "2025-10-01", "entity_id_partition": 1}


def user_28day(i: int, login: str) -> dict[str, Any]:
    rec = user_day(i, login, "x", 0)
    for key in ("day", "day_partition"):
        rec.pop(key)
    rec["report_start_day"] = "2026-08-26"
    rec["report_end_day"] = "2026-09-22"
    return rec


LEGACY = [{"date": "2024-06-24", "total_active_users": 24, "total_engaged_users": 20,
           "copilot_ide_code_completions": {"total_engaged_users": 20, "languages": [],
                                            "editors": []}},
          {"date": "2024-06-25", "total_active_users": 22, "total_engaged_users": 19}]


# ---------------------------------------------------------------------------------------------
# seats (github-copilot-seats)
# ---------------------------------------------------------------------------------------------

JETBRAINS_EDITOR = "JetBrains-IU/242.23726.103/copilot-intellij/1.5.52-241"   # VERIFY string


def assignee(login: str, uid: int) -> dict[str, Any]:
    return {"login": login, "id": uid, "node_id": f"U_{uid}", "name": f"Name {CANARY}",
            "email": CANARY_EMAIL if login == CANARY_LOGIN else f"{login}@example.com",
            "avatar_url": f"https://github.com/images/{login}.gif", "gravatar_id": "",
            "url": f"https://api.github.com/users/{login}",
            "html_url": f"https://github.com/{login}", "type": "User", "site_admin": False}


def seat(login: str, uid: int, *, org: str | None, activity: str | None,
         editor: str | None = "vscode/1.77.3/copilot/1.86.82", plan: str | None = "business",
         team: bool = False, pending: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "created_at": "2026-03-01T09:00:00-06:00", "updated_at": "2026-03-02T09:00:00-06:00",
        "pending_cancellation_date": pending, "last_activity_at": activity,
        "last_activity_editor": editor, "last_authenticated_at": activity,
        "assignee": assignee(login, uid)}
    if plan is not None:
        row["plan_type"] = plan
    if org is not None:
        row["organization"] = {"login": org, "id": 7, "url": f"https://api.github.com/orgs/{org}",
                               "description": f"org {CANARY}"}
    if team:
        row["assigning_team"] = {"id": 1, "slug": "platform", "name": "Platform",
                                 "description": f"team {CANARY}",
                                 "html_url": "https://github.com/orgs/acme-eng/teams/platform"}
    return row


#: Snapshot 2026-09-20: last activity 0, 7, 8, 30, 31, 90, 91 days before, and never.
BUCKET_DAYS = (("2026-09-20T08:00:00Z", "0-7"), ("2026-09-13T23:59:59Z", "0-7"),
               ("2026-09-12T12:00:00Z", "8-30"), ("2026-08-21T00:00:00Z", "8-30"),
               ("2026-08-20T23:00:00Z", "31-90"), ("2026-06-22T10:00:00Z", "31-90"),
               ("2026-06-21T10:00:00Z", "none_90d"), (None, "none_90d"))


def enterprise_seats() -> dict[str, Any]:
    rows = [
        seat(CANARY_LOGIN, 1000, org=ORG, activity="2026-09-19T17:00:00-06:00", team=True),
        seat(CANARY_LOGIN, 1000, org="acme-data", activity="2026-09-01T10:00:00Z",
             editor=JETBRAINS_EDITOR, plan="enterprise"),
    ]
    for j, (ts, _) in enumerate(BUCKET_DAYS):
        rows.append(seat(USERS[j + 1], 1001 + j, org=ORG, activity=ts, plan=None if j == 7 else
                         "business", pending="2026-10-01" if j == 6 else None,
                         editor=None if ts is None else "vscode/1.77.3/copilot/1.86.82"))
    return {"total_seats": len(rows) - 1, "seats": rows}


def org_seats() -> dict[str, Any]:
    rows = [seat(USERS[9], 1009, org=None, activity="2026-09-18T10:00:00Z", plan="business"),
            seat(USERS[10], 1010, org=None, activity="2026-09-18T10:00:00Z", plan="business",
                 team=True, editor=JETBRAINS_EDITOR)]
    return {"total_seats": 2, "seats": rows}


# ---------------------------------------------------------------------------------------------
# agent tasks (github-agent-tasks) and usage records (github-usage-records)
# ---------------------------------------------------------------------------------------------


def task(task_id: str, sessions: list[dict[str, Any]], artifacts: list[str]) -> dict[str, Any]:
    return {"id": task_id, "url": f"https://api.github.com/agents/tasks/{task_id}",
            "name": f"Fix the login button {CANARY}", "creator": {"id": 1000},
            "creator_type": "user", "owner": {"id": 7}, "repository": {"id": 1296269},
            "state": sessions[-1]["state"] if sessions else "queued",
            "session_count": len(sessions),
            "artifacts": [{"provider": "github", "type": t,
                           "data": {"id": 42} if t == "pull" else
                           {"head_ref": f"copilot/{CANARY}", "base_ref": "main"}}
                          for t in artifacts],
            "archived_at": None, "created_at": "2026-09-20T10:00:00Z",
            "updated_at": "2026-09-20T11:00:00Z", "sessions": sessions}


def session(sid: str, state: str, usage: dict[str, Any] | None, model: str = "claude-sonnet-4.6",
            user: int = 1000) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": sid, "name": f"session {CANARY}", "user": {"id": user}, "owner": {"id": 7},
        "repository": {"id": 1296269}, "task_id": "t", "state": state,
        "created_at": "2026-09-20T10:00:00Z", "updated_at": "2026-09-20T11:00:00Z",
        "completed_at": "2026-09-20T10:45:00Z", "prompt": f"Please fix {CANARY}",
        "head_ref": f"copilot/fix-{CANARY}", "base_ref": "main", "model": model}
    if usage is not None:
        row["usage"] = usage
    if state == "failed":
        row["error"] = {"message": f"boom {CANARY}"}
    return row


def repo_tasks() -> list[dict[str, Any]]:
    t1 = task("a1b2c3d4-0000-4000-8000-000000000001", [
        session("s1a2b3c4-0000-4000-8000-000000000001", "completed",
                {"type": "ai_credits", "amount": 23_284_800_000}),
        session("s1a2b3c4-0000-4000-8000-000000000002", "failed",
                {"type": "ai_credits", "amount": 1_000_000_000}, model="Auto: GPT-5.4"),
    ], ["branch", "pull"])
    t2 = task("a1b2c3d4-0000-4000-8000-000000000002", [
        session("s1a2b3c4-0000-4000-8000-000000000003", "timed_out",
                {"type": "ai_credits", "amount": 150_000_000}, user=1006),
        session("s1a2b3c4-0000-4000-8000-000000000004", "completed",
                {"type": "premium_requests", "amount": json_decimal("1.5")}),
    ], [])
    return [envelope(f"/agents/repos/{ORG}/web-{CANARY}/tasks/{t1['id']}", t1),
            envelope(f"/agents/repos/{ORG}/api/tasks/{t2['id']}", t2)]


def user_tasks() -> dict[str, Any]:
    t = task("a1b2c3d4-0000-4000-8000-000000000009", [
        session("s1a2b3c4-0000-4000-8000-000000000009", "completed",
                {"type": "ai_credits", "amount": 500_000_000})], ["pull"])
    return envelope(f"/agents/tasks/{t['id']}", t)


def task_list_page() -> dict[str, Any]:
    t = task("a1b2c3d4-0000-4000-8000-00000000000a", [], ["pull"])
    t.pop("sessions")
    return envelope(f"/agents/repos/{ORG}/api/tasks", {"tasks": [t], "total_active_count": 1})


USAGE_RECORDS = [
    {"type": "request", "user_id": 12345, "enterprise_id": 1, "github_request_id": "req-abc-123",
     "endpoint": "/chat/completions",
     "body": json.dumps({"messages": [{"role": "user", "content": f"Hello {CANARY}"}]}),
     "@timestamp": 1719600000000},
    {"type": "response", "user_id": 12345, "enterprise_id": 1, "github_request_id": "req-abc-123",
     "endpoint": "/chat/completions",
     "body": json.dumps({"choices": [{"message": {"content": f"Hi {CANARY}"}}],
                         "github_request_id": "nested"}),
     "@timestamp": 1719600000500},
]


# ---------------------------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------------------------


def json_decimal(text: str) -> str:
    """A placeholder for a JSON number written verbatim (``1000.0`` stays ``1000.0``)."""
    return f"__DEC__{text}"


def _fix_decimals(text: str) -> str:
    out, i = [], 0
    marker = '"__DEC__'
    while True:
        j = text.find(marker, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        end = text.index('"', j + len(marker))
        out.append(text[i:j])
        out.append(text[j + len(marker):end])
        i = end + 1


def write(rel: str, obj: Any, *, ndjson: bool = False) -> None:
    path = HERE / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if ndjson:
        text = "".join(_fix_decimals(json.dumps(o, sort_keys=True, ensure_ascii=False)) + "\n"
                       for o in obj)
    else:
        text = _fix_decimals(json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False)) + "\n"
    path.write_text(text, encoding="utf-8")


def build() -> list[str]:
    """Write every fixture; returns the relative paths written."""
    written: list[str] = []

    def out(rel: str, obj: Any, **kw: Any) -> None:
        write(rel, obj, **kw)
        written.append(rel)

    ent = f"/enterprises/{ENTERPRISE}/settings/billing"
    out("config/budgets.json", envelope(f"{ent}/budgets", budgets_page()))
    out("config/budget_user_states_7.json",
        envelope(f"{ent}/budgets/{BUDGET_IDS[7]}/user-states",
                 user_states(["1", "2", "3", "4", "5", "6", "100"], "50")))
    out("config/budget_user_states_3.json",
        envelope(f"{ent}/budgets/{BUDGET_IDS[3]}/user-states",
                 user_states(["10", "20.5", "300"], "250.0")))
    out("config/cost_centers.json", envelope(f"{ent}/cost-centers", cost_centers_page()))
    out("config/org_billing.jsonl", [
        envelope(f"/orgs/org-{s.replace('_', '-')}/copilot/billing", org_billing(s, p))
        for s, p in (("assign_all", "business"), ("assign_selected", "enterprise"),
                     ("disabled", "business"), ("unconfigured", None))], ndjson=True)
    out("config/org_billing_bare.json", org_billing("assign_selected", "business"))
    out("config/budgets_bare.json", {"budgets": [copy.deepcopy(budgets_page()["budgets"][0])]})
    out("metrics/users-1-day_example.ndjson", [DOC_USER_RECORD], ndjson=True)
    out("metrics/users-1-day_12x3.ndjson",
        [user_day(i, login, day, d) for d, day in enumerate(DAYS)
         for i, login in enumerate(USERS)], ndjson=True)
    out("metrics/user-teams-1-day.ndjson", user_teams_records(), ndjson=True)
    out("metrics/enterprise-1-day.ndjson",
        [enterprise_day(day, 2 + d) for d, day in enumerate(DAYS)], ndjson=True)
    org_days = []
    for d, day in enumerate(DAYS):
        rec = enterprise_day(day, 1 + d)
        rec["organization_id"] = "2002"
        org_days.append(rec)
    out("metrics/organization-1-day.ndjson", org_days, ndjson=True)
    out("metrics/repos-1-day.ndjson", repos_records(), ndjson=True)
    out("metrics/enterprise-28-day.ndjson", [enterprise_28day("1", ("2025-09-30", "2025-10-01"))],
        ndjson=True)
    out("metrics/users-28-day_dashboard.ndjson",
        [user_28day(i, login) for i, login in enumerate(USERS)], ndjson=True)
    bad = {"report_start_day": "2026-08-26", "report_end_day": "2026-09-22",
           "charts": [{"title": f"Daily active users {CANARY}", "values": [1, 2]}]}
    out("metrics/dashboard_other_shape.ndjson", [user_28day(0, USERS[0]), bad], ndjson=True)
    out("metrics/legacy_metrics.json", LEGACY)
    out("seats/enterprise_seats.json",
        envelope(f"/enterprises/{ENTERPRISE}/copilot/billing/seats", enterprise_seats(),
                 fetched_at="2026-09-20T12:00:00Z"))
    out("seats/org_seats.json", envelope(f"/orgs/{ORG}/copilot/billing/seats", org_seats()))
    out("agent_tasks/repo_tasks.jsonl", repo_tasks(), ndjson=True)
    out("agent_tasks/user_tasks.json", user_tasks())
    out("agent_tasks/task_list.json", task_list_page())
    out("usage_records/usage_records.json", USAGE_RECORDS)
    out("usage_records/usage_records.ndjson", USAGE_RECORDS, ndjson=True)
    return written


if __name__ == "__main__":
    for rel in build():
        print(rel)

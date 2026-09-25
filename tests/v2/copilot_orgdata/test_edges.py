"""Defensive branches of the CP-ORGDATA adapters (malformed pages, windows, overflow)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tokenbill.adapters.github_agent_tasks import AgentTasksAdapter
from tokenbill.adapters.github_config import CopilotConfigAdapter
from tokenbill.adapters.github_metrics import SOURCE_KIND_REPOS, CopilotMetricsAdapter
from tokenbill.core.records import MAX_TOKENS

from .helpers import FIXTURES, opts, write_lines

METRICS = CopilotMetricsAdapter()
CONFIG = CopilotConfigAdapter()
TASKS = AgentTasksAdapter()


def _agg(day: str, **kw: Any) -> dict[str, Any]:
    rec = {"day": day, "enterprise_id": "1", "daily_active_users": 7,
           "pull_requests": {"total_merged": 1}}
    rec.update(kw)
    return rec


def test_aggregated_records_window_duplicates_and_unknown_fields(tmp_path: Path) -> None:
    rows = [_agg("2026-09-20", new_field=1), _agg("2026-09-20"), _agg("2020-01-01"),
            _agg("2026-09-21", daily_active_users=-1),
            {"day": "2026-09-20", "repo_id": 1, "pull_requests": {"total_merged": MAX_TOKENS}},
            {"day": "2026-09-20", "repo_id": 2, "pull_requests": {"total_merged": 5}},
            {"day": "2019-01-01", "repo_id": 3, "pull_requests": {}}]
    res = METRICS.read(write_lines(tmp_path / "a.ndjson", rows),
                       opts(since_ms=1_780_000_000_000))
    assert [(o.date_utc, o.source_kind) for o in res.outcomes] == [
        ("2026-09-20", "github.copilot_metrics"), ("2026-09-20", SOURCE_KIND_REPOS)]
    assert res.stats["duplicate_records"] == 1 and res.stats["outside_window"] == 2
    assert sorted(q.reason for q in res.quarantined) == [
        "bad_type:daily_active_users", "bad_type:pull_requests"]
    assert {n.code: n.count for n in res.notes}["dq.unknown_fields"] == 1


def test_breakdown_entries_must_be_objects(tmp_path: Path) -> None:
    rec = {"day": "2026-09-20", "user_login": "a", "totals_by_feature": [3],
           "totals_by_3rd_party_agent": ["x"]}
    res = METRICS.read(write_lines(tmp_path / "u.ndjson", [rec]), opts())
    assert [q.reason for q in res.quarantined] == ["not_object"]
    big = {"day": "2026-09-20", "user_login": "b",
           "totals_by_ide": [{"ide": "vscode", "user_initiated_interaction_count": MAX_TOKENS},
                             {"ide": "vscode", "user_initiated_interaction_count": 1}]}
    res = METRICS.read(write_lines(tmp_path / "v.ndjson", [big]), opts())
    assert [q.reason for q in res.quarantined] == ["bad_type:ide:vscode"]


def test_envelope_with_a_list_of_records_and_non_objects(tmp_path: Path) -> None:
    env = {"request": {"path": "/enterprises/a/copilot/metrics/reports/users-1-day"},
           "response": [{"day": "2026-09-20", "user_login": "a"}, 7]}
    path = tmp_path / "e.json"
    path.write_text(json.dumps(env), encoding="utf-8")
    res = METRICS.read(path, opts())
    assert len(res.activity) == 1 and [q.reason for q in res.quarantined] == ["not_object"]
    env["response"] = "not json"
    path.write_text(json.dumps(env), encoding="utf-8")
    assert METRICS.read(path, opts()).quarantined[0].reason == "bad_json"
    env["response"] = 5
    path.write_text(json.dumps(env), encoding="utf-8")
    assert METRICS.read(path, opts()).quarantined[0].reason == "not_object"


def test_config_malformed_pages(tmp_path: Path) -> None:
    docs = [
        {"request": {"path": "/enterprises/a/settings/billing/budgets/b9/user-states"},
         "response": {"user_states": "none"}},
        {"request": {"path": "/enterprises/a/settings/billing/budgets/b8/user-states"},
         "response": {"user_states": [7, {"consumed_amount": "x"}, {"consumed_amount": 1}]}},
        {"budget_id": "b7", "user_states": [{"consumed_amount": 2, "target_amount": 1}]},
        {"costCenters": [7, {"resources": []}, {"name": "ok", "resources": [5, {"type": 7}]}]},
        {"unrelated": True},
    ]
    path = tmp_path / "c.jsonl"
    path.write_text("".join(json.dumps(d) + "\n" for d in docs), encoding="utf-8")
    res = CONFIG.read(path, opts())
    got = {c.entity_id: dict(c.attrs) for c in res.config}
    assert got["budget:b8"]["n_users"] == 1 and got["budget:b7"]["n_at_or_over_target"] == 1
    assert got["cc:ok"]["n_users"] == 0 and res.stats["skipped_documents"] == 1
    assert sorted(q.reason for q in res.quarantined) == [
        "bad_type:consumed_amount", "bad_type:user_states", "missing:name", "not_object",
        "not_object"]


def test_agent_task_forms(tmp_path: Path) -> None:
    task = json.loads((FIXTURES / "agent_tasks" / "user_tasks.json").read_text(
        encoding="utf-8"))["response"]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"tasks": [task, 5]}), encoding="utf-8")
    head = path.read_bytes()
    assert TASKS.sniff(path, head)
    res = TASKS.read(path, opts())
    assert len(res.aggregates) == 1 and [q.reason for q in res.quarantined] == ["not_object"]
    lst = tmp_path / "list.ndjson"
    lst.write_text(json.dumps([task]) + "\n", encoding="utf-8")
    assert len(TASKS.read(lst, opts()).aggregates) == 1
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"hello": [1]}), encoding="utf-8")
    assert TASKS.read(other, opts()).stats["skipped_documents"] == 1
    sessions_only = {"sessions": [dict(task["sessions"][0], id="z1")], "task_id": "t"}
    so = tmp_path / "so.json"
    so.write_text(json.dumps(sessions_only), encoding="utf-8")
    assert TASKS.sniff(so, so.read_bytes()) and len(TASKS.read(so, opts()).aggregates) == 1

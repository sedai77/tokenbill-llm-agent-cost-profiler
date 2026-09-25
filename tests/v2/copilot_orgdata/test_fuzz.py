"""Hypothesis fuzz of the CP-ORGDATA NDJSON and JSON parsers: only ``TokenbillError`` subclasses may
escape (SPEC §21 #5), results are deterministic and leak nothing. ``TB_ORGDATA_FUZZ_EXAMPLES``
raises the example count (default 40 per property)."""

from __future__ import annotations

import json
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.adapters.github_agent_tasks import AgentTasksAdapter
from tokenbill.adapters.github_config import CopilotConfigAdapter
from tokenbill.adapters.github_metrics import CopilotMetricsAdapter
from tokenbill.adapters.github_seats import CopilotSeatsAdapter
from tokenbill.adapters.github_usage_records import UsageRecordsRefusal
from tokenbill.copilot.teammap import build_maps
from tokenbill.core.errors import TokenbillError
from tokenbill.core.records import MAX_TOKENS
from tokenbill.core.types import IngestResult

from .helpers import blob, opts

EXAMPLES = int(os.environ.get("TB_ORGDATA_FUZZ_EXAMPLES", "40"))
FUZZ = settings(max_examples=EXAMPLES, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
ADAPTERS = (CopilotConfigAdapter(), CopilotMetricsAdapter(), CopilotSeatsAdapter(),
            AgentTasksAdapter(), UsageRecordsRefusal())

#: Keys the parsers look at, so random documents reach deep code paths.
KEYS = ("budgets", "budget_scope", "budget_type", "budget_amount", "budget_product_sku",
        "budget_product_skus", "user", "consumed_amount", "user_states", "target_amount",
        "costCenters", "resources", "name", "type", "ai_credit_pool_state", "target_amount",
        "seat_breakdown", "seat_management_setting", "plan_type", "seats", "assignee", "login",
        "organization", "assigning_team", "last_activity_at", "created_at", "day", "user_login",
        "user_id", "ai_credits_used", "totals_by_ide", "ide", "totals_by_feature", "feature",
        "totals_by_model_feature", "model", "totals_by_cli", "token_usage", "day_totals",
        "report_start_day", "report_end_day", "pull_requests", "total_merged", "organization_id",
        "enterprise_id", "repo_id", "slug", "team_id", "tasks", "sessions", "state", "usage",
        "amount", "id", "request", "response", "path", "github_request_id", "body",
        "user_initiated_interaction_count", "loc_added_sum", "daily_active_users", "expires_at")
SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(min_value=-2**60, max_value=2**60),
    st.decimals(allow_nan=False, allow_infinity=False, places=3),
    st.sampled_from(["2026-09-20", "2026-09-20T10:00:00Z", "business", "user", "vscode",
                     "intellij", "assign_all", "ai_credits", "premium_requests", "completed",
                     "/orgs/a/copilot/billing", "/agents/tasks/x", "", "User", "Team"]),
    st.text(max_size=12))
JSONISH = st.recursive(
    SCALARS,
    lambda inner: st.one_of(st.lists(inner, max_size=4),
                            st.dictionaries(st.sampled_from(KEYS), inner, max_size=6)),
    max_leaves=25)


_DEC_RE = re.compile(r'"__DEC__([^"]*)"')


def _dump(value: Any, **kw: Any) -> str:
    """JSON with ``Decimal`` values written as exact number literals."""
    text = json.dumps(value, default=lambda d: f"__DEC__{d}" if isinstance(d, Decimal) else str(d),
                      **kw)
    return _DEC_RE.sub(r"\1", text)


def _read_all(data: bytes, suffix: str) -> list[IngestResult]:
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"src{suffix}"
        path.write_bytes(data)
        for adapter in ADAPTERS:
            assert isinstance(adapter.sniff(path, data[:65536]), bool)
            try:
                result = adapter.read(path, opts())
            except TokenbillError:
                continue
            assert isinstance(result, IngestResult)
            _check(result)
            out.append(result)
    return out


def _check(result: IngestResult) -> None:
    assert "\x00" not in blob(result)
    for day in result.activity:
        assert all(0 <= v <= MAX_TOKENS for _, v in day.counts)
    for agg in result.aggregates:
        assert agg.reported_cost_nano is None or agg.reported_cost_nano >= 0


@FUZZ
@given(st.binary(max_size=2048))
def test_random_bytes(data: bytes) -> None:
    _read_all(data, ".json")


@FUZZ
@given(st.lists(JSONISH, min_size=1, max_size=5))
def test_random_ndjson(values: list[Any]) -> None:
    _read_all("".join(_dump(v) + "\n" for v in values).encode(), ".ndjson")


@FUZZ
@given(JSONISH)
def test_random_json_document(value: Any) -> None:
    _read_all(_dump(value, indent=1).encode(), ".json")


@FUZZ
@given(st.sampled_from(["/enterprises/a/settings/billing/budgets",
                        "/enterprises/a/settings/billing/budgets/b1/user-states",
                        "/enterprises/a/settings/billing/cost-centers", "/orgs/o/copilot/billing",
                        "/orgs/o/copilot/billing/seats", "/agents/repos/o/r/tasks/t",
                        "/agents/tasks", "/enterprises/a/copilot/usage-records"]),
       JSONISH)
def test_random_envelopes(path: str, body: Any) -> None:
    env = {"request": {"path": path, "query": {}}, "response": body, "fetched_ms": 5}
    _read_all(_dump(env).encode(), ".json")


NUMBER = st.one_of(st.integers(min_value=-5, max_value=2**54),
                   st.decimals(allow_nan=False, allow_infinity=False, places=2),
                   st.none(), st.booleans(), st.text(max_size=3))


@FUZZ
@given(st.dictionaries(st.sampled_from(("ai_credits_used", "user_initiated_interaction_count",
                                        "code_generation_activity_count", "loc_added_sum",
                                        "loc_deleted_sum", "distinct_mcp_use_count")),
                       NUMBER, max_size=6),
       st.lists(st.tuples(st.sampled_from(("vscode", "IntelliJ IDEA", "", "x" * 80)), NUMBER),
                max_size=3))
def test_metrics_user_record_values(values: dict[str, Any], ides: list[tuple[str, Any]]) -> None:
    rec = {"day": "2026-09-20", "user_login": "fuzz-user", "user_id": 1, **values,
           "totals_by_ide": [{"ide": i, "user_initiated_interaction_count": n} for i, n in ides]}
    results = _read_all((_dump(rec) + "\n").encode(), ".ndjson")
    metrics = [r for r in results if r.source.adapter == "github-copilot-metrics"]
    assert metrics and len(metrics[0].activity) + len(metrics[0].quarantined) == 1


@FUZZ
@given(st.lists(st.fixed_dictionaries({"user_login": st.sampled_from(["a", "b", "c", "d", "e",
                                                                      "f", "A", "x@y"]),
                                       "slug": st.sampled_from(["t1", "t2", "t3"]),
                                       "day": st.sampled_from(["2026-09-20", "2026-09-21",
                                                               "bad"])}), max_size=30),
       st.integers(min_value=1, max_value=4))
def test_build_maps_properties(rows: list[dict[str, str]], k: int) -> None:
    team_map, _ = build_maps(rows, None, k=k)
    assert build_maps(list(reversed(rows)), None, k=k)[0] == team_map
    for login, team in team_map.items():
        assert "@" not in login and login == login.lower()
        assert any(r["slug"] == team and r["user_login"].lower() == login for r in rows)

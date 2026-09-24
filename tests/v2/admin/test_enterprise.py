"""Claude Enterprise Analytics adapter (SPEC §5.11; brief ADMIN acceptance 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.adapters.anthropic_admin import EnterpriseAnalyticsAdapter, ts_ms
from tokenbill.core.builders import CANARY

from .helpers import (
    DAY_MS,
    assert_person_free,
    dims,
    fixture,
    manifest_entry,
    opts,
    read,
    tokens,
    write_json,
)

ENT = "anthropic/enterprise/"


def _entry(name: str) -> dict:
    return manifest_entry(f"{ENT}{name}_report.json")["expect"]


def test_usage_report_aggregates() -> None:
    result = read("anthropic-enterprise-analytics", fixture(f"{ENT}usage_report.json"))
    expect = _entry("usage")
    assert len(result.aggregates) == expect["aggregates"]
    assert tokens(result) == expect["usage_tokens"]
    for agg in result.aggregates:
        assert agg.source_kind == "anthropic.enterprise_usage"
        d = dims(agg)
        assert set(d) == {"channel", "model", "product", "speed", "inference_geo",
                          "context_window"}
        assert d["channel"] == "anthropic_api"
        assert agg.fetched_ms == ts_ms("2026-09-22T12:00:00Z", "t")  # data_refreshed_at
    finality = {agg.bucket_start_ms: agg.finality for agg in result.aggregates}
    assert finality[ts_ms("2026-08-01", "d")] == "final"
    assert finality[ts_ms("2026-09-01", "d")] == "provisional"
    assert result.capabilities == frozenset({"aggregates"})


def test_cost_report_amount_and_list_amount_exact() -> None:
    result = read("anthropic-enterprise-analytics", fixture(f"{ENT}cost_report.json"))
    expect = _entry("cost")
    assert len(result.cost_lines) == expect["cost_lines"]
    assert sum(c.amount_nano for c in result.cost_lines) == expect["amount_nano"]
    assert sum(c.list_amount_nano or 0 for c in result.cost_lines) == expect["list_amount_nano"]
    for line in result.cost_lines:
        assert line.source_kind == "anthropic.enterprise_cost"
        assert line.channel == "anthropic_api"
        assert line.list_amount_nano is not None and line.amount_nano <= line.list_amount_nano
        want = "final" if line.date_utc == "2026-08-01" else "provisional"
        assert line.finality == want
    code = [c for c in result.cost_lines if c.cost_type == "code_execution"]
    assert {c.amount_nano for c in code} == {412_800_000_000}  # "41280.000000" cents = $412.80
    assert result.stats["rounding_remainder_e18"] == 0


@pytest.mark.parametrize("now, expected", [("2026-09-02", "provisional"),
                                           ("2026-10-01", "provisional"),
                                           ("2026-10-02", "final")])
def test_dates_within_30_days_are_provisional(now: str, expected: str) -> None:
    result = EnterpriseAnalyticsAdapter().read(fixture(f"{ENT}cost_report.json"),
                                               opts(now_ms=ts_ms(now, "now")))
    sept = {c.finality for c in result.cost_lines if c.date_utc == "2026-09-01"}
    assert sept == {expected}


def test_user_usage_report_rolled_to_teams() -> None:
    result = read("anthropic-enterprise-analytics", fixture(f"{ENT}user_usage_report.json"))
    expect = _entry("user_usage")
    assert len(result.aggregates) == expect["aggregates"]
    (agg,) = result.aggregates
    assert agg.source_kind == "anthropic.enterprise_team_usage"
    assert dims(agg)["team"] == "platform"
    assert tokens(result) == expect["usage_tokens"]
    (note,) = [n for n in result.notes if n.code == "dq.outcomes_suppressed"]
    assert note.count == expect["dropped_groups"]
    assert result.outcomes == []
    assert result.capabilities == frozenset({"aggregates", "attribution.team"})
    page = json.loads(fixture(f"{ENT}user_usage_report.json").read_text())
    raw = [r["actor"]["user_id"] for r in page["data"]] + [r["actor"]["name"]
                                                          for r in page["data"]]
    assert_person_free(result, *raw)


def test_user_cost_report_rolled_to_teams() -> None:
    result = read("anthropic-enterprise-analytics", fixture(f"{ENT}user_cost_report.json"))
    expect = _entry("user_cost")
    assert len(result.aggregates) == expect["aggregates"]
    assert {dims(a)["team"] for a in result.aggregates} == {"platform", "payments"}
    assert sum(a.reported_cost_nano for a in result.aggregates) == expect["reported_cost_nano"]
    for agg in result.aggregates:
        assert agg.source_kind == "anthropic.enterprise_team_cost"
        assert agg.reported_cost_basis == "invoice"
        assert agg.list_cost_nano is not None and agg.list_cost_nano >= agg.reported_cost_nano
        assert agg.usage.total_input == agg.usage.output == 0
        assert dims(agg)["token_type"] == "output_tokens"
        assert agg.bucket_end_ms - agg.bucket_start_ms == DAY_MS
    assert_person_free(result, "user_0000Example", "Person 0")


def test_whole_directory_of_endpoints(tmp_path: Path) -> None:
    result = read("anthropic-enterprise-analytics", fixture("anthropic/enterprise"))
    kinds = {a.source_kind for a in result.aggregates}
    assert kinds == {"anthropic.enterprise_usage", "anthropic.enterprise_team_usage",
                     "anthropic.enterprise_team_cost"}
    assert len(result.cost_lines) == _entry("cost")["cost_lines"]
    assert result.stats["files"] == 4 and result.stats["pages"] == 4
    assert result.capabilities == frozenset({"aggregates", "cost", "attribution.team"})
    assert CANARY not in repr(result)


def test_user_records_need_a_bucket(tmp_path: Path) -> None:
    rec = {"actor": {"type": "user_actor", "user_id": "user_1", "email": "a@x.io"},
           "uncached_input_tokens": 5, "output_tokens": 1, "starting_at": None}
    cost = {"actor": {"type": "user_actor", "user_id": "user_1"}, "amount": None,
            "starting_at": "2026-09-01T00:00:00Z"}
    bad_cur = {"actor": {"type": "user_actor", "user_id": "user_2"}, "amount": "1",
               "currency": "JPY", "starting_at": "2026-09-01T00:00:00Z"}
    page = {"data": [rec], "data_refreshed_at": "not a date", "organization_id": "org"}
    result = read("anthropic-enterprise-analytics", write_json(tmp_path / "u.json", page))
    assert [q.reason for q in result.quarantined] == ["missing:starting_at"]
    page = {"data": [cost, bad_cur], "organization_id": "org"}
    result = read("anthropic-enterprise-analytics", write_json(tmp_path / "c.json", page))
    assert [q.reason for q in result.quarantined] == ["missing:amount", "bad_type:currency"]


def test_wrapper_endpoint_hint_classifies_empty_pages(tmp_path: Path) -> None:
    wrapped = {"endpoint": "/v1/organizations/analytics/user_cost_report",
               "fetched_at": "2026-09-22T00:00:00Z",
               "response": {"data": [], "has_more": False, "next_page": None}}
    path = write_json(tmp_path / "w.json", wrapped)
    adapter = EnterpriseAnalyticsAdapter()
    assert adapter.sniff(path, path.read_bytes())
    result = adapter.read(path, opts())
    assert result.stats["pages"] == 1 and result.aggregates == []

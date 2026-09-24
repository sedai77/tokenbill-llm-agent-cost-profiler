"""Regression tests for defects found in the adversarial review of package ADMIN (each test names
the defect it pins): recorded-page wrappers with JSON-text bodies, OpenAI disjoint usage fields,
case-variant actor identities (k-anonymity), out-of-range counts and timestamps (only quarantine,
never an aborted read or a non-``TokenbillError``), export directories with metadata files, the
``attribution.team`` capability and the GCP label allowlist."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from tokenbill.adapters.anthropic_admin import MAX_TS_MS, BadRecord, date_of, ts_ms
from tokenbill.core.errors import SourceError

from .helpers import dims, fixture, read, write_json, write_jsonl

USAGE = "anthropic/usage_report_2026-08.json"
DAY_S = 1_788_220_800  # 2026-09-01T00:00Z


def _cc(email: str, day: str = "2026-09-10T00:00:00Z", **metrics: int) -> dict:
    return {"date": day, "actor": {"type": "user_actor", "email_address": email},
            "core_metrics": {"num_sessions": 1, "commits_by_claude_code": 1, **metrics},
            "model_breakdown": [{"model": "claude-opus-5",
                                 "tokens": {"input": 10, "output": 1}}]}


# --- recorded-page wrapper whose body is the HTTP body as JSON text ---------------------------


def test_wrapper_with_json_text_body(tmp_path: Path) -> None:
    page = json.loads(fixture(USAGE).read_text())
    direct = read("anthropic-usage-report", fixture(USAGE))
    wrapped = {"url": "https://api.anthropic.com/v1/organizations/usage_report/messages",
               "status": 200, "fetched_at": "2026-09-22T10:00:00Z", "body": json.dumps(page)}
    res = read("anthropic-usage-report", write_json(tmp_path / "w.json", wrapped))
    assert res.quarantined == []
    assert [a.usage for a in res.aggregates] == [a.usage for a in direct.aggregates]
    assert {a.fetched_ms for a in res.aggregates} == {ts_ms("2026-09-22T10:00:00Z", "t")}
    # an unusable body is not a page (formerly the wrapper itself was parsed as a bucket)
    broken = dict(wrapped, body="{not json")
    res = read("anthropic-usage-report", write_json(tmp_path / "b.json", broken))
    assert [q.reason for q in res.quarantined] == ["bad_type:page"]


# --- OpenAI usage buckets: the disjoint fields map directly (SPEC §5.2) ------------------------


def _oai_page(result: dict) -> dict:
    return {"object": "page", "data": [{"object": "bucket", "start_time": DAY_S,
                                        "end_time": DAY_S + 86_400, "results": [result]}]}


def test_openai_disjoint_fields_without_input_tokens(tmp_path: Path) -> None:
    row = {"object": "organization.usage.completions.result", "input_uncached_tokens": 300,
           "input_cached_tokens": 600, "input_cache_write_tokens": 100, "output_tokens": 50,
           "project_id": "p", "model": "gpt-5.6-sol"}
    res = read("openai-usage-buckets", write_json(tmp_path / "u.json", _oai_page(row)))
    (agg,) = res.aggregates
    u = agg.usage
    assert (u.uncached_input, u.cache_read, u.cache_write_other, u.output) == (300, 600, 100, 50)
    assert res.quarantined == [] and res.notes == []
    neither = {k: v for k, v in row.items() if k != "input_uncached_tokens"}
    res = read("openai-usage-buckets", write_json(tmp_path / "n.json", _oai_page(neither)))
    assert [q.reason for q in res.quarantined] == ["missing:input_tokens"]
    bad_total = dict(row, input_tokens=-1)
    res = read("openai-usage-buckets", write_json(tmp_path / "t.json", _oai_page(bad_total)))
    assert [q.reason for q in res.quarantined] == ["bad_usage"]


# --- k-anonymity: one person under two spellings is one user --------------------------------


def test_case_variant_actor_counts_once(tmp_path: Path) -> None:
    recs = [_cc(f"u{i}@x.io") for i in range(4)] + [_cc("U0@X.IO")]
    path = write_json(tmp_path / "cc.json", {"data": recs})
    # four people: never published as a group of five
    res = read("anthropic-cc-analytics", path, team_map=(), k_anonymity=5)
    assert res.outcomes == [] and res.aggregates == []
    assert res.stats["users_dropped"] == 4
    res = read("anthropic-cc-analytics", path, team_map=(), k_anonymity=4)
    (out,) = res.outcomes
    assert (out.n_users, out.commits) == (4, 5)
    ent = [{"actor": {"type": "user_actor", "user_id": uid, "email": f"{uid}@x.io"},
            "starting_at": "2026-09-01T00:00:00Z", "uncached_input_tokens": 1,
            "output_tokens": 1} for uid in ("user_A1", "user_B2", "user_C3", "user_D4",
                                            "USER_a1")]
    res = read("anthropic-enterprise-analytics",
               write_json(tmp_path / "e.json", {"data": ent, "organization_id": "o"}),
               team_map=(), k_anonymity=5)
    assert res.aggregates == [] and res.stats["users_dropped"] == 4


# --- out-of-range counts: quarantined, never an aborted read ----------------------------------


def test_outcome_count_overflow_is_quarantined(tmp_path: Path) -> None:
    big = 2**53 - 5
    recs = [_cc(f"u{i}@x.io") for i in range(5)] + [_cc("u0@x.io", num_sessions=big),
                                                    _cc("u1@x.io", num_sessions=big)]
    path = write_json(tmp_path / "cc.json", {"data": recs})
    res = read("anthropic-cc-analytics", path, team_map=(), k_anonymity=5)
    assert [q.reason for q in res.quarantined] == ["bad_usage"]   # the second big record
    (out,) = res.outcomes
    assert out.n_users == 5 and out.sessions == 5 + big == 2**53
    with pytest.raises(SourceError, match="bad_usage"):
        read("anthropic-cc-analytics", path, team_map=(), k_anonymity=5, lenient=False)


def test_other_group_overflow_suppresses_instead_of_aborting(tmp_path: Path) -> None:
    big = 2**53 - 5
    team_map = tuple((f"u{i}@x.io", f"t{i}") for i in range(6))
    recs = [{**_cc(f"u{i}@x.io"), "model_breakdown": [
        {"model": "claude-opus-5", "tokens": {"input": big}}]} for i in range(6)]
    recs += [_cc(f"w{i}@x.io") for i in range(5)]                      # an unmapped team of 5
    res = read("anthropic-cc-analytics", write_json(tmp_path / "cc.json", {"data": recs}),
               team_map=team_map, k_anonymity=5)
    assert [(q.locator, q.reason) for q in res.quarantined] == [("rollup:2026-09-10",
                                                                 "bad_usage")]
    assert [(o.team, o.n_users) for o in res.outcomes] == [("(unmapped)", 5)]
    (note,) = [n for n in res.notes if n.code == "dq.outcomes_suppressed"]
    assert note.count == 6 and res.stats["users_dropped"] == 6
    recs = [{**_cc(f"u{i}@x.io"), "core_metrics": {"num_sessions": big}} for i in range(6)]
    res = read("anthropic-cc-analytics", write_json(tmp_path / "cc2.json", {"data": recs}),
               team_map=team_map, k_anonymity=5)
    assert res.outcomes == [] and [q.reason for q in res.quarantined] == ["bad_usage"]


# --- timestamps past year 9999: BadRecord, never ValueError ----------------------------------


def test_far_future_timestamps_are_quarantined(tmp_path: Path) -> None:
    with pytest.raises(BadRecord):
        date_of(MAX_TS_MS)
    assert date_of(MAX_TS_MS - 1) == "9999-12-31"
    with pytest.raises(BadRecord):
        ts_ms("9999-12-31T23:30:00-01:00", "t")                     # year 10000 in UTC
    far = 2**53 // 1000 - 100
    row = {"object": "organization.costs.result", "line_item": "x", "project_id": "p",
           "amount": {"value": "0.5", "currency": "usd"}}
    page = {"object": "page", "data": [{"object": "bucket", "start_time": far,
                                        "end_time": far + 1, "results": [row]}]}
    res = read("openai-costs", write_json(tmp_path / "c.json", page))
    assert res.cost_lines == [] and [q.reason for q in res.quarantined] == [
        "bad_type:start_time"]
    res = read("anthropic-cc-analytics", write_json(tmp_path / "cc.json", {"data": [
        _cc("a@x.io", day="9999-12-31T23:30:00-01:00")]}))
    assert [q.reason for q in res.quarantined] == ["bad_type:date"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["line_item_usage_start_date", "line_item_usage_account_id",
                "line_item_product_code", "line_item_usage_type", "line_item_unblended_cost"])
    w.writerow(["9999-12-31T23:30:00-01:00", "111122223333", "AmazonBedrock", "USE1-x", "1"])
    (tmp_path / "cur.csv").write_text(buf.getvalue())
    res = read("aws-cur", tmp_path / "cur.csv")
    assert [q.reason for q in res.quarantined] == ["bad_type:line_item_usage_start_date"]


# --- directories: only data files are read ---------------------------------------------------


def test_cur_export_directory_with_metadata(tmp_path: Path) -> None:
    root = tmp_path / "export"
    data = root / "data" / "BILLING_PERIOD=2026-09"
    meta = root / "metadata" / "BILLING_PERIOD=2026-09"
    data.mkdir(parents=True)
    meta.mkdir(parents=True)
    (data / "cur2-00001.csv.gz").write_bytes(fixture("cloud/cur2_bedrock_2026-09.csv.gz")
                                             .read_bytes())
    (meta / "cur2-Manifest.json").write_text(json.dumps(
        {"reportName": "cur2", "columns": [{"name": "line_item_usage_type"},
                                           {"name": "line_item_product_code"}]}, indent=2))
    (root / "README.txt").write_text("export notes\n")
    single = read("aws-cur", fixture("cloud/cur2_bedrock_2026-09.csv.gz"))
    res = read("aws-cur", root, lenient=False)
    assert res.stats["files"] == 1 and res.quarantined == []
    assert [c.amount_nano for c in res.cost_lines] == [c.amount_nano for c in single.cost_lines]


def test_page_and_gcp_directories_skip_non_data_files(tmp_path: Path) -> None:
    d = tmp_path / "pull"
    write_json(d / "usage.json", json.loads(fixture(USAGE).read_text()))
    (d / "notes.txt").write_text("not a page\n")
    res = read("anthropic-usage-report", d, lenient=False)
    assert res.stats["files"] == 1 and len(res.aggregates) == 8
    g = tmp_path / "gcp"
    g.mkdir()
    (g / "rows.jsonl").write_bytes(fixture("cloud/gcp_billing_2026-09.jsonl").read_bytes())
    (g / "schema.txt").write_text("sku.id STRING\n")
    res = read("gcp-billing", g, lenient=False)
    assert res.stats["files"] == 1 and res.cost_lines


# --- attribution.team only when some team is actually mapped --------------------------------


def test_attribution_team_needs_a_mapped_team(tmp_path: Path) -> None:
    path = write_json(tmp_path / "cc.json", {"data": [_cc(f"u{i}@x.io") for i in range(5)]})
    res = read("anthropic-cc-analytics", path, team_map=())
    assert {o.team for o in res.outcomes} == {"(unmapped)"}
    assert res.capabilities == frozenset({"aggregates", "outcomes"})
    res = read("anthropic-cc-analytics", path,
               team_map=tuple((f"u{i}@x.io", "core") for i in range(5)))
    assert "attribution.team" in res.capabilities


# --- GCP labels: only attribution dimensions --------------------------------------------------


def test_gcp_labels_limited_to_attribution_dims(tmp_path: Path) -> None:
    row = {"sku": {"id": "S-1", "description": "Claude Opus 5 Input Tokens"},
           "usage_start_time": "2026-09-01T00:00:00Z", "project": {"id": "p"},
           "cost": "1", "usage": {"amount": 10, "unit": "tokens"},
           "location": {"region": "global"},
           "labels": [{"key": "team", "value": "search"}, {"key": "cost-center", "value": "cc7"},
                      {"key": "department", "value": "eng"},
                      {"key": "environment", "value": "prod"}]}
    res = read("gcp-billing", write_jsonl(tmp_path / "g.jsonl", [row]))
    (agg,) = res.aggregates
    d = dims(agg)
    assert d["team"] == "search" and d["cost_center"] == "cc7"
    assert "department" not in d and "environment" not in d
    assert [n.count for n in res.notes if n.code == "dq.unknown_fields"] == [2]

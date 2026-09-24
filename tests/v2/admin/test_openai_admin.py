"""OpenAI organization usage buckets and costs adapters (SPEC §5.11; brief ADMIN acceptance 4)."""

from __future__ import annotations

import json
from pathlib import Path

from tokenbill.adapters.anthropic_admin import ts_ms
from tokenbill.adapters.openai_admin import OPENAI_WRITE_TTL_S
from tokenbill.core.builders import make_ctx
from tokenbill.core.testing import FakePricer

from .helpers import dims, fixture, h, manifest_entry, read, tokens, write_json

USAGE = "openai/usage_completions_2026-09.json"
COSTS = "openai/costs_2026-09.json"
DAY_S = 1_788_220_800  # 2026-09-01T00:00Z


def _page(results: list[dict], obj: str = "bucket") -> dict:
    return {"object": "page", "has_more": False, "next_page": None,
            "data": [{"object": obj, "start_time": DAY_S, "end_time": DAY_S + 86_400,
                      "results": results}]}


def _usage(**kw: object) -> dict:
    row = {"object": "organization.usage.completions.result", "input_tokens": 1000,
           "input_cached_tokens": 600, "input_cache_write_tokens": 100,
           "input_uncached_tokens": 300, "output_tokens": 50, "num_model_requests": 2,
           "project_id": "proj_a", "api_key_id": "key_a", "user_id": None,
           "model": "gpt-5.6-sol", "batch": None, "service_tier": None}
    row.update(kw)
    return row


def test_usage_fixture() -> None:
    result = read("openai-usage-buckets", fixture(USAGE))
    expect = manifest_entry(USAGE)["expect"]
    assert len(result.aggregates) == expect["aggregates"]
    assert tokens(result) == expect["usage_tokens"]
    assert result.stats["results_skipped_other_kinds"] == 2   # embeddings
    assert result.stats["person_dims_dropped"] == 4          # user_id rows summed
    for agg in result.aggregates:
        assert agg.source_kind == "openai.usage"
        assert dims(agg)["channel"] == "openai_api"
        assert dims(agg)["model"] == "gpt-5.6-sol"
    tiers = {dims(a).get("workspace_id"): dims(a).get("service_tier") for a in result.aggregates}
    assert tiers[h("proj_03Lab")] == "standard"                 # "default" → standard
    assert tiers[h("proj_01Search")] is None and tiers[h("proj_02Support")] is None
    lab = [a for a in result.aggregates if dims(a).get("workspace_id") == h("proj_03Lab")]
    assert all(a.usage.uncached_input == 2000 and "api_key_id" not in dims(a) for a in lab)
    assert "user_01Ann" not in json.dumps([a.dims for a in result.aggregates])


def test_disjoint_buckets_and_write_ttl(tmp_path: Path) -> None:
    result = read("openai-usage-buckets", write_json(tmp_path / "u.json", _page([_usage()])))
    (agg,) = result.aggregates
    u = agg.usage
    assert (u.uncached_input, u.cache_read, u.cache_write_other, u.output) == (300, 600, 100, 50)
    assert u.cache_write_other_ttl_s == OPENAI_WRITE_TTL_S == 1800
    assert u.total_input == 1000
    assert dims(agg) == {"api_key_id": h("key_a"), "channel": "openai_api",
                         "model": "gpt-5.6-sol", "workspace_id": h("proj_a")}
    assert agg.bucket_start_ms == DAY_S * 1000
    assert result.notes == []
    # FakePricer prices the promotional gpt-5.6-sol row on these buckets
    priced = FakePricer().price_usage(u, make_ctx("gpt-5.6-sol"), ts_ms=agg.bucket_start_ms)
    assert priced.unpriced_reason is None and priced.exact_nano > 0


def test_uncached_derived_when_absent_and_sum_checks(tmp_path: Path) -> None:
    rows = [_usage(input_uncached_tokens=None),
            _usage(input_uncached_tokens=None, input_tokens=500, project_id="p2"),
            _usage(input_uncached_tokens=10, project_id="p3"),
            _usage(input_cache_write_tokens=0, input_uncached_tokens=400, project_id="p4")]
    result = read("openai-usage-buckets", write_json(tmp_path / "u.json", _page(rows)))
    by_ws = {dims(a)["workspace_id"]: a.usage for a in result.aggregates}
    assert by_ws[h("proj_a")].uncached_input == 300          # 1000 − 600 − 100
    assert h("p2") not in by_ws                              # 600 + 100 > 500: quarantined
    assert by_ws[h("p3")].uncached_input == 10               # disjoint kept, flagged
    assert by_ws[h("p4")].cache_write_other == 0
    assert by_ws[h("p4")].cache_write_other_ttl_s is None
    notes = {(n.detail[:40], n.count) for n in result.notes if n.code == "dq.sum_check_failed"}
    assert len(notes) == 2
    assert [q.reason for q in result.quarantined] == ["bad_usage"]


def test_service_tier_mapping(tmp_path: Path) -> None:
    rows = [_usage(service_tier="default", project_id="a"),
            _usage(service_tier="flex", project_id="b"),
            _usage(batch=True, service_tier="default", project_id="c"),
            _usage(batch="yes", project_id="d")]
    result = read("openai-usage-buckets", write_json(tmp_path / "u.json", _page(rows)))
    tiers = {dims(a)["workspace_id"]: dims(a)["service_tier"] for a in result.aggregates}
    assert tiers == {h("a"): "standard", h("b"): "flex", h("c"): "batch"}
    assert [q.reason for q in result.quarantined] == ["bad_type:batch"]


def test_bucket_times_are_unix_seconds(tmp_path: Path) -> None:
    page = _page([_usage()])
    page["data"].append({"object": "bucket", "start_time": "2026-09-01", "end_time": 1,
                         "results": []})
    page["data"].append({"object": "bucket", "start_time": -5, "end_time": 1, "results": []})
    page["data"].append({"object": "bucket", "end_time": 1, "results": []})
    result = read("openai-usage-buckets", write_json(tmp_path / "u.json", page))
    assert len(result.aggregates) == 1
    assert [q.reason for q in result.quarantined] == ["bad_type:start_time",
                                                      "bad_type:start_time",
                                                      "missing:start_time"]


def test_costs_fixture_exact() -> None:
    result = read("openai-costs", fixture(COSTS))
    expect = manifest_entry(COSTS)["expect"]
    assert len(result.cost_lines) == expect["cost_lines"]
    assert sum(c.amount_nano for c in result.cost_lines) == expect["amount_nano"]
    for line in result.cost_lines:
        assert line.channel == "openai_api" and line.source_kind == "openai.costs"
        assert line.description.startswith("gpt-5.6-sol, ")
        assert line.model is None and line.token_type is None  # never guessed from line items
        assert line.workspace_id in (h("proj_01Search"), h("proj_02Support"), h("proj_03Lab"))
    assert result.capabilities == frozenset({"cost"})


def test_fixture_costs_equal_usage_at_list() -> None:
    """The costs page is the usage page priced at the facts.json list rates (FakePricer)."""
    usage = read("openai-usage-buckets", fixture(USAGE))
    costs = read("openai-costs", fixture(COSTS))
    pricer = FakePricer()
    priced: dict[tuple, int] = {}
    for agg in usage.aggregates:
        assert agg.usage.total_input < 272_000          # below the long-context band
        result = pricer.price_usage(agg.usage, make_ctx("gpt-5.6-sol"),
                                    ts_ms=agg.bucket_start_ms)
        assert result.unpriced_reason is None and result.estimated is None
        key = (agg.bucket_start_ms, dims(agg)["workspace_id"])
        priced[key] = priced.get(key, 0) + result.exact_nano
    invoiced: dict[tuple, int] = {}
    for line in costs.cost_lines:
        key = (ts_ms(line.date_utc, "d"), line.workspace_id)
        invoiced[key] = invoiced.get(key, 0) + line.amount_nano
    assert invoiced == priced


def test_costs_json_numbers_exact_and_currency(tmp_path: Path) -> None:
    good = {"object": "organization.costs.result", "amount": {"value": "X", "currency": "usd"},
            "line_item": "gpt-5.6-sol, input", "project_id": "p"}
    other = {**good, "line_item": "gpt-5.6-sol, output", "amount": {"value": 1,
                                                                    "currency": "eur"}}
    missing = {**good, "line_item": "x", "amount": None}
    path = tmp_path / "c.json"
    text = json.dumps(_page([good, other, missing]))
    path.write_text(text.replace('"X"', "0.123456789012345678901"))
    result = read("openai-costs", path)
    (line,) = result.cost_lines
    assert line.amount_nano == 123_456_789
    assert result.stats["rounding_remainder_e18"] == 12_345_679  # 0.012345678901 nano
    assert [q.reason for q in result.quarantined] == ["bad_type:currency", "missing:amount"]


def test_costs_rows_differing_by_api_key_are_summed(tmp_path: Path) -> None:
    row = {"object": "organization.costs.result", "amount": {"value": "0.5", "currency": "usd"},
           "line_item": "gpt-5.6-sol, input", "project_id": "p", "api_key_id": "k1"}
    rows = [row, {**row, "api_key_id": "k2"}]
    (line,) = read("openai-costs", write_json(tmp_path / "c.json", _page(rows))).cost_lines
    assert line.amount_nano == 1_000_000_000

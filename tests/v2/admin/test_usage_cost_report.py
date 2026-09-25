"""Anthropic usage report and cost report adapters (SPEC §5.11; brief ADMIN acceptance 1)."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.adapters.anthropic_admin import (
    CHANNEL,
    CostReportAdapter,
    UsageReportAdapter,
    date_of,
)
from tokenbill.core.builders import make_ctx
from tokenbill.core.errors import SourceError
from tokenbill.core.ids import key_id
from tokenbill.core.money import cents_to_nano
from tokenbill.core.testing import FakePricer

from .helpers import (
    DAY_MS,
    MANIFEST,
    NAME_KEY,
    NOW_MS,
    dims,
    fixture,
    h,
    manifest_entry,
    opts,
    read,
    tokens,
    write_json,
)

USAGE = "anthropic/usage_report_2026-08.json"
COST = "anthropic/cost_report_2026-08.json"


def _bucket(results: list[dict], day: str = "2026-08-10") -> dict:
    nxt = f"2026-08-{int(day[-2:]) + 1:02d}"
    return {"data": [{"starting_at": f"{day}T00:00:00Z", "ending_at": f"{nxt}T00:00:00Z",
                      "results": results}], "has_more": False, "next_page": None}


def _usage_row(**kw: object) -> dict:
    row = {"api_key_id": "apikey_x", "workspace_id": "wrkspc_x", "model": "claude-opus-5",
           "uncached_input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 1000,
           "cache_creation": {"ephemeral_5m_input_tokens": 50, "ephemeral_1h_input_tokens": 7},
           "server_tool_use": {"web_search_requests": 3}, "service_tier": "standard",
           "context_window": "0-200k", "inference_geo": "global", "account_id": None,
           "service_account_id": None}
    row.update(kw)
    return row


def _cost_row(amount: object, **kw: object) -> dict:
    row = {"amount": amount, "currency": "USD", "cost_type": "tokens", "model": "claude-opus-5",
           "token_type": "output_tokens", "description": "Claude Opus 5 Usage - Output Tokens",
           "workspace_id": "wrkspc_x", "service_tier": "standard", "inference_geo": "global",
           "context_window": "0-200k"}
    row.update(kw)
    return row


# ---------------------------------------------------------------------------------------------
# usage report
# ---------------------------------------------------------------------------------------------


def test_usage_report_fixture_buckets_and_dims() -> None:
    result = read("anthropic-usage-report", fixture(USAGE))
    expect = manifest_entry(USAGE)["expect"]
    assert len(result.aggregates) == expect["aggregates"]
    assert tokens(result) == expect["usage_tokens"]
    assert result.capabilities == frozenset({"aggregates"})
    assert result.cost_lines == [] and result.outcomes == [] and result.requests == []
    page = json.loads(fixture(USAGE).read_text())
    by_key = {(date_of(a.bucket_start_ms), dims(a)["api_key_id"]): a for a in result.aggregates}
    assert len(by_key) == len(result.aggregates)
    for bucket in page["data"]:
        for row in bucket["results"]:
            agg = by_key[(bucket["starting_at"][:10], h(row["api_key_id"]))]
            assert agg.bucket_end_ms - agg.bucket_start_ms == DAY_MS
            u = agg.usage
            assert u.uncached_input == row["uncached_input_tokens"]
            assert u.cache_read == row["cache_read_input_tokens"]
            assert u.cache_write_5m == row["cache_creation"]["ephemeral_5m_input_tokens"]
            assert u.cache_write_1h == row["cache_creation"]["ephemeral_1h_input_tokens"]
            assert u.output == row["output_tokens"]
            assert u.web_search_requests == row["server_tool_use"]["web_search_requests"]
            d = dims(agg)
            assert d["channel"] == CHANNEL
            assert d["model"] == row["model"]
            assert d["service_tier"] == "standard" and d["context_window"] == "0-200k"
            if row["workspace_id"] is None:
                assert "workspace_id" not in d  # default workspace
            else:
                assert d["workspace_id"] == h(row["workspace_id"])
            if row["inference_geo"] == "not_available":
                assert "inference_geo" not in d
            else:
                assert d["inference_geo"] == row["inference_geo"]
    for agg in result.aggregates:
        assert agg.source_kind == "anthropic.usage_report"
        assert agg.finality == "final"
        assert agg.dims == tuple(sorted(agg.dims))
    assert result.source.name_key_id == key_id(NAME_KEY)
    assert result.source.principal_key_id is None


def test_usage_report_hand_built_page(tmp_path: Path) -> None:
    path = write_json(tmp_path / "u.json", _bucket([_usage_row(speed="fast")]))
    result = read("anthropic-usage-report", path)
    (agg,) = result.aggregates
    u = agg.usage
    assert (u.uncached_input, u.cache_read, u.cache_write_5m, u.cache_write_1h, u.output,
            u.web_search_requests) == (100, 1000, 50, 7, 10, 3)
    assert dims(agg) == {"api_key_id": h("apikey_x"), "channel": "anthropic_api",
                         "context_window": "0-200k", "inference_geo": "global",
                         "model": "claude-opus-5", "service_tier": "standard", "speed": "fast",
                         "workspace_id": h("wrkspc_x")}
    assert agg.bucket_end_ms - agg.bucket_start_ms == DAY_MS
    assert agg.agg_id.startswith("ag_")


def test_person_level_grouping_is_dropped_and_summed(tmp_path: Path) -> None:
    rows = [_usage_row(account_id="user_01Alice", uncached_input_tokens=100),
            _usage_row(account_id="user_02Bob", service_account_id="svac_01",
                       uncached_input_tokens=250)]
    path = write_json(tmp_path / "u.json", _bucket(rows))
    result = read("anthropic-usage-report", path)
    (agg,) = result.aggregates
    assert agg.usage.uncached_input == 350
    assert result.stats["person_dims_dropped"] == 3
    blob = json.dumps(result.aggregates[0].dims)
    assert "user_0" not in blob and "svac" not in blob


def test_name_allowlist_keeps_workspace_in_clear(tmp_path: Path) -> None:
    path = write_json(tmp_path / "u.json", _bucket([_usage_row()]))
    result = read("anthropic-usage-report", path, name_allowlist=frozenset({"wrkspc_x"}))
    d = dims(result.aggregates[0])
    assert d["workspace_id"] == "wrkspc_x"
    assert d["api_key_id"] == h("apikey_x")  # not allowlisted


def test_unsplit_cache_creation_goes_to_unknown(tmp_path: Path) -> None:
    row = _usage_row(cache_creation=None, cache_creation_input_tokens=90)
    path = write_json(tmp_path / "u.json", _bucket([row]))
    (agg,) = read("anthropic-usage-report", path).aggregates
    assert (agg.usage.cache_write_5m, agg.usage.cache_write_1h,
            agg.usage.cache_write_unknown) == (0, 0, 90)
    bad = _usage_row(cache_creation_input_tokens=10)  # less than the 5m + 1h split
    result = read("anthropic-usage-report", write_json(tmp_path / "b.json", _bucket([bad])))
    assert [q.reason for q in result.quarantined] == ["bad_usage"]


@pytest.mark.parametrize("now_days, finality", [(0, "provisional"), (29, "provisional"),
                                                (31, "final")])
def test_revision_window_relative_to_injected_clock(tmp_path: Path, now_days: int,
                                                    finality: str) -> None:
    path = write_json(tmp_path / "u.json", _bucket([_usage_row()]))
    end = 20_676 * DAY_MS  # 2026-08-11T00:00Z = end of the 2026-08-10 bucket
    result = read("anthropic-usage-report", path, now_ms=end + now_days * DAY_MS)
    assert result.aggregates[0].finality == finality


def test_usage_report_pages_jsonl_is_provisional() -> None:
    rel = "anthropic/usage_report_2026-09_pages.jsonl"
    result = read("anthropic-usage-report", fixture(rel))
    assert result.stats["pages"] == 2
    assert len(result.aggregates) == manifest_entry(rel)["expect"]["aggregates"]
    assert {a.finality for a in result.aggregates} == {"provisional"}


def test_usage_report_edge_fixture() -> None:
    rel = "anthropic/edge/usage_report_edge.json"
    result = read("anthropic-usage-report", fixture(rel))
    tiers = {dims(a).get("service_tier") for a in result.aggregates}
    assert "priority" in tiers
    fast = [a for a in result.aggregates if dims(a).get("speed") == "fast"]
    assert fast and fast[0].usage.web_fetch_requests == 2
    assert dims(fast[0])["context_window"] == "200k-1M"
    us = [a for a in result.aggregates if dims(a).get("inference_geo") == "us"]
    assert len(us) == 1 and us[0].usage.uncached_input == 600_000  # two accounts summed


def test_malformed_usage_rows_are_quarantined(tmp_path: Path) -> None:
    rows = [_usage_row(), _usage_row(output_tokens=-1), _usage_row(uncached_input_tokens=1.5),
            _usage_row(output_tokens=None), "not a row", _usage_row(model="x" * 80),
            _usage_row(server_tool_use=[1])]
    page = _bucket(rows)
    page["data"].append({"starting_at": "yesterday", "ending_at": "2026-08-12T00:00:00Z",
                         "results": []})
    page["data"].append({"starting_at": "2026-08-12T00:00:00Z",
                         "ending_at": "2026-08-11T00:00:00Z", "results": []})
    page["data"].append({"starting_at": "2026-08-12T00:00:00Z",
                         "ending_at": "2026-08-13T00:00:00Z"})
    page["data"].append(7)
    path = write_json(tmp_path / "u.json", page)
    result = read("anthropic-usage-report", path)
    assert len(result.aggregates) == 1
    reasons = [q.reason for q in result.quarantined]
    assert reasons == ["bad_usage", "bad_usage", "missing:output_tokens", "not_object",
                       "bad_type:model", "bad_type:server_tool_use", "bad_type:starting_at",
                       "bad_type:ending_at", "missing:results", "not_object"]
    locators = [q.locator for q in result.quarantined]
    assert locators[0] == "doc/bucket:0/result:1"
    assert all(q.source_id == result.source.source_id for q in result.quarantined)
    (note,) = [n for n in result.notes if n.code == "dq.quarantined"]
    assert note.count == len(reasons) and note.severity == "warn"
    with pytest.raises(SourceError, match=r"u\.json: doc/bucket:0/result:1: bad_usage"):
        read("anthropic-usage-report", path, lenient=False)


# ---------------------------------------------------------------------------------------------
# cost report
# ---------------------------------------------------------------------------------------------


def test_cost_report_fixture_exact_nano() -> None:
    result = read("anthropic-cost-report", fixture(COST))
    expect = manifest_entry(COST)["expect"]
    assert len(result.cost_lines) == expect["cost_lines"]
    assert sum(c.amount_nano for c in result.cost_lines) == expect["amount_nano"]
    assert result.stats["rounding_remainder_e18"] == 0
    assert result.capabilities == frozenset({"cost"})
    page = json.loads(fixture(COST).read_text())
    rows = [r for b in page["data"] for r in b["results"]]
    for line, row in zip(sorted(result.cost_lines, key=lambda c: c.amount_nano),
                         sorted(rows, key=lambda r: Decimal(r["amount"])), strict=True):
        assert line.amount_nano == cents_to_nano(row["amount"])[0]
    for line in result.cost_lines:
        assert line.channel == CHANNEL and line.source_kind == "anthropic.cost_report"
        assert line.finality == "final" and line.currency == "USD"
        assert line.principal is None and line.sku is None and line.list_amount_nano is None
    default_ws = [c for c in result.cost_lines if c.workspace_id is None]
    assert default_ws and all(c.model == "claude-haiku-4-5" for c in default_ws)
    assert all(c.inference_geo is None for c in default_ws)  # "not_available" → None
    web = [c for c in result.cost_lines if c.cost_type == "web_search"]
    assert web and all(c.model is None and c.token_type is None for c in web)


def test_many_decimal_cents_strings_exact(tmp_path: Path) -> None:
    amount = "123.456789012345678901234567"
    path = write_json(tmp_path / "c.json", _bucket([_cost_row(amount)]))
    result = read("anthropic-cost-report", path)
    (line,) = result.cost_lines
    exact = Decimal(amount) / 100  # USD
    assert line.amount_nano == 1_234_567_890
    rem = result.stats["rounding_remainder_e18"]
    assert rem == 123_456_789
    assert abs(Decimal(line.amount_nano) / 10**9 + Decimal(rem) / 10**18 - exact) < Decimal(
        "1e-18")


def test_cost_amount_as_json_number_is_exact(tmp_path: Path) -> None:
    # a JSON number whose binary float differs from the decimal text
    path = tmp_path / "c.json"
    page = _bucket([_cost_row("X")])
    text = json.dumps(page).replace('"X"', "12345678.123456789012345")  # cents, 23 digits
    path.write_text(text)
    result = read("anthropic-cost-report", path)
    (line,) = result.cost_lines
    assert line.amount_nano == 123_456_781_234_568
    # exact: 123456.78123456789012345 USD − 123456.781234568 = −1.0987655e-10 USD; a float
    # (≈ 17 significant digits) would give −1.1e-10
    assert result.stats["rounding_remainder_e18"] == -109_876_550


def test_context_windows_fold_into_one_line(tmp_path: Path) -> None:
    rows = [_cost_row("0.0000000001", context_window="0-200k"),
            _cost_row("12.5000000004", context_window="200k-1M")]
    result = read("anthropic-cost-report", write_json(tmp_path / "c.json", _bucket(rows)))
    (line,) = result.cost_lines
    assert line.amount_nano == 125_000_000
    assert result.stats["rounding_remainder_e18"] == 5_000_000


def test_cost_report_edge_fixture() -> None:
    rel = "anthropic/edge/cost_report_edge.json"
    result = read("anthropic-cost-report", fixture(rel))
    expect = manifest_entry(rel)["expect"]
    assert len(result.cost_lines) == expect["cost_lines"]
    assert sum(c.amount_nano for c in result.cost_lines) == expect["amount_nano"]
    assert result.stats["rounding_remainder_e18"] == expect["rounding_remainder_e18"] == 4500
    types = {c.cost_type for c in result.cost_lines}
    assert {"code_execution", "session_usage", "tokens", "web_search"} <= types
    folded = [c for c in result.cost_lines if c.description.endswith("Output Tokens (Fast)")]
    assert [c.amount_nano for c in folded] == [250_000_000]  # two context windows, exact sum


def test_edge_pair_token_lines_at_list_with_modifiers() -> None:
    """US-geo (1.1×) and fast-mode (Opus 5 $10/$50) lines equal FakePricer on the usage rows;
    the Priority Tier usage has no cost line (the cost report excludes it)."""
    usage = read("anthropic-usage-report", fixture("anthropic/edge/usage_report_edge.json"))
    cost = read("anthropic-cost-report", fixture("anthropic/edge/cost_report_edge.json"))
    pricer = FakePricer()
    priced: dict[tuple, int] = defaultdict(int)
    for agg in usage.aggregates:
        d = dims(agg)
        if d["service_tier"] == "priority":
            continue
        ctx = make_ctx(d["model"], inference_geo=d.get("inference_geo"),
                       speed=d.get("speed", "standard"))
        for line in pricer.price_usage(agg.usage, ctx, ts_ms=agg.bucket_start_ms).lines:
            if line.bucket in _TOKEN_TYPE:
                priced[(d["model"], _TOKEN_TYPE[line.bucket])] += line.amount_nano
            elif line.bucket == "web_search":
                priced[(None, "web_search")] += line.amount_nano
    invoiced = {(c.model, c.token_type or c.cost_type): c.amount_nano for c in cost.cost_lines
                if c.cost_type in ("tokens", "web_search")}
    assert invoiced == {k: v for k, v in priced.items() if v}


def test_cost_rows_rejected(tmp_path: Path) -> None:
    rows = [_cost_row("1"), _cost_row(None), _cost_row("abc"), _cost_row("1", currency="EUR"),
            _cost_row(True), _cost_row("1e999"), _cost_row("1E-400"),
            _cost_row("1", description="bad\x1b[31m")]
    result = read("anthropic-cost-report", write_json(tmp_path / "c.json", _bucket(rows)))
    assert len(result.cost_lines) == 1
    assert [q.reason for q in result.quarantined] == [
        "missing:amount", "bad_type:amount", "bad_type:currency", "bad_type:amount",
        "bad_type:amount", "bad_type:amount", "bad_type:description"]


def test_negative_adjustment_amounts_are_kept(tmp_path: Path) -> None:
    rows = [_cost_row("-250.5", cost_type="tokens")]
    (line,) = read("anthropic-cost-report", write_json(tmp_path / "c.json",
                                                       _bucket(rows))).cost_lines
    assert line.amount_nano == -2_505_000_000


# ---------------------------------------------------------------------------------------------
# the reconcilable pair: cost report == usage report priced at list (FakePricer, facts.json)
# ---------------------------------------------------------------------------------------------

_TOKEN_TYPE = {"uncached_input": "uncached_input_tokens", "output": "output_tokens",
               "cache_read": "cache_read_input_tokens",
               "cache_write_5m": "cache_creation.ephemeral_5m_input_tokens",
               "cache_write_1h": "cache_creation.ephemeral_1h_input_tokens"}


def test_recon_pair_prices_exactly_with_fake_pricer() -> None:
    usage = read("anthropic-usage-report", fixture(USAGE))
    cost = read("anthropic-cost-report", fixture(COST))
    pricer = FakePricer()
    priced: dict[tuple, int] = defaultdict(int)
    for agg in usage.aggregates:
        d = dims(agg)
        ctx = make_ctx(d["model"], channel=d["channel"], service_tier=d["service_tier"],
                       inference_geo=d.get("inference_geo"))
        result = pricer.price_usage(agg.usage, ctx, ts_ms=agg.bucket_start_ms)
        assert result.unpriced_reason is None
        day = date_of(agg.bucket_start_ms)
        for line in result.lines:
            assert line.exact
            if line.bucket == "web_search":
                priced[(day, d.get("workspace_id"), None, "web_search")] += line.amount_nano
            else:
                priced[(day, d.get("workspace_id"), d["model"], _TOKEN_TYPE[line.bucket])] += (
                    line.amount_nano)
    invoiced = {}
    for line in cost.cost_lines:
        key = (line.date_utc, line.workspace_id, line.model,
               line.token_type if line.cost_type == "tokens" else line.cost_type)
        invoiced[key] = line.amount_nano
    assert invoiced == {k: v for k, v in priced.items() if v}
    pair = MANIFEST["recon_pairs"][0]
    assert pair["usage"] == USAGE and pair["cost"] == COST


def test_now_default_zero_means_provisional() -> None:
    result = UsageReportAdapter().read(fixture(USAGE), opts(now_ms=0))
    assert {a.finality for a in result.aggregates} == {"provisional"}
    result = CostReportAdapter().read(fixture(COST), opts(now_ms=NOW_MS))
    assert {c.finality for c in result.cost_lines} == {"final"}

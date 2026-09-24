"""GCP billing export adapter (SPEC §5.13, D31; brief ADMIN acceptance 5)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from tokenbill.adapters.cloud_billing import GcpBillingExportAdapter
from tokenbill.core import catalog
from tokenbill.core.builders import CANARY
from tokenbill.core.errors import SourceError
from tokenbill.core.records import to_json

from .helpers import (
    assert_person_free,
    dims,
    fixture,
    h,
    manifest_entry,
    read,
    result_json,
    tokens,
    write_jsonl,
)

JSONL = "cloud/gcp_billing_2026-09.jsonl"
CSV = "cloud/gcp_billing_2026-09.csv"


def _row(**kw: object) -> dict:
    row = {"service": {"description": "Vertex AI"},
           "sku": {"id": "A1B2-0001", "description": "Claude Opus 5 Input Tokens"},
           "usage_start_time": "2026-09-01 10:00:00 UTC", "project": {"id": "acme-prod"},
           "labels": [{"key": "team", "value": "search"}],
           "location": {"region": "us-east5", "location": "us-east5"},
           "cost": "10", "currency": "USD", "usage": {"amount": 2_000_000, "unit": "tokens"},
           "credits": [{"amount": "-0.5", "type": "DISCOUNT"}], "cost_type": "regular"}
    row.update(kw)
    return row


def test_fixture_jsonl() -> None:
    result = read("gcp-billing", fixture(JSONL))
    expect = manifest_entry(JSONL)["expect"]
    assert len(result.cost_lines) == expect["cost_lines"]
    assert sum(c.amount_nano for c in result.cost_lines) == expect["amount_nano"]
    assert sum(c.list_amount_nano for c in result.cost_lines) == expect["list_amount_nano"]
    assert len(result.aggregates) == expect["aggregates"]
    assert tokens(result) == expect["usage_tokens"]
    for key in ("rows_skipped_not_claude", "rows_skipped_tax", "rows_unit_unknown"):
        assert result.stats[key] == expect[key]
    for line in result.cost_lines:
        assert line.source_kind == "gcp.billing_export" and line.channel == "vertex"
        assert line.workspace_id in (h("acme-ml-prod"), h("acme-ml-dev"))
        assert "Claude" in line.description and line.sku
    assert result.capabilities == frozenset({"aggregates", "cost", "attribution.team"})


def test_csv_equals_jsonl() -> None:
    a = read("gcp-billing", fixture(JSONL))
    b = read("gcp-billing", fixture(CSV))
    assert [to_json(x) for x in a.cost_lines] == [to_json(x) for x in b.cost_lines]
    assert [to_json(x) for x in a.aggregates] == [to_json(x) for x in b.aggregates]


def test_amount_is_cost_plus_credits(tmp_path: Path) -> None:
    rows = [_row(), _row(credits=[{"amount": "-0.25"}, {"amount": "-0.125"}],
                         sku={"id": "A1B2-0002", "description": "Claude Opus 5 Output Tokens"})]
    result = read("gcp-billing", write_jsonl(tmp_path / "g.jsonl", rows))
    by_sku = {c.sku: c for c in result.cost_lines}
    assert by_sku["A1B2-0001"].amount_nano == 9_500_000_000
    assert by_sku["A1B2-0001"].list_amount_nano == 10_000_000_000
    assert by_sku["A1B2-0002"].amount_nano == 9_625_000_000


def test_region_to_scope_and_labels_allowlist() -> None:
    result = read("gcp-billing", fixture(JSONL))
    scopes = {c.sku: c.endpoint_scope for c in result.cost_lines}
    assert scopes["A1B2-C3D4-0001"] == "global"
    assert scopes["A1B2-C3D4-0002"] == "regional"
    assert scopes["A1B2-C3D4-0009"] is None
    assert [n.count for n in result.notes if n.code == "dq.scope_unknown"] == [2]
    teams = {dims(a).get("team") for a in result.aggregates}
    assert teams == {"search", "data"}
    assert [n.count for n in result.notes if n.code == "dq.unknown_fields"] == [6]
    assert CANARY not in result_json(result)
    assert_person_free(result, "acme-ml-prod", "ACME ML")


def test_unmapped_sku_and_verified_rule(tmp_path: Path, monkeypatch) -> None:
    rows = [_row()]
    result = read("gcp-billing", write_jsonl(tmp_path / "g.jsonl", rows))
    (agg,) = result.aggregates
    assert dims(agg)["sku"] == "A1B2-0001" and agg.usage.uncached_input == 2_000_000
    assert [n.code for n in result.notes] == ["dq.unmapped_sku"]
    rule = catalog.SkuRule(source_kind="gcp.billing_export", pattern=r"A1B2-0001",
                           model="claude-opus-5@20260724", bucket="output",
                           endpoint_scope="global", service_tier="standard", unit_tokens=1,
                           verified=True, source="test")
    monkeypatch.setattr(catalog, "SKU_RULES", (rule,))
    result = read("gcp-billing", write_jsonl(tmp_path / "g2.jsonl",
                                             [_row(sku={"id": "A1B2-0001",
                                                        "description": "SKU 1"})]))
    (agg,) = result.aggregates
    assert agg.usage.output == 2_000_000 and "sku" not in dims(agg)
    assert dims(agg)["model"] == "claude-opus-5"                  # normalized Vertex id
    (line,) = result.cost_lines
    assert (line.model, line.token_type, line.service_tier) == ("claude-opus-5", "output",
                                                                "standard")
    assert line.endpoint_scope == "regional"                    # region beats the rule


def test_units_from_pricing_units_or_rule(tmp_path: Path, monkeypatch) -> None:
    rows = [_row(usage={"amount": 9, "unit": "count", "amount_in_pricing_units": "0.5",
                        "pricing_unit": "1M tokens"}),
            _row(usage={"amount": 9, "unit": "count"},
                 sku={"id": "Z-2", "description": "Claude Z"})]
    result = read("gcp-billing", write_jsonl(tmp_path / "g.jsonl", rows))
    assert tokens(result) == 500_000
    assert result.stats["rows_unit_unknown"] == 1
    rule = dataclasses.replace(catalog.SKU_RULES[0], source_kind="gcp.billing_export",
                               pattern="Z-2", verified=True, unit_tokens=1000)
    monkeypatch.setattr(catalog, "SKU_RULES", (rule,))
    result = read("gcp-billing", write_jsonl(tmp_path / "g2.jsonl", rows[1:]))
    assert tokens(result) == 9_000


def test_non_claude_tax_and_bad_rows(tmp_path: Path) -> None:
    rows = [_row(), _row(sku={"id": "G-1", "description": "Gemini 3 Flash"}),
            _row(cost_type="tax"), _row(cost_type="adjustment"),
            _row(cost=None), _row(currency="EUR"), _row(usage_start_time=None),
            _row(credits="nope"), _row(credits=[{"x": 1}]), _row(labels=5),
            _row(labels=[3]), _row(cost="abc")]
    path = write_jsonl(tmp_path / "g.jsonl", rows)
    (path.parent / "x").mkdir()
    result = read("gcp-billing", path)
    assert result.stats["rows_skipped_not_claude"] == 1
    assert result.stats["rows_skipped_tax"] == 1
    adjustments = [c for c in result.cost_lines if c.cost_type == "adjustment"]
    assert len(adjustments) == 1
    assert len(result.aggregates) == 1                       # adjustment rows carry no tokens
    assert [q.reason for q in result.quarantined] == [
        "missing:cost", "bad_type:currency", "missing:usage_start_time", "bad_type:credits",
        "bad_type:credits", "bad_type:labels", "bad_type:labels", "bad_type:cost"]


def test_csv_flat_label_columns_and_sniff(tmp_path: Path) -> None:
    path = tmp_path / "g.csv"
    path.write_text("sku_id,sku_description,usage_start_time,project_id,labels.team,cost,"
                    "credits_amount,location_region,usage_amount,usage_unit\n"
                    "S-1,Claude Opus 5 Input Tokens,2026-09-01T00:00:00Z,p1,infra,2.5,-0.5,"
                    "global,1000,tokens\n")
    adapter = GcpBillingExportAdapter()
    assert adapter.sniff(path, path.read_bytes())
    result = read("gcp-billing", path)
    (line,) = result.cost_lines
    assert line.amount_nano == 2_000_000_000 and line.endpoint_scope == "global"
    (agg,) = result.aggregates
    assert dims(agg)["team"] == "infra"


def test_bad_json_lines_and_strict(tmp_path: Path) -> None:
    path = tmp_path / "g.jsonl"
    path.write_text(json.dumps(_row()) + "\n{broken\n[1]\n")
    result = read("gcp-billing", path)
    assert len(result.cost_lines) == 1
    assert [q.reason for q in result.quarantined] == ["bad_json", "not_object"]
    with pytest.raises(SourceError, match="line:2"):
        read("gcp-billing", path, lenient=False)
